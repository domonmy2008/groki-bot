"""Tests for ESP32 client connection management."""

import asyncio
import json
import logging

import pytest
import pytest_asyncio
import websockets

from stackchan_mcp.audio_stream import SpeakResult
from stackchan_mcp.esp32_client import (
    ESP32Connection,
    ESP32Manager,
    bracket_tts_payloads,
    trim_pending_audio,
    websocket_server_options,
)


@pytest_asyncio.fixture
async def manager():
    """Create and start an ESP32Manager on a free port."""
    mgr = ESP32Manager()
    await mgr.start("127.0.0.1", 0)  # Port 0 = OS picks a free port

    # Get the actual port
    server = mgr._server
    port = server.sockets[0].getsockname()[1]
    mgr._test_port = port

    yield mgr
    await mgr.stop()


@pytest.mark.asyncio
async def test_manager_starts_and_stops():
    """Manager can start and stop cleanly."""
    mgr = ESP32Manager()
    await mgr.start("127.0.0.1", 0)
    assert mgr._server is not None
    await mgr.stop()
    assert mgr._server is None


@pytest.mark.asyncio
async def test_no_device_connected():
    """call_tool returns error when no device is connected."""
    mgr = ESP32Manager()
    result, error = await mgr.call_tool("self.robot.set_head_angles", {"yaw": 0, "pitch": 0})
    assert result is None
    assert error is not None
    assert "not connected" in error["message"].lower() or "No ESP32" in error["message"]


@pytest.mark.asyncio
async def test_get_status_disconnected():
    """get_status returns disconnected state."""
    mgr = ESP32Manager()
    status = mgr.get_status()
    assert status["connected"] is False
    assert status["device_id"] is None


@pytest.mark.asyncio
async def test_listen_detect_triggers_wake_callback_without_state_callback():
    mgr = ESP32Manager()
    wake_detected = asyncio.Event()

    async def on_wake_detected() -> None:
        wake_detected.set()

    mgr.on_wake_detected = on_wake_detected

    mgr._observe_device_state({"type": "listen", "state": "detect"})

    await asyncio.wait_for(wake_detected.wait(), timeout=1.0)


@pytest.mark.asyncio
async def test_esp32_hello_handshake(manager):
    """ESP32 can connect and complete hello handshake."""
    port = manager._test_port

    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        # Send hello
        hello = {
            "type": "hello",
            "version": 1,
            "features": {"mcp": True},
            "transport": "websocket",
            "audio_params": {
                "format": "opus",
                "sample_rate": 16000,
                "channels": 1,
                "frame_duration": 60,
            },
        }
        await ws.send(json.dumps(hello))

        # Receive hello response
        resp_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        resp = json.loads(resp_raw)
        assert resp["type"] == "hello"
        assert resp["version"] == 1
        assert "session_id" in resp

        # Receive initialize request from gateway
        init_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        init_msg = json.loads(init_raw)
        assert init_msg["type"] == "mcp"
        assert init_msg["payload"]["method"] == "initialize"

        # Send initialize response
        init_resp = {
            "session_id": init_msg["session_id"],
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "id": init_msg["payload"]["id"],
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "test-device", "version": "1.0.0"},
                },
            },
        }
        await ws.send(json.dumps(init_resp))

        # Receive tools/list request
        tools_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        tools_msg = json.loads(tools_raw)
        assert tools_msg["type"] == "mcp"
        assert tools_msg["payload"]["method"] == "tools/list"

        # Send tools/list response
        tools_resp = {
            "session_id": tools_msg["session_id"],
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "id": tools_msg["payload"]["id"],
                "result": {
                    "tools": [
                        {
                            "name": "self.robot.set_head_angles",
                            "description": "Set head angles",
                            "inputSchema": {"type": "object"},
                        }
                    ],
                    "nextCursor": "",
                },
            },
        }
        await ws.send(json.dumps(tools_resp))

        # Wait for manager to process
        await asyncio.sleep(0.2)

        # Verify connection is established
        assert manager.device_connected is True
        status = manager.get_status()
        assert status["connected"] is True
        assert status["tools_count"] == 1


@pytest.mark.asyncio
async def test_esp32_hello_accepts_voice_only_device_without_mcp(manager):
    """features.mcp=false devices (e.g. ciniml/stackchan-idf's XiaoZhiClient)
    are accepted as voice-only: hello still succeeds, but no MCP
    initialize/tools_list follows and the connection never reaches
    ``initialized`` — the device has no MCP client to answer those.
    """
    port = manager._test_port

    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await ws.send(json.dumps({
            "type": "hello",
            "version": 1,
            "transport": "websocket",
            "features": {"mcp": False},
            "audio_params": {
                "format": "opus",
                "sample_rate": 16000,
                "channels": 1,
                "frame_duration": 60,
            },
        }))

        resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
        assert resp["type"] == "hello"
        assert "session_id" in resp

        # No MCP initialize should follow — waiting past a short read
        # timeout must not surface a request the device could never answer.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ws.recv(), timeout=0.3)

        await asyncio.sleep(0.1)
        assert manager.device_connected is True
        status = manager.get_status()
        assert status["connected"] is True
        assert status["initialized"] is False
        assert status["mcp_supported"] is False
        assert status["tools_count"] == 0

        result, error = await manager.call_tool("self.led.set_all", {"r": 0})
        assert result is None
        assert error is not None
        assert "features.mcp=false" in error["message"]


