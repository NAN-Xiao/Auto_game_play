"""Experience strategy layer for differentiating app vs game experience modes.

This module provides a pluggable strategy that injects context-specific
execution rules into the agent's prompt without modifying the execution
pipeline itself. The base strategy is a no-op (preserving existing generic
app behavior); subclasses add domain-specific intelligence.
"""

from __future__ import annotations

from typing import Any


class ExperienceStrategy:
    """Base strategy: transparent pass-through for generic app experience."""

    def enrich_execution_rules(self, plan: dict[str, Any], original_goal: str) -> list[str]:
        return []

    def build_state_tracking_prompt(self, plan: dict[str, Any]) -> str | None:
        return None

    def build_state_checkpoint_summary(self, plan: dict[str, Any]) -> str | None:
        """Return a template the agent uses to compress state at segment boundaries."""
        return None


class GameExperienceStrategy(ExperienceStrategy):
    """Game-specific strategy: state tracking, goal decomposition, stuck recovery."""

    def enrich_execution_rules(self, plan: dict[str, Any], original_goal: str) -> list[str]:
        rules = [
            "- 状态追踪：每步 thinking 必须维护简短状态块——当前等级/资源/进度、当前子目标、已完成子目标、连续无进展次数、最近 3 次操作是否生效",
            "- 目标分解：将体验目标拆为有序阶段子目标（如：新手引导→主线任务→首次付费点→核心玩法体验），按顺序推进，完成一个标记一个",
            "- 卡关恢复：如果连续 3 步操作无明显进展，按以下优先级尝试——①检查是否有跳过/关闭/自动战斗按钮 ②回退到上一级菜单重新进入 ③尝试其他可点击元素 ④记录卡点并跳过",
            "- 资源决策：遇到消耗类选择（升级/购买/强化/抽卡），优先推进主流程体验，不在支线上消耗资源；记录付费点但不执行真实付费",
            "- 战斗策略：有自动战斗/挂机/扫荡选项时优先开启；手动战斗优先使用技能而非普攻；战斗结束后检查奖励和状态变化",
            "- 界面识别：区分主界面、战斗界面、背包界面、任务界面、商店界面、设置界面；切换界面后先观察再操作",
        ]
        return rules

    def build_state_tracking_prompt(self, plan: dict[str, Any]) -> str | None:
        return (
            "你正在执行游戏长时间体验任务。每步 thinking 中必须包含如下状态块（用于跨步骤连贯决策）：\n"
            "【游戏状态】等级:? | 主要资源:? | 当前子目标:? | 阶段进度:?/? | 无进展计数:?\n"
            "如果某项信息尚未获取，填\"未知\"；获取后立即更新。"
        )

    def build_state_checkpoint_summary(self, plan: dict[str, Any]) -> str | None:
        return (
            "当你感知到阶段性进展（完成一个子目标、进入新区域、解锁新功能）时，"
            "在 thinking 末尾追加一行 【阶段小结】用一句话记录：从哪到哪、获得了什么、花了多少步。"
            "这行会被系统提取用于长期记忆，即使历史消息被裁剪你仍可在 system message 中看到它。"
        )


class EcommerceExperienceStrategy(ExperienceStrategy):
    """E-commerce app strategy: flow tracking, price/promotion awareness."""

    def enrich_execution_rules(self, plan: dict[str, Any], original_goal: str) -> list[str]:
        return [
            "- 流程追踪：记录用户购物路径——搜索→浏览→详情→加购→结算→支付，标记每个环节的转化摩擦",
            "- 价格敏感：重点记录商品价格、优惠券、满减规则、会员价差异",
            "- 弹窗记录：记录每次弹窗的触发时机、内容和关闭方式",
        ]


class SocialExperienceStrategy(ExperienceStrategy):
    """Social/messaging app strategy: interaction flow, content feed."""

    def enrich_execution_rules(self, plan: dict[str, Any], original_goal: str) -> list[str]:
        return [
            "- 交互路径：记录核心社交动作的操作路径和反馈——发消息、点赞、评论、分享、关注",
            "- 信息流体验：记录内容加载速度、推荐相关性、刷新机制",
            "- 通知与打扰：记录推送、弹窗、红点的触发频率和干扰程度",
        ]


_GAME_KEYWORDS = ("游戏", "游玩", "关卡", "战斗", "玩法", "副本", "刷图", "抽卡", "角色养成", "PVP", "PVE")
_ECOMMERCE_KEYWORDS = ("购物", "电商", "淘宝", "京东", "拼多多", "下单", "购买", "加购", "商城")
_SOCIAL_KEYWORDS = ("社交", "聊天", "通讯", "朋友圈", "动态", "微信", "QQ", "微博")


def select_strategy(memory_policy: str, original_goal: str) -> ExperienceStrategy:
    """Select the appropriate strategy based on memory policy and goal semantics."""
    if memory_policy == "stateful_flow":
        if any(kw in original_goal for kw in _GAME_KEYWORDS):
            return GameExperienceStrategy()
        if any(kw in original_goal for kw in _ECOMMERCE_KEYWORDS):
            return EcommerceExperienceStrategy()
        if any(kw in original_goal for kw in _SOCIAL_KEYWORDS):
            return SocialExperienceStrategy()
    return ExperienceStrategy()
