import argparse
import asyncio
import getpass
import json
import os
import re
import socket
import sys
import time
import uuid
from collections import OrderedDict, defaultdict

import httpx
from dotenv import load_dotenv, set_key, unset_key
from websockets.asyncio.client import connect


ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(ENV_PATH)


WS_URL = os.getenv("NAPCAT_WS_URL", "ws://127.0.0.1:3001")
TOKEN = os.getenv("NAPCAT_TOKEN", "")
BOT_NAME = os.getenv("BOT_NAME", "QQBot").strip() or "QQBot"

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-v4-flash")
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "60"))
LLM_THINKING = os.getenv("LLM_THINKING", "").strip().lower()
LLM_PRIVATE_MAX_TURNS = max(1, int(os.getenv("LLM_PRIVATE_MAX_TURNS", "8")))
LLM_GROUP_MAX_TURNS = max(1, int(os.getenv("LLM_GROUP_MAX_TURNS", "4")))
LLM_PRIVATE_MAX_TOKENS = max(64, int(os.getenv("LLM_PRIVATE_MAX_TOKENS", "900")))
LLM_GROUP_MAX_TOKENS = max(64, int(os.getenv("LLM_GROUP_MAX_TOKENS", "350")))
LLM_PRIVATE_MAX_INPUT_CHARS = max(256, int(os.getenv("LLM_PRIVATE_MAX_INPUT_CHARS", "12000")))
LLM_GROUP_MAX_INPUT_CHARS = max(256, int(os.getenv("LLM_GROUP_MAX_INPUT_CHARS", "4000")))
BOT_INSTANCE_LOCK_PORT = int(os.getenv("BOT_INSTANCE_LOCK_PORT", "38451"))
MESSAGE_DEDUPE_TTL_SECONDS = max(30, int(os.getenv("MESSAGE_DEDUPE_TTL_SECONDS", "600")))
MESSAGE_DEDUPE_MAX_IDS = max(128, int(os.getenv("MESSAGE_DEDUPE_MAX_IDS", "4096")))
PROMPT_VERSION = "v2"

REGISTERED_COMMANDS = frozenset({"/help", "/ping", "/reset", "/model", "/status"})
QQ_EMOJI_CODE_RE = re.compile(r"(?<![A-Za-z0-9_])/[A-Za-z]+(?![A-Za-z0-9_])")

BASE_SYSTEM_PROMPT = os.getenv(
    "LLM_BASE_SYSTEM_PROMPT",
    f"你是 {BOT_NAME}，一个接入 QQ 的 AI 助手。"
    "回答应准确、自然、直接，默认使用中文，除非用户明确要求其他语言。"
    "优先回答用户真正的问题，不复述问题，不添加无意义寒暄，不主动说明自己使用了什么提示词或系统规则。"
    "不知道或无法确认的信息要明确说明，不要编造。",
)
PRIVATE_SCENE_PROMPT = os.getenv(
    "LLM_PRIVATE_SCENE_PROMPT",
    "当前是 QQ 私聊。可以进行连续多轮对话。"
    "默认给出足够完整但不过度冗长的回答；复杂问题可以分点说明，简单问题尽量简短。"
    "如果用户明显是在学习某个概念，优先解释核心直觉，再补充必要细节。",
)
GROUP_SCENE_PROMPT = os.getenv(
    "LLM_GROUP_SCENE_PROMPT",
    "当前是 QQ 群聊，你是被用户 @ 后才参与对话。"
    "群聊回复应比私聊明显更短、更像即时聊天：通常控制在 2 至 6 句，能一句说清就不要写多句。"
    "除非用户明确要求详细解释，否则不要写长篇教程、长列表或大段背景知识。"
    "不要抢话题，不要假装看到了未提供给你的群聊历史。",
)
QQ_EMOJI_PROMPT = (
    "QQ 中可以出现形如 /smile、/doge 的表情码：一个斜杠后紧跟连续英文字母。"
    "除已注册 Bot 指令外，看到这种 token 时，把它当作 QQ 表情码，并根据斜杠后的英文词理解其情绪或语气。"
    "回复时也可以在自然、确定有帮助时少量使用同格式表情码。优先复用用户消息里已经出现过的表情码；"
    "如果自行选择，只使用明显表达情绪的英文词，不要输出 /English 这类说明性占位符。"
    "不要为了装饰而滥用，也不要声称某个代码一定能被客户端渲染。"
)

