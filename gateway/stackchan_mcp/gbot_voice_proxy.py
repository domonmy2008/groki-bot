# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Opt-in Grok Bot voice, retaining the established device audio transport."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import time

from .device_emotion import DEAD_LED_RGB
from .gbot_voice_bridge import GbotVoiceBridge
from .gemini_voice_proxy import GeminiVoiceProxy

logger = logging.getLogger(__name__)


@dataclass
class GbotVoiceProxy(GeminiVoiceProxy):
    health_check_interval_s: float = 30.0
    health_check_timeout_s: float = 10.0
    connect_timeout_s: float = 40.0
    reconnect_initial_delay_s: float = 1.0
    reconnect_max_delay_s: float = 30.0
    # After end_the_call the wake gate is DORMANT; the voice WS can die quietly.
    # KWS OPEN must wait for supervisor reconnect instead of feeding a dead socket
    # (which would _handle_session_dead -> CLOSE + red and feel "unwakeable").
    wake_reconnect_timeout_s: float = 15.0
    _health_task: asyncio.Task | None = field(default=None, init=False)
    _reconnect_nudge: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _health: dict = field(default_factory=lambda: {
        "provider": "gbot_voice", "state": "stopped", "reconnect_attempts": 0,
        "successful_checks": 0, "last_verified_at": None, "last_check_at": None,
        "last_error": None, "action_required": False,
    }, init=False)

    def health_snapshot(self):
        result = dict(self._health)
        if result["state"] == "connected" and not self.connected:
            result["state"] = "reconnecting"
        return result

    def _record_connection(self, connected, error=None):
        self._health["state"] = "connected" if connected else "reconnecting"
        self._health["last_error"] = error
        self._health["action_required"] = error in {
            "login_unavailable", "login_expired_open_grok_bot", "unsupported_storage",
            "upstream_http_401", "upstream_http_403",
        }
        if connected:
            self._health["last_verified_at"] = time.time()
            # Successful supervisor reconnect clears sticky dead/red from a prior drop.
            self._reported_dead = False

    def _nudge_reconnect(self) -> None:
        """Wake the supervisor sleep so recovery can start immediately."""
        self._reconnect_nudge.set()

    async def _sleep_until_nudge(self, delay: float) -> None:
        self._reconnect_nudge.clear()
        try:
            await asyncio.wait_for(self._reconnect_nudge.wait(), timeout=max(0.0, delay))
        except asyncio.TimeoutError:
            pass

    async def start(self, info, device_hello, send_to_device):
        if self._health_task is not None:
            await self.stop()
        self._status.voice_health_provider = self.health_snapshot
        self._health["state"] = "connecting"
        try:
            connected = await self._try_connect(info, device_hello, send_to_device)
        except asyncio.CancelledError:
            await super().stop()
            self._health["state"] = "stopped"
            raise
        self._record_connection(connected, None if connected else self._health["last_error"])
        self._health_task = asyncio.create_task(
            self._supervise(info, device_hello, send_to_device), name="gbot-voice-health"
        )
        return connected

    async def _try_connect(self, info, device_hello, send_to_device):
        try:
            connected = await asyncio.wait_for(
                super().start(info, device_hello, send_to_device), self.connect_timeout_s
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Grok voice connection failed (details redacted)")
            connected = False
        if not connected:
            self._health["last_error"] = (
                getattr(self._bridge, "metrics", {}).get("connection_error")
                or "connection_failed"
            )
            await self._abort_in_flight_tts()
            await super().stop()
            self._stopped = False
        return connected

    async def _supervise(self, info, device_hello, send_to_device):
        delay = self.reconnect_initial_delay_s
        checked_at = asyncio.get_running_loop().time()
        disconnected_at = None
        while not self._stopped:
            if self.connected:
                disconnected_at = None
                await asyncio.sleep(min(1.0, self.health_check_interval_s))
                if self._stopped:
                    return
                now = asyncio.get_running_loop().time()
                if self.connected and now - checked_at < self.health_check_interval_s:
                    continue
                if self.connected:
                    self._health["last_check_at"] = time.time()
                    if await self._bridge.check_connection(self.health_check_timeout_s):
                        self._health["successful_checks"] += 1
                        self._record_connection(True)
                        checked_at = asyncio.get_running_loop().time()
                        delay = self.reconnect_initial_delay_s
                        continue
                    self._health["last_error"] = "voice_check_failed"
                    if self._bridge.metrics.get("connection_error") == "response_stalled":
                        self._health["last_error"] = "response_stalled"
            elif self.running:
                # A bridge may already be restoring its own transport/session.
                now = asyncio.get_running_loop().time()
                if disconnected_at is None:
                    disconnected_at = now
                if now - disconnected_at < self.connect_timeout_s:
                    await asyncio.sleep(min(1.0, self.health_check_interval_s))
                    continue
            self._record_connection(False, self._health["last_error"] or "connection_lost")
            await self._abort_in_flight_tts()
            await super().stop()
            # The supervisor owns recovery; explicit stop cancels this task.
            self._stopped = False
            await self._sleep_until_nudge(
                self.reconnect_max_delay_s if self._health["action_required"] else delay
            )
            if self._stopped:
                return
            self._health["reconnect_attempts"] += 1
            connected = await self._try_connect(info, device_hello, send_to_device)
            self._record_connection(connected, None if connected else self._health["last_error"])
            if connected:
                checked_at = asyncio.get_running_loop().time()
            delay = min(self.reconnect_max_delay_s, delay * 2)

    async def _prepare_for_wake(self) -> bool:
        """On KWS OPEN, refuse to listen into a dead Grok voice socket.

        Reuses the existing health supervisor: nudge its backoff sleep, then
        wait until ``connected`` (or timeout). Returning False makes the
        shared wake path close the gate instead of lighting cyan into a void.
        """
        if self.connected:
            return True
        timeout = max(0.0, float(self.wake_reconnect_timeout_s))
        logger.info(
            "wake OPEN while Grok voice unhealthy (state=%s error=%s); "
            "waiting up to %.1fs for supervisor reconnect",
            self._health.get("state"),
            self._health.get("last_error"),
            timeout,
        )
        self._nudge_reconnect()
        if timeout <= 0:
            return False
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if self._stopped:
                return False
            if self.connected:
                logger.info("wake OPEN: Grok voice reconnected; entering LISTENING")
                return True
            self._nudge_reconnect()
            await asyncio.sleep(0.1)
        logger.warning(
            "wake OPEN aborted: Grok voice still disconnected after %.1fs",
            timeout,
        )
        return False

    async def _handle_session_dead(self) -> None:
        """Voice WS ended. Close an active listen window; let supervisor recover.

        Unlike Gemini Live's audio-cache TTL path, Grok calls this on every
        unexpected receive exit — including silent death while DORMANT after
        end_the_call. Sticky red + CLOSE there is what feels like "cannot wake".
        """
        was_listening = False
        try:
            gate = self._wake_gate
            if gate is not None:
                was_listening = gate.close()
                if was_listening:
                    self._status.on_wake_closed()
        except Exception:
            logger.exception("session-dead wake gate close failed")
        try:
            await self._abort_in_flight_tts()
        except Exception:
            logger.exception("session-dead abort tts failed")
        self._nudge_reconnect()
        if was_listening:
            self._reported_dead = True
            try:
                await self._set_leds_safe(*DEAD_LED_RGB)
            except Exception:
                logger.exception("session-dead red LED failed")
            return
        # DORMANT silent death: no sticky red; supervisor reconnects quietly.
        self._reported_dead = False
        logger.info(
            "Grok voice session died while DORMANT; supervisor nudged to reconnect"
        )

    async def stop(self):
        task, self._health_task = self._health_task, None
        self._stopped = True
        self._nudge_reconnect()
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._abort_in_flight_tts()
        await super().stop()
        self._health["state"] = "stopped"

    def _voice_api_key(self):
        return None

    def _voice_requires_key(self):
        return False

    def _voice_requires_device(self):
        return False

    def _voice_hello(self):
        return {"provider": "gbot_voice", "experimental": True}

    def _speak_waits_for_first_audio(self):
        return True

    def _voice_tts_mode(self):
        return "native"

    def _make_bridge(self, esp32, **kwargs):
        return (self.bridge_factory or GbotVoiceBridge)(
            esp32=esp32,
            tool_executor=self.voice_tool_executor,
            tool_executor_factory=self.voice_tool_executor_factory,
            tool_extensions=self.voice_tool_extensions,
            **kwargs,
        )
