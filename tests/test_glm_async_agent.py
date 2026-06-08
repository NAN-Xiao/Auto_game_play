"""Tests for AsyncGLMAgent multimodal context handling.

Regression coverage for issue #348: a stale initial screenshot leaked into
every LLM request (the per-step ``remove_images_from_message`` only stripped
the *last* message), so ``autoglm-phone`` saw two screenshots per turn and
produced wrong ``Tap`` coordinates. Each request must carry exactly one
screenshot — the current one — unless the user explicitly attached reference
images for the first turn. History must not retain images.
"""

import asyncio
import base64
import copy
import json
from io import BytesIO
from typing import Any

from PIL import Image

from AutoGLM_GUI.actions import ActionResult
from AutoGLM_GUI.agents.glm.async_agent import AsyncGLMAgent
from AutoGLM_GUI.config import AgentConfig, ModelConfig
from AutoGLM_GUI.device_protocol import Screenshot
from AutoGLM_GUI.model import MessageBuilder


def _count_images(messages: list[dict[str, Any]]) -> int:
    count = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            count += sum(
                1
                for part in content
                if isinstance(part, dict) and part.get("type") == "image_url"
            )
    return count


def _text_part(message: dict[str, Any]) -> str:
    return next(
        part["text"]
        for part in message["content"]
        if isinstance(part, dict) and part.get("type") == "text"
    )


class _FakeDevice:
    device_id = "fake-001"

    def __init__(self) -> None:
        self._n = 0
        self.swipes: list[tuple[int, int, int, int]] = []

    def get_screenshot(self, timeout: int = 10) -> Screenshot:
        self._n += 1
        return Screenshot(base64_data=f"img{self._n}", width=1080, height=2400)

    def get_current_app(self) -> str:
        return "com.example.app"

    def swipe(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int | None = None,
        delay: float | None = None,
    ) -> None:
        _ = (duration_ms, delay)
        self.swipes.append((start_x, start_y, end_x, end_y))


def _make_agent() -> AsyncGLMAgent:
    return AsyncGLMAgent(
        model_config=ModelConfig(),
        agent_config=AgentConfig(max_steps=10, verbose=False),
        device=_FakeDevice(),
    )


def _make_observation_agent() -> AsyncGLMAgent:
    return AsyncGLMAgent(
        model_config=ModelConfig(),
        agent_config=AgentConfig(
            max_steps=10,
            verbose=False,
            observation_window_enabled=False,
            observation_window_screenshot_count=3,
            observation_window_interval_seconds=0,
        ),
        device=_FakeDevice(),
    )


def _make_unlimited_observation_agent() -> AsyncGLMAgent:
    return AsyncGLMAgent(
        model_config=ModelConfig(),
        agent_config=AgentConfig(
            max_steps=None,
            run_limit_type="unlimited",
            verbose=False,
            observation_window_enabled=False,
            observation_window_screenshot_count=3,
            observation_window_interval_seconds=0,
        ),
        device=_FakeDevice(),
    )


async def _drain(agen) -> None:
    async for _ in agen:
        pass


# ---------------------------------------------------------------------------
# MessageBuilder
# ---------------------------------------------------------------------------


def test_create_user_message_puts_image_first():
    msg = MessageBuilder.create_user_message("hello", image_base64="abc")
    assert [part["type"] for part in msg["content"]] == ["image_url", "text"]
    assert msg["content"][1]["text"] == "hello"


def test_remove_images_keeps_text_parts_as_list():
    msg = MessageBuilder.create_user_message("hello", image_base64="abc")
    stripped = MessageBuilder.remove_images_from_message(msg)

    assert isinstance(stripped["content"], list)
    assert _count_images([stripped]) == 0
    assert stripped["content"] == [{"type": "text", "text": "hello"}]


def test_remove_images_passes_through_string_content():
    msg = MessageBuilder.create_assistant_message("done")
    assert MessageBuilder.remove_images_from_message(msg) == msg


