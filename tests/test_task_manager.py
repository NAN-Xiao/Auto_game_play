"""Unit tests for task manager queueing and cancellation semantics."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from AutoGLM_GUI.task_manager import TaskManager
from AutoGLM_GUI.task_store import TaskStatus, TaskStore


def test_task_manager_runs_fifo_per_device_and_parallel_across_devices(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = TaskStore(tmp_path / "tasks.db")
        manager = TaskManager(store)
        start_order: list[str] = []
        active_count = 0
        max_active = 0
        lock = asyncio.Lock()

        async def fake_executor(task: dict[str, object]) -> None:
            nonlocal active_count, max_active
            async with lock:
                start_order.append(str(task["id"]))
                active_count += 1
                max_active = max(max_active, active_count)
            await asyncio.sleep(0.05)
            await manager._finalize_task(
                task_id=str(task["id"]),
                status=TaskStatus.SUCCEEDED.value,
                final_message=str(task["input_text"]),
                step_count=1,
            )
            async with lock:
                active_count -= 1

        manager.register_executor("fake", fake_executor)

        task_a = store.create_task_run(
            source="chat",
            executor_key="fake",
            device_id="device-a",
            device_serial="serial-a",
            input_text="A1",
        )
        task_b = store.create_task_run(
            source="chat",
            executor_key="fake",
            device_id="device-a",
            device_serial="serial-a",
            input_text="A2",
        )
        task_c = store.create_task_run(
            source="chat",
            executor_key="fake",
            device_id="device-b",
            device_serial="serial-b",
            input_text="B1",
        )

        for task in (task_a, task_b, task_c):
            manager._completion_events[str(task["id"])] = asyncio.Event()

        await manager.start()
        await asyncio.gather(
            manager.wait_for_task(str(task_a["id"]), timeout=2),
            manager.wait_for_task(str(task_b["id"]), timeout=2),
            manager.wait_for_task(str(task_c["id"]), timeout=2),
        )

        assert start_order.index(str(task_a["id"])) < start_order.index(
            str(task_b["id"])
        )
        assert max_active >= 2
        assert store.get_task(str(task_a["id"]))["status"] == TaskStatus.SUCCEEDED.value
        assert store.get_task(str(task_b["id"]))["status"] == TaskStatus.SUCCEEDED.value
        assert store.get_task(str(task_c["id"]))["status"] == TaskStatus.SUCCEEDED.value

        await manager.shutdown()
        store.close()

    asyncio.run(scenario())


def test_task_manager_can_cancel_queued_task(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = TaskStore(tmp_path / "tasks.db")
        manager = TaskManager(store)
        unblock = asyncio.Event()
        started = asyncio.Event()

        async def blocking_executor(task: dict[str, object]) -> None:
            started.set()
            await unblock.wait()
            await manager._finalize_task(
                task_id=str(task["id"]),
                status=TaskStatus.SUCCEEDED.value,
                final_message="done",
                step_count=1,
            )

        manager.register_executor("fake", blocking_executor)

        running = store.create_task_run(
            source="chat",
            executor_key="fake",
            device_id="device-a",
            device_serial="serial-a",
            input_text="first",
        )
        queued = store.create_task_run(
            source="chat",
            executor_key="fake",
            device_id="device-a",
            device_serial="serial-a",
            input_text="second",
        )

        for task in (running, queued):
            manager._completion_events[str(task["id"])] = asyncio.Event()

        await manager.start()
        await asyncio.wait_for(started.wait(), timeout=2)

        cancelled = await manager.cancel_task(str(queued["id"]))
        assert cancelled is not None
        assert cancelled["status"] == TaskStatus.CANCELLED.value

        unblock.set()
        await manager.wait_for_task(str(running["id"]), timeout=2)
        final_queued = await manager.wait_for_task(str(queued["id"]), timeout=2)
        assert final_queued is not None
        assert final_queued["status"] == TaskStatus.CANCELLED.value
        assert final_queued["stop_reason"] == "user_stopped"

        await manager.shutdown()
        store.close()

    asyncio.run(scenario())


def test_task_manager_can_cancel_running_task(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = TaskStore(tmp_path / "tasks.db")
        manager = TaskManager(store)
        running = asyncio.Event()
        cancelled = asyncio.Event()

        async def cancellable_executor(task: dict[str, object]) -> None:
            task_id = str(task["id"])

            def abort_handler() -> None:
                cancelled.set()

            manager._abort_handlers[task_id] = abort_handler
            running.set()
            await cancelled.wait()
            manager._abort_handlers.pop(task_id, None)
            manager._cancel_requested.discard(task_id)
            await manager._finalize_task(
                task_id=task_id,
                status=TaskStatus.CANCELLED.value,
                final_message="Task cancelled by user",
                step_count=0,
            )

        manager.register_executor("fake", cancellable_executor)

        task = store.create_task_run(
            source="chat",
            executor_key="fake",
            device_id="device-a",
            device_serial="serial-a",
            input_text="cancel me",
        )
        manager._completion_events[str(task["id"])] = asyncio.Event()

        await manager.start()
        await asyncio.wait_for(running.wait(), timeout=2)

        current = await manager.cancel_task(str(task["id"]))
        assert current is not None

        final_task = await manager.wait_for_task(str(task["id"]), timeout=2)
        assert final_task is not None
        assert final_task["status"] == TaskStatus.CANCELLED.value
        assert final_task["stop_reason"] == "user_stopped"

        await manager.shutdown()
        store.close()

    asyncio.run(scenario())


def test_task_manager_marks_running_tasks_interrupted_on_start(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    task = store.create_task_run(
        source="chat",
        executor_key="classic_chat",
        device_id="device-a",
        device_serial="serial-a",
        input_text="resume me",
        status=TaskStatus.RUNNING.value,
    )
    manager = TaskManager(store)

    asyncio.run(manager.start())

    recovered = store.get_task(str(task["id"]))
    events = store.list_task_events(str(task["id"]))

    assert recovered is not None
    assert recovered["status"] == TaskStatus.INTERRUPTED.value
    assert recovered["stop_reason"] == "service_interrupted"
    assert any(event["event_type"] == "error" for event in events)

    asyncio.run(manager.shutdown())
    store.close()


def test_execute_layered_chat_counts_inner_steps_and_skips_legacy_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import AutoGLM_GUI.device_manager as device_manager_module
    import AutoGLM_GUI.history_manager as history_manager_module
    import AutoGLM_GUI.layered_agent_service as layered_service

    class FakeRun:
        def __init__(self) -> None:
            self.final_output = "已完成整理"

        def cancel(self) -> None:  # pragma: no cover - not exercised here
            pass

        async def stream_events(self):
            yield {
                "type": "tool_call",
                "payload": {"tool_name": "chat", "tool_args": {}},
            }
            yield {
                "type": "tool_result",
                "payload": {
                    "tool_name": "chat",
                    "result": "已打开设置",
                    "steps": 4,
                    "success": True,
                },
            }
            yield {
                "type": "tool_call",
                "payload": {"tool_name": "chat", "tool_args": {}},
            }
            yield {
                "type": "tool_result",
                "payload": {
                    "tool_name": "chat",
                    "result": "已切换 Wi-Fi",
                    "steps": 3,
                    "success": True,
                },
            }
            yield {"type": "message", "payload": {"content": "继续下一步"}}
            yield {
                "type": "done",
                "payload": {"content": "已完成整理", "success": True},
            }

    def fake_start_run(
        *, task_id: str, session_id: str, message: str, device_id: str = ""
    ) -> FakeRun:
        return FakeRun()

    monkeypatch.setattr(layered_service, "start_run", fake_start_run)

    legacy_history_calls: list[tuple[object, ...]] = []

    class FakeHistoryManager:
        def add_record(self, *args: object, **kwargs: object) -> None:
            legacy_history_calls.append((args, kwargs))

    monkeypatch.setattr(history_manager_module, "history_manager", FakeHistoryManager())

    class FakeDeviceManager:
        def get_serial_by_device_id(self, device_id: str) -> str:
            return "serial-a"

    monkeypatch.setattr(
        device_manager_module.DeviceManager,
        "get_instance",
        staticmethod(lambda: FakeDeviceManager()),
    )

    async def scenario() -> None:
        store = TaskStore(tmp_path / "tasks.db")
        manager = TaskManager(store)
        await manager.start()
        session = await manager.create_chat_session(
            device_id="device-a",
            device_serial="serial-a",
            mode="layered",
        )
        task = await manager.submit_chat_task(
            session_id=str(session["id"]),
            device_id="device-a",
            device_serial="serial-a",
            message="整理一下手机",
        )

        final_task = await manager.wait_for_task(str(task["id"]), timeout=5)

        assert final_task is not None
        assert final_task["status"] == TaskStatus.SUCCEEDED.value
        assert final_task["step_count"] == 7

        await manager.shutdown()
        store.close()

    asyncio.run(scenario())

    assert legacy_history_calls == []


def test_execute_layered_task_reraises_non_user_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import AutoGLM_GUI.layered_agent_service as layered_service

    class FakeRun:
        final_output = ""

        async def cancel(self) -> None:
            pass

        async def stream_events(self):
            raise asyncio.CancelledError
            yield

    def fake_start_run(
        *, task_id: str, session_id: str, message: str, device_id: str = ""
    ) -> FakeRun:
        return FakeRun()

    monkeypatch.setattr(layered_service, "start_run", fake_start_run)

    async def scenario() -> None:
        store = TaskStore(tmp_path / "tasks.db")
        manager = TaskManager(store)
        task = store.create_task_run(
            source="chat",
            executor_key="layered_chat",
            device_id="device-a",
            device_serial="serial-a",
            input_text="cancelled by shutdown",
        )

        with pytest.raises(asyncio.CancelledError):
            await manager._execute_layered_task(
                task,
                session_id="session-a",
                clear_session_after_run=False,
                metrics_source="layered",
            )

        store.close()

    asyncio.run(scenario())


def test_layered_task_run_closes_stream_iterator_on_cancel() -> None:
    import AutoGLM_GUI.layered_agent_service as layered_service

    async def scenario() -> None:
        closed = asyncio.Event()
        started = asyncio.Event()

        class BlockingIterator:
            def __aiter__(self):
                return self

            async def __anext__(self):
                started.set()
                await asyncio.Event().wait()

            async def aclose(self) -> None:
                closed.set()

        class FakeResult:
            final_output = ""

            def __init__(self) -> None:
                self.iterator = BlockingIterator()

            def cancel(self, mode: str = "immediate") -> None:
                pass

            def stream_events(self):
                return self.iterator

        run = layered_service.LayeredTaskRun(
            task_id="task-close-stream",
            session_id="session-a",
            result=FakeResult(),
        )
        events: list[dict[str, object]] = []

        async def consume_events() -> None:
            async for event in run.stream_events():
                events.append(event)

        consumer = asyncio.create_task(consume_events())
        await asyncio.wait_for(started.wait(), timeout=1)
        await run.cancel()
        await asyncio.wait_for(consumer, timeout=1)

        assert closed.is_set()
        assert events == [
            {"type": "cancelled", "payload": {"message": "Task cancelled by user"}}
        ]

    asyncio.run(scenario())


def test_submit_chat_task_uses_layered_executor_for_layered_sessions(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = TaskStore(tmp_path / "tasks.db")
        manager = TaskManager(store)
        session = await manager.create_chat_session(
            device_id="device-a",
            device_serial="serial-a",
            mode="layered",
        )

        task = await manager.submit_chat_task(
            session_id=str(session["id"]),
            device_id="device-a",
            device_serial="serial-a",
            message="复杂任务",
        )

        assert task["executor_key"] == "layered_chat"
        assert task["source"] == "chat"

        await manager.shutdown()
        store.close()

    asyncio.run(scenario())


def test_report_request_detection_avoids_plain_app_operations() -> None:
    assert TaskManager._looks_like_report_request("请按缺陷、亮点输出报告") is True
    assert TaskManager._looks_like_report_request("open the report page") is False
    assert TaskManager._looks_like_report_request("打开报告页面") is False


def test_followup_report_request_uses_retained_experience_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import AutoGLM_GUI.experience_report as experience_report_module

    async def fake_generate_experience_report(*, report_request, context):
        assert report_request == "请按缺陷、亮点、建议输出报告"
        assert "experience goal" in context.text
        assert "# Segment Summaries" in context.text
        assert "segment summary: tap login" in context.text
        return "generated report"

    async def fake_generate_experience_segment_summary(
        *,
        source_task,
        start_step,
        end_step,
        segment_text,
    ):
        assert source_task["input_text"] == "experience goal"
        assert (start_step, end_step) == (1, 1)
        assert "tap login" in segment_text
        return "segment summary: tap login"

    monkeypatch.setattr(
        experience_report_module,
        "generate_experience_report",
        fake_generate_experience_report,
    )
    monkeypatch.setattr(
        experience_report_module,
        "generate_experience_segment_summary",
        fake_generate_experience_segment_summary,
    )

    async def scenario() -> None:
        store = TaskStore(tmp_path / "tasks.db")
        manager = TaskManager(store)
        monkeypatch.setattr(manager, "_ensure_worker", lambda device_id: None)
        session = await manager.create_chat_session(
            device_id="device-a",
            device_serial="serial-a",
            mode="classic",
        )
        source_task = store.create_task_run(
            source="chat",
            executor_key="classic_chat",
            session_id=session["id"],
            device_id="device-a",
            device_serial="serial-a",
            input_text="experience goal",
        )
        store.append_event(
            task_id=source_task["id"],
            event_type="step",
            payload={
                "step": 1,
                "thinking": "check current screen",
                "action": {"action": "tap login"},
                "success": True,
                "finished": False,
                "screenshot": "c2NyZWVu",
            },
        )
        store.update_task_terminal(
            task_id=source_task["id"],
            status=TaskStatus.CANCELLED.value,
            final_message="Task cancelled by user",
            error_message="Task cancelled by user",
            stop_reason="user_stopped",
            step_count=1,
        )

        report_task = await manager.submit_chat_task(
            session_id=session["id"],
            device_id="device-a",
            device_serial="serial-a",
            message="请按缺陷、亮点、建议输出报告",
        )
        assert report_task["executor_key"] == "experience_report"

        task = store.get_task(report_task["id"])
        assert task is not None
        await manager._execute_experience_report(task)

        completed = store.get_task(report_task["id"])
        assert completed is not None
        assert completed["status"] == TaskStatus.SUCCEEDED.value
        assert completed["stop_reason"] == "report_generated"
        assert completed["final_message"] == "generated report"
        assert completed["step_count"] == 0
        events = store.list_task_events(report_task["id"])
        assert any(
            event["event_type"] == "experience_report_source" for event in events
        )
        assert any(
            event["event_type"] == "experience_report_context" for event in events
        )
        context_event = next(
            event
            for event in events
            if event["event_type"] == "experience_report_context"
        )
        assert context_event["payload"]["source_step_count"] == 1
        assert context_event["payload"]["segment_summary_count"] == 1
        source_events = store.list_task_events(source_task["id"])
        source_summary = next(
            event
            for event in source_events
            if event["event_type"] == "experience_segment_summary"
        )
        assert source_summary["role"] == "system"
        assert source_summary["payload"]["summary"] == "segment summary: tap login"
        assert any(
            event["event_type"] == "message"
            and event["payload"]["content"] == "generated report"
            for event in events
        )

        await manager.shutdown()
        store.close()

    asyncio.run(scenario())


def test_experience_summary_runs_in_background_on_step_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, bool]] = []

    async def fake_ensure_experience_segment_summaries(
        *,
        store,
        source_task,
        segment_step_size=30,
        include_partial_segment=True,
    ):
        _ = (store, segment_step_size)
        calls.append((source_task["id"], include_partial_segment))
        await asyncio.sleep(0)
        return []

    monkeypatch.setattr(
        "AutoGLM_GUI.experience_report.ensure_experience_segment_summaries",
        fake_ensure_experience_segment_summaries,
    )

    async def scenario() -> None:
        store = TaskStore(tmp_path / "tasks.db")
        manager = TaskManager(store)
        session = store.create_session(
            kind="chat",
            mode="classic",
            device_id="device-a",
            device_serial="serial-a",
        )
        source_task = store.create_task_run(
            source="chat",
            executor_key="classic_chat",
            session_id=session["id"],
            device_id="device-a",
            device_serial="serial-a",
            input_text="experience",
        )

        manager._schedule_experience_summary_for_progress(
            task=source_task,
            previous_step_count=29,
            current_step_count=30,
        )

        assert str(source_task["id"]) in manager._experience_summary_tasks
        await asyncio.gather(*manager._experience_summary_tasks.values())
        assert calls == [(source_task["id"], False)]

        await manager.shutdown()
        store.close()

    asyncio.run(scenario())


def test_experience_summary_generation_is_globally_limited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = 0
    max_active = 0

    async def fake_ensure_experience_segment_summaries(
        *,
        store,
        source_task,
        segment_step_size=30,
        include_partial_segment=True,
    ):
        nonlocal active, max_active
        _ = (store, source_task, segment_step_size, include_partial_segment)
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        active -= 1
        return []

    monkeypatch.setattr(
        "AutoGLM_GUI.experience_report.ensure_experience_segment_summaries",
        fake_ensure_experience_segment_summaries,
    )

    async def scenario() -> None:
        store = TaskStore(tmp_path / "tasks.db")
        manager = TaskManager(store)
        session = store.create_session(
            kind="chat",
            mode="classic",
            device_id="device-a",
            device_serial="serial-a",
        )
        tasks = [
            store.create_task_run(
                source="chat",
                executor_key="classic_chat",
                session_id=session["id"],
                device_id="device-a",
                device_serial="serial-a",
                input_text=f"experience {idx}",
            )
            for idx in range(2)
        ]

        await asyncio.gather(
            *[
                manager._ensure_experience_summaries_ready(source_task)
                for source_task in tasks
            ]
        )

        assert max_active == 1

        await manager.shutdown()
        store.close()

    asyncio.run(scenario())


def test_report_waiting_for_background_summary_emits_status_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_generate_experience_report(*, report_request, context):
        _ = (report_request, context)
        return "generated report"

    async def fake_ensure_experience_segment_summaries(
        *,
        store,
        source_task,
        segment_step_size=30,
        include_partial_segment=True,
    ):
        _ = (store, source_task, segment_step_size, include_partial_segment)
        await asyncio.sleep(0)
        return [
            {
                "version": "v1",
                "start_step": 1,
                "end_step": 1,
                "summary": "ready",
                "screenshot_refs": [],
            }
        ]

    monkeypatch.setattr(
        "AutoGLM_GUI.experience_report.generate_experience_report",
        fake_generate_experience_report,
    )
    monkeypatch.setattr(
        "AutoGLM_GUI.experience_report.ensure_experience_segment_summaries",
        fake_ensure_experience_segment_summaries,
    )

    async def scenario() -> None:
        store = TaskStore(tmp_path / "tasks.db")
        manager = TaskManager(store)
        monkeypatch.setattr(manager, "_ensure_worker", lambda device_id: None)
        session = await manager.create_chat_session(
            device_id="device-a",
            device_serial="serial-a",
            mode="classic",
        )
        source_task = store.create_task_run(
            source="chat",
            executor_key="classic_chat",
            session_id=session["id"],
            device_id="device-a",
            device_serial="serial-a",
            input_text="experience goal",
        )
        store.append_event(
            task_id=source_task["id"],
            event_type="step",
            payload={"step": 1, "thinking": "check", "success": True},
        )
        store.update_task_terminal(
            task_id=source_task["id"],
            status=TaskStatus.CANCELLED.value,
            final_message="Task cancelled by user",
            error_message="Task cancelled by user",
            stop_reason="user_stopped",
            step_count=1,
        )
        background_task = asyncio.create_task(asyncio.sleep(0.05))
        manager._experience_summary_tasks[source_task["id"]] = background_task

        report_task = await manager.submit_chat_task(
            session_id=session["id"],
            device_id="device-a",
            device_serial="serial-a",
            message="请输出报告",
        )
        task = store.get_task(report_task["id"])
        assert task is not None
        await manager._execute_experience_report(task)

        messages = [
            event["payload"]["content"]
            for event in store.list_task_events(report_task["id"])
            if event["event_type"] == "message"
        ]
        assert "正在整理体验记忆并生成报告..." in messages
        assert "generated report" in messages

        await manager.shutdown()
        store.close()

    asyncio.run(scenario())


def test_classic_experience_task_generates_report_in_same_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import AutoGLM_GUI.experience_report as experience_report_module
    import AutoGLM_GUI.phone_agent_manager as phone_agent_manager_module

    async def fake_generate_experience_report(*, report_request, context):
        assert report_request == "输出任务难度分析"
        assert context.step_count == 1
        return "experience report"

    async def fake_generate_experience_segment_summary(
        *,
        source_task,
        start_step,
        end_step,
        segment_text,
    ):
        _ = (source_task, start_step, end_step, segment_text)
        return "stage summary"

    monkeypatch.setattr(
        experience_report_module,
        "generate_experience_report",
        fake_generate_experience_report,
    )
    monkeypatch.setattr(
        experience_report_module,
        "generate_experience_segment_summary",
        fake_generate_experience_segment_summary,
    )

    class FakeAgent:
        async def cancel(self) -> None:
            return None

        async def stream(self, prompt: str, **_: object):
            assert "体验目标" in prompt
            yield {
                "type": "step",
                "data": {
                    "step": 1,
                    "thinking": "查看任务",
                    "action": {"action": "tap task"},
                    "success": True,
                    "finished": False,
                    "screenshot": "c2NyZWVu",
                },
            }
            yield {
                "type": "done",
                "data": {"message": "done", "steps": 1, "success": True},
            }

    class FakeManager:
        def acquire_device_async(self, device_id: str, **_: object):
            _ = device_id
            return asyncio.sleep(0, result=True)

        def get_agent_with_context(self, device_id: str, **_: object):
            _ = device_id
            return FakeAgent()

        def register_abort_handler(self, *args: object, **kwargs: object) -> None:
            _ = (args, kwargs)

        def unregister_abort_handler(self, *args: object, **kwargs: object) -> None:
            _ = (args, kwargs)

        def set_error_state(self, *args: object, **kwargs: object) -> None:
            _ = (args, kwargs)

        def release_device(self, *args: object, **kwargs: object) -> None:
            _ = (args, kwargs)

    monkeypatch.setattr(
        phone_agent_manager_module.PhoneAgentManager,
        "get_instance",
        staticmethod(lambda: FakeManager()),
    )

    async def scenario() -> None:
        store = TaskStore(tmp_path / "tasks.db")
        manager = TaskManager(store)
        session = store.create_session(
            kind="chat",
            mode="classic",
            device_id="device-a",
            device_serial="serial-a",
        )
        task = store.create_task_run(
            source="chat",
            executor_key="classic_chat",
            session_id=session["id"],
            device_id="device-a",
            device_serial="serial-a",
            input_text="体验一下这个游戏",
        )
        store.append_event(
            task_id=task["id"],
            event_type="user_message",
            role="user",
            payload={
                "message": "体验一下这个游戏",
                "attachments": [],
                "experience": {
                    "goal": "体验一下这个游戏",
                    "auto_generate_report": True,
                    "plan": {
                        "execution_goal": "体验一下这个游戏",
                        "observation_targets": ["任务内容"],
                        "analysis_lenses": ["任务难度曲线"],
                        "evaluation_dimensions": ["系统设计"],
                        "report_request": "输出任务难度分析",
                        "stop_conditions": ["覆盖关键任务节点后停止"],
                        "sampling_strategy": ["记录关键任务截图"],
                    },
                },
            },
        )

        await manager._execute_classic_chat(task)

        events = store.list_task_events(task["id"])
        assert any(
            event["event_type"] == "experience_report" for event in events
        )
        completed = store.get_task(task["id"])
        assert completed is not None
        assert completed["final_message"] == "experience report"
        store.close()

    asyncio.run(scenario())


def test_classic_experience_task_handles_device_busy_before_payload_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from AutoGLM_GUI.exceptions import DeviceBusyError
    import AutoGLM_GUI.phone_agent_manager as phone_agent_manager_module

    class FakeManager:
        async def acquire_device_async(self, device_id: str, **_: object) -> bool:
            _ = device_id
            raise DeviceBusyError("busy")

        def unregister_abort_handler(self, *args: object, **kwargs: object) -> None:
            _ = (args, kwargs)

        def set_error_state(self, *args: object, **kwargs: object) -> None:
            _ = (args, kwargs)

        def release_device(self, *args: object, **kwargs: object) -> None:
            _ = (args, kwargs)

    monkeypatch.setattr(
        phone_agent_manager_module.PhoneAgentManager,
        "get_instance",
        staticmethod(lambda: FakeManager()),
    )

    async def scenario() -> None:
        store = TaskStore(tmp_path / "tasks.db")
        manager = TaskManager(store)
        session = store.create_session(
            kind="chat",
            mode="classic",
            device_id="device-busy",
            device_serial="serial-busy",
        )
        task = store.create_task_run(
            source="chat",
            executor_key="classic_chat",
            session_id=session["id"],
            device_id="device-busy",
            device_serial="serial-busy",
            input_text="experience goal",
        )
        store.append_event(
            task_id=task["id"],
            event_type="user_message",
            role="user",
            payload={
                "message": "experience goal",
                "attachments": [],
                "experience": {
                    "goal": "experience goal",
                    "auto_generate_report": True,
                    "plan": {
                        "execution_goal": "experience goal",
                        "observation_targets": ["tasks"],
                        "analysis_lenses": ["difficulty"],
                        "evaluation_dimensions": ["system design"],
                        "report_request": "report",
                        "stop_conditions": ["stop"],
                        "sampling_strategy": ["capture"],
                    },
                },
            },
        )

        await manager._execute_classic_chat(task)

        completed = store.get_task(task["id"])
        assert completed is not None
        assert completed["status"] == TaskStatus.FAILED.value
        assert completed["stop_reason"] == "device_busy"
        store.close()

    asyncio.run(scenario())
