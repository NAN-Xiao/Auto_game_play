from fastapi import FastAPI
from fastapi.testclient import TestClient

from AutoGLM_GUI.api.experience import router


def test_experience_plan_returns_question_for_missing_scope() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.post(
        "/api/experience/plan",
        json={"messages": ["体验这个游戏，关注任务，最后给我一份难度分析"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["stage"] == "asking"
    assert body["question"]
    assert body["plan"]["analysis_lenses"]


def test_experience_plan_uses_default_dimensions_for_comprehensive_review() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.post(
        "/api/experience/plan",
        json={"messages": ["做一个综合游戏性比较，最后给我一个综合评测"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert "美术表现" in body["plan"]["evaluation_dimensions"]
    assert "系统设计" in body["plan"]["evaluation_dimensions"]