def test_create_user_message_with_images_preserves_order_and_mime_types():
    msg = MessageBuilder.create_user_message_with_images(
        "hello",
        [
            {"mime_type": "image/png", "data": "screen"},
            {"mime_type": "image/jpeg", "data": "reference"},
        ],
    )

    assert [part["type"] for part in msg["content"]] == [
        "image_url",
        "image_url",
        "text",
    ]
    assert msg["content"][0]["image_url"]["url"] == "data:image/png;base64,screen"
    assert msg["content"][1]["image_url"]["url"] == "data:image/jpeg;base64,reference"
    assert msg["content"][2]["text"] == "hello"


def test_create_user_message_with_images_downscales_valid_screenshots():
    original = Image.new("RGB", (1600, 2400), "white")
    original_buffer = BytesIO()
    original.save(original_buffer, format="PNG")
    original_base64 = base64.b64encode(original_buffer.getvalue()).decode("ascii")

    msg = MessageBuilder.create_user_message_with_images(
        "hello",
        [{"mime_type": "image/png", "data": original_base64}],
    )

    data_url = msg["content"][0]["image_url"]["url"]
    assert data_url.startswith("data:image/jpeg;base64,")

    compressed_base64 = data_url.removeprefix("data:image/jpeg;base64,")
    assert len(compressed_base64) < len(original_base64)

    with Image.open(BytesIO(base64.b64decode(compressed_base64))) as compressed:
        assert max(compressed.size) <= 1024


def test_create_user_message_with_images_uses_smaller_budget_for_large_batches():
    original = Image.new("RGB", (1600, 2400), "white")
    original_buffer = BytesIO()
    original.save(original_buffer, format="PNG")
    original_base64 = base64.b64encode(original_buffer.getvalue()).decode("ascii")

    msg = MessageBuilder.create_user_message_with_images(
        "hello",
        [{"mime_type": "image/png", "data": original_base64} for _ in range(10)],
    )

    image_parts = [part for part in msg["content"] if part.get("type") == "image_url"]
    assert len(image_parts) == 10
    for part in image_parts:
        data_url = part["image_url"]["url"]
        assert data_url.startswith("data:image/jpeg;base64,")
        compressed_base64 = data_url.removeprefix("data:image/jpeg;base64,")
        with Image.open(BytesIO(base64.b64decode(compressed_base64))) as compressed:
            assert max(compressed.size) <= 640


# ---------------------------------------------------------------------------
# AsyncGLMAgent context invariant
# ---------------------------------------------------------------------------


def test_each_request_carries_exactly_one_image(monkeypatch):
    agent = _make_agent()

    captured: list[list[dict[str, Any]]] = []
    raw_responses = iter(
        [
            "I will tap the icon.\ndo(action=Tap(element=[500, 500]))",
            "All done.\nfinish(message=ok)",
        ]
    )

    async def fake_stream(messages):
        captured.append(copy.deepcopy(messages))
        yield {"type": "raw", "content": next(raw_responses)}

    action_results = iter(
        [
            ActionResult(success=True, should_finish=False, message=None),
            ActionResult(success=True, should_finish=True, message="ok"),
        ]
    )

    monkeypatch.setattr(agent, "_stream_openai", fake_stream)
    monkeypatch.setattr(
        agent.action_handler, "execute", lambda *a, **k: next(action_results)
    )

    async def run() -> None:
        agent._prepare_initial_context("play a song", "img0", "com.android.launcher")
        await _drain(agent._execute_step())
        await _drain(agent._execute_step())

    asyncio.run(run())

    assert len(captured) == 2
    for request in captured:
        assert _count_images(request) == 1
        # the lone image must sit on the last (current) user message
        assert request[-1]["role"] == "user"
        assert any(
            isinstance(p, dict) and p.get("type") == "image_url"
            for p in request[-1]["content"]
        )

    # task text is part of the first request, not repeated afterwards
    assert "play a song" in _text_part(captured[0][-1])
    assert "play a song" not in _text_part(captured[1][-1])


def test_context_retains_no_images_after_step(monkeypatch):
    agent = _make_agent()

    async def fake_stream(messages):
        yield {"type": "raw", "content": "do(action=Tap(element=[1, 1]))"}

    monkeypatch.setattr(agent, "_stream_openai", fake_stream)
    monkeypatch.setattr(
        agent.action_handler,
        "execute",
        lambda *a, **k: ActionResult(success=True, should_finish=False, message=None),
    )

    async def run() -> None:
        agent._prepare_initial_context("task", "img0", "app")
        await _drain(agent._execute_step())

    asyncio.run(run())

    assert _count_images(agent._context) == 0


