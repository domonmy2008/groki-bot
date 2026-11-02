# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Exercise the real proxy through a loopback stand-in for the upstream APIs."""

import asyncio
import base64
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import aiohttp
from aiohttp import web
import pytest

from stackchan_mcp.gbot_voice_bridge import GbotVoiceBridge
from stackchan_mcp.gbot_voice_proxy import GbotVoiceProxy
from stackchan_mcp.capture_server import DEBUG_STATUS_KEY, handle_voice_health
from stackchan_mcp.debug_status import DebugStatus


async def eventually(predicate, timeout=2):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), timeout)


class Upstream:
    def __init__(self):
        self.available = True
        self.acknowledge = True
        self.credential_requests = 0
        self.sockets = []
        self.updates = []
        self.finish_responses = True
        self.failure_reason = "private upstream detail"
        self.suppress_responses = False
        self.response_requests = []

    async def credential(self, request):
        self.credential_requests += 1
        if not self.available:
            return web.json_response({"error": self.failure_reason}, status=502)
        return web.json_response({"credential": {
            "clientSecret": "test-ephemeral-secret",
            "expiresAtUnixSeconds": time.time() + 60,
            "model": "fixture",
            "websocketUrl": "wss://api.x.ai/v1/realtime",
        }})

    async def session(self, request):
        return web.json_response({"instructions": "fixture", "tools": []})

    async def voice(self, request):
        ws = web.WebSocketResponse(protocols=["xai-client-secret.test-ephemeral-secret"])
        await ws.prepare(request)
        self.sockets.append(ws)
        async for message in ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                continue
            event = json.loads(message.data)
            if event["type"] == "session.update":
                self.updates.append(event)
                if self.acknowledge:
                    await ws.send_json({"type": "session.updated", "session": {}})
            elif event["type"] == "response.create":
                self.response_requests.append(ws)
                if self.suppress_responses:
                    continue
                response_id = "reply-" + str(len(self.sockets))
                await ws.send_json({"type": "response.created", "response": {"id": response_id}})
                await ws.send_json({
                    "type": "response.audio.delta", "response_id": response_id,
                    "delta": base64.b64encode(bytes(2880 * 4)).decode(),
                })
                if self.finish_responses:
                    await ws.send_json({"type": "response.done", "response": {"id": response_id}})
        return ws


