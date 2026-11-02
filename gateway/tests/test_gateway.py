"""Tests for gateway module."""

import asyncio

import pytest

from stackchan_mcp.cloud_proxy import CloudProxy
from stackchan_mcp.gateway import Gateway, get_gateway
from stackchan_mcp.gemini_voice_proxy import GeminiVoiceProxy


def test_get_gateway_singleton():
    """get_gateway returns the same instance."""
    # Reset singleton for test isolation
    import stackchan_mcp.gateway as gw_mod
    gw_mod._gateway = None

    g1 = get_gateway()
    g2 = get_gateway()
    assert g1 is g2

    # Cleanup
    gw_mod._gateway = None


def test_vision_url_uses_explicit_url(monkeypatch):
    """VISION_URL overrides host/port construction for remote tunnels."""
    monkeypatch.setenv("VISION_URL", "https://stackchan.example.ts.net:8443/capture")
    monkeypatch.setenv("VISION_HOST", "192.0.2.10")
    monkeypatch.setenv("CAPTURE_PORT", "8766")

    gw = Gateway()

    assert gw.vision_url == "https://stackchan.example.ts.net:8443/capture"


def test_vision_url_uses_lan_host(monkeypatch):
    """VISION_HOST and CAPTURE_PORT still build the default LAN capture URL."""
    monkeypatch.delenv("VISION_URL", raising=False)
    monkeypatch.setenv("VISION_HOST", "192.0.2.10")
    monkeypatch.setenv("CAPTURE_PORT", "8766")

    gw = Gateway()

    assert gw.vision_url == "http://192.0.2.10:8766/capture"


def test_vision_token_prefers_explicit_token(monkeypatch):
    """VISION_TOKEN can be separated from the WebSocket token."""
    monkeypatch.setenv("VISION_TOKEN", "capture-token")
    monkeypatch.setenv("STACKCHAN_TOKEN", "ws-token")

    gw = Gateway()

    assert gw.vision_token == "capture-token"


def test_vision_token_falls_back_to_stackchan_token(monkeypatch):
    """Capture uploads use the gateway token by default."""
    monkeypatch.delenv("VISION_TOKEN", raising=False)
    monkeypatch.setenv("STACKCHAN_TOKEN", "ws-token")
    monkeypatch.setenv("BEARER_TOKEN", "legacy-token")

    gw = Gateway()

    assert gw.vision_token == "ws-token"


@pytest.mark.asyncio
async def test_gateway_start_stop(monkeypatch):
    """Gateway can start and stop."""
    monkeypatch.setenv("WS_PORT", "0")  # Random port
    monkeypatch.setenv("CAPTURE_PORT", "0")  # Random port

    gw = Gateway()
    await gw.start()
    assert gw._running is True
    assert gw.esp32._server is not None

    await gw.stop()
    assert gw._running is False


@pytest.mark.asyncio
async def test_track_endpoint_forwards_to_tracking_bridge():
    """POST /track on the capture server reaches TrackingBridge.handle_detection.

    Face-tracker fan-out: the vision tracker (or its live-view server) posts a face
    detection to http://gateway:8766/track, and the gateway routes it into
    the singleton TrackingBridge. Replace handle_detection with a recorder
    so we don't need real servos.
    """
    gw = Gateway()

    captured: list[dict] = []

    async def recorder(detection):
        captured.append(detection)
        return True

    gw.tracking_bridge.handle_detection = recorder  # type: ignore[assignment]

    payload = {"x": 0.4, "y": 0.55, "confidence": 0.88}

    class _FakeRequest:
        async def json(self):
            return payload

    response = await gw._handle_track(_FakeRequest())
    assert response.status == 200
    assert '"ok": true' in response.text
    assert captured == [payload]


@pytest.mark.asyncio
async def test_track_demo_reaction_waits_for_esp32_initialized(monkeypatch):
    """A boot-time face must not burn the demo line before ESP32 is ready."""
    monkeypatch.setenv("STACKCHAN_USB_DISABLE", "1")
    gw = Gateway()

    async def recorder(_detection):
        return True

    class _FakeDemoReactions:
        def __init__(self):
            self.checked = 0
            self.spawned = 0

        def maybe_face_entered(self, _detection):
            self.checked += 1
            return True

        def spawn_face_reaction(self):
            self.spawned += 1

    payload = {"x": 0.4, "y": 0.55, "confidence": 0.88}

    class _FakeRequest:
        async def json(self):
            return payload

    demo = _FakeDemoReactions()
    gw.tracking_bridge.handle_detection = recorder  # type: ignore[assignment]
    gw.demo_reactions = demo  # type: ignore[assignment]

    gw.esp32.get_status = lambda: {"connected": True, "initialized": False}  # type: ignore[method-assign]
    response = await gw._handle_track(_FakeRequest())
    assert response.status == 200
    assert demo.checked == 0
    assert demo.spawned == 0

    gw.esp32.get_status = lambda: {"connected": True, "initialized": True}  # type: ignore[method-assign]
    response = await gw._handle_track(_FakeRequest())
    assert response.status == 200
    assert demo.checked == 1
    assert demo.spawned == 1