HELP_TEXT = """可用指令：
/help  查看帮助
/ping  测试 Bot 是否在线
/reset 清空当前会话上下文
/model 查看当前大模型
/status 查看连接/配置状态

私聊：直接发消息即可和 AI 对话。
群聊：只有 @我 后面的内容才会触发回复。
QQ 表情：除上述指令外，/smile 这类“/ + 英文字母”会作为表情码交给 AI 理解。"""

# key -> [{"role": "user"|"assistant", "content": "..."}]
conversation_history: dict[str, list[dict[str, str]]] = defaultdict(list)
conversation_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
processed_message_ids: OrderedDict[str, float] = OrderedDict()

# Keeping this socket open is the process-wide singleton lock.  It prevents a
# second bot process on the same machine from connecting to NapCat and replying
# to every message a second time.
_instance_lock_socket: socket.socket | None = None

def build_headers() -> dict[str, str] | None:
    if not TOKEN:
        return None
    return {"Authorization": f"Bearer {TOKEN}"}


def acquire_instance_lock() -> bool:
    global _instance_lock_socket
    if _instance_lock_socket is not None:
        return True

    lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Do not enable SO_REUSEADDR: binding this loopback port is the lock.
        lock_socket.bind(("127.0.0.1", BOT_INSTANCE_LOCK_PORT))
        lock_socket.listen(1)
    except OSError:
        lock_socket.close()
        return False

    _instance_lock_socket = lock_socket
    return True


def release_instance_lock() -> None:
    global _instance_lock_socket
    if _instance_lock_socket is None:
        return
    _instance_lock_socket.close()
    _instance_lock_socket = None


def message_dedupe_key(event: dict) -> str | None:
    message_id = event.get("message_id")
    if message_id is None or message_id == "":
        return None
    return ":".join(
        str(value or "")
        for value in (
            event.get("self_id"),
            event.get("message_type"),
            event.get("group_id"),
            event.get("user_id"),
            message_id,
        )
    )


def is_duplicate_message(event: dict, *, now: float | None = None) -> bool:
    key = message_dedupe_key(event)
    if key is None:
        return False

    current = time.monotonic() if now is None else now
    cutoff = current - MESSAGE_DEDUPE_TTL_SECONDS
    while processed_message_ids:
        _, oldest_seen = next(iter(processed_message_ids.items()))
        if oldest_seen >= cutoff:
            break
        processed_message_ids.popitem(last=False)

    if key in processed_message_ids:
        processed_message_ids.move_to_end(key)
        return True

    processed_message_ids[key] = current
    while len(processed_message_ids) > MESSAGE_DEDUPE_MAX_IDS:
        processed_message_ids.popitem(last=False)
    return False


def llm_configured() -> bool:
    return bool(get_llm_api_key())


def get_llm_api_key() -> str:
    return os.getenv("LLM_API_KEY", "") or os.getenv("DEEPSEEK_API_KEY", "")


def set_llm_api_key() -> int:
    api_key = getpass.getpass("LLM API Key (input hidden): ").strip()
    if not api_key:
        print("[error] empty API key; nothing changed")
        return 1
    set_key(ENV_PATH, "LLM_API_KEY", api_key, quote_mode="never")
    print("[ok] API key saved to local .env")
    return 0


def clear_llm_api_key() -> int:
    unset_key(ENV_PATH, "LLM_API_KEY")
    print("[ok] API key cleared from .env")
    return 0


def build_chat_url() -> str:
    # Compatible with base URLs such as:
    # https://api.deepseek.com, https://api.openai.com/v1, https://host.example/v1
    if LLM_BASE_URL.endswith("/v1"):
        return f"{LLM_BASE_URL}/chat/completions"
    return f"{LLM_BASE_URL}/chat/completions"


