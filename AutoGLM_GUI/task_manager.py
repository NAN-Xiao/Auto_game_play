"""Task orchestration and execution."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

from AutoGLM_GUI.config import MemoryPolicy
from AutoGLM_GUI.logger import logger
from AutoGLM_GUI.metrics import record_trace_latency_metrics
from AutoGLM_GUI.task_store import (
    TERMINAL_TASK_STATUSES,
    TaskEventRecord,
    TaskRecord,
    TaskSessionRecord,
    TaskStatus,
    TaskStore,
    task_store,
)
import AutoGLM_GUI.trace as trace_module

TaskExecutor = Callable[[TaskRecord], Awaitable[None]]
TaskImageAttachment = dict[str, Any]
TaskExperiencePayload = dict[str, Any]
EXPERIENCE_SUMMARY_MAX_CONCURRENCY = 1
ITERATIVE_OBSERVATION_HINTS = (
    "观看",
    "浏览",
    "阅读",
    "观察",
    "停留",
    "等待",
    "体验",
    "查看",
    "播放",
    "看",
    "足够时长",
    "一段时间",
)
ITERATIVE_ITEM_HINTS = (
    "每个",
    "每条",
    "每一",
    "逐个",
    "逐条",
    "依次",
    "一个个",
    "多个",
    "多条",
)
ITERATIVE_SUMMARY_HINTS = (
    "提炼",
    "总结",
    "归纳",
    "分析",
    "判断",
    "识别",
    "记录",
    "获取",
    "收集",
    "提取",
    "内容",
    "信息",
    "文案",
    "字幕",
    "文字",
    "要点",
    "结论",
)
ITERATIVE_SWITCH_HINTS = (
    "下一个",
    "下一条",
    "下一页",
    "切换",
    "滑动",
    "翻页",
    "继续",
    "换下一个",
)
OBSERVATION_WINDOW_RULE_HINTS = (
    "间隔截屏",
    "间隔截图",
    "连续截图",
    "连续画面",
    "多帧",
    "多图",
    "多次截图",
    "观察窗口",
    "采样",
)
DYNAMIC_OBSERVATION_SUBJECT_HINTS = (
    "app",
    "应用",
    "视频",
    "抖音",
    "短视频",
    "游戏",
    "直播",
    "动画",
    "播放",
    "战斗",
    "关卡",
    "动态",
    "连续画面",
    "画面变化",
    "页面变化",
)
STATEFUL_FLOW_HINTS = (
    "app",
    "应用",
    "软件",
    "游戏",
    "游玩",
    "玩法",
    "关卡",
    "战斗",
    "任务",
    "剧情",
    "新手",
    "引导",
    "登录",
    "注册",
    "表单",
    "流程",
    "聊天",
    "通讯",
    "消息",
    "对话",
    "客服",
    "背包",
    "资源",
    "血量",
    "等级",
    "进度",
)
INDEPENDENT_ITEM_HINTS = (
    "视频",
    "短视频",
    "抖音",
    "商品",
    "帖子",
    "新闻",
    "文章",
    "列表",
    "信息流",
)


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _looks_like_iterative_observation_task(text: str) -> bool:
    compact_text = "".join(text.split())
    has_sampling_rule = (
        "至少3次" in compact_text or "至少三次" in compact_text or "多次" in text
    ) and _contains_any(text, ("对象", "截图", "观察", "采样", "证据"))
    has_iteration = _contains_any(text, ITERATIVE_ITEM_HINTS)
    has_observation = _contains_any(text, ITERATIVE_OBSERVATION_HINTS)
    has_summary = _contains_any(text, ITERATIVE_SUMMARY_HINTS)
    has_switch = _contains_any(text, ITERATIVE_SWITCH_HINTS)
    return has_sampling_rule or (
        (has_iteration or has_switch) and has_observation and has_summary
    )


def _looks_like_observation_window_task(text: str) -> bool:
    has_explicit_window_rule = _contains_any(text, OBSERVATION_WINDOW_RULE_HINTS)
    has_dynamic_subject = _contains_any(text, DYNAMIC_OBSERVATION_SUBJECT_HINTS)
    has_observation = _contains_any(text, ITERATIVE_OBSERVATION_HINTS)
    return (
        has_explicit_window_rule
        or _looks_like_iterative_observation_task(text)
        or (has_dynamic_subject and has_observation)
    )


def _infer_memory_policy(text: str) -> MemoryPolicy:
    if _contains_any(text, STATEFUL_FLOW_HINTS):
        return "stateful_flow"
    if _looks_like_iterative_observation_task(text) and _contains_any(
        text, INDEPENDENT_ITEM_HINTS
    ):
        return "independent_items"
    return "hybrid"


class TaskManager:
    """Queue-backed task manager with per-device workers."""

    def __init__(self, store: TaskStore = task_store):
        self.store = store
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._abort_handlers: dict[
            str, Callable[[], Any] | Callable[[], Awaitable[Any]]
        ] = {}
        self._running_executor_tasks: dict[str, asyncio.Task[None]] = {}
        self._completion_events: dict[str, asyncio.Event] = {}
        self._cancel_requested: set[str] = set()
        self._executor_cancel_requested: set[str] = set()
        self._executors: dict[str, TaskExecutor] = {}
        self._experience_summary_tasks: dict[str, asyncio.Task[None]] = {}
        self._experience_summary_rerun_requested: set[str] = set()
        self._experience_summary_include_partial: set[str] = set()
        self._experience_summary_semaphore: asyncio.Semaphore | None = None
        self._started = False
        self._takeover_sessions: dict[str, bool] = {}
        self._shutdown = False
        self.register_executor("classic_chat", self._execute_classic_chat)
        self.register_executor("layered_chat", self._execute_layered_chat)
        self.register_executor("experience_report", self._execute_experience_report)
        self.register_executor("scheduled_workflow", self._execute_scheduled_workflow)
        self.register_executor(
            "scheduled_layered_workflow", self._execute_scheduled_layered_workflow
        )

    def register_executor(self, executor_key: str, executor: TaskExecutor) -> None:
        self._executors[executor_key] = executor

    async def start(self) -> None:
        if self._started:
            return
        self._shutdown = False
        interrupted = await asyncio.to_thread(self.store.mark_running_tasks_interrupted)
        if interrupted:
            logger.warning(f"Recovered {interrupted} interrupted task(s)")
        for device_id in await asyncio.to_thread(self.store.get_queued_device_ids):
            self._ensure_worker(device_id)
        self._started = True

    async def shutdown(self) -> None:
        self._shutdown = True
        workers = list(self._workers.values())
        self._workers.clear()
        self._running_executor_tasks.clear()
        self._executor_cancel_requested.clear()
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        summary_tasks = list(self._experience_summary_tasks.values())
        self._experience_summary_tasks.clear()
        self._experience_summary_rerun_requested.clear()
        self._experience_summary_include_partial.clear()
        for summary_task in summary_tasks:
            summary_task.cancel()
        if summary_tasks:
            await asyncio.gather(*summary_tasks, return_exceptions=True)
        self._started = False

    async def create_chat_session(
        self, *, device_id: str, device_serial: str, mode: str = "classic"
    ) -> TaskSessionRecord:
        return await asyncio.to_thread(
            self.store.create_session,
            kind="chat",
            mode=mode,
            device_id=device_id,
            device_serial=device_serial,
        )

    async def get_session(self, session_id: str) -> TaskSessionRecord | None:
        return await asyncio.to_thread(self.store.get_session, session_id)

    async def get_or_create_legacy_chat_session(
        self, *, device_id: str, device_serial: str, mode: str = "classic"
    ) -> TaskSessionRecord:
        session = await asyncio.to_thread(
            self.store.get_latest_open_chat_session,
            device_id=device_id,
            device_serial=device_serial,
            mode=mode,
        )
        if session:
            return session
        return await self.create_chat_session(
            device_id=device_id,
            device_serial=device_serial,
            mode=mode,
        )

    async def archive_session(self, session_id: str) -> TaskSessionRecord | None:
        session = await self.get_session(session_id)
        if session is None:
            return None
        archived = await asyncio.to_thread(self.store.archive_session, session_id)
        if archived is not None:
            # Clean up the contextual agent for this session to prevent memory leak.
            # The agent key pattern is "device_id:chat:session_id".
            device_id = str(archived["device_id"])
            context = f"chat:{session_id}"
            try:
                from AutoGLM_GUI.phone_agent_manager import PhoneAgentManager

                manager = PhoneAgentManager.get_instance()
                manager.destroy_agent(device_id, context=context)
            except Exception as exc:
                logger.debug(
                    f"Contextual agent cleanup skipped for {device_id}/{context}: {exc}"
                )
        return archived

    async def submit_chat_task(
        self,
        *,
        session_id: str,
        device_id: str,
        device_serial: str,
        message: str,
        attachments: list[TaskImageAttachment] | None = None,
        experience: TaskExperiencePayload | None = None,
    ) -> TaskRecord:
        session = await self.get_session(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")

        session_mode = str(session["mode"])
        executor_key = {
            "classic": "classic_chat",
            "layered": "layered_chat",
        }.get(session_mode)
        if executor_key is None:
            raise ValueError(f"Unsupported session mode: {session_mode}")

        source_task: TaskRecord | None = None
        if experience is None and self._looks_like_report_request(
            message.strip().lower()
        ):
            source_task = await asyncio.to_thread(
                self.store.get_latest_reportable_session_task,
                session_id,
            )
            if source_task is not None:
                executor_key = "experience_report"

        task = await asyncio.to_thread(
            self.store.create_task_run,
            source="chat",
            executor_key=executor_key,
            session_id=session_id,
            device_id=device_id,
            device_serial=device_serial,
            input_text=message,
        )
        await asyncio.to_thread(
            self.store.append_event,
            task_id=task["id"],
            event_type="user_message",
            role="user",
            payload={
                "message": message,
                "attachments": attachments or [],
                "experience": experience,
            },
        )
        if source_task is not None:
            await asyncio.to_thread(
                self.store.append_event,
                task_id=task["id"],
                event_type="experience_report_source",
                role="system",
                payload={
                    "source_task_id": str(source_task["id"]),
                    "source_stop_reason": source_task.get("stop_reason"),
                    "source_step_count": int(source_task.get("step_count") or 0),
                    "message": "Using retained app experience as report context",
                },
            )
        self._completion_events[task["id"]] = asyncio.Event()
        self._ensure_worker(device_id)
        return task

    @staticmethod
    def _looks_like_report_request(message: str) -> bool:
        continuation_keywords = (
            "继续",
            "接着",
            "再跑",
            "再执行",
            "重新",
            "重跑",
            "换成",
            "改成",
            "调整策略",
            "改变策略",
            "按这个策略",
            "下次",
            "接下来",
            "继续看",
            "继续体验",
            "继续执行",
            "continue",
            "resume",
            "rerun",
            "restart",
            "change strategy",
            "adjust strategy",
        )
        past_context_keywords = (
            "刚才",
            "刚刚",
            "上次",
            "之前",
            "前面",
            "上一轮",
            "刚看",
            "刚做",
            "刚跑",
            "刚体验",
            "刚操作",
            "previous",
            "earlier",
            "just watched",
            "just did",
        )
        recall_keywords = (
            "看了什么",
            "做了什么",
            "浏览了什么",
            "体验了什么",
            "讲了什么",
            "什么视频",
            "什么内容",
            "介绍",
            "结论",
            "详细讲",
            "展开",
            "列一下",
            "是哪几个",
            "有哪些",
            "第一个",
            "第二个",
            "第三个",
            "上一个",
            "最后一个",
            "what did",
            "what have",
            "what was",
            "which",
            "details",
        )
        positive_keywords = (
            "报告",
            "汇报",
            "总结",
            "汇总",
            "复盘",
            "评测",
            "评价",
            "分析",
            "输出",
            "格式",
            "按以下",
            "按照",
            "生成",
            "整理",
            "report",
            "summary",
            "summarize",
            "review",
            "evaluate",
            "evaluation",
            "format",
            "analysis",
        )
        if any(keyword in message for keyword in continuation_keywords):
            return False
        operation_keywords = (
            "点击",
            "打开",
            "进入",
            "返回",
            "观看",
            "看视频",
            "浏览",
            "体验",
            "测试",
            "做一个",
            "运行",
            "执行",
            "启动",
            "试玩",
            "玩一下",
            "滑动",
            "输入",
            "搜索",
            "切换",
            "切下一个",
            "下一个",
            "关闭",
            "安装",
            "卸载",
            "登录",
            "tap",
            "click",
            "open",
            "swipe",
            "scroll",
            "watch",
            "browse",
            "test",
            "run",
            "play",
            "next",
            "type",
            "input",
            "search",
        )
        has_past_context = any(keyword in message for keyword in past_context_keywords)
        has_recall = any(keyword in message for keyword in recall_keywords)
        has_positive = any(keyword in message for keyword in positive_keywords)
        if has_past_context and (has_recall or has_positive):
            return True
        if not has_positive:
            return False
        has_operation = any(keyword in message for keyword in operation_keywords)
        if has_operation and not has_past_context:
            return False
        return True

    def _get_task_user_image_attachments(
        self, task_id: str
    ) -> list[TaskImageAttachment]:
        events = self.store.list_task_events(task_id)
        for event in events:
            if event["event_type"] != "user_message":
                continue
            payload = event.get("payload", {})
            attachments = payload.get("attachments")
            if not isinstance(attachments, list):
                return []
            return [
                attachment
                for attachment in attachments
                if isinstance(attachment, dict)
                and isinstance(attachment.get("mime_type"), str)
                and isinstance(attachment.get("data"), str)
            ]
        return []

    def _get_task_experience_payload(
        self, task_id: str
    ) -> TaskExperiencePayload | None:
        events = self.store.list_task_events(task_id)
        for event in events:
            if event["event_type"] != "user_message":
                continue
            payload = event.get("payload", {})
            experience = payload.get("experience")
            if isinstance(experience, dict):
                return dict(experience)
            return None
        return None

    @staticmethod
    def _build_experience_semantic_context(
        original_goal: str,
        experience: TaskExperiencePayload,
    ) -> str:
        plan = experience.get("plan")
        if not isinstance(plan, dict):
            return original_goal

        def _list_values(values: Any) -> list[str]:
            if not isinstance(values, list):
                return []
            return [str(value) for value in values if str(value).strip()]

        return "\n".join(
            [
                original_goal,
                str(plan.get("execution_goal") or ""),
                "\n".join(_list_values(plan.get("observation_targets"))),
                "\n".join(_list_values(plan.get("analysis_lenses"))),
                "\n".join(_list_values(plan.get("stop_conditions"))),
                "\n".join(_list_values(plan.get("sampling_strategy"))),
            ]
        )

    @staticmethod
    def _experience_requires_observation_window(
        original_goal: str,
        experience: TaskExperiencePayload,
    ) -> bool:
        semantic_context = TaskManager._build_experience_semantic_context(
            original_goal,
            experience,
        )
        return _looks_like_observation_window_task(semantic_context)

    @staticmethod
    def _build_observation_window_description(
        *,
        observation_window_screenshot_count: int | None = None,
        observation_window_interval_seconds: float | None = None,
    ) -> str:
        if (
            observation_window_screenshot_count is not None
            and observation_window_interval_seconds is not None
        ):
            return (
                f"按当前系统观察窗口配置完成 {observation_window_screenshot_count} "
                f"张截图、间隔 {observation_window_interval_seconds:g} 秒的多帧采样"
            )
        return "按当前系统观察窗口配置完成多帧间隔截图"

    @staticmethod
    def _build_iterative_observation_execution_rules(
        observation_window_description: str,
    ) -> list[str]:
        return [
            "- 逐项对象门槛只在你根据用户语义判断存在“逐个对象/每个对象/依次对象 + 停留观察 + 提炼记录 + 切换下一个”结构时启用。",
            "- 启用逐项对象门槛后，必须按循环执行：当前对象观察窗口采样 -> 当前对象综合小结 -> 带 OBJECT_SUMMARY 的切换动作 -> 新对象重新采样。",
            f"- 启用逐项对象门槛后，每个对象都要先完成观察窗口采样；观察窗口会{observation_window_description}。",
            "- 启用逐项对象门槛后，每一步思考里都要显式维护对象边界：当前对象是什么、当前观察窗口是否已足以小结、是否已经满足切换条件。",
            "- 启用逐项对象门槛后，未完成当前对象的观察窗口综合分析前，禁止使用 Swipe、Tap、翻页、滑动、点击“下一个”或任何会切换到下一个对象的动作。",
            "- 启用逐项对象门槛后，先形成当前对象的可见小结；小结重点必须服从用户原始目标和对象类型，不要把任务固定成视频总结，也不要预设 UI、数据、来源、互动指标一定是主体或补充。只有完成当前对象小结后，才允许切换到下一个对象。",
            '- 启用逐项对象门槛后，小结不能只写在思考中。准备切换下一个对象时，本次 do(...) 动作必须携带 message="OBJECT_SUMMARY: ..."，把当前对象小结写入 message；如果使用 Tap、Swipe、Back、列表选择或点击屏幕中部等方式切换对象，同样必须带这个 message。',
            "- OBJECT_SUMMARY 内容必须是给用户看的结论，不要写“我将要总结/我需要记录”这类过程话；根据用户语义自动选择结构：视频任务偏内容/文案/画面/观点，App 体验偏流程/交互/状态变化/问题，游戏任务偏目标/操作反馈/关卡机制/体验卡点，商品/帖子/页面偏主体内容/关键信息/判断依据。",
            "- UI、控件、状态、布局、文案、账号/来源、时间、价格、互动数据等是否是核心，必须由用户目标决定；App、游戏、通讯、流程、体验任务中，UI 与交互状态通常就是核心证据。只有当这些信息和用户目标无关时，才把它们降为背景或忽略。",
            "- 启用逐项对象门槛后，如果观察窗口内画面完全静止，也要把“无明显变化”记录为证据；这不等于可以跳过当前对象小结。",
            "- 启用逐项对象门槛后，切换后必须把新对象当作新的观察对象重新计数，不能把上一个对象的截图/证据混入当前对象。",
            "- 启用逐项对象门槛后，如果用户没有明确指定“只处理一个/只看当前/完成 1 个就停”，禁止在只完成 1 个对象后 finish；遇到“每个/下一个/继续/同样总结”时，默认这是开放式循环，不是单对象任务。",
            "- 启用逐项对象门槛后，未指定明确数量上限时，至少完成 2 个不同对象的可见小结后才允许考虑是否达到停止条件；如果用户语义是持续处理后续对象，应继续切换和总结，直到用户停止、系统限制、或连续多次无法进入新对象。",
            "- 启用逐项对象门槛后，finish 只能在满足明确停止条件时使用；finish message 必须列出已完成的每个对象小结和停止原因，禁止用“已切换到下一个，用户可以继续观看/浏览”这类话术结束任务。",
        ]

    @staticmethod
    def _build_memory_policy_execution_rules(memory_policy: str | None) -> list[str]:
        if memory_policy == "independent_items":
            return [
                "- 记忆方式：独立对象模式。每轮只基于原始委托、当前观察窗口和当前界面判断当前对象；历史截图、thinking、小结和动作只用于落库和最终报告，不要依赖上一对象内容来判断当前对象。"
            ]
        if memory_policy == "stateful_flow":
            return [
                "- 记忆方式：连续状态模式。每轮都要维护必要运行态记忆：当前目标、已完成节点、当前状态、上一步操作结果、失败/重试原因、需要避免重复的动作；截图和详细轨迹仍以任务事件为准用于最终报告。"
            ]
        return [
            "- 记忆方式：混合模式。当前对象内结论主要看本轮观察窗口；但要保留必要运行态记忆，例如已处理数量、切换是否成功、当前目标、阶段进度和需要避免重复的动作。"
        ]

    @staticmethod
    def _build_chat_execution_input(
        original_goal: str,
        *,
        observation_window_screenshot_count: int | None = None,
        observation_window_interval_seconds: float | None = None,
    ) -> str:
        requires_iterative_sampling = _looks_like_iterative_observation_task(
            original_goal
        )
        requires_observation_window = _looks_like_observation_window_task(original_goal)
        if not (requires_iterative_sampling or requires_observation_window):
            return original_goal

        observation_window_description = (
            TaskManager._build_observation_window_description(
                observation_window_screenshot_count=observation_window_screenshot_count,
                observation_window_interval_seconds=observation_window_interval_seconds,
            )
        )
        sections = [
            "你现在在执行一次 Android 自然语言任务。",
            "请根据用户原始委托的语义自行拆解执行结构，不要把持续观察任务当成一次性问答。",
            "",
            f"原始委托：{original_goal}",
            "",
            "语义拆解协议：",
            "- 先从原始委托中判断观察对象、执行阶段、证据要求、切换条件和最终产物；观察对象可以是视频、商品、帖子、页面、关卡、任务、流程阶段或任意用户语义对象。",
            "- 如果用户要求逐个/每个/依次处理某类对象，并要求停留、观看、浏览、观察、提炼、总结、记录或分析，就必须按“当前对象内取证 -> 当前对象结论 -> 切换下一个对象”的循环执行。",
            "- 如果用户没有要求逐项对象，就按关键路径或阶段取证，不要机械套用逐项循环。",
            "",
            "执行要求：",
            *TaskManager._build_memory_policy_execution_rules("hybrid"),
            *(
                [
                    "- 后端语义预判：当前任务包含动态/连续画面观察语义；本次会先按配置采集同一对象的多帧截图，再让模型综合判断下一步。"
                ]
                if requires_observation_window
                else []
            ),
            *(
                [
                    "- 后端语义预判：当前任务包含逐项对象观察语义，请启用下面的逐项对象边界和切换门槛。"
                ]
                if requires_iterative_sampling
                else []
            ),
            f"- 如果当前任务需要动态/连续画面观察，先综合同一轮观察窗口内的多张图；观察窗口会{observation_window_description}。",
            *(
                TaskManager._build_iterative_observation_execution_rules(
                    observation_window_description
                )
                if requires_iterative_sampling
                else []
            ),
            "- 完成用户明确要求的范围后可以结束任务；如果这是开放式逐项循环任务，不要主动提前结束。",
        ]
        return "\n".join(sections)

    @staticmethod
    def _build_experience_execution_input(
        original_goal: str,
        experience: TaskExperiencePayload,
        *,
        observation_window_screenshot_count: int | None = None,
        observation_window_interval_seconds: float | None = None,
    ) -> str:
        plan = experience.get("plan")
        if not isinstance(plan, dict):
            return original_goal

        def _list_lines(values: Any) -> list[str]:
            if not isinstance(values, list):
                return []
            return [f"- {str(value)}" for value in values if str(value).strip()]

        sampling_strategy = _list_lines(plan.get("sampling_strategy"))
        semantic_context = TaskManager._build_experience_semantic_context(
            original_goal,
            experience,
        )
        requires_iterative_sampling = _looks_like_iterative_observation_task(
            semantic_context
        )
        requires_observation_window = _looks_like_observation_window_task(
            semantic_context
        )
        memory_policy = str(plan.get("memory_policy") or "hybrid")
        observation_window_description = (
            TaskManager._build_observation_window_description(
                observation_window_screenshot_count=observation_window_screenshot_count,
                observation_window_interval_seconds=observation_window_interval_seconds,
            )
        )

        sections = [
            "你现在在执行一次 Android 应用/游戏体验任务。",
            "请围绕下面已经确认的体验委托去探索、观察、取证，并在关键节点形成稳定结论。",
            "你需要根据用户自然语言语义自行拆解执行结构，而不是套固定任务类型。",
            "",
            f"原始委托：{original_goal}",
            f"体验目标：{str(plan.get('execution_goal') or original_goal)}",
            "",
            "重点观察：",
            *(_list_lines(plan.get("observation_targets")) or ["- 关键页面变化"]),
            "",
            "分析方法：",
            *(_list_lines(plan.get("analysis_lenses")) or ["- 关键问题归纳"]),
            "",
            "评估维度：",
            *(_list_lines(plan.get("evaluation_dimensions")) or ["- 综合体验"]),
            "",
            "输出要求：",
            f"- {str(plan.get('report_request') or '输出体验分析报告')}",
            "",
            "停止条件：",
            *(_list_lines(plan.get("stop_conditions")) or ["- 覆盖关键路径后停止"]),
            "",
            "取证策略：",
            *(sampling_strategy or ["- 保留关键截图与证据"]),
            "",
            "记忆策略：",
            *TaskManager._build_memory_policy_execution_rules(memory_policy),
            "",
            "语义拆解协议：",
            "- 先从原始委托中判断观察对象、执行阶段、证据要求、切换条件和最终产物；观察对象可以是视频、商品、帖子、页面、关卡、任务、流程阶段或任意用户语义对象。",
            "- 如果用户要求逐个/每个/依次处理某类对象，并要求停留、观看、浏览、观察、提炼、总结、记录或分析，就必须按“当前对象内取证 -> 当前对象结论 -> 切换下一个对象”的循环执行。",
            "- 如果用户没有要求逐项对象，就按关键路径或阶段取证，不要机械套用逐项循环。",
            "",
            "执行要求：",
            "- 优先覆盖和委托目标直接相关的关键路径，不要盲目扩散。",
            "- 遇到关键文案、数值、付费点、任务节点时要重点观察。",
            *(
                [
                    "- 后端语义预判：当前任务包含动态/连续画面观察语义；本次会先按配置采集同一对象的多帧截图，再让模型综合判断下一步。"
                ]
                if requires_observation_window
                else []
            ),
            *(
                [
                    "- 后端语义预判：当前任务包含逐项对象观察语义，请启用下面的逐项对象边界和切换门槛。"
                ]
                if requires_iterative_sampling
                else []
            ),
            f"- 如果当前任务需要动态/连续画面观察，先综合同一轮观察窗口内的多张图；观察窗口会{observation_window_description}。",
            *(
                TaskManager._build_iterative_observation_execution_rules(
                    observation_window_description
                )
                if requires_iterative_sampling
                else []
            ),
            *(
                [
                    "- 开放式逐项循环任务不要主动提前结束；后续会在用户停止、系统限制或明确无法继续时基于轨迹生成报告。"
                ]
                if requires_iterative_sampling
                else ["- 完成关键覆盖后可以结束任务，后续会基于你的轨迹自动生成报告。"]
            ),
        ]
        return "\n".join(sections)

    async def enqueue_scheduled_task(
        self,
        *,
        scheduled_task_id: str,
        workflow_uuid: str,
        device_id: str,
        device_serial: str,
        input_text: str,
        schedule_fire_id: str,
        executor_key: str = "scheduled_workflow",
    ) -> TaskRecord:
        task = await asyncio.to_thread(
            self.store.create_task_run,
            source="scheduled",
            executor_key=executor_key,
            scheduled_task_id=scheduled_task_id,
            workflow_uuid=workflow_uuid,
            schedule_fire_id=schedule_fire_id,
            device_id=device_id,
            device_serial=device_serial,
            input_text=input_text,
        )
        self._completion_events[task["id"]] = asyncio.Event()
        self._ensure_worker(device_id)
        return task

    async def wait_for_task(
        self, task_id: str, timeout: float | None = None
    ) -> TaskRecord | None:
        task = await asyncio.to_thread(self.store.get_task, task_id)
        if task is None:
            return None
        if task["status"] in TERMINAL_TASK_STATUSES:
            return task

        event = self._completion_events.setdefault(task_id, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            return await asyncio.to_thread(self.store.get_task, task_id)
        return await asyncio.to_thread(self.store.get_task, task_id)

    async def cancel_task(self, task_id: str) -> TaskRecord | None:
        task = await asyncio.to_thread(self.store.get_task, task_id)
        if task is None:
            return None

        status = task["status"]
        if status in TERMINAL_TASK_STATUSES:
            return task

        if status == TaskStatus.QUEUED.value:
            updated = await asyncio.to_thread(self.store.cancel_queued_task, task_id)
            if updated:
                self._mark_task_complete(task_id)
            return updated

        if status == TaskStatus.RUNNING.value:
            self._cancel_requested.add(task_id)
            await self._append_cancel_requested_event(task_id)
            handler = self._abort_handlers.get(task_id)
            if handler is not None:
                result = handler()
                if inspect.isawaitable(result):
                    await result
            executor_task = self._running_executor_tasks.get(task_id)
            if executor_task is not None and not executor_task.done():
                self._executor_cancel_requested.add(task_id)
                executor_task.cancel()
            return await asyncio.to_thread(self.store.get_task, task_id)

        return task

    async def cancel_latest_chat_task(
        self, device_id: str, mode: str | None = None
    ) -> TaskRecord | None:
        task = await asyncio.to_thread(
            self.store.get_latest_active_chat_task, device_id, mode
        )
        if task is None:
            return None
        return await self.cancel_task(task["id"])

    def _ensure_worker(self, device_id: str) -> None:
        if self._shutdown:
            return
        worker = self._workers.get(device_id)
        if worker is None or worker.done():
            self._workers[device_id] = asyncio.create_task(
                self._device_worker(device_id),
                name=f"TaskWorker-{device_id}",
            )

    @staticmethod
    def _register_abort_handler(
        manager: Any,
        device_id: str,
        handler: Callable[[], Any] | Callable[[], Awaitable[Any]],
        *,
        context: str,
    ) -> None:
        try:
            manager.register_abort_handler(device_id, handler, context=context)
        except TypeError:
            manager.register_abort_handler(device_id, handler)

    @staticmethod
    def _unregister_abort_handler(
        manager: Any,
        device_id: str,
        *,
        context: str,
    ) -> None:
        try:
            manager.unregister_abort_handler(device_id, context=context)
        except TypeError:
            manager.unregister_abort_handler(device_id)

    async def _record_trace_artifacts(
        self,
        *,
        task_id: str,
        trace_id: str,
        metrics_source: str,
        step_count: int,
        total_duration_ms: int,
    ) -> None:
        try:
            step_summaries = trace_module.list_step_timing_summaries(trace_id=trace_id)
            trace_summary_dict = trace_module.get_trace_timing_summary(
                trace_id=trace_id,
                total_duration_ms=total_duration_ms,
                steps=step_count,
            )
            if trace_summary_dict is not None:
                await self._append_task_event(
                    task_id=task_id,
                    event_type="trace_summary",
                    payload={
                        "summary": trace_summary_dict,
                        "step_summaries": step_summaries,
                    },
                    role="system",
                    trace_id=trace_id,
                    replay_source=metrics_source,
                )
            record_trace_latency_metrics(
                source=metrics_source,
                trace_summary=trace_summary_dict,
                step_summaries=step_summaries,
            )
        except Exception:
            logger.warning(
                "Failed to persist trace artifacts for task %s",
                task_id,
                exc_info=True,
            )

    async def _write_replay_task_start(
        self,
        *,
        task: TaskRecord,
        trace_id: str,
        source: str,
    ) -> None:
        replay_task = {**task, "trace_id": trace_id}
        await asyncio.to_thread(
            trace_module.write_replay_task_start,
            task_id=str(task["id"]),
            trace_id=trace_id,
            task=replay_task,
            source=source,
        )

    async def _append_task_event(
        self,
        *,
        task_id: str,
        event_type: str,
        payload: dict[str, Any],
        role: str = "assistant",
        trace_id: str | None = None,
        replay_source: str | None = None,
        task: TaskRecord | None = None,
    ) -> TaskEventRecord:
        event_record = await asyncio.to_thread(
            self.store.append_event,
            task_id=task_id,
            event_type=event_type,
            payload=payload,
            role=role,
        )
        if trace_id and replay_source:
            replay_task = task
            if replay_task is None:
                replay_task = await asyncio.to_thread(self.store.get_task, task_id)
            await asyncio.to_thread(
                trace_module.write_replay_event,
                task_id=task_id,
                trace_id=trace_id,
                event_record=event_record,
                source=replay_source,
                task=replay_task,
            )
        return event_record

    async def _append_cancel_requested_event(self, task_id: str) -> None:
        existing_events = await asyncio.to_thread(self.store.list_task_events, task_id)
        if any(event["event_type"] == "cancelled" for event in existing_events):
            return
        await self._append_task_event(
            task_id=task_id,
            event_type="cancelled",
            payload={
                "message": "Task cancelled by user",
                "stop_reason": "user_stopped",
            },
            role="assistant",
        )

    async def _finalize_traced_task(
        self,
        *,
        task_id: str,
        trace_id: str,
        status: str,
        final_message: str,
        stop_reason: str | None,
        step_count: int,
        metrics_source: str,
        start_perf: float,
    ) -> None:
        total_duration_ms = int((time.perf_counter() - start_perf) * 1000)
        try:
            with trace_module.trace_context(trace_id, reset_stack=False):
                await self._finalize_task(
                    task_id=task_id,
                    status=status,
                    final_message=final_message,
                    stop_reason=stop_reason,
                    step_count=step_count,
                    trace_id=trace_id,
                    mark_complete=False,
                    replay_source=metrics_source,
                )
                await self._record_trace_artifacts(
                    task_id=task_id,
                    trace_id=trace_id,
                    metrics_source=metrics_source,
                    step_count=step_count,
                    total_duration_ms=total_duration_ms,
                )
                self._mark_task_complete(task_id)
        finally:
            trace_module.clear_trace_data(trace_id)

    async def _device_worker(self, device_id: str) -> None:
        async def run_executor(executor: TaskExecutor, task: TaskRecord) -> None:
            await executor(task)

        try:
            while not self._shutdown:
                task = await asyncio.to_thread(
                    self.store.claim_next_queued_task, device_id
                )
                if task is None:
                    break

                executor = self._executors.get(task["executor_key"])
                if executor is None:
                    await self._fail_task(
                        task,
                        f"Unsupported executor: {task['executor_key']}",
                    )
                    continue

                try:
                    task_id = str(task["id"])
                    executor_task = asyncio.create_task(
                        run_executor(executor, task),
                        name=f"TaskExecutor-{task_id}",
                    )
                    self._running_executor_tasks[task_id] = executor_task
                    await executor_task
                except asyncio.CancelledError:
                    task_id = str(task["id"])
                    if (
                        task_id in self._cancel_requested
                        or task_id in self._executor_cancel_requested
                    ):
                        current = await asyncio.to_thread(self.store.get_task, task_id)
                        if (
                            current is not None
                            and current["status"] not in TERMINAL_TASK_STATUSES
                        ):
                            await self._finalize_task(
                                task_id=task_id,
                                status=TaskStatus.CANCELLED.value,
                                final_message="Task cancelled by user",
                                stop_reason="user_stopped",
                                step_count=int(current.get("step_count", 0)),
                            )
                        continue
                    else:
                        await self._interrupt_task(
                            task,
                            "Task interrupted because the service shut down",
                        )
                    raise
                except Exception as exc:  # pragma: no cover - safety net
                    logger.exception(f"Task {task['id']} crashed unexpectedly")
                    await self._fail_task(task, str(exc))
                finally:
                    self._running_executor_tasks.pop(str(task["id"]), None)
                    self._executor_cancel_requested.discard(str(task["id"]))
        finally:
            self._workers.pop(device_id, None)

    async def _execute_classic_chat(self, task: TaskRecord) -> None:
        from AutoGLM_GUI.exceptions import AgentInitializationError, DeviceBusyError
        from AutoGLM_GUI.phone_agent_manager import PhoneAgentManager
        from AutoGLM_GUI.experience_report import (
            build_experience_report_context,
            generate_experience_report,
        )

        manager = PhoneAgentManager.get_instance()
        task_id = task["id"]
        device_id = task["device_id"]
        session_id = task["session_id"] or task_id
        context = f"chat:{session_id}"
        trace_id = trace_module.create_trace_id()
        start_perf = time.perf_counter()
        acquired = False
        final_status = TaskStatus.FAILED.value
        final_message = ""
        stop_reason = "error"
        step_count = 0
        terminal_event_received = False
        abort_registered = False
        agent: Any | None = None
        experience_payload: TaskExperiencePayload | None = None

        try:
            with trace_module.trace_context(trace_id):
                await asyncio.to_thread(self.store.set_task_trace_id, task_id, trace_id)
                await self._write_replay_task_start(
                    task=task,
                    trace_id=trace_id,
                    source="classic_chat",
                )
                acquired = await manager.acquire_device_async(
                    device_id,
                    auto_initialize=True,
                    context=context,
                )
                agent = await asyncio.to_thread(
                    manager.get_agent_with_context,
                    device_id,
                    context=context,
                    agent_type=None,
                )
                user_image_attachments = await asyncio.to_thread(
                    self._get_task_user_image_attachments,
                    task_id,
                )
                experience_payload = await asyncio.to_thread(
                    self._get_task_experience_payload,
                    task_id,
                )
                image_attachment_setter: (
                    Callable[[list[TaskImageAttachment]], None] | None
                ) = None
                setter_candidate = getattr(agent, "set_user_image_attachments", None)
                if callable(setter_candidate):
                    image_attachment_setter = cast(
                        Callable[[list[TaskImageAttachment]], None],
                        setter_candidate,
                    )

                async def cancel_handler() -> None:
                    await agent.cancel()

                self._abort_handlers[task_id] = cancel_handler
                self._register_abort_handler(
                    manager,
                    device_id,
                    cancel_handler,
                    context=context,
                )
                abort_registered = True

                # Early cancel: if cancel was requested before streaming
                # started (race with cancel_task), skip the stream entirely
                if task_id in self._cancel_requested:
                    final_message = "Task cancelled by user"
                    final_status = TaskStatus.CANCELLED.value
                    stop_reason = "user_stopped"
                elif user_image_attachments and image_attachment_setter is None:
                    final_message = (
                        "Current agent does not support user image attachments"
                    )
                    final_status = TaskStatus.FAILED.value
                    stop_reason = "unsupported_image_attachments"
                else:
                    if user_image_attachments and image_attachment_setter is not None:
                        image_attachment_setter(user_image_attachments)
                    event_type = ""
                    event_data: dict[str, Any] = {}

                    # 检查是否有待继续的 takeover
                    is_continue = self._takeover_sessions.pop(session_id, False)
                    stream_kwargs: dict[str, Any] = {}
                    if is_continue:
                        # Only pass continue_with when the agent supports it
                        # (DroidRunAgent and MidsceneAgent don't have this param)
                        sig = inspect.signature(agent.stream)
                        if "continue_with" in sig.parameters:
                            stream_kwargs["continue_with"] = task["input_text"]
                    agent_config = getattr(agent, "agent_config", None)
                    observation_window_screenshot_count = getattr(
                        agent_config,
                        "observation_window_screenshot_count",
                        None,
                    )
                    observation_window_interval_seconds = getattr(
                        agent_config,
                        "observation_window_interval_seconds",
                        None,
                    )
                    original_memory_policy = getattr(
                        agent_config,
                        "memory_policy",
                        None,
                    )
                    if experience_payload is not None:
                        plan = experience_payload.get("plan")
                        if isinstance(plan, dict) and agent_config is not None:
                            memory_policy = str(plan.get("memory_policy") or "hybrid")
                            if memory_policy in {
                                "independent_items",
                                "hybrid",
                                "stateful_flow",
                            }:
                                setattr(agent_config, "memory_policy", memory_policy)
                        prompt_input = self._build_experience_execution_input(
                            str(task["input_text"]),
                            experience_payload,
                            observation_window_screenshot_count=observation_window_screenshot_count,
                            observation_window_interval_seconds=observation_window_interval_seconds,
                        )
                    else:
                        if agent_config is not None:
                            setattr(
                                agent_config,
                                "memory_policy",
                                _infer_memory_policy(str(task["input_text"])),
                            )
                        prompt_input = self._build_chat_execution_input(
                            str(task["input_text"]),
                            observation_window_screenshot_count=observation_window_screenshot_count,
                            observation_window_interval_seconds=observation_window_interval_seconds,
                        )
                    try:
                        async for event in agent.stream(
                            prompt_input,
                            **stream_kwargs,
                        ):
                            event_type = event["type"]
                            event_data = dict(event.get("data", {}))
                            previous_step_count = step_count

                            if event_type == "step":
                                step_count = max(
                                    step_count, int(event_data.get("step", 0))
                                )
                                timings = trace_module.get_step_timing_summary(
                                    step_count,
                                    trace_id=trace_id,
                                )
                                if timings is not None:
                                    event_data = {**event_data, "timings": timings}

                            await self._append_task_event(
                                task_id=task_id,
                                event_type=event_type,
                                payload=event_data,
                                role="assistant",
                                trace_id=trace_id,
                                replay_source="classic_chat",
                                task=task,
                            )
                            if event_type == "step":
                                self._schedule_experience_summary_for_progress(
                                    task=task,
                                    previous_step_count=previous_step_count,
                                    current_step_count=step_count,
                                )
                    finally:
                        if (
                            agent_config is not None
                            and original_memory_policy is not None
                        ):
                            setattr(
                                agent_config,
                                "memory_policy",
                                original_memory_policy,
                            )

                    if event_type == "takeover":
                        final_message = str(event_data.get("message", ""))
                        final_status = TaskStatus.SUCCEEDED.value
                        stop_reason = "takeover"
                        step_count = int(event_data.get("steps", step_count))
                        self._takeover_sessions[session_id] = True
                        terminal_event_received = True
                    elif event_type == "done":
                        done_success = bool(event_data.get("success", False))
                        final_message = str(
                            event_data.get("message")
                            or ("Task completed" if done_success else "Task failed")
                        )
                        final_status = (
                            TaskStatus.SUCCEEDED.value
                            if done_success
                            else TaskStatus.FAILED.value
                        )
                        stop_reason = str(
                            event_data.get(
                                "stop_reason",
                                "completed" if done_success else "error",
                            )
                        )
                        step_count = int(event_data.get("steps", step_count))
                        terminal_event_received = True
                    elif event_type == "error":
                        final_message = str(event_data.get("message", "Task failed"))
                        final_status = TaskStatus.FAILED.value
                        stop_reason = str(event_data.get("stop_reason", "error"))
                        terminal_event_received = True
                    elif event_type == "cancelled":
                        final_message = str(
                            event_data.get("message", "Task cancelled by user")
                        )
                        final_status = TaskStatus.CANCELLED.value
                        stop_reason = str(event_data.get("stop_reason", "user_stopped"))
                        terminal_event_received = True

            if not final_message and not terminal_event_received:
                final_message = "Task finished without a final response"
                final_status = TaskStatus.FAILED.value
                stop_reason = "error"

            # If cancel was requested but the stream exited normally (agent
            # sets _is_running=False without raising CancelledError), override
            # the status so the task is recorded as CANCELLED.
            if (
                task_id in self._cancel_requested
                and final_status != TaskStatus.CANCELLED.value
            ):
                final_message = "Task cancelled by user"
                final_status = TaskStatus.CANCELLED.value
                stop_reason = "user_stopped"
        except asyncio.CancelledError:
            if task_id in self._cancel_requested:
                final_message = "Task cancelled by user"
                final_status = TaskStatus.CANCELLED.value
                stop_reason = "user_stopped"
                await self._append_experience_retained_event(
                    task=task,
                    final_status=final_status,
                    stop_reason=stop_reason,
                    step_count=step_count,
                    trace_id=trace_id,
                    replay_source="classic_chat",
                )
                await self._finalize_traced_task(
                    task_id=task_id,
                    trace_id=trace_id,
                    status=final_status,
                    final_message=final_message,
                    stop_reason=stop_reason,
                    step_count=step_count,
                    metrics_source="chat",
                    start_perf=start_perf,
                )
                return
            raise
        except DeviceBusyError:
            final_message = f"Device {device_id} is busy. Please wait."
            final_status = TaskStatus.FAILED.value
            stop_reason = "device_busy"
        except AgentInitializationError as exc:
            final_message = (
                f"初始化失败: {exc}. 请检查全局配置 (base_url, api_key, model_name)"
            )
            final_status = TaskStatus.FAILED.value
            stop_reason = "initialization_failed"
        except Exception as exc:
            final_message = str(exc)
            final_status = TaskStatus.FAILED.value
            stop_reason = "error"
        finally:
            self._cancel_requested.discard(task_id)
            self._abort_handlers.pop(task_id, None)
            if abort_registered:
                self._unregister_abort_handler(
                    manager,
                    device_id,
                    context=context,
                )
            if final_status == TaskStatus.FAILED.value:
                manager.set_error_state(device_id, final_message, context=context)
            if acquired:
                manager.release_device(device_id, context=context)

        await self._append_experience_retained_event(
            task=task,
            final_status=final_status,
            stop_reason=stop_reason,
            step_count=step_count,
            trace_id=trace_id,
            replay_source="classic_chat",
        )

        if (
            experience_payload is not None
            and final_status in {TaskStatus.SUCCEEDED.value, TaskStatus.CANCELLED.value}
            and experience_payload.get("auto_generate_report", True)
        ):
            summaries = await self._ensure_experience_summaries_ready(
                task,
                trace_id=trace_id,
            )
            context = await asyncio.to_thread(
                build_experience_report_context,
                store=self.store,
                source_task=task,
            )
            report_request = ""
            plan = experience_payload.get("plan")
            if isinstance(plan, dict):
                report_request = str(plan.get("report_request") or "").strip()
            report_request = (
                report_request or "输出一份包含结论、依据和风险的体验分析报告"
            )
            await self._append_task_event(
                task_id=task_id,
                event_type="experience_report_context",
                payload={
                    "source_task_id": str(task["id"]),
                    "source_status": final_status,
                    "source_stop_reason": stop_reason,
                    "source_step_count": step_count,
                    "segment_summary_count": len(summaries),
                    "context_chars": len(context.text),
                },
                role="system",
                trace_id=trace_id,
                replay_source="classic_chat",
                task=task,
            )
            report = await generate_experience_report(
                report_request=report_request,
                context=context,
            )
            if report:
                final_message = report
                await self._append_task_event(
                    task_id=task_id,
                    event_type="experience_report",
                    payload={"content": report},
                    role="assistant",
                    trace_id=trace_id,
                    replay_source="classic_chat",
                    task=task,
                )

        await self._finalize_traced_task(
            task_id=task_id,
            trace_id=trace_id,
            status=final_status,
            final_message=final_message,
            stop_reason=stop_reason,
            step_count=step_count,
            metrics_source="chat",
            start_perf=start_perf,
        )

    async def _execute_layered_chat(self, task: TaskRecord) -> None:
        await self._execute_layered_task(
            task,
            session_id=str(task["session_id"] or task["id"]),
            clear_session_after_run=False,
            metrics_source="layered",
        )

    async def _execute_layered_task(
        self,
        task: TaskRecord,
        *,
        session_id: str,
        clear_session_after_run: bool,
        metrics_source: str,
    ) -> None:
        from AutoGLM_GUI.layered_agent_service import (
            reset_session as reset_layered_session,
            start_run,
        )

        task_id = str(task["id"])
        trace_id = trace_module.create_trace_id()
        start_perf = time.perf_counter()
        final_status = TaskStatus.FAILED.value
        final_message = ""
        stop_reason = "error"
        step_count = 0
        run = None
        terminal_event_received = False

        try:
            with trace_module.trace_context(trace_id):
                await asyncio.to_thread(self.store.set_task_trace_id, task_id, trace_id)
                await self._write_replay_task_start(
                    task=task,
                    trace_id=trace_id,
                    source=metrics_source,
                )
                run = start_run(
                    task_id=task_id,
                    session_id=session_id,
                    message=str(task["input_text"]),
                    device_id=str(task["device_id"]),
                )
                self._abort_handlers[task_id] = run.cancel

                async for event in run.stream_events():
                    event_type = str(event["type"])
                    event_payload = dict(event.get("payload", {}))
                    await self._append_task_event(
                        task_id=task_id,
                        event_type=event_type,
                        payload=event_payload,
                        role="assistant",
                        trace_id=trace_id,
                        replay_source=metrics_source,
                        task=task,
                    )

                    if event_type == "tool_result":
                        previous_step_count = step_count
                        sub_steps = event_payload.get("steps", 0)
                        if isinstance(sub_steps, (int, float)):
                            step_count += int(sub_steps)
                        self._schedule_experience_summary_for_progress(
                            task=task,
                            previous_step_count=previous_step_count,
                            current_step_count=step_count,
                        )
                    elif event_type == "done":
                        done_success = bool(event_payload.get("success", False))
                        final_message = str(event_payload.get("content") or "")
                        final_status = (
                            TaskStatus.SUCCEEDED.value
                            if done_success
                            else TaskStatus.FAILED.value
                        )
                        stop_reason = str(
                            event_payload.get(
                                "stop_reason",
                                "completed" if done_success else "error",
                            )
                        )
                        terminal_event_received = True
                    elif event_type == "error":
                        final_message = str(event_payload.get("message", "Task failed"))
                        final_status = TaskStatus.FAILED.value
                        stop_reason = str(event_payload.get("stop_reason", "error"))
                        terminal_event_received = True
                    elif event_type == "cancelled":
                        final_message = str(
                            event_payload.get("message", "Task cancelled by user")
                        )
                        final_status = TaskStatus.CANCELLED.value
                        stop_reason = str(
                            event_payload.get("stop_reason", "user_stopped")
                        )
                        terminal_event_received = True

                if task_id in self._cancel_requested:
                    final_message = "Task cancelled by user"
                    final_status = TaskStatus.CANCELLED.value
                    stop_reason = "user_stopped"

            if not final_message and run:
                final_message = run.final_output

            if not final_message and terminal_event_received:
                final_message = (
                    "Task completed"
                    if final_status == TaskStatus.SUCCEEDED.value
                    else "Task failed"
                )

            if not final_message and not terminal_event_received:
                final_message = "Task finished without a final response"
                final_status = TaskStatus.FAILED.value
                stop_reason = "error"
        except asyncio.CancelledError:
            if task_id in self._cancel_requested:
                final_message = "Task cancelled by user"
                final_status = TaskStatus.CANCELLED.value
                stop_reason = "user_stopped"
            else:
                raise
        except Exception as exc:
            if task_id in self._cancel_requested:
                final_message = "Task cancelled by user"
                final_status = TaskStatus.CANCELLED.value
                stop_reason = "user_stopped"
            else:
                final_message = str(exc)
                final_status = TaskStatus.FAILED.value
                stop_reason = "error"
        finally:
            self._cancel_requested.discard(task_id)
            self._abort_handlers.pop(task_id, None)
            if clear_session_after_run:
                reset_layered_session(session_id)

        await self._append_experience_retained_event(
            task=task,
            final_status=final_status,
            stop_reason=stop_reason,
            step_count=step_count,
            trace_id=trace_id,
            replay_source=metrics_source,
        )

        await self._finalize_traced_task(
            task_id=task_id,
            trace_id=trace_id,
            status=final_status,
            final_message=final_message,
            stop_reason=stop_reason,
            step_count=step_count,
            metrics_source=metrics_source,
            start_perf=start_perf,
        )

    async def _execute_experience_report(self, task: TaskRecord) -> None:
        from AutoGLM_GUI.experience_report import (
            build_experience_report_context,
            generate_experience_report,
        )

        task_id = str(task["id"])
        session_id = str(task["session_id"] or "")
        trace_id = trace_module.create_trace_id()
        start_perf = time.perf_counter()
        final_status = TaskStatus.FAILED.value
        final_message = ""
        stop_reason = "error"

        try:
            with trace_module.trace_context(trace_id):
                await asyncio.to_thread(self.store.set_task_trace_id, task_id, trace_id)
                await self._write_replay_task_start(
                    task=task,
                    trace_id=trace_id,
                    source="experience_report",
                )
                if task_id in self._cancel_requested:
                    final_message = "Task cancelled by user"
                    final_status = TaskStatus.CANCELLED.value
                    stop_reason = "user_stopped"
                else:
                    source_task = await asyncio.to_thread(
                        self._get_experience_report_source_task,
                        task_id,
                        session_id,
                    )
                    if source_task is None:
                        final_message = (
                            "No retained app experience found. Run an experience first, "
                            "then stop and enter the report format."
                        )
                        final_status = TaskStatus.FAILED.value
                        stop_reason = "report_context_missing"
                    else:
                        summaries = await self._ensure_experience_summaries_ready(
                            source_task,
                            report_task=task,
                            trace_id=trace_id,
                        )
                        context = await asyncio.to_thread(
                            build_experience_report_context,
                            store=self.store,
                            source_task=source_task,
                        )
                        await self._append_task_event(
                            task_id=task_id,
                            event_type="experience_report_context",
                            payload={
                                "source_task_id": str(source_task["id"]),
                                "source_status": source_task.get("status"),
                                "source_stop_reason": source_task.get("stop_reason"),
                                "source_step_count": int(
                                    source_task.get("step_count") or 0
                                ),
                                "segment_summary_count": len(summaries),
                                "context_chars": len(context.text),
                            },
                            role="system",
                            trace_id=trace_id,
                            replay_source="experience_report",
                            task=task,
                        )
                        report = await generate_experience_report(
                            report_request=str(task["input_text"]),
                            context=context,
                        )
                        if not report:
                            report = "Report generation returned an empty response."
                        await self._append_task_event(
                            task_id=task_id,
                            event_type="message",
                            payload={"content": report},
                            role="assistant",
                            trace_id=trace_id,
                            replay_source="experience_report",
                            task=task,
                        )
                        final_message = report
                        final_status = TaskStatus.SUCCEEDED.value
                        stop_reason = "report_generated"
        except asyncio.CancelledError:
            if task_id in self._cancel_requested:
                final_message = "Task cancelled by user"
                final_status = TaskStatus.CANCELLED.value
                stop_reason = "user_stopped"
            else:
                raise
        except Exception as exc:
            if task_id in self._cancel_requested:
                final_message = "Task cancelled by user"
                final_status = TaskStatus.CANCELLED.value
                stop_reason = "user_stopped"
            else:
                final_message = str(exc)
                final_status = TaskStatus.FAILED.value
                stop_reason = "error"
        finally:
            self._cancel_requested.discard(task_id)

        await self._finalize_traced_task(
            task_id=task_id,
            trace_id=trace_id,
            status=final_status,
            final_message=final_message,
            stop_reason=stop_reason,
            step_count=0,
            metrics_source="experience_report",
            start_perf=start_perf,
        )

    def _get_experience_report_source_task(
        self, task_id: str, session_id: str
    ) -> TaskRecord | None:
        events = self.store.list_task_events(task_id)
        for event in events:
            if event["event_type"] != "experience_report_source":
                continue
            payload = dict(event.get("payload") or {})
            source_task_id = payload.get("source_task_id")
            if isinstance(source_task_id, str) and source_task_id:
                return self.store.get_task(source_task_id)
        if not session_id:
            return None
        return self.store.get_latest_reportable_session_task(session_id)

    async def _append_experience_retained_event(
        self,
        *,
        task: TaskRecord,
        final_status: str,
        stop_reason: str,
        step_count: int,
        trace_id: str | None,
        replay_source: str,
    ) -> None:
        if task.get("source") != "chat":
            return
        if task.get("executor_key") not in {"classic_chat", "layered_chat"}:
            return
        if not task.get("session_id"):
            return
        if step_count <= 0:
            return
        if final_status not in {status.value for status in TERMINAL_TASK_STATUSES}:
            return
        await self._append_task_event(
            task_id=str(task["id"]),
            event_type="experience_retained",
            payload={
                "message": (
                    "This app experience has been retained. Continue in chat with "
                    "the report format, focus areas, and required structure."
                ),
                "source_task_id": str(task["id"]),
                "stop_reason": stop_reason,
                "step_count": step_count,
            },
            role="system",
            trace_id=trace_id,
            replay_source=replay_source if trace_id else None,
            task=task,
        )
        self._schedule_experience_summary_update(
            task=task,
            include_partial_segment=True,
        )

    def _schedule_experience_summary_for_progress(
        self,
        *,
        task: TaskRecord,
        previous_step_count: int,
        current_step_count: int,
    ) -> None:
        if current_step_count <= previous_step_count:
            return
        from AutoGLM_GUI.experience_report import DEFAULT_SEGMENT_STEP_SIZE

        if (
            current_step_count // DEFAULT_SEGMENT_STEP_SIZE
            <= previous_step_count // DEFAULT_SEGMENT_STEP_SIZE
        ):
            return
        self._schedule_experience_summary_update(
            task=task,
            include_partial_segment=False,
        )

    def _schedule_experience_summary_update(
        self,
        *,
        task: TaskRecord,
        include_partial_segment: bool,
    ) -> None:
        task_id = str(task["id"])
        if task.get("source") != "chat":
            return
        if task.get("executor_key") not in {"classic_chat", "layered_chat"}:
            return
        if include_partial_segment:
            self._experience_summary_include_partial.add(task_id)

        existing = self._experience_summary_tasks.get(task_id)
        if existing is not None and not existing.done():
            self._experience_summary_rerun_requested.add(task_id)
            return

        summary_task = asyncio.create_task(
            self._run_experience_summary_worker(task),
            name=f"experience-summary-{task_id}",
        )
        self._experience_summary_tasks[task_id] = summary_task

    def _get_experience_summary_semaphore(self) -> asyncio.Semaphore:
        if self._experience_summary_semaphore is None:
            self._experience_summary_semaphore = asyncio.Semaphore(
                EXPERIENCE_SUMMARY_MAX_CONCURRENCY
            )
        return self._experience_summary_semaphore

    async def _run_experience_summary_worker(self, task: TaskRecord) -> None:
        from AutoGLM_GUI.experience_report import ensure_experience_segment_summaries

        task_id = str(task["id"])
        try:
            while True:
                include_partial_segment = (
                    task_id in self._experience_summary_include_partial
                )
                self._experience_summary_include_partial.discard(task_id)
                try:
                    async with self._get_experience_summary_semaphore():
                        await ensure_experience_segment_summaries(
                            store=self.store,
                            source_task=task,
                            include_partial_segment=include_partial_segment,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "Experience segment summary failed for task %s",
                        task_id,
                        exc_info=True,
                    )

                if task_id not in self._experience_summary_rerun_requested:
                    break
                self._experience_summary_rerun_requested.discard(task_id)
        finally:
            current = asyncio.current_task()
            if self._experience_summary_tasks.get(task_id) is current:
                self._experience_summary_tasks.pop(task_id, None)
            self._experience_summary_rerun_requested.discard(task_id)
            self._experience_summary_include_partial.discard(task_id)

    async def _ensure_experience_summaries_ready(
        self,
        source_task: TaskRecord,
        *,
        report_task: TaskRecord | None = None,
        trace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        from AutoGLM_GUI.experience_report import ensure_experience_segment_summaries

        task_id = str(source_task["id"])
        running_summary = self._experience_summary_tasks.get(task_id)
        if running_summary is not None and not running_summary.done():
            if report_task is not None:
                await self._append_task_event(
                    task_id=str(report_task["id"]),
                    event_type="message",
                    payload={"content": "正在整理体验记忆并生成报告..."},
                    role="assistant",
                    trace_id=trace_id,
                    replay_source="experience_report" if trace_id else None,
                    task=report_task,
                )
            self._experience_summary_include_partial.add(task_id)
            self._experience_summary_rerun_requested.add(task_id)
            await running_summary

        async with self._get_experience_summary_semaphore():
            return await ensure_experience_segment_summaries(
                store=self.store,
                source_task=source_task,
                include_partial_segment=True,
            )

    async def _execute_scheduled_layered_workflow(self, task: TaskRecord) -> None:
        await self._execute_layered_task(
            task,
            session_id=str(task["id"]),
            clear_session_after_run=True,
            metrics_source="scheduled",
        )

    async def _execute_scheduled_workflow(self, task: TaskRecord) -> None:
        from AutoGLM_GUI.exceptions import AgentInitializationError, DeviceBusyError
        from AutoGLM_GUI.phone_agent_manager import PhoneAgentManager

        manager = PhoneAgentManager.get_instance()
        task_id = str(task["id"])
        device_id = str(task["device_id"])
        context = "scheduled"
        trace_id = trace_module.create_trace_id()
        start_perf = time.perf_counter()
        acquired = False
        final_status = TaskStatus.FAILED.value
        final_message = ""
        stop_reason = "error"
        step_count = 0
        terminal_event_received = False
        abort_registered = False

        try:
            with trace_module.trace_context(trace_id):
                await asyncio.to_thread(self.store.set_task_trace_id, task_id, trace_id)
                await self._write_replay_task_start(
                    task=task,
                    trace_id=trace_id,
                    source="scheduled",
                )
                acquired = await manager.acquire_device_async(
                    device_id,
                    auto_initialize=True,
                    context=context,
                )
                agent = await asyncio.to_thread(
                    manager.get_agent_with_context,
                    device_id,
                    context=context,
                    agent_type=None,
                )

                async def cancel_handler() -> None:
                    await agent.cancel()

                self._abort_handlers[task_id] = cancel_handler
                self._register_abort_handler(
                    manager,
                    device_id,
                    cancel_handler,
                    context=context,
                )
                abort_registered = True
                agent.reset()

                # Early cancel: if cancel was requested before streaming started
                if task_id in self._cancel_requested:
                    final_message = "Task cancelled by user"
                    final_status = TaskStatus.CANCELLED.value
                    stop_reason = "user_stopped"
                else:
                    async for event in agent.stream(task["input_text"]):
                        event_type = event["type"]
                        event_data = dict(event.get("data", {}))
                        if event_type == "thinking":
                            await self._append_task_event(
                                task_id=task_id,
                                event_type="thinking",
                                payload=event_data,
                                role="assistant",
                                trace_id=trace_id,
                                replay_source="scheduled",
                                task=task,
                            )
                        elif event_type == "observation":
                            await self._append_task_event(
                                task_id=task_id,
                                event_type="observation",
                                payload=event_data,
                                role="assistant",
                                trace_id=trace_id,
                                replay_source="scheduled",
                                task=task,
                            )
                        elif event_type == "step":
                            step_count = max(
                                step_count,
                                int(event_data.get("step", 0)),
                            )
                            timings = trace_module.get_step_timing_summary(
                                step_count,
                                trace_id=trace_id,
                            )
                            if timings is not None:
                                event_data = {**event_data, "timings": timings}
                            await self._append_task_event(
                                task_id=task_id,
                                event_type="step",
                                payload=event_data,
                                role="assistant",
                                trace_id=trace_id,
                                replay_source="scheduled",
                                task=task,
                            )
                        elif event_type == "done":
                            done_success = bool(event_data.get("success", False))
                            final_message = str(
                                event_data.get("message")
                                or ("Task completed" if done_success else "Task failed")
                            )
                            final_status = (
                                TaskStatus.SUCCEEDED.value
                                if done_success
                                else TaskStatus.FAILED.value
                            )
                            stop_reason = str(
                                event_data.get(
                                    "stop_reason",
                                    "completed" if done_success else "error",
                                )
                            )
                            step_count = int(event_data.get("steps", step_count))
                            terminal_event_received = True
                        elif event_type == "error":
                            final_message = str(
                                event_data.get("message", "Task failed")
                            )
                            final_status = TaskStatus.FAILED.value
                            stop_reason = str(event_data.get("stop_reason", "error"))
                            terminal_event_received = True
                            await self._append_task_event(
                                task_id=task_id,
                                event_type="error",
                                payload={
                                    "message": final_message,
                                    "stop_reason": stop_reason,
                                },
                                role="assistant",
                                trace_id=trace_id,
                                replay_source="scheduled",
                                task=task,
                            )
                        elif event_type == "cancelled":
                            final_message = str(
                                event_data.get("message", "Task cancelled by user")
                            )
                            final_status = TaskStatus.CANCELLED.value
                            stop_reason = str(
                                event_data.get("stop_reason", "user_stopped")
                            )
                            terminal_event_received = True

                if not final_message and not terminal_event_received:
                    final_message = "Task finished without a final response"
                    final_status = TaskStatus.FAILED.value
                    stop_reason = "error"

                # If cancel was requested but the stream exited normally,
                # override status to CANCELLED.
                if (
                    task_id in self._cancel_requested
                    and final_status != TaskStatus.CANCELLED.value
                ):
                    final_message = "Task cancelled by user"
                    final_status = TaskStatus.CANCELLED.value
                    stop_reason = "user_stopped"
        except asyncio.CancelledError:
            if task_id in self._cancel_requested:
                final_message = "Task cancelled by user"
                final_status = TaskStatus.CANCELLED.value
                stop_reason = "user_stopped"
            else:
                raise
        except DeviceBusyError:
            final_message = f"Device {device_id} is busy. Please wait."
            final_status = TaskStatus.FAILED.value
            stop_reason = "device_busy"
        except AgentInitializationError as exc:
            final_message = (
                f"初始化失败: {exc}. 请检查全局配置 (base_url, api_key, model_name)"
            )
            final_status = TaskStatus.FAILED.value
            stop_reason = "initialization_failed"
        except Exception as exc:
            final_message = str(exc)
            final_status = TaskStatus.FAILED.value
            stop_reason = "error"
        finally:
            self._cancel_requested.discard(task_id)
            self._abort_handlers.pop(task_id, None)
            if abort_registered:
                self._unregister_abort_handler(
                    manager,
                    device_id,
                    context=context,
                )
            if final_status == TaskStatus.FAILED.value:
                manager.set_error_state(device_id, final_message, context=context)
            if acquired:
                manager.release_device(device_id, context=context)

        await self._finalize_traced_task(
            task_id=task_id,
            trace_id=trace_id,
            status=final_status,
            final_message=final_message,
            stop_reason=stop_reason,
            step_count=step_count,
            metrics_source="scheduled",
            start_perf=start_perf,
        )

    async def _finalize_task(
        self,
        *,
        task_id: str,
        status: str,
        final_message: str,
        step_count: int,
        stop_reason: str | None = None,
        trace_id: str | None = None,
        mark_complete: bool = True,
        replay_source: str = "task_finalize",
    ) -> None:
        normalized_stop_reason = stop_reason
        if normalized_stop_reason is None:
            if status == TaskStatus.SUCCEEDED.value:
                normalized_stop_reason = "completed"
            elif status == TaskStatus.CANCELLED.value:
                normalized_stop_reason = "user_stopped"
            else:
                normalized_stop_reason = "error"

        if status == TaskStatus.SUCCEEDED.value:
            event_type = "done"
            payload = {
                "message": final_message,
                "steps": step_count,
                "success": True,
                "stop_reason": normalized_stop_reason,
            }
            error_message = None
        elif status == TaskStatus.CANCELLED.value:
            event_type = "cancelled"
            payload = {
                "message": final_message,
                "stop_reason": normalized_stop_reason,
            }
            error_message = final_message
        else:
            event_type = "error"
            payload = {
                "message": final_message,
                "stop_reason": normalized_stop_reason,
            }
            error_message = final_message

        existing_events = await asyncio.to_thread(self.store.list_task_events, task_id)
        if not any(event["event_type"] == event_type for event in existing_events):
            await self._append_task_event(
                task_id=task_id,
                event_type=event_type,
                payload=payload,
                role="assistant",
                trace_id=trace_id,
                replay_source=replay_source if trace_id else None,
            )

        await asyncio.to_thread(
            self.store.update_task_terminal,
            task_id=task_id,
            status=status,
            final_message=final_message,
            error_message=error_message,
            stop_reason=normalized_stop_reason,
            step_count=step_count,
            trace_id=trace_id,
        )
        if trace_id:
            status_events = await asyncio.to_thread(
                self.store.list_task_events, task_id
            )
            final_status_event = next(
                (
                    event
                    for event in reversed(status_events)
                    if event["event_type"] == "status"
                ),
                None,
            )
            if final_status_event is not None:
                final_task = await asyncio.to_thread(self.store.get_task, task_id)
                await asyncio.to_thread(
                    trace_module.write_replay_event,
                    task_id=task_id,
                    trace_id=trace_id,
                    event_record=final_status_event,
                    source=replay_source,
                    task=final_task,
                )
        if mark_complete:
            self._mark_task_complete(task_id)

    async def _fail_task(self, task: TaskRecord, message: str) -> None:
        await self._append_task_event(
            task_id=task["id"],
            event_type="error",
            payload={"message": message, "stop_reason": "error"},
            role="assistant",
        )
        await self._finalize_task(
            task_id=task["id"],
            status=TaskStatus.FAILED.value,
            final_message=message,
            stop_reason="error",
            step_count=int(task.get("step_count", 0)),
        )

    async def _interrupt_task(self, task: TaskRecord, message: str) -> None:
        await self._append_task_event(
            task_id=task["id"],
            event_type="error",
            payload={"message": message, "stop_reason": "service_interrupted"},
            role="assistant",
        )
        await asyncio.to_thread(
            self.store.update_task_terminal,
            task_id=task["id"],
            status=TaskStatus.INTERRUPTED.value,
            final_message=message,
            error_message=message,
            stop_reason="service_interrupted",
            step_count=int(task.get("step_count", 0)),
        )
        self._mark_task_complete(task["id"])

    def _mark_task_complete(self, task_id: str) -> None:
        event = self._completion_events.setdefault(task_id, asyncio.Event())
        event.set()


task_manager = TaskManager()