@pytest.mark.asyncio
async def test_send_emotion_works_for_voice_only_device(manager):
    """Voice-only XiaoZhi clients still accept llm.emotion JSON."""
    port = manager._test_port

    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await ws.send(json.dumps({
            "type": "hello",
            "version": 1,
            "transport": "websocket",
            "features": {"mcp": False},
        }))
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
        await asyncio.sleep(0.1)

        result, error = await manager.send_emotion("thinking")

        assert error is None
        assert result == {"ok": True, "emotion": "thinking"}
        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        msg = json.loads(raw)
        assert msg == {
            "session_id": hello["session_id"],
            "type": "llm",
            "emotion": "thinking",
        }


@pytest.mark.asyncio
async def test_send_led_works_for_voice_only_device(manager):
    """Voice-only XiaoZhi clients accept the direct led JSON control frame."""
    port = manager._test_port

    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await ws.send(json.dumps({
            "type": "hello",
            "version": 1,
            "transport": "websocket",
            "features": {"mcp": False},
        }))
        await asyncio.wait_for(ws.recv(), timeout=5.0)
        await asyncio.sleep(0.1)

        result, error = await manager.send_led(0, 180, 180)

        assert error is None
        assert result == {"ok": True, "r": 0, "g": 180, "b": 180}
        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        msg = json.loads(raw)
        assert msg == {"type": "led", "r": 0, "g": 180, "b": 180}


@pytest.mark.asyncio
async def test_send_head_works_for_voice_only_device(manager):
    """Voice-only XiaoZhi clients accept the direct head JSON control frame."""
    port = manager._test_port

    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await ws.send(json.dumps({
            "type": "hello",
            "version": 1,
            "transport": "websocket",
            "features": {"mcp": False},
        }))
        await asyncio.wait_for(ws.recv(), timeout=5.0)
        await asyncio.sleep(0.1)

        result, error = await manager.send_head(30, 50, 200)

        assert error is None
        assert result == {"ok": True, "yaw": 30, "pitch": 50, "speed": 200}
        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        msg = json.loads(raw)
        assert msg == {"type": "head", "yaw": 30, "pitch": 50, "speed": 200}


@pytest.mark.asyncio
async def test_ciniml_client_full_conversation_round_trip():
    """Simulate ciniml/stackchan-idf's XiaoZhiClient (components/conversation/
    xiaozhi_client.cpp) end to end against the real WebSocket handler.

    Message shapes are copied verbatim from the client source so this test
    catches protocol-dialect drift, not just handler wiring:
      - hello: ``send_hello()`` — ``features.mcp=false``, ``audio_params``
        16 kHz / 1 channel / 60 ms.
      - listen start: ``send_listen_start()``, sent only after the server
        hello (``handle_server_hello``), ``mode=auto`` (server-side VAD).
      - uplink: ``encode_and_send()`` ships raw Opus bytes as a WS binary
        frame with no framing header.
      - downlink: ``parse_control()`` / ``handle_tts()`` expect ``stt`` /
        ``llm`` / ``tts`` JSON plus raw Opus binary, with the ``tts`` state
        sequence start -> sentence_start -> stop.
      - abort: ``cancel_response()`` sends ``{type, reason, session_id}``.
    """

    class ScriptedCloudProxy:
        """Fake voice backend that replays one realistic conversation turn.

        Stands in for GeminiVoiceProxy/CloudProxy so this test only
        exercises the device-facing dialect in esp32_client.py, not a real
        upstream voice backend (already covered by test_gemini_voice_proxy.py
        / test_cloud_proxy.py).
        """

        def __init__(self) -> None:
            self.connected = True
            self.device_hello: dict | None = None
            self.uplink_frames: list[bytes] = []
            self.json_from_device: list[dict] = []
            self._send_to_device = None

        async def start(self, info, device_hello, send_to_device):
            self.device_hello = device_hello
            self._send_to_device = send_to_device
            return True

        async def stop(self) -> None:
            pass

        async def try_handle_device_mcp(self, message: dict) -> bool:
            return False

        async def send_device_json(self, message: dict) -> bool:
            self.json_from_device.append(message)
            return True

        async def send_device_binary(self, data: bytes) -> bool:
            self.uplink_frames.append(data)
            # One scripted conversation turn, xiaozhi dialect.
            await self._send_to_device(json.dumps({"type": "stt", "text": "你好"}))
            await self._send_to_device(json.dumps({"type": "llm", "emotion": "happy"}))
            await self._send_to_device(json.dumps({"type": "tts", "state": "start"}))
            await self._send_to_device(
                json.dumps({"type": "tts", "state": "sentence_start", "text": "你好呀"})
            )
            await self._send_to_device(b"\x4f\x50\x55\x53-fake-downlink-opus")
            await self._send_to_device(json.dumps({"type": "tts", "state": "stop"}))
            return True

    proxy = ScriptedCloudProxy()
    mgr = ESP32Manager(cloud_proxy_factory=lambda: proxy)
    await mgr.start("127.0.0.1", 0)
    port = mgr._server.sockets[0].getsockname()[1]

    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            # 1. hello — xiaozhi_client.cpp Impl::send_hello().
            await ws.send(json.dumps({
                "type": "hello",
                "version": 1,
                "transport": "websocket",
                "features": {"mcp": False},
                "audio_params": {
                    "format": "opus",
                    "sample_rate": 16000,
                    "channels": 1,
                    "frame_duration": 60,
                },
            }))

            # 2. server hello — handle_server_hello() reads session_id and
            # audio_params.sample_rate before opening the downlink decoder.
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            assert resp["type"] == "hello"
            session_id = resp["session_id"]
            assert session_id

            # An mcp=false device never receives an MCP initialize request.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(ws.recv(), timeout=0.2)

            # 3. listen start (mode=auto) — Impl::send_listen_start(), fired
            # only after the server hello lands.
            await ws.send(json.dumps({
                "session_id": session_id,
                "type": "listen",
                "state": "start",
                "mode": "auto",
            }))

            # 4. uplink — raw Opus bytes, no framing header
            # (Impl::encode_and_send -> esp_websocket_client_send_bin).
            await ws.send(b"\x00\x11\x22raw-uplink-opus")

            # 5. downlink control + audio, xiaozhi dialect
            # (Impl::parse_control / Impl::handle_tts).
            stt = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            assert stt == {"type": "stt", "text": "你好"}
            llm = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            assert llm == {"type": "llm", "emotion": "happy"}
            tts_start = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            assert tts_start == {"type": "tts", "state": "start"}
            sentence = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            assert sentence == {"type": "tts", "state": "sentence_start", "text": "你好呀"}
            audio = await asyncio.wait_for(ws.recv(), timeout=5.0)
            assert isinstance(audio, bytes) and audio
            tts_stop = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            assert tts_stop == {"type": "tts", "state": "stop"}

            # 6. abort / barge-in — Impl::cancel_response().
            await ws.send(json.dumps({
                "type": "abort",
                "reason": "wake_word_detected",
                "session_id": session_id,
            }))
            await asyncio.sleep(0.1)

            assert proxy.device_hello is not None
            assert proxy.device_hello["features"]["mcp"] is False
            assert proxy.uplink_frames == [b"\x00\x11\x22raw-uplink-opus"]
            assert proxy.json_from_device[-1] == {
                "type": "abort",
                "reason": "wake_word_detected",
                "session_id": session_id,
            }
            assert mgr.device_connected is True
            status = mgr.get_status()
            assert status["mcp_supported"] is False
            assert status["initialized"] is False
    finally:
        await mgr.stop()


