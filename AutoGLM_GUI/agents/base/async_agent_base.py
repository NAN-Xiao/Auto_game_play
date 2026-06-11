"""AsyncAgentBase - 异步 Agent 基类，提取 GLM/Gemini 共享逻辑。

子类只需实现:
- _get_default_system_prompt(lang) → 默认 system prompt
- _prepare_initial_context(task, screenshot, current_app, reference_images) → 构建首条消息
- _execute_step() → 单步执行（LLM 调用 + action 执行）
"""

import asyncio
import copy
import json
import re
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from typing import Any
from collections.abc import AsyncIterator, Callable

from openai import AsyncOpenAI

from AutoGLM_GUI.actions import ActionHandler
from AutoGLM_GUI.config import AgentConfig, ModelConfig
from AutoGLM_GUI.device_protocol import DeviceProtocol, Screenshot
from AutoGLM_GUI.logger import logger
from AutoGLM_GUI.model import MessageBuilder
from AutoGLM_GUI.trace import summarize_text, trace_span


WATCHDOG_REPEATED_ACTION_LIMIT = 12
WATCHDOG_NO_PROGRESS_LIMIT = 20
STRICT_FINISH_RETRY_LIMIT = 1
STRICT_EMPTY_FINISH_LIMIT = 2
STRICT_FINISH_SUPPRESSION_LIMIT = 3
OBSERVATION_WINDOW_MAX_SCREENSHOTS = 20
OBSERVATION_WINDOW_MAX_INTERVAL_SECONDS = 60.0
MODEL_STREAM_CHUNK_TIMEOUT_SECONDS = 120.0
MODEL_STREAM_CREATE_TIMEOUT_SECONDS = 60.0
HYBRID_HISTORY_MESSAGE_LIMIT = 6
STATEFUL_HISTORY_MESSAGE_LIMIT = 12


def _collect_model_message_stats(messages: list[dict[str, Any]]) -> dict[str, Any]:
    image_count = 0
    image_base64_chars = 0
    max_image_base64_chars = 0
    text_chars = 0

    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            text_chars += len(content)
            continue
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str):
                    text_chars += len(text)
                continue
            if part.get("type") != "image_url":
                continue
            image_url = part.get("image_url")
            if not isinstance(image_url, dict):
                continue
            url = image_url.get("url")
            if not isinstance(url, str):
                continue
            base64_chars = len(url.rsplit(",", 1)[-1])
            image_count += 1
            image_base64_chars += base64_chars
            max_image_base64_chars = max(max_image_base64_chars, base64_chars)

    return {
        "image_count": image_count,
        "image_base64_chars": image_base64_chars,
        "max_image_base64_chars": max_image_base64_chars,
        "text_chars": text_chars,
        "estimated_payload_chars": image_base64_chars + text_chars,
    }


