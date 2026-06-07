"""Lightweight experience plan parser for chat experience mode."""

from __future__ import annotations

from dataclasses import dataclass

from AutoGLM_GUI.schemas import ExperiencePlan, ExperiencePlanResponse


DEFAULT_EVALUATION_DIMENSIONS = [
    "美术表现",
    "核心玩法体验",
    "系统设计",
    "商业化设计",
]


_OBSERVATION_HINTS: dict[str, list[str]] = {
    "任务": ["任务内容", "任务流程", "任务目标反馈"],
    "数值": ["显式数值变化", "奖励与消耗变化", "成长反馈"],
    "付费": ["付费点", "价格梯度", "弹窗与礼包触发时机"],
    "广告": ["广告触发频率", "广告出现时机", "广告干扰程度"],
    "引导": ["新手引导文案", "引导步骤", "学习成本"],
    "战斗": ["战斗反馈", "战斗节奏", "战斗门槛"],
    "美术": ["画面风格", "界面质感", "视觉一致性"],
}

_ANALYSIS_HINTS: dict[str, list[str]] = {
    "任务": ["任务难度曲线", "任务卡点识别", "任务设计合理性"],
    "数值": ["数值压力变化", "奖励合理性", "成长节奏"],
    "付费": ["付费曲线", "商业化压力", "价值感知"],
    "广告": ["广告干扰评估", "收益交换合理性"],
    "引导": ["引导清晰度", "上手门槛", "早期流失风险"],
    "战斗": ["玩法节奏", "战斗挫败感", "核心体验一致性"],
    "美术": ["美术风格适配度", "视觉吸引力"],
}


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


def _extract_observation_targets(text: str) -> list[str]:
    targets: list[str] = []
    for keyword, values in _OBSERVATION_HINTS.items():
        if keyword in text:
            targets.extend(values)
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
    if not lenses:
        lenses = ["关键问题归纳", "体验亮点提炼", "潜在风险判断"]
    return _dedupe(lenses)


def _extract_evaluation_dimensions(text: str) -> list[str]:
    dimensions: list[str] = []
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
    if not conditions:
        conditions.append("覆盖目标相关关键路径后停止")
    return _dedupe(conditions)


def _build_sampling_strategy(
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
    return _dedupe(strategy)


def _guess_missing_fields(
    text: str,
    evaluation_dimensions: list[str],
) -> tuple[list[str], str | None]:
    missing_fields: list[str] = []
    question: str | None = None

    if not _contains_any(text, ("前期", "中期", "后期", "完整", "全流程", "新手")):
        missing_fields.append("scope")
    if not evaluation_dimensions and not _contains_any(
        text, ("付费", "广告", "任务", "引导", "战斗", "美术")
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
            _extract_observation_targets(text),
            _extract_analysis_lenses(text),
        ),
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
