# QQ AI Bot

最小链路：QQ 小号 -> NapCatQQ -> OneBot 11 WebSocket -> Python -> 大模型 API。

## 当前行为

- 私聊：直接发送任何文字即可进入 AI 对话。
- 群聊：只有 `@Bot` 后面的内容才会触发回复；普通群消息完全忽略。
- Bot 名称通过 `.env` 的 `BOT_NAME` 自定义，源码不绑定任何具体 QQ 昵称或 QQ 号。
- 每个私聊用户拥有独立上下文；群聊按“群 + 用户”隔离上下文。
- 私聊默认保留最近 8 轮，群聊默认保留最近 4 轮；可用 `/reset` 清空当前会话。
- 同一台机器只允许一个 Bot 实例运行，误启动第二个实例时会直接退出，避免双重回复。
- 相同 OneBot `message_id` 在短时间内重复上报时只处理一次，作为消息级幂等保护。

## 指令

- `/help`：查看帮助
- `/ping`：测试 Bot 是否在线
- `/reset`：清空当前会话上下文
- `/model`：查看当前模型
- `/status`：查看 Bot / LLM 配置状态

群聊中的指令也必须先 `@Bot`，例如：

```text
@Bot /ping
```

除上述已注册指令外，形如 `/smile`、`/doge` 的 `/ + 英文字母` token 会作为 QQ 表情码交给 AI 理解，而不是报“未知指令”。AI 回复时也允许少量使用这种格式；QQ 客户端能识别的代码会按客户端能力渲染。

## NapCat 配置

WebSocket 服务端：

- Host: `127.0.0.1`
- Port: `3001`
- 消息格式: `array`
- 上报自身消息: 关闭
- Token: 本机联调可留空
- 心跳间隔: `30000`

## 安装

```powershell
cd E:\study\qq-bot
uv sync
```

## 配置大模型

默认使用 DeepSeek 的 OpenAI-compatible Chat Completions 接口：

- Base URL: `https://api.deepseek.com`
- Model: `deepseek-v4-flash`
- 对 QQ 即时聊天，模板默认设置 `LLM_THINKING=disabled`，减少不必要的推理开销和延迟。使用不支持 DeepSeek `thinking` 参数的其他兼容服务时，把该项留空即可不发送此字段。

项目使用根目录下的 `.env` 保存本机配置和密钥。`.env` 与 `.env.*` 均已加入 `.gitignore`，`.env.example` 是唯一允许提交的模板。项目运行时只从环境变量 / `.env` 读取秘密，不把 API Key 写进源码、README 或 Git 历史。

首次配置可以从模板开始：

```powershell
cd E:\study\qq-bot
Copy-Item .env.example .env
```

然后填写 `.env` 中的：

```text
BOT_NAME=<YOUR_BOT_NAME>
LLM_API_KEY=<YOUR_API_KEY>
```

也可以用隐藏输入命令写入 `.env`：

```powershell
uv run python bot.py --set-api-key
```

保存后可检查：

```powershell
uv run python bot.py --check
```

如果需要删除本地 Key：

```powershell
uv run python bot.py --clear-api-key
```

以后换成其他 OpenAI-compatible 服务，只需要改 `.env` 中的 `LLM_BASE_URL`、`LLM_MODEL` 和 `LLM_API_KEY`，不需要改 Bot 主逻辑。

## Prompt Engineering

当前 Prompt 采用两层结构：

1. **固定身份层**：规定 Bot 的基本身份、准确性要求、默认语言和“不编造”原则。
2. **场景层**：私聊与群聊分别使用不同规则。

默认策略：

- 私聊：允许更完整的解释，保留最近 8 轮上下文，单次输出最多约 900 tokens。
- 群聊：偏即时聊天风格，通常控制在 2-6 句，保留最近 4 轮上下文，单次输出最多约 350 tokens。
- 群聊单条输入默认最多 4000 字符，私聊最多 12000 字符，避免意外超长请求消耗大量 token。

这些参数都可以通过环境变量调整，无需修改 QQ / NapCat 接入逻辑：

```powershell
$env:LLM_PRIVATE_MAX_TURNS='8'
$env:LLM_GROUP_MAX_TURNS='4'
$env:LLM_PRIVATE_MAX_TOKENS='900'
$env:LLM_GROUP_MAX_TOKENS='350'
$env:LLM_PRIVATE_MAX_INPUT_CHARS='12000'
$env:LLM_GROUP_MAX_INPUT_CHARS='4000'
```

运行安全参数也可以调整：

```powershell
$env:BOT_INSTANCE_LOCK_PORT='38451'
$env:MESSAGE_DEDUPE_TTL_SECONDS='600'
$env:MESSAGE_DEDUPE_MAX_IDS='4096'
```

Prompt 文本也可以临时覆盖：

```powershell
$env:LLM_BASE_SYSTEM_PROMPT='你的固定身份提示词'
$env:LLM_PRIVATE_SCENE_PROMPT='你的私聊场景提示词'
$env:LLM_GROUP_SCENE_PROMPT='你的群聊场景提示词'
```

`/status` 会显示当前 Prompt 版本、上下文轮数和输出上限，方便后续做 A/B 调整。

## 启动

```powershell
cd E:\study\qq-bot
uv run python bot.py
```

私聊示例：

```text
你好，解释一下动态规划
```

群聊示例：

```text
@Bot 帮我解释一下动态规划
```

## NapCat Token

如果 NapCat WebSocket 配置了 Token：

```powershell
$env:NAPCAT_TOKEN='你的 Token'
```

默认连接地址为 `ws://127.0.0.1:3001`，需要修改时设置 `NAPCAT_WS_URL`。
