"""Contract tests for the Gemini voice backend.

The proxy must be a duck-type drop-in for CloudProxy, so the tests pin
the externally-visible surface (``connected`` / ``start`` / ``stop`` /
``send_device_json`` / ``send_device_binary`` / ``speak_text`` /
``try_handle_device_mcp``) rather than internal plumbing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from stackchan_mcp.cloud_proxy import CloudConnectionInfo
from stackchan_mcp.device_emotion import (
    DEAD_LED_RGB,
    LISTENING_EMOTION,
    WAKE_CONFIRM_EMOTION,
)
from stackchan_mcp.gemini_live_bridge import GeminiLiveBridge
from stackchan_mcp.gemini_voice_proxy import GEMINI_HELLO_MARKER, GeminiVoiceProxy
from stackchan_mcp.opus_codec import OUTPUT_FRAME_BYTES
from stackchan_mcp.wake_gate import (
    LISTENING_LED_RGB,
    WakeGate,
    WakeGateResult,
    WakeGateState,
    create_wake_gate_from_env,
)


# --- Test doubles -------------------------------------------------------------


class FakeESP32:
    device_connected = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"content": []}, None


class FakeVoiceOnlyESP32(FakeESP32):
    class Connection:
        mcp_supported = False

    device_connected = True
    connection = Connection()

    def __init__(self) -> None:
        super().__init__()
        self.emotions: list[str] = []
        self.leds: list[tuple[int, int, int]] = []

    async def send_emotion(self, emotion: str, *, notify_activity: bool = True):
        self.emotions.append(emotion)
        return {"ok": True, "emotion": emotion}, None

    async def send_led(
        self,
        r: int,
        g: int,
        b: int,
        *,
        notify_activity: bool = True,
    ):
        self.leds.append((r, g, b))
        return {"ok": True, "r": r, "g": g, "b": b}, None


class LockCheckingESP32(FakeESP32):
    def __init__(self) -> None:
        super().__init__()
        self.send_lock: asyncio.Lock | None = None
        self.locked_at_call: list[bool] = []

    async def call_tool(self, name, arguments):
        assert self.send_lock is not None
        self.locked_at_call.append(self.send_lock.locked())
        await asyncio.sleep(0)
        return await super().call_tool(name, arguments)


class FakeBridge:
    """Minimal stand-in for GeminiLiveBridge.

    Records what the proxy hands it. Lets tests fire ``on_audio`` and
    ``on_turn_complete`` callbacks deterministically.
    """

    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        self.on_audio = kwargs.get("on_audio")
        self.on_text = kwargs.get("on_text")
        self.on_turn_complete = kwargs.get("on_turn_complete")
        self._running = False
        self._connected = False
        self.audio_sent: list[bytes] = []
        self.stream_end_sent = False
        self.activity_start_sent = False
        self.activity_end_sent = False
        self.context_cleared = False
        self.realtime_text_calls: list[str] = []
        self.current_turn_emotion_face: str | None = None
        self.clear_turn_emotion_face_called = False
        # GeminiLiveBridge exposes ``_session`` as the live session handle.
        # speak_text uses ``getattr(self._bridge, "_session", None)``.
        self._session = self  # session methods are routed back here

    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> None:
        self._running = True
        self._connected = True

    async def stop(self) -> None:
        self._running = False
        self._connected = False

    async def wait_connected(self, timeout: float = 10.0) -> bool:
        return self._connected

    async def send_audio(self, pcm: bytes) -> None:
        self.audio_sent.append(pcm)

    async def send_audio_stream_end(self) -> None:
        self.stream_end_sent = True

    async def send_activity_start(self) -> None:
        self.activity_start_sent = True

    async def send_activity_end(self) -> None:
        self.activity_end_sent = True

    async def clear_conversation_context(self) -> None:
        self.context_cleared = True

    # 3.1 Flash Live wants send_realtime_input(text=...) for live prompts.
    async def send_realtime_input(self, **kwargs):
        text = kwargs.get("text")
        if text is not None:
            self.realtime_text_calls.append(text)
        if kwargs.get("audio_stream_end"):
            self.stream_end_sent = True
        audio = kwargs.get("audio")
        if audio is not None:
            self.audio_sent.append(getattr(audio, "data", audio))

    def clear_turn_emotion_face(self) -> None:
        self.clear_turn_emotion_face_called = True
        self.current_turn_emotion_face = None


class FakeCodec:
    """Captures encode/decode without depending on libopus."""

    def __init__(self) -> None:
        self.decoded: list[bytes] = []
        self.encoded: list[bytes] = []
        self.flushed = False
        self.reset_called = False
        self._encode_buffer = bytearray()

    def decode_device_frame(self, opus: bytes) -> bytes:
        self.decoded.append(opus)
        return b"PCM" * 4

    def encode_pcm_24k(self, pcm: bytes) -> list[bytes]:
        self.encoded.append(pcm)
        # Emit one opus frame per call to keep the proxy's branching busy.
        return [b"OPUS-FRAME"]

    def flush(self) -> list[bytes]:
        self.flushed = True
        return [b"OPUS-TAIL"]

    def reset_encode_buffer(self) -> None:
        self.reset_called = True
        self._encode_buffer.clear()


class FakeUSBTransport:
    """Records LED / tool calls placed via the USB Serial/JTAG path."""

    def __init__(self, *, connected: bool = True, raise_exc: Exception | None = None) -> None:
        self.connected = connected
        self.calls: list[tuple[str, dict]] = []
        self._raise = raise_exc

    async def call_tool(self, name: str, args: dict) -> dict:
        if self._raise is not None:
            raise self._raise
        self.calls.append((name, args))
        return {"result": "ok"}


class FakeEdgeTTSProvider:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.spoken: list[str] = []
        self.aborted = False
        self.speaking = False

    async def speak_text(self, text: str) -> dict[str, Any]:
        self.spoken.append(text)
        return {"ok": True, "frames_sent": 1, "voice": "fake"}

    async def abort(self) -> None:
        self.aborted = True
        self.speaking = False


class ScriptedKeywordSpotter:
    available = True

    def __init__(self, hits: list[bool]) -> None:
        self.hits = list(hits)
        self.reset_count = 0

    def detect(self, _pcm_16khz: bytes) -> bool:
        return self.hits.pop(0) if self.hits else False

    def reset(self) -> None:
        self.reset_count += 1


@pytest.fixture
def info():
    return CloudConnectionInfo(
        cloud_url="wss://example.invalid/ignored",
        authorization="",
        protocol_version="1",
        device_id="dev-1",
        client_id="cli-1",
    )


def _make_proxy(
    *,
    monkeypatch=None,
    esp32: Any | None = None,
    bridge_factory=FakeBridge,
    codec_factory=FakeCodec,
    debug_status=None,
) -> tuple[GeminiVoiceProxy, FakeESP32, list]:
    if monkeypatch is not None:
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("STACKCHAN_WAKE_WORD", "0")
    esp32 = esp32 or FakeESP32()
    sent: list = []

    async def send(msg):
        sent.append(msg)

    proxy = GeminiVoiceProxy(
        esp32_ref=lambda: esp32,
        bridge_factory=bridge_factory,
        codec_factory=codec_factory,
        edge_tts_provider_factory=FakeEdgeTTSProvider,
        device_tts_start_delay_s=0.0,
        device_frame_interval_s=0.0,
        device_prebuffer_ms=0.0,
        device_frame_pace_ms=0.0,
        wake_confirm_hold_s=0.0,
        debug_status=debug_status,
    )
    return proxy, esp32, sent


# --- start / stop / connected ------------------------------------------------


@pytest.mark.asyncio
async def test_start_without_api_key_returns_false_and_stays_offline(monkeypatch, info):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    proxy, _, _sent = _make_proxy()

    async def send(_): pass

    ok = await proxy.start(info, {"type": "hello"}, send)
    assert ok is False
    assert proxy.connected is False


@pytest.mark.asyncio
async def test_start_with_api_key_brings_bridge_up_and_marks_server_hello(monkeypatch, info):
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch)

    async def send(msg):
        sent.append(msg)

    ok = await proxy.start(info, {"type": "hello"}, send)
    assert ok is True
    assert proxy.connected is True
    # server_hello marker lets debug logs identify the backend.
    assert proxy.server_hello is not None
    assert proxy.server_hello.get("provider") == GEMINI_HELLO_MARKER["provider"]


@pytest.mark.asyncio
async def test_stop_tears_down_bridge_and_marks_disconnected(monkeypatch, info):
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch)

    async def send(msg):
        sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    await proxy.stop()
    assert proxy.connected is False


# --- device → gemini ---------------------------------------------------------


@pytest.mark.asyncio
async def test_send_device_binary_forwards_decoded_pcm_to_bridge(monkeypatch, info):
    proxy, _, _sent = _make_proxy(monkeypatch=monkeypatch)
    bridge_kwargs = {}

    def capture_bridge(*args, **kwargs):
        bridge_kwargs.update(kwargs)
        return FakeBridge(*args, **kwargs)

    proxy.bridge_factory = capture_bridge
    tracking_calls = []

    async def switch_tracking(enabled):
        tracking_calls.append(enabled)
        return {"ok": True, "enabled": enabled}

    proxy.set_face_tracking = switch_tracking

    async def send(_msg): pass

    await proxy.start(info, {"type": "hello"}, send)
    assert await bridge_kwargs["set_face_tracking"](True) == {"ok": True, "enabled": True}
    assert tracking_calls == [True]
    bridge = proxy._bridge  # FakeBridge
    forwarded = await proxy.send_device_binary(b"\x00" * 100)
    assert forwarded is True
    # One Opus frame in → one PCM payload pushed to Gemini.
    assert len(bridge.audio_sent) == 1


@pytest.mark.asyncio
async def test_wake_gate_blocks_audio_until_keyword_then_replays_preroll(
    monkeypatch,
    info,
):
    proxy, esp32, sent = _make_proxy(monkeypatch=monkeypatch)
    gate = WakeGate(
        kws=ScriptedKeywordSpotter([False, True]),
        activity_rms_threshold=0.0,
    )
    proxy.wake_gate_factory = lambda: gate

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    bridge = proxy._bridge

    assert await proxy.send_device_binary(b"frame-1") is True
    assert bridge.audio_sent == []

    assert await proxy.send_device_binary(b"frame-2") is True
    assert bridge.audio_sent == [b"PCM" * 4, b"PCM" * 4]
    assert gate.state == WakeGateState.LISTENING
    led_calls = [c for c in esp32.calls if c[0] == "self.led.set_all"]
    assert led_calls[-1][1] == {"r": 0, "g": 180, "b": 180}


@pytest.mark.asyncio
async def test_wake_gate_uses_emotion_channel_when_ws_has_no_mcp(
    monkeypatch,
    info,
):
    esp32 = FakeVoiceOnlyESP32()
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch, esp32=esp32)
    gate = WakeGate(
        kws=ScriptedKeywordSpotter([True]),
        activity_rms_threshold=0.0,
    )
    proxy.wake_gate_factory = lambda: gate

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    esp32.calls.clear()

    assert await proxy.send_device_binary(b"wake") is True

    assert esp32.calls == []
    assert esp32.emotions == [WAKE_CONFIRM_EMOTION, LISTENING_EMOTION]
    assert LISTENING_EMOTION != "thinking"
    assert "thinking" not in esp32.emotions
    assert esp32.leds == [LISTENING_LED_RGB]


@pytest.mark.asyncio
async def test_wake_gate_prefers_direct_led_when_mcp_state_unknown(
    monkeypatch,
    info,
):
    class DirectLedUnknownMcpESP32:
        device_connected = True
        connection = None

        def __init__(self) -> None:
            self.leds: list[tuple[int, int, int]] = []
            self.emotions: list[str] = []

        async def call_tool(self, name, arguments):
            raise AssertionError(f"unexpected MCP LED call: {name} {arguments}")

        async def send_emotion(self, emotion: str, *, notify_activity: bool = True):
            self.emotions.append(emotion)
            return {"ok": True, "emotion": emotion}, None

        async def send_led(
            self,
            r: int,
            g: int,
            b: int,
            *,
            notify_activity: bool = True,
        ):
            self.leds.append((r, g, b))
            return {"ok": True, "r": r, "g": g, "b": b}, None

    esp32 = DirectLedUnknownMcpESP32()
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch, esp32=esp32)
    gate = WakeGate(
        kws=ScriptedKeywordSpotter([True]),
        activity_rms_threshold=0.0,
    )
    proxy.wake_gate_factory = lambda: gate

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)

    assert await proxy.send_device_binary(b"wake") is True

    assert esp32.leds == [LISTENING_LED_RGB]
    assert esp32.emotions == [WAKE_CONFIRM_EMOTION, LISTENING_EMOTION]


@pytest.mark.asyncio
async def test_set_leds_safe_logs_when_esp32_ref_missing(caplog):
    proxy = GeminiVoiceProxy(esp32_ref=None)

    with caplog.at_level(logging.INFO, logger="stackchan_mcp.gemini_voice_proxy"):
        await proxy._set_leds_safe(*LISTENING_LED_RGB)

    assert "reason=esp32_ref_missing" in caplog.text


@pytest.mark.asyncio
async def test_wake_gate_led_failure_on_ws_only_does_not_break_audio(
    monkeypatch,
    info,
):
    class FailingLedESP32(FakeVoiceOnlyESP32):
        async def send_led(
            self,
            r: int,
            g: int,
            b: int,
            *,
            notify_activity: bool = True,
        ):
            self.leds.append((r, g, b))
            return None, {"code": -32000, "message": "led failed"}

    esp32 = FailingLedESP32()
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch, esp32=esp32)
    gate = WakeGate(
        kws=ScriptedKeywordSpotter([True]),
        activity_rms_threshold=0.0,
    )
    proxy.wake_gate_factory = lambda: gate

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)

    assert await proxy.send_device_binary(b"wake") is True
    assert gate.state == WakeGateState.LISTENING
    assert esp32.emotions == [WAKE_CONFIRM_EMOTION, LISTENING_EMOTION]
    assert esp32.leds == [LISTENING_LED_RGB]


def _tts_states(sent: list) -> list[str]:
    states: list[str] = []
    for message in sent:
        if not isinstance(message, str):
            continue
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            continue
        if payload.get("type") == "tts" and "state" in payload:
            states.append(payload["state"])
    return states


@pytest.mark.asyncio
async def test_wake_sends_chime_tts_envelope_and_opus_frames(monkeypatch, info):
    proxy, _esp32, sent = _make_proxy(monkeypatch=monkeypatch)
    gate = WakeGate(
        kws=ScriptedKeywordSpotter([True]),
        activity_rms_threshold=0.0,
    )
    proxy.wake_gate_factory = lambda: gate

    async def send(msg):
        sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    sent.clear()
    assert await proxy.send_device_binary(b"wake") is True
    if proxy._tts_tasks:
        await asyncio.gather(*list(proxy._tts_tasks), return_exceptions=True)

    assert _tts_states(sent) == ["start", "sentence_start", "stop"]
    assert any(isinstance(message, (bytes, bytearray)) for message in sent)
    assert not any(
        isinstance(message, str)
        and json.loads(message).get("emotion") == "thinking"
        for message in sent
        if isinstance(message, str) and message.startswith("{")
    )


@pytest.mark.asyncio
async def test_wake_gate_idle_close_stops_forwarding_and_extinguishes_led(
    monkeypatch,
    info,
):
    now = 0.0

    def clock() -> float:
        return now

    proxy, esp32, sent = _make_proxy(monkeypatch=monkeypatch)
    gate = WakeGate(
        kws=ScriptedKeywordSpotter([True, False]),
        idle_s=1.0,
        activity_rms_threshold=1000.0,
        clock=clock,
    )
    proxy.wake_gate_factory = lambda: gate

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    bridge = proxy._bridge
    await proxy.send_device_binary(b"wake")
    bridge.audio_sent.clear()
    esp32.calls.clear()

    now = 1.1
    assert await proxy.send_device_binary(b"noise") is True

    assert bridge.audio_sent == []
    assert bridge.context_cleared is True
    led_calls = [c for c in esp32.calls if c[0] == "self.led.set_all"]
    assert led_calls[-1][1] == {"r": 0, "g": 0, "b": 0}
    assert gate.state == WakeGateState.DORMANT


@pytest.mark.asyncio
async def test_session_dead_closes_listening_and_lights_red(monkeypatch, info):
    esp32 = FakeVoiceOnlyESP32()
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch, esp32=esp32)
    gate = WakeGate(
        kws=ScriptedKeywordSpotter([True]),
        activity_rms_threshold=0.0,
    )
    proxy.wake_gate_factory = lambda: gate

    async def send(msg):
        sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    await proxy.send_device_binary(b"wake")
    assert gate.state == WakeGateState.LISTENING

    await proxy._handle_session_dead()
    assert gate.state == WakeGateState.DORMANT
    assert esp32.leds[-1] == DEAD_LED_RGB


class _RecordingLiveSession:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send_realtime_input(self, **kwargs):
        self.calls.append(kwargs)


def _audio_payloads(session: _RecordingLiveSession) -> list[bytes]:
    out: list[bytes] = []
    for call in session.calls:
        audio = call.get("audio")
        if audio is None:
            continue
        out.append(getattr(audio, "data", audio))
    return out


@pytest.mark.asyncio
async def test_device_audio_is_cached_when_live_session_missing(monkeypatch, info):
    """Without a Live session, device binary audio must be buffered, not dropped at the proxy entry."""
    proxy, esp32, sent = _make_proxy(monkeypatch=monkeypatch)
    gate = WakeGate(
        kws=ScriptedKeywordSpotter([True, False]),
        activity_rms_threshold=0.0,
    )
    proxy.wake_gate_factory = lambda: gate

    async def send(msg):
        sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    assert await proxy.send_device_binary(b"wake") is True

    live = GeminiLiveBridge(esp32, api_key="k", reconnect_audio_ttl_s=5.0)
    live._session = None
    proxy._bridge = live

    assert proxy.connected is False
    assert await proxy.send_device_binary(b"held") is True
    assert list(live._pending_audio) == [b"PCM" * 4]

    session = _RecordingLiveSession()
    live._session = session
    await live._flush_reconnect_audio()
    assert _audio_payloads(session) == [b"PCM" * 4]


@pytest.mark.asyncio
async def test_device_audio_cache_timeout_closes_listen_and_lights_red(
    monkeypatch, info
):
    esp32 = FakeVoiceOnlyESP32()
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch, esp32=esp32)
    gate = WakeGate(
        kws=ScriptedKeywordSpotter([True, False]),
        activity_rms_threshold=0.0,
    )
    proxy.wake_gate_factory = lambda: gate

    async def send(msg):
        sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    await proxy.send_device_binary(b"wake")
    assert gate.state == WakeGateState.LISTENING

    live = GeminiLiveBridge(
        esp32,
        api_key="k",
        on_session_dead=proxy._handle_session_dead,
        reconnect_audio_ttl_s=0.05,
    )
    live._session = None
    proxy._bridge = live

    assert await proxy.send_device_binary(b"held") is True
    await asyncio.sleep(0.12)

    assert list(live._pending_audio) == []
    assert gate.state == WakeGateState.DORMANT
    assert esp32.leds[-1] == DEAD_LED_RGB


class _AbortBoomEdge(FakeEdgeTTSProvider):
    async def abort(self) -> None:
        raise RuntimeError("abort failed")


@pytest.mark.asyncio
async def test_session_dead_lights_red_when_abort_raises(monkeypatch, info, caplog):
    esp32 = FakeVoiceOnlyESP32()
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch, esp32=esp32)
    gate = WakeGate(
        kws=ScriptedKeywordSpotter([True, False]),
        activity_rms_threshold=0.0,
    )
    proxy.wake_gate_factory = lambda: gate

    async def send(msg):
        sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    await proxy.send_device_binary(b"wake")
    if proxy._tts_tasks:
        await asyncio.gather(*list(proxy._tts_tasks), return_exceptions=True)
    esp32.leds.clear()
    proxy._edge_tts = _AbortBoomEdge()

    live = GeminiLiveBridge(
        esp32,
        api_key="k",
        on_session_dead=proxy._handle_session_dead,
        reconnect_audio_ttl_s=0.05,
    )
    live._session = None
    proxy._bridge = live

    with caplog.at_level(logging.ERROR):
        assert await proxy.send_device_binary(b"held") is True
        await asyncio.sleep(0.12)

    assert esp32.leds == [DEAD_LED_RGB]
    assert "abort failed" in caplog.text


@pytest.mark.asyncio
async def test_wake_confirm_expression_dwells_before_listening(monkeypatch, info):
    esp32 = FakeVoiceOnlyESP32()
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch, esp32=esp32)
    proxy.wake_confirm_hold_s = 0.05
    gate = WakeGate(
        kws=ScriptedKeywordSpotter([True]),
        activity_rms_threshold=0.0,
    )
    proxy.wake_gate_factory = lambda: gate

    async def send(msg):
        sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await proxy.send_device_binary(b"wake") is True
    elapsed = loop.time() - started

    assert esp32.emotions == [WAKE_CONFIRM_EMOTION, LISTENING_EMOTION]
    assert elapsed >= 0.05


@pytest.mark.asyncio
async def test_wake_chime_waits_for_tts_start_delay_before_first_frame(
    monkeypatch, info
):
    proxy, _esp32, sent = _make_proxy(monkeypatch=monkeypatch)
    proxy.device_tts_start_delay_s = 0.05
    gate = WakeGate(
        kws=ScriptedKeywordSpotter([True]),
        activity_rms_threshold=0.0,
    )
    proxy.wake_gate_factory = lambda: gate
    stamps: list[tuple[float, object]] = []

    async def send(msg):
        stamps.append((asyncio.get_running_loop().time(), msg))
        sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    sent.clear()
    stamps.clear()
    assert await proxy.send_device_binary(b"wake") is True
    if proxy._tts_tasks:
        await asyncio.gather(*list(proxy._tts_tasks), return_exceptions=True)

    start_at = next(
        t
        for t, message in stamps
        if isinstance(message, str) and json.loads(message).get("state") == "start"
    )
    first_frame_at = next(
        t for t, message in stamps if isinstance(message, (bytes, bytearray))
    )
    assert first_frame_at - start_at >= 0.05


@pytest.mark.asyncio
async def test_wake_chime_keeps_start_delay_with_default_prebuffer(monkeypatch, info):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("STACKCHAN_WAKE_WORD", "0")
    esp32 = FakeVoiceOnlyESP32()
    stamps: list[tuple[float, object]] = []
    sent: list = []

    async def send(msg):
        stamps.append((asyncio.get_running_loop().time(), msg))
        sent.append(msg)

    proxy = GeminiVoiceProxy(
        esp32_ref=lambda: esp32,
        bridge_factory=FakeBridge,
        codec_factory=FakeCodec,
        edge_tts_provider_factory=FakeEdgeTTSProvider,
        device_frame_interval_s=0.0,
        device_frame_pace_ms=0.0,
        wake_confirm_hold_s=0.0,
    )
    assert proxy.device_prebuffer_ms == 200.0
    assert proxy.device_tts_start_delay_s == 0.08
    gate = WakeGate(
        kws=ScriptedKeywordSpotter([True]),
        activity_rms_threshold=0.0,
    )
    proxy.wake_gate_factory = lambda: gate

    await proxy.start(info, {"type": "hello"}, send)
    sent.clear()
    stamps.clear()
    assert await proxy.send_device_binary(b"wake") is True
    if proxy._tts_tasks:
        await asyncio.gather(*list(proxy._tts_tasks), return_exceptions=True)

    start_at = next(
        t
        for t, message in stamps
        if isinstance(message, str) and json.loads(message).get("state") == "start"
    )
    first_frame_at = next(
        t for t, message in stamps if isinstance(message, (bytes, bytearray))
    )
    assert first_frame_at - start_at >= proxy.device_tts_start_delay_s


@pytest.mark.asyncio
async def test_wake_gate_idle_close_uses_direct_led_when_ws_has_no_mcp(
    monkeypatch,
    info,
):
    now = 0.0

    def clock() -> float:
        return now

    esp32 = FakeVoiceOnlyESP32()
    proxy, _, _sent = _make_proxy(monkeypatch=monkeypatch, esp32=esp32)
    gate = WakeGate(
        kws=ScriptedKeywordSpotter([True, False]),
        idle_s=1.0,
        activity_rms_threshold=1000.0,
        clock=clock,
    )
    proxy.wake_gate_factory = lambda: gate

    async def send(_msg): pass

    await proxy.start(info, {"type": "hello"}, send)
    await proxy.send_device_binary(b"wake")
    esp32.leds.clear()

    now = 1.1
    assert await proxy.send_device_binary(b"noise") is True

    assert esp32.calls == []
    assert esp32.leds == [(0, 0, 0)]
    assert gate.state == WakeGateState.DORMANT


@pytest.mark.asyncio
async def test_bridge_end_conversation_closes_wake_gate_and_extinguishes_led(
    monkeypatch,
    info,
):
    proxy, esp32, sent = _make_proxy(monkeypatch=monkeypatch)
    gate = WakeGate(
        kws=ScriptedKeywordSpotter([True]),
        activity_rms_threshold=0.0,
    )
    proxy.wake_gate_factory = lambda: gate

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    await proxy.send_device_binary(b"wake")
    esp32.calls.clear()

    callback = proxy._bridge.kwargs["on_end_conversation"]
    await callback()

    assert gate.state == WakeGateState.DORMANT
    led_calls = [c for c in esp32.calls if c[0] == "self.led.set_all"]
    assert led_calls[-1][1] == {"r": 0, "g": 0, "b": 0}


@pytest.mark.asyncio
async def test_disabled_wake_gate_is_cached_for_the_session(monkeypatch, info, caplog):
    monkeypatch.setenv("STACKCHAN_WAKE_WORD", "0")
    proxy, _, _sent = _make_proxy(monkeypatch=monkeypatch)
    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        return create_wake_gate_from_env()

    proxy.wake_gate_factory = factory

    async def send(_msg):
        return None

    with caplog.at_level(logging.INFO, logger="stackchan_mcp.wake_gate"):
        await proxy.start(info, {"type": "hello"}, send)
        for _ in range(8):
            assert await proxy.send_device_binary(b"frame") is True
        assert calls == 1
        assert proxy._wake_gate is None
        assert proxy._wake_gate_passthrough is True
        assert proxy._bridge.audio_sent == [b"PCM" * 4] * 8
        assert caplog.text.count("wake word gate disabled") == 1

        await proxy.stop()
        await proxy.start(info, {"type": "hello"}, send)
        assert await proxy.send_device_binary(b"frame") is True
        assert calls == 2
        assert caplog.text.count("wake word gate disabled") == 2


@pytest.mark.asyncio
async def test_wake_word_disabled_env_keeps_existing_pass_through(
    monkeypatch,
    info,
):
    monkeypatch.setenv("STACKCHAN_WAKE_WORD", "0")
    proxy, _, _sent = _make_proxy(monkeypatch=monkeypatch)

    async def send(_msg): pass

    await proxy.start(info, {"type": "hello"}, send)
    bridge = proxy._bridge

    assert proxy._wake_gate is None
    assert await proxy.send_device_binary(b"frame") is True
    assert bridge.audio_sent == [b"PCM" * 4]


@pytest.mark.asyncio
async def test_send_device_binary_drops_when_not_connected():
    proxy, _, _ = _make_proxy()
    assert await proxy.send_device_binary(b"\x00" * 100) is False


@pytest.mark.asyncio
async def test_listen_stop_triggers_audio_stream_end(monkeypatch, info):
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch)

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    await proxy.send_device_json({"type": "listen", "state": "stop"})
    assert proxy._bridge.stream_end_sent is True


@pytest.mark.asyncio
async def test_abort_resets_encode_buffer_and_sends_tts_stop(monkeypatch, info):
    """abort must clear codec AND emit tts.stop so the device leaves
    Speaking state. Without the stop envelope the firmware drops the next
    utterance silently."""
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch)

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    codec = proxy._codec
    proxy._tts_active = True
    sent.clear()
    await proxy.send_device_json({"type": "abort"})
    assert codec.reset_called is True
    assert proxy._tts_active is False
    json_msgs = [m for m in sent if isinstance(m, str)]
    assert any(json.loads(m).get("state") == "stop" for m in json_msgs)


@pytest.mark.asyncio
async def test_abort_when_no_tts_active_does_not_send_stop(monkeypatch, info):
    """Don't emit a spurious tts.stop when nothing was playing."""
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch)

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    proxy._tts_active = False
    sent.clear()
    await proxy.send_device_json({"type": "abort"})
    assert sent == []