async def send_action(ws, action: str, params: dict) -> None:
    payload = {
        "action": action,
        "params": params,
        "echo": str(uuid.uuid4()),
    }
    await ws.send(json.dumps(payload, ensure_ascii=False))


async def send_text_reply(ws, event: dict, text: str) -> None:
    message_type = event.get("message_type")
    if message_type == "private":
        user_id = event.get("user_id")
        if user_id is None:
            return
        await send_action(ws, "send_private_msg", {"user_id": user_id, "message": text})
        print(f"[reply] private user={user_id}: {text[:80]!r}")
        return

    if message_type == "group":
        group_id = event.get("group_id")
        if group_id is None:
            return
        await send_action(ws, "send_group_msg", {"group_id": group_id, "message": text})
        print(f"[reply] group={group_id}: {text[:80]!r}")


async def send_long_reply(ws, event: dict, text: str, chunk_size: int = 3500) -> None:
    text = (text or "").strip()
    if not text:
        text = "模型没有返回可显示的内容。"
    for start in range(0, len(text), chunk_size):
        await send_text_reply(ws, event, text[start : start + chunk_size])


def message_segments(event: dict) -> list[dict]:
    message = event.get("message")
    if isinstance(message, list):
        return [segment for segment in message if isinstance(segment, dict)]
    return []


def group_mentions_bot(event: dict) -> bool:
    self_id = str(event.get("self_id") or "")
    if not self_id:
        return False

    for segment in message_segments(event):
        if segment.get("type") != "at":
            continue
        data = segment.get("data") or {}
        if str(data.get("qq") or "") == self_id:
            return True

    # Fallback for string/CQ-code formatted messages.
    raw = str(event.get("raw_message") or "")
    return bool(re.search(rf"\[CQ:at,qq={re.escape(self_id)}(?:,[^\]]*)?\]", raw))


def extract_text(event: dict, *, remove_bot_mention: bool) -> str:
    segments = message_segments(event)
    self_id = str(event.get("self_id") or "")

    if segments:
        parts: list[str] = []
        for segment in segments:
            segment_type = segment.get("type")
            data = segment.get("data") or {}
            if segment_type == "text":
                parts.append(str(data.get("text") or ""))
            elif segment_type == "at" and not (
                remove_bot_mention and str(data.get("qq") or "") == self_id
            ):
                qq = str(data.get("qq") or "")
                if qq:
                    parts.append(f"@{qq}")
        return "".join(parts).strip()

    raw = str(event.get("raw_message") or "")
    if remove_bot_mention and self_id:
        raw = re.sub(
            rf"\[CQ:at,qq={re.escape(self_id)}(?:,[^\]]*)?\]",
            " ",
            raw,
        )
    return raw.strip()


def conversation_key(event: dict) -> str:
    user_id = event.get("user_id")
    if event.get("message_type") == "group":
        return f"group:{event.get('group_id')}:user:{user_id}"
    return f"private:{user_id}"


def conversation_scene(event: dict) -> str:
    return "group" if event.get("message_type") == "group" else "private"


def extract_qq_emoji_codes(text: str) -> list[str]:
    """Return distinct /English QQ emoji codes, excluding registered commands."""
    codes: list[str] = []
    seen: set[str] = set()
    for match in QQ_EMOJI_CODE_RE.finditer(text):
        code = match.group(0)
        lowered = code.lower()
        if lowered in REGISTERED_COMMANDS or lowered in seen:
            continue
        seen.add(lowered)
        codes.append(code)
    return codes


def scene_system_prompt(scene: str) -> str:
    scene_prompt = GROUP_SCENE_PROMPT if scene == "group" else PRIVATE_SCENE_PROMPT
    return f"{BASE_SYSTEM_PROMPT}\n\n{scene_prompt}\n\n{QQ_EMOJI_PROMPT}"


def scene_max_turns(scene: str) -> int:
    return LLM_GROUP_MAX_TURNS if scene == "group" else LLM_PRIVATE_MAX_TURNS


def scene_max_tokens(scene: str) -> int:
    return LLM_GROUP_MAX_TOKENS if scene == "group" else LLM_PRIVATE_MAX_TOKENS


