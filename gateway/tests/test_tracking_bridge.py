"""Tests for Apple Vision tracking bridge."""

import pytest

from stackchan_mcp.tracking_bridge import TrackingBridge, TrackingConfig


class FakeESP32:
    device_connected = True

    def __init__(self):
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"content": []}, None


class FakeESP32WithNotifyFlags:
    device_connected = True

    def __init__(self):
        self.calls = []

    async def call_tool(self, name, arguments, **kwargs):
        self.calls.append((name, arguments, kwargs))
        return {"content": []}, None


async def pump_resampler(bridge: TrackingBridge, *, fraction: float = 1.0) -> None:
    """Drive one deterministic resampler tick for tests."""
    assert bridge._last_target_update_s is not None
    await bridge._resample_tick(
        bridge._segment_start_s + bridge._segment_duration_s * fraction
    )


@pytest.mark.asyncio
async def test_tracking_detection_moves_head_when_confident():
    esp32 = FakeESP32()
    bridge = TrackingBridge(esp32, config=TrackingConfig(smoothing=1.0, move_threshold=0.0))

    moved = await bridge.handle_detection({"x": 0.25, "y": 0.6, "confidence": 0.9})
    await pump_resampler(bridge)

    assert moved is True
    name, args = esp32.calls[0]
    assert name == "self.robot.set_head_angles"
    # x=0.25 sits left-of-centre. With the corrected yaw polarity the head
    # rotates negatively (toward the user-left side of the camera frame).
    assert args["yaw"] < 0
    assert 0 <= args["pitch"] <= 45
    assert args["speed"] == 700
    await bridge.stop()


@pytest.mark.asyncio
async def test_tracking_default_pitch_limit_allows_higher_head_raise():
    """Follow mode uses the expanded head pitch range."""
    esp32 = FakeESP32()
    bridge = TrackingBridge(
        esp32,
        config=TrackingConfig(
            gain_y=3.0,
            smoothing=1.0,
            move_threshold=0.0,
        ),
    )

    moved = await bridge.handle_detection({"x": 0.5, "y": 1.0, "confidence": 0.9})
    await pump_resampler(bridge)

    assert moved is True
    _name, args = esp32.calls[0]
    assert args["pitch"] == 45
    await bridge.stop()


@pytest.mark.asyncio
async def test_tracking_ignores_low_confidence_and_working_mode():
    esp32 = FakeESP32()
    bridge = TrackingBridge(esp32)

    assert await bridge.handle_detection({"x": 0.1, "y": 0.5, "confidence": 0.1}) is False
    bridge.mode = "working"
    assert await bridge.handle_detection({"x": 0.1, "y": 0.5, "confidence": 0.9}) is False
    assert esp32.calls == []


@pytest.mark.asyncio
async def test_wake_policy_chases_only_while_listening():
    """Gateway policy: LISTENING may chase; DORMANT and a failing policy hold still."""
    esp32 = FakeESP32()
    allowed = {"on": False}

    def policy() -> bool:
        if allowed["on"] == "boom":
            raise RuntimeError("policy down")
        return bool(allowed["on"])

    bridge = TrackingBridge(
        esp32,
        config=TrackingConfig(smoothing=1.0, move_threshold=0.0),
        is_listening=lambda: bool(allowed["on"]) if allowed["on"] != "boom" else False,
        wake_motion_allowed=policy,
    )
    detection = {"x": 0.8, "y": 0.5, "confidence": 0.9}
    try:
        assert await bridge.handle_detection(detection) is False
        allowed["on"] = True
        assert bridge.mode == "quiet"
        assert await bridge.handle_detection(detection) is True
        await pump_resampler(bridge)
        assert len(esp32.calls) == 1
        allowed["on"] = False
        esp32.calls.clear()
        assert await bridge.handle_detection(detection) is False
        await pump_resampler(bridge)
        assert esp32.calls == []
        allowed["on"] = "boom"
        assert await bridge.handle_detection(detection) is False
    finally:
        await bridge.stop()