# --- gemini → device ---------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_gemini_text_uses_edge_tts_provider(monkeypatch, info):
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch)

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    await proxy._handle_gemini_text("你好呀")
    tasks = list(proxy._tts_tasks)
    assert tasks, "edge-tts should run in a background task"
    await asyncio.gather(*tasks)

    assert proxy._edge_tts.spoken == ["你好呀"]


@pytest.mark.asyncio
async def test_handle_gemini_audio_emits_start_envelope_then_opus_frames(monkeypatch, info):
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch)

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    sent.clear()  # drop any handshake side effects

    # Pretend Gemini just sent 24 kHz PCM.
    await proxy._handle_gemini_audio(b"\x00" * (OUTPUT_FRAME_BYTES))
    # Drain task ships frames at its own cadence; flush before asserting.
    await proxy._frame_queue.join()
    # We must see at minimum: tts.start JSON, sentence_start JSON, one Opus frame.
    json_msgs = [m for m in sent if isinstance(m, str)]
    binary_msgs = [m for m in sent if isinstance(m, (bytes, bytearray))]
    assert any(json.loads(m).get("state") == "start" for m in json_msgs)
    assert any(json.loads(m).get("state") == "sentence_start" for m in json_msgs)
    assert binary_msgs, "device should have received the Opus frames"


