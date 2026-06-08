"""Lightweight experience plan parser for chat experience mode."""

from __future__ import annotations

from dataclasses import dataclass

from AutoGLM_GUI.config import MemoryPolicy
from AutoGLM_GUI.schemas import ExperiencePlan, ExperiencePlanResponse


DEFAULT_EVALUATION_DIMENSIONS = [
    "美术表现",
    "核心玩法体验",
    "系统设计",
    "商业化设计",
]


_OBSERVATION_HINTS: dict[str, list[str]] = {
    "内容": ["核心内容主题", "关键信息点", "内容呈现方式"],
    "文案": ["核心文案", "标题/字幕/按钮文案", "文案与画面或页面元素的配合"],
    "卖点": ["核心卖点", "卖点呈现位置", "卖点可信度证据"],
    "信息": ["关键信息结构", "信息传达清晰度", "信息缺失或歧义"],
    "视频": ["画面内容", "字幕/口播/音乐", "内容节奏变化"],
    "任务": ["任务内容", "任务流程", "任务目标反馈"],
    "数值": ["显式数值变化", "奖励与消耗变化", "成长反馈"],
    "付费": ["付费点", "价格梯度", "弹窗与礼包触发时机"],
    "广告": ["广告触发频率", "广告出现时机", "广告干扰程度"],
    "引导": ["新手引导文案", "引导步骤", "学习成本"],
    "战斗": ["战斗反馈", "战斗节奏", "战斗门槛"],
    "美术": ["画面风格", "界面质感", "视觉一致性"],
}

_ANALYSIS_HINTS: dict[str, list[str]] = {
    "内容": ["内容主题提炼", "信息表达有效性", "内容吸引力判断"],
    "文案": ["核心文案提炼", "文案清晰度", "文案转化或引导作用"],
    "卖点": ["卖点表达强度", "卖点差异化", "价值感知"],
    "信息": ["信息层级合理性", "信息理解成本", "信息完整性"],
    "视频": ["内容节奏", "画面/字幕/口播协同", "观看吸引力"],
    "任务": ["任务难度曲线", "任务卡点识别", "任务设计合理性"],
    "数值": ["数值压力变化", "奖励合理性", "成长节奏"],
    "付费": ["付费曲线", "商业化压力", "价值感知"],
    "广告": ["广告干扰评估", "收益交换合理性"],
    "引导": ["引导清晰度", "上手门槛", "早期流失风险"],
    "战斗": ["玩法节奏", "战斗挫败感", "核心体验一致性"],
    "美术": ["美术风格适配度", "视觉吸引力"],
}

_CONTENT_FOCUS_KEYWORDS = (
    "内容",
    "文案",
    "卖点",
    "信息",
    "视频",
    "字幕",
    "口播",
    "标题",
    "画面",
    "音乐",
)

