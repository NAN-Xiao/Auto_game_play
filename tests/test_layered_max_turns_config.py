import json

import pytest

from AutoGLM_GUI.config_manager import (
    LAYERED_MAX_TURNS_DEFAULT,
    OBSERVATION_WINDOW_INTERVAL_SECONDS_DEFAULT,
    OBSERVATION_WINDOW_SCREENSHOT_COUNT_DEFAULT,
    ConfigModel,
)


def test_layered_max_turns_default() -> None:
    config = ConfigModel()
    assert config.layered_max_turns == LAYERED_MAX_TURNS_DEFAULT


def test_agent_type_default_is_glm_async() -> None:
    config = ConfigModel()
    assert config.agent_type == "glm-async"


def test_run_limit_defaults_to_autonomous_with_100_steps_fallback() -> None:
    config = ConfigModel()
    assert config.run_limit_type == "autonomous"
    assert config.default_max_steps == 100
    assert config.default_max_duration_seconds is None


def test_observation_window_defaults() -> None:
    config = ConfigModel()
    assert (
        config.observation_window_screenshot_count
        == OBSERVATION_WINDOW_SCREENSHOT_COUNT_DEFAULT
    )
    assert (
        config.observation_window_interval_seconds
        == OBSERVATION_WINDOW_INTERVAL_SECONDS_DEFAULT
    )


def test_run_limit_type_validation() -> None:
    with pytest.raises(ValueError, match="autonomous.*steps.*duration.*unlimited"):
        ConfigModel(run_limit_type="bad")


def test_default_max_duration_seconds_validation() -> None:
    with pytest.raises(
        ValueError, match="default_max_duration_seconds must be positive"
    ):
        ConfigModel(default_max_duration_seconds=0)


def test_layered_max_turns_minimum_validation() -> None:
    with pytest.raises(ValueError, match="layered_max_turns must be >= 1"):
        ConfigModel(layered_max_turns=0)


def test_observation_window_bounds_validation() -> None:
    with pytest.raises(
        ValueError,
        match="observation_window_screenshot_count must be between 1 and 20",
    ):
        ConfigModel(observation_window_screenshot_count=0)
    with pytest.raises(
        ValueError,
        match="observation_window_interval_seconds must be between 0 and 60",
    ):
        ConfigModel(observation_window_interval_seconds=61)


def test_layered_max_turns_allows_positive_values() -> None:
    config = ConfigModel(layered_max_turns=1)
    assert config.layered_max_turns == 1


def test_layered_max_turns_env_var_parsing(monkeypatch) -> None:
    from AutoGLM_GUI.config_manager import UnifiedConfigManager

    manager = UnifiedConfigManager()
    monkeypatch.setenv("AUTOGLM_LAYERED_MAX_TURNS", "75")
    manager.load_env_config()
    config = manager.get_effective_config()
    assert config.layered_max_turns == 75


def test_layered_max_turns_env_var_invalid(monkeypatch) -> None:
    from AutoGLM_GUI.config_manager import UnifiedConfigManager

    manager = UnifiedConfigManager()
    monkeypatch.setenv("AUTOGLM_LAYERED_MAX_TURNS", "invalid")
    manager.load_env_config()
    config = manager.get_effective_config()
    assert config.layered_max_turns == 50


def test_load_file_config_migrates_legacy_glm_agent_type(tmp_path, monkeypatch):
    from AutoGLM_GUI.config_manager import UnifiedConfigManager

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "base_url": "https://example.com/v1",
                "model_name": "autoglm-phone-9b",
                "agent_type": "glm",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # Use a fresh singleton so this test does not depend on global config state.
    monkeypatch.setattr(UnifiedConfigManager, "_instance", None)
    monkeypatch.setattr(UnifiedConfigManager, "_config_path", config_path)
    manager = UnifiedConfigManager()

    loaded = manager.load_file_config(force_reload=True)

    assert loaded is True
    assert manager.get_effective_config().agent_type == "glm-async"


def test_load_file_config_migrates_legacy_null_steps_to_unlimited(
    tmp_path, monkeypatch
) -> None:
    from AutoGLM_GUI.config_manager import UnifiedConfigManager

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "base_url": "https://example.com/v1",
                "model_name": "autoglm-phone-9b",
                "default_max_steps": None,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(UnifiedConfigManager, "_instance", None)
    monkeypatch.setattr(UnifiedConfigManager, "_config_path", config_path)
    manager = UnifiedConfigManager()

    loaded = manager.load_file_config(force_reload=True)

    assert loaded is True
    config = manager.get_effective_config()
    assert config.run_limit_type == "unlimited"
    assert config.default_max_steps is None


