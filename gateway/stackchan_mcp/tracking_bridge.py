"""Face-tracking bridge that turns Vision detections into head motion."""

from __future__ import annotations

import logging
import asyncio
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class TrackingConfig:
    gain_x: float = 1.0
    gain_y: float = 1.0
    offset_x: float = 0.0
    offset_y: float = 0.0
    smoothing: float = 0.35
    deadzone: float = 0.04
    move_threshold: float = 2.0
    min_confidence: float = 0.5
    max_yaw: int = 60
    min_pitch: int = 0
    max_pitch: int = 45
    # dy<0 (face in the upper half) swings toward min_pitch; dy>0 toward max_pitch.
    # Using max_pitch as the full swing on both sides would saturate at y≈0.66.
    neutral_pitch: int = 15


class TrackingBridge:
    USB_SERVO_TIMEOUT_S = 0.08
    RESAMPLE_HZ = 50.0
    MIN_SEGMENT_S = 1.0 / RESAMPLE_HZ
    MAX_SEGMENT_S = 0.15
    STALE_TARGET_S = 0.4
    TRACKING_SPEED = 700

    def __init__(
        self,
        esp32: Any,
        *,
        config: TrackingConfig | None = None,
        usb_transport: Any | None = None,
        on_face_detected: Callable[[], None] | None = None,
        on_auto_head_command: Callable[[], None] | None = None,
        on_head_command: Callable[[], None] | None = None,
        is_tts_active: Callable[[], bool] | None = None,
        is_listening: Callable[[], bool] | None = None,
        wake_motion_allowed: Callable[[], bool] | None = None,
    ) -> None:
        self.esp32 = esp32
        self.config = config or TrackingConfig()
        self.enabled = True
        self._mode = "idle"
        self._is_tts_active = is_tts_active
        self._is_listening = is_listening
        # When set, face chase runs only while this returns true (LISTENING).
        # DORMANT and passthrough (no gate) should return false so the head
        # stays home. None keeps the historical "follow unless quiet" rule
        # for unit tests that do not install a wake policy.
        self._wake_motion_allowed = wake_motion_allowed
        neutral = float(self.config.neutral_pitch)
        self._yaw = self._output_yaw = self._segment_start_yaw = self._segment_end_yaw = self._cached_yaw = 0.0
        self._pitch = self._output_pitch = self._segment_start_pitch = self._segment_end_pitch = self._cached_pitch = neutral
        self._segment_start_s = 0.0
        self._segment_duration_s = self.MIN_SEGMENT_S
        self._last_detection_s: float | None = None
        self._last_detection_timestamp_s: float | None = None
        self._last_target_update_s: float | None = None
        self._last_sent_pose: tuple[int, int] | None = None
        self._resample_task: asyncio.Task[None] | None = None
        self._has_seen_face = False
        self.usb_transport = usb_transport
        self._on_face_detected = on_face_detected
        # Backward-compatible alias: tracking head writes are automatic
        # control, not manual user move_head commands.
        self._on_auto_head_command = on_auto_head_command or on_head_command

    @property
    def mode(self) -> str:
        if self._is_tts_active is not None and self._is_tts_active():
            return "working"
        if self._is_listening is not None and self._is_listening():
            return "quiet"
        return self._mode

    @mode.setter
    def mode(self, value: str) -> None:
        self._mode = value

    async def stop(self) -> None:
        if self._resample_task is None:
            return
        self._resample_task.cancel()
        try:
            await self._resample_task
        except asyncio.CancelledError:
            pass
        self._resample_task = None

    @property
    def last_position(self) -> tuple[int, int] | None:
        if not self._has_seen_face:
            return None
        return round(self._cached_yaw), round(self._cached_pitch)

    def _wake_follow_allowed(self) -> bool:
        """True when autonomous face chase may send a head command.

        A wake policy (gateway) allows chase only inside an open wake window.
        Without a policy, chase is allowed except in the legacy quiet mode.
        """
        policy = self._wake_motion_allowed
        if policy is None:
            return self.mode != "quiet"
        try:
            return bool(policy())
        except Exception:
            logger.debug("wake motion policy failed; holding the head still", exc_info=True)
            return False

    def _follow_blocked(self) -> bool:
        """Speaking, a closed wake gate, or no device: do not chase."""
        if not self.enabled or not self._device_reachable():
            return True
        if self.mode == "working":
            return True
        return not self._wake_follow_allowed()

    def _device_reachable(self) -> bool:
        usb = self.usb_transport
        if usb is not None and getattr(usb, "connected", False):
            return True
        return getattr(self.esp32, "device_connected", False)

    async def snap_to_last_position(self, *, speed: int = 250) -> bool:
        if not self._device_reachable():
            return False
        yaw, pitch = self.last_position or (0, 0)
        await self._send_head_angles({
            "yaw": max(-90, min(90, int(yaw))),
            "pitch": max(0, min(60, int(pitch))),
            "speed": max(100, min(1000, int(speed))),
        })
        return True

    async def _send_head_angles(self, args: dict[str, Any]) -> None:
        if self._on_auto_head_command is not None:
            self._on_auto_head_command()
        usb = self.usb_transport
        if usb is not None and getattr(usb, "connected", False):
            try:
                await usb.call_tool("self.robot.set_head_angles", args, timeout_s=self.USB_SERVO_TIMEOUT_S)
                return
            except Exception as exc:
                logger.warning("USB set_head_angles failed (%s); WS fallback", exc)
        await self._call_esp32_head(args)

    def _esp32_voice_only(self) -> bool:
        return getattr(self.esp32, "mcp_supported", True) is False

    async def _call_esp32_head(self, args: dict[str, Any]) -> None:
        send_head = getattr(self.esp32, "send_head", None)
        if callable(send_head) and self._esp32_voice_only():
            # Voice-only firmware rejects MCP set_head_angles; use the WS
            # ``head`` message, same fallback as Gemini's move_head.
            _result, error = await send_head(
                args["yaw"],
                args["pitch"],
                args["speed"],
                notify_activity=False,
                notify_head_command=False,
            )
            if error:
                logger.debug("WS head send failed: %s", error.get("message", error))
            return
        try:
            await self.esp32.call_tool(
                "self.robot.set_head_angles",
                args,
                notify_activity=False,
                notify_head_command=False,
            )
        except TypeError:
            # Unit fakes and older ESP32 manager surfaces only accept
            # (name, arguments). They have no IdleGate callback to suppress.
            await self.esp32.call_tool("self.robot.set_head_angles", args)

    def update_config(self, values: dict[str, Any]) -> dict[str, Any]:
        for key, value in values.items():
            if hasattr(self.config, key):
                setattr(self.config, key, float(value))
        return self.config_dict()

    def config_dict(self) -> dict[str, float]:
        return self.config.__dict__.copy()

    async def handle_detection(self, detection: dict[str, Any]) -> bool:
        confidence = float(detection.get("confidence", 0))
        if confidence < self.config.min_confidence:
            return False
        if self._on_face_detected is not None:
            self._on_face_detected()
        if self._follow_blocked():
            return False
        x = float(detection.get("x", 0.5))
        y = float(detection.get("y", 0.5))
        yaw, pitch = self._map_to_head(x, y)
        self._cached_yaw, self._cached_pitch = yaw, pitch
        self._has_seen_face = True
        if (
            abs(yaw - self._yaw) < self.config.move_threshold
            and abs(pitch - self._pitch) < self.config.move_threshold
        ):
            return False
        self._update_resample_target(
            self._smooth(self._yaw, yaw),
            self._smooth(self._pitch, pitch),
            detection=detection,
        )
        self._ensure_resample_task()
        return True

    def _update_resample_target(
        self,
        yaw: float,
        pitch: float,
        *,
        detection: dict[str, Any],
    ) -> None:
        now = asyncio.get_running_loop().time()
        current_yaw, current_pitch = self._interpolated_pose(now)
        self._output_yaw, self._output_pitch = current_yaw, current_pitch
        self._segment_start_yaw, self._segment_start_pitch = current_yaw, current_pitch
        self._segment_end_yaw, self._segment_end_pitch = yaw, pitch
        self._segment_start_s = now
        self._segment_duration_s = self._segment_duration(now, detection)
        self._last_target_update_s = now
        self._yaw, self._pitch = yaw, pitch

    def _segment_duration(self, now: float, detection: dict[str, Any]) -> float:
        try:
            ts: float | None = float(detection.get("timestamp"))
        except (TypeError, ValueError):
            ts = None
        if ts is not None and self._last_detection_timestamp_s is not None:
            dt = ts - self._last_detection_timestamp_s
        elif self._last_detection_s is not None:
            dt = now - self._last_detection_s
        else:
            dt = 1.0 / 30.0
        self._last_detection_s = now
        if ts is not None:
            self._last_detection_timestamp_s = ts
        if dt <= 0:
            dt = 1.0 / 30.0
        return max(self.MIN_SEGMENT_S, min(self.MAX_SEGMENT_S, dt))

    def _ensure_resample_task(self) -> None:
        if self._resample_task is not None and not self._resample_task.done():
            return
        self._resample_task = asyncio.create_task(self._resample_loop(), name="stackchan-face-tracking-resampler")

    async def _resample_loop(self) -> None:
        loop = asyncio.get_running_loop()
        interval = 1.0 / self.RESAMPLE_HZ
        next_tick = loop.time()
        try:
            while True:
                now = loop.time()
                if not await self._resample_tick(now):
                    break
                next_tick += interval
                await asyncio.sleep(max(0.0, next_tick - loop.time()))
        finally:
            self._resample_task = None

    async def _resample_tick(self, now: float | None = None) -> bool:
        if now is None:
            now = asyncio.get_running_loop().time()
        if (
            self._follow_blocked()
            or self._last_target_update_s is None
            or now - self._last_target_update_s > self.STALE_TARGET_S
        ):
            return False
        yaw, pitch = self._interpolated_pose(now)
        self._output_yaw, self._output_pitch = yaw, pitch
        pose = (round(yaw), round(pitch))
        if pose == self._last_sent_pose:
            return True
        self._last_sent_pose = pose
        await self._send_head_angles(
            {"yaw": pose[0], "pitch": pose[1], "speed": self.TRACKING_SPEED},
        )
        return True

    def _interpolated_pose(self, now: float) -> tuple[float, float]:
        if self._last_target_update_s is None:
            return self._output_yaw, self._output_pitch
        t = 1.0 if self._segment_duration_s <= 0 else (now - self._segment_start_s) / self._segment_duration_s
        t = max(0.0, min(1.0, t))
        yaw = self._segment_start_yaw + (self._segment_end_yaw - self._segment_start_yaw) * t
        pitch = self._segment_start_pitch + (self._segment_end_pitch - self._segment_start_pitch) * t
        return yaw, pitch

    def _map_to_head(self, x: float, y: float) -> tuple[float, float]:
        dx = x - 0.5
        dy = y - 0.5
        if abs(dx) < self.config.deadzone:
            dx = 0.0
        if abs(dy) < self.config.deadzone:
            dy = 0.0
        yaw = (dx * 2.0 * self.config.max_yaw * self.config.gain_x) + self.config.offset_x
        neutral = float(self.config.neutral_pitch)
        swing = neutral - float(self.config.min_pitch) if dy < 0 else float(self.config.max_pitch) - neutral
        pitch = neutral + (dy * 2.0 * swing * self.config.gain_y) + self.config.offset_y
        yaw = max(-self.config.max_yaw, min(self.config.max_yaw, yaw))
        pitch = max(self.config.min_pitch, min(self.config.max_pitch, pitch))
        return yaw, pitch

    def _smooth(self, previous: float, current: float) -> float:
        alpha = max(0.0, min(1.0, self.config.smoothing))
        return previous * (1.0 - alpha) + current * alpha
