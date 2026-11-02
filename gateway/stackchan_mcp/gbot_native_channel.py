# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Filtered local-helper channel for native Grok Bot presence and voice updates."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from urllib.parse import urlencode

import aiohttp

logger = logging.getLogger(__name__)


def update_event(message, agent_id, call_id):
    if not isinstance(message, dict) or not isinstance(message.get("payload"), dict):
        return None
    payload = message["payload"]
    if payload.get("agentId") != agent_id or payload.get("callId") != call_id:
        return None
    channel = message.get("channel")
    if channel == "voice-call-nudge" and payload.get("kind") == "outcome":
        text, topic = payload.get("update"), "work_landed"
        event_id = "seeded-nudge-" + str(uuid.uuid4())
        timestamp = int(time.time() * 1000)
        is_user = False
    elif channel == "typed-message":
        text, topic = payload.get("text"), "user_message"
        entry_id = payload.get("entryId")
        timestamp = payload.get("atMs")
        if (
            not isinstance(entry_id, str)
            or not 0 < len(entry_id) <= 200
            or type(timestamp) not in {int, float}
            or not math.isfinite(timestamp)
            or timestamp <= 0
        ):
            return None
        event_id, is_user = "written-turn-" + entry_id, True
    else:
        return None  # Overheard is a different native protocol, not a completed task.
    if not isinstance(text, str) or not text.strip() or len(text) > 64000:
        return None
    return (
        {
            "type": "async_update.add",
            "event_id": event_id,
            "async_update": {
                "topic": topic,
                "payload": [
                    {
                        "message": {"type": "text", "content": text},
                        "timestampMs": timestamp,
                    }
                ],
            },
        },
        is_user,
    )


class NativeVoiceChannel:
    def __init__(self, http, base_url, agent_id, call_id, on_update):
        self._http, self._url = http, base_url
        self.agent_id, self.call_id = agent_id, call_id
        self._on_update = on_update
        self._task = None
        self._closed = False
        self._registered = False
        self._presence_owned = False
        self._retry_delay = 5.0
        self._typed_seen = set()
        self.connected = asyncio.Event()

    async def _presence(self, on):
        async with self._http.post(
            self._url + "/native/presence",
            json={
                "agentId": self.agent_id,
                "callId": self.call_id,
                "isOnTheLine": on,
                "acceptsOverheard": False,
            },
            allow_redirects=False,
        ) as response:
            logger.info("Grok native presence on=%s status=%d", on, response.status)
            if response.status != 200:
                raise RuntimeError("native_presence_unavailable")
            result = await response.json()
            if result.get("ok") is False:
                raise RuntimeError("native_presence_rejected")

    @property
    def ready(self):
        return not self._closed and self._registered and self.connected.is_set()

    async def start(self):
        # Native sandbox availability must not hold the independent audio socket.
        self._task = asyncio.create_task(self._run(), name="gbot-native-events")

    async def _run(self):
        while not self._closed:
            try:
                await self._presence(True)
                self._presence_owned = True
                self._registered = True
                await self._listen()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Grok native channel state=unavailable retry=pending")
            finally:
                self._registered = False
                self.connected.clear()
            if not self._closed:
                await asyncio.sleep(self._retry_delay)

    async def accept_event(self, message):
        parsed = update_event(message, self.agent_id, self.call_id)
        if self._closed or parsed is None:
            return
        event, is_user = parsed
        if is_user:
            if event["event_id"] in self._typed_seen or len(self._typed_seen) >= 2048:
                return
            self._typed_seen.add(event["event_id"])
        await self._on_update(event, is_user)

    async def _listen(self):
        query = urlencode({"agentId": self.agent_id, "callId": self.call_id})
        async with self._http.get(
            self._url + "/native/events?" + query,
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=60),
        ) as response:
            if response.status != 200:
                raise RuntimeError("native_events_unavailable")
            self.connected.set()
            logger.info("Grok native channel state=ready")
            data, length = [], 0
            async for raw in response.content:
                if self._closed:
                    break
                line = raw.decode("utf-8").rstrip("\r\n")
                if not line:
                    if data:
                        await self.accept_event(json.loads("\n".join(data)))
                    data, length = [], 0
                elif line.startswith("data:"):
                    length += len(line)
                    if length > 70000:
                        raise ValueError("native_event_too_large")
                    data.append(line[5:].lstrip())

    async def close(self):
        self._closed = True
        self.connected.clear()
        if self._task and self._task is not asyncio.current_task():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._registered = False
        if self._presence_owned:
            self._presence_owned = False
            try:
                await asyncio.wait_for(self._presence(False), 3)
            except Exception:
                pass