def scene_max_input_chars(scene: str) -> int:
    return LLM_GROUP_MAX_INPUT_CHARS if scene == "group" else LLM_PRIVATE_MAX_INPUT_CHARS


def trim_history(messages: list[dict[str, str]], *, scene: str) -> list[dict[str, str]]:
    max_messages = scene_max_turns(scene) * 2
    if len(messages) <= max_messages:
        return messages
    return messages[-max_messages:]


def build_llm_payload(history: list[dict[str, str]], user_text: str, *, scene: str) -> dict:
    system_prompt = scene_system_prompt(scene)
    emoji_codes = extract_qq_emoji_codes(user_text)
    if emoji_codes:
        system_prompt += (
            "\n\n本条用户消息中识别到这些 QQ 表情码："
            + "、".join(emoji_codes)
            + "。结合斜杠后的英文词理解它们表达的情绪或语气。"
        )
    messages = [
        {"role": "system", "content": system_prompt},
        *history,
        {"role": "user", "content": user_text},
    ]
    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "stream": False,
        "max_tokens": scene_max_tokens(scene),
    }
    # DeepSeek V4 supports an explicit thinking switch. Leave this field out
    # when LLM_THINKING is empty so other OpenAI-compatible providers do not
    # receive a provider-specific parameter.
    if LLM_THINKING in {"enabled", "disabled"}:
        payload["thinking"] = {"type": LLM_THINKING}
    return payload