@pytest.mark.asyncio
async def test_handle_gemini_audio_waits_after_tts_start_before_first_frame(
    monkeypatch,
    info,
):
    """The ESP32 schedules tts.start on its main task; don't race binary
    Opus frames in before it has entered Speaking state."""
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch)
    proxy.device_tts_start_delay_s = 0.123
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("stackchan_mcp.gemini_voice_proxy.asyncio.sleep", fake_sleep)

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    sent.clear()

    await proxy._handle_gemini_audio(b"\x00" * OUTPUT_FRAME_BYTES)
    # Drain task is paced; join lets it ship the queued frame before we assert.
    await proxy._frame_queue.join()

    assert sleeps == [0.123]
    assert isinstance(sent[0], str)
    assert json.loads(sent[0])["state"] == "start"
    assert any(isinstance(m, (bytes, bytearray)) for m in sent[2:])


@pytest.mark.asyncio
async def test_handle_gemini_audio_skipped_when_tts_started_already(monkeypatch, info):
    """A second audio chunk during the same turn must NOT resend tts.start."""
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch)

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    sent.clear()

    await proxy._handle_gemini_audio(b"\x00" * OUTPUT_FRAME_BYTES)
    await proxy._handle_gemini_audio(b"\x00" * OUTPUT_FRAME_BYTES)
    start_msgs = [
        m for m in sent
        if isinstance(m, str) and json.loads(m).get("state") == "start"
    ]
    assert len(start_msgs) == 1


