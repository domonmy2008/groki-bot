// SPDX-FileCopyrightText: 2026 Kenta IDA <fuga@fugafuga.org>
// SPDX-License-Identifier: BSL-1.0

#include "demo_loop.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <cstdint>

#include <M5Unified.h>
#include <esp_log.h>
#include <esp_random.h>
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include <wifi_config_service/mcp_events.hpp>
#include <wifi_config_service/wifi_config_service.hpp>
#include "config_service/config_service.hpp"

#include "avatar/expression.hpp"
#include "avatar/expression_controller.hpp"
#include "battery.hpp"
#include "groki_motion/face_input.hpp"
#include "face_intent_map.hpp"
#include "groki_motion/motion.hpp"
#include "device_ui.hpp"
#include "lt_timer.hpp"
#include "screens.hpp"
#include "settings_sinks.hpp"
#include "speech.hpp"
#include "wifi_sta.hpp"

namespace stackchan::app {

namespace {
constexpr const char* kTag = "stackchan";
} // namespace

[[noreturn]] void run_demo_loop(const DemoLoopArgs& args)
{
    // Aliases keep the body textually identical to its pre-extraction form
    // in app_main.cpp (where these were file-scope globals / parameters).
    SharedState* const g_state = args.state;
    stackchan::board::Board* const g_board = args.board;
    stackchan::board::Si12tTouch* const g_touch = args.touch;
    const std::string& jtts_config_json = args.jtts_config_json;
    const bool has_battery = args.has_battery;
    const bool btn_a_toggles_ui = args.btn_a_toggles_ui;
    const bool touch_gaze_follow = args.touch_gaze_follow;
    const bool conversation_enabled = args.conversation_enabled;
    const bool jtts_idle_enabled = args.jtts_idle_enabled;
    const bool external_servo_control = args.external_servo_control;
    const ServoLimits& limits = args.limits;

    using namespace stackchan;

    // Servo limits for petting and, only when autonomous_idle is set, the
    // optional random poses (every kPoseMinMs..kPoseMaxMs). servo_task also
    // clamps defensively. The default path homes once and then stays still.
    const float kYawMinDeg = static_cast<float>(limits.yaw_min_deg);
    const float kYawMaxDeg = static_cast<float>(limits.yaw_max_deg);
    const float kPitchMinDeg = static_cast<float>(limits.pitch_min_deg);
    const float kPitchMaxDeg = static_cast<float>(limits.pitch_max_deg);
    constexpr std::uint32_t kPoseMinMs = 4000;
    constexpr std::uint32_t kPoseMaxMs = 8000;
    // Protocol resting pose. Pitch 45° matches the gateway WS home command;
    // clamp_deg keeps it inside this board's servo limits (often tighter).
    constexpr float kRestYawDeg = 0.0f;
    constexpr float kRestPitchDeg = 45.0f;
    constexpr std::uint16_t kRestSpeed = 180;
    constexpr std::uint32_t kSpeechMinMs = 6000;
    constexpr std::uint32_t kSpeechMaxMs = 12000;

    static app::Speech speech;
    speech.configure(jtts_config_json);

    // LT timekeeper — ticked every loop iteration; speaks through the same
    // Speech instance (so the avatar's mouth moves) and publishes state for
    // the on-device LT tab. configure() is fed later from NVS (Phase 4).
    static app::LtTimer lt_timer;

    auto rand_range_ms = [](std::uint32_t low, std::uint32_t high) {
        return low + (esp_random() % (high - low + 1));
    };

    std::uint32_t next_pose_ms = 0;
    // One-shot home when autonomous idle is off. After this, targets are left
    // alone so gateway WS head commands are not pulled back to rest.
    bool resting_home_sent = false;
    std::uint32_t next_speech_ms = 2000; // first babble shortly after boot

    // Base-board battery monitor (INA226 on the internal I2C bus). Read here —
    // the only task that touches m5::In_I2C — and published to SharedState +
    // the BLE / Wi-Fi services. Only the M5 base has the INA226; on boards
    // without it (Takao) skip entirely, leaving battery_* = -1 ("—" everywhere).
    constexpr std::uint32_t kBatteryPeriodMs = 5000;
    app::BatteryMonitor battery;
    if (has_battery) {
        battery.begin();
    }
    std::uint32_t next_battery_ms = 0;

    // Nadenade (head-petting) detection on the top-mounted Si12T sensor.
    //
    // A static "is something touching?" test kept false-firing on 2.4 GHz
    // EMI. Captured sensor traces show the real discriminator is the *onset
    // order*: a real pet drags across the head, so each zone first reaches a
    // firm contact (intensity 3) in spatial order — front→middle→back, or the
    // reverse. (Untouched, the chip reads a clean 0 0 0; the zones overlap
    // heavily mid-stroke — front=3,middle=3 ties etc. — so tracking a single
    // "dominant" zone doesn't work; the first-hit timestamps do.)
    //
    // We trigger only when all three zones have hit intensity 3 within one
    // gesture AND their first-hit times are monotonic across the head, with
    // the two ends hit in *different* samples so a single uniform RFI spike
    // (all three at once) can't qualify.
    //   - kStrokePeakIntensity: a zone counts as "hit" at this intensity (3).
    //   - kStrokeGapMs: an all-quiet stretch this long ends the gesture.
    constexpr std::uint8_t kStrokePeakIntensity = 3;
    constexpr std::uint32_t kStrokeGapMs = 600;
    constexpr std::uint32_t kNadenadeCooldownMs = 4000;
    std::array<std::uint32_t, 3> stroke_hit_ms{0, 0, 0}; // first-hit-3 time per zone (0 = not yet)
    std::uint32_t stroke_active_ms = 0;   // last time any zone was non-zero
    std::uint32_t next_nadenade_ms = 0;   // earliest time we'll trigger again
    constexpr std::uint32_t kHeadPetRestoreDelayMs = 3000;
    groki_motion::Limits motion_limits{
        .yaw_min_deg = kYawMinDeg,
        .yaw_max_deg = kYawMaxDeg,
        .pitch_min_deg = kPitchMinDeg,
        .pitch_max_deg = kPitchMaxDeg,
    };
    bool head_pet_touch_active = false;
    bool head_pet_restore_pending = false;
    std::uint32_t head_pet_overlay_cmd = 0; // last overlay command this task published (token for a conditional clear)
    std::uint32_t gesture_overlay_cmd = 0;  // last overlay from a screen gesture (token for a conditional clear)
    float head_pet_prev_yaw = 0.0f;
    float head_pet_prev_pitch = 0.0f;
    std::uint32_t head_pet_restore_at_ms = 0;

    // Last-applied speaker volume percent. Watches the SharedState atom
    // so the device-UI's −/+ nudge buttons re-apply via the same
    // setVolume + NVS path the BLE / WiFi sinks use. Seeded from the
    // current atom (set by boot's apply_speaker_volume_sink call).
    std::uint16_t last_speaker_volume_pct =
        g_state->speaker.volume_pct.load(std::memory_order_relaxed);
    // One-touch mute edge detection (corner tap / BtnA hold). Applied via
    // apply_speaker_volume (NOT the sink) — mute is session-only, no NVS.
    bool last_speaker_muted = g_state->speaker.muted.load(std::memory_order_relaxed);

    // Set true by the (render-task) completion callback so demo_loop knows
    // the previous balloon finished. Atomics keep it thread-safe.
    static std::atomic<bool> balloon_in_flight{false};

    // Wi-Fi state edge detection: while disconnected we pin a persistent
    // "Wi-Fi: 已断开" balloon and suppress babble; when it reconnects we
    // clear the balloon so normal demo behaviour resumes.
    bool wifi_warning_active = false;

    groki_motion::FaceInput face_input;
    face_input.set_screen_center(static_cast<float>(M5.Display.width()) * 0.5f,
                                 static_cast<float>(M5.Display.height()) * 0.5f);

    bool overlay_owns_gesture = false;

    for (;;) {
        // Camera session in progress: every In_I2C touch (M5.update's
        // touch/BtnPWR poll, INA226 battery, Si12T nadenade, BMI270 shake)
        // would re-init the I2C controller under the camera's SCCB driver
        // and kill the capture. Idle the whole iteration instead — sessions
        // are ~1.5 s one-shots, so buttons/touch/IMU just miss a beat.
        if (g_state->i2c_quiesce.load(std::memory_order_acquire)) {
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }

        // Drive M5.update() so M5.Touch / M5.BtnPWR latch their state machines.
        M5.update();

        const std::uint32_t now_ms = static_cast<std::uint32_t>(esp_timer_get_time() / 1000);

        // Live-apply speaker_volume_pct changes from the device-UI (BLE /
        // HTTP sinks already apply directly). Cheap atomic compare per
        // iteration; only triggers M5.Speaker.setVolume + NVS write on
        // an actual change.
        {
            const std::uint16_t cur = g_state->speaker.volume_pct.load(std::memory_order_relaxed);
            if (cur != last_speaker_volume_pct) {
                last_speaker_volume_pct = cur;
                settings_sinks::apply_speaker_volume_persist(cur);
            }
        }

        // One-touch mute toggles (device_ui corner tap / atom_status BtnA
        // hold). Re-run apply_speaker_volume with the unchanged percent so
        // the M5.Speaker master volume snaps to 0 / back immediately — this
        // also silences audio that's already mid-playback, since the mixer
        // applies master volume per chunk. A short haptic confirms the
        // toggle on boards with a vibration motor (no-op elsewhere).
        {
            const bool muted = g_state->speaker.muted.load(std::memory_order_relaxed);
            if (muted != last_speaker_muted) {
                last_speaker_muted = muted;
                settings_sinks::apply_speaker_volume(
                    g_state->speaker.volume_pct.load(std::memory_order_relaxed));
                ESP_LOGI(kTag, "speaker %s", muted ? "muted" : "unmuted");
                if (g_board != nullptr) (void)g_board->vibrate(20);
            }
        }

        // BtnB (StopWatch Blue / G1) — manual expression cycle with haptic
        // confirmation. wasPressed() is false on boards without BtnB so the
        // check is harmless universally.
        if (M5.BtnB.wasPressed()) {
            const int cur = g_state->face.expression.load(std::memory_order_relaxed);
            g_state->face.expression.store(
                (cur + 1) % static_cast<int>(avatar::kExpressionCount), std::memory_order_relaxed);
            g_state->note_face_activity();
            if (g_board != nullptr) (void)g_board->vibrate(30);
        }
        // BtnA on StopWatch (= Yellow / G2) — toggle the device_ui open/close.
        // The corner tap-to-open hot zone on a round AMOLED is awkward to hit
        // (corners are within the visible circle but at the edge of the touch
        // pad), so a physical button is a more reliable opener. On other
        // boards BtnA either has no role (CoreS3 uses touch only) or is
        // already claimed by atom_status's gesture vocab (polled via
        // screens::poll_inputs above; btn_a_toggles_ui is false there).
        if (btn_a_toggles_ui && g_board != nullptr && M5.BtnA.wasPressed()) {
            app::ui::toggle();
            (void)g_board->vibrate(20);
        }

        // LT timekeeper: re-configure when BLE/HTTP (or the boot seed) pushed
        // a new config JSON, then consume UI commands / update the countdown /
        // announce the 1-minute warning + overtime through speech + balloon.
        static std::uint32_t lt_cfg_seen = 0;
        if (const std::uint32_t v = g_state->lt_config_version(); v != lt_cfg_seen) {
            lt_cfg_seen = v;
            lt_timer.configure(g_state->snapshot_lt_config(), g_state);
        }
        lt_timer.tick(*g_state, speech, now_ms);

        // Battery: sample the INA226 every few seconds and fan the result out to
        // the device UI (SharedState) + the BLE / Wi-Fi settings services.
        if (has_battery && now_ms >= next_battery_ms) {
            next_battery_ms = now_ms + kBatteryPeriodMs;
            if (auto r = battery.read()) {
                const int mv = static_cast<int>(r->voltage * 1000.0f + 0.5f);
                const int ma = static_cast<int>(r->current * 1000.0f + (r->current >= 0 ? 0.5f : -0.5f));
                const int pct = app::battery_percent_from_voltage(r->voltage);
                g_state->battery.mv.store(static_cast<std::int16_t>(mv), std::memory_order_relaxed);
                g_state->battery.ma.store(static_cast<std::int16_t>(ma), std::memory_order_relaxed);
                g_state->battery.pct.store(static_cast<std::int8_t>(pct), std::memory_order_relaxed);
                config::notify_battery(mv, ma, pct);
                wifi_config::set_battery(mv, ma, pct);
            }
        }

        // On-device overlay input. Button-driven screens (atom_status's
        // BtnA gesture vocab) poll every tick; the LCD-touch block below is
        // inert on boards without a touch panel (M5.Touch reports nothing),
        // so no per-board split is needed here.
        app::screens::poll_inputs();
        {
            const auto td = M5.Touch.getDetail();
            // Only real playback blocks expression gestures and arms barge-in; Thinking is waiting,
            // so touch works as usual (a transient overlay already wins over the thinking face).
            const bool conv_speaking =
                static_cast<stackchan::avatar::VoiceState>(g_state->conv.voice_state.load(
                    std::memory_order_relaxed)) == stackchan::avatar::VoiceState::Speaking;
            const bool barge_in_armed = g_state->barge_in_enabled.load(std::memory_order_relaxed) && conv_speaking;
            // Overlay flicks still switch tabs while the UI is shown.
            // FaceInput only sees leftover motion after that.
            if (td.wasFlicked()) {
                app::screens::handle_flick(td.distanceX(), td.distanceY());
            }
            if (td.wasPressed()) {
                const bool consumed = app::screens::handle_tap(td.x, td.y);
                if (consumed) {
                    overlay_owns_gesture = true;
                } else if (barge_in_armed) {
                    g_state->barge_in_request.store(true, std::memory_order_relaxed);
                }
            }
            const groki_motion::Policy face_policy{
                .expressions_enabled = !conv_speaking,
                .overlay_owns_panel = overlay_owns_gesture || app::screens::overlay_active(),
            };

            groki_motion::TouchSample touch{};
            touch.x = static_cast<std::int16_t>(td.x);
            touch.y = static_cast<std::int16_t>(td.y);
            touch.pressed = td.isPressed();
            touch.was_pressed = td.wasPressed();
            touch.was_clicked = td.wasClicked();
            touch.click_count = td.getClickCount();
            touch.was_hold = td.wasHold();
            touch.is_moving = td.isFlicking() || td.isDragging();
            touch.was_flicked = td.wasFlicked();
            touch.distance_x = static_cast<std::int16_t>(td.distanceX());
            touch.distance_y = static_cast<std::int16_t>(td.distanceY());
            touch.now_ms = now_ms;

            groki_motion::ImuSample imu{};
            imu.now_ms = now_ms;
            float ax = 0.0f, ay = 0.0f, az = 0.0f;
            if (M5.Imu.getAccel(&ax, &ay, &az)) {
                imu.valid = true;
                // StopWatch BMI270: the official demo swaps raw X/Y to get screen coordinates.
                if (touch_gaze_follow) {
                    imu.ax = ay;
                    imu.ay = ax;
                } else {
                    imu.ax = ax;
                    imu.ay = ay;
                }
                imu.az = az;
            }

            const auto face = face_input.tick(touch, imu, face_policy);
            if (face.gaze_active) {
                g_state->face.gaze_target_h.store(face.gaze_h, std::memory_order_relaxed);
                g_state->face.gaze_target_v.store(face.gaze_v, std::memory_order_relaxed);
            } else {
                g_state->face.gaze_target_h.store(0.0f, std::memory_order_relaxed);
                g_state->face.gaze_target_v.store(0.0f, std::memory_order_relaxed);
            }

            using groki_motion::Intent;
            const Intent intent = face.intent;
            if (td.isPressed() || td.wasPressed()) {
                g_state->note_face_activity();
            }
            if (intent == Intent::StrokeRestore || intent == Intent::DizzyEnd) {
                // Only clear the overlay the screen gesture published; newer overlays from other sources stay.
                if (g_state->clear_face_overlay_if(gesture_overlay_cmd)) {
                    gesture_overlay_cmd = 0;
                }
            } else if (intent == Intent::FlickLeft || intent == Intent::FlickRight) {
                std::int16_t next = static_cast<std::int16_t>(
                    g_state->face.expression.load(std::memory_order_relaxed)) +
                    face.preview_step;
                const auto count = static_cast<std::int16_t>(avatar::kExpressionCount);
                next %= count;
                if (next < 0) {
                    next += count;
                }
                g_state->face.expression.store(next, std::memory_order_relaxed);
                g_state->note_face_activity();
                if (g_board != nullptr) {
                    (void)g_board->vibrate(30);
                }
            } else if (intent != Intent::None) {
                const auto next = expression_for(intent);
                const std::uint32_t hold_ms =
                    (intent == Intent::Stroke || intent == Intent::DizzyStart)
                        ? 0
                        : avatar::ExpressionController::kDefaultOverlayHoldMs;
                gesture_overlay_cmd = g_state->request_face_overlay(next, hold_ms);
                if (g_board != nullptr) {
                    const std::uint16_t ms = intent == Intent::DizzyStart ? 80 : 30;
                    (void)g_board->vibrate(ms);
                }
                ESP_LOGI(kTag, "face intent %u → expr %u", static_cast<unsigned>(intent),
                         static_cast<unsigned>(next));
            }
            if (!td.isPressed()) {
                overlay_owns_gesture = false;
            }
        }

        const bool conv_active = g_state->conv.active.load(std::memory_order_relaxed);
        const bool conv_idle = g_state->conv.idle.load(std::memory_order_relaxed);
        const bool audio_streaming = g_state->audio_stream_active.load(std::memory_order_relaxed);

        // While a BLE audio stream is playing, the streamer owns the speaker
        // and drives mouth_open itself. Stand down completely — stop any
        // in-flight babble (its playRaw would fight the stream on the I2S
        // bus) and don't touch mouth_open.
        if (audio_streaming) {
            if (speech.is_speaking()) speech.stop();
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }

        // Nadenade and the resting-home pose run when there is no conversation
        // OR the conversation is idly listening. Random look-around is off
        // unless servo.autonomous_idle is set; wake-time motion comes from
        // the gateway. The full demo (mouth-sync, Wi-Fi balloon, babble,
        // expression cycle) runs only when there is no conversation at all —
        // otherwise it would fight the conversation task for the avatar and
        // the I2S bus.
        const bool allow_idle_demo = !conv_active || conv_idle;
        const bool allow_full_demo = !conv_active;

        // While the conversation is thinking / speaking it owns the avatar —
        // stand down completely.
        if (!allow_idle_demo) {
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }

        if (allow_full_demo) {
            // When idle jtts babble is enabled, drive the mouth from the
            // speech envelope and run the Wi-Fi check + random babble. When
            // disabled, demo_loop becomes a no-op on the mouth so the mic
            // lip-sync task (main/mic_lip_sync_task.cpp), if active, owns
            // `mouth_open` without us overwriting it with 0 every tick.
            if (jtts_idle_enabled) {
                // Mouth opens with the current speech envelope; closed while silent.
                g_state->face.mouth_open.store(speech.current_mouth_open(), std::memory_order_relaxed);

                // The "Wi-Fi: 已断开" balloon and the babble suppression below only
                // make sense when the assistant actually needs the network — i.e.
                // when the conversation backend (OpenAI / Gemini / XiaoZhi) is on.
                // With conversation disabled the demo is fully self-contained
                // (local jtts babble), so we ignore Wi-Fi state entirely and let
                // the idle behaviour run from boot without waiting for an AP.
                const bool wifi_ok = !conversation_enabled || app::wifi_is_connected();
                if (!wifi_ok && !wifi_warning_active) {
                    speech.stop();
                    // hold_ms = UINT32_MAX so the balloon stays put until we clear it.
                    g_state->set_balloon_text("Wi-Fi: 已断开", /*hold_ms=*/UINT32_MAX);
                    balloon_in_flight.store(false, std::memory_order_release);
                    wifi_warning_active = true;
                } else if (wifi_ok && wifi_warning_active) {
                    g_state->clear_balloon();
                    wifi_warning_active = false;
                    next_speech_ms = now_ms + 1500;
                }

                // Kick off a new babble + balloon once the previous balloon is done
                // (callback resets balloon_in_flight) AND audio is idle AND the
                // random dwell time has elapsed. Suppressed while Wi-Fi is down so
                // the disconnected balloon stays visible.
                if (!wifi_warning_active &&
                    now_ms >= next_speech_ms &&
                    !speech.is_speaking() &&
                    !balloon_in_flight.load(std::memory_order_acquire)) {
                    // Speak a phrase and show ITS display text in the balloon —
                    // babble() returns the display (発話内容) of the same phrase
                    // it synthesises (発声内容), so screen and voice always match.
                    const std::string display = speech.babble(esp_random());
                    if (!display.empty()) {
                        balloon_in_flight.store(true, std::memory_order_release);
                        g_state->set_balloon_text(display, /*hold_ms=*/0, [] {
                            balloon_in_flight.store(false, std::memory_order_release);
                        });
                    }
                    next_speech_ms = now_ms + rand_range_ms(kSpeechMinMs, kSpeechMaxMs);
                }
            }
        }

        // Nadenade: poll the top sensor and look for a directional stroke
        // across the three zones. On a completed stroke, run a quick happy
        // head-wobble. The wobble blocks demo_loop's normal scheduling for
        // ~1.4 s but the render and servo tasks keep running.
        if (g_touch != nullptr && !wifi_warning_active && now_ms >= next_nadenade_ms) {
            const auto reading = g_touch->read();
            const std::uint8_t f = reading.front(), mid = reading.middle(), bk = reading.back();
            const std::uint8_t mx = std::max({f, mid, bk});

            // Edge-triggered diagnostic — only log when the reading
            // actually changes, otherwise a chip that gets stuck at
            // `2 2 2` from RFI floods the serial port at 20 Hz.
            static std::uint8_t last_logged[3] = {0xFF, 0xFF, 0xFF};
            if (f != last_logged[0] || mid != last_logged[1] || bk != last_logged[2]) {
                if (reading.any_touched() ||
                    last_logged[0] != 0 || last_logged[1] != 0 || last_logged[2] != 0) {
                    ESP_LOGI(kTag, "touch raw: front=%u middle=%u back=%u", f, mid, bk);
                }
                last_logged[0] = f;
                last_logged[1] = mid;
                last_logged[2] = bk;
            }

            const bool firmly_touched = reading.firmly_touched();
            if (reading.any_touched()) {
                // A light touch counts as attention too: any touch immediately wakes it from idle decay.
                g_state->note_face_activity();
            }
            if (firmly_touched && !head_pet_touch_active && !external_servo_control) {
                if (!head_pet_restore_pending) {
                    head_pet_prev_yaw = g_state->servo.target_yaw_deg.load(std::memory_order_relaxed);
                    head_pet_prev_pitch = g_state->servo.target_pitch_deg.load(std::memory_order_relaxed);
                }
                head_pet_touch_active = true;
                head_pet_restore_pending = false;
                speech.stop();

                // Holding the top of the head = being petted: the aora shy face (averted eyes, blush, floating hearts).
                // Petting shows shyness, not plain happiness.
                head_pet_overlay_cmd = g_state->request_face_overlay(avatar::Expression::Affection, 0);
                balloon_in_flight.store(true, std::memory_order_release);
                g_state->set_balloon_text("摸摸♡", /*hold_ms=*/2200, [] {
                    balloon_in_flight.store(false, std::memory_order_release);
                });

                const auto pose = groki_motion::head_pet_pose(
                    head_pet_prev_yaw, head_pet_prev_pitch, motion_limits, esp_random());
                g_state->servo.speed_override.store(pose.speed, std::memory_order_relaxed);
                g_state->servo.target_yaw_deg.store(pose.yaw_deg, std::memory_order_relaxed);
                g_state->servo.target_pitch_deg.store(pose.pitch_deg, std::memory_order_relaxed);
                next_speech_ms = now_ms + 1500;
                next_pose_ms = std::max(next_pose_ms, now_ms + 2000);
            } else if (!firmly_touched && head_pet_touch_active) {
                head_pet_touch_active = false;
                head_pet_restore_pending = true;
                head_pet_restore_at_ms = now_ms + kHeadPetRestoreDelayMs;
            }

            // End (and clear) the gesture once the head's been all-quiet for
            // longer than the inter-zone gap.
            if (now_ms - stroke_active_ms > kStrokeGapMs) {
                stroke_hit_ms = {0, 0, 0};
            }
            if (mx > 0) stroke_active_ms = now_ms;

            // Record the first time each zone reaches a firm contact in this
            // gesture.
            if (f   >= kStrokePeakIntensity && stroke_hit_ms[0] == 0) stroke_hit_ms[0] = now_ms;
            if (mid >= kStrokePeakIntensity && stroke_hit_ms[1] == 0) stroke_hit_ms[1] = now_ms;
            if (bk  >= kStrokePeakIntensity && stroke_hit_ms[2] == 0) stroke_hit_ms[2] = now_ms;

            bool stroke_complete = false;
            if (stroke_hit_ms[0] && stroke_hit_ms[1] && stroke_hit_ms[2]) {
                // All three zones firmly touched within one gesture. Accept
                // only a monotonic onset order across the head, with the two
                // ends hit in different samples (so a single all-three RFI
                // spike — equal timestamps — can't qualify).
                const auto a = stroke_hit_ms[0], b = stroke_hit_ms[1], c = stroke_hit_ms[2];
                const bool fwd = a <= b && b <= c && a < c;   // front→middle→back
                const bool rev = a >= b && b >= c && a > c;   // back→middle→front
                stroke_complete = fwd || rev;
                if (!stroke_complete) {
                    // Hit all three but not cleanly ordered → drop so a noisy
                    // simultaneous lift can't linger and re-qualify.
                    stroke_hit_ms = {0, 0, 0};
                }
            }

            if (stroke_complete) {
                const char* direction =
                    stroke_hit_ms[0] < stroke_hit_ms[2] ? "front_to_back" : "back_to_front";
                ESP_LOGI(kTag, "nadenade! stroke %s (hit ms: f=%u m=%u b=%u)",
                         direction,
                         static_cast<unsigned>(stroke_hit_ms[0]),
                         static_cast<unsigned>(stroke_hit_ms[1]),
                         static_cast<unsigned>(stroke_hit_ms[2]));
                stackchan::wifi_config::mcp_events::publish_touch_stroke(direction);
                speech.stop();
                const float prev_yaw = g_state->servo.target_yaw_deg.load(std::memory_order_relaxed);

                g_state->request_face_overlay(avatar::Expression::Affection,
                                              avatar::ExpressionController::kDefaultOverlayHoldMs);
                balloon_in_flight.store(true, std::memory_order_release);
                g_state->set_balloon_text("摸摸♡", /*hold_ms=*/2200, [] {
                    balloon_in_flight.store(false, std::memory_order_release);
                });

                // Head-wobble writes servo targets — skip it entirely when an
                // external source (ESP-NOW remote) owns the head; still show
                // the happy face + balloon above.
                if (!external_servo_control) {
                    // speed_override is consumed once: write it again before every target change.
                    constexpr std::uint32_t kHalfPeriodMs = 160;
                    for (const auto& cmd : groki_motion::nadenade_wobble_steps()) {
                        g_state->servo.speed_override.store(cmd.speed, std::memory_order_relaxed);
                        g_state->servo.target_yaw_deg.store(cmd.yaw_deg, std::memory_order_relaxed);
                        vTaskDelay(pdMS_TO_TICKS(kHalfPeriodMs));
                    }
                    g_state->servo.speed_override.store(groki_motion::kNadenadeWobbleSpeed,
                                                        std::memory_order_relaxed);
                    g_state->servo.target_yaw_deg.store(prev_yaw, std::memory_order_relaxed);
                    vTaskDelay(pdMS_TO_TICKS(kHalfPeriodMs));
                    g_state->servo.speed_override.store(0, std::memory_order_relaxed);
                }

                stroke_hit_ms = {0, 0, 0};
                stroke_active_ms = 0;
                const std::uint32_t after_ms = static_cast<std::uint32_t>(esp_timer_get_time() / 1000);
                next_nadenade_ms = after_ms + kNadenadeCooldownMs;
                // Push back demo activity so the wobble doesn't fight a
                // freshly-scheduled random pose / babble.
                next_speech_ms = after_ms + 1500;
                next_pose_ms = std::max(next_pose_ms, after_ms + 2000);
                continue;
            }
        }

        if (head_pet_restore_pending && now_ms >= head_pet_restore_at_ms) {
            head_pet_restore_pending = false;
            if (g_state->clear_face_overlay_if(head_pet_overlay_cmd)) {
                head_pet_overlay_cmd = 0;
            }
            g_state->servo.speed_override.store(200, std::memory_order_relaxed);
            g_state->servo.target_yaw_deg.store(head_pet_prev_yaw, std::memory_order_relaxed);
            g_state->servo.target_pitch_deg.store(head_pet_prev_pitch, std::memory_order_relaxed);
        }

        // Wake-time idle / look-around is owned by the gateway via WS
        // ``{"type":"head","yaw":..,"pitch":..,"speed":..}`` while the wake
        // gate is LISTENING. This task cannot see DORMANT vs LISTENING.
        // With autonomous_idle left false, command the protocol home pose
        // once (yaw 0, pitch 45, clamped), then leave the targets alone.
        // A head_hold still in the future means the gateway already owns
        // the head and will send home itself when the gate closes.
        const bool servo_available = !external_servo_control && !head_pet_touch_active &&
                                     !head_pet_restore_pending &&
                                     !g_state->servo.masked.load(std::memory_order_relaxed);
        const bool head_held = static_cast<std::int32_t>(
                                   now_ms - g_state->servo.head_hold_until_ms.load(std::memory_order_relaxed)) < 0;
        if (servo_available && head_held) {
            resting_home_sent = true;
        } else if (servo_available && g_state->servo.autonomous_idle.load(std::memory_order_relaxed)) {
            if (now_ms >= next_pose_ms) {
                const auto pose = groki_motion::idle_pose(
                    g_state->servo.target_yaw_deg.load(std::memory_order_relaxed),
                    g_state->servo.target_pitch_deg.load(std::memory_order_relaxed),
                    motion_limits, esp_random());
                g_state->servo.speed_override.store(pose.speed, std::memory_order_relaxed);
                g_state->servo.target_yaw_deg.store(pose.yaw_deg, std::memory_order_relaxed);
                g_state->servo.target_pitch_deg.store(pose.pitch_deg, std::memory_order_relaxed);
                next_pose_ms = now_ms + rand_range_ms(kPoseMinMs, kPoseMaxMs);
            }
        } else if (servo_available && !resting_home_sent) {
            const float home_yaw = clamp_deg(kRestYawDeg, limits.yaw_min_deg, limits.yaw_max_deg);
            const float home_pitch = clamp_deg(kRestPitchDeg, limits.pitch_min_deg, limits.pitch_max_deg);
            const float yaw = g_state->servo.target_yaw_deg.load(std::memory_order_relaxed);
            const float pitch = g_state->servo.target_pitch_deg.load(std::memory_order_relaxed);
            if (std::fabs(yaw - home_yaw) > 0.5f || std::fabs(pitch - home_pitch) > 0.5f) {
                g_state->servo.speed_override.store(kRestSpeed, std::memory_order_relaxed);
                g_state->servo.target_yaw_deg.store(home_yaw, std::memory_order_relaxed);
                g_state->servo.target_pitch_deg.store(home_pitch, std::memory_order_relaxed);
            }
            resting_home_sent = true;
        }

        vTaskDelay(pdMS_TO_TICKS(50));
    }
}

} // namespace stackchan::app
