"""Tests for gateway-owned StackChan idle behavior."""

import random
import time

import pytest

from stackchan_mcp.idle_behavior import IdleBehavior


class FakeESP32:
    """In-memory ESP32Manager stand-in.

    F06 fix: idle_behavior now routes head writes through esp32.call_tool
    (same path as MCP tools) instead of connection.call_tool, so the fake
    records calls on itself. The legacy `connection.initialized` field is
    no longer consulted.
    """

    def __init__(self, *, connected: bool = True, moving: bool = False):
        self.device_connected = connected
        self.calls: list[tuple[str, dict]] = []
        self.connection = None  # Intentionally None: regression for F06
        self.moving = moving

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "self.robot.get_head_angles":
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f'{{"yaw":0,"pitch":15,"moving":{str(self.moving).lower()}}}',
                    }
                ],
            }, None
        return {"content": []}, None


class FakeUsbTransport:
    def __init__(self, connected: bool = True, raises: Exception | None = None, moving: bool = False):
        self.connected = connected
        self.calls: list[tuple[str, dict]] = []
        self._raises = raises
        self.moving = moving

    async def call_tool(self, name, arguments, *, timeout_s=None):
        self.calls.append((name, arguments))
        if self._raises is not None:
            raise self._raises
        if name == "self.robot.get_head_angles":
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": f'{{"yaw":0,"pitch":15,"moving":{str(self.moving).lower()}}}',
                        }
                    ]
                },
            }
        return {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}