@pytest.mark.asyncio
async def test_frame_pacing_spaces_drain_sends_evenly(monkeypatch, info):
    """The drain task must pace frames at the 60 ms audio clock so
    the device sees a steady stream regardless of Gemini's burstiness."""
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch)
    proxy.device_frame_pace_ms = 60.0
    # Disable burst-preheat so this test exercises the pure steady-pace
    # contract: with no burst, every frame stays one frame length apart.
    proxy.device_burst_ms = 0.0
    send_times: list[float] = []

    async def send(msg):
        sent.append(msg)
        if isinstance(msg, (bytes, bytearray)):
            send_times.append(asyncio.get_running_loop().time())

    await proxy.start(info, {"type": "hello"}, send)
    sent.clear()

    # Three bursts in a row simulate Gemini dumping a clump of PCM at once.
    for _ in range(3):
        await proxy._handle_gemini_audio(b"\x00" * OUTPUT_FRAME_BYTES)
    await proxy._frame_queue.join()

    binary_msgs = [m for m in sent if isinstance(m, (bytes, bytearray))]
    assert len(binary_msgs) == 3, "every queued frame should still reach the device"
    gaps = [b - a for a, b in zip(send_times, send_times[1:])]
    assert all(gap >= 0.045 for gap in gaps), gaps


