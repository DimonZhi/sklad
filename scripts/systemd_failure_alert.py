#!/usr/bin/env python3
from __future__ import annotations

import os
import sys

from telegram_alert import (
    AUTHORIZED_USERS_PATH,
    ENV_PATH,
    TelegramClient,
    create_ssl_context,
    load_authorized_chat_ids,
    load_dotenv,
    parse_allowed_chat_ids,
)


SUCCESS_RESULTS = {"success", ""}


def should_send_alert() -> bool:
    return os.environ.get("SERVICE_RESULT", "").strip().lower() not in SUCCESS_RESULTS


def build_message(service_name: str) -> str:
    service_result = os.environ.get("SERVICE_RESULT", "unknown")
    exit_code = os.environ.get("EXIT_CODE", "unknown")
    exit_status = os.environ.get("EXIT_STATUS", "unknown")

    return (
        "Внимание: importcds упал или аварийно остановился.\n"
        f"Сервис: {service_name}\n"
        f"SERVICE_RESULT: {service_result}\n"
        f"EXIT_CODE: {exit_code}\n"
        f"EXIT_STATUS: {exit_status}"
    )


def main() -> int:
    if not should_send_alert():
        return 0

    service_name = sys.argv[1] if len(sys.argv) > 1 else "importcds.service"

    load_dotenv(ENV_PATH)
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not telegram_token:
        print("Fill TELEGRAM_BOT_TOKEN in .env first", file=sys.stderr)
        return 1

    chat_ids = load_authorized_chat_ids(AUTHORIZED_USERS_PATH)
    chat_ids |= parse_allowed_chat_ids(os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", ""))
    chat_ids |= parse_allowed_chat_ids(os.environ.get("TELEGRAM_ALERT_CHAT_IDS", ""))
    if not chat_ids:
        print("No authorized Telegram users to notify", file=sys.stderr)
        return 1

    telegram = TelegramClient(telegram_token, create_ssl_context())
    message = build_message(service_name)

    failed = 0
    for chat_id in sorted(chat_ids):
        try:
            telegram.send_message(chat_id, message)
        except Exception as error:
            failed += 1
            print(f"Failed to notify {chat_id}: {error}", file=sys.stderr)

    print(f"Systemd failure alert sent to {len(chat_ids) - failed}/{len(chat_ids)} users")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
