**English** | [简体中文](README.zh-CN.md)

# Groki Bot Gateway

A small Python service that runs on your computer and gives the
[Groki Bot](../README.md) desktop robot a voice brain. It lives in the
`gateway/` directory of the Groki Bot repository; how it fits together with
the firmware and Grok Bot is described in
[docs/architecture.md](../docs/architecture.md).

- **Voice conversation** through Google Gemini Live by default, or Grok login voice, with the robot's
  microphone and speaker.
- **"Hi Grok" wake word**, detected on your computer, so the gateway only
  forwards microphone audio to the selected voice backend after you call the robot.
- **An MCP server** so Claude Code, Claude Desktop, or any other MCP client
  can make the robot speak and read its status.
- **Face tracking** on a Mac with a camera: the robot turns its head to
  follow your face (see [Face tracking](#face-tracking)).
- Optional: voice control of your Mac, an `ask_claude` voice tool that
  hands hard questions to the Claude CLI (offered only when the `claude` CLI
  is installed), and a Gemini-only `ask_grokbot` voice tool that
  hands tasks to an agent in the Grok Bot app and reads its answers aloud
  (see [Optional: Grok Bot hand-off](#optional-grok-bot-hand-off)).

```text
Robot ← XiaoZhi WebSocket :8765 → Gateway ← MCP → Claude / MCP client
Gateway voice → Gemini Live (default) / Grok login voice
Grok voice → localhost:18771 login helper → native Grok Bot
Gemini ask_grokbot → optional localhost:18770 → gbot CLI → Grok Bot
```

## Do you need this?

The Groki Bot firmware can already talk on its own: flash it, enter your own
OpenAI or Gemini API key in its settings page, and it works without any
computer. Install this gateway if you want:

- the "Hi Grok" wake word,
- the head to follow your face (Mac with a camera),
- Claude (or another MCP client) to make the robot speak,
- voice commands that reach your computer (Mac control, `ask_claude`).

**What works with the Groki Bot firmware.** Voice conversation, the wake
word, face tracking, and the `speak` and `get_status` MCP tools. The Groki Bot
firmware's XiaoZhi client announces `features.mcp=false`, so MCP tools that
drive hardware (`move_head`, `set_led`, `set_avatar`, `take_photo` and similar)
return an error with that firmware. They work with firmware that implements
the XiaoZhi device MCP tools. Face tracking does not use MCP: it turns the
head with the XiaoZhi `head` message, which the firmware handles. The shared voice
device tools (`STACKCHAN_GEMINI_DEVICE_TOOLS=1`) do not need MCP either: with
this firmware `move_head` is sent as a `head` message (firmware newer than
v0.2.1), `set_avatar` as an emotion and `set_all_leds` as an `led` message.
Camera tools have no such path and do not work with this firmware.

## What you need

| | |
|---|---|
| Robot | M5Stack CoreS3 with the Stack-chan base, flashed with the [Groki Bot firmware](../README.md) |
| Computer | macOS (tested on Apple Silicon). Linux should work for voice; Mac control is macOS only. |
| Network | Robot and computer on the same Wi-Fi / LAN |
| Software | [uv](https://docs.astral.sh/uv/), Git, and the Opus audio library (`brew install opus` on macOS, `sudo apt install libopus0` on Debian/Ubuntu) |
| API key | A Google AI Studio key for Gemini Live: <https://aistudio.google.com/apikey> |

## 1. Flash the robot

Follow [path 1 in the Groki Bot README](../docs/voice-backends.md#path-1-firmware-only)
(web flasher in Chrome or Edge; you can skip the API key). Come back here once the robot boots and
joins your Wi-Fi.

## 2. Install the gateway

```bash
git clone https://github.com/JosssphZhou/groki-bot.git
cd groki-bot/gateway
uv sync --all-extras
cp .env.example .env
```

Open `.env`, choose a voice backend, and set a device token:

- Default Gemini: set `GEMINI_API_KEY`. Grok login voice uses the setup below instead and does not need this key.
- `STACKCHAN_TOKEN`: a random secret, for example from `openssl rand -hex 16`.
  Recommended; leave it empty only on a network you trust.

Note: the PyPI package named `stackchan-mcp` is the upstream project
([kisaragi-mochi/stackchan-mcp](https://github.com/kisaragi-mochi/stackchan-mcp))
and does not include the Gemini voice or wake word. Install from this
repository as shown above.

### Grok Bot login voice (macOS)

Gemini is the default backend. To use Grok voice, install and sign in to the Grok Bot desktop app on the same Mac, and make sure Node.js is installed. This integration was tested with Grok Bot 0.71.2 and uses internal APIs, so application updates, account permissions, and usage limits may affect availability. No separate xAI voice API key or firmware reflashing is needed.

After installing the gateway, configure the following in `gateway/.env` with your own agent name and ID:

```dotenv
STACKCHAN_VOICE_BACKEND=gbot
STACKCHAN_GBOT_VOICE_BOT_ID=YOUR_AGENT_ID
STACKCHAN_TOOL_BOT=assistant
STACKCHAN_TOOL_BOT_ID=YOUR_AGENT_ID
```

In a separate terminal, start the login helper from `gateway/` and keep it running:

```sh
STACKCHAN_GBOT_VOICE_BOT_ID=YOUR_AGENT_ID \
STACKCHAN_TOOL_BOT=assistant \
STACKCHAN_TOOL_BOT_ID=YOUR_AGENT_ID \
node scripts/gbot_voice_session_bridge.mjs
```

The Node helper does not load `.env` automatically; pass the same values as shown. It reads the existing macOS app login and Keychain, keeps credentials in memory, and binds only to `127.0.0.1:18771`. Next, start the Python gateway using one of the methods in step 3. `GEMINI_API_KEY` is not required for this backend. To switch back, set `STACKCHAN_VOICE_BACKEND=gemini` and restart the gateway with your existing Gemini key.

To keep the helper running after closing the terminal or restarting your Mac, install it as a user LaunchAgent from `gateway/` with the same agent ID:

```sh
python3 scripts/install_gbot_voice_launchd.py --agent-id YOUR_AGENT_ID --enable-desktop --enable-tools
```

It starts at login and restarts after exit. Add `--dry-run` to preview the plist, or `--replace` to back up and update an existing installation (interrupts current voice calls). Keep this checkout at its installed path. Restart the Python gateway after installation and verify a spoken reply on the robot. The LaunchAgent manages only the login helper; it does not start the Python gateway or repair upstream login or sandbox failures. `http://127.0.0.1:18771/health` confirms helper liveness only. Logs are in `~/Library/Logs/stackchan-gbot-voice.log`.

The gateway checks for a fresh server acknowledgement on the active voice connection every 30 seconds without playing test audio. Failed initial connections, dropped or unresponsive connections, and stalled replies trigger automatic recovery. Retries back off from 1 second to at most 30 seconds. Recovery discards old audio and does not replay questions or desktop tasks. Expired logins and incompatible upstream APIs still require account attention or an application update.

`http://127.0.0.1:8766/debug/voice-health` reports the actual voice connection: HTTP 200 when connected, or HTTP 503 while connecting, recovering, or unavailable. `action_required` identifies login/account problems; `last_error` contains only a fixed error category. Use your configured gateway port if different.

From `gateway/`, run an isolated recovery check against real Grok audio. It simulates an unavailable login helper and closes only its own test connection, leaving robot calls alone:

```sh
PYTHONPATH=. python scripts/probe_gbot_voice_recovery.py --agent-id YOUR_AGENT_ID --output-dir /tmp/groki-recovery-check
```

With the explicit bot name and matching IDs, Grok directly uses native `send_task`, `search_conversations`, and `end_the_call`; it does not add a local `ask_grokbot` wrapper or use the CLI forwarding service. Shared local hardware and optional Mac tools retain their existing opt-in settings, including the legacy-named `STACKCHAN_GEMINI_DEVICE_TOOLS`. Conversation search is not web search. Unknown native tools are not enabled by the legacy `STACKCHAN_GBOT_VOICE_TOOLS` switch.

If the desktop sandbox is unavailable, audio stays usable while the native channel retries in the background. Task dispatching and conversation search are refused until presence and the event stream are ready. Acceptance means the task was accepted, not that a desktop message was visibly delivered. Typed-message subscriptions can still fail; they reconnect independently and do not replay messages missed during a disconnect. No claims of full feature parity or acoustic barge-in are made. See [validation](GBOT_VOICE_VALIDATION.md) and [latency methods](VOICE_LATENCY_BENCHMARK.md).

### Wake word model (optional, about 33 MB)

```bash
mkdir -p models/kws && cd models/kws
curl -L -O https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2
tar xf sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2
rm sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2
cd ../..
```

Without the model the gateway still works, but it sends everything the
microphone hears to the selected voice backend while the robot is connected. Check the model
offline (macOS, uses the `say` command):

```bash
uv run --all-extras python scripts/kws_offline_repro.py --generate-say
# prints "hit: ..." or "miss: ..."
```

### Changing the wake word

The default wake word is "Hi Grok". The detector (sherpa-onnx keyword
spotting) matches a sequence of sounds, so any phrase works without
retraining the model. Set it in `.env` and restart the gateway:

- "Hey Groki" instead: `STACKCHAN_WAKE_PHRASE=hey groki` (built in, with the
  same vowel variants as "Hi Grok").
- Your own phrase: set `STACKCHAN_WAKE_PHRASE` to the phrase and
  `STACKCHAN_WAKE_KEYWORD` to its sounds, then `@` and the phrase. English
  words use CMU dictionary phonemes (ARPAbet, with stress digits); Chinese
  uses pinyin split into initial and final, with tone marks. Every sound must
  appear in the model's `tokens.txt`. Examples:

  ```bash
  STACKCHAN_WAKE_PHRASE=hello robot
  STACKCHAN_WAKE_KEYWORD=HH AH0 L OW1 R OW1 B AA2 T @hello_robot

  STACKCHAN_WAKE_PHRASE=小机器人
  STACKCHAN_WAKE_KEYWORD=x iǎo j ī q ì r én @小机器人
  ```

Check a new wake word before relying on it: `kws_offline_repro.py` reads the
same settings (`--voice` picks another macOS voice, `--wav` uses your own
recording). Two or three syllables with clear consonants work best; if it
triggers too rarely or too often, adjust `STACKCHAN_KWS_THRESHOLD` and
`STACKCHAN_KWS_SCORE`.

## 3. Start it and check

```bash
uv run --all-extras stackchan-mcp --check     # config and port check, exits right away
```

Then start the gateway in one of two ways.

**A. Let Claude Code start it (simplest).** The gateway runs while Claude Code
is open:

```bash
claude mcp add groki -- uv run --all-extras --directory "$(pwd)" stackchan-mcp
```

For Claude Desktop, add this to `claude_desktop_config.json` (use the
absolute path of your clone):

```json
{
  "mcpServers": {
    "groki": {
      "command": "uv",
      "args": ["run", "--all-extras", "--directory", "/absolute/path/to/groki-bot/gateway", "stackchan-mcp"]
    }
  }
}
```

**B. Run it all the time, as a background service.** Use this if you want to
talk to the robot without an MCP client open:

```bash
./scripts/run_stackchan_gateway_launchd.sh
```

The script keeps stdin open so the stdio MCP server does not exit. Point a
launchd agent (macOS) or systemd unit (Linux) at it for start at login.

Only one gateway can use ports 8765 and 8766 at a time, so pick A or B.

When it is running you should see `server listening on 0.0.0.0:8765` in the
log, and <http://127.0.0.1:8766/debug/status> returns JSON.

## 4. Point the robot at the gateway

In the Groki Bot BLE settings page
(<https://josssphzhou.github.io/groki-bot/settings.html>, desktop Chrome
or Edge), tab "对话" (Conversation):

1. Provider: "XiaoZhi 服务器" (XiaoZhi server).
2. "XiaoZhi 服务器 URL": `ws://<your computer's LAN IP>:8765`
   (find the IP with `ipconfig getifaddr en0` on macOS or `hostname -I` on Linux).
3. "XiaoZhi 令牌" (token): the same value as `STACKCHAN_TOKEN`, or empty if
   you left that empty.
4. "保存并重启" (Save and restart).

`/debug/status` should now show `"device": {"connected": true, ...}`. Say
"Hi Grok" and start talking (or "Hey Groki" if you set
`STACKCHAN_WAKE_PHRASE=hey groki`, see [Changing the wake word](#changing-the-wake-word)).

## Face tracking

The robot turns its head to follow your face. A small Mac program,
[tools/vision-tracker](../tools/vision-tracker/README.md), watches the camera
with Apple Vision and posts the face position to the gateway at
`http://127.0.0.1:8766/track`; the gateway turns it into head angles and sends
them to the robot. Camera frames never leave the Mac and are not saved.

What you need:

- A Mac (macOS 13 or later) with a camera: the built-in one, a Studio Display,
  an iPhone as Continuity Camera, or a USB webcam.
- A CoreS3 with the Stack-chan base and its two head servos, connected to the
  gateway (step 4 above).
- Firmware that handles the gateway's `head` message: a release newer than
  v0.2.1, or a build from `main`. Older firmware ignores the message and the
  head does not move.
- Swift 5.9 or later to build the tracker (Xcode, or the Command Line Tools:
  `xcode-select --install`).

### Build the tracker

From the repository root:

```bash
cd tools/vision-tracker
swift build -c release
```

This produces `tools/vision-tracker/.build/release/groki-vision-tracker`,
which is where the gateway looks for it.

### Allow camera access

macOS asks for camera permission the first time the tracker opens the camera,
and the permission belongs to the app that started it: your terminal (or
Claude Code's terminal, or Claude Desktop) when the gateway runs from there.
To get the prompt once, run the tracker by hand in the terminal you will
start the gateway from, click Allow, then stop it with Ctrl-C:

```bash
tools/vision-tracker/.build/release/groki-vision-tracker
```

If you missed the prompt, or the gateway runs as a background service, allow
the app in System Settings → Privacy & Security → Camera, then restart the
gateway. Without permission the tracker exits with code 2 and the gateway
retries with a growing delay (up to 5 minutes).

### Start it

Nothing extra: the gateway starts the tracker when it starts and restarts it
if it exits. The head follows faces from the start. If the tracker has not
been built, the gateway logs one line and runs normally without face
tracking:

```
face tracker unavailable: executable not found at .../tools/vision-tracker/.build/release/groki-vision-tracker (build with: cd tools/vision-tracker && swift build -c release); the head will not follow faces
```

To run the tracker yourself instead, set `STACKCHAN_FACE_TRACKER_AUTOSTART=0`
and start it with
`groki-vision-tracker --endpoint http://127.0.0.1:8766/track --fps 8`.

### Check that it works

1. The gateway log shows `face tracker started pid=<pid> endpoint=http://127.0.0.1:8766/track`,
   and the tracker prints `Vision tracker camera: <camera name>`.
2. Sit in front of the camera and open <http://127.0.0.1:8766/debug/status>.
   Under `face_tracking`: `tracker_running` is `true`, `face_reported` turns
   `true` and `last_face_at` keeps updating while the camera sees you,
   `head_follow` is `true` and `mode` is `idle`.
3. Move left and right: the head turns toward you and keeps following, within
   the robot's servo limits (by default ±40° left/right, -10° to +25° up/down).
   When the camera loses your face, the head stops following and after about
   two seconds the robot's own idle head motion takes over again.

### While you talk

Following pauses by design during a conversation: the gateway sends no head
moves while the robot speaks (`mode` is `working`) or while it listens after
the wake word (`mode` is `quiet`), so servo noise and motion do not get in
the way of the conversation. It follows again with the next detection once
the conversation is over. The firmware also holds a head command that arrives
while the robot is speaking and applies it when the reply ends.

### Turn head follow on and off

- By voice (Gemini or Grok gateway backend): "look at me" turns following on, "stop looking at
  me" turns it off. Face detection keeps running either way.
- `STACKCHAN_HEAD_FOLLOW_DEFAULT=0` starts the gateway with following off, for
  a robot without head servos or when you want the head still.
- `STACKCHAN_FACE_TRACKER_AUTOSTART=0` does not start the tracker at all, for
  a computer without a camera.

### Choose a camera

Without a setting the tracker uses a Studio Display camera first, then an
iPhone (Continuity Camera), then the first camera macOS lists. To pick one,
set `STACKCHAN_FACE_TRACKER_CAMERA` to part of its name (case-insensitive),
for example `STACKCHAN_FACE_TRACKER_CAMERA=FaceTime`. The tracker prints the
cameras it found when it starts.

## Optional: Grok Bot hand-off

The instructions below are the **optional CLI hand-off for Gemini voice**. Grok login voice uses native tools instead; see [Grok Bot login voice](#grok-bot-login-voice-macos). It does not run the port 18770 forwarding service.

With this on, you can say "Hi Grok, look up tomorrow's weather in Tokyo" or
"research X for me": Gemini acknowledges the accepted task, hands the task to an agent in the Grok Bot app, and reads the
agent's replies aloud as they come in. Small talk stays with Gemini. The full
message flow is in [docs/architecture.md](../docs/architecture.md#4-grok-bot-hand-off).

It is off unless you configure it. You need, on the same computer as the
gateway:

- The Grok Bot app, installed and signed in, with an agent (or group) that
  should receive tasks.
- The `gbot` command line tool from
  [grok-bot-cli](https://github.com/ScriptedAlchemy/grok-bot-cli) (needs a
  recent Node.js, see its README): `npm install --global grok-bot-cli`, then
  check with `gbot bots list`.
  `gbot` uses the app's session; the gateway never sees a Grok token.

Steps:

1. Pick the agent name from `gbot bots list` (or create one, for example
   `gbot bots create --name assistant`) and add it to `.env`:
   `STACKCHAN_TOOL_BOT=assistant`. This is what turns the feature on.
2. Start the forwarding service in a second terminal (or as a second
   background service) and leave it running:

   ```bash
   uv run --all-extras stackchan-gbot-proxy
   curl -s http://127.0.0.1:18770/health     # {"ok": true, "service": "gbot-http", ...}
   ```

3. Restart the gateway. Ask the robot to look something up.

Test the forwarding service without the robot:

```bash
curl -s -X POST http://127.0.0.1:18770/send \
  -H 'Content-Type: application/json' \
  -d '{"text": "Reply with one short sentence: hello", "target": "assistant"}'
```

## Configuration reference

All settings live in `.env` (read once at start; restart the gateway, or
reconnect the MCP client, after a change).

| Variable | Default | Meaning |
|---|---|---|
| `GEMINI_API_KEY` | none | Required only for the Gemini backend, not Grok login voice |
| `STACKCHAN_GEMINI_VOICE` | `Kore` | Gemini voice name |
| `STACKCHAN_GEMINI_MODEL` | `gemini-3.8-live` | Gemini Live model (set `gemini-3.1-flash-live-preview` to go back) |
| `STACKCHAN_TOKEN` | empty | Shared secret with the robot; also protects `/capture` and `/debug/inject-text` |
| `HOST`, `WS_PORT`, `CAPTURE_PORT` | `0.0.0.0`, `8765`, `8766` | Listen address and ports |
| `STACKCHAN_WS_PING_INTERVAL` | `60` | Device WebSocket ping interval in seconds. `0` disables pings |
| `STACKCHAN_WS_PING_TIMEOUT` | `60` | Seconds to wait for a pong before closing the device socket (close code 1011). `0` waits indefinitely |
| `STACKCHAN_WS_MAX_QUEUE` | `128` | Incoming frames buffered before the socket pauses reading. `0` disables that limit |
| `STACKCHAN_HELLO_VOICE_WAIT_S` | `2` | How long hello may wait for voice connect before answering; the rest continues in the background |
| `STACKCHAN_UPLINK_PREROLL_FRAMES` | `400` | Opus frames kept while voice is still connecting. `0` keeps every frame |
| `STACKCHAN_TTS_REPLAY_TTL_S` | `45` | How long audio the device missed (socket already closed) is replayed after reconnect. `0` disables replay |
| `STACKCHAN_WAKE_WORD` | on | `0` disables the wake word gate |
| `STACKCHAN_KWS_SCORE` | `7` | Higher makes the wake word easier to trigger |
| `STACKCHAN_KWS_THRESHOLD` | `0.05` | Lower makes the wake word more sensitive |
| `STACKCHAN_KWS_MODEL_DIR` | `./models/kws` | Wake word model location |
| `STACKCHAN_WAKE_PHRASE`, `STACKCHAN_WAKE_KEYWORD` | `hi grok` | Wake word; see [Changing the wake word](#changing-the-wake-word). `STACKCHAN_WAKE_PHRASE=hey groki` alone switches to "Hey Groki" |
| `STACKCHAN_WAKE_IDLE_S` | `30` | Silence before the listening window closes |
| `STACKCHAN_MAC_CONTROL` | off | `1` lets voice commands control this Mac (see Safety) |
| `STACKCHAN_FACE_TRACKER_AUTOSTART` | on | `0` does not start the Mac face tracker (computer without a camera) |
| `STACKCHAN_FACE_TRACKER_BIN` | `tools/vision-tracker/.build/release/groki-vision-tracker` | Face tracker executable |
| `STACKCHAN_FACE_TRACKER_CAMERA` | empty | Part of the camera name to use; empty uses the tracker's default order |
| `STACKCHAN_HEAD_FOLLOW_DEFAULT` | on | `0` starts with head follow off; "look at me" turns it on |
| `STACKCHAN_GEMINI_DEVICE_TOOLS` | off | `1` gives both voice backends face/LED/head tools. Firmware with device MCP gets them as MCP calls; the Groki Bot firmware gets them as XiaoZhi emotion, `led` and `head` messages (`head` needs firmware newer than v0.2.1) |
| `STACKCHAN_USB_TRANSPORT` | off | `1` enables the USB serial control channel; it locks `/dev/cu.usbmodem*` |
| `STACKCHAN_ASK_CLAUDE` | on when the `claude` CLI is found | `0` removes the `ask_claude` voice tool even when the CLI is installed |
| `STACKCHAN_CLAUDE_BIN` | `claude` on PATH | Claude CLI used by `ask_claude` |
| `STACKCHAN_CLAUDE_MODEL` | `claude-sonnet-5` | Model passed to the Claude CLI by `ask_claude` and Mac tasks |
| `STACKCHAN_VOICE_BACKEND` | `gemini` | `gbot` uses Grok login voice; `xiaozhi` forwards to XiaoZhi cloud |
| `STACKCHAN_GBOT_VOICE_BOT_ID` | none | Grok voice agent ID; helper and gateway must agree |
| `STACKCHAN_GBOT_VOICE_URL` | `http://127.0.0.1:18771` | Loopback HTTP login helper only |
| `STACKCHAN_GBOT_VOICE_LANGUAGE` | `zh` | Input transcription language, e.g. `en` or `auto` |
| `STACKCHAN_PERSONALITY_FILE` | `personality.md` in this directory, if present | Extra persona text appended to Gemini's instructions. The built-in persona is a short one named Groki that replies in the user's language; text in this file overrides it where they differ (name, language, style) |
| `STACKCHAN_TOOL_BOT` | empty (off) | Agent name; enables native Grok desktop access or Gemini `ask_grokbot` |
| `STACKCHAN_TOOL_BOT_ID` | empty | Required for native Grok; must match `STACKCHAN_GBOT_VOICE_BOT_ID`. Optional for Gemini |
| `STACKCHAN_TOOL_BOT_PREFIX` | built-in read-aloud hint | Text put in front of every task; set it empty to send tasks as is (Gemini CLI path only) |
| `STACKCHAN_GBOT_URL` | `http://127.0.0.1:18770` | Forwarding service address (Gemini CLI path only) |
| `STACKCHAN_ASK_TIMEOUT` | `110` | Seconds to wait for the agent's replies (Gemini CLI path only) |
| `STACKCHAN_GBOT_BIN` | `gbot` on PATH | Forwarding service: path to `gbot` (Gemini CLI path only) |
| `STACKCHAN_GBOT_HTTP_PORT` | `18770` | Forwarding service port (it always binds 127.0.0.1) (Gemini CLI path only) |

## Safety

- **Mac control is off by default.** With `STACKCHAN_MAC_CONTROL=1`, anyone
  within earshot of the robot can open apps and URLs, change volume, take
  screenshots, run Shortcuts, and start `claude -p` tasks that may edit files
  in the gateway directory. The voice prompt asks for spoken confirmation
  before destructive actions, but that is a model instruction, not a hard
  permission check.
- **Set `STACKCHAN_TOKEN`.** Without it, any device on your network can
  connect as the robot, and the `/debug/inject-text` endpoint accepts text
  from anyone who can reach port 8766.
- **The Grok Bot forwarding service binds 127.0.0.1 only.** Any program on
  this computer that can reach port 18770 can message your Grok Bot agents as
  you. Do not expose it to the network.
- **Keep keys out of Git.** `.env` is ignored by `.gitignore`. Do not paste
  keys into scripts, test files or issues.

## Troubleshooting

| Symptom | Check |
|---|---|
| Robot never connects | Same network? Firewall allows incoming 8765? URL starts with `ws://`, not `wss://`, and uses the computer's LAN IP. Log line `ESP32 auth rejected` means the token does not match. |
| Connects but never answers | `GEMINI_API_KEY` set? `/debug/status` → `gemini.last_error`. An error containing `reported as leaked` means Google disabled the key: create a new one. |
| Gemini connection times out | The computer must be able to reach Google's Gemini API. In regions where Google is blocked, run the gateway behind a working network proxy. |
| Wake word never triggers | Model downloaded to `models/kws`? Log shows `KWS ready`? Try `STACKCHAN_KWS_THRESHOLD=0.03`, or run `scripts/kws_offline_repro.py`. `STACKCHAN_WAKE_WORD=0` turns the gate off. |
| `sherpa-onnx unavailable` / `Library not loaded: libonnxruntime` | Run `uv sync --all-extras` again; it installs `sherpa-onnx-core`, which ships the runtime. |
| `Could not find Opus library` | Install Opus (`brew install opus` or `apt install libopus0`). |
| Head does not follow faces | `/debug/status` → `face_tracking`. `null`: gateway too old or not restarted. `tracker_running: false`: build the tracker, or check the log for `face tracker exited returncode=2` (no camera or no camera permission, see [Allow camera access](#allow-camera-access)). `face_reported: false`: the camera does not see a face. `head_follow: false`: say "look at me". Everything fine but the head is still: the firmware is older than the `head` message; update it. |
| Flashing fails with "port busy" | Stop the gateway if you enabled `STACKCHAN_USB_TRANSPORT`. |
| `.env` change has no effect | Restart the gateway; with setup A, reconnect the MCP server (`/mcp` in Claude Code). |
| Gemini hand-off says the task could not be sent | Is `stackchan-gbot-proxy` running (`curl http://127.0.0.1:18770/health`)? Does `gbot bots list` work in a terminal and list the name in `STACKCHAN_TOOL_BOT`? Is the Grok Bot app signed in? |

## MCP tools

| Tool | With Groki Bot firmware |
|---|---|
| `get_status` | Works: gateway and robot connection state |
| `speak(text)` | Works: the gateway synthesizes speech and plays it on the robot |
| `say(text, voice?)` | Needs a separately installed VOICEVOX engine |
| `set_voice_mode` | Advanced: routes transcribed speech into a terminal session; off by default |
| `get_device_info`, `set_volume`, `set_brightness`, `move_head`, `get_head_angles`, `set_avatar`, `set_mouth`, `set_mouth_sequence`, `set_blink`, `set_led`, `set_leds`, `set_all_leds`, `clear_leds`, `set_background_color`, `get_touch_state`, `take_photo`, `install_avatar_assets`, `check_vm_en`, `gpio_test`, `uart_diag` | Need firmware with XiaoZhi device MCP tools |

## Development

```bash
uv sync --all-extras
NO_PROXY="*" uv run --all-extras pytest -q
```

## Credits and license

This gateway is a fork of
[kisaragi-mochi/stackchan-mcp](https://github.com/kisaragi-mochi/stackchan-mcp),
which grew out of the [stack-chan](https://github.com/mongonta0716/stack-chan)
community. It speaks the [XiaoZhi](https://github.com/78/xiaozhi-esp32)
WebSocket protocol and uses [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)
for the wake word.

MIT License, see [LICENSE](LICENSE). The rest of the Groki Bot repository
(the firmware) is under its own license; see [../LICENSE](../LICENSE) and
[../THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md). The `gbot` CLI
([grok-bot-cli](https://github.com/ScriptedAlchemy/grok-bot-cli), MIT) is an
external tool and is not included.
