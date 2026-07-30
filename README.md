# sklad

Automation scripts and Telegram bot for MoySklad price conversion.

## Setup

Copy `.env.example` to `.env` and fill secrets:

```bash
cp .env.example .env
```

Install Python dependencies (needed for the `Отчет по закупкам` Excel export):

```bash
pip3 install -r requirements.txt
```

Required values:

- `MOYSKLAD_TOKEN` - MoySklad API token.
- `TELEGRAM_BOT_TOKEN` - service Telegram bot token.
- `TELEGRAM_ACCESS_PASSWORD` - admin password users enter in Telegram.
- `TELEGRAM_USER_ACCESS_PASSWORD` - regular user password for stock search only.

Optional (needed only for the `Проверить количество` button):

- `AVITO_CLIENT_ID` / `AVITO_CLIENT_SECRET` - Avito API credentials from
  avito.ru/professionals/api. If unset, the bot still runs; the button
  replies with a configuration error instead of a report.

By default, admin password is `1821`, regular user password is `123`.
Admins receive importcds error alerts and can convert prices. Regular users only see
the `Поиск по складу` button and do not receive alerts.

## `Проверить количество` report

Admin-only. Compares Avito listings against MoySklad stock and replies with an
Excel file. Takes several minutes and blocks the bot while it runs.

Avito listings carry **no quantity** for this account: one ad means "this title is
listed", not "one copy in stock". The Autoload API that exposes a real `Quantity`
field needs a paid tariff, and the public ad pages show no quantity either. So the
report compares *presence on Avito* against *MoySklad stock*, not two quantities.

Sheets:

- `Проверить` - needs action:
  - `Вероятно продано, нет отгрузки` - stock in MoySklad, but the Avito ad is
    closed. Usually means it sold on Avito and nobody created the shipment
    (отгрузка) in MoySklad. A heuristic: an ad can also close because its
    placement expired.
  - `На Avito, но склад 0` - ad is still running while MoySklad shows nothing left.
  - `Остаток неизвестен` - MoySklad returned no quantity for the item.
- `Не выставлено` - in stock, never had an Avito ad. Informational.
- `Без совпадения` - active Avito ads whose titles matched no MoySklad item
  (there is no shared SKU, so matching is by title). Worth reviewing for naming
  drift between the two systems.

Items that are consistent (listed and in stock, or absent and out of stock) are left
out of the file.

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