@pytest.mark.asyncio
async def test_demo_arm_endpoint_clears_next_face_trigger(monkeypatch):
    monkeypatch.setenv("STACKCHAN_USB_DISABLE", "1")
    gw = Gateway()

    class _FakeDemoReactions:
        def __init__(self):
            self.armed = 0

        def arm_face_entry(self):
            self.armed += 1

    class _FakeRequest:
        pass

    demo = _FakeDemoReactions()
    gw.demo_reactions = demo  # type: ignore[assignment]

    response = await gw._handle_demo_arm(_FakeRequest())

    assert response.status == 200
    assert demo.armed == 1


@pytest.mark.asyncio
async def test_track_endpoint_returns_400_on_invalid_json():
    """Bad JSON from the upstream tracker must not crash the gateway."""
    gw = Gateway()

    class _BadRequest:
        async def json(self):
            raise ValueError("not json")

    response = await gw._handle_track(_BadRequest())
    assert response.status == 400
    assert '"ok": false' in response.text


# --- face tracking: talk/listen arbitration ----------------------------------


@pytest.mark.asyncio
async def test_gateway_voice_signal_arbitrates_tracking(monkeypatch):
    from types import SimpleNamespace
    from stackchan_mcp.debug_status import DebugStatus
    from stackchan_mcp.wake_gate import WakeGate, WakeGateState

    monkeypatch.setattr("stackchan_mcp.debug_status._status", DebugStatus())
    monkeypatch.setenv("STACKCHAN_USB_DISABLE", "1")
    gw = Gateway()
    proxy = gw._make_voice_proxy()
    gate = WakeGate(kws=None)
    proxy._wake_gate = gate
    gw.esp32._connection = SimpleNamespace(cloud_proxy=proxy)
    assert gw.tracking_bridge.mode == "idle"
    assert gw._wake_head_motion_allowed() is False
    gate.state = WakeGateState.LISTENING
    assert gw.tracking_bridge.mode == "quiet"
    assert gw._wake_head_motion_allowed() is True
    proxy._status.on_tts_state(True)
    assert gw.tracking_bridge.mode == "working"
    proxy._status.on_tts_state(False)
    gate.state = WakeGateState.DORMANT
    assert gw.tracking_bridge.mode == "idle"
    assert gw._wake_head_motion_allowed() is False
    proxy._wake_gate_passthrough = True
    gate.state = WakeGateState.LISTENING
    assert gw._wake_head_motion_allowed() is False


@pytest.mark.asyncio
async def test_gbot_wake_hook_homes_on_dormant(monkeypatch):
    monkeypatch.setenv("STACKCHAN_USB_DISABLE", "1")
    monkeypatch.setenv("STACKCHAN_VOICE_BACKEND", "gbot")
    gw = Gateway()
    proxy = gw._make_voice_proxy()
    assert proxy.on_wake_motion == gw._on_wake_motion

    class Dev:
        device_connected = True
        mcp_supported = False

        def __init__(self):
            self.head: list[tuple[int, int, int]] = []

        async def send_head(self, yaw, pitch, speed, **kwargs):
            self.head.append((int(yaw), int(pitch), int(speed)))
            return {"ok": True}, None

    dev = Dev()
    gw.idle_behavior.esp32 = dev
    await proxy.on_wake_motion(False)
    assert dev.head == [(0, 45, gw.idle_behavior.HOME_SPEED)]
    await proxy.on_wake_motion(True)
    assert gw.idle_behavior.wake_active is True
    assert len(dev.head) == 1
    await proxy.on_wake_motion(False)
    assert gw.idle_behavior.wake_active is False
    assert dev.head[-1][:2] == (0, 45)