class AsyncAgentBase(ABC):
    """异步 Agent 基类。

    提供共享的:
    - OpenAI client 初始化
    - ActionHandler 初始化
    - stream() 主循环（截图 → 步骤循环 → 完成/取消）
    - cancel / reset / run / properties
    """

    def __init__(
        self,
        model_config: ModelConfig,
        agent_config: AgentConfig,
        device: DeviceProtocol,
        confirmation_callback: Callable[[str], bool] | None = None,
        takeover_callback: Callable[[str], None] | None = None,
    ):
        self.model_config = model_config
        self.agent_config = agent_config

        self.openai_client = AsyncOpenAI(
            base_url=model_config.base_url,
            api_key=model_config.api_key,
            timeout=120,
        )

        self.device = device
        self.action_handler = ActionHandler(
            device=self.device,
            confirmation_callback=confirmation_callback,
            takeover_callback=takeover_callback,
        )

        self._cancel_event = asyncio.Event()

        # System prompt: 优先用配置的，否则用子类默认的
        system_prompt = self.agent_config.system_prompt
        if system_prompt is None:
            system_prompt = self._get_default_system_prompt(self.agent_config.lang)

        self._initial_system_message = MessageBuilder.create_system_message(
            system_prompt
        )

        # State
        self._context: list[dict[str, Any]] = [self._initial_system_message]
        self._user_image_attachments: list[dict[str, str]] = []
        self._step_count = 0
        self._is_running = False
        self._strict_finish_recovery_pending = False
        self._strict_finish_recovery_attempts = 0
        self._active_task: str | None = None
        self._state_checkpoints: list[str] = []

    # ==================== 子类必须实现 ====================

    @abstractmethod
    def _get_default_system_prompt(self, lang: str) -> str:
        """返回默认 system prompt。"""
        ...

    @abstractmethod
    def _prepare_initial_context(
        self,
        task: str,
        screenshot_base64: str,
        current_app: str,
        reference_images: list[dict[str, str]] | None = None,
    ) -> None:
        """构建首条用户消息并添加到 self._context。"""
        ...

    @abstractmethod
    async def _execute_step(self) -> AsyncGenerator[dict[str, Any], None]:
        """执行单步：获取截图 → 调用 LLM → 执行动作。

        子类必须实现为 async generator（使用 yield）。
        """
        raise NotImplementedError
        yield  # pragma: no cover — make Pyright see this as async generator

    # ==================== 共享逻辑 ====================

    def _observation_window_count(self) -> int:
        count = max(
            1,
            min(
                int(self.agent_config.observation_window_screenshot_count),
                OBSERVATION_WINDOW_MAX_SCREENSHOTS,
            ),
        )
        if self.agent_config.observation_window_enabled or count > 1:
            return count
        return 1

    def _observation_window_interval_seconds(self) -> float:
        if self._observation_window_count() <= 1:
            return 0.0
        return max(
            0.0,
            min(
                float(self.agent_config.observation_window_interval_seconds),
                OBSERVATION_WINDOW_MAX_INTERVAL_SECONDS,
            ),
        )

    async def _wait_observation_window_interval(
        self, *, sample_index: int, sample_count: int
    ) -> None:
        interval_seconds = self._observation_window_interval_seconds()
        if interval_seconds <= 0:
            return
        attrs = {
            "step": self._step_count,
            "agent_type": self.__class__.__name__,
            "sample_index": sample_index,
            "sample_count": sample_count,
            "duration_ms": round(interval_seconds * 1000, 3),
        }
        with trace_span("sleep.observation_window", attrs=attrs):
            try:
                await asyncio.wait_for(
                    self._cancel_event.wait(), timeout=interval_seconds
                )
            except asyncio.TimeoutError:
                return
            raise asyncio.CancelledError()

    def _build_observation_event(
        self,
        *,
        phase: str,
        sample_index: int,
        sample_count: int,
        screenshot: Screenshot | None = None,
    ) -> dict[str, Any]:
        interval_seconds = self._observation_window_interval_seconds()
        payload: dict[str, Any] = {
            "phase": phase,
            "step": self._step_count,
            "sample_index": sample_index,
            "sample_count": sample_count,
            "interval_seconds": interval_seconds,
            "observation_window": sample_count > 1,
        }
        if screenshot is not None:
            payload.update(
                {
                    "screenshot": screenshot.base64_data,
                    "width": screenshot.width,
                    "height": screenshot.height,
                }
            )
        if phase == "start":
            payload["message"] = (
                f"开始直接截帧：共 {sample_count} 张，间隔 {interval_seconds:g} 秒。"
            )
        elif phase == "sample":
            payload["message"] = f"已截取第 {sample_index}/{sample_count} 张截图。"
        elif phase == "complete":
            payload["message"] = (
                f"截帧完成：已采集 {sample_count} 张截图，开始一次性多模态综合分析。"
            )
        return {"type": "observation", "data": payload}

    async def _capture_observation_window(self) -> list[Screenshot]:
        """Capture the current screen, optionally as a temporal observation window."""
        sample_count = self._observation_window_count()
        screenshots: list[Screenshot] = []
        for index in range(sample_count):
            if sample_count > 1 and index > 0:
                await self._wait_observation_window_interval(
                    sample_index=index + 1,
                    sample_count=sample_count,
                )
            with trace_span(
                "step.capture_screenshot",
                attrs={
                    "step": self._step_count,
                    "agent_type": self.__class__.__name__,
                    "sample_index": index + 1,
                    "sample_count": sample_count,
                    "observation_window": sample_count > 1,
                },
            ):
                screenshots.append(await asyncio.to_thread(self.device.get_screenshot))
        return screenshots

    async def _observe_observation_window(
        self,
    ) -> AsyncGenerator[dict[str, Any] | list[Screenshot], None]:
        """Capture an observation window and stream user-visible progress events."""
        sample_count = self._observation_window_count()
        screenshots: list[Screenshot] = []
        if sample_count > 1:
            yield self._build_observation_event(
                phase="start",
                sample_index=0,
                sample_count=sample_count,
            )
        for index in range(sample_count):
            if sample_count > 1 and index > 0:
                await self._wait_observation_window_interval(
                    sample_index=index + 1,
                    sample_count=sample_count,
                )
            with trace_span(
                "step.capture_screenshot",
                attrs={
                    "step": self._step_count,
                    "agent_type": self.__class__.__name__,
                    "sample_index": index + 1,
                    "sample_count": sample_count,
                    "observation_window": sample_count > 1,
                },
            ):
                screenshot = await asyncio.to_thread(self.device.get_screenshot)
                screenshots.append(screenshot)
            if sample_count > 1:
                yield self._build_observation_event(
                    phase="sample",
                    sample_index=index + 1,
                    sample_count=sample_count,
                    screenshot=screenshot,
                )
        if sample_count > 1:
            yield self._build_observation_event(
                phase="complete",
                sample_index=sample_count,
                sample_count=sample_count,
            )
        yield screenshots

    def _build_observation_window_notice(
        self, *, screenshot_count: int, reference_image_count: int = 0
    ) -> str:
        if screenshot_count <= 1 and reference_image_count <= 0:
            return ""
        parts: list[str] = []
        if screenshot_count > 1:
            parts.append(
                "Observation window: the first "
                f"{screenshot_count} images are chronological screenshots of the "
                "same current screen/object. Analyze these frames together for "
                f"motion, content changes, timing, and evidence. Image {screenshot_count} "
                "is the latest actionable screen for tap/swipe coordinates; earlier "
                "frames are temporal evidence only."
            )
        if reference_image_count > 0:
            first_reference = screenshot_count + 1
            last_reference = screenshot_count + reference_image_count
            image_word = "image" if reference_image_count == 1 else "images"
            parts.append(
                f"User attached {reference_image_count} reference {image_word}. "
                f"Images {first_reference}-{last_reference} are user-provided "
                "references for the task, not current actionable screens."
            )
        return "\n\n".join(parts)

    def _is_strict_run_mode(self) -> bool:
        return self.agent_config.run_limit_type != "autonomous"

    def _uses_stateless_observation_turns(self) -> bool:
        if self._observation_window_count() <= 1:
            return False
        return self.agent_config.memory_policy == "independent_items"

    def _history_message_limit_for_policy(self) -> int | None:
        if self.agent_config.memory_policy == "hybrid":
            return HYBRID_HISTORY_MESSAGE_LIMIT
        if self.agent_config.memory_policy == "stateful_flow":
            return STATEFUL_HISTORY_MESSAGE_LIMIT
        return None

    _CHECKPOINT_PATTERN = re.compile(r"【阶段小结】(.+?)(?:\n|$)")
    _CHECKPOINT_SECTION_MARKER = "\n\n** 阶段进度记忆 **\n"
    _MAX_CHECKPOINTS = 20

    def _extract_checkpoints_from_messages(
        self, messages: list[dict[str, Any]]
    ) -> list[str]:
        """Extract 【阶段小结】 lines from assistant messages."""
        checkpoints: list[str] = []
        for msg in messages:
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content", "")
            if not isinstance(content, str):
                continue
            for match in self._CHECKPOINT_PATTERN.finditer(content):
                checkpoint = match.group(1).strip()
                if checkpoint and checkpoint not in checkpoints:
                    checkpoints.append(checkpoint)
        return checkpoints

    def _inject_checkpoints_into_system_message(self) -> None:
        """Inject accumulated state checkpoints into the system message."""
        if not self._state_checkpoints:
            return
        if not self._context:
            return
        system_msg = self._context[0]
        if system_msg.get("role") != "system":
            return

        base_content = system_msg.get("content", "")
        if not isinstance(base_content, str):
            return

        # Strip any previously injected checkpoint section
        marker = self._CHECKPOINT_SECTION_MARKER
        marker_idx = base_content.find(marker)
        if marker_idx != -1:
            base_content = base_content[:marker_idx]

        # Append current checkpoints
        checkpoint_lines = [
            f"{i+1}. {cp}" for i, cp in enumerate(self._state_checkpoints)
        ]
        checkpoint_section = (
            f"{marker}"
            + "\n".join(checkpoint_lines)
        )
        self._context[0] = {
            **system_msg,
            "content": base_content + checkpoint_section,
        }

    def _trim_context_history(self) -> None:
        limit = self._history_message_limit_for_policy()
        if limit is None or len(self._context) <= limit + 1:
            return

        # Extract checkpoints from messages about to be dropped
        messages_to_drop = self._context[1:-limit]
        new_checkpoints = self._extract_checkpoints_from_messages(messages_to_drop)
        if new_checkpoints:
            self._state_checkpoints.extend(new_checkpoints)
            # Keep only the most recent checkpoints
            if len(self._state_checkpoints) > self._MAX_CHECKPOINTS:
                self._state_checkpoints = self._state_checkpoints[
                    -self._MAX_CHECKPOINTS:
                ]

        self._context = [self._context[0], *self._context[-limit:]]

        # Re-inject checkpoints into system message
        if self._state_checkpoints:
            self._inject_checkpoints_into_system_message()

    def _prepare_context_for_model_turn(self) -> bool:
        if self._uses_stateless_observation_turns():
            self._context = [copy.deepcopy(self._initial_system_message)]
            return True

        self._context = [
            MessageBuilder.remove_images_from_message(message)
            for message in self._context
        ]
        self._trim_context_history()
        return False

    def _build_stateless_observation_turn_notice(self, *, include_task: bool) -> str:
        parts: list[str] = []
        if include_task and self._active_task:
            parts.append(f"** Original Task **\n\n{self._active_task}")
        parts.append(
            "** Per-turn Memory Policy **\n\n"
            "当前是逐项循环任务的单轮分析。历史截图、thinking、小结和动作已经由任务事件持久化，"
            "最后报告会从这些事件/数据库记录汇总；本轮不要依赖或复述历史对象内容，只根据用户原始目标、"
            "当前观察窗口和当前界面判断当前对象，输出本轮给用户看的结论并选择下一步。"
        )
        return "\n\n".join(parts)

    async def _iter_model_stream_chunks(self, stream: Any) -> AsyncGenerator[Any, None]:
        iterator = stream.__aiter__()
        while True:
            try:
                yield await asyncio.wait_for(
                    iterator.__anext__(),
                    timeout=MODEL_STREAM_CHUNK_TIMEOUT_SECONDS,
                )
            except StopAsyncIteration:
                return
            except TimeoutError as exc:
                raise TimeoutError(
                    "Model stream produced no output for "
                    f"{MODEL_STREAM_CHUNK_TIMEOUT_SECONDS:g} seconds"
                ) from exc

    async def _create_model_stream(self, messages: list[dict[str, Any]]) -> Any:
        stats = _collect_model_message_stats(messages)
        with trace_span(
            "model.stream.create",
            attrs={
                "agent_type": self.__class__.__name__,
                "model_name": self.model_config.model_name,
                "message_count": len(messages),
                "timeout_seconds": MODEL_STREAM_CREATE_TIMEOUT_SECONDS,
                **stats,
            },
        ):
            try:
                return await asyncio.wait_for(
                    self.openai_client.chat.completions.create(
                        messages=messages,  # type: ignore[arg-type]
                        model=self.model_config.model_name,
                        max_tokens=self.model_config.max_tokens,
                        temperature=self.model_config.temperature,
                        top_p=self.model_config.top_p,
                        frequency_penalty=self.model_config.frequency_penalty,
                        extra_body=self.model_config.extra_body,
                        stream=True,
                    ),
                    timeout=MODEL_STREAM_CREATE_TIMEOUT_SECONDS,
                )
            except TimeoutError as exc:
                raise TimeoutError(
                    "Model stream was not created within "
                    f"{MODEL_STREAM_CREATE_TIMEOUT_SECONDS:g} seconds"
                ) from exc

    def _is_empty_finish_step(self, step_data: dict[str, Any]) -> bool:
        action = step_data.get("action") or {}
        action_message = action.get("message")
        step_message = step_data.get("message")
        return (
            action.get("_metadata") == "finish"
            and not str(action_message or "").strip()
            and not str(step_message or "").strip()
        )

    def _build_strict_finish_recovery_instruction(
        self, step_data: dict[str, Any]
    ) -> str:
        action = step_data.get("action") or {}
        finish_message = str(
            action.get("message") or step_data.get("message") or ""
        ).strip()
        previous_summary = (
            f"\n模型刚才给出的 finish message：{finish_message}"
            if finish_message
            else "\n模型刚才给出的是空 finish，没有可展示小结。"
        )
        if self._is_empty_finish_step(step_data):
            violation = (
                "上一次模型返回了空 finish，这是无效步骤：没有给用户可见总结，"
                "也没有执行切换动作。"
            )
        else:
            violation = (
                "上一次模型调用了 finish，但当前是严格运行模式，finish 不是有效动作。"
            )
        return (
            f"{violation}{previous_summary}\n"
            "必须立即基于刚才同一轮观察窗口内的多张截图补做当前对象结论，"
            "然后执行一个切换到下一个对象的 do(...) 动作。\n"
            "硬性要求：\n"
            "1. 不要再次调用 finish。\n"
            "2. 不要只把总结写在 thinking 中。\n"
            '3. 切换动作必须携带 message="OBJECT_SUMMARY: ..."，'
            "message 内容要直接给用户展示，并按用户原始目标和当前对象类型自动选择总结结构。"
            "视频任务偏内容/文案/画面/观点；App 体验偏流程/交互/状态变化/问题；"
            "游戏任务偏目标/操作反馈/关卡机制/体验卡点；商品/帖子/页面偏主体内容、"
            "关键信息和判断依据。UI、控件、状态、布局、文案、账号/来源、时间、"
            "价格、互动数据等是否是核心，必须由用户目标决定；App、游戏、通讯、"
            "流程、体验任务中，UI 与交互状态通常就是核心证据。只有当这些信息和"
            "用户目标无关时，才把它们降为背景或忽略。\n"
            "4. 如果当前界面是短视频/信息流，优先使用 Swipe 切换到下一个对象；"
            "如果当前界面不是信息流，再选择合适的 Tap/Back/Wait 等动作。"
        )

    def _consume_strict_finish_recovery_instruction(self) -> str | None:
        if not self._strict_finish_recovery_pending:
            return None
        self._strict_finish_recovery_pending = False
        return (
            "严格运行模式恢复步骤：复用刚才采集的观察窗口截图，"
            "不要重新采样。你必须基于这些截图输出 OBJECT_SUMMARY 并执行切换动作。"
        )

    async def stream(
        self, task: str, *, continue_with: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """流式执行任务，支持取消和继续执行。

        Args:
            task: 任务文本或用户输入
            continue_with: 如果设置，表示在 Take_over/Interact 后继续执行。
                           不重置 step_count，将用户输入添加到上下文后继续循环。
        """
        INTERACTION_ACTIONS = ("Take_over", "Interact")

        self._is_running = True
        if continue_with is None:
            self._step_count = 0
            self._active_task = task
        else:
            logger.info(
                "Continuing agent stream from step %d with user input: %s",
                self._step_count,
                summarize_text(continue_with) or "",
            )
        self._cancel_event.clear()

        if continue_with is not None:
            self._context.append(MessageBuilder.create_user_message(continue_with))

        with trace_span(
            "agent.stream",
            attrs={
                "agent_type": self.__class__.__name__,
                "device_id": self.device.device_id,
                "model_name": self.model_config.model_name,
                "run_limit_type": self.agent_config.run_limit_type,
                "max_steps": self.agent_config.max_steps,
                "max_duration_seconds": self.agent_config.max_duration_seconds,
                "task_preview": summarize_text(task) or "",
            },
        ) as stream_span:
            try:
                if continue_with is None:
                    try:
                        with trace_span(
                            "agent.prepare_initial_state",
                            attrs={
                                "agent_type": self.__class__.__name__,
                                "device_id": self.device.device_id,
                            },
                        ):
                            screenshot = await asyncio.to_thread(
                                self.device.get_screenshot
                            )
                            current_app = await asyncio.to_thread(
                                self.device.get_current_app
                            )
                    except Exception as e:
                        logger.error(f"Failed to get device info: {e}")
                        stream_span.set_attributes(
                            {"success": False, "error_kind": "initial_device_state"}
                        )
                        yield {
                            "type": "error",
                            "data": {"message": f"Device error: {e}"},
                        }
                        yield {
                            "type": "done",
                            "data": {
                                "message": f"Device error: {e}",
                                "steps": 0,
                                "success": False,
                            },
                        }
                        return

                    with trace_span(
                        "agent.prepare_initial_context",
                        attrs={"agent_type": self.__class__.__name__},
                    ):
                        self._prepare_initial_context(
                            task,
                            screenshot.base64_data,
                            current_app,
                            self._user_image_attachments,
                        )

                started_at = time.monotonic()
                repeated_action_count = 0
                no_progress_count = 0
                strict_empty_finish_count = 0
                strict_finish_suppressed_count = 0
                last_action_signature: str | None = None

                while self._is_running:
                    elapsed_seconds = time.monotonic() - started_at
                    if (
                        self.agent_config.run_limit_type == "steps"
                        and self.agent_config.max_steps is not None
                        and self._step_count >= self.agent_config.max_steps
                    ):
                        break
                    if (
                        self.agent_config.run_limit_type == "duration"
                        and self.agent_config.max_duration_seconds is not None
                        and elapsed_seconds >= self.agent_config.max_duration_seconds
                    ):
                        break

                    if self._cancel_event.is_set():
                        raise asyncio.CancelledError()

                    step_number = self._step_count + 1
                    with trace_span(
                        "agent.step",
                        attrs={
                            "agent_type": self.__class__.__name__,
                            "step": step_number,
                            "device_id": self.device.device_id,
                        },
                    ) as step_span:
                        async for event in self._execute_step():
                            if (
                                event["type"] == "step"
                                and event["data"].get("finished")
                                and event["data"].get("success", True)
                                and self._is_strict_run_mode()
                            ):
                                strict_finish_suppressed_count += 1
                                strict_empty_finish_count = (
                                    strict_empty_finish_count + 1
                                    if self._is_empty_finish_step(event["data"])
                                    else 0
                                )
                                event = {
                                    **event,
                                    "data": {
                                        **event["data"],
                                        "finished": False,
                                        "finish_suppressed": True,
                                        "strict_recovery_pending": True,
                                        "empty_finish_count": strict_empty_finish_count,
                                        "suppressed_finish_count": strict_finish_suppressed_count,
                                    },
                                }
                                self._context.append(
                                    MessageBuilder.create_user_message(
                                        self._build_strict_finish_recovery_instruction(
                                            event["data"]
                                        )
                                    )
                                )
                                self._strict_finish_recovery_pending = True
                                self._strict_finish_recovery_attempts = 0
                            if event["type"] == "step":
                                if not event["data"].get("finish_suppressed"):
                                    strict_empty_finish_count = 0
                                    strict_finish_suppressed_count = 0
                                step_span.set_attributes(
                                    {
                                        "success": event["data"].get("success"),
                                        "finished": event["data"].get("finished"),
                                        "action_name": (
                                            event["data"].get("action") or {}
                                        ).get("action"),
                                    }
                                )
                            if event["type"] == "step":
                                action_signature = json.dumps(
                                    event["data"].get("action"),
                                    ensure_ascii=False,
                                    sort_keys=True,
                                )
                                if action_signature and action_signature != "null":
                                    if action_signature == last_action_signature:
                                        repeated_action_count += 1
                                    else:
                                        last_action_signature = action_signature
                                        repeated_action_count = 1
                                    no_progress_count = 0
                                else:
                                    no_progress_count += 1

                            yield event

                            if (
                                event["type"] == "step"
                                and event["data"].get("finish_suppressed")
                                and (
                                    strict_empty_finish_count
                                    >= STRICT_EMPTY_FINISH_LIMIT
                                    or strict_finish_suppressed_count
                                    >= STRICT_FINISH_SUPPRESSION_LIMIT
                                )
                            ):
                                if (
                                    strict_empty_finish_count
                                    >= STRICT_EMPTY_FINISH_LIMIT
                                ):
                                    stop_reason = "strict_empty_finish_loop"
                                    message = (
                                        "严格运行模式已停止：模型连续返回空 finish，"
                                        "没有按要求输出 OBJECT_SUMMARY 或切换下一个对象。"
                                    )
                                else:
                                    stop_reason = "strict_finish_loop"
                                    message = (
                                        "严格运行模式已停止：模型连续调用 finish，"
                                        "没有按要求执行携带 OBJECT_SUMMARY 的切换动作。"
                                    )
                                stream_span.set_attributes(
                                    {
                                        "success": False,
                                        "steps": self._step_count,
                                        "error_kind": stop_reason,
                                    }
                                )
                                yield {
                                    "type": "done",
                                    "data": {
                                        "message": message,
                                        "steps": self._step_count,
                                        "success": False,
                                        "stop_reason": stop_reason,
                                    },
                                }
                                return

                            if event["type"] == "step":
                                step_data = event["data"]
                                action = step_data.get("action") or {}
                                if (
                                    step_data.get("waiting_for_input")
                                    or action.get("action") in INTERACTION_ACTIONS
                                ):
                                    yield {
                                        "type": "takeover",
                                        "data": {
                                            "message": step_data.get("message", ""),
                                            "steps": self._step_count,
                                            "success": True,
                                            "stop_reason": "takeover",
                                        },
                                    }
                                    return

                            if (
                                event["type"] == "step"
                                and event["data"].get("finished")
                                and not event["data"].get("success", True)
                            ):
                                stream_span.set_attributes(
                                    {
                                        "success": False,
                                        "steps": self._step_count,
                                        "error_kind": "step_failed",
                                    }
                                )
                                yield {
                                    "type": "done",
                                    "data": {
                                        "message": event["data"].get("message")
                                        or "Task failed",
                                        "steps": self._step_count,
                                        "success": False,
                                        "stop_reason": "step_failed",
                                    },
                                }
                                return

                            if (
                                event["type"] == "step"
                                and event["data"].get("finished")
                                and self.agent_config.run_limit_type == "autonomous"
                            ):
                                success = event["data"].get("success", True)
                                stream_span.set_attributes(
                                    {
                                        "success": success,
                                        "steps": self._step_count,
                                    }
                                )
                                yield {
                                    "type": "done",
                                    "data": {
                                        "message": event["data"].get("message")
                                        or (
                                            "Task completed"
                                            if success
                                            else "Task failed"
                                        ),
                                        "steps": self._step_count,
                                        "success": success,
                                    },
                                }
                                return

                            if repeated_action_count >= WATCHDOG_REPEATED_ACTION_LIMIT:
                                stream_span.set_attributes(
                                    {
                                        "success": False,
                                        "steps": self._step_count,
                                        "error_kind": "watchdog_repeated_actions",
                                    }
                                )
                                yield {
                                    "type": "done",
                                    "data": {
                                        "message": "Watchdog stopped task after repeated actions",
                                        "steps": self._step_count,
                                        "success": False,
                                        "stop_reason": "watchdog_repeated_actions",
                                    },
                                }
                                return

                            if no_progress_count >= WATCHDOG_NO_PROGRESS_LIMIT:
                                stream_span.set_attributes(
                                    {
                                        "success": False,
                                        "steps": self._step_count,
                                        "error_kind": "watchdog_no_progress",
                                    }
                                )
                                yield {
                                    "type": "done",
                                    "data": {
                                        "message": "Watchdog stopped task because no progress was detected",
                                        "steps": self._step_count,
                                        "success": False,
                                        "stop_reason": "watchdog_no_progress",
                                    },
                                }
                                return

                if (
                    self.agent_config.run_limit_type == "duration"
                    and self.agent_config.max_duration_seconds is not None
                    and time.monotonic() - started_at
                    >= self.agent_config.max_duration_seconds
                ):
                    stream_span.set_attributes(
                        {
                            "success": False,
                            "steps": self._step_count,
                            "error_kind": "max_duration",
                        }
                    )
                    yield {
                        "type": "done",
                        "data": {
                            "message": "Max duration reached",
                            "steps": self._step_count,
                            "success": False,
                            "stop_reason": "max_duration_reached",
                        },
                    }
                    return

                if (
                    self.agent_config.run_limit_type == "steps"
                    and self.agent_config.max_steps is not None
                    and self._step_count >= self.agent_config.max_steps
                ):
                    stream_span.set_attributes(
                        {
                            "success": False,
                            "steps": self._step_count,
                            "error_kind": "max_steps",
                        }
                    )
                    yield {
                        "type": "done",
                        "data": {
                            "message": "Max steps reached",
                            "steps": self._step_count,
                            "success": False,
                            "stop_reason": "max_steps_reached",
                        },
                    }

            except asyncio.CancelledError:
                stream_span.set_attributes(
                    {
                        "success": False,
                        "steps": self._step_count,
                        "error_kind": "cancelled",
                    }
                )
                yield {
                    "type": "cancelled",
                    "data": {
                        "message": "Task cancelled by user",
                        "stop_reason": "user_stopped",
                    },
                }
                raise

            finally:
                self._user_image_attachments = []
                self._is_running = False

    def set_user_image_attachments(self, attachments: list[dict[str, str]]) -> None:
        """Set user-supplied reference images for the next streamed task."""
        self._user_image_attachments = attachments.copy()

    async def cancel(self) -> None:
        """取消当前执行。"""
        self._cancel_event.set()
        self._is_running = False
        logger.info(f"{self.__class__.__name__} cancelled by user")

    def reset(self) -> None:
        """重置状态。"""
        self._context = [copy.deepcopy(self._initial_system_message)]
        self._user_image_attachments = []
        self._step_count = 0
        self._active_task = None
        self._strict_finish_recovery_pending = False
        self._strict_finish_recovery_attempts = 0
        self._state_checkpoints = []
        self._is_running = False
        self._cancel_event.clear()

    async def run(self, task: str) -> str:
        """运行完整任务（兼容接口）。"""
        final_message = ""
        async for event in self.stream(task):
            if event["type"] == "done":
                final_message = event["data"].get("message", "")
        return final_message

    @property
    def step_count(self) -> int:
        return self._step_count

    @property
    def context(self) -> list[dict[str, Any]]:
        return self._context.copy()

    @property
    def is_running(self) -> bool:
        return self._is_running