def test_user_reference_images_are_sent_on_first_request_only(monkeypatch):
    agent = _make_agent()

    captured: list[list[dict[str, Any]]] = []
    raw_responses = iter(
        [
            "I will tap.\ndo(action=Tap(element=[500, 500]))",
            "Done.\nfinish(message=ok)",
        ]
    )

    async def fake_stream(messages):
        captured.append(copy.deepcopy(messages))
        yield {"type": "raw", "content": next(raw_responses)}

    action_results = iter(
        [
            ActionResult(success=True, should_finish=False, message=None),
            ActionResult(success=True, should_finish=True, message="ok"),
        ]
    )

    monkeypatch.setattr(agent, "_stream_openai", fake_stream)
    monkeypatch.setattr(
        agent.action_handler, "execute", lambda *a, **k: next(action_results)
    )

    async def run() -> None:
        agent._prepare_initial_context(
            "use the attached image",
            "img0",
            "com.android.launcher",
            reference_images=[{"mime_type": "image/jpeg", "data": "ref1"}],
        )
        await _drain(agent._execute_step())
        await _drain(agent._execute_step())

    asyncio.run(run())

    assert len(captured) == 2
    assert _count_images(captured[0]) == 2
    assert _count_images(captured[1]) == 1
    assert "User attached 1 reference image" in _text_part(captured[0][-1])


def test_observation_window_streams_sample_progress(monkeypatch):
    agent = _make_observation_agent()
    captured: list[list[dict[str, Any]]] = []

    async def fake_stream(messages):
        captured.append(copy.deepcopy(messages))
        yield {"type": "raw", "content": "Done.\nfinish(message=ok)"}

    monkeypatch.setattr(agent, "_stream_openai", fake_stream)
    monkeypatch.setattr(
        agent.action_handler,
        "execute",
        lambda *a, **k: ActionResult(success=True, should_finish=True, message="ok"),
    )

    async def run() -> list[dict[str, Any]]:
        agent._prepare_initial_context("watch", "img0", "app")
        events: list[dict[str, Any]] = []
        async for event in agent._execute_step():
            events.append(event)
        return events

    events = asyncio.run(run())
    observation_events = [event for event in events if event["type"] == "observation"]

    assert [event["data"]["phase"] for event in observation_events] == [
        "start",
        "sample",
        "sample",
        "sample",
        "complete",
    ]
    assert [event["data"].get("sample_index") for event in observation_events] == [
        0,
        1,
        2,
        3,
        3,
    ]
    assert observation_events[1]["data"]["screenshot"] == "img1"
    assert observation_events[-1]["data"]["message"] == (
        "截帧完成：已采集 3 张截图，开始一次性多模态综合分析。"
    )
    assert len(captured) == 1
    assert _count_images(captured[0]) == 3


def test_observation_window_captures_first_sample_without_wait(monkeypatch):
    agent = _make_observation_agent()
    wait_sample_indices: list[int] = []

    async def fake_wait(*, sample_index: int, sample_count: int) -> None:
        wait_sample_indices.append(sample_index)

    monkeypatch.setattr(agent, "_wait_observation_window_interval", fake_wait)

    async def run() -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        async for event in agent._observe_observation_window():
            if isinstance(event, dict):
                events.append(event)
        return events

    events = asyncio.run(run())
    sample_events = [
        event
        for event in events
        if event["type"] == "observation" and event["data"]["phase"] == "sample"
    ]

    assert wait_sample_indices == [2, 3]
    assert [event["data"]["sample_index"] for event in sample_events] == [1, 2, 3]