@pytest.mark.asyncio
async def test_esp32_server_hello_waits_for_cloud_proxy_start():
    """Hello is not sent before voice start begins.

    A still-blocked start used to hold the read loop until the upstream
    socket was ready. The handler now waits only a short budget; this test
    still checks that hello is not flushed in the first instant, and that
    releasing a fast start lets hello through. The slow-start case is
    covered separately.
    """

    class DelayedCloudProxy:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.connected = True

        async def start(self, info, device_hello, send_to_device):
            self.started.set()
            await self.release.wait()
            return True

        async def stop(self):
            pass

        async def try_handle_device_mcp(self, message):
            return False

        async def send_device_json(self, message):
            return True

        async def send_device_binary(self, data):
            return True

    proxy = DelayedCloudProxy()
    mgr = ESP32Manager(cloud_proxy_factory=lambda: proxy)
    await mgr.start("127.0.0.1", 0)
    port = mgr._server.sockets[0].getsockname()[1]

    try:
        async with websockets.connect(
            f"ws://127.0.0.1:{port}",
            additional_headers={"Authorization": "Bearer test-token"},
        ) as ws:
            await ws.send(json.dumps({
                "type": "hello",
                "version": 1,
                "features": {"mcp": True},
                "transport": "websocket",
            }))

            await asyncio.wait_for(proxy.started.wait(), timeout=1.0)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(ws.recv(), timeout=0.05)

            proxy.release.set()
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.0))
            assert resp["type"] == "hello"
    finally:
        await mgr.stop()


def test_websocket_keepalive_defaults_and_env(monkeypatch):
    """Device socket keepalive is explicit and at least 60s unless overridden."""
    monkeypatch.delenv("STACKCHAN_WS_PING_INTERVAL", raising=False)
    monkeypatch.delenv("STACKCHAN_WS_PING_TIMEOUT", raising=False)
    monkeypatch.delenv("STACKCHAN_WS_MAX_QUEUE", raising=False)
    options = websocket_server_options()
    assert options["ping_interval"] >= 60
    assert options["ping_timeout"] >= 60
    assert options["max_queue"] >= 32

    monkeypatch.setenv("STACKCHAN_WS_PING_INTERVAL", "90")
    monkeypatch.setenv("STACKCHAN_WS_PING_TIMEOUT", "0")
    monkeypatch.setenv("STACKCHAN_WS_MAX_QUEUE", "32")
    options = websocket_server_options()
    assert options["ping_interval"] == 90
    assert options["ping_timeout"] is None
    assert options["max_queue"] == 32