_ITERATIVE_ITEM_KEYWORDS = (
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

_ITEM_OBSERVATION_KEYWORDS = (
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
    "几秒",
)

_ITEM_SUMMARY_KEYWORDS = (
    "提炼",
    "总结",
    "归纳",
    "分析",
    "判断",
    "识别",
    "记录",
    "内容",
    "要点",
    "结论",
)

_ITEM_SWITCH_KEYWORDS = (
    "下一个",
    "下一条",
    "下一页",
    "切换",
    "滑动",
    "翻页",
    "继续",
    "换下一个",
)

_STATEFUL_FLOW_KEYWORDS = (
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

_INDEPENDENT_ITEM_KEYWORDS = (
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


@dataclass(frozen=True)
class _PlanDraft:
    plan: ExperiencePlan
    missing_fields: list[str]
    question: str | None


def _dedupe(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = item.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _requires_iterative_item_sampling(text: str) -> bool:
    has_iteration = _contains_any(text, _ITERATIVE_ITEM_KEYWORDS)
    has_observation = _contains_any(text, _ITEM_OBSERVATION_KEYWORDS)
    has_summary = _contains_any(text, _ITEM_SUMMARY_KEYWORDS)
    has_switch = _contains_any(text, _ITEM_SWITCH_KEYWORDS)
    return (has_iteration or has_switch) and has_observation and has_summary


def _extract_observation_targets(text: str) -> list[str]:
    targets: list[str] = []
    for keyword, values in _OBSERVATION_HINTS.items():
        if keyword in text:
            targets.extend(values)
    if _requires_iterative_item_sampling(text):
        targets.extend(
            [
                "单个观察对象的连续状态变化",
                "每个观察对象的关键内容与证据",
                "切换前后的对象边界",
            ]
        )
    if not targets:
        targets = ["关键页面变化", "关键文案", "关键截图"]
    return _dedupe(targets)


def _extract_analysis_lenses(text: str) -> list[str]:
    lenses: list[str] = []
    for keyword, values in _ANALYSIS_HINTS.items():
        if keyword in text:
            lenses.extend(values)

    if _contains_any(text, ("综合", "比较", "评测", "评价")):
        lenses.extend(["综合优劣比较", "关键问题归纳", "体验风险判断"])
    if _requires_iterative_item_sampling(text):
        lenses.extend(["同一对象多次采样后的综合判断", "不同对象之间的内容差异"])
    if not lenses:
        lenses = ["关键问题归纳", "体验亮点提炼", "潜在风险判断"]
    return _dedupe(lenses)


def _extract_evaluation_dimensions(text: str) -> list[str]:
    dimensions: list[str] = []
    if _contains_any(text, _CONTENT_FOCUS_KEYWORDS):
        dimensions.append("内容表达")
    if "美术" in text:
        dimensions.append("美术表现")
    if _contains_any(text, ("玩法", "游玩", "战斗", "体验")):
        dimensions.append("核心玩法体验")
    if _contains_any(text, ("系统", "任务", "设计")):
        dimensions.append("系统设计")
    if _contains_any(text, ("付费", "商业化", "首充", "礼包")):
        dimensions.append("商业化设计")
    if _contains_any(text, ("广告", "商业化")):
        dimensions.append("广告/商业化干扰")

    if _contains_any(text, ("综合", "综合游戏性", "综合评价", "综合评测")):
        dimensions.extend(DEFAULT_EVALUATION_DIMENSIONS)
    return _dedupe(dimensions)


def _extract_report_request(text: str) -> str:
    for marker in ("最后", "输出", "生成", "总结", "给一个", "给我"):
        idx = text.find(marker)
        if idx != -1:
            report = text[idx:].strip("，, 。")
            if report:
                return report
    return "输出一份包含结论、依据和风险的体验分析报告"


def _extract_stop_conditions(text: str) -> list[str]:
    conditions: list[str] = []
    if _contains_any(text, ("前期", "新手", "前 10 分钟", "前10分钟")):
        conditions.append("覆盖前期/新手流程后停止")
    if _contains_any(text, ("完整流程", "全流程")):
        conditions.append("尽量覆盖完整主流程")
    if "任务" in text:
        conditions.append("覆盖关键任务节点后停止")
    if "付费" in text:
        conditions.append("覆盖关键付费触点后停止")
    if _requires_iterative_item_sampling(text):
        conditions.append("完成若干对象的逐项观察、提炼与切换后停止")
    if not conditions:
        conditions.append("覆盖目标相关关键路径后停止")
    return _dedupe(conditions)


def _build_sampling_strategy(
    text: str,
    observation_targets: list[str],
    analysis_lenses: list[str],
) -> list[str]:
    strategy = [
        "保留关键页面截图和阶段性转折点",
        "记录导致判断变化的关键文案或数字",
    ]
    if any("数值" in target for target in observation_targets):
        strategy.append("重点记录显式数值、奖励和消耗变化")
    if any("付费" in lens or "商业化" in lens for lens in analysis_lenses):
        strategy.append("重点保留付费弹窗、价格和触发时机")
    if any("任务" in lens for lens in analysis_lenses):
        strategy.append("重点记录任务要求、完成门槛和阻塞节点")
    if _contains_any(text, _CONTENT_FOCUS_KEYWORDS):
        strategy.append(
            "重点记录标题、字幕、口播、核心文案、画面或页面中的关键信息证据"
        )
    if _requires_iterative_item_sampling(text):
        strategy.extend(
            [
                "对每个被观察对象按观察窗口配置完成间隔截图/观察，不要只凭单张截图下结论",
                "切换到下一个对象前，先综合当前对象观察窗口内的多帧采样，提炼内容、状态变化和关键证据",
                "切换后明确记录新对象边界，避免把不同对象的证据混在一起",
            ]
        )
    return _dedupe(strategy)


def _infer_memory_policy(text: str) -> MemoryPolicy:
    if _contains_any(text, _STATEFUL_FLOW_KEYWORDS):
        if _requires_iterative_item_sampling(text):
            return "hybrid"
        return "stateful_flow"
    if _requires_iterative_item_sampling(text) and _contains_any(
        text, _INDEPENDENT_ITEM_KEYWORDS
    ):
        return "independent_items"
    return "hybrid"


def _guess_missing_fields(
    text: str,
    evaluation_dimensions: list[str],
) -> tuple[list[str], str | None]:
    missing_fields: list[str] = []
    question: str | None = None

    if not _contains_any(text, ("前期", "中期", "后期", "完整", "全流程", "新手")):
        missing_fields.append("scope")
    if not evaluation_dimensions and not _contains_any(
        text,
        (
            "付费",
            "广告",
            "任务",
            "引导",
            "战斗",
            "美术",
            *_CONTENT_FOCUS_KEYWORDS,
        ),
    ):
        missing_fields.append("focus")
    if not _contains_any(text, ("报告", "总结", "分析", "比较", "评测")):
        missing_fields.append("report")

    if "scope" in missing_fields:
        question = "你更想看前期、新手流程，还是尽量覆盖完整流程？"
    elif "focus" in missing_fields:
        question = "这次更想重点看任务、付费、广告、引导，还是做综合游戏性评测？"
    elif "report" in missing_fields:
        question = "最后的输出更偏结论报告、证据分析，还是维度比较？"
    return missing_fields, question


def _build_plan(text: str) -> _PlanDraft:
    evaluation_dimensions = _extract_evaluation_dimensions(text)
    missing_fields, question = _guess_missing_fields(text, evaluation_dimensions)
    plan = ExperiencePlan(
        execution_goal=text.strip(),
        observation_targets=_extract_observation_targets(text),
        analysis_lenses=_extract_analysis_lenses(text),
        evaluation_dimensions=(
            evaluation_dimensions
            if evaluation_dimensions
            else DEFAULT_EVALUATION_DIMENSIONS.copy()
        ),
        report_request=_extract_report_request(text),
        stop_conditions=_extract_stop_conditions(text),
        sampling_strategy=_build_sampling_strategy(
            text,
            _extract_observation_targets(text),
            _extract_analysis_lenses(text),
        ),
        memory_policy=_infer_memory_policy(text),
    )
    return _PlanDraft(plan=plan, missing_fields=missing_fields, question=question)


def build_experience_plan(messages: list[str]) -> ExperiencePlanResponse:
    conversation = _dedupe(messages)
    merged_text = "\n".join(conversation)
    draft = _build_plan(merged_text)
    stage = "asking" if draft.question else "awaiting_confirmation"
    return ExperiencePlanResponse(
        stage=stage,
        plan=draft.plan,
        question=draft.question,
        missing_fields=draft.missing_fields,
        conversation=conversation,
    )