@asynccontextmanager
async def connected_proxy(monkeypatch, upstream):
    app = web.Application()
    app.router.add_get("/credential", upstream.credential)
    app.router.add_post("/session", upstream.session)
    app.router.add_get("/voice", upstream.voice)
    status = DebugStatus()
    app[DEBUG_STATUS_KEY] = status
    app.router.add_get("/debug/voice-health", handle_voice_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    address = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
    original = aiohttp.ClientSession.ws_connect

    def local_voice(session, url, **kwargs):
        assert url.startswith("wss://api.x.ai/")
        return original(session, address + "/voice", **kwargs)

    monkeypatch.setattr(aiohttp.ClientSession, "ws_connect", local_voice)
    monkeypatch.delenv("STACKCHAN_TOOL_BOT", raising=False)
    monkeypatch.delenv("STACKCHAN_GBOT_VOICE_TOOLS", raising=False)

    def bridge_factory(**kwargs):
        bridge = GbotVoiceBridge(**kwargs, bridge_url=address, agent_id="fixture", local_tools=False)
        bridge.response_stall_timeout_s = 0.06
        return bridge

    delivered = []

    async def sink(message):
        delivered.append(message)

    proxy = GbotVoiceProxy(
        bridge_factory=bridge_factory, wake_gate_factory=lambda: None,
        device_tts_start_delay_s=0, device_frame_pace_ms=0, device_prebuffer_ms=0,
        debug_status=status,
    )
    proxy.reconnect_initial_delay_s = 0.01
    proxy.reconnect_max_delay_s = 0.04
    proxy.health_check_interval_s = 0.03
    proxy.health_check_timeout_s = 0.05
    proxy.connect_timeout_s = 0.08
    proxy.health_url = address + "/debug/voice-health"
    try:
        yield proxy, delivered, sink
    finally:
        await proxy.stop()
        for socket in upstream.sockets:
            await socket.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_initial_voice_failure_recovers_without_reconnecting_device(monkeypatch):
    upstream = Upstream()
    upstream.available = False
    async with connected_proxy(monkeypatch, upstream) as (proxy, delivered, sink):
        assert not await proxy.start(SimpleNamespace(device_id="fixture-device"), {}, sink)
        upstream.available = True
        await eventually(lambda: proxy.connected)
        result = await proxy.speak_text("test reply", session_id="fixture", prompt_audio_frames=[])
        assert result["ok"] and any(isinstance(frame, bytes) for frame in delivered)
        assert proxy._status.snapshot()["voice"]["state"] == "connected"


@pytest.mark.asyncio
async def test_open_socket_stays_up_without_empty_session_probe(monkeypatch):
    upstream = Upstream()
    async with connected_proxy(monkeypatch, upstream) as (proxy, delivered, sink):
        assert await proxy.start(SimpleNamespace(device_id="fixture-device"), {}, sink)
        await eventually(lambda: proxy.health_snapshot()["successful_checks"] >= 2)
        assert proxy.connected
        assert len(upstream.sockets) == 1
        assert upstream.sockets[0].closed is False
        assert upstream.updates
        assert all(update["session"] != {} for update in upstream.updates)
        result = await proxy.speak_text("still here", session_id="fixture", prompt_audio_frames=[])
        assert result["ok"] and any(isinstance(frame, bytes) for frame in delivered)


@pytest.mark.asyncio
async def test_check_connection_fails_when_socket_or_receive_task_is_dead(monkeypatch):
    upstream = Upstream()
    async with connected_proxy(monkeypatch, upstream) as (proxy, _delivered, sink):
        assert await proxy.start(SimpleNamespace(device_id="fixture-device"), {}, sink)
        bridge = proxy._bridge
        before = len(upstream.updates)
        assert await bridge.check_connection(0.05) is True
        assert len(upstream.updates) == before

        health = proxy._health_task
        proxy._health_task = None
        health.cancel()
        await asyncio.gather(health, return_exceptions=True)

        original_ws = bridge._ws
        original_task = bridge._task
        try:
            bridge._ws = SimpleNamespace(closed=True, exception=lambda: None)
            assert await bridge.check_connection(0.05) is False

            finished = asyncio.create_task(asyncio.sleep(0))
            await finished
            bridge._ws = SimpleNamespace(closed=False, exception=lambda: None)
            bridge._task = finished
            bridge.running = True
            bridge._stopped = False
            bridge._connected_event.set()
            assert await bridge.check_connection(0.05) is False

            bridge._ws = SimpleNamespace(
                closed=False, exception=lambda: RuntimeError("pong timeout")
            )
            bridge._task = original_task
            assert await bridge.check_connection(0.05) is False
            assert all(update["session"] != {} for update in upstream.updates)
        finally:
            bridge._ws = original_ws
            bridge._task = original_task
            bridge.running = True
            bridge._stopped = False
            bridge._connected_event.set()


@pytest.mark.asyncio
async def test_closed_voice_socket_recovers_and_stop_prevents_further_retries(monkeypatch):
    upstream = Upstream()
    async with connected_proxy(monkeypatch, upstream) as (proxy, delivered, sink):
        assert await proxy.start(SimpleNamespace(device_id="fixture-device"), {}, sink)
        await upstream.sockets[0].close(code=1001)
        await eventually(lambda: len(upstream.sockets) >= 2 and proxy.connected)
        upstream.available = False
        await upstream.sockets[-1].close()
        await eventually(lambda: proxy._status.snapshot()["voice"]["state"] == "reconnecting")
        await proxy.stop()
        attempts = upstream.credential_requests
        await asyncio.sleep(0.1)
        assert upstream.credential_requests == attempts
        assert proxy._status.snapshot()["voice"]["state"] == "stopped"


@pytest.mark.asyncio
async def test_failed_health_check_releases_speaker_before_reconnecting(monkeypatch):
    upstream = Upstream()
    upstream.finish_responses = False
    async with connected_proxy(monkeypatch, upstream) as (proxy, delivered, sink):
        assert await proxy.start(SimpleNamespace(device_id="fixture-device"), {}, sink)
        speaking = asyncio.create_task(proxy.speak_text(
            "interrupted reply", session_id="fixture", prompt_audio_frames=[]
        ))
        try:
            await eventually(lambda: any(
                isinstance(item, str) and json.loads(item).get("state") == "start"
                for item in delivered
            ))
            upstream.acknowledge = False
            await eventually(lambda: proxy._status.snapshot()["voice"]["state"] == "reconnecting")
            await eventually(lambda: any(
                isinstance(item, str) and json.loads(item).get("state") == "stop"
                for item in delivered
            ))
        finally:
            speaking.cancel()
            await asyncio.gather(speaking, return_exceptions=True)


@pytest.mark.asyncio
async def test_no_session_ack_is_unhealthy_even_when_http_and_socket_are_open(monkeypatch):
    upstream = Upstream()
    upstream.acknowledge = False
    async with connected_proxy(monkeypatch, upstream) as (proxy, delivered, sink):
        assert not await proxy.start(SimpleNamespace(device_id="fixture-device"), {}, sink)
        async with aiohttp.ClientSession() as client:
            async with client.get(proxy.health_url) as response:
                assert response.status == 503
                assert (await response.json())["ok"] is False
            upstream.acknowledge = True
            await eventually(lambda: proxy.connected)
            async with client.get(proxy.health_url) as response:
                assert response.status == 200
                assert (await response.json())["last_verified_at"] is not None


@pytest.mark.asyncio
async def test_expired_login_is_actionable_without_exposing_upstream_details(monkeypatch):
    upstream = Upstream()
    upstream.available = False
    upstream.failure_reason = "login_expired_open_grok_bot"
    async with connected_proxy(monkeypatch, upstream) as (proxy, delivered, sink):
        assert not await proxy.start(SimpleNamespace(device_id="fixture-device"), {}, sink)
        async with aiohttp.ClientSession() as client:
            async with client.get(proxy.health_url) as response:
                health = await response.json()
                assert health["action_required"] is True
                assert health["last_error"] == "login_expired_open_grok_bot"
        upstream.available = True
        await eventually(lambda: proxy.connected)
        health = proxy._status.snapshot()["voice"]
        assert health["action_required"] is False
        assert health["last_error"] is None


@pytest.mark.asyncio
async def test_silent_reply_recovers_without_replaying_user_request(monkeypatch):
    upstream = Upstream()
    upstream.suppress_responses = True
    async with connected_proxy(monkeypatch, upstream) as (proxy, delivered, sink):
        assert await proxy.start(SimpleNamespace(device_id="fixture-device"), {}, sink)
        speaking = asyncio.create_task(proxy.speak_text(
            "original request", session_id="fixture", prompt_audio_frames=[]
        ))
        try:
            await eventually(lambda: len(upstream.sockets) >= 2 and proxy.connected)
            assert len(upstream.response_requests) == 1
            interrupted = await asyncio.wait_for(speaking, 0.1)
            assert interrupted["ok"] is False
            upstream.suppress_responses = False
            result = await proxy.speak_text("new request", session_id="fixture", prompt_audio_frames=[])
            assert result["ok"]
            assert len(upstream.response_requests) == 2
        finally:
            speaking.cancel()
            await asyncio.gather(speaking, return_exceptions=True)


@pytest.mark.asyncio
async def test_dormant_session_dead_does_not_sticky_red(monkeypatch):
    """Silent WS death while DORMANT must not paint sticky red / reported_dead."""
    from stackchan_mcp.device_emotion import DEAD_LED_RGB
    from stackchan_mcp.wake_gate import WakeGate, WakeGateState

    class FakeSpotter:
        available = True

        def detect(self, pcm):
            return False

        def reset(self):
            return None

    class LedESP32:
        device_connected = True

        def __init__(self):
            self.leds = []

        async def send_led(self, r, g, b, *, notify_activity=True):
            self.leds.append((r, g, b))
            return {"ok": True}, None

    esp32 = LedESP32()
    gate = WakeGate(kws=FakeSpotter(), activity_rms_threshold=0.0)
    assert gate.state == WakeGateState.DORMANT
    proxy = GbotVoiceProxy(
        wake_gate_factory=lambda: gate,
        esp32_ref=lambda: esp32,
        device_tts_start_delay_s=0,
        device_frame_pace_ms=0,
        device_prebuffer_ms=0,
        debug_status=DebugStatus(),
    )
    proxy._wake_gate = gate
    proxy._stopped = False

    await proxy._handle_session_dead()
    assert gate.state == WakeGateState.DORMANT
    assert proxy._reported_dead is False
    assert DEAD_LED_RGB not in esp32.leds


@pytest.mark.asyncio
async def test_listening_session_dead_still_closes_and_lights_red(monkeypatch):
    from stackchan_mcp.device_emotion import DEAD_LED_RGB
    from stackchan_mcp.wake_gate import WakeGate, WakeGateState

    class FakeSpotter:
        available = True

        def detect(self, pcm):
            return True

        def reset(self):
            return None

    class LedESP32:
        device_connected = True

        def __init__(self):
            self.leds = []

        async def send_led(self, r, g, b, *, notify_activity=True):
            self.leds.append((r, g, b))
            return {"ok": True}, None

    esp32 = LedESP32()
    gate = WakeGate(kws=FakeSpotter(), activity_rms_threshold=0.0)
    gate.process(b"\x00\x00" * 160)
    assert gate.state == WakeGateState.LISTENING
    proxy = GbotVoiceProxy(
        wake_gate_factory=lambda: gate,
        esp32_ref=lambda: esp32,
        device_tts_start_delay_s=0,
        device_frame_pace_ms=0,
        device_prebuffer_ms=0,
        debug_status=DebugStatus(),
    )
    proxy._wake_gate = gate
    proxy._stopped = False

    await proxy._handle_session_dead()
    assert gate.state == WakeGateState.DORMANT
    assert proxy._reported_dead is True
    assert esp32.leds[-1] == DEAD_LED_RGB


@pytest.mark.asyncio
async def test_prepare_for_wake_waits_for_supervisor_reconnect(monkeypatch):
    proxy = GbotVoiceProxy(
        wake_gate_factory=lambda: None,
        device_tts_start_delay_s=0,
        device_frame_pace_ms=0,
        device_prebuffer_ms=0,
        debug_status=DebugStatus(),
    )
    proxy.wake_reconnect_timeout_s = 0.5
    proxy._stopped = False
    proxy._health["state"] = "reconnecting"
    proxy._health["last_error"] = "connection_lost"

    connected = {"value": False}

    def fake_connected(self):
        return connected["value"]

    monkeypatch.setattr(GbotVoiceProxy, "connected", property(fake_connected))

    async def flip():
        await asyncio.sleep(0.05)
        connected["value"] = True

    task = asyncio.create_task(flip())
    try:
        assert await proxy._prepare_for_wake() is True
    finally:
        await task


@pytest.mark.asyncio
async def test_prepare_for_wake_aborts_when_still_disconnected(monkeypatch):
    proxy = GbotVoiceProxy(
        wake_gate_factory=lambda: None,
        device_tts_start_delay_s=0,
        device_frame_pace_ms=0,
        device_prebuffer_ms=0,
        debug_status=DebugStatus(),
    )
    proxy.wake_reconnect_timeout_s = 0.15
    proxy._stopped = False
    monkeypatch.setattr(GbotVoiceProxy, "connected", property(lambda self: False))
    assert await proxy._prepare_for_wake() is False


@pytest.mark.asyncio
async def test_wake_open_aborts_when_prepare_for_wake_fails(monkeypatch):
    """KWS OPEN with unhealthy voice closes the gate instead of listening into a void."""
    from stackchan_mcp.wake_gate import WakeGate, WakeGateState

    class OnceSpotter:
        available = True

        def __init__(self):
            self._hit = False

        def detect(self, pcm):
            if self._hit:
                return False
            self._hit = True
            return True

        def reset(self):
            return None

    class Bridge:
        running = True
        sent = []

        async def send_audio(self, pcm):
            self.sent.append(pcm)

        async def send_activity_start(self):
            return None

        async def send_activity_end(self):
            return None

    gate = WakeGate(kws=OnceSpotter(), activity_rms_threshold=999999.0)
    bridge = Bridge()
    proxy = GbotVoiceProxy(
        wake_gate_factory=lambda: gate,
        device_tts_start_delay_s=0,
        device_frame_pace_ms=0,
        device_prebuffer_ms=0,
        wake_confirm_hold_s=0,
        debug_status=DebugStatus(),
    )
    proxy._wake_gate = gate
    proxy._bridge = bridge
    proxy._stopped = False
    monkeypatch.setattr(
        GbotVoiceProxy, "_prepare_for_wake", lambda self: asyncio.sleep(0, result=False)
    )

    await proxy._forward_gated_device_pcm(b"\x00\x00" * 160)
    assert gate.state == WakeGateState.DORMANT
    assert bridge.sent == []

@pytest.mark.asyncio
async def test_end_the_call_aborts_tts_and_nudges_when_disconnected(monkeypatch):
    """Hangup frees the speaker immediately and starts reconnect if needed."""
    from stackchan_mcp.wake_gate import WakeGate, WakeGateState

    class Spotter:
        available = True

        def detect(self, pcm):
            return False

        def reset(self):
            return None

    gate = WakeGate(kws=Spotter(), activity_rms_threshold=0.0)
    gate.state = WakeGateState.LISTENING
    proxy = GbotVoiceProxy(
        wake_gate_factory=lambda: gate,
        device_tts_start_delay_s=0,
        device_frame_pace_ms=0,
        device_prebuffer_ms=0,
        debug_status=DebugStatus(),
    )
    proxy._wake_gate = gate
    proxy._stopped = False
    proxy._tts_active = True
    proxy._health["state"] = "reconnecting"
    proxy._health["last_error"] = "connection_lost"
    monkeypatch.setattr(GbotVoiceProxy, "connected", property(lambda self: False))
    nudged = {"value": False}
    monkeypatch.setattr(
        proxy, "_nudge_reconnect", lambda: nudged.__setitem__("value", True)
    )

    await proxy._handle_bridge_end_conversation()

    assert gate.state == WakeGateState.DORMANT
    assert proxy._tts_active is False
    assert proxy._suppress_tts_until_wake is True
    assert nudged["value"] is True


@pytest.mark.asyncio
async def test_end_the_call_keeps_healthy_session_without_nudge(monkeypatch):
    from stackchan_mcp.wake_gate import WakeGate, WakeGateState

    class Spotter:
        available = True

        def detect(self, pcm):
            return False

        def reset(self):
            return None

    gate = WakeGate(kws=Spotter(), activity_rms_threshold=0.0)
    gate.state = WakeGateState.LISTENING
    proxy = GbotVoiceProxy(
        wake_gate_factory=lambda: gate,
        device_tts_start_delay_s=0,
        device_frame_pace_ms=0,
        device_prebuffer_ms=0,
        debug_status=DebugStatus(),
    )
    proxy._wake_gate = gate
    proxy._stopped = False
    monkeypatch.setattr(GbotVoiceProxy, "connected", property(lambda self: True))
    nudged = {"value": False}
    monkeypatch.setattr(
        proxy, "_nudge_reconnect", lambda: nudged.__setitem__("value", True)
    )

    await proxy._handle_bridge_end_conversation()

    assert gate.state == WakeGateState.DORMANT
    assert nudged["value"] is False
    assert proxy._suppress_tts_until_wake is True