@pytest.mark.asyncio
async def test_xiaozhi_speech_pauses_detection_until_cloud_tts_stops(monkeypatch):
    import json
    from types import SimpleNamespace
    from stackchan_mcp.debug_status import DebugStatus

    monkeypatch.setattr("stackchan_mcp.debug_status._status", DebugStatus())
    monkeypatch.setenv("STACKCHAN_USB_DISABLE", "1")
    monkeypatch.setenv("STACKCHAN_VOICE_BACKEND", "xiaozhi")
    gw = Gateway()
    await gw.start_face_tracking()
    cloud = gw._make_voice_proxy()
    assert isinstance(cloud, CloudProxy)
    gw.esp32._connection = SimpleNamespace(cloud_proxy=cloud, connected=True)
    calls = []

    async def head(name, arguments, **kwargs):
        if name == "self.robot.set_head_angles":
            calls.append((name, arguments))
        return {"ok": True}, None

    gw.esp32.call_tool = head
    detection = {"x": 0.8, "y": 0.5, "confidence": 0.9}
    await cloud._handle_cloud_json(json.dumps({"type": "tts", "state": "start"}))
    assert gw.tracking_bridge.mode == "working"
    assert await gw.tracking_bridge.handle_detection(detection) is False
    assert calls == []
    await cloud._handle_cloud_json(json.dumps({"type": "tts", "state": "stop"}))
    assert gw.tracking_bridge.mode == "idle"
    # No wake gate on the xiaozhi proxy: autonomous chase stays off.
    assert await gw.tracking_bridge.handle_detection(detection) is False
    assert calls == []
    await gw.tracking_bridge.stop()


@pytest.mark.asyncio
async def test_local_say_state_pauses_detection_until_tts_stop(monkeypatch):
    from stackchan_mcp.debug_status import DebugStatus

    monkeypatch.setattr("stackchan_mcp.debug_status._status", DebugStatus())
    monkeypatch.setenv("STACKCHAN_USB_DISABLE", "1")
    gw = Gateway()
    await gw.start_face_tracking()
    sent = []

    class Device:
        connected = True
        cloud_proxy = None

        async def send_tts_state(self, state):
            sent.append(state)

    gw.esp32._connection = Device()
    calls = []

    async def head(name, arguments, **kwargs):
        if name == "self.robot.set_head_angles":
            calls.append((name, arguments))
        return {"ok": True}, None

    gw.esp32.call_tool = head
    detection = {"x": 0.8, "y": 0.5, "confidence": 0.9}
    await gw.esp32.send_tts_state("start")  # orchestrator's say path
    assert gw.tracking_bridge.mode == "working"
    assert await gw.tracking_bridge.handle_detection(detection) is False
    assert calls == []
    await gw.esp32.send_tts_state("stop")
    assert gw.tracking_bridge.mode == "idle"
    # No voice proxy, so there is no LISTENING window to chase in.
    assert await gw.tracking_bridge.handle_detection(detection) is False
    assert calls == []
    assert sent == ["start", "stop"]
    await gw.tracking_bridge.stop()


# --- face tracking: tracker process and head-follow switch -------------------