@pytest.mark.asyncio
async def test_serve_passes_keepalive_options(monkeypatch):
    captured: dict = {}

    class _Server:
        def close(self):
            return None

        async def wait_closed(self):
            return None

    async def fake_serve(*args, **kwargs):
        captured["kwargs"] = kwargs
        return _Server()

    monkeypatch.delenv("STACKCHAN_WS_PING_INTERVAL", raising=False)
    monkeypatch.delenv("STACKCHAN_WS_PING_TIMEOUT", raising=False)
    monkeypatch.delenv("STACKCHAN_WS_MAX_QUEUE", raising=False)
    monkeypatch.setattr("stackchan_mcp.esp32_client.websockets.serve", fake_serve)
    mgr = ESP32Manager()
    await mgr.start("127.0.0.1", 0)
    await mgr.stop()
    assert captured["kwargs"]["ping_interval"] == 60
    assert captured["kwargs"]["ping_timeout"] == 60
    assert captured["kwargs"]["max_queue"] == 128
    assert captured["kwargs"]["process_request"].__func__ is ESP32Manager._check_auth


def test_trim_pending_audio_drops_oldest_opus_and_keeps_json():
    pending = [
        ("bin", b"old"),
        ("json", {"type": "listen"}),
        ("bin", b"new"),
    ]
    dropped = trim_pending_audio(pending, 1)
    assert dropped == 1
    assert pending == [("json", {"type": "listen"}), ("bin", b"new")]
    assert trim_pending_audio(pending, 0) == 0
    assert len(pending) == 2


def test_bracket_tts_payloads_adds_missing_stop():
    payloads = bracket_tts_payloads(
        [json.dumps({"type": "tts", "state": "start"}), b"opus"]
    )
    assert json.loads(payloads[0])["state"] == "start"
    assert payloads[1] == b"opus"
    assert json.loads(payloads[-1])["state"] == "stop"


@pytest.mark.asyncio
async def test_disconnect_logs_close_code_and_reason(manager, caplog):
    """A device close is logged with the code and reason, including 1011."""
    caplog.set_level(logging.INFO, logger="stackchan_mcp.esp32_client")
    port = manager._test_port
    ws = await websockets.connect(f"ws://127.0.0.1:{port}")
    await ws.close(1011, "keepalive ping timeout")
    for _ in range(40):
        if any(
            "1011" in record.message and "keepalive ping timeout" in record.message
            for record in caplog.records
        ):
            break
        await asyncio.sleep(0.05)
    else:
        messages = [record.message for record in caplog.records]
        pytest.fail(f"close code/reason missing from logs: {messages}")


@pytest.mark.asyncio
async def test_hello_does_not_wait_out_slow_voice_start(monkeypatch):
    """Hello returns while voice start is still blocked, and early Opus is kept."""
    monkeypatch.setenv("STACKCHAN_HELLO_VOICE_WAIT_S", "0.05")

    class SlowProxy:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.frames: list[bytes] = []
            self.jsons: list[dict] = []

        async def start(self, info, device_hello, send_to_device):
            self.started.set()
            await self.release.wait()
            return True

        async def stop(self):
            return None

        async def try_handle_device_mcp(self, message):
            return False

        async def send_device_json(self, message):
            self.jsons.append(message)
            return True

        async def send_device_binary(self, data):
            self.frames.append(data)
            return True

    proxy = SlowProxy()
    mgr = ESP32Manager(cloud_proxy_factory=lambda: proxy)
    await mgr.start("127.0.0.1", 0)
    port = mgr._server.sockets[0].getsockname()[1]
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(json.dumps({
                "type": "hello",
                "version": 1,
                "features": {"mcp": False},
                "transport": "websocket",
            }))
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.0))
            assert resp["type"] == "hello"
            assert not proxy.release.is_set()

            await ws.send(json.dumps({"type": "listen", "state": "start", "mode": "auto"}))
            await ws.send(b"frame-a")
            await ws.send(b"frame-b")
            await asyncio.sleep(0.05)
            assert proxy.frames == []
            assert proxy.jsons == []

            proxy.release.set()
            for _ in range(40):
                if proxy.frames == [b"frame-a", b"frame-b"] and proxy.jsons:
                    break
                await asyncio.sleep(0.05)
            assert proxy.jsons[0]["type"] == "listen"
            assert proxy.frames == [b"frame-a", b"frame-b"]
    finally:
        proxy.release.set()
        await mgr.stop()


