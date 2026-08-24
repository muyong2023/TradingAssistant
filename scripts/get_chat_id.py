#!/usr/bin/env python3
"""从 Telegram getUpdates 取出 chat_id，并写回 config/.env。

用法：先给你的 bot 发一条消息，然后
    python3 scripts/get_chat_id.py
"""
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / "config" / ".env"


def read_env() -> dict:
    if not ENV_PATH.exists():
        sys.exit(f"找不到 {ENV_PATH}")
    env = {}
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def get_updates(token: str) -> list:
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as e:
        # 不要把 token 回显到报错里
        sys.exit(f"Telegram 返回 HTTP {e.code}。401 通常意味着 token 不对或已被 revoke。")
    except urllib.error.URLError as e:
        sys.exit(f"连不上 Telegram：{e.reason}")
    if not payload.get("ok"):
        sys.exit(f"Telegram 拒绝了请求：{payload.get('description')}")
    return payload.get("result", [])


def extract_chats(updates: list) -> dict:
    """返回 {chat_id: 描述}，按出现顺序去重。"""
    chats = {}
    for upd in updates:
        msg = (
            upd.get("message")
            or upd.get("channel_post")
            or upd.get("edited_message")
            or {}
        )
        chat = msg.get("chat")
        if not chat:
            continue
        cid = chat["id"]
        if cid in chats:
            continue
        kind = chat.get("type", "?")
        name = chat.get("title") or " ".join(
            filter(None, [chat.get("first_name"), chat.get("last_name")])
        ) or chat.get("username", "")
        chats[cid] = f"{kind}: {name}".strip()
    return chats


def write_chat_id(chat_id: int) -> None:
    text = ENV_PATH.read_text()
    new_text, n = re.subn(
        r"^TELEGRAM_CHAT_ID=.*$", f"TELEGRAM_CHAT_ID={chat_id}", text, flags=re.M
    )
    if n == 0:
        new_text = text.rstrip("\n") + f"\nTELEGRAM_CHAT_ID={chat_id}\n"
    ENV_PATH.write_text(new_text)


def main() -> None:
    env = read_env()
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        sys.exit(f"{ENV_PATH} 里的 TELEGRAM_BOT_TOKEN 还是空的，先把它填上。")

    chats = extract_chats(get_updates(token))
    if not chats:
        sys.exit(
            "getUpdates 没返回任何对话。请在 Telegram 里打开你的 bot、点 Start 或发一句话，"
            "然后重跑。\n"
            "（注意：Telegram 只保留 24 小时内的 update；若 bot 已设置过 webhook，"
            "getUpdates 会一直为空。）"
        )

    print("找到以下对话：")
    for cid, desc in chats.items():
        print(f"  {cid}\t{desc}")

    if len(chats) > 1:
        print("\n多于一个对话，没有自动写入。把你要用的那个 id 告诉我，或手动填进 config/.env。")
        return

    chat_id = next(iter(chats))
    write_chat_id(chat_id)
    print(f"\n已写入 config/.env：TELEGRAM_CHAT_ID={chat_id}")


if __name__ == "__main__":
    main()