@pytest.mark.asyncio
async def test_drain_burst_preheat_sends_first_frames_immediately(monkeypatch, info):
    """Burst-preheat: the first ceil(burst_ms / pace_ms) frames of a turn ship
    back-to-back (skipping the pace gate) to fill the firmware decode queue
    fast, then the drain settles back into the steady frame pace."""
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch)
    proxy.device_frame_pace_ms = 60.0
    proxy.device_burst_ms = 400.0  # ceil(400 / 60) == 7 burst frames
    send_times: list[float] = []

    async def send(msg):
        sent.append(msg)
        if isinstance(msg, (bytes, bytearray)):
            send_times.append(asyncio.get_running_loop().time())

    await proxy.start(info, {"type": "hello"}, send)
    sent.clear()

    # Feed more than the 7-frame burst budget so we can observe the handoff
    # from burst (back-to-back) to steady pace.
    for _ in range(10):
        await proxy._handle_gemini_audio(b"\x00" * OUTPUT_FRAME_BYTES)
    await proxy._frame_queue.join()

    binary_msgs = [m for m in sent if isinstance(m, (bytes, bytearray))]
    assert len(binary_msgs) == 10, "every queued frame should still reach the device"
    gaps = [b - a for a, b in zip(send_times, send_times[1:])]
    # First 7 frames are the burst: their 6 inter-frame gaps are near-zero.
    burst_gaps = gaps[:6]
    assert all(gap < 0.010 for gap in burst_gaps), burst_gaps
    # Frame 8 onward: steady ~60 ms pace resumes (allow scheduling slack).
    pace_gaps = gaps[6:]
    assert all(gap >= 0.045 for gap in pace_gaps), pace_gaps


@pytest.mark.asyncio
async def test_device_websocket_sends_share_send_lock(monkeypatch, info):
    esp32 = LockCheckingESP32()
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch, esp32=esp32)
    esp32.send_lock = proxy._send_lock
    locked_at_send: list[bool] = []

    async def send(msg):
        locked_at_send.append(proxy._send_lock.locked())
        sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)

    await proxy._handle_gemini_audio(b"\x00" * OUTPUT_FRAME_BYTES)
    await proxy._frame_queue.join()
    await asyncio.gather(*list(proxy._tts_tasks), return_exceptions=True)

    assert locked_at_send and all(locked_at_send)
    assert esp32.locked_at_call and all(esp32.locked_at_call)


@pytest.mark.asyncio
async def test_begin_tts_lights_blue_leds_via_ws_when_no_usb(monkeypatch, info):
    """When Gemini opens its mouth, the base ring should turn soft blue."""
    proxy, esp32, sent = _make_proxy(monkeypatch=monkeypatch)

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    esp32.calls.clear()

    await proxy._handle_gemini_audio(b"\x00" * OUTPUT_FRAME_BYTES)
    # LED dispatch is fire-and-forget; let the scheduled task run.
    await asyncio.gather(*list(proxy._tts_tasks), return_exceptions=True)

    led_calls = [c for c in esp32.calls if c[0] == "self.led.set_all"]
    assert led_calls, "begin_tts should fire a set_all_leds call"
    assert led_calls[0][1] == {"r": 0, "g": 80, "b": 180}


@pytest.mark.asyncio
async def test_begin_tts_uses_recorded_emotion_face(monkeypatch, info):
    proxy, esp32, sent = _make_proxy(monkeypatch=monkeypatch)

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    proxy._bridge.current_turn_emotion_face = "sleeping"
    esp32.calls.clear()

    await proxy._handle_gemini_audio(b"\x00" * OUTPUT_FRAME_BYTES)
    await asyncio.gather(*list(proxy._tts_tasks), return_exceptions=True)

    avatar_calls = [c for c in esp32.calls if c[0] == "self.display.set_avatar"]
    assert avatar_calls
    assert avatar_calls[0][1] == {"face": "sleeping"}


@pytest.mark.asyncio
async def test_begin_tts_falls_back_to_happy_without_emotion_record(monkeypatch, info):
    proxy, esp32, sent = _make_proxy(monkeypatch=monkeypatch)

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    esp32.calls.clear()

    await proxy._handle_gemini_audio(b"\x00" * OUTPUT_FRAME_BYTES)
    await asyncio.gather(*list(proxy._tts_tasks), return_exceptions=True)

    avatar_calls = [c for c in esp32.calls if c[0] == "self.display.set_avatar"]
    assert avatar_calls
    assert avatar_calls[0][1] == {"face": "happy"}