@pytest.mark.asyncio
async def test_undelivered_tts_replays_after_reconnect():
    """Audio the closed socket never accepted is sent on the next hello."""

    class HoldingProxy:
        def __init__(self, payloads):
            self._payloads = payloads

        async def start(self, info, device_hello, send_to_device):
            return True

        async def stop(self):
            return None

        def take_undelivered_tts(self):
            payloads, self._payloads = self._payloads, []
            return payloads

        async def try_handle_device_mcp(self, message):
            return False

        async def send_device_json(self, message):
            return True

        async def send_device_binary(self, data):
            return True

    created = {"n": 0}

    def factory():
        created["n"] += 1
        if created["n"] == 1:
            return HoldingProxy([
                json.dumps({"type": "tts", "state": "start"}),
                b"\x01\x02opus",
            ])
        return HoldingProxy([])

    mgr = ESP32Manager(cloud_proxy_factory=factory)
    await mgr.start("127.0.0.1", 0)
    port = mgr._server.sockets[0].getsockname()[1]
    headers = {"Device-Id": "desk-1"}
    hello = {
        "type": "hello",
        "version": 1,
        "features": {"mcp": False},
        "transport": "websocket",
    }
    try:
        async with websockets.connect(
            f"ws://127.0.0.1:{port}",
            additional_headers=headers,
        ) as ws:
            await ws.send(json.dumps(hello))
            first = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert first["type"] == "hello"
        for _ in range(40):
            if mgr._undelivered_tts is not None:
                break
            await asyncio.sleep(0.05)
        assert mgr._undelivered_tts is not None

        async with websockets.connect(
            f"ws://127.0.0.1:{port}",
            additional_headers=headers,
        ) as ws:
            await ws.send(json.dumps(hello))
            seen = []
            deadline = asyncio.get_running_loop().time() + 2.0
            while asyncio.get_running_loop().time() < deadline and len(seen) < 4:
                seen.append(await asyncio.wait_for(ws.recv(), timeout=2.0))
            kinds = []
            for item in seen:
                if isinstance(item, bytes):
                    kinds.append(item)
                else:
                    kinds.append(json.loads(item).get("type") if json.loads(item).get("type") != "tts"
                                 else json.loads(item).get("state"))
            assert "hello" in kinds
            assert "start" in kinds
            assert b"\x01\x02opus" in kinds
            assert "stop" in kinds
    finally:
        await mgr.stop()


@pytest.mark.asyncio
async def test_esp32_tool_call_relay(manager):
    """Gateway relays tool calls to ESP32."""
    port = manager._test_port

    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        # Complete handshake
        await _complete_handshake(ws, tools=[
            {"name": "self.robot.set_head_angles", "description": "Set head", "inputSchema": {}}
        ])

        await asyncio.sleep(0.2)

        # Now call tool via manager
        call_task = asyncio.create_task(
            manager.call_tool("self.robot.set_head_angles", {"yaw": 45, "pitch": 10})
        )

        # ESP32 receives the request
        req_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        req_msg = json.loads(req_raw)
        assert req_msg["type"] == "mcp"
        assert req_msg["payload"]["method"] == "tools/call"
        assert req_msg["payload"]["params"]["name"] == "self.robot.set_head_angles"
        assert req_msg["payload"]["params"]["arguments"] == {"yaw": 45, "pitch": 10}

        # ESP32 sends response
        tool_resp = {
            "session_id": req_msg["session_id"],
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "id": req_msg["payload"]["id"],
                "result": {
                    "content": [{"type": "text", "text": "true"}],
                    "isError": False,
                },
            },
        }
        await ws.send(json.dumps(tool_resp))

        # Verify result
        result, error = await asyncio.wait_for(call_task, timeout=5.0)
        assert error is None
        assert result["content"][0]["text"] == "true"


@pytest.mark.asyncio
async def test_external_hook_client_can_relay_tool_call(manager):
    """Hook WebSocket clients can send MCP tool calls without replacing ESP32."""
    port = manager._test_port

    async with websockets.connect(f"ws://127.0.0.1:{port}") as device_ws:
        await _complete_handshake(device_ws, tools=[
            {"name": "self.display.set_avatar", "description": "Set avatar", "inputSchema": {}}
        ])
        await asyncio.sleep(0.2)

        async with websockets.connect(f"ws://127.0.0.1:{port}") as hook_ws:
            await hook_ws.send(json.dumps({
                "session_id": "hook-test",
                "type": "mcp",
                "payload": {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {
                        "name": "self.display.set_avatar",
                        "arguments": {"face": "happy"},
                    },
                },
            }))

            req_raw = await asyncio.wait_for(device_ws.recv(), timeout=5.0)
            req_msg = json.loads(req_raw)
            assert req_msg["payload"]["method"] == "tools/call"
            assert req_msg["payload"]["params"]["name"] == "self.display.set_avatar"
            assert req_msg["payload"]["params"]["arguments"] == {"face": "happy"}

            await device_ws.send(json.dumps({
                "session_id": req_msg["session_id"],
                "type": "mcp",
                "payload": {
                    "jsonrpc": "2.0",
                    "id": req_msg["payload"]["id"],
                    "result": {
                        "content": [{"type": "text", "text": "ok"}],
                        "isError": False,
                    },
                },
            }))

            response = json.loads(await asyncio.wait_for(hook_ws.recv(), timeout=5.0))
            assert response["session_id"] == "hook-test"
            assert response["payload"]["id"] == 7
            assert response["payload"]["result"]["content"][0]["text"] == "ok"
            assert manager.device_connected is True


@pytest.mark.asyncio
async def test_external_hook_client_uses_usb_when_ws_device_is_down():
    """Attach-mode MCP calls should still work through USB when WiFi is down."""
    calls = []

    class FakeUSB:
        connected = True

        async def call_tool(self, name, arguments, *, timeout_s):
            calls.append((name, arguments, timeout_s))
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [{"type": "text", "text": "usb-ok"}],
                    "isError": False,
                },
            }

    class FakeHookWebSocket:
        def __init__(self):
            self.sent = []

        async def send(self, data):
            self.sent.append(json.loads(data))

    manager = ESP32Manager(usb_transport=FakeUSB())
    hook_ws = FakeHookWebSocket()

    await manager._handle_external_mcp_request(
        hook_ws,
        {
            "session_id": "hook-usb",
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {
                    "name": "self.display.set_avatar",
                    "arguments": {"face": "happy"},
                },
            },
        },
        session_id="fallback",
    )

    assert calls == [("self.display.set_avatar", {"face": "happy"}, 2.0)]
    assert hook_ws.sent[0]["session_id"] == "hook-usb"
    assert hook_ws.sent[0]["payload"]["id"] == 9
    assert hook_ws.sent[0]["payload"]["result"]["content"][0]["text"] == "usb-ok"


