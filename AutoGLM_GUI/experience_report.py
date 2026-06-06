"""Experience report generation from persisted task events."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

from AutoGLM_GUI.config import ModelConfig
from AutoGLM_GUI.config_manager import config_manager
from AutoGLM_GUI.model import MessageBuilder
from AutoGLM_GUI.model.error_details import (
    model_error_message,
    serialize_model_error_async,
    trace_error_attrs,
)
from AutoGLM_GUI.task_store import TaskEventRecord, TaskRecord, TaskStore
from AutoGLM_GUI.trace import summarize_text, trace_span


REPORT_CONTEXT_EVENT_TYPES = {
    "step",
    "thinking",
    "message",
    "done",
    "error",
    "cancelled",
    "takeover",
    "tool_call",
    "tool_result",
}

SEGMENT_SUMMARY_EVENT_TYPE = "experience_segment_summary"
SEGMENT_SUMMARY_VERSION = "v1"
DEFAULT_SEGMENT_STEP_SIZE = 30
MAX_RAW_REPORT_STEPS = 240
MAX_TEXT_FIELD_CHARS = 900
MAX_CONTEXT_CHARS = 50000
MAX_SEGMENT_INPUT_CHARS = 16000
MAX_REPORT_SCREENSHOTS = 6
MAX_SEGMENT_SCREENSHOT_REFS = 2


@dataclass(frozen=True)
class ReportContext:
    """Compact report source assembled from one previous experience task."""

    source_task: TaskRecord
    text: str
    step_count: int
    screenshots: list[dict[str, str]]


def _truncate_text(value: Any, limit: int = MAX_TEXT_FIELD_CHARS) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    text = text.strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...(truncated)"


def _truncate_middle(text: str, limit: int = MAX_CONTEXT_CHARS) -> str:
    if len(text) <= limit:
        return text
    notice = f"\n\n...omitted {len(text) - limit} chars from the middle...\n\n"
    keep = max((limit - len(notice)) // 2, 0)
    return f"{text[:keep]}{notice}{text[-keep:]}"


def _payload_has_screenshot(payload: dict[str, Any]) -> bool:
    if isinstance(payload.get("screenshot"), str) and payload["screenshot"]:
        return True
    if (
        isinstance(payload.get("screenshot_base64"), str)
        and payload["screenshot_base64"]
    ):
        return True
    image = payload.get("image")
    return isinstance(image, str) and bool(image)


def _extract_screenshot(payload: dict[str, Any]) -> str | None:
    for key in ("screenshot", "screenshot_base64", "image"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _summarize_action(action: Any) -> str:
    if action is None:
        return ""
    if isinstance(action, dict):
        return json.dumps(action, ensure_ascii=False, sort_keys=True, default=str)
    return str(action)


def _step_index(payload: dict[str, Any], fallback: int) -> int:
    raw_step = payload.get("step")
    if isinstance(raw_step, int):
        return raw_step
    if isinstance(raw_step, float):
        return int(raw_step)
    if isinstance(raw_step, str) and raw_step.isdigit():
        return int(raw_step)
    return fallback


def _format_event_line(
    event: TaskEventRecord,
    *,
    step_fallback: int,
) -> str | None:
    event_type = str(event["event_type"])
    payload = dict(event.get("payload") or {})

    if event_type == "thinking":
        chunk = _truncate_text(payload.get("chunk") or payload.get("content"), 240)
        return f"- thinking: {chunk}" if chunk else None

    if event_type == "message":
        content = _truncate_text(payload.get("content") or payload.get("message"))
        return f"- assistant_message: {content}" if content else None

    if event_type == "step":
        step = _step_index(payload, step_fallback)
        thinking = _truncate_text(payload.get("thinking"), 360)
        action = _truncate_text(_summarize_action(payload.get("action")), 360)
        message = _truncate_text(payload.get("message"), 360)
        success = payload.get("success")
        finished = payload.get("finished")
        screenshot_note = "yes" if _payload_has_screenshot(payload) else "no"
        parts = [
            f"- step {step}",
            f"screenshot={screenshot_note}",
            f"success={success}" if success is not None else "",
            f"finished={finished}" if finished is not None else "",
            f"thinking={thinking}" if thinking else "",
            f"action={action}" if action else "",
            f"message={message}" if message else "",
        ]
        return "; ".join(part for part in parts if part)

    if event_type in {"done", "error", "cancelled", "takeover"}:
        message = _truncate_text(payload.get("message") or payload.get("content"))
        stop_reason = _truncate_text(payload.get("stop_reason"), 120)
        success = payload.get("success")
        parts = [
            f"- {event_type}",
            f"success={success}" if success is not None else "",
            f"stop_reason={stop_reason}" if stop_reason else "",
            f"message={message}" if message else "",
        ]
        return "; ".join(part for part in parts if part)

    if event_type == "tool_call":
        tool_name = _truncate_text(payload.get("tool_name"), 120)
        tool_args = _truncate_text(payload.get("tool_args"), 420)
        return f"- tool_call: {tool_name}; args={tool_args}"

    if event_type == "tool_result":
        tool_name = _truncate_text(payload.get("tool_name"), 120)
        result = _truncate_text(payload.get("result"), 520)
        steps = payload.get("steps")
        success = payload.get("success")
        parts = [
            f"- tool_result: {tool_name}",
            f"steps={steps}" if steps is not None else "",
            f"success={success}" if success is not None else "",
            f"result={result}" if result else "",
        ]
        return "; ".join(part for part in parts if part)

    return None


def _select_report_screenshots(
    events: list[TaskEventRecord],
    segment_summaries: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    if segment_summaries:
        selected_from_refs = _select_report_screenshots_from_refs(
            events,
            segment_summaries,
        )
        if selected_from_refs:
            return selected_from_refs

    candidates: list[tuple[int, bool, str]] = []
    for fallback_step, event in enumerate(events, start=1):
        if event["event_type"] != "step":
            continue
        payload = dict(event.get("payload") or {})
        screenshot = _extract_screenshot(payload)
        if not screenshot:
            continue
        step = _step_index(payload, fallback_step)
        important = payload.get("success") is False or payload.get("finished") is True
        candidates.append((step, important, screenshot))

    if not candidates:
        return []

    selected: list[tuple[int, str]] = []
    first = candidates[0]
    last = candidates[-1]
    selected.append((first[0], first[2]))

    for step, important, screenshot in candidates:
        if important:
            selected.append((step, screenshot))
        if len(selected) >= MAX_REPORT_SCREENSHOTS - 1:
            break

    selected.append((last[0], last[2]))

    deduped: list[dict[str, str]] = []
    seen: set[tuple[int, str]] = set()
    for step, screenshot in selected:
        key = (step, screenshot[:64])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(
            {
                "mime_type": "image/png",
                "data": screenshot,
                "label": f"step {step}",
            }
        )
        if len(deduped) >= MAX_REPORT_SCREENSHOTS:
            break
    return deduped


def _screenshot_ref_key(ref: dict[str, Any]) -> tuple[int, str, str] | None:
    step = ref.get("step")
    label = ref.get("label")
    reason = ref.get("reason")
    if not isinstance(step, int):
        return None
    if not isinstance(label, str) or not label:
        return None
    if not isinstance(reason, str) or not reason:
        return None
    return step, label, reason


def _select_segment_screenshot_refs(
    candidates: list[tuple[int, bool, str]],
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen_steps: set[int] = set()

    def add(step: int, reason: str) -> None:
        if step in seen_steps:
            return
        seen_steps.add(step)
        selected.append(
            {
                "step": step,
                "label": f"step {step}",
                "reason": reason,
            }
        )

    for step, important, _screenshot in candidates:
        if important:
            add(step, "important")
        if len(selected) >= MAX_SEGMENT_SCREENSHOT_REFS:
            return selected

    if candidates:
        add(candidates[0][0], "segment_start")
    if len(selected) < MAX_SEGMENT_SCREENSHOT_REFS and len(candidates) > 1:
        add(candidates[-1][0], "segment_end")
    return selected[:MAX_SEGMENT_SCREENSHOT_REFS]


def _extract_step_screenshots(
    events: list[TaskEventRecord],
) -> dict[int, str]:
    screenshots: dict[int, str] = {}
    step_count = 0
    for fallback_step, event in enumerate(events, start=1):
        if event["event_type"] != "step":
            continue
        step_count += 1
        payload = dict(event.get("payload") or {})
        screenshot = _extract_screenshot(payload)
        if not screenshot:
            continue
        step = _step_index(payload, step_count or fallback_step)
        screenshots.setdefault(step, screenshot)
    return screenshots


def _select_report_screenshots_from_refs(
    events: list[TaskEventRecord],
    segment_summaries: list[dict[str, Any]],
) -> list[dict[str, str]]:
    step_screenshots = _extract_step_screenshots(events)
    if not step_screenshots:
        return []

    selected: list[dict[str, str]] = []
    seen_steps: set[int] = set()
    for payload in segment_summaries:
        refs = payload.get("screenshot_refs")
        if not isinstance(refs, list):
            continue
        for raw_ref in refs:
            if not isinstance(raw_ref, dict):
                continue
            ref_key = _screenshot_ref_key(raw_ref)
            if ref_key is None:
                continue
            step, label, _reason = ref_key
            screenshot = step_screenshots.get(step)
            if not screenshot or step in seen_steps:
                continue
            seen_steps.add(step)
            selected.append(
                {
                    "mime_type": "image/png",
                    "data": screenshot,
                    "label": label,
                }
            )
            if len(selected) >= MAX_REPORT_SCREENSHOTS:
                return selected
    return selected


def _segment_summary_key(payload: dict[str, Any]) -> tuple[int, int] | None:
    start_step = payload.get("start_step")
    end_step = payload.get("end_step")
    if isinstance(start_step, int) and isinstance(end_step, int):
        return start_step, end_step
    return None


def _existing_segment_summaries(
    events: list[TaskEventRecord],
) -> dict[tuple[int, int], dict[str, Any]]:
    summaries: dict[tuple[int, int], dict[str, Any]] = {}
    for event in events:
        if event["event_type"] != SEGMENT_SUMMARY_EVENT_TYPE:
            continue
        payload = dict(event.get("payload") or {})
        if payload.get("version") != SEGMENT_SUMMARY_VERSION:
            continue
        summary = payload.get("summary")
        key = _segment_summary_key(payload)
        if key is None or not isinstance(summary, str) or not summary.strip():
            continue
        summaries[key] = payload
    return summaries


def _build_step_segments(
    events: list[TaskEventRecord],
    *,
    segment_step_size: int = DEFAULT_SEGMENT_STEP_SIZE,
    include_partial_segment: bool = True,
) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    current_lines: list[str] = []
    current_screenshot_candidates: list[tuple[int, bool, str]] = []
    current_start_step = 1
    current_end_step = 0
    step_count = 0

    def flush(*, force: bool = False) -> None:
        nonlocal current_lines, current_screenshot_candidates
        nonlocal current_start_step, current_end_step
        if not current_lines:
            return
        segment_steps = (
            current_end_step - current_start_step + 1
            if current_end_step >= current_start_step
            else 0
        )
        if not force and segment_steps < segment_step_size:
            return
        segments.append(
            {
                "start_step": current_start_step,
                "end_step": max(current_end_step, current_start_step),
                "text": _truncate_middle(
                    "\n".join(current_lines),
                    limit=MAX_SEGMENT_INPUT_CHARS,
                ),
                "screenshot_refs": _select_segment_screenshot_refs(
                    current_screenshot_candidates
                ),
            }
        )
        current_lines = []
        current_screenshot_candidates = []
        current_start_step = current_end_step + 1

    for event in events:
        event_type = str(event["event_type"])
        if event_type == SEGMENT_SUMMARY_EVENT_TYPE:
            continue
        if event_type not in REPORT_CONTEXT_EVENT_TYPES:
            continue

        if event_type == "step":
            step_count += 1
            if (
                current_lines
                and step_count > current_start_step
                and (step_count - current_start_step) >= segment_step_size
            ):
                flush()
            current_end_step = step_count
            payload = dict(event.get("payload") or {})
            screenshot = _extract_screenshot(payload)
            if screenshot:
                current_screenshot_candidates.append(
                    (
                        _step_index(payload, step_count),
                        payload.get("success") is False
                        or payload.get("finished") is True,
                        screenshot,
                    )
                )

        line = _format_event_line(event, step_fallback=max(step_count, 1))
        if line:
            current_lines.append(line)

    flush(force=include_partial_segment)
    return segments


async def generate_experience_segment_summary(
    *,
    source_task: TaskRecord,
    start_step: int,
    end_step: int,
    segment_text: str,
) -> str:
    """Summarize one bounded step segment for later final report generation."""

    model_config = _report_model_config()
    if not model_config.base_url:
        raise ValueError(
            "base_url is not configured. Configure a model before generating reports."
        )
    if not model_config.model_name:
        raise ValueError(
            "model_name is not configured. Configure a model before generating reports."
        )

    client = AsyncOpenAI(
        base_url=model_config.base_url,
        api_key=model_config.api_key,
        timeout=120,
    )
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": (
                "You summarize a bounded Android app experience segment for a later "
                "product experience report. Preserve concrete observations, user "
                "journey, UX issues, errors, dead ends, successful flows, and evidence. "
                "Do not invent facts."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Original goal: {_truncate_text(source_task.get('input_text'), 1200)}\n"
                f"Segment: steps {start_step}-{end_step}\n\n"
                f"Segment trajectory:\n{segment_text}\n\n"
                "Return a compact structured summary in Chinese unless the original "
                "content is clearly English."
            ),
        },
    ]

    with trace_span(
        "experience_report.segment_llm",
        attrs={
            "source_task_id": str(source_task["id"]),
            "model_name": model_config.model_name,
            "start_step": start_step,
            "end_step": end_step,
        },
    ) as span:
        try:
            response = await client.chat.completions.create(
                messages=messages,  # type: ignore[arg-type]
                model=model_config.model_name,
                max_tokens=model_config.max_tokens,
                temperature=model_config.temperature,
                top_p=model_config.top_p,
                frequency_penalty=model_config.frequency_penalty,
                extra_body=model_config.extra_body,
            )
        except Exception as exc:
            details = await serialize_model_error_async(
                exc,
                model_config=model_config,
                call_site=(
                    "AutoGLM_GUI.experience_report.generate_experience_segment_summary"
                ),
            )
            span.set_attributes(trace_error_attrs(details))
            raise RuntimeError(model_error_message(exc)) from exc

    content = response.choices[0].message.content if response.choices else ""
    return _truncate_text(content, 2400)


async def ensure_experience_segment_summaries(
    *,
    store: TaskStore,
    source_task: TaskRecord,
    segment_step_size: int = DEFAULT_SEGMENT_STEP_SIZE,
    include_partial_segment: bool = True,
) -> list[dict[str, Any]]:
    """Create and persist missing per-segment summaries for a source task."""

    events = await asyncio.to_thread(store.list_task_events, str(source_task["id"]))
    existing = _existing_segment_summaries(events)
    segments = _build_step_segments(
        events,
        segment_step_size=segment_step_size,
        include_partial_segment=include_partial_segment,
    )
    summaries: list[dict[str, Any]] = []

    for segment in segments:
        key = (int(segment["start_step"]), int(segment["end_step"]))
        payload = existing.get(key)
        if payload is None:
            match_fields = {
                "version": SEGMENT_SUMMARY_VERSION,
                "start_step": key[0],
                "end_step": key[1],
            }
            existing_event = await asyncio.to_thread(
                store.find_event_by_payload_fields,
                task_id=str(source_task["id"]),
                event_type=SEGMENT_SUMMARY_EVENT_TYPE,
                payload_fields=match_fields,
            )
            if existing_event is not None:
                payload = dict(existing_event.get("payload") or {})
                if "screenshot_refs" not in payload:
                    payload = {
                        **payload,
                        "screenshot_refs": segment.get("screenshot_refs", []),
                    }
                existing[key] = payload
                summaries.append(payload)
                continue

            summary = await generate_experience_segment_summary(
                source_task=source_task,
                start_step=key[0],
                end_step=key[1],
                segment_text=str(segment["text"]),
            )
            payload = {
                "version": SEGMENT_SUMMARY_VERSION,
                "segment_step_size": segment_step_size,
                "start_step": key[0],
                "end_step": key[1],
                "summary": summary,
                "screenshot_refs": segment.get("screenshot_refs", []),
            }
            persisted_event, _created = await asyncio.to_thread(
                store.append_event_if_missing_by_payload_fields,
                task_id=str(source_task["id"]),
                event_type=SEGMENT_SUMMARY_EVENT_TYPE,
                role="system",
                payload=payload,
                payload_fields=match_fields,
            )
            payload = dict(persisted_event.get("payload") or payload)
        elif "screenshot_refs" not in payload:
            payload = {**payload, "screenshot_refs": segment.get("screenshot_refs", [])}
        summaries.append(payload)

    return summaries


def build_experience_report_context(
    *,
    store: TaskStore,
    source_task: TaskRecord,
) -> ReportContext:
    """Build compact report context from the persisted previous task."""

    events = store.list_task_events(str(source_task["id"]))
    segment_summaries = sorted(
        _existing_segment_summaries(events).values(),
        key=lambda payload: (
            int(payload.get("start_step") or 0),
            int(payload.get("end_step") or 0),
        ),
    )
    lines: list[str] = [
        "# Previous App Experience",
        f"task_id: {source_task['id']}",
        f"mode: {source_task.get('executor_key')}",
        f"user_goal: {_truncate_text(source_task.get('input_text'), 1400)}",
        f"status: {source_task.get('status')}",
        f"stop_reason: {source_task.get('stop_reason')}",
        f"step_count: {source_task.get('step_count')}",
        f"started_at: {source_task.get('started_at')}",
        f"finished_at: {source_task.get('finished_at')}",
        "",
    ]

    step_events = int(source_task.get("step_count") or 0)
    if segment_summaries:
        lines.extend(
            [
                "# Segment Summaries",
                (
                    "The original trajectory was compressed into bounded step "
                    "summaries before final report generation."
                ),
            ]
        )
        for payload in segment_summaries:
            summary = _truncate_text(payload.get("summary"), 2400)
            lines.append(
                f"## Steps {payload.get('start_step')}-{payload.get('end_step')}"
            )
            lines.append(summary)
    else:
        lines.append("# Trajectory")
        raw_step_events = 0
        for event in events:
            event_type = str(event["event_type"])
            if event_type not in REPORT_CONTEXT_EVENT_TYPES:
                continue
            if event_type == "step":
                raw_step_events += 1
                if raw_step_events > MAX_RAW_REPORT_STEPS:
                    lines.append(
                        f"- omitted remaining steps after {MAX_RAW_REPORT_STEPS}"
                    )
                    break
            line = _format_event_line(event, step_fallback=raw_step_events)
            if line:
                lines.append(line)
        step_events = raw_step_events or step_events

    text = "\n".join(lines)
    text = _truncate_middle(text, limit=MAX_CONTEXT_CHARS)

    return ReportContext(
        source_task=source_task,
        text=text,
        step_count=step_events or int(source_task.get("step_count") or 0),
        screenshots=_select_report_screenshots(events, segment_summaries),
    )


def _report_model_config() -> ModelConfig:
    config_manager.load_file_config()
    effective_config = config_manager.get_effective_config()
    base_url = effective_config.decision_base_url or effective_config.base_url
    api_key = effective_config.decision_api_key or effective_config.api_key
    model_name = effective_config.decision_model_name or effective_config.model_name
    return ModelConfig(
        base_url=base_url,
        api_key=api_key or "EMPTY",
        model_name=model_name,
    )


async def generate_experience_report(
    *,
    report_request: str,
    context: ReportContext,
) -> str:
    """Generate a report using the configured OpenAI-compatible model."""

    model_config = _report_model_config()
    if not model_config.base_url:
        raise ValueError(
            "base_url is not configured. Configure a model before generating reports."
        )
    if not model_config.model_name:
        raise ValueError(
            "model_name is not configured. Configure a model before generating reports."
        )

    client = AsyncOpenAI(
        base_url=model_config.base_url,
        api_key=model_config.api_key,
        timeout=120,
    )
    user_text = (
        "Previous experience context:\n"
        f"{context.text}\n\n"
        "User report requirements:\n"
        f"{report_request}"
    )
    if context.screenshots:
        labels = ", ".join(
            image.get("label", f"image {idx + 1}")
            for idx, image in enumerate(context.screenshots)
        )
        user_text = (
            f"Key screenshots are attached for visual evidence ({labels}).\n\n"
            f"{user_text}"
        )

    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "You generate product experience reports for any Android app. "
                "Use only the provided trajectory context. Do not claim details "
                "that are not supported by the context. Follow the user's requested "
                "format, focus areas, language, and level of detail exactly."
            ),
        },
    ]
    messages.append(
        MessageBuilder.create_user_message_with_images(
            user_text,
            [
                {"mime_type": image["mime_type"], "data": image["data"]}
                for image in context.screenshots
            ],
        )
    )

    with trace_span(
        "experience_report.llm",
        attrs={
            "source_task_id": str(context.source_task["id"]),
            "model_name": model_config.model_name,
            "step_count": context.step_count,
            "request_preview": summarize_text(report_request) or "",
        },
    ) as span:
        try:
            response = await client.chat.completions.create(
                messages=messages,  # type: ignore[arg-type]
                model=model_config.model_name,
                max_tokens=model_config.max_tokens,
                temperature=model_config.temperature,
                top_p=model_config.top_p,
                frequency_penalty=model_config.frequency_penalty,
                extra_body=model_config.extra_body,
            )
        except Exception as exc:
            details = await serialize_model_error_async(
                exc,
                model_config=model_config,
                call_site="AutoGLM_GUI.experience_report.generate_experience_report",
            )
            span.set_attributes(trace_error_attrs(details))
            raise RuntimeError(model_error_message(exc)) from exc

    content = response.choices[0].message.content if response.choices else ""
    if isinstance(content, list):
        return "".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part)
            for part in content
        ).strip()
    return str(content or "").strip()