def test_strict_empty_finish_recovery_reuses_observation_window(monkeypatch):
    agent = _make_unlimited_observation_agent()
    captured: list[list[dict[str, Any]]] = []
    raw_responses = iter(
        [
            'finish(message="")',
            (
                'do(action="Swipe", start=[500, 800], end=[500, 200], '
                'message="OBJECT_SUMMARY: 当前视频讲解热点新闻，字幕信息明确。")'
            ),
            'finish(message="")',
            'finish(message="")',
        ]
    )

    async def fake_stream(messages):
        captured.append(copy.deepcopy(messages))
        yield {"type": "raw", "content": next(raw_responses)}

    monkeypatch.setattr(agent, "_stream_openai", fake_stream)

    async def run() -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        async for event in agent.stream(
            "观看抖音视频 每个视频根据画面主体和视频文案生成总结后自动切下一个视频"
        ):
            events.append(event)
        return events

    events = asyncio.run(run())
    sample_events = [
        event
        for event in events
        if event["type"] == "observation" and event["data"]["phase"] == "sample"
    ]
    step_events = [event for event in events if event["type"] == "step"]

    assert len(captured) == 4
    assert [event["data"]["screenshot"] for event in sample_events[:3]] == [
        "img2",
        "img3",
        "img4",
    ]
    assert _count_images(captured[0]) == 3
    assert _count_images(captured[1]) == 3
    assert [part["image_url"]["url"] for part in captured[0][-1]["content"][:3]] == [
        part["image_url"]["url"] for part in captured[1][-1]["content"][:3]
    ]
    assert "Strict Mode Recovery" in _text_part(captured[1][-1])
    assert step_events[0]["data"]["finish_suppressed"] is True
    assert step_events[1]["data"]["action"]["action"] == "Swipe"
    assert step_events[1]["data"]["message"].startswith("OBJECT_SUMMARY:")
    assert events[-1]["type"] == "done"
    assert events[-1]["data"]["stop_reason"] == "strict_empty_finish_loop"


def test_iterative_observation_turns_do_not_carry_previous_history(monkeypatch):
    agent = _make_unlimited_observation_agent()
    agent.agent_config.run_limit_type = "steps"
    agent.agent_config.max_steps = 2
    agent.agent_config.memory_policy = "independent_items"
    captured: list[list[dict[str, Any]]] = []
    raw_responses = iter(
        [
            (
                'do(action="Swipe", start=[500, 800], end=[500, 200], '
                'message="OBJECT_SUMMARY: 视频 1 总结：旧对象内容。")'
            ),
            (
                'do(action="Swipe", start=[500, 800], end=[500, 200], '
                'message="OBJECT_SUMMARY: 视频 2 总结：新对象内容。")'
            ),
        ]
    )

    async def fake_stream(messages):
        captured.append(copy.deepcopy(messages))
        yield {"type": "raw", "content": next(raw_responses)}

    monkeypatch.setattr(agent, "_stream_openai", fake_stream)

    task = (
        "观看内容，当前任务包含逐项对象观察语义；每轮输出 OBJECT_SUMMARY "
        "并切换下一个对象"
    )

    async def run() -> list[dict[str, Any]]:
        return [event async for event in agent.stream(task)]

    asyncio.run(run())

    assert len(captured) == 2
    assert len(captured[0]) == 2
    assert len(captured[1]) == 2
    assert "Original Task" in _text_part(captured[1][-1])
    assert "视频 1 总结" not in json.dumps(captured[1], ensure_ascii=False)


def test_hybrid_observation_turns_keep_compact_runtime_history(monkeypatch):
    agent = _make_unlimited_observation_agent()
    agent.agent_config.run_limit_type = "steps"
    agent.agent_config.max_steps = 2
    agent.agent_config.memory_policy = "hybrid"
    captured: list[list[dict[str, Any]]] = []
    raw_responses = iter(
        [
            (
                'do(action="Swipe", start=[500, 800], end=[500, 200], '
                'message="OBJECT_SUMMARY: 第 1 轮已完成，切换成功。")'
            ),
            (
                'do(action="Swipe", start=[500, 800], end=[500, 200], '
                'message="OBJECT_SUMMARY: 第 2 轮继续推进。")'
            ),
        ]
    )

    async def fake_stream(messages):
        captured.append(copy.deepcopy(messages))
        yield {"type": "raw", "content": next(raw_responses)}

    monkeypatch.setattr(agent, "_stream_openai", fake_stream)

    async def run() -> None:
        await _drain(agent.stream("体验游戏流程，记录目标、进度和上一步反馈"))

    asyncio.run(run())

    assert len(captured) == 2
    assert "第 1 轮已完成" in json.dumps(captured[1], ensure_ascii=False)