@pytest.mark.asyncio
async def test_live_voice_states_pause_tracking_and_resume_head_writes():
    esp32 = FakeESP32()
    voice = {"tts": False, "listening": False}
    bridge = TrackingBridge(
        esp32,
        config=TrackingConfig(smoothing=1.0, move_threshold=0.0),
        is_tts_active=lambda: voice["tts"],
        is_listening=lambda: voice["listening"],
    )
    detection = {"x": 0.8, "y": 0.5, "confidence": 0.9}
    try:
        voice["tts"] = True
        assert bridge.mode == "working"
        assert await bridge.handle_detection(detection) is False
        voice["tts"] = False
        voice["listening"] = True
        assert bridge.mode == "quiet"
        assert await bridge.handle_detection(detection) is False
        assert esp32.calls == []
        voice["listening"] = False
        assert bridge.mode == "idle"
        assert await bridge.handle_detection(detection) is True
        await pump_resampler(bridge)
        assert len(esp32.calls) == 1
    finally:
        await bridge.stop()


@pytest.mark.asyncio
async def test_active_resampler_stops_writing_when_speech_begins():
    esp32 = FakeESP32()
    voice = {"tts": False}
    bridge = TrackingBridge(
        esp32, config=TrackingConfig(smoothing=1.0, move_threshold=0.0),
        is_tts_active=lambda: voice["tts"],
    )
    try:
        assert await bridge.handle_detection({"x": 0.8, "y": 0.5, "confidence": 0.9})
        voice["tts"] = True
        await pump_resampler(bridge)
        assert esp32.calls == []
    finally:
        await bridge.stop()


def test_tracking_config_updates_live():
    bridge = TrackingBridge(FakeESP32())

    cfg = bridge.update_config({"gain_x": 1.5, "offset_y": 2, "unknown": 9})

    assert cfg["gain_x"] == 1.5
    assert cfg["offset_y"] == 2
    assert "unknown" not in cfg


def test_tracking_usb_timeout_stays_under_frame_interval():
    """Face tracking runs at 10fps; a flaky USB frame must fail over quickly."""
    assert TrackingBridge.USB_SERVO_TIMEOUT_S < 0.1


def test_tracking_resampler_runs_at_firmware_tick_rate():
    """Gateway output should match the firmware spring tick cadence."""
    assert TrackingBridge.RESAMPLE_HZ == 50.0


@pytest.mark.asyncio
async def test_tracking_resampler_lerps_between_detection_targets():
    """A target jump is fed to firmware as an interpolated curve, not a step."""
    esp32 = FakeESP32()
    bridge = TrackingBridge(
        esp32,
        config=TrackingConfig(
            smoothing=1.0,
            move_threshold=0.0,
            deadzone=0.0,
        ),
    )

    await bridge.handle_detection(
        {"x": 0.5, "y": 0.5, "confidence": 0.9, "timestamp": 100.0}
    )
    await pump_resampler(bridge)
    esp32.calls.clear()

    await bridge.handle_detection(
        {"x": 1.0, "y": 0.5, "confidence": 0.9, "timestamp": 100.1}
    )
    await pump_resampler(bridge, fraction=0.5)
    await pump_resampler(bridge, fraction=1.0)

    mid_yaw = esp32.calls[0][1]["yaw"]
    end_yaw = esp32.calls[-1][1]["yaw"]
    assert 0 < mid_yaw < end_yaw
    assert 55 <= end_yaw <= 60
    await bridge.stop()


def test_last_position_is_none_before_any_detection():
    """Snap-on-wake distinguishes 'never seen' from 'centred'."""
    bridge = TrackingBridge(FakeESP32())
    assert bridge.last_position is None


@pytest.mark.asyncio
async def test_last_position_returns_cached_face_after_detection():
    esp32 = FakeESP32()
    bridge = TrackingBridge(esp32, config=TrackingConfig(smoothing=1.0, move_threshold=0.0))
    await bridge.handle_detection({"x": 0.2, "y": 0.6, "confidence": 0.9})
    pos = bridge.last_position
    assert pos is not None
    yaw, pitch = pos
    # x=0.2 is left-of-centre → yaw goes negative under the corrected polarity.
    assert yaw < 0
    assert 0 <= pitch <= 45


@pytest.mark.asyncio
async def test_snap_to_last_position_falls_back_to_centre_without_detection():
    """No face cached -> move_head(0,0)."""
    esp32 = FakeESP32()
    bridge = TrackingBridge(esp32)
    moved = await bridge.snap_to_last_position(speed=80)
    assert moved is True
    assert esp32.calls == [
        ("self.robot.set_head_angles", {"yaw": 0, "pitch": 0, "speed": 100}),
    ]