class _FakeTrackerChild:
    """Runs like a real process until it is terminated or the test makes it exit."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode = None
        self.terminated = False
        self._exited = asyncio.Event()

    def terminate(self):
        self.terminated = True
        self.exit(0)

    def kill(self):
        self.exit(-9)

    def exit(self, code: int) -> None:
        if self.returncode is None:
            self.returncode = code
            self._exited.set()

    async def wait(self):
        await self._exited.wait()
        return self.returncode


@pytest.fixture
def fake_tracker(monkeypatch, tmp_path):
    """Replace the tracker child process with a fake and point at an existing binary."""
    binary = tmp_path / "groki-vision-tracker"
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setenv("STACKCHAN_FACE_TRACKER_BIN", str(binary))
    monkeypatch.setenv("STACKCHAN_FACE_TRACKER_AUTOSTART", "1")
    monkeypatch.setenv("STACKCHAN_USB_DISABLE", "1")
    monkeypatch.setenv("WS_PORT", "0")
    monkeypatch.setenv("CAPTURE_PORT", "0")
    children: list[_FakeTrackerChild] = []
    calls: list[tuple] = []

    async def spawn(*args, **kwargs):
        calls.append(args)
        child = _FakeTrackerChild(1000 + len(children))
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return {"children": children, "calls": calls, "binary": binary}


async def _until(predicate, timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


def test_default_tracker_binary_is_the_swift_release_build(monkeypatch):
    from pathlib import Path

    from stackchan_mcp.face_tracker import default_tracker_binary

    monkeypatch.delenv("STACKCHAN_FACE_TRACKER_BIN", raising=False)
    repo = Path(__file__).resolve().parents[2]
    assert default_tracker_binary() == (
        repo / "tools" / "vision-tracker" / ".build" / "release" / "groki-vision-tracker"
    )


@pytest.mark.asyncio
async def test_gateway_start_launches_face_tracker_and_stop_reaps_it(fake_tracker):
    gw = Gateway()
    await gw.start()
    try:
        await _until(lambda: len(fake_tracker["children"]) == 1)
        args = fake_tracker["calls"][0]
        assert args[0] == str(fake_tracker["binary"])
        assert args[1] == "--endpoint" and args[2].endswith("/track")
        assert args[-2:] == ("--fps", "8")
    finally:
        await gw.stop()
    child = fake_tracker["children"][0]
    assert child.terminated is True
    assert child.returncode is not None
    assert len(fake_tracker["children"]) == 1


@pytest.mark.asyncio
async def test_face_tracker_camera_setting_is_passed_to_the_tracker(fake_tracker, monkeypatch):
    monkeypatch.setenv("STACKCHAN_FACE_TRACKER_CAMERA", "BRIO")
    gw = Gateway()
    await gw.start()
    try:
        await _until(lambda: len(fake_tracker["calls"]) == 1)
        assert fake_tracker["calls"][0][-2:] == ("--camera", "BRIO")
    finally:
        await gw.stop()


@pytest.mark.asyncio
async def test_face_tracker_restarts_after_unexpected_exit(fake_tracker, caplog):
    caplog.set_level("INFO")
    gw = Gateway()
    gw.face_tracker.initial_backoff_s = 0.01
    await gw.start()
    try:
        await _until(lambda: len(fake_tracker["children"]) == 1)
        fake_tracker["children"][0].exit(1)
        await _until(lambda: len(fake_tracker["children"]) == 2)
        assert any(
            "face tracker exited" in r.getMessage() and "returncode=1" in r.getMessage()
            for r in caplog.records
        )
    finally:
        await gw.stop()
    assert all(child.returncode is not None for child in fake_tracker["children"])


@pytest.mark.asyncio
async def test_face_tracker_autostart_can_be_disabled(fake_tracker, monkeypatch):
    monkeypatch.setenv("STACKCHAN_FACE_TRACKER_AUTOSTART", "0")
    gw = Gateway()
    await gw.start()
    try:
        await asyncio.sleep(0.02)
        assert fake_tracker["children"] == []
        assert gw.face_tracker.running is False
    finally:
        await gw.stop()


@pytest.mark.asyncio
async def test_missing_face_tracker_logs_one_line_and_gateway_keeps_running(
    fake_tracker, monkeypatch, caplog,
):
    caplog.set_level("INFO")
    monkeypatch.setenv("STACKCHAN_FACE_TRACKER_BIN", str(fake_tracker["binary"]) + "-missing")
    gw = Gateway()
    gw.face_tracker.initial_backoff_s = 0.001
    await gw.start()
    try:
        await _until(lambda: gw.face_tracker._task is not None and gw.face_tracker._task.done())
        await asyncio.sleep(0.02)
        tracker_lines = [
            r for r in caplog.records
            if r.name == "stackchan_mcp.face_tracker" and r.levelname == "WARNING"
        ]
        assert len(tracker_lines) == 1
        assert "executable not found" in tracker_lines[0].getMessage()
        assert "swift build -c release" in tracker_lines[0].getMessage()
        assert fake_tracker["children"] == []
        assert gw._running is True
        assert gw.face_tracker.running is False

        # The rest of the gateway still works: /track answers and "look at me"
        # reports that the tracker is not running.
        response = await gw._handle_track(_JsonRequest({"x": 0.5, "y": 0.5, "confidence": 0.9}))
        assert response.status == 200
        on = await gw._handle_external_local_tool("set_face_tracking", {"enabled": True})
        assert on["ok"] is True and on["face_detector_running"] is False
        assert "warning" in on
    finally:
        await gw.stop()


@pytest.mark.asyncio
async def test_face_tracker_launch_failure_logs_one_line(fake_tracker, monkeypatch, caplog):
    caplog.set_level("INFO")

    async def spawn_fails(*args, **kwargs):
        raise PermissionError("not executable")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn_fails)
    gw = Gateway()
    await gw.start()
    try:
        await _until(lambda: gw.face_tracker._task is not None and gw.face_tracker._task.done())
        tracker_lines = [
            r for r in caplog.records
            if r.name == "stackchan_mcp.face_tracker" and r.levelname == "WARNING"
        ]
        assert len(tracker_lines) == 1
        assert "failed to start" in tracker_lines[0].getMessage()
        assert gw._running is True
    finally:
        await gw.stop()


@pytest.mark.asyncio
async def test_look_at_me_only_toggles_head_follow(fake_tracker):
    gw = Gateway()
    assert gw.tracking_bridge.enabled is True  # head follow is on at startup
    await gw.start()
    try:
        await _until(lambda: gw.face_tracker.running)

        off = await gw._handle_external_local_tool("set_face_tracking", {"enabled": False})
        assert off["ok"] is True and off["enabled"] is False
        assert gw.tracking_bridge.enabled is False
        assert fake_tracker["children"][0].returncode is None
        assert gw.face_tracker.running is True

        on = await gw._handle_external_local_tool("set_face_tracking", {"enabled": True})
        assert on["ok"] is True and on["enabled"] is True
        assert on["face_detector_running"] is True
        assert gw.tracking_bridge.enabled is True
        assert len(fake_tracker["children"]) == 1
        assert fake_tracker["children"][0].returncode is None
    finally:
        await gw.stop()


@pytest.mark.asyncio
async def test_set_face_tracking_rejects_non_boolean(monkeypatch):
    monkeypatch.setenv("STACKCHAN_USB_DISABLE", "1")
    gw = Gateway()
    result = await gw._handle_external_local_tool("set_face_tracking", {"enabled": "yes"})
    assert result == {"ok": False, "error": "enabled must be a boolean"}
    assert gw.tracking_bridge.enabled is True


class _HeadRecordingDevice:
    """A connected device stand-in that records the head commands it receives."""

    connected = True
    cloud_proxy = None
    device_id = "stackchan-test"
    initialized = False
    mcp_supported = True
    tools: list = []


def _connect_head_recorder(gw) -> list[dict]:
    head_calls: list[dict] = []

    async def call_tool(name, arguments, **kwargs):
        if name == "self.robot.set_head_angles":
            head_calls.append(arguments)
        return {"ok": True}, None

    gw.esp32._connection = _HeadRecordingDevice()
    gw.esp32.call_tool = call_tool
    return head_calls


def _open_listening_gate(gw, device=None):
    """Attach a LISTENING wake gate so autonomous face chase is allowed."""
    from types import SimpleNamespace
    from stackchan_mcp.wake_gate import WakeGate, WakeGateState

    gate = WakeGate(kws=None)
    gate.state = WakeGateState.LISTENING
    proxy = SimpleNamespace(_wake_gate=gate, _wake_gate_passthrough=False)
    target = device if device is not None else gw.esp32._connection
    target.cloud_proxy = proxy
    return gate


class _JsonRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


async def _send_face(gw, x: float) -> None:
    response = await gw._handle_track(_JsonRequest({"x": x, "y": 0.5, "confidence": 0.9}))
    assert response.status == 200


@pytest.mark.asyncio
async def test_head_follows_face_right_after_start_without_any_tool(fake_tracker, monkeypatch):
    from stackchan_mcp.debug_status import DebugStatus

    monkeypatch.setattr("stackchan_mcp.debug_status._status", DebugStatus())
    gw = Gateway()
    await gw.start()
    try:
        head_calls = _connect_head_recorder(gw)
        await _send_face(gw, 0.9)
        await asyncio.sleep(0.05)
        assert head_calls == []  # DORMANT / no gate: stay still

        _open_listening_gate(gw)
        await _send_face(gw, 0.9)
        await _until(lambda: head_calls)
    finally:
        await gw.stop()
        await gw.tracking_bridge.stop()


@pytest.mark.asyncio
async def test_voice_only_device_follows_face_through_ws_head_message(fake_tracker, monkeypatch):
    """Groki Bot firmware reports features.mcp=false: tracking uses the WS head message."""
    from stackchan_mcp.debug_status import DebugStatus

    monkeypatch.setattr("stackchan_mcp.debug_status._status", DebugStatus())
    gw = Gateway()
    await gw.start()
    try:
        sent: list[str] = []

        class VoiceOnlyDevice(_HeadRecordingDevice):
            mcp_supported = False

            async def send_head(self, yaw, pitch, speed):
                sent.append((yaw, pitch, speed))
                return {"ok": True}

        mcp_calls = []

        async def call_tool(name, arguments, **kwargs):
            mcp_calls.append(name)
            return None, {"code": -32000, "message": "ESP32 MCP unsupported (features.mcp=false)"}

        device = VoiceOnlyDevice()
        gw.esp32._connection = device
        gw.esp32.call_tool = call_tool
        _open_listening_gate(gw, device)
        await _send_face(gw, 0.9)
        # The head turns toward the face on the right (positive yaw).
        await _until(lambda: any(yaw > 0 for yaw, _pitch, _speed in sent))
        assert {speed for _yaw, _pitch, speed in sent} == {gw.tracking_bridge.TRACKING_SPEED}
        assert "self.robot.set_head_angles" not in mcp_calls
    finally:
        await gw.stop()
        await gw.tracking_bridge.stop()


@pytest.mark.asyncio
async def test_stop_looking_halts_head_but_keeps_face_time_and_look_at_me_resumes(
    fake_tracker, monkeypatch,
):
    from stackchan_mcp.debug_status import DebugStatus

    monkeypatch.setattr("stackchan_mcp.debug_status._status", DebugStatus())
    gw = Gateway()
    await gw.start()
    try:
        await _until(lambda: gw.face_tracker.running)
        head_calls = _connect_head_recorder(gw)
        await gw._handle_external_local_tool("set_face_tracking", {"enabled": False})

        await _send_face(gw, 0.9)
        await asyncio.sleep(0.05)
        assert head_calls == []
        assert gw.face_tracker.available is True  # detections still count

        await gw._handle_external_local_tool("set_face_tracking", {"enabled": True})
        await _send_face(gw, 0.9)
        await asyncio.sleep(0.05)
        assert head_calls == []  # follow on, but the wake gate is still closed

        _open_listening_gate(gw)
        await _send_face(gw, 0.9)
        await _until(lambda: head_calls)
    finally:
        await gw.stop()
        await gw.tracking_bridge.stop()


@pytest.mark.asyncio
async def test_head_follow_default_off_env_keeps_head_still_until_look_at_me(
    fake_tracker, monkeypatch,
):
    from stackchan_mcp.debug_status import DebugStatus

    monkeypatch.setattr("stackchan_mcp.debug_status._status", DebugStatus())
    monkeypatch.setenv("STACKCHAN_HEAD_FOLLOW_DEFAULT", "0")
    gw = Gateway()
    await gw.start()
    try:
        head_calls = _connect_head_recorder(gw)
        await _send_face(gw, 0.9)
        await asyncio.sleep(0.05)
        assert head_calls == []

        await gw._handle_external_local_tool("set_face_tracking", {"enabled": True})
        await _send_face(gw, 0.9)
        await asyncio.sleep(0.05)
        assert head_calls == []

        _open_listening_gate(gw)
        await _send_face(gw, 0.9)
        await _until(lambda: head_calls)
    finally:
        await gw.stop()
        await gw.tracking_bridge.stop()


@pytest.mark.asyncio
async def test_debug_status_reports_face_tracking(fake_tracker, monkeypatch):
    from stackchan_mcp.debug_status import get_debug_status

    gw = Gateway()
    await gw.start()
    try:
        await _until(lambda: gw.face_tracker.running)
        section = get_debug_status().snapshot()["face_tracking"]
        assert section["tracker_running"] is True
        assert section["tracker_pid"] == fake_tracker["children"][0].pid
        assert section["face_reported"] is False
        assert section["last_face_at"] is None
        assert section["head_follow"] is True
        assert section["mode"] == "idle"

        await _send_face(gw, 0.5)
        await gw._handle_external_local_tool("set_face_tracking", {"enabled": False})
        section = get_debug_status().snapshot()["face_tracking"]
        assert section["face_reported"] is True
        assert section["last_face_at"] is not None
        assert section["head_follow"] is False
    finally:
        await gw.stop()
        await gw.tracking_bridge.stop()


@pytest.mark.asyncio
async def test_face_detector_counts_as_available_only_after_face_since_this_start(
    fake_tracker,
):
    gw = Gateway()
    gw.face_tracker.initial_backoff_s = 0.01
    await gw.start()
    try:
        await _until(lambda: gw.face_tracker.running)
        assert gw.face_tracker.available is False

        await gw._handle_track(_JsonRequest({"x": 0.5, "y": 0.5, "confidence": 0.9}))
        assert gw.face_tracker.available is True

        fake_tracker["children"][0].exit(1)
        await _until(lambda: len(fake_tracker["children"]) == 2 and gw.face_tracker.running)
        assert gw.face_tracker.available is False  # a restart needs a new face report
    finally:
        await gw.stop()
        await gw.tracking_bridge.stop()


@pytest.mark.asyncio
async def test_camera_unavailable_exit_keeps_backing_off(fake_tracker, caplog):
    """Exit code 2 (no camera or no permission) never resets the backoff."""
    caplog.set_level("INFO")
    gw = Gateway()
    gw.face_tracker.initial_backoff_s = 0.1
    gw.face_tracker.stable_run_s = 0.0  # every run counts as stable
    await gw.start()
    try:
        children = fake_tracker["children"]
        for code, expected_count in ((2, 2), (2, 3), (1, 4)):
            await _until(lambda: len(children) == expected_count - 1)
            children[-1].exit(code)
            await _until(lambda: len(children) == expected_count)
        delays = [
            r.getMessage().rsplit("restarting in ", 1)[1]
            for r in caplog.records
            if "face tracker exited" in r.getMessage()
        ]
        assert delays == ["0.1s", "0.2s", "0.1s"]
    finally:
        await gw.stop()


@pytest.mark.asyncio
async def test_gemini_voice_switch_reaches_gateway_local_handler(monkeypatch):
    monkeypatch.setenv("STACKCHAN_USB_DISABLE", "1")
    monkeypatch.setenv("STACKCHAN_VOICE_BACKEND", "gemini")
    gw = Gateway()
    proxy = gw._make_voice_proxy()
    calls = []

    async def start():
        calls.append(True)
        return {"ok": True, "enabled": True}

    async def stop():
        calls.append(False)
        return {"ok": True, "enabled": False}

    gw.start_face_tracking = start
    gw.stop_face_tracking = stop
    assert await proxy.set_face_tracking(True) == {"ok": True, "enabled": True}
    assert await proxy.set_face_tracking(False) == {"ok": True, "enabled": False}
    assert calls == [True, False]


# --- voice backend selection -------------------------------------------------


def test_voice_backend_default_is_gemini(monkeypatch):
    monkeypatch.delenv("STACKCHAN_VOICE_BACKEND", raising=False)
    gw = Gateway()
    assert gw.voice_backend == "gemini"
    assert isinstance(gw._make_voice_proxy(), GeminiVoiceProxy)


def test_voice_backend_xiaozhi_returns_cloud_proxy(monkeypatch):
    monkeypatch.setenv("STACKCHAN_VOICE_BACKEND", "xiaozhi")
    gw = Gateway()
    assert gw.voice_backend == "xiaozhi"
    assert isinstance(gw._make_voice_proxy(), CloudProxy)


def test_voice_backend_unknown_falls_back_to_gemini(monkeypatch):
    """Typos in STACKCHAN_VOICE_BACKEND must not break the gateway."""
    monkeypatch.setenv("STACKCHAN_VOICE_BACKEND", "azure")
    gw = Gateway()
    assert isinstance(gw._make_voice_proxy(), GeminiVoiceProxy)


def test_voice_backend_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("STACKCHAN_VOICE_BACKEND", "Xiaozhi")
    gw = Gateway()
    assert gw.voice_backend == "xiaozhi"
    assert isinstance(gw._make_voice_proxy(), CloudProxy)


def test_voice_backend_gemini_passes_usb_and_esp32(monkeypatch):
    """GeminiVoiceProxy must see the same USB transport + esp32 the rest uses."""
    monkeypatch.setenv("STACKCHAN_VOICE_BACKEND", "gemini")
    monkeypatch.setenv("STACKCHAN_USB_DISABLE", "1")  # avoid touching /dev/cu.*
    gw = Gateway()
    proxy = gw._make_voice_proxy()
    assert isinstance(proxy, GeminiVoiceProxy)
    assert proxy.usb_transport is gw.usb_transport  # both None here
    # esp32_ref deferred-resolves to the live esp32 manager.
    assert proxy.esp32_ref() is gw.esp32
    assert proxy.set_face_tracking is not None


def test_gateway_wires_wake_detected_callback(monkeypatch):
    monkeypatch.setenv("STACKCHAN_USB_DISABLE", "1")
    gw = Gateway()
    assert gw.esp32.on_wake_detected == gw._run_wake_response
    assert gw.esp32.on_end_conversation == gw._handle_end_conversation


@pytest.mark.asyncio
async def test_wake_response_runs_head_and_led_concurrently(monkeypatch):
    monkeypatch.setenv("STACKCHAN_USB_DISABLE", "1")
    gw = Gateway()

    started: set[str] = set()
    release = asyncio.Event()
    calls: list[tuple[str, dict]] = []

    def mark_started(name: str) -> None:
        started.add(name)
        if started == {"head", "led"}:
            release.set()

    class FakeESP32:
        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            if name == "self.robot.set_head_angles":
                mark_started("head")
            elif name == "self.led.set_all":
                mark_started("led")
            await asyncio.wait_for(release.wait(), timeout=1.0)
            return {"ok": True}, None

    gw.esp32 = FakeESP32()

    await asyncio.wait_for(gw._run_wake_response(), timeout=1.0)

    assert ("self.robot.set_head_angles", {"yaw": 0, "pitch": 0, "speed": 700}) in calls
    assert ("self.led.set_all", {"r": 0, "g": 100, "b": 255}) in calls


@pytest.mark.asyncio
async def test_end_conversation_turns_off_led_resets_head_and_clears_context(monkeypatch):
    monkeypatch.setenv("STACKCHAN_USB_DISABLE", "1")
    gw = Gateway()

    calls: list[tuple[str, dict]] = []

    class FakeBridge:
        def __init__(self):
            self.cleared = False

        async def clear_conversation_context(self):
            self.cleared = True

    class FakeProxy:
        def __init__(self, bridge):
            self._bridge = bridge

    class FakeConnection:
        def __init__(self, proxy):
            self.cloud_proxy = proxy

    class FakeESP32:
        def __init__(self, bridge):
            self.connection = FakeConnection(FakeProxy(bridge))

        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return {"ok": True}, None

    bridge = FakeBridge()
    gw.esp32 = FakeESP32(bridge)

    result = await gw._handle_end_conversation()

    assert result == {"ok": True}
    assert bridge.cleared is True
    assert ("self.led.set_all", {"r": 0, "g": 0, "b": 0}) in calls
    assert ("self.robot.set_head_angles", {"yaw": 0, "pitch": 0, "speed": 700}) in calls


@pytest.mark.asyncio
async def test_gemini_say_routes_to_active_proxy_speak_text():
    """gemini_say external tool forwards text to the live proxy's speak_text."""
    from types import SimpleNamespace

    gw = Gateway()
    calls = {}

    async def fake_speak_text(text, *, session_id, prompt_audio_frames, emotion=None):
        calls["args"] = (text, session_id, prompt_audio_frames, emotion)
        return {"ok": True, "text": text, "provider": "gemini_live"}

    gw.esp32 = SimpleNamespace(
        connection=SimpleNamespace(cloud_proxy=SimpleNamespace(speak_text=fake_speak_text))
    )

    result = await gw._handle_external_local_tool("gemini_say", {"text": "搞定了"})

    assert result["ok"] is True
    assert calls["args"] == ("搞定了", "hook-notify", [], None)


