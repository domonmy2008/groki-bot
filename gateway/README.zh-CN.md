[English](README.md) | **简体中文**

# Groki Bot 网关

运行在你电脑上的一个 Python 服务，给 [Groki Bot](../README.zh-CN.md) 桌面机器人提供语音能力。它在 Groki Bot 仓库的 `gateway/` 目录；它和固件、Grok Bot 怎么配合，见[架构与通信说明](../docs/architecture.zh-CN.md)。

- **语音对话**：默认通过 Google Gemini Live，也可选择 Grok 登录态语音，用机器人的麦克风和喇叭对话。
- **「Hi Grok」唤醒词**：在电脑上识别，喊到机器人之后网关才把麦克风声音发给所选语音后端。
- **MCP 服务**：Claude Code、Claude Desktop 或其他 MCP 客户端可以让机器人说话、读取它的状态。
- **人脸追踪**：在带摄像头的 Mac 上，机器人转头跟着你的脸（见[人脸追踪](#人脸追踪)）。
- 可选：用语音控制你的 Mac；把难题交给 Claude CLI 回答的 `ask_claude` 语音工具（装了 `claude` 命令行才提供）；Gemini 后端把任务交给 Grok Bot 应用里的智能体、再把回复念出来的 `ask_grokbot` 语音工具（见[可选：把任务交给 Grok Bot](#可选把任务交给-grok-bot)）。

```text
Robot ← XiaoZhi WebSocket :8765 → Gateway ← MCP → Claude / MCP client
Gateway voice → Gemini Live (default) / Grok login voice
Grok voice → localhost:18771 login helper → native Grok Bot
Gemini ask_grokbot → optional localhost:18770 → gbot CLI → Grok Bot
```

## 需不需要装网关

Groki Bot 固件自己就能对话：刷好固件，在设置页填上自己的 OpenAI 或 Gemini 密钥，不接电脑也能说话。以下情况再装网关：

- 想用「Hi Grok」唤醒词；
- 想让机器人转头跟着你的脸（需要带摄像头的 Mac）；
- 想让 Claude（或其他 MCP 客户端）让机器人说话；
- 想用语音操作电脑（Mac 控制、`ask_claude`）。

**配合 Groki Bot 固件能用的部分**：语音对话、唤醒词、人脸追踪，以及 `speak`、`get_status` 两个 MCP 工具。Groki Bot 固件的 XiaoZhi 客户端声明 `features.mcp=false`，所以驱动硬件的 MCP 工具（`move_head`、`set_led`、`set_avatar`、`take_photo` 等）在这个固件上会返回错误；它们需要实现了 XiaoZhi 设备端 MCP 工具的固件。人脸追踪不走 MCP，它用 XiaoZhi 的 `head` 消息转头，固件能处理这条消息。两个语音后端共用的设备工具（`STACKCHAN_GEMINI_DEVICE_TOOLS=1`）也不需要 MCP：配合这个固件时，`move_head` 以 `head` 消息发送（需要 v0.2.1 之后的固件），`set_avatar` 以表情消息发送，`set_all_leds` 以 `led` 消息发送。摄像头类工具没有这样的通道，在这个固件上不能用。

## 准备

| 项目 | 要求 |
|---|---|
| 机器人 | M5Stack CoreS3 加 Stack-chan 底座，刷好 [Groki Bot 固件](../README.zh-CN.md) |
| 电脑 | macOS（在 Apple Silicon 上测试过）。Linux 预计可以跑语音；Mac 控制只支持 macOS。 |
| 网络 | 机器人和电脑在同一个 Wi-Fi 或局域网 |
| 软件 | [uv](https://docs.astral.sh/uv/)、Git、Opus 音频库（macOS 用 `brew install opus`，Debian/Ubuntu 用 `sudo apt install libopus0`） |
| 密钥 | Gemini Live 用的 Google AI Studio 密钥：<https://aistudio.google.com/apikey> |

## 1. 给机器人刷固件

按 [Groki Bot README 的用法 1](../docs/voice-backends.zh-CN.md#用法-1只刷固件) 操作（用 Chrome 或 Edge 打开网页刷写，可以不填 API 密钥）。机器人开机并连上 Wi-Fi 后回到这里。

## 2. 安装网关

```bash
git clone https://github.com/JosssphZhou/groki-bot.git
cd groki-bot/gateway
uv sync --all-extras
cp .env.example .env
```

打开 `.env`，选择语音后端并设置设备令牌：

- 默认 Gemini：填写 `GEMINI_API_KEY`。Grok 登录态语音按下节配置，不需要这把密钥。
- `STACKCHAN_TOKEN`：一串随机密码，例如用 `openssl rand -hex 16` 生成。建议填写；只有在完全信任的网络里才留空。

注意：PyPI 上名为 `stackchan-mcp` 的包是上游项目（[kisaragi-mochi/stackchan-mcp](https://github.com/kisaragi-mochi/stackchan-mcp)），不含 Gemini 语音和唤醒词。请按上面的方式从本仓库安装。

### Grok Bot 登录态语音（macOS）

默认后端仍是 Gemini。要改用 Grok 语音，请在同一台 Mac 上安装并登录 Grok Bot 桌面应用，并准备好 Node.js。当前接入基于 Grok Bot 0.71.2 的内部接口，后续应用更新、账号权限与使用额度都可能影响可用性。不需要另配 xAI 语音 API 密钥，也不用重新刷写固件。

安装网关后，在 `gateway/.env` 填入自己的智能体名称与 ID：

```dotenv
STACKCHAN_VOICE_BACKEND=gbot
STACKCHAN_GBOT_VOICE_BOT_ID=YOUR_AGENT_ID
STACKCHAN_TOOL_BOT=助手
STACKCHAN_TOOL_BOT_ID=YOUR_AGENT_ID
```

另开一个终端窗口，在 `gateway/` 中启动登录桥并保持运行：

```sh
STACKCHAN_GBOT_VOICE_BOT_ID=YOUR_AGENT_ID \
STACKCHAN_TOOL_BOT=助手 \
STACKCHAN_TOOL_BOT_ID=YOUR_AGENT_ID \
node scripts/gbot_voice_session_bridge.mjs
```

Node 登录桥不会自动读取 `.env`，因此需要如上显式传入相同配置。它读取本机应用的现有登录状态和系统钥匙串，凭据仅保留在内存中，且仅监听 `127.0.0.1:18771`。接着按第 3 步的任意一种方式启动 Python 网关；该后端不需要 `GEMINI_API_KEY`。若要切回 Gemini，将 `STACKCHAN_VOICE_BACKEND` 改回 `gemini`，保留原 Gemini 密钥并重启网关即可。

也可以安装为 macOS 用户登录项，避免关闭终端或重启电脑后登录桥退出。在 `gateway/` 执行（ID 与上面配置一致）：

```sh
python3 scripts/install_gbot_voice_launchd.py --agent-id YOUR_AGENT_ID --enable-desktop --enable-tools
```

安装项在登录时启动，进程退出后会自动重启。需要预览配置可加 `--dry-run`；更新已有安装可加 `--replace`，会先备份旧配置，并中断当前语音通话。安装后保持仓库路径不变，重启 Python 网关，再对机器人说一句话确认回复。登录项仅管理登录桥，不启动 Python 网关，也不修复上游登录或桌面沙盒故障。`http://127.0.0.1:18771/health` 只确认桥进程存活；日志位于 `~/Library/Logs/stackchan-gbot-voice.log`。

网关会每 30 秒在当前语音连接上确认服务端仍能响应，不播放测试声音。初次连接失败、连接断开、服务端不回应或回复停滞时，网关会清理旧音频并自动重连；重试间隔从 1 秒逐步延长，最长 30 秒。不会重新发送之前的提问或桌面任务。登录失效或接口不兼容仍需处理账户或更新程序。

`http://127.0.0.1:8766/debug/voice-health` 返回真实语音连接状态：可用时 HTTP 200，正在连接、恢复或不可用时 HTTP 503。`action_required` 表示需要处理登录或账户，`last_error` 仅包含固定错误分类。端口以网关实际配置为准。

可以在 `gateway/` 运行独立恢复检查，使用真实 Grok 连接，模拟登录桥暂时不可用并断开测试连接。它不会中断机器人当前通话：

```sh
PYTHONPATH=. python scripts/probe_gbot_voice_recovery.py --agent-id YOUR_AGENT_ID --output-dir /tmp/groki-recovery-check
```

显式配置智能体名称与匹配的 ID 后，Grok 直接调用原生的 `send_task`、`search_conversations` 与 `end_the_call`，不再增加本地 `ask_grokbot` 包装，也不经过 CLI 转发服务。本地硬件和可选 Mac 工具沿用原有的开关，包括名称仍为 `STACKCHAN_GEMINI_DEVICE_TOOLS` 的设备工具开关。注意会话搜索不等于网页搜索；旧有的 `STACKCHAN_GBOT_VOICE_TOOLS` 开关不会开放未知原生工具。

桌面沙盒环境不可用时，语音仍可正常使用，原生通道会在后台持续重试。在在线状态（presence）和事件流就绪前，任务分配与会话搜索会明确返回暂不可用。受理仅代表系统已接收任务，不代表桌面端聊天界面已可见送达。桌面文字消息订阅仍可能报错，会自动独立重连，断开期间遗漏的消息不会补发。当前版本并未宣称已实现完全的功能一致，也未验证支持纯声学打断。详见[验证范围](GBOT_VOICE_VALIDATION.md)和[延迟测量方法](VOICE_LATENCY_BENCHMARK.md)。

### 唤醒词模型（可选，约 33 MB）

```bash
mkdir -p models/kws && cd models/kws
curl -L -O https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2
tar xf sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2
rm sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2
cd ../..
```

没有模型网关也能用，但机器人连着的时候，麦克风听到的所有声音都会发给所选语音后端。离线检查模型（macOS，会调用系统的 `say` 命令）：

```bash
uv run --all-extras python scripts/kws_offline_repro.py --generate-say
# 命中时输出 hit，没命中时输出 miss
```

### 更换唤醒词

默认唤醒词是「Hi Grok」。唤醒词检测（sherpa-onnx 关键词识别）匹配的是一串读音，换成任何短语都不需要重新训练模型。在 `.env` 里设置后重启网关：

- 改用「Hey Groki」：`STACKCHAN_WAKE_PHRASE=hey groki`（内置，和「Hi Grok」一样带几种元音读法）。
- 自定义短语：`STACKCHAN_WAKE_PHRASE` 填短语，`STACKCHAN_WAKE_KEYWORD` 填它的读音，后面接 `@` 和短语。英文用 CMU 词典音标（ARPAbet，带重音数字）；中文用拼音，声母和韵母分开写，韵母带声调。每个读音都必须出现在模型的 `tokens.txt` 里。例如：

  ```bash
  STACKCHAN_WAKE_PHRASE=小机器人
  STACKCHAN_WAKE_KEYWORD=x iǎo j ī q ì r én @小机器人

  STACKCHAN_WAKE_PHRASE=hello robot
  STACKCHAN_WAKE_KEYWORD=HH AH0 L OW1 R OW1 B AA2 T @hello_robot
  ```

换了唤醒词先离线试一下：`kws_offline_repro.py` 读取同样的设置（`--voice` 换一个 macOS 声音，`--wav` 用自己的录音）。两到三个音节、辅音清楚的短语效果最好；太难唤醒或太容易误触发时，调整 `STACKCHAN_KWS_THRESHOLD` 和 `STACKCHAN_KWS_SCORE`。

## 3. 启动和检查

```bash
uv run --all-extras stackchan-mcp --check     # 检查配置和端口，立即退出
```

然后用下面两种方式之一启动网关。

**A. 让 Claude Code 启动（最简单）**：Claude Code 开着的时候网关就在运行。

```bash
claude mcp add groki -- uv run --all-extras --directory "$(pwd)" stackchan-mcp
```

Claude Desktop 在 `claude_desktop_config.json` 里加入下面的配置（路径换成你克隆的绝对路径）：

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

**B. 作为后台服务常驻**：不开 MCP 客户端也想和机器人对话时用这种方式。

```bash
./scripts/run_stackchan_gateway_launchd.sh
```

这个脚本让标准输入保持打开，stdio MCP 服务就不会退出。想开机自动启动，可以让 launchd（macOS）或 systemd（Linux）运行它。

8765 和 8766 端口同一时间只能给一个网关用，A 和 B 选一种。

运行起来后，日志里会出现 `server listening on 0.0.0.0:8765`，打开 <http://127.0.0.1:8766/debug/status> 会返回 JSON。

## 4. 让机器人连上网关

在 Groki Bot 蓝牙设置页（<https://josssphzhou.github.io/groki-bot/settings.html>，桌面版 Chrome 或 Edge）的「对话」标签页：

1. 服务商选「XiaoZhi 服务器」。
2. 「XiaoZhi 服务器 URL」填 `ws://<电脑的局域网 IP>:8765`（macOS 用 `ipconfig getifaddr en0` 查 IP，Linux 用 `hostname -I`）。
3. 「XiaoZhi 令牌」填和 `STACKCHAN_TOKEN` 相同的值；`STACKCHAN_TOKEN` 留空时这里也留空。
4. 点「保存并重启」。

这时 `/debug/status` 应显示 `"device": {"connected": true, ...}`。说「Hi Grok」就可以开始对话（设了 `STACKCHAN_WAKE_PHRASE=hey groki` 时说「Hey Groki」，见[更换唤醒词](#更换唤醒词)）。

## 人脸追踪

机器人转头跟着你的脸。Mac 上的一个小程序 [tools/vision-tracker](../tools/vision-tracker/README.md) 用 Apple Vision 看摄像头画面，把人脸位置发到网关的 `http://127.0.0.1:8766/track`；网关把位置换算成头部角度发给机器人。摄像头画面不离开这台 Mac，也不保存。

需要：

- 一台带摄像头的 Mac（macOS 13 或更新）：内置摄像头、Studio Display、用作连续互通相机的 iPhone，或 USB 摄像头都可以。
- CoreS3 加 Stack-chan 底座和两个头部舵机，并且已经连上网关（上面第 4 步）。
- 能处理网关 `head` 消息的固件：v0.2.1 之后的版本，或者从 `main` 编译的固件。旧固件会忽略这条消息，头不会动。
- 编译追踪程序需要 Swift 5.9 或更新（Xcode，或命令行工具：`xcode-select --install`）。

### 编译追踪程序

在仓库根目录运行：

```bash
cd tools/vision-tracker
swift build -c release
```

产物是 `tools/vision-tracker/.build/release/groki-vision-tracker`，网关默认就在这个位置找它。

### 允许使用摄像头

追踪程序第一次打开摄像头时，macOS 会弹窗询问权限，权限记在启动它的那个程序名下：网关在终端里运行时就是这个终端（或 Claude Code 所在的终端、Claude Desktop）。想先把弹窗处理掉，可以在之后启动网关的那个终端里手动运行一次追踪程序，点「允许」，再按 Ctrl-C 结束：

```bash
tools/vision-tracker/.build/release/groki-vision-tracker
```

错过了弹窗，或者网关作为后台服务运行时，到「系统设置 → 隐私与安全性 → 摄像头」里允许对应的程序，再重启网关。没有权限时追踪程序以退出码 2 退出，网关会隔一段时间再试，间隔逐次加长，最长 5 分钟。

### 启动

不需要额外操作：网关启动时自动拉起追踪程序，程序退出了会自动重启。头只在唤醒之后跟随人脸；没唤醒时停在原点。追踪程序还没编译时，网关只记一行日志，其余功能照常运行：

```
face tracker unavailable: executable not found at .../tools/vision-tracker/.build/release/groki-vision-tracker (build with: cd tools/vision-tracker && swift build -c release); the head will not follow faces
```

想自己手动运行追踪程序，设 `STACKCHAN_FACE_TRACKER_AUTOSTART=0`，然后运行 `groki-vision-tracker --endpoint http://127.0.0.1:8766/track --fps 8`。

### 确认在工作

1. 网关日志出现 `face tracker started pid=<进程号> endpoint=http://127.0.0.1:8766/track`，追踪程序输出 `Vision tracker camera: <摄像头名>`。
2. 坐到摄像头前，打开 <http://127.0.0.1:8766/debug/status>，看 `face_tracking` 一节：`tracker_running` 是 `true`；摄像头看到你时 `face_reported` 变成 `true`，`last_face_at` 不断更新；`head_follow` 是 `true`，`mode` 是 `idle`。
3. 先说唤醒词，再左右移动：头会转向你并持续跟随，角度在舵机限位之内（默认左右 ±40°，上下 -10° 到 +25°）。没唤醒时，以及门关上之后，头停在原点（yaw 0、pitch 45，再按舵机限位夹紧），不会自己东张西望。

### 只有唤醒态才动

人脸跟随只在唤醒门开着的时候跑（说完「Hi Grok」后 `mode` 为 `quiet`）。机器人说话时（`mode` 为 `working`）暂停跟随。门回到休眠（超时，或「退下」/结束通话）后，网关把头发回原点并保持不动。关掉唤醒词（直通）时，自动跟随和空闲张望都保持关闭；当前这句话里主动发来的转头指令仍然可以动。机器人说话时收到的转头指令，固件也会先存着，等这段回复说完再执行。

### 打开和关闭转头跟随

- 用语音（Gemini 或 Grok 网关后端）：说「看着我」打开跟随，说「别看了」关闭。人脸识别在两种状态下都继续运行。
- `STACKCHAN_HEAD_FOLLOW_DEFAULT=0`：网关启动时不跟随，适合没装头部舵机或想让头保持不动的情况。
- `STACKCHAN_FACE_TRACKER_AUTOSTART=0`：完全不启动追踪程序，适合没有摄像头的电脑。

### 选择摄像头

不设置时，追踪程序先选 Studio Display 的摄像头，其次是 iPhone（连续互通相机），再其次是 macOS 列出的第一个摄像头。想指定某个摄像头，把 `STACKCHAN_FACE_TRACKER_CAMERA` 设为它名字里的一段（不区分大小写），例如 `STACKCHAN_FACE_TRACKER_CAMERA=FaceTime`。追踪程序启动时会列出它找到的所有摄像头。

## 可选：把任务交给 Grok Bot

以下是 **Gemini 语音后端的可选 CLI 联动**。Grok 登录态语音直接走原生工具，见 [Grok Bot 登录态语音](#grok-bot-登录态语音macos)，不启动 18770 转发服务。

打开这项功能后，可以说「Hi Grok，帮我查一下明天东京的天气」或「帮我调研一下 X」：Gemini 确认受理后回应，把任务交给 Grok Bot 应用里的智能体，智能体每回一段话，机器人就念一段。闲聊仍由 Gemini 直接回答。完整的消息流程见[架构与通信说明](../docs/architecture.zh-CN.md#4-把任务交给-grok-bot)。

不配置就不会开启。需要在运行网关的同一台电脑上准备：

- 装好并登录 Grok Bot 应用，里面有一个接任务的智能体（或群组）。
- [grok-bot-cli](https://github.com/ScriptedAlchemy/grok-bot-cli) 提供的 `gbot` 命令行工具（需要较新的 Node.js，版本要求见它的 README）：`npm install --global grok-bot-cli`，装好后用 `gbot bots list` 检查。`gbot` 使用应用的登录状态，网关不接触任何 Grok 令牌。

步骤：

1. 从 `gbot bots list` 里选一个智能体名字（也可以新建，例如 `gbot bots create --name 助手`），写进 `.env`：`STACKCHAN_TOOL_BOT=助手`。填了这一项功能才会开启。
2. 另开一个终端（或另设一个后台服务）启动转发服务，保持运行：

   ```bash
   uv run --all-extras stackchan-gbot-proxy
   curl -s http://127.0.0.1:18770/health     # {"ok": true, "service": "gbot-http", ...}
   ```

3. 重启网关，让机器人帮你查点东西。

不接机器人，单独测试转发服务：

```bash
curl -s -X POST http://127.0.0.1:18770/send \
  -H 'Content-Type: application/json' \
  -d '{"text": "用一句话回答：你好", "target": "助手"}'
```

## 配置项

所有配置写在 `.env` 里，启动时读取一次；修改后要重启网关，或者重新连接 MCP 客户端。

| 变量 | 默认值 | 作用 |
|---|---|---|
| `GEMINI_API_KEY` | 无 | 仅 Gemini 后端需要，Grok 登录态语音不需要 |
| `STACKCHAN_GEMINI_VOICE` | `Kore` | Gemini 的音色 |
| `STACKCHAN_GEMINI_MODEL` | `gemini-3.8-live` | Gemini Live 模型（改回 `gemini-3.1-flash-live-preview` 即可退回） |
| `STACKCHAN_TOKEN` | 空 | 与机器人共享的密码，同时保护 `/capture` 和 `/debug/inject-text` |
| `HOST`、`WS_PORT`、`CAPTURE_PORT` | `0.0.0.0`、`8765`、`8766` | 监听地址和端口 |
| `STACKCHAN_WS_PING_INTERVAL` | `60` | 设备 WebSocket 的 ping 间隔（秒）。`0` 表示不发送 ping |
| `STACKCHAN_WS_PING_TIMEOUT` | `60` | 等多久收不到 pong 就关闭设备连接（关闭码 1011）。`0` 表示一直等 |
| `STACKCHAN_WS_MAX_QUEUE` | `128` | 入站帧堆积到多少后暂停从 socket 读取。`0` 表示不限制 |
| `STACKCHAN_HELLO_VOICE_WAIT_S` | `2` | hello 最多等待语音连接的秒数，超时后先回复，其余连接在后台继续 |
| `STACKCHAN_UPLINK_PREROLL_FRAMES` | `400` | 语音尚未连上时保留的 Opus 帧数。`0` 表示全部保留 |
| `STACKCHAN_TTS_REPLAY_TTL_S` | `45` | 设备连接已关闭、没能播出的音频，在重连后保留并重放的秒数。`0` 关闭重放 |
| `STACKCHAN_WAKE_WORD` | 开 | 设为 `0` 关闭唤醒词 |
| `STACKCHAN_KWS_SCORE` | `7` | 越高越容易唤醒 |
| `STACKCHAN_KWS_THRESHOLD` | `0.05` | 越低越灵敏 |
| `STACKCHAN_KWS_MODEL_DIR` | `./models/kws` | 唤醒词模型位置 |
| `STACKCHAN_WAKE_PHRASE`、`STACKCHAN_WAKE_KEYWORD` | `hi grok` | 唤醒词，见[更换唤醒词](#更换唤醒词)。只设 `STACKCHAN_WAKE_PHRASE=hey groki` 即可改用「Hey Groki」 |
| `STACKCHAN_WAKE_IDLE_S` | `30` | 安静多久后结束这一轮聆听 |
| `STACKCHAN_MAC_CONTROL` | 关 | 设为 `1` 允许语音控制这台 Mac（见「安全」） |
| `STACKCHAN_FACE_TRACKER_AUTOSTART` | 开 | 设为 `0` 时不启动 Mac 人脸追踪程序（没有摄像头的电脑） |
| `STACKCHAN_FACE_TRACKER_BIN` | `tools/vision-tracker/.build/release/groki-vision-tracker` | 人脸追踪程序的路径 |
| `STACKCHAN_FACE_TRACKER_CAMERA` | 空 | 要用的摄像头名字里的一段；留空按追踪程序的默认顺序选 |
| `STACKCHAN_HEAD_FOLLOW_DEFAULT` | 开 | 设为 `0` 时网关启动后不转头跟随，说「看着我」再打开 |
| `STACKCHAN_GEMINI_DEVICE_TOOLS` | 关 | 设为 `1` 让两个语音后端使用表情、灯光、头部工具。带设备端 MCP 的固件通过 MCP 调用收到；Groki Bot 固件通过 XiaoZhi 的表情、`led`、`head` 消息收到（`head` 需要 v0.2.1 之后的固件） |
| `STACKCHAN_USB_TRANSPORT` | 关 | 设为 `1` 启用 USB 串口控制通道；它会独占 `/dev/cu.usbmodem*` |
| `STACKCHAN_ASK_CLAUDE` | 找到 `claude` 命令行时开启 | 设为 `0` 时即使装了命令行也不提供 `ask_claude` 语音工具 |
| `STACKCHAN_CLAUDE_BIN` | PATH 里的 `claude` | `ask_claude` 使用的 Claude CLI |
| `STACKCHAN_CLAUDE_MODEL` | `claude-sonnet-5` | `ask_claude` 和 Mac 后台任务调用 Claude CLI 时使用的模型 |
| `STACKCHAN_VOICE_BACKEND` | `gemini` | `gbot` 使用 Grok 登录态语音；`xiaozhi` 转发到 XiaoZhi 云服务 |
| `STACKCHAN_GBOT_VOICE_BOT_ID` | 无 | Grok 语音智能体 ID，登录桥与网关必须一致 |
| `STACKCHAN_GBOT_VOICE_URL` | `http://127.0.0.1:18771` | 仅本机 HTTP 登录桥地址 |
| `STACKCHAN_GBOT_VOICE_LANGUAGE` | `zh` | 输入转写语言，如 `en` 或 `auto` |
| `STACKCHAN_PERSONALITY_FILE` | 本目录的 `personality.md`（存在时） | 追加到 Gemini 指令后面的人设文字。内置人设很简短：名字叫 Groki，用用户说话的语言回答；这个文件里的内容和它不一致时（名字、语言、风格）以文件为准 |
| `STACKCHAN_TOOL_BOT` | 空（关闭） | 目标智能体名字；启用 Grok 原生桌面联动，或 Gemini 的 `ask_grokbot` |
| `STACKCHAN_TOOL_BOT_ID` | 空 | Grok 原生路径必填，须匹配 `STACKCHAN_GBOT_VOICE_BOT_ID`；Gemini 路径可选 |
| `STACKCHAN_TOOL_BOT_PREFIX` | 内置的朗读提示 | 加在每个任务前面的文字；设为空则原样发送任务（仅 Gemini CLI 路径） |
| `STACKCHAN_GBOT_URL` | `http://127.0.0.1:18770` | 转发服务地址（仅 Gemini CLI 路径） |
| `STACKCHAN_ASK_TIMEOUT` | `110` | 等智能体回复的秒数（仅 Gemini CLI 路径） |
| `STACKCHAN_GBOT_BIN` | PATH 里的 `gbot` | 转发服务使用的 `gbot` 路径（仅 Gemini CLI 路径） |
| `STACKCHAN_GBOT_HTTP_PORT` | `18770` | 转发服务端口（始终只绑定 127.0.0.1）（仅 Gemini CLI 路径） |


## 延迟相关 `.env` 旋钮（本地）

首包可听延迟（TTFA）主要来自云端 Grok 语音（约 ~3s），不是下面这些本地旋钮。
当前调优值与含义见 [`.env.latency.example`](.env.latency.example)：

| 变量 | 当前调优 | 含义 |
|---|---|---|
| `STACKCHAN_GEMINI_TTS_START_DELAY_SECONDS` | `0.04` | `tts.start` 后稍等再发首帧，避免与 Listening 竞态 |
| `STACKCHAN_GEMINI_PREBUFFER_MS` | `80` | 起播前预缓冲毫秒数，缓解下行抖动 |
| `STACKCHAN_WAKE_CONFIRM_HOLD_S` | `0.15` | 唤醒确认表情停留时间（仅 UX） |
| `STACKCHAN_GBOT_UPLINK_RMS_LOG` | `0` | 上行 RMS 调试日志，常态关闭 |

复制到 `gateway/.env` 后重启网关即可；不要提交真实 `.env`。

## 安全

- **Mac 控制默认关闭。** 设了 `STACKCHAN_MAC_CONTROL=1` 之后，任何在机器人旁边说话的人都能打开应用和网址、调音量、截屏、运行快捷指令，还能启动可能修改网关目录下文件的 `claude -p` 任务。语音提示词要求破坏性操作先口头确认，但这只是给模型的指令，不是强制的权限检查。
- **请设置 `STACKCHAN_TOKEN`。** 不设的话，同一网络里的任何设备都能冒充机器人连上来，能访问 8766 端口的人也能往 `/debug/inject-text` 发文字。
- **Grok Bot 转发服务只绑定 127.0.0.1。** 这台电脑上能访问 18770 端口的任何程序，都能以你的身份给 Grok Bot 智能体发消息。不要把它暴露到网络上。
- **密钥不要进 Git。** `.gitignore` 已经忽略 `.env`。不要把密钥写进脚本、测试文件或 issue。

## 常见问题

| 现象 | 排查 |
|---|---|
| 机器人一直连不上 | 是否在同一网络？防火墙是否允许 8765 入站？地址是否以 `ws://` 开头（不是 `wss://`）并且用的是电脑的局域网 IP？日志出现 `ESP32 auth rejected` 表示 token 不一致。 |
| 连上了但不回答 | 是否填了 `GEMINI_API_KEY`？看 `/debug/status` 里的 `gemini.last_error`。错误里有 `reported as leaked` 表示 Google 已经停用这把密钥，需要新建一把。 |
| 连接 Gemini 超时 | 电脑需要能访问 Google 的 Gemini 接口。在无法直连 Google 的地区，需要让网关通过可用的网络代理运行。 |
| 喊了唤醒词没反应 | 模型是否下载到 `models/kws`？日志里有没有 `KWS ready`？可以试 `STACKCHAN_KWS_THRESHOLD=0.03`，或运行 `scripts/kws_offline_repro.py`。`STACKCHAN_WAKE_WORD=0` 可关闭唤醒词。 |
| 报 `sherpa-onnx unavailable` 或 `Library not loaded: libonnxruntime` | 重新运行 `uv sync --all-extras`，它会安装自带运行库的 `sherpa-onnx-core`。 |
| 报 `Could not find Opus library` | 安装 Opus（`brew install opus` 或 `apt install libopus0`）。 |
| 头不跟着人脸转 | 看 `/debug/status` 的 `face_tracking`。是 `null`：网关版本太旧或没有重启。`tracker_running: false`：先编译追踪程序，或在日志里找 `face tracker exited returncode=2`（没有摄像头或没有摄像头权限，见[允许使用摄像头](#允许使用摄像头)）。`face_reported: false`：摄像头没看到人脸。`head_follow: false`：说「看着我」。这些都正常但头不动：固件太旧，不认 `head` 消息，需要更新固件。 |
| 刷固件提示串口被占用 | 如果开了 `STACKCHAN_USB_TRANSPORT`，先停掉网关。 |
| 改了 `.env` 不生效 | 重启网关；用方式 A 时在 Claude Code 里用 `/mcp` 重新连接。 |
| Gemini 路径提示任务没送到 | `stackchan-gbot-proxy` 在运行吗（`curl http://127.0.0.1:18770/health`）？终端里 `gbot bots list` 能用、并且列出了 `STACKCHAN_TOOL_BOT` 里的名字吗？Grok Bot 应用登录了吗？ |

## MCP 工具

| 工具 | 配合 Groki Bot 固件 |
|---|---|
| `get_status` | 可用：网关和机器人的连接状态 |
| `speak(text)` | 可用：网关合成语音，在机器人上播放 |
| `say(text, voice?)` | 需要另外安装 VOICEVOX 引擎 |
| `set_voice_mode` | 进阶功能：把转写的语音送进终端会话，默认关闭 |
| `get_device_info`、`set_volume`、`set_brightness`、`move_head`、`get_head_angles`、`set_avatar`、`set_mouth`、`set_mouth_sequence`、`set_blink`、`set_led`、`set_leds`、`set_all_leds`、`clear_leds`、`set_background_color`、`get_touch_state`、`take_photo`、`install_avatar_assets`、`check_vm_en`、`gpio_test`、`uart_diag` | 需要带 XiaoZhi 设备端 MCP 工具的固件 |

## 开发

```bash
uv sync --all-extras
NO_PROXY="*" uv run --all-extras pytest -q
```

## 致谢与许可证

本网关派生自 [kisaragi-mochi/stackchan-mcp](https://github.com/kisaragi-mochi/stackchan-mcp)，该项目源自 [stack-chan](https://github.com/mongonta0716/stack-chan) 社区。网关使用 [XiaoZhi](https://github.com/78/xiaozhi-esp32) 的 WebSocket 协议，唤醒词使用 [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)。

采用 MIT 许可证，见 [LICENSE](LICENSE)。Groki Bot 仓库的其余部分（固件）使用各自的许可证，见 [../LICENSE](../LICENSE) 和 [../THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。`gbot` 命令行（[grok-bot-cli](https://github.com/ScriptedAlchemy/grok-bot-cli)，MIT）是外部工具，不包含在本仓库。