@pytest.mark.asyncio
async def test_snap_to_last_position_uses_cached_face():
    """After at least one accepted detection, snap reuses the cached pose."""
    esp32 = FakeESP32()
    bridge = TrackingBridge(esp32, config=TrackingConfig(smoothing=1.0, move_threshold=0.0))
    await bridge.handle_detection({"x": 0.8, "y": 0.4, "confidence": 0.9})
    esp32.calls.clear()
    moved = await bridge.snap_to_last_position()
    assert moved is True
    assert len(esp32.calls) == 1
    name, args = esp32.calls[0]
    assert name == "self.robot.set_head_angles"
    assert args["yaw"] != 0  # not centre — used the cached position
    assert args["speed"] == 250


@pytest.mark.asyncio
async def test_snap_to_last_position_skips_when_device_offline():
    esp32 = FakeESP32()
    esp32.device_connected = False
    bridge = TrackingBridge(esp32)
    moved = await bridge.snap_to_last_position()
    assert moved is False
    assert esp32.calls == []


# --- USB transport path ------------------------------------------------------


class FakeUsbTransport:
    """In-memory stand-in for UsbTransport. Mirrors the .connected /
    .call_tool surface the TrackingBridge depends on."""

    def __init__(self, connected: bool = True, raises: Exception | None = None) -> None:
        self.connected = connected
        self.calls: list[tuple[str, dict, float | None]] = []
        self._raises = raises

    async def call_tool(self, name, arguments, *, timeout_s=None):
        self.calls.append((name, arguments, timeout_s))
        if self._raises is not None:
            raise self._raises
        return {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}


@pytest.mark.asyncio
async def test_handle_detection_prefers_usb_when_connected():
    """Face-tracking writes go through USB, not WebSocket, when both are up."""
    esp32 = FakeESP32()
    usb = FakeUsbTransport(connected=True)
    bridge = TrackingBridge(
        esp32,
        config=TrackingConfig(smoothing=1.0, move_threshold=0.0),
        usb_transport=usb,
    )

    moved = await bridge.handle_detection({"x": 0.7, "y": 0.4, "confidence": 0.9})
    await pump_resampler(bridge)

    assert moved is True
    assert len(usb.calls) == 1
    name, args, timeout = usb.calls[0]
    assert name == "self.robot.set_head_angles"
    assert "yaw" in args and "pitch" in args
    assert timeout == TrackingBridge.USB_SERVO_TIMEOUT_S
    # WebSocket path stays untouched while USB is healthy.
    assert esp32.calls == []
    await bridge.stop()


@pytest.mark.asyncio
async def test_handle_detection_falls_back_to_ws_when_usb_disconnected():
    """USB down → use the existing esp32.call_tool path so tracking still works."""
    esp32 = FakeESP32()
    usb = FakeUsbTransport(connected=False)
    bridge = TrackingBridge(
        esp32,
        config=TrackingConfig(smoothing=1.0, move_threshold=0.0),
        usb_transport=usb,
    )

    moved = await bridge.handle_detection({"x": 0.7, "y": 0.4, "confidence": 0.9})
    await pump_resampler(bridge)

    assert moved is True
    assert usb.calls == []
    assert len(esp32.calls) == 1
    await bridge.stop()


@pytest.mark.asyncio
async def test_tracking_auto_head_command_does_not_look_like_manual_move():
    esp32 = FakeESP32WithNotifyFlags()
    auto_events = 0

    def on_auto_head_command():
        nonlocal auto_events
        auto_events += 1

    bridge = TrackingBridge(
        esp32,
        config=TrackingConfig(smoothing=1.0, move_threshold=0.0),
        usb_transport=None,
        on_auto_head_command=on_auto_head_command,
    )

    moved = await bridge.handle_detection({"x": 0.7, "y": 0.4, "confidence": 0.9})
    await pump_resampler(bridge)

    assert moved is True
    assert auto_events == 1
    assert esp32.calls[0][2] == {
        "notify_activity": False,
        "notify_head_command": False,
    }
    await bridge.stop()