def set_head_calls(calls: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    return [call for call in calls if call[0] == "self.robot.set_head_angles"]


@pytest.mark.asyncio
async def test_idle_step_moves_head_with_safe_pitch():
    esp32 = FakeESP32()
    idle = IdleBehavior(
        esp32,
        min_interval_s=0.01,
        max_interval_s=0.01,
        pause_seconds=0.01,
        rng=random.Random(1),
    )

    await idle.set_wake_active(True)
    sent = await idle.step_once()

    assert sent is True
    writes = set_head_calls(esp32.calls)
    assert len(writes) == 1
    name, args = writes[0]
    assert name == "self.robot.set_head_angles"
    assert -90 <= args["yaw"] <= 90
    assert 0 <= args["pitch"] <= 60
    assert 100 <= args["speed"] <= 400


@pytest.mark.asyncio
async def test_idle_step_works_when_connection_is_none():
    """F06 regression: stdio MCP mode has esp32.connection=None even though
    esp32.call_tool routes fine. idle must not block on that."""
    esp32 = FakeESP32()
    assert esp32.connection is None  # explicit pre-condition
    idle = IdleBehavior(esp32, rng=random.Random(1))
    await idle.set_wake_active(True)
    sent = await idle.step_once()
    assert sent is True
    assert len(set_head_calls(esp32.calls)) == 1


@pytest.mark.asyncio
async def test_activity_pauses_idle_step():
    esp32 = FakeESP32()
    idle = IdleBehavior(esp32, pause_seconds=60, rng=random.Random(1))

    await idle.set_wake_active(True)
    idle.notify_activity()
    sent = await idle.step_once()

    assert sent is False
    assert esp32.calls == []


@pytest.mark.asyncio
async def test_idle_step_skips_disconnected_device():
    esp32 = FakeESP32(connected=False)
    idle = IdleBehavior(esp32, rng=random.Random(1))
    await idle.set_wake_active(True)

    sent = await idle.step_once()

    assert sent is False
    assert esp32.calls == []


@pytest.mark.asyncio
async def test_idle_step_works_with_only_usb_up():
    """USB online + WS offline: idle still moves (USB is enough for control)."""
    esp32 = FakeESP32(connected=False)
    usb = FakeUsbTransport(connected=True)
    idle = IdleBehavior(esp32, rng=random.Random(1), usb_transport=usb)
    await idle.set_wake_active(True)

    sent = await idle.step_once()

    assert sent is True
    assert len(set_head_calls(usb.calls)) == 1
    assert esp32.calls == []


@pytest.mark.asyncio
async def test_idle_step_prefers_usb_when_both_up():
    esp32 = FakeESP32(connected=True)
    usb = FakeUsbTransport(connected=True)
    idle = IdleBehavior(esp32, rng=random.Random(1), usb_transport=usb)
    await idle.set_wake_active(True)

    sent = await idle.step_once()

    assert sent is True
    assert len(set_head_calls(usb.calls)) == 1
    assert esp32.calls == []


@pytest.mark.asyncio
async def test_idle_step_falls_back_to_ws_when_usb_raises():
    esp32 = FakeESP32(connected=True)
    usb = FakeUsbTransport(connected=True, raises=RuntimeError("usb gone"))
    idle = IdleBehavior(esp32, rng=random.Random(1), usb_transport=usb)
    await idle.set_wake_active(True)

    sent = await idle.step_once()

    assert sent is True
    assert len(set_head_calls(usb.calls)) == 1
    assert len(set_head_calls(esp32.calls)) == 1


@pytest.mark.asyncio
async def test_idle_step_defers_when_head_is_still_moving():
    esp32 = FakeESP32(moving=True)
    idle = IdleBehavior(esp32, rng=random.Random(1))
    await idle.set_wake_active(True)

    sent = await idle.step_once()

    assert sent is False
    assert set_head_calls(esp32.calls) == []
    assert idle._defer_until > time.monotonic()


@pytest.mark.asyncio
async def test_idle_start_is_idempotent_for_double_entry(monkeypatch):
    """With the gateway idle turned on explicitly, a second start creates no second task.

    It is off by default; this test only checks idempotence when explicitly enabled.
    """
    monkeypatch.setenv("STACKCHAN_GATEWAY_IDLE_ENABLED", "1")
    esp32 = FakeESP32()
    idle = IdleBehavior(
        esp32,
        min_interval_s=10,
        max_interval_s=10,
        rng=random.Random(1),
    )
    try:
        await idle.start()
        first_task = idle._task
        await idle.start()
        assert idle._task is first_task
        assert idle.running is True
    finally:
        await idle.stop()
        assert idle.running is False


@pytest.mark.asyncio
async def test_idle_disabled_by_default_so_firmware_owns_idle():
    """STACKCHAN_GATEWAY_IDLE_ENABLED defaults to 0, and wake motion
    defaults off, so start() does not spawn a look-around task."""
    esp32 = FakeESP32()
    idle = IdleBehavior(esp32, rng=random.Random(1))
    await idle.start()
    assert idle.running is False
    assert idle._task is None
    assert idle.wake_active is False


class VoiceOnlyESP32:
    """features.mcp=false: set_head_angles is rejected, WS head works."""

    device_connected = True
    mcp_supported = False

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.head_calls: list[tuple] = []

    async def call_tool(self, name, arguments, **kwargs):
        self.calls.append((name, arguments))
        if name == "self.robot.get_head_angles":
            return {
                "content": [
                    {"type": "text", "text": '{"yaw":10,"pitch":20,"moving":false}'}
                ],
            }, None
        return None, {"code": -32000, "message": "ESP32 MCP unsupported (features.mcp=false)"}

    async def send_head(self, yaw, pitch, speed, **kwargs):
        self.head_calls.append((yaw, pitch, speed, kwargs))
        return {"ok": True}, None


@pytest.mark.asyncio
async def test_idle_step_suppressed_until_wake_active():
    esp32 = FakeESP32()
    idle = IdleBehavior(esp32, rng=random.Random(1))

    assert idle.wake_active is False
    assert await idle.step_once() is False
    assert esp32.calls == []

    await idle.set_wake_active(True)
    assert await idle.step_once() is True
    assert len(set_head_calls(esp32.calls)) == 1


@pytest.mark.asyncio
async def test_voice_only_idle_uses_ws_head_message():
    esp32 = VoiceOnlyESP32()
    idle = IdleBehavior(esp32, rng=random.Random(1))
    await idle.set_wake_active(True)

    sent = await idle.step_once()

    assert sent is True
    assert set_head_calls(esp32.calls) == []
    assert len(esp32.head_calls) == 1
    _yaw, _pitch, speed, kwargs = esp32.head_calls[0]
    assert 100 <= speed <= 400
    assert kwargs == {"notify_activity": False, "notify_head_command": False}


@pytest.mark.asyncio
async def test_dormant_transition_sends_home_once_and_stops_steps():
    esp32 = VoiceOnlyESP32()
    idle = IdleBehavior(esp32, rng=random.Random(1))
    await idle.set_wake_active(True)
    assert await idle.step_once() is True
    esp32.head_calls.clear()

    await idle.set_wake_active(False)

    assert idle.wake_active is False
    assert len(esp32.head_calls) == 1
    yaw, pitch, speed, _kwargs = esp32.head_calls[0]
    assert (yaw, pitch, speed) == (IdleBehavior.HOME_YAW, IdleBehavior.HOME_PITCH, IdleBehavior.HOME_SPEED)
    assert await idle.step_once() is False

    await idle.set_wake_active(False)
    assert len(esp32.head_calls) == 1


@pytest.mark.asyncio
async def test_dormant_home_retries_when_device_was_down():
    esp32 = FakeESP32(connected=False)
    idle = IdleBehavior(esp32, rng=random.Random(1))

    await idle.set_wake_active(False)
    assert esp32.calls == []

    esp32.device_connected = True
    await idle.set_wake_active(False)
    writes = set_head_calls(esp32.calls)
    assert len(writes) == 1
    assert writes[0][1]["yaw"] == IdleBehavior.HOME_YAW
    assert writes[0][1]["pitch"] == IdleBehavior.HOME_PITCH