@pytest.mark.asyncio
async def test_end_tts_clears_leds(monkeypatch, info):
    proxy, esp32, sent = _make_proxy(monkeypatch=monkeypatch)

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    await proxy._handle_gemini_audio(b"\x00" * OUTPUT_FRAME_BYTES)
    await asyncio.gather(*list(proxy._tts_tasks), return_exceptions=True)
    esp32.calls.clear()

    await proxy.end_tts()
    await asyncio.gather(*list(proxy._tts_tasks), return_exceptions=True)

    led_calls = [c for c in esp32.calls if c[0] == "self.led.set_all"]
    assert led_calls, "end_tts should fire a set_all_leds call to extinguish"
    assert led_calls[-1][1] == {"r": 0, "g": 0, "b": 0}


@pytest.mark.asyncio
async def test_end_tts_restores_wake_listening_led(monkeypatch, info):
    proxy, esp32, sent = _make_proxy(monkeypatch=monkeypatch)
    gate = WakeGate(
        kws=ScriptedKeywordSpotter([True]),
        activity_rms_threshold=0.0,
    )
    proxy.wake_gate_factory = lambda: gate

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    await proxy.send_device_binary(b"wake")
    await proxy._handle_gemini_audio(b"\x00" * OUTPUT_FRAME_BYTES)
    await asyncio.gather(*list(proxy._tts_tasks), return_exceptions=True)
    esp32.calls.clear()

    await proxy.end_tts()
    await asyncio.gather(*list(proxy._tts_tasks), return_exceptions=True)

    led_calls = [c for c in esp32.calls if c[0] == "self.led.set_all"]
    assert led_calls[-1][1] == {
        "r": LISTENING_LED_RGB[0],
        "g": LISTENING_LED_RGB[1],
        "b": LISTENING_LED_RGB[2],
    }


@pytest.mark.asyncio
async def test_end_tts_clears_recorded_emotion_face(monkeypatch, info):
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch)

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    proxy._bridge.current_turn_emotion_face = "thinking"

    await proxy._handle_gemini_audio(b"\x00" * OUTPUT_FRAME_BYTES)
    await proxy.end_tts()

    assert proxy._bridge.current_turn_emotion_face is None
    assert proxy._bridge.clear_turn_emotion_face_called is True


@pytest.mark.asyncio
async def test_end_tts_without_active_audio_still_clears_emotion_face(monkeypatch, info):
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch)

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    proxy._bridge.current_turn_emotion_face = "sleeping"

    await proxy.end_tts()

    assert sent == []
    assert proxy._bridge.current_turn_emotion_face is None


@pytest.mark.asyncio
async def test_led_prefers_usb_transport_when_connected(monkeypatch, info):
    proxy, esp32, sent = _make_proxy(monkeypatch=monkeypatch)
    usb = FakeUSBTransport(connected=True)
    proxy.usb_transport = usb

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    esp32.calls.clear()
    usb.calls.clear()

    await proxy._handle_gemini_audio(b"\x00" * OUTPUT_FRAME_BYTES)
    await asyncio.gather(*list(proxy._tts_tasks), return_exceptions=True)

    assert any(c[0] == "self.led.set_all" for c in usb.calls)
    assert not any(c[0] == "self.led.set_all" for c in esp32.calls), (
        "USB should win; WS fallback must not double-send"
    )


@pytest.mark.asyncio
async def test_led_failure_does_not_break_tts_stream(monkeypatch, info):
    """A flaky LED layer must never knock out audio playback."""
    proxy, esp32, sent = _make_proxy(monkeypatch=monkeypatch)
    proxy.usb_transport = FakeUSBTransport(
        connected=True, raise_exc=RuntimeError("USB EIO")
    )

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    sent.clear()
    esp32.calls.clear()

    await proxy._handle_gemini_audio(b"\x00" * OUTPUT_FRAME_BYTES)
    await asyncio.gather(*list(proxy._tts_tasks), return_exceptions=True)

    # USB raised -> WS fallback should still set the LED.
    led_calls = [c for c in esp32.calls if c[0] == "self.led.set_all"]
    assert led_calls, "WS fallback should have fired the LED call"
    # And TTS must still have produced the start envelope + audio frames.
    json_msgs = [m for m in sent if isinstance(m, str)]
    binary_msgs = [m for m in sent if isinstance(m, (bytes, bytearray))]
    assert any(json.loads(m).get("state") == "start" for m in json_msgs)
    assert binary_msgs


@pytest.mark.asyncio
async def test_prebuffer_holds_first_chunk_until_threshold(monkeypatch, info):
    """Below the prebuffer threshold, no tts.start and no Opus go to the device.

    Gemini emits 24 kHz PCM in bursty chunks; releasing each chunk straight
    away starves the firmware buffer the moment Gemini pauses. Hold the first
    burst until we have enough cushion (200 ms by default) and ship it as one
    shot so playback stays smooth.
    """
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch)
    proxy.device_prebuffer_ms = 200.0  # 200ms @ 24 kHz int16 = 9600 bytes

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    sent.clear()
    proxy._codec.encoded.clear()

    # One 60 ms frame is well under the 200 ms threshold.
    await proxy._handle_gemini_audio(b"\x00" * OUTPUT_FRAME_BYTES)
    assert sent == [], "no envelopes or audio should fly before prebuffer fills"
    assert proxy._codec.encoded == [], "codec should not be touched yet"

    # Feed enough to cross 9600 bytes total (60ms * 4 = 240ms = 11520 bytes).
    for _ in range(3):
        await proxy._handle_gemini_audio(b"\x00" * OUTPUT_FRAME_BYTES)
    await proxy._frame_queue.join()

    json_msgs = [m for m in sent if isinstance(m, str)]
    binary_msgs = [m for m in sent if isinstance(m, (bytes, bytearray))]
    assert any(json.loads(m).get("state") == "start" for m in json_msgs)
    assert binary_msgs, "burst should ship Opus frames once threshold trips"
    assert len(proxy._codec.encoded) == 1
    assert len(proxy._codec.encoded[0]) == OUTPUT_FRAME_BYTES * 4


@pytest.mark.asyncio
async def test_prebuffer_passthrough_after_threshold_crossed(monkeypatch, info):
    """Once the prebuffer trips, later chunks ship immediately without batching."""
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch)
    proxy.device_prebuffer_ms = 60.0  # one frame trips it

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    sent.clear()
    proxy._codec.encoded.clear()

    await proxy._handle_gemini_audio(b"\x00" * OUTPUT_FRAME_BYTES)  # trips threshold
    await proxy._handle_gemini_audio(b"\x11" * OUTPUT_FRAME_BYTES)  # passthrough

    # Two encode calls: one burst (first chunk) + one direct (second chunk).
    assert len(proxy._codec.encoded) == 2
    assert proxy._codec.encoded[1] == b"\x11" * OUTPUT_FRAME_BYTES


@pytest.mark.asyncio
async def test_end_tts_flushes_unfilled_prebuffer(monkeypatch, info):
    """If Gemini's turn ends before the threshold trips, end_tts must still ship
    the held PCM — otherwise short utterances would never reach the speaker."""
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch)
    proxy.device_prebuffer_ms = 200.0  # threshold beyond a single 60ms chunk

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    sent.clear()
    proxy._codec.encoded.clear()

    await proxy._handle_gemini_audio(b"\x22" * OUTPUT_FRAME_BYTES)  # held back
    assert sent == []

    await proxy.end_tts()

    json_msgs = [m for m in sent if isinstance(m, str)]
    binary_msgs = [m for m in sent if isinstance(m, (bytes, bytearray))]
    states = [json.loads(m).get("state") for m in json_msgs]
    assert "start" in states, "end_tts must open the TTS envelope before flushing PCM"
    assert "stop" in states
    assert binary_msgs, "the held PCM should reach the device on end_tts"
    assert proxy._codec.encoded == [b"\x22" * OUTPUT_FRAME_BYTES]