@pytest.mark.asyncio
async def test_handle_detection_falls_back_to_ws_when_usb_raises():
    """USB times out / cable yanked mid-write → degrade to WS, don't drop the frame."""
    esp32 = FakeESP32()
    usb = FakeUsbTransport(connected=True, raises=RuntimeError("usb gone"))
    bridge = TrackingBridge(
        esp32,
        config=TrackingConfig(smoothing=1.0, move_threshold=0.0),
        usb_transport=usb,
    )

    moved = await bridge.handle_detection({"x": 0.7, "y": 0.4, "confidence": 0.9})
    await pump_resampler(bridge)

    assert moved is True
    assert len(usb.calls) == 1  # we attempted USB
    assert len(esp32.calls) == 1  # then fell back to WS
    await bridge.stop()


@pytest.mark.asyncio
async def test_snap_to_last_position_uses_usb_when_connected():
    esp32 = FakeESP32()
    usb = FakeUsbTransport(connected=True)
    bridge = TrackingBridge(esp32, usb_transport=usb)
    moved = await bridge.snap_to_last_position(speed=80)
    assert moved is True
    assert len(usb.calls) == 1
    assert esp32.calls == []


# --- Control is available when either USB or WS is up ---


@pytest.mark.asyncio
async def test_handle_detection_works_when_ws_down_but_usb_up():
    """WiFi flap should not kill face tracking while USB is healthy."""
    esp32 = FakeESP32()
    esp32.device_connected = False  # WS is down
    usb = FakeUsbTransport(connected=True)
    bridge = TrackingBridge(
        esp32,
        config=TrackingConfig(smoothing=1.0, move_threshold=0.0),
        usb_transport=usb,
    )

    moved = await bridge.handle_detection({"x": 0.7, "y": 0.4, "confidence": 0.9})
    await pump_resampler(bridge)

    assert moved is True
    assert len(usb.calls) == 1
    assert esp32.calls == []
    await bridge.stop()


@pytest.mark.asyncio
async def test_handle_detection_skips_when_both_channels_down():
    esp32 = FakeESP32()
    esp32.device_connected = False
    usb = FakeUsbTransport(connected=False)
    bridge = TrackingBridge(esp32, usb_transport=usb)
    moved = await bridge.handle_detection({"x": 0.7, "y": 0.4, "confidence": 0.9})
    assert moved is False
    assert usb.calls == []
    assert esp32.calls == []


@pytest.mark.asyncio
async def test_snap_to_last_position_works_when_ws_down_but_usb_up():
    esp32 = FakeESP32()
    esp32.device_connected = False
    usb = FakeUsbTransport(connected=True)
    bridge = TrackingBridge(esp32, usb_transport=usb)
    moved = await bridge.snap_to_last_position()
    assert moved is True
    assert len(usb.calls) == 1


# --- Pitch swings separately above and below neutral --------------------------


def _build_bridge_for_pitch() -> TrackingBridge:
    return TrackingBridge(
        FakeESP32(),
        config=TrackingConfig(smoothing=1.0, move_threshold=0.0, deadzone=0.0),
    )


def test_map_to_head_pitch_neutral_when_face_centred():
    bridge = _build_bridge_for_pitch()
    _, pitch = bridge._map_to_head(0.5, 0.5)
    assert pitch == 15  # neutral_pitch default


def test_map_to_head_pitch_hits_min_when_face_at_top():
    bridge = _build_bridge_for_pitch()
    _, pitch = bridge._map_to_head(0.5, 0.0)
    assert pitch == 0  # min_pitch — no early saturation on the upward swing


def test_map_to_head_pitch_hits_max_only_at_bottom():
    bridge = _build_bridge_for_pitch()
    _, pitch = bridge._map_to_head(0.5, 1.0)
    assert pitch == 45  # max_pitch — full downward swing


def test_map_to_head_pitch_does_not_saturate_at_two_thirds():
    """Regression: using the full range on both sides made y≈0.66 max out already."""
    bridge = _build_bridge_for_pitch()
    _, pitch = bridge._map_to_head(0.5, 0.66)
    # neutral 15 + dy=0.16 * 2.0 * 30 swing ≈ 24.6 → well under 45.
    assert 20 <= pitch < 35