@pytest.mark.asyncio
async def test_external_hook_client_get_status_reports_usb_when_ws_is_down():
    """Attach-mode get_status should not depend on tools/list over USB."""

    class FakeUSB:
        connected = True

    class FakeHookWebSocket:
        def __init__(self):
            self.sent = []

        async def send(self, data):
            self.sent.append(json.loads(data))

    manager = ESP32Manager(usb_transport=FakeUSB())
    hook_ws = FakeHookWebSocket()

    await manager._handle_external_mcp_request(
        hook_ws,
        {
            "session_id": "hook-status",
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {"name": "get_status", "arguments": {}},
            },
        },
        session_id="fallback",
    )

    result = hook_ws.sent[0]["payload"]["result"]
    status = json.loads(result["content"][0]["text"])
    assert status["connected"] is True
    assert status["ws_connected"] is False
    assert status["usb_connected"] is True
    assert status["transport"] == "usb"


@pytest.mark.asyncio
async def test_external_hook_client_calls_local_tool_handler():
    """Attach-mode gateway-local tools should not be relayed to firmware."""
    calls = []

    async def local_tool_handler(name, arguments):
        calls.append((name, arguments))
        return {"ok": True, "enabled": arguments["enabled"]}

    class FakeHookWebSocket:
        def __init__(self):
            self.sent = []

        async def send(self, data):
            self.sent.append(json.loads(data))

    manager = ESP32Manager(local_tool_handler=local_tool_handler)
    hook_ws = FakeHookWebSocket()

    await manager._handle_external_mcp_request(
        hook_ws,
        {
            "session_id": "hook-local",
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {"name": "set_voice_mode", "arguments": {"enabled": True}},
            },
        },
        session_id="fallback",
    )

    assert calls == [("set_voice_mode", {"enabled": True})]
    result = hook_ws.sent[0]["payload"]["result"]
    assert json.loads(result["content"][0]["text"]) == {"ok": True, "enabled": True}

    await manager._handle_external_mcp_request(
        hook_ws,
        {
            "session_id": "hook-local", "type": "mcp",
            "payload": {
                "jsonrpc": "2.0", "id": 12, "method": "tools/call",
                "params": {"name": "set_face_tracking", "arguments": {"enabled": False}},
            },
        },
        session_id="fallback",
    )
    assert calls[-1] == ("set_face_tracking", {"enabled": False})
    assert json.loads(hook_ws.sent[-1]["payload"]["result"]["content"][0]["text"]) == {
        "ok": True, "enabled": False,
    }


@pytest.mark.asyncio
async def test_external_hook_client_can_call_speak(manager, monkeypatch):
    """Hook WebSocket clients can trigger local speak streaming."""
    port = manager._test_port

    async def fake_speak_text(
        send_to_device,
        text,
        *,
        emotion=None,
        set_avatar=None,
        cloud_proxy=None,
        session_id="",
        require_cloud=False,
    ):
        assert require_cloud is False
        await send_to_device(json.dumps({"type": "tts", "state": "start"}))
        await send_to_device(b"opus")
        await send_to_device(json.dumps({"type": "tts", "state": "stop"}))
        return SpeakResult(ok=True, text=text, emotion=emotion, frames_sent=1, provider="fake")

    import stackchan_mcp.esp32_client as esp32_client

    monkeypatch.setattr(esp32_client, "speak_text", fake_speak_text)

    async with websockets.connect(f"ws://127.0.0.1:{port}") as device_ws:
        await _complete_handshake(device_ws, tools=[])
        await asyncio.sleep(0.2)

        async with websockets.connect(f"ws://127.0.0.1:{port}") as hook_ws:
            await hook_ws.send(json.dumps({
                "session_id": "hook-speak",
                "type": "mcp",
                "payload": {
                    "jsonrpc": "2.0",
                    "id": 8,
                    "method": "tools/call",
                    "params": {"name": "speak", "arguments": {"text": "你好", "emotion": "happy"}},
                },
            }))

            assert json.loads(await asyncio.wait_for(device_ws.recv(), timeout=5.0)) == {
                "type": "tts",
                "state": "start",
            }
            assert await asyncio.wait_for(device_ws.recv(), timeout=5.0) == b"opus"
            assert json.loads(await asyncio.wait_for(device_ws.recv(), timeout=5.0)) == {
                "type": "tts",
                "state": "stop",
            }

            response = json.loads(await asyncio.wait_for(hook_ws.recv(), timeout=5.0))
            assert response["payload"]["id"] == 8
            assert response["payload"]["result"]["ok"] is True
            assert response["payload"]["result"]["emotion"] == "happy"


@pytest.mark.asyncio
async def test_esp32_disconnect_handling(manager):
    """Manager handles ESP32 disconnection gracefully."""
    port = manager._test_port

    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await _complete_handshake(ws)
        await asyncio.sleep(0.2)
        assert manager.device_connected is True

    # Connection closed
    await asyncio.sleep(0.2)
    assert manager.device_connected is False


