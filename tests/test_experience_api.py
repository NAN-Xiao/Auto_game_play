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
    assert body["plan"]["memory_policy"] == "stateful_flow"


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


def test_experience_plan_adds_iterative_item_sampling_strategy() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.post(
        "/api/experience/plan",
        json={
            "messages": [
                "依次浏览每个商品详情，停留一段时间观察卖点和价格，"
                "提炼内容要点后切换下一个"
            ]
        },
    )

    assert response.status_code == 200
    plan = response.json()["plan"]
    assert "单个观察对象的连续状态变化" in plan["observation_targets"]
    assert "同一对象多次采样后的综合判断" in plan["analysis_lenses"]
    assert any(
        "按观察窗口配置完成间隔截图/观察" in item for item in plan["sampling_strategy"]
    )
    assert any("观察窗口内的多帧采样" in item for item in plan["sampling_strategy"])
    assert plan["memory_policy"] == "independent_items"


def test_experience_plan_updates_content_fields_from_followup_focus() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.post(
        "/api/experience/plan",
        json={
            "messages": [
                "帮我打开抖音 观看视频 每个视频观看足够时长 "
                "然后提炼视频内容 之后切换下一个视频",
                "关注核心文案",
            ]
        },
    )

    assert response.status_code == 200
    plan = response.json()["plan"]
    assert "核心文案" in plan["observation_targets"]
    assert "标题/字幕/按钮文案" in plan["observation_targets"]
    assert "核心文案提炼" in plan["analysis_lenses"]
    assert "内容表达" in plan["evaluation_dimensions"]
    assert any("核心文案" in item for item in plan["sampling_strategy"])
    assert any(
        "按观察窗口配置完成间隔截图/观察" in item for item in plan["sampling_strategy"]
    )
    assert plan["memory_policy"] == "independent_items"


def test_experience_plan_uses_stateful_memory_for_plain_game_flow() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.post(
        "/api/experience/plan",
        json={"messages": ["体验这个游戏的新手任务流程，记录任务反馈和卡点"]},
    )

    assert response.status_code == 200
    assert response.json()["plan"]["memory_policy"] == "stateful_flow"