@pytest.mark.asyncio
async def test_end_tts_flushes_codec_and_sends_stop(monkeypatch, info):
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch)

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)

    # Bring TTS state alive first.
    await proxy._handle_gemini_audio(b"\x00" * OUTPUT_FRAME_BYTES)
    sent.clear()
    await proxy.end_tts()

    json_msgs = [m for m in sent if isinstance(m, str)]
    assert any(json.loads(m).get("state") == "stop" for m in json_msgs)
    assert proxy._codec.flushed is True


@pytest.mark.asyncio
async def test_end_tts_without_active_tts_is_noop(monkeypatch, info):
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch)

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    sent.clear()
    await proxy.end_tts()
    assert sent == []


# --- try_handle_device_mcp ---------------------------------------------------


@pytest.mark.asyncio
async def test_try_handle_device_mcp_always_false(monkeypatch, info):
    """Gemini-mode function calls don't round-trip through the device."""
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch)

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    assert (
        await proxy.try_handle_device_mcp({"type": "mcp", "payload": {"id": 42}})
        is False
    )


# --- speak_text --------------------------------------------------------------


@pytest.mark.asyncio
async def test_speak_text_sends_prompt_to_gemini_session(monkeypatch, info):
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch)

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)

    # speak_text must return quickly here; fake codec leaves empty buffer.
    proxy._tts_active = False
    result = await proxy.speak_text(
        "你好呀",
        session_id="sess-1",
        prompt_audio_frames=[],
        emotion="happy",
    )
    assert result["ok"] is False
    assert result["frames_sent"] == 0
    assert result["provider"] == "gemini_live"
    # The bridge's fake session should have seen the prompt via realtime_input.
    bridge = proxy._bridge
    assert bridge.realtime_text_calls, "Gemini did not receive the speak prompt"
    text = bridge.realtime_text_calls[0]
    assert "你好呀" in text
    # Emotion hint reached the device JSON channel.
    json_emo = [
        m for m in sent
        if isinstance(m, str) and json.loads(m).get("type") == "llm"
    ]
    assert any(json.loads(m).get("emotion") == "happy" for m in json_emo)


@pytest.mark.asyncio
async def test_speak_text_returns_error_when_disconnected(monkeypatch, info):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    proxy, _, sent = _make_proxy()

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    result = await proxy.speak_text(
        "hi",
        session_id="x",
        prompt_audio_frames=[],
    )
    assert result["ok"] is False
    assert "not connected" in result["error"]


# --- Bridge wiring -----------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_factory_receives_voice_callbacks(monkeypatch, info):
    """AUDIO path: on_audio drives Opus directly, transcript only feeds logs.

    We still request response_modality="TEXT" so the bridge enables
    output_audio_transcription, but the wiring is reversed compared to the
    pre-fix-voice-latency setup: native PCM goes to _handle_gemini_audio,
    text just goes to the logger (not edge-tts).
    """
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch)

    captured = {}

    def capture(*args, **kwargs):
        captured.update(kwargs)
        return FakeBridge(*args, **kwargs)

    proxy.bridge_factory = capture

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    # Bound methods compare equal across accesses when they target the same
    # function on the same instance.
    assert captured.get("on_audio") == proxy._handle_gemini_audio
    assert captured.get("on_text") == proxy._log_gemini_transcript
    assert captured.get("on_turn_complete") == proxy.end_tts
    assert captured.get("response_modality") == "TEXT"
    assert captured.get("debug_status") is proxy._status


@pytest.mark.asyncio
async def test_drain_restores_cushion_after_source_stall(monkeypatch, info):
    """After the source (Gemini) stalls mid-reply, the drain task must catch up to restore the device-side cushion.

    The cushion is estimated open-loop: when frame n goes out the device has received n×60 ms of audio and has used
    (t_n − t_0) of wall-clock time since the first frame, so cushion = n×60 − (t_n − t_0). If after a T ms stall
    frames are still sent one per period (no catch-up), T ms of cushion are lost for good; the longer the turn, the thinner the cushion,
    and below the device's jitter tolerance the audio audibly breaks up.
    """
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch)
    proxy.device_frame_pace_ms = 60.0
    proxy.device_burst_ms = 400.0
    send_times: list[float] = []

    async def send(msg):
        sent.append(msg)
        if isinstance(msg, (bytes, bytearray)):
            send_times.append(asyncio.get_running_loop().time())

    await proxy.start(info, {"type": "hello"}, send)
    sent.clear()

    # First batch of 10 frames (600 ms of audio): 7 warm-up + 3 at the steady pace, cushion about 420 ms.
    for _ in range(10):
        await proxy._handle_gemini_audio(b"\x00" * OUTPUT_FRAME_BYTES)
    await proxy._frame_queue.join()
    # The source pauses 400 ms, nearly the whole warm-up cushion.
    await asyncio.sleep(0.4)
    # Second batch of 10 frames: the drain task must catch up at once and bring the cushion back to target.
    for _ in range(10):
        await proxy._handle_gemini_audio(b"\x00" * OUTPUT_FRAME_BYTES)
    await proxy._frame_queue.join()

    binary_msgs = [m for m in sent if isinstance(m, (bytes, bytearray))]
    assert len(binary_msgs) == 20, "every frame must reach the device"
    t0 = send_times[0]
    cushions = [
        (i + 1) * 60.0 - (t - t0) * 1000.0 for i, t in enumerate(send_times)
    ]
    assert cushions[-1] >= 300.0, (
        f"cushion not restored after the stall: last frame {cushions[-1]:.0f} ms, "
        f"all {[f'{c:.0f}' for c in cushions]}"
    )


# --- Session keepalive wiring --------------------------------------------------