@pytest.mark.asyncio
async def test_replacing_speaking_device_clears_old_tts_without_erasing_new_speech():
    """A stale WebSocket cannot leave tracking paused or clear its replacement's TTS."""
    from stackchan_mcp.debug_status import DebugStatus

    status = DebugStatus()
    mgr = ESP32Manager(debug_status=status)
    await mgr.start("127.0.0.1", 0)
    port = mgr._server.sockets[0].getsockname()[1]
    hello = json.dumps({"type": "hello", "version": 1, "features": {"mcp": False}})

    async def wait_for_tts(active):
        async def check():
            while status.tts_active != active:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(check(), timeout=1.0)

    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as old:
            await old.send(hello)
            await asyncio.wait_for(old.recv(), timeout=1.0)
            await old.send(json.dumps({"type": "tts", "state": "start"}))
            await wait_for_tts(True)
            await mgr.send_tts_state("start")  # local say also belongs to the old socket
            assert status._tts_sources == {"device", "local"}
            async with websockets.connect(f"ws://127.0.0.1:{port}") as new:
                await new.send(hello)
                await asyncio.wait_for(new.recv(), timeout=1.0)
                await wait_for_tts(False)
                assert status._tts_sources == set()
                await old.send(json.dumps({"type": "tts", "state": "start"}))
                await asyncio.sleep(0.05)
                assert status.tts_active is False
                await new.send(json.dumps({"type": "tts", "state": "start"}))
                await wait_for_tts(True)
                # The old socket can still have queued events; none owns the new device.
                await old.send(json.dumps({"type": "tts", "state": "stop"}))
                await old.close()
                await asyncio.sleep(0.05)
                assert status.tts_active is True
                assert status._tts_sources == {"device"}
                await new.send(json.dumps({"type": "tts", "state": "stop"}))
                await wait_for_tts(False)
    finally:
        await mgr.stop()


@pytest.mark.asyncio
async def test_auth_rejection(manager):
    """Unauthorized connections are rejected."""
    import os
    port = manager._test_port

    # Set token to require auth
    os.environ["STACKCHAN_TOKEN"] = "test-secret-token"
    try:
        # Try connecting without auth — should fail
        with pytest.raises(Exception):
            async with websockets.connect(
                f"ws://127.0.0.1:{port}",
                additional_headers={"Authorization": "Bearer wrong-token"},
            ) as ws:
                await ws.recv()
    finally:
        del os.environ["STACKCHAN_TOKEN"]


# ---------------------------------------------------------------------------
# send_audio_frame (TTS pipeline egress, Issue #70 PR2)
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    """Minimal stand-in for websockets.ServerConnection used in unit tests."""

    def __init__(self) -> None:
        self.sent: list[bytes | str] = []

    async def send(self, data):
        self.sent.append(data)


@pytest.mark.asyncio
async def test_connection_send_audio_frame_sends_binary():
    """ESP32Connection.send_audio_frame writes the bytes to the underlying WS."""
    ws = _FakeWebSocket()
    conn = ESP32Connection(ws, session_id="session-1")  # type: ignore[arg-type]

    await conn.send_audio_frame(b"opus_payload_bytes")

    assert ws.sent == [b"opus_payload_bytes"]


@pytest.mark.asyncio
async def test_connection_send_led_sends_control_json_and_clamps_channels():
    """ESP32Connection.send_led writes the led control frame directly."""
    ws = _FakeWebSocket()
    conn = ESP32Connection(ws, session_id="session-led")  # type: ignore[arg-type]

    result = await conn.send_led(-1, 300, 128)

    assert result == {"ok": True, "r": 0, "g": 255, "b": 128}
    assert len(ws.sent) == 1
    assert json.loads(ws.sent[0]) == {"type": "led", "r": 0, "g": 255, "b": 128}


@pytest.mark.asyncio
async def test_connection_send_head_sends_control_json_and_clamps_values():
    """ESP32Connection.send_head writes the head control frame directly."""
    ws = _FakeWebSocket()
    conn = ESP32Connection(ws, session_id="session-head")  # type: ignore[arg-type]

    result = await conn.send_head(-100, 99, 20)

    assert result == {"ok": True, "yaw": -90, "pitch": 60, "speed": 100}
    assert len(ws.sent) == 1
    assert json.loads(ws.sent[0]) == {
        "type": "head",
        "yaw": -90,
        "pitch": 60,
        "speed": 100,
    }


@pytest.mark.asyncio
async def test_connection_send_audio_frame_raises_after_disconnect():
    """A disconnected connection refuses to send rather than silently dropping."""
    ws = _FakeWebSocket()
    conn = ESP32Connection(ws, session_id="session-1")  # type: ignore[arg-type]

    conn.disconnect()

    with pytest.raises(ConnectionError):
        await conn.send_audio_frame(b"opus_payload_bytes")
    assert ws.sent == []


@pytest.mark.asyncio
async def test_manager_send_audio_frame_no_device():
    """ESP32Manager.send_audio_frame raises when no device is attached.

    The orchestrator turns this into a clean MCP error JSON; without
    this guard the call would AttributeError on a None connection.
    """
    mgr = ESP32Manager()

    with pytest.raises(ConnectionError):
        await mgr.send_audio_frame(b"opus_payload_bytes")