# --- USB ↔ WS handoff scenarios ------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_usb_online_uses_usb():
    """Scenario 1: USB online → use USB."""
    esp32 = FakeESP32()
    usb = FakeUsbTransport(connected=True)
    bridge = TrackingBridge(
        esp32,
        config=TrackingConfig(smoothing=1.0, move_threshold=0.0),
        usb_transport=usb,
    )
    await bridge.handle_detection({"x": 0.7, "y": 0.4, "confidence": 0.9})
    await pump_resampler(bridge)
    assert len(usb.calls) == 1
    assert esp32.calls == []
    await bridge.stop()


@pytest.mark.asyncio
async def test_scenario_usb_disconnect_falls_back_to_ws():
    """Scenario 2: USB unplugged → fall back to WS automatically, nothing breaks."""
    esp32 = FakeESP32()
    usb = FakeUsbTransport(connected=False)  # unplugged
    bridge = TrackingBridge(
        esp32,
        config=TrackingConfig(smoothing=1.0, move_threshold=0.0),
        usb_transport=usb,
    )
    moved = await bridge.handle_detection({"x": 0.7, "y": 0.4, "confidence": 0.9})
    await pump_resampler(bridge)
    assert moved is True
    assert usb.calls == []  # USB not tried (connected=False skips it)
    assert len(esp32.calls) == 1
    await bridge.stop()


@pytest.mark.asyncio
async def test_scenario_usb_reconnect_switches_back_to_usb():
    """Scenario 3: USB plugged back in → switch back to USB automatically."""
    esp32 = FakeESP32()
    usb = FakeUsbTransport(connected=False)
    bridge = TrackingBridge(
        esp32,
        config=TrackingConfig(smoothing=1.0, move_threshold=0.0),
        usb_transport=usb,
    )
    # WS while unplugged
    await bridge.handle_detection({"x": 0.3, "y": 0.4, "confidence": 0.9})
    await pump_resampler(bridge)
    assert len(esp32.calls) == 1 and usb.calls == []
    # USB reconnects
    usb.connected = True
    esp32.calls.clear()
    await bridge.handle_detection({"x": 0.7, "y": 0.6, "confidence": 0.9})
    await pump_resampler(bridge)
    assert len(usb.calls) == 1
    assert esp32.calls == []  # back on USB
    await bridge.stop()


@pytest.mark.asyncio
async def test_scenario_no_usb_transport_pure_ws():
    """Scenario 4: no USB at all (STACKCHAN_USB_DISABLE=1) → WS only."""
    esp32 = FakeESP32()
    bridge = TrackingBridge(
        esp32,
        config=TrackingConfig(smoothing=1.0, move_threshold=0.0),
        usb_transport=None,  # no transport created
    )
    moved = await bridge.handle_detection({"x": 0.7, "y": 0.4, "confidence": 0.9})
    await pump_resampler(bridge)
    assert moved is True
    assert len(esp32.calls) == 1
    # snap uses WS too
    moved = await bridge.snap_to_last_position()
    assert moved is True
    assert len(esp32.calls) == 2
    await bridge.stop()


class FakeVoiceOnlyESP32:
    """Firmware without MCP: set_head_angles is rejected, WS ``head`` works."""

    device_connected = True
    mcp_supported = False

    def __init__(self):
        self.calls = []
        self.head_calls = []

    async def call_tool(self, name, arguments, **kwargs):
        self.calls.append((name, arguments))
        return None, {"code": -32000, "message": "ESP32 MCP unsupported (features.mcp=false)"}

    async def send_head(self, yaw, pitch, speed, **kwargs):
        self.head_calls.append(((yaw, pitch, speed), kwargs))
        return {"ok": True}, None


@pytest.mark.asyncio
async def test_voice_only_device_tracks_via_ws_head_message():
    esp32 = FakeVoiceOnlyESP32()
    bridge = TrackingBridge(
        esp32,
        config=TrackingConfig(smoothing=1.0, move_threshold=0.0),
        usb_transport=None,
    )

    moved = await bridge.handle_detection({"x": 0.7, "y": 0.4, "confidence": 0.9})
    await pump_resampler(bridge)

    assert moved is True
    assert esp32.calls == []
    assert len(esp32.head_calls) == 1
    (_yaw, _pitch, speed), kwargs = esp32.head_calls[0]
    assert speed == TrackingBridge.TRACKING_SPEED
    assert kwargs == {"notify_activity": False, "notify_head_command": False}
    await bridge.stop()
