"""Contract tests for MCP tool implementations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

import AutoGLM_GUI.api.mcp as mcp_api
import AutoGLM_GUI.device_manager as device_manager_module
from AutoGLM_GUI.exceptions import DeviceNotAvailableError

pytestmark = [pytest.mark.contract, pytest.mark.release_gate]


@dataclass
class FakeConnectionType:
    value: str


@dataclass
class FakeManagedDevice:
    connection_type: FakeConnectionType


@dataclass
class FakeScreenshot:
    base64_data: str
    width: int
    height: int
    is_sensitive: bool


class FakeRemoteDevice:
    def __init__(self, screenshot: FakeScreenshot) -> None:
        self._screenshot = screenshot

    def get_screenshot(self, timeout: int = 10) -> FakeScreenshot:
        return self._screenshot


class FakeDeviceManager:
    def __init__(self) -> None:
        self.device_id_to_serial = {
            "local-device": "serial-local",
            "remote-device": "serial-remote",
        }
        self.serial_to_device = {
            "serial-local": FakeManagedDevice(FakeConnectionType("usb")),
            "serial-remote": FakeManagedDevice(FakeConnectionType("remote")),
        }
        self.remote_instances = {
            "serial-remote": FakeRemoteDevice(
                FakeScreenshot(
                    base64_data="REMOTE_IMG",
                    width=800,
                    height=1600,
                    is_sensitive=True,
                )
            )
        }

    def get_serial_by_device_id(self, device_id: str) -> str | None:
        return self.device_id_to_serial.get(device_id)

    def get_device_by_serial(self, serial: str) -> FakeManagedDevice | None:
        return self.serial_to_device.get(serial)

    def get_remote_device_instance(self, serial: str) -> FakeRemoteDevice | None:
        return self.remote_instances.get(serial)


@pytest.fixture
def mcp_env(monkeypatch: pytest.MonkeyPatch) -> dict:
    fake_manager = FakeDeviceManager()
    captured_requests: list[str | None] = []

    class FakeDeviceManagerClass:
        @staticmethod
        def get_instance() -> FakeDeviceManager:
            return fake_manager

    async def fake_capture_screenshot(device_id: str | None = None) -> FakeScreenshot:
        captured_requests.append(device_id)
        return FakeScreenshot(
            base64_data="LOCAL_IMG",
            width=1080,
            height=1920,
            is_sensitive=False,
        )

    monkeypatch.setattr(device_manager_module, "DeviceManager", FakeDeviceManagerClass)
    monkeypatch.setattr(mcp_api, "capture_screenshot_async", fake_capture_screenshot)

    return {
        "manager": fake_manager,
        "captured_requests": captured_requests,
    }


def test_mcp_screenshot_requires_device_id(mcp_env: dict) -> None:
    result = asyncio.run(mcp_api.screenshot(""))

    assert result.model_dump() == {
        "success": False,
        "image": "",
        "width": 0,
        "height": 0,
        "is_sensitive": False,
        "error": "device_id is required",
    }


def test_mcp_screenshot_device_not_found(mcp_env: dict) -> None:
    result = asyncio.run(mcp_api.screenshot("unknown-device"))

    assert result.success is False
    assert result.error == "Device unknown-device not found"


def test_mcp_screenshot_local_device_success(mcp_env: dict) -> None:
    result = asyncio.run(mcp_api.screenshot("local-device"))

    assert result.model_dump() == {
        "success": True,
        "image": "LOCAL_IMG",
        "width": 1080,
        "height": 1920,
        "is_sensitive": False,
        "error": None,
    }
    assert mcp_env["captured_requests"] == ["local-device"]


def test_mcp_screenshot_remote_device_success(mcp_env: dict) -> None:
    result = asyncio.run(mcp_api.screenshot("remote-device"))

    assert result.model_dump() == {
        "success": True,
        "image": "REMOTE_IMG",
        "width": 800,
        "height": 1600,
        "is_sensitive": True,
        "error": None,
    }


def test_mcp_screenshot_remote_device_missing_instance(mcp_env: dict) -> None:
    mcp_env["manager"].remote_instances.pop("serial-remote", None)

    result = asyncio.run(mcp_api.screenshot("remote-device"))

    assert result.success is False
    assert result.error == "Remote device serial-remote not found"


def test_mcp_screenshot_handles_device_not_available_error(
    mcp_env: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def raise_unavailable(device_id: str | None = None) -> FakeScreenshot:
        raise DeviceNotAvailableError("device temporarily offline")

    monkeypatch.setattr(mcp_api, "capture_screenshot_async", raise_unavailable)

    result = asyncio.run(mcp_api.screenshot("local-device"))

    assert result.model_dump() == {
        "success": False,
        "image": "",
        "width": 0,
        "height": 0,
        "is_sensitive": False,
        "error": "device temporarily offline",
    }


def test_mcp_chat_temporarily_uses_step_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeAgentConfig:
        max_steps = None
        run_limit_type = "duration"
        max_duration_seconds = 86400
        system_prompt = "original"

    class _FakeAgent:
        def __init__(self) -> None:
            self.agent_config = _FakeAgentConfig()
            self.step_count = 1
            self.seen_run_limit_type: str | None = None
            self.seen_max_steps: int | None = None

        def reset(self) -> None:
            pass

        async def run(self, message: str) -> str:
            _ = message
            self.seen_run_limit_type = self.agent_config.run_limit_type
            self.seen_max_steps = self.agent_config.max_steps
            return "ok"

    class _FakePhoneAgentManager:
        def __init__(self) -> None:
            self.agent = _FakeAgent()
            self.released: list[str] = []

        async def acquire_device_async(self, *args, **kwargs) -> bool:
            _ = (args, kwargs)
            return True

        def get_agent_with_context(self, *args, **kwargs) -> _FakeAgent:
            _ = (args, kwargs)
            return self.agent

        def release_device(self, device_id: str, **kwargs) -> None:
            _ = kwargs
            self.released.append(device_id)

    manager = _FakePhoneAgentManager()

    import AutoGLM_GUI.phone_agent_manager as pam_mod

    monkeypatch.setattr(
        pam_mod.PhoneAgentManager,
        "get_instance",
        staticmethod(lambda: manager),
    )

    result = asyncio.run(mcp_api.chat("device-1", "open settings"))

    assert result["success"] is True
    assert manager.agent.seen_run_limit_type == "steps"
    assert manager.agent.seen_max_steps == mcp_api.MCP_MAX_STEPS
    assert manager.agent.agent_config.run_limit_type == "duration"
    assert manager.agent.agent_config.max_steps is None
    assert manager.agent.agent_config.max_duration_seconds == 86400
    assert manager.released == ["device-1"]