@pytest.mark.asyncio
async def test_start_with_wake_gate_disables_keepalive_by_default(monkeypatch, info):
    from stackchan_mcp.debug_status import DebugStatus

    st = DebugStatus()
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch, debug_status=st)
    gate = WakeGate(
        kws=ScriptedKeywordSpotter([False]),
        activity_rms_threshold=0.0,
    )
    proxy.wake_gate_factory = lambda: gate

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    try:
        assert proxy._keepalive is None
        assert (
            st.snapshot()["gemini"]["keepalive_disabled_reason"]
            == "interval_disabled"
        )
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_start_with_wake_gate_starts_keepalive_when_env_enabled(monkeypatch, info):
    from stackchan_mcp.debug_status import DebugStatus

    monkeypatch.setenv("STACKCHAN_GEMINI_KEEPALIVE_S", "90")
    st = DebugStatus()
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch, debug_status=st)
    gate = WakeGate(
        kws=ScriptedKeywordSpotter([False]),
        activity_rms_threshold=0.0,
    )
    proxy.wake_gate_factory = lambda: gate

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    try:
        assert proxy._keepalive is not None
        assert proxy._keepalive.running is True
        assert st.snapshot()["gemini"]["keepalive_running"] is True
        assert proxy._keepalive_should_send() is True
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_keepalive_pauses_while_listening(monkeypatch, info):
    from stackchan_mcp.debug_status import DebugStatus

    st = DebugStatus()
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch, debug_status=st)
    gate = WakeGate(
        kws=ScriptedKeywordSpotter([True]),
        activity_rms_threshold=0.0,
    )
    proxy.wake_gate_factory = lambda: gate

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    try:
        await proxy.send_device_binary(b"wake")
        assert gate.state == WakeGateState.LISTENING
        assert proxy._keepalive_should_send() is False
        assert st.snapshot()["gemini"]["last_keepalive_skip_reason"] == "listening"
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_start_without_wake_gate_skips_keepalive(monkeypatch, info):
    from stackchan_mcp.debug_status import DebugStatus

    st = DebugStatus()
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch, debug_status=st)

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)  # wake word off → pass-through
    try:
        assert proxy._wake_gate is None
        assert proxy._keepalive is None
        assert (
            st.snapshot()["gemini"]["keepalive_disabled_reason"]
            == "wake_gate_absent"
        )
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_keepalive_env_zero_disables(monkeypatch, info):
    from stackchan_mcp.debug_status import DebugStatus

    monkeypatch.setenv("STACKCHAN_GEMINI_KEEPALIVE_S", "0")
    st = DebugStatus()
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch, debug_status=st)
    gate = WakeGate(
        kws=ScriptedKeywordSpotter([False]),
        activity_rms_threshold=0.0,
    )
    proxy.wake_gate_factory = lambda: gate

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    try:
        assert proxy._keepalive is None
        assert (
            st.snapshot()["gemini"]["keepalive_disabled_reason"]
            == "interval_disabled"
        )
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_send_keepalive_silence_falls_back_to_send_audio(monkeypatch, info):
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch)

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    try:
        bridge = proxy._bridge  # FakeBridge has no send_keepalive_audio
        await proxy._send_keepalive_silence(b"\x00" * 64)
        assert bridge.audio_sent == [b"\x00" * 64]
    finally:
        await proxy.stop()


def _manual_vad_call_kinds(calls: list[dict]) -> list[str]:
    kinds: list[str] = []
    for call in calls:
        if call.get("activity_start") is not None:
            kinds.append("start")
        elif call.get("activity_end") is not None:
            kinds.append("end")
        elif call.get("audio") is not None:
            kinds.append("audio")
    return kinds


@pytest.mark.asyncio
async def test_second_utterance_in_listening_window_reopens_activity(monkeypatch):
    from stackchan_mcp.gemini_live_bridge import GeminiLiveBridge

    monkeypatch.setenv("STACKCHAN_MANUAL_VAD", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    class LiveSession:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def send_realtime_input(self, **kwargs) -> None:
            self.calls.append(kwargs)

    live = LiveSession()
    bridge = GeminiLiveBridge(FakeESP32(), api_key="test-key")
    bridge._session = live

    proxy, _, _ = _make_proxy(monkeypatch=monkeypatch)
    proxy._bridge = bridge
    proxy._codec = FakeCodec()
    gate = WakeGate(
        kws=ScriptedKeywordSpotter([False]),
        activity_rms_threshold=0.0,
    )
    gate.state = WakeGateState.LISTENING
    proxy._wake_gate = gate

    await proxy._forward_gated_device_pcm(b"round1")
    await bridge.send_audio_stream_end()
    await proxy._forward_gated_device_pcm(b"round2")

    assert _manual_vad_call_kinds(live.calls) == ["start", "audio", "end", "start", "audio"]


@pytest.mark.asyncio
async def test_fallback_pass_through_opens_manual_vad_before_audio(monkeypatch):
    from stackchan_mcp.gemini_live_bridge import GeminiLiveBridge

    monkeypatch.setenv("STACKCHAN_MANUAL_VAD", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    class LiveSession:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def send_realtime_input(self, **kwargs) -> None:
            self.calls.append(kwargs)

    live = LiveSession()
    bridge = GeminiLiveBridge(FakeESP32(), api_key="test-key")
    bridge._session = live

    proxy, _, _ = _make_proxy(monkeypatch=monkeypatch)
    proxy._bridge = bridge
    proxy._codec = FakeCodec()
    proxy._wake_gate = None

    await proxy._forward_gated_device_pcm(b"bypass")

    assert _manual_vad_call_kinds(live.calls) == ["start", "audio"]


@pytest.mark.asyncio
async def test_wake_sends_manual_vad_activity_start(monkeypatch, info):
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch)
    gate = WakeGate(
        kws=ScriptedKeywordSpotter([True]),
        activity_rms_threshold=0.0,
    )
    proxy.wake_gate_factory = lambda: gate

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    try:
        await proxy.send_device_binary(b"wake")
        assert proxy._bridge.activity_start_sent is True
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_gate_close_skips_led_off_while_tts_active(monkeypatch, info):
    from stackchan_mcp.debug_status import DebugStatus

    st = DebugStatus()
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch, debug_status=st)

    class TtsAwareGate(WakeGate):
        def process(self, pcm):
            self.state = WakeGateState.LISTENING
            return WakeGateResult(closed=True)

    gate = TtsAwareGate(
        kws=ScriptedKeywordSpotter([True]),
        activity_rms_threshold=0.0,
        is_tts_active=lambda: True,
    )
    proxy._wake_gate = gate
    proxy._tts_active = True
    proxy._bridge = FakeBridge()
    proxy._codec = FakeCodec()

    led_calls: list[tuple[int, int, int]] = []

    async def capture_led(r, g, b):
        led_calls.append((r, g, b))

    proxy._set_leds_safe = capture_led  # type: ignore[method-assign]

    await proxy._forward_gated_device_pcm(b"pcm")

    assert led_calls == []
    assert proxy._bridge.activity_end_sent is True


@pytest.mark.asyncio
async def test_wake_gate_crash_rebuilds_with_backoff(monkeypatch, info):
    from stackchan_mcp.debug_status import DebugStatus

    st = DebugStatus()
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch, debug_status=st)
    builds = {"count": 0}

    class FlakyGate(WakeGate):
        def process(self, pcm):
            raise RuntimeError("kws exploded")

    def factory():
        builds["count"] += 1
        if builds["count"] == 1:
            return FlakyGate(
                kws=ScriptedKeywordSpotter([False]),
                activity_rms_threshold=0.0,
            )
        return WakeGate(
            kws=ScriptedKeywordSpotter([False]),
            activity_rms_threshold=0.0,
        )

    proxy.wake_gate_factory = factory
    proxy._wake_gate_rebuild_max = 3

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    try:
        await proxy.send_device_binary(b"one")
        assert proxy._wake_gate_failures == 1
        assert proxy._wake_gate is None

        proxy._wake_gate_next_rebuild_at = 0.0
        await proxy.send_device_binary(b"two")
        assert isinstance(proxy._wake_gate, WakeGate)
        assert proxy._wake_gate_failures == 0
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_wake_gate_permanent_failure_reports_unavailable(monkeypatch, info):
    from stackchan_mcp.debug_status import DebugStatus

    st = DebugStatus()
    proxy, _, sent = _make_proxy(monkeypatch=monkeypatch, debug_status=st)

    class FlakyGate(WakeGate):
        def process(self, pcm):
            raise RuntimeError("kws exploded")

    proxy.wake_gate_factory = lambda: FlakyGate(
        kws=ScriptedKeywordSpotter([False]),
        activity_rms_threshold=0.0,
    )
    proxy._wake_gate_rebuild_max = 1

    async def send(msg): sent.append(msg)

    await proxy.start(info, {"type": "hello"}, send)
    try:
        await proxy.send_device_binary(b"fail")
        snap = st.snapshot()["wake_gate"]
        assert snap["available"] is False
        assert snap["state"] == "UNAVAILABLE"
    finally:
        await proxy.stop()