def test_default_max_steps_allows_none() -> None:
    config = ConfigModel(default_max_steps=None)
    assert config.default_max_steps is None


def test_default_max_steps_env_var_parsing(monkeypatch) -> None:
    from AutoGLM_GUI.config_manager import UnifiedConfigManager

    manager = UnifiedConfigManager()
    monkeypatch.setenv("AUTOGLM_DEFAULT_MAX_STEPS", "10000")
    manager.load_env_config()
    config = manager.get_effective_config()
    assert config.default_max_steps == 10000


def test_run_limit_env_var_parsing(monkeypatch) -> None:
    from AutoGLM_GUI.config_manager import UnifiedConfigManager

    manager = UnifiedConfigManager()
    monkeypatch.setenv("AUTOGLM_RUN_LIMIT_TYPE", "duration")
    monkeypatch.setenv("AUTOGLM_DEFAULT_MAX_DURATION_SECONDS", "86400")
    manager.load_env_config()
    config = manager.get_effective_config()
    assert config.run_limit_type == "duration"
    assert config.default_max_duration_seconds == 86400


def test_observation_window_env_var_parsing(monkeypatch) -> None:
    from AutoGLM_GUI.config_manager import UnifiedConfigManager

    manager = UnifiedConfigManager()
    monkeypatch.setenv("AUTOGLM_OBSERVATION_WINDOW_SCREENSHOT_COUNT", "9")
    monkeypatch.setenv("AUTOGLM_OBSERVATION_WINDOW_INTERVAL_SECONDS", "1.5")
    manager.load_env_config()
    config = manager.get_effective_config()
    assert config.observation_window_screenshot_count == 9
    assert config.observation_window_interval_seconds == 1.5


def test_save_file_config_persists_explicit_null_default_max_steps(
    tmp_path, monkeypatch
) -> None:
    from AutoGLM_GUI.config_manager import UnifiedConfigManager

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "base_url": "https://example.com/v1",
                "model_name": "autoglm-phone-9b",
                "default_max_steps": 100,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(UnifiedConfigManager, "_instance", None)
    monkeypatch.setattr(UnifiedConfigManager, "_config_path", config_path)
    manager = UnifiedConfigManager()

    saved = manager.save_file_config(
        base_url="https://example.com/v1",
        model_name="autoglm-phone-9b",
        default_max_steps=None,
        default_max_steps_set=True,
        merge_mode=True,
    )

    assert saved is True
    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert "default_max_steps" in persisted
    assert persisted["default_max_steps"] is None


def test_save_file_config_persists_duration_run_limit(tmp_path, monkeypatch) -> None:
    from AutoGLM_GUI.config_manager import UnifiedConfigManager

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "base_url": "https://example.com/v1",
                "model_name": "autoglm-phone-9b",
                "run_limit_type": "steps",
                "default_max_duration_seconds": None,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(UnifiedConfigManager, "_instance", None)
    monkeypatch.setattr(UnifiedConfigManager, "_config_path", config_path)
    manager = UnifiedConfigManager()

    saved = manager.save_file_config(
        base_url="https://example.com/v1",
        model_name="autoglm-phone-9b",
        run_limit_type="duration",
        default_max_duration_seconds=86400,
        run_limit_type_set=True,
        default_max_duration_seconds_set=True,
        merge_mode=True,
    )

    assert saved is True
    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["run_limit_type"] == "duration"
    assert persisted["default_max_duration_seconds"] == 86400


def test_save_file_config_persists_observation_window_fields(
    tmp_path, monkeypatch
) -> None:
    from AutoGLM_GUI.config_manager import UnifiedConfigManager

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "base_url": "https://example.com/v1",
                "model_name": "autoglm-phone-9b",
                "observation_window_screenshot_count": 5,
                "observation_window_interval_seconds": 3.0,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(UnifiedConfigManager, "_instance", None)
    monkeypatch.setattr(UnifiedConfigManager, "_config_path", config_path)
    manager = UnifiedConfigManager()

    saved = manager.save_file_config(
        base_url="https://example.com/v1",
        model_name="autoglm-phone-9b",
        observation_window_screenshot_count=7,
        observation_window_interval_seconds=2.0,
        observation_window_screenshot_count_set=True,
        observation_window_interval_seconds_set=True,
        merge_mode=True,
    )

    assert saved is True
    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["observation_window_screenshot_count"] == 7
    assert persisted["observation_window_interval_seconds"] == 2.0
