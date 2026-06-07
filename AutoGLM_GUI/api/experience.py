"""Experience planning API for chat experience mode."""

from __future__ import annotations

from fastapi import APIRouter

from AutoGLM_GUI.experience_planner import build_experience_plan
from AutoGLM_GUI.schemas import ExperiencePlanRequest, ExperiencePlanResponse

router = APIRouter()


@router.post("/api/experience/plan", response_model=ExperiencePlanResponse)
def create_experience_plan(
    request: ExperiencePlanRequest,
) -> ExperiencePlanResponse:
    return build_experience_plan(request.messages)