@pytest.mark.asyncio
async def test_gemini_say_without_session_returns_error():
    from types import SimpleNamespace

    gw = Gateway()
    gw.esp32 = SimpleNamespace(connection=None)

    result = await gw._handle_external_local_tool("gemini_say", {"text": "hi"})

    assert result["ok"] is False
    assert "no active gemini" in result["error"]


@pytest.mark.asyncio
async def test_debug_inject_text_routes_to_active_bridge():
    from types import SimpleNamespace

    gw = Gateway()
    texts: list[str] = []

    async def inject_user_text(text: str) -> bool:
        texts.append(text)
        return True

    gw.esp32 = SimpleNamespace(
        connection=SimpleNamespace(
            cloud_proxy=SimpleNamespace(
                _bridge=SimpleNamespace(inject_user_text=inject_user_text)
            )
        )
    )

    active = await gw._inject_debug_text("放一首歌")

    assert active is True
    assert texts == ["放一首歌"]


def test_usb_transport_is_off_by_default(monkeypatch):
    """Opening /dev/cu.usbmodem* by default blocks flashing, so it is opt-in."""
    monkeypatch.delenv("STACKCHAN_USB_TRANSPORT", raising=False)
    monkeypatch.delenv("STACKCHAN_USB_DISABLE", raising=False)
    assert Gateway().usb_transport is None


def test_usb_transport_opt_in_and_disable_wins(monkeypatch):
    from stackchan_mcp.usb_transport import UsbTransport

    monkeypatch.setenv("STACKCHAN_USB_TRANSPORT", "1")
    monkeypatch.delenv("STACKCHAN_USB_DISABLE", raising=False)
    assert isinstance(Gateway().usb_transport, UsbTransport)

    monkeypatch.setenv("STACKCHAN_USB_DISABLE", "1")
    assert Gateway().usb_transport is None
