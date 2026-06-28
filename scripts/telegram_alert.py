#!/usr/bin/env python3
from __future__ import annotations

import os
import sys

from telegram_price_bot import (
    AUTHORIZED_USERS_PATH,
    ENV_PATH,
    TelegramClient,
    create_ssl_context,
    load_authorized_chat_ids,
    load_dotenv,
    parse_allowed_chat_ids,
)


DEFAULT_ALERT_TEXT = "Внимание: бот importcds сломался или остановился."


def read_alert_text() -> str:
    if len(sys.argv) > 1:
        return " ".join(sys.argv[1:]).strip()

    if not sys.stdin.isatty():
        text = sys.stdin.read().strip()
        if text:
            return text

    return DEFAULT_ALERT_TEXT


def main() -> int:
    load_dotenv(ENV_PATH)

    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not telegram_token:
        print("Fill TELEGRAM_BOT_TOKEN in .env first", file=sys.stderr)
        return 1

    chat_ids = load_authorized_chat_ids(AUTHORIZED_USERS_PATH)
    chat_ids |= parse_allowed_chat_ids(os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", ""))
    if not chat_ids:
        print("No authorized Telegram users to notify", file=sys.stderr)
        return 1

    telegram = TelegramClient(telegram_token, create_ssl_context())
    alert_text = read_alert_text()

    failed = 0
    for chat_id in sorted(chat_ids):
        try:
            telegram.send_message(chat_id, alert_text)
        except Exception as error:
            failed += 1
            print(f"Failed to notify {chat_id}: {error}", file=sys.stderr)

    print(f"Alert sent to {len(chat_ids) - failed}/{len(chat_ids)} users")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
