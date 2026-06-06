"""Tests for retained app experience report context compaction."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import AutoGLM_GUI.experience_report as experience_report_module
from AutoGLM_GUI.experience_report import (
    build_experience_report_context,
    ensure_experience_segment_summaries,
)
from AutoGLM_GUI.task_store import TaskStatus, TaskStore


def _create_source_task_with_steps(
    store: TaskStore,
    *,
    step_count: int,
    include_screenshots: bool = False,
):
    session = store.create_session(
        kind="chat",
        mode="classic",
        device_id="device-1",
        device_serial="serial-1",
        session_id="session-1",
    )
    task = store.create_task_run(
        source="chat",
        executor_key="classic_chat",
        session_id=session["id"],
        device_id="device-1",
        device_serial="serial-1",
        input_text="long app evaluation",
    )
    for step in range(1, step_count + 1):
        payload = {
            "step": step,
            "thinking": f"thinking step {step}",
            "action": {"action": "tap", "target": f"button {step}"},
            "success": True,
            "finished": False,
        }
        if include_screenshots:
            payload["screenshot"] = f"screen-{step}"
            if step in {2, 35, 70}:
                payload["success"] = False
            if step in {30, 60, 90}:
                payload["finished"] = True
        store.append_event(
            task_id=task["id"],
            event_type="step",
            role="assistant",
            payload=payload,
        )
    store.update_task_terminal(
        task_id=task["id"],
        status=TaskStatus.CANCELLED.value,
        final_message="Task cancelled by user",
        error_message="Task cancelled by user",
        stop_reason="user_stopped",
        step_count=step_count,
    )
    return store.get_task(task["id"]) or task


def test_ensure_experience_segment_summaries_persists_and_reuses_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    source_task = _create_source_task_with_steps(store, step_count=90)
    calls: list[tuple[int, int, str]] = []

    async def fake_generate_experience_segment_summary(
        *,
        source_task,
        start_step,
        end_step,
        segment_text,
    ):
        _ = source_task
        calls.append((start_step, end_step, segment_text))
        return f"summary {start_step}-{end_step}"

    monkeypatch.setattr(
        experience_report_module,
        "generate_experience_segment_summary",
        fake_generate_experience_segment_summary,
    )

    summaries = asyncio.run(
        ensure_experience_segment_summaries(
            store=store,
            source_task=source_task,
            segment_step_size=30,
        )
    )

    assert [(item["start_step"], item["end_step"]) for item in summaries] == [
        (1, 30),
        (31, 60),
        (61, 90),
    ]
    assert [(start, end) for start, end, _ in calls] == [
        (1, 30),
        (31, 60),
        (61, 90),
    ]
    assert "thinking step 30" in calls[0][2]
    assert "thinking step 31" in calls[1][2]

    cached = asyncio.run(
        ensure_experience_segment_summaries(
            store=store,
            source_task=source_task,
            segment_step_size=30,
        )
    )

    assert [(item["start_step"], item["end_step"]) for item in cached] == [
        (1, 30),
        (31, 60),
        (61, 90),
    ]
    assert len(calls) == 3
    events = store.list_task_events(source_task["id"])
    assert [
        event["payload"]["summary"]
        for event in events
        if event["event_type"] == "experience_segment_summary"
    ] == ["summary 1-30", "summary 31-60", "summary 61-90"]

    store.close()


def test_segment_summaries_persist_screenshot_refs_and_report_uses_ref_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    source_task = _create_source_task_with_steps(
        store,
        step_count=90,
        include_screenshots=True,
    )

    async def fake_generate_experience_segment_summary(
        *,
        source_task,
        start_step,
        end_step,
        segment_text,
    ):
        _ = (source_task, segment_text)
        return f"summary {start_step}-{end_step}"

    monkeypatch.setattr(
        experience_report_module,
        "generate_experience_segment_summary",
        fake_generate_experience_segment_summary,
    )

    summaries = asyncio.run(
        ensure_experience_segment_summaries(
            store=store,
            source_task=source_task,
            segment_step_size=30,
        )
    )

    assert [summary["screenshot_refs"] for summary in summaries] == [
        [
            {"step": 2, "label": "step 2", "reason": "important"},
            {"step": 30, "label": "step 30", "reason": "important"},
        ],
        [
            {"step": 35, "label": "step 35", "reason": "important"},
            {"step": 60, "label": "step 60", "reason": "important"},
        ],
        [
            {"step": 70, "label": "step 70", "reason": "important"},
            {"step": 90, "label": "step 90", "reason": "important"},
        ],
    ]

    context = build_experience_report_context(store=store, source_task=source_task)

    assert [image["label"] for image in context.screenshots] == [
        "step 2",
        "step 30",
        "step 35",
        "step 60",
        "step 70",
        "step 90",
    ]
    assert [image["data"] for image in context.screenshots] == [
        "screen-2",
        "screen-30",
        "screen-35",
        "screen-60",
        "screen-70",
        "screen-90",
    ]

    store.close()


def test_concurrent_segment_summary_creation_does_not_duplicate_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    source_task = _create_source_task_with_steps(store, step_count=30)
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_generate_experience_segment_summary(
        *,
        source_task,
        start_step,
        end_step,
        segment_text,
    ):
        _ = (source_task, start_step, end_step, segment_text)
        started.set()
        await release.wait()
        return "concurrent summary"

    monkeypatch.setattr(
        experience_report_module,
        "generate_experience_segment_summary",
        fake_generate_experience_segment_summary,
    )

    async def scenario() -> None:
        first = asyncio.create_task(
            ensure_experience_segment_summaries(
                store=store,
                source_task=source_task,
                segment_step_size=30,
            )
        )
        await started.wait()
        second = asyncio.create_task(
            ensure_experience_segment_summaries(
                store=store,
                source_task=source_task,
                segment_step_size=30,
            )
        )
        release.set()
        first_result, second_result = await asyncio.gather(first, second)
        assert first_result[0]["summary"] == "concurrent summary"
        assert second_result[0]["summary"] == "concurrent summary"

    asyncio.run(scenario())

    summary_events = [
        event
        for event in store.list_task_events(source_task["id"])
        if event["event_type"] == "experience_segment_summary"
    ]
    assert len(summary_events) == 1

    store.close()


def test_experience_report_context_prefers_segment_summaries(
    tmp_path: Path,
) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    source_task = _create_source_task_with_steps(store, step_count=90)
    for start, end in ((1, 30), (31, 60), (61, 90)):
        store.append_event(
            task_id=source_task["id"],
            event_type="experience_segment_summary",
            role="system",
            payload={
                "version": "v1",
                "segment_step_size": 30,
                "start_step": start,
                "end_step": end,
                "summary": f"cached summary {start}-{end}",
            },
        )

    context = build_experience_report_context(store=store, source_task=source_task)

    assert "# Segment Summaries" in context.text
    assert "cached summary 1-30" in context.text
    assert "cached summary 61-90" in context.text
    assert "thinking step 90" not in context.text
    assert context.step_count == 90

    store.close()
