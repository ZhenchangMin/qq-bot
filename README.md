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

## QQ 表情

Bot 不依赖 `/xxx` 文本让 QQ 客户端自行猜测表情。NapCat / OneBot 11 会把 QQ 内置表情作为 `face` 消息段上报和发送：

```json
{"type":"face","data":{"id":"182"}}
```

- 收到常用 QQ `face` 时，Bot 会把对应语义（例如“笑哭”“发怒”“点赞”）提供给大模型理解。
- 大模型如果想使用表情，只能选择代码内的白名单语义标签；发送前 Bot 会把标签转换成真正的 OneBot `face` 段。
- 内部标签不会直接发给 QQ 用户，未知标签会被丢弃。
- 这样不会出现模型随意输出一个 `/xxx`，但 QQ 客户端并不识别、最终把原始文本展示给用户的问题。

当前输出白名单使用 NapCat 当前支持的真实 QQ face ID，覆盖：呲牙、流泪、大哭、害羞、尴尬、发怒、酷、白眼、流汗、拥抱、爱心、赞、胜利、鼓掌、可怜、笑哭、沉思、狂笑、拜谢、牛啊、耶、emo。后续要扩展时应增加明确的 `face id`，而不是让模型自由生成文本快捷码。

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
