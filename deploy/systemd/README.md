# systemd deployment

Assumed server paths:

- `/opt/sklad` - service bot project
- `/opt/importcds` - main `importcds` bot project

If your paths differ, replace them in the unit files.

## Install units

```bash
sudo cp /opt/sklad/deploy/systemd/telegram-price-bot.service.example /etc/systemd/system/telegram-price-bot.service
sudo cp /opt/sklad/deploy/systemd/importcds.service.example /etc/systemd/system/importcds.service
sudo systemctl daemon-reload
```

## Start services

```bash
sudo systemctl enable --now telegram-price-bot.service
sudo systemctl enable --now importcds.service
```

## Check status and logs

```bash
systemctl status telegram-price-bot.service
systemctl status importcds.service
journalctl -u telegram-price-bot.service -f
journalctl -u importcds.service -f
```

## Test alerts

First, open the service Telegram bot and enter the access password so your chat id is saved in:

```text
/opt/sklad/data/telegram_authorized_users.json
```

Send a direct test alert:

```bash
/usr/local/bin/python3 /opt/sklad/scripts/telegram_alert.py "Test: importcds alert"
```

Simulate a hard crash of `importcds`:

```bash
sudo systemctl kill -s SIGKILL importcds.service
```

`systemd` should run `ExecStopPost`, send the alert, then restart `importcds`.

Do not use `systemctl stop importcds.service` for this test: a manual stop has `SERVICE_RESULT=success`, so `systemd_failure_alert.py` skips the alert.
