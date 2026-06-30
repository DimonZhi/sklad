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
- `TELEGRAM_ACCESS_PASSWORD` - admin password users enter in Telegram.
- `TELEGRAM_USER_ACCESS_PASSWORD` - regular user password for stock search only.

By default, admin password is `1821`, regular user password is `123`.
Admins receive importcds error alerts and can convert prices. Regular users only see
the `Поиск по складу` button and do not receive alerts.

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
