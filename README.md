# sklad

Automation scripts and Telegram bot for MoySklad price conversion.

## Setup

Copy `.env.example` to `.env` and fill secrets:

```bash
cp .env.example .env
```

Required values:

- `MOYSKLAD_TOKEN` - MoySklad API token.
- `TELEGRAM_BOT_TOKEN` - service Telegram bot token.
- `TELEGRAM_ACCESS_PASSWORD` - password users enter in Telegram.

## Run Telegram bot locally

```bash
/usr/local/bin/python3 scripts/telegram_price_bot.py
```

Open the bot in Telegram, send `/start`, then enter the access password.

## Send test alert

```bash
/usr/local/bin/python3 scripts/telegram_alert.py "Test alert"
```

## systemd

Example unit files are in `deploy/systemd/`.