@pytest.mark.asyncio
async def test_manager_send_led_no_device_returns_error():
    """ESP32Manager.send_led reports offline without raising."""
    mgr = ESP32Manager()

    result, error = await mgr.send_led(0, 180, 180)

    assert result is None
    assert error is not None
    assert error["message"] == "No ESP32 device connected"


@pytest.mark.asyncio
async def test_manager_send_head_no_device_returns_error():
    """ESP32Manager.send_head reports offline without raising."""
    mgr = ESP32Manager()

    result, error = await mgr.send_head(0, 0, 150)

    assert result is None
    assert error is not None
    assert error["message"] == "No ESP32 device connected"


@pytest.mark.asyncio
async def test_connection_send_tts_state_sends_json():
    """ESP32Connection.send_tts_state writes a tts state JSON message."""
    ws = _FakeWebSocket()
    conn = ESP32Connection(ws, session_id="session-tts")  # type: ignore[arg-type]

    await conn.send_tts_state("start")

    assert len(ws.sent) == 1
    payload = json.loads(ws.sent[0])
    assert payload == {
        "session_id": "session-tts",
        "type": "tts",
        "state": "start",
    }


@pytest.mark.asyncio
async def test_connection_send_tts_state_raises_after_disconnect():
    """A disconnected connection refuses to send TTS notifications."""
    ws = _FakeWebSocket()
    conn = ESP32Connection(ws, session_id="session-tts")  # type: ignore[arg-type]

    conn.disconnect()

    with pytest.raises(ConnectionError):
        await conn.send_tts_state("stop")
    assert ws.sent == []


@pytest.mark.asyncio
async def test_manager_send_tts_state_no_device():
    """ESP32Manager.send_tts_state raises when no device is attached."""
    mgr = ESP32Manager()

    with pytest.raises(ConnectionError):
        await mgr.send_tts_state("start")


class _FailingWebSocket:
    """WebSocket that raises a websockets-specific error on send()."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.send_calls = 0

    async def send(self, data):
        self.send_calls += 1
        raise self._exc


@pytest.mark.asyncio
async def test_send_audio_frame_translates_websockets_close_to_connection_error():
    """websockets.ConnectionClosed becomes ConnectionError + marks dead.

    Without translation the websockets-specific exception would
    bypass the orchestrator's ``except ConnectionError`` filter and
    leak as a stack trace through the MCP transport.
    """
    import websockets.exceptions

    closed = websockets.exceptions.ConnectionClosed(rcvd=None, sent=None)
    ws = _FailingWebSocket(closed)
    conn = ESP32Connection(ws, session_id="session-1")  # type: ignore[arg-type]

    with pytest.raises(ConnectionError, match="WebSocket send"):
        await conn.send_audio_frame(b"opus")

    # After the translated failure, the connection is marked dead so
    # subsequent sends fail fast without re-touching the dead socket.
    assert not conn.connected
    with pytest.raises(ConnectionError):
        await conn.send_audio_frame(b"more")
    assert ws.send_calls == 1


@pytest.mark.asyncio
async def test_send_tts_state_translates_oserror_to_connection_error():
    """OSError on send (e.g. broken pipe) is translated to ConnectionError."""
    ws = _FailingWebSocket(OSError("broken pipe"))
    conn = ESP32Connection(ws, session_id="session-1")  # type: ignore[arg-type]

    with pytest.raises(ConnectionError, match="WebSocket send"):
        await conn.send_tts_state("start")
    assert not conn.connected


def test_connection_default_protocol_version_is_one():
    """Fresh ESP32Connection defaults to WebSocket protocol v1.

    v1 is what the gateway's audio framing currently targets (raw
    Opus binary frames). v2/v3 wrap payloads in a BinaryProtocol
    header which this gateway does not yet emit; the hello handler
    logs a warning when a non-v1 device negotiates so operators know
    the TTS path may not work for them.
    """
    ws = _FakeWebSocket()
    conn = ESP32Connection(ws, session_id="session-1")  # type: ignore[arg-type]

    assert conn.protocol_version == 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _complete_handshake(ws, tools=None):
    """Complete the full ESP32 handshake sequence."""
    if tools is None:
        tools = []

    # Send hello
    hello = {
        "type": "hello",
        "version": 1,
        "features": {"mcp": True},
        "transport": "websocket",
    }
    await ws.send(json.dumps(hello))

    # Receive hello response
    await asyncio.wait_for(ws.recv(), timeout=5.0)

    # Receive and respond to initialize
    init_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
    init_msg = json.loads(init_raw)
    init_resp = {
        "session_id": init_msg["session_id"],
        "type": "mcp",
        "payload": {
            "jsonrpc": "2.0",
            "id": init_msg["payload"]["id"],
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "test-device", "version": "1.0.0"},
            },
        },
    }
    await ws.send(json.dumps(init_resp))

    # Receive and respond to tools/list
    tools_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
    tools_msg = json.loads(tools_raw)
    tools_resp = {
        "session_id": tools_msg["session_id"],
        "type": "mcp",
        "payload": {
            "jsonrpc": "2.0",
            "id": tools_msg["payload"]["id"],
            "result": {"tools": tools, "nextCursor": ""},
        },
    }
    await ws.send(json.dumps(tools_resp))