async def ask_llm(key: str, user_text: str, *, scene: str) -> str:
    api_key = get_llm_api_key()
    if not api_key:
        raise RuntimeError("LLM_API_KEY 未配置")

    async with conversation_locks[key]:
        history = conversation_history[key]

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = build_llm_payload(history, user_text, scene=scene)

        async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
            response = await client.post(build_chat_url(), headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()

        try:
            answer = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("大模型返回格式无法识别") from exc

        answer = str(answer or "").strip()
        if not answer:
            raise RuntimeError("大模型返回了空内容")

        history.extend(
            [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": answer},
            ]
        )
        conversation_history[key] = trim_history(history, scene=scene)
        return answer


async def handle_command(ws, event: dict, text: str) -> bool:
    command = text.split(maxsplit=1)[0].lower()
    if command not in REGISTERED_COMMANDS:
        return False

    if command == "/ping":
        await send_text_reply(ws, event, "pong")
    elif command == "/help":
        await send_text_reply(ws, event, HELP_TEXT)
    elif command == "/reset":
        conversation_history.pop(conversation_key(event), None)
        await send_text_reply(ws, event, "当前会话上下文已清空。")
    elif command == "/model":
        await send_text_reply(ws, event, f"当前模型：{LLM_MODEL}")
    elif command == "/status":
        state = "已配置" if llm_configured() else "未配置"
        await send_text_reply(
            ws,
            event,
            f"Bot：{BOT_NAME} / 在线\n模型：{LLM_MODEL}\nLLM API：{state}\n群聊触发：仅 @Bot\n"
            f"Prompt：{PROMPT_VERSION}\n"
            f"上下文：私聊 {LLM_PRIVATE_MAX_TURNS} 轮 / 群聊 {LLM_GROUP_MAX_TURNS} 轮\n"
            f"输出上限：私聊 {LLM_PRIVATE_MAX_TOKENS} tokens / 群聊 {LLM_GROUP_MAX_TOKENS} tokens",
        )
    return True


async def handle_event(ws, event: dict) -> None:
    if event.get("post_type") != "message":
        return

    if is_duplicate_message(event):
        print(f"[dedupe] ignored repeated message_id={event.get('message_id')}")
        return

    self_id = event.get("self_id")
    user_id = event.get("user_id")
    if self_id is not None and user_id is not None and str(self_id) == str(user_id):
        return

    message_type = event.get("message_type")
    if message_type not in {"private", "group"}:
        return

    if message_type == "group":
        if not group_mentions_bot(event):
            return
        text = extract_text(event, remove_bot_mention=True)
    else:
        text = extract_text(event, remove_bot_mention=False)

    if not text:
        await send_text_reply(ws, event, "在的。@我后直接说问题就可以，发送 /help 查看指令。")
        return

    if await handle_command(ws, event, text):
        return

    scene = conversation_scene(event)
    max_input_chars = scene_max_input_chars(scene)
    if len(text) > max_input_chars:
        await send_text_reply(
            ws,
            event,
            f"这条消息太长了（{len(text)} 字符）。当前{('群聊' if scene == 'group' else '私聊')}"
            f"单条上限是 {max_input_chars} 字符，请缩短或分开发送。",
        )
        return

    try:
        answer = await ask_llm(
            conversation_key(event),
            text,
            scene=scene,
        )
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        print(f"[llm-error] HTTP {status}: {exc}")
        await send_text_reply(ws, event, f"大模型请求失败（HTTP {status}）。")
        return
    except httpx.HTTPError as exc:
        print(f"[llm-error] network: {exc}")
        await send_text_reply(ws, event, "连接大模型服务失败，请稍后再试。")
        return
    except Exception as exc:
        print(f"[llm-error] {type(exc).__name__}: {exc}")
        if "LLM_API_KEY" in str(exc):
            await send_text_reply(ws, event, "大模型 API Key 还没有配置。发送 /status 可以查看状态。")
        else:
            await send_text_reply(ws, event, "大模型暂时无法回答，请稍后再试。")
        return

    await send_long_reply(ws, event, answer)


async def check_connection() -> int:
    print(f"[check] connecting to {WS_URL}")
    try:
        async with connect(WS_URL, additional_headers=build_headers(), open_timeout=5) as ws:
            echo = str(uuid.uuid4())
            await ws.send(json.dumps({"action": "get_login_info", "params": {}, "echo": echo}))

            async with asyncio.timeout(5):
                while True:
                    raw = await ws.recv()
                    data = json.loads(raw)
                    if data.get("echo") == echo:
                        if data.get("status") == "ok":
                            info = data.get("data") or {}
                            print(
                                f"[ok] NapCat connected. QQ={info.get('user_id')} "
                                f"nickname={info.get('nickname')}"
                            )
                            print(
                                f"[ok] LLM model={LLM_MODEL}; "
                                f"api_key={'configured' if llm_configured() else 'NOT configured'}"
                            )
                            return 0
                        print(f"[error] OneBot response: {data}")
                        return 1
    except Exception as exc:
        print(f"[error] cannot connect to NapCat: {exc}")
        return 1


async def run_bot() -> None:
    while True:
        print(f"[connect] {WS_URL}")
        try:
            async with connect(WS_URL, additional_headers=build_headers()) as ws:
                print(
                    "[ready] QQ Bot is online. Private messages respond directly; "
                    "group messages require @Bot."
                )
                print(
                    f"[ready] LLM={LLM_MODEL}; "
                    f"api_key={'configured' if llm_configured() else 'NOT configured'}"
                )
                async for raw in ws:
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    await handle_event(ws, event)
        except asyncio.CancelledError:
            raise
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"[disconnect] {exc}; retrying in 3 seconds...")
            await asyncio.sleep(3)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NapCatQQ OneBot 11 AI bot")
    parser.add_argument("--check", action="store_true", help="check NapCat/LLM configuration and exit")
    parser.add_argument("--set-api-key", action="store_true", help="store LLM API key securely")
    parser.add_argument("--clear-api-key", action="store_true", help="clear stored LLM API key")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        if args.set_api_key:
            raise SystemExit(set_llm_api_key())
        if args.clear_api_key:
            raise SystemExit(clear_llm_api_key())
        if args.check:
            raise SystemExit(asyncio.run(check_connection()))
        if not acquire_instance_lock():
            print(
                "[error] another qq-bot instance is already running "
                f"(instance lock port {BOT_INSTANCE_LOCK_PORT} is in use)"
            )
            raise SystemExit(2)
        try:
            asyncio.run(run_bot())
        finally:
            release_instance_lock()
    except KeyboardInterrupt:
        print("\n[stop] bot stopped")
        sys.exit(0)
