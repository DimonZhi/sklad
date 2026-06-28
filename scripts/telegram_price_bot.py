#!/usr/bin/env python3
from __future__ import annotations

import gzip
import json
import os
import ssl
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"
DATA_DIR = PROJECT_ROOT / "data"
AUTHORIZED_USERS_PATH = DATA_DIR / "telegram_authorized_users.json"

DEFAULT_MOYSKLAD_BASE_URL = "https://api.moysklad.ru/api/remap/1.2"
TELEGRAM_API_BASE_URL = "https://api.telegram.org"

SUPPLY_SEARCH_FIELD = "name"
ROUND_FINAL_PRICE_RUB_TO = Decimal("1")
RESET_DISCOUNT_AFTER_UPDATE = True

TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
CONVERT_BUTTON = "Конвертировать цены в приемке"
DEFAULT_TELEGRAM_ACCESS_PASSWORD = "1821"


class BotError(RuntimeError):
    pass


class MoySkladError(RuntimeError):
    pass


@dataclass
class DialogState:
    step: str
    supply_number: str | None = None
    usd_to_rub_rate: Decimal | None = None


@dataclass
class PriceUpdateResult:
    supply_name: str
    supply_id: str
    positions_found: int
    positions_updated: int
    total_old_usd: Decimal
    total_new_rub: Decimal
    duplicate_count: int


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "y", "on"}


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_authorized_chat_ids(path: Path) -> set[int]:
    if not path.exists():
        return set()

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise BotError(f"Cannot read authorized users file: {path}") from error

    raw_ids = data.get("chat_ids", []) if isinstance(data, dict) else data
    chat_ids: set[int] = set()
    for raw_id in raw_ids:
        try:
            chat_ids.add(int(raw_id))
        except (TypeError, ValueError):
            continue
    return chat_ids


def save_authorized_chat_ids(path: Path, chat_ids: set[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"chat_ids": sorted(chat_ids)}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def add_authorized_chat_id(path: Path, chat_ids: set[int], chat_id: int) -> None:
    if chat_id in chat_ids:
        return
    chat_ids.add(chat_id)
    save_authorized_chat_ids(path, chat_ids)


def create_ssl_context() -> ssl.SSLContext:
    ca_bundle = os.environ.get("MOYSKLAD_CA_BUNDLE", "").strip()
    if ca_bundle:
        return ssl.create_default_context(cafile=ca_bundle)

    if env_flag("MOYSKLAD_INSECURE_SSL"):
        print(
            "Warning: SSL certificate verification is disabled via MOYSKLAD_INSECURE_SSL.",
            file=sys.stderr,
        )
        return ssl._create_unverified_context()

    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()

    return ssl.create_default_context(cafile=certifi.where())


def decode_response(body: bytes, content_encoding: str | None) -> str:
    if content_encoding and "gzip" in content_encoding.lower():
        body = gzip.decompress(body)
    return body.decode("utf-8")


def encode_body(body: dict[str, Any] | list[Any] | None) -> bytes | None:
    if body is None:
        return None
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def parse_api_error(text: str) -> str:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text.strip() or "empty response"

    errors = data.get("errors")
    if isinstance(errors, list) and errors:
        return "; ".join(str(error.get("error") or error) for error in errors)

    description = data.get("description")
    if description:
        return str(description)

    return text.strip() or "empty response"


def to_decimal(value: Any, name: str) -> Decimal:
    text = str(value).strip().replace(",", ".")
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError, TypeError) as error:
        raise BotError(f"Некорректное значение для {name}: {value!r}") from error


def api_money_to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value)) / Decimal("100")
    except (InvalidOperation, ValueError, TypeError) as error:
        raise MoySkladError(f"Invalid price value from API: {value!r}") from error


def decimal_to_api_money(value: Decimal) -> int:
    return int((value * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise BotError(f"Шаг округления должен быть больше нуля: {step}")
    return (value / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step


def discounted_price(price: Decimal, discount_percent: Decimal) -> Decimal:
    multiplier = (Decimal("100") - discount_percent) / Decimal("100")
    return price * multiplier


def supply_number_candidates(raw_number: str) -> list[str]:
    number = raw_number.strip()
    candidates = [number]
    if number.isdigit():
        candidates.extend(number.zfill(width) for width in (4, 5, 6))

    seen: set[str] = set()
    unique: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            unique.append(candidate)
            seen.add(candidate)
    return unique


class MoySkladClient:
    def __init__(self, token: str, base_url: str, ssl_context: ssl.SSLContext) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.ssl_context = ssl_context

    def get(self, path_or_url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self._build_url(path_or_url, params)
        return self._request_json("GET", url)

    def put(self, path_or_url: str, body: dict[str, Any]) -> dict[str, Any]:
        url = self._build_url(path_or_url, None)
        return self._request_json("PUT", url, body)

    def _build_url(self, path_or_url: str, params: dict[str, Any] | None) -> str:
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            url = path_or_url
        else:
            url = f"{self.base_url}/{path_or_url.lstrip('/')}"

        if params:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{urlencode(params)}"

        return url

    def _request_json(
        self,
        method: str,
        url: str,
        body: dict[str, Any] | list[Any] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json;charset=utf-8",
            "Accept-Encoding": "gzip",
            "Content-Type": "application/json",
        }

        for attempt in range(MAX_RETRIES + 1):
            request = Request(url, data=encode_body(body), method=method, headers=headers)
            try:
                with urlopen(
                    request,
                    timeout=TIMEOUT_SECONDS,
                    context=self.ssl_context,
                ) as response:
                    text = decode_response(response.read(), response.headers.get("Content-Encoding"))
                    return json.loads(text) if text.strip() else {}
            except HTTPError as error:
                text = decode_response(error.read(), error.headers.get("Content-Encoding"))
                if error.code == 429 or 500 <= error.code < 600:
                    if attempt < MAX_RETRIES:
                        retry_after = error.headers.get("Retry-After")
                        delay = int(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                        time.sleep(delay)
                        continue

                message = parse_api_error(text)
                raise MoySkladError(f"HTTP {error.code}: {message}") from error
            except URLError as error:
                if attempt < MAX_RETRIES:
                    time.sleep(2**attempt)
                    continue
                raise MoySkladError(f"Network error: {error.reason}") from error
            except json.JSONDecodeError as error:
                raise MoySkladError(f"API returned invalid JSON from {url}") from error

        raise MoySkladError("Request failed after retries")


def iter_collection(
    client: MoySkladClient,
    path: str,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    next_url: str | None = None

    while True:
        data = client.get(next_url or path, None if next_url else params)
        batch = data.get("rows")
        if not isinstance(batch, list):
            raise MoySkladError(f"Expected collection response for {path}")

        rows.extend(batch)
        next_url = data.get("meta", {}).get("nextHref")
        if not next_url:
            return rows


def find_supply(client: MoySkladClient, supply_number: str) -> tuple[dict[str, Any], int]:
    rows_by_id: dict[str, dict[str, Any]] = {}
    for candidate in supply_number_candidates(supply_number):
        rows = iter_collection(
            client,
            "/entity/supply",
            {
                "filter": f"{SUPPLY_SEARCH_FIELD}={candidate}",
                "limit": 100,
                "order": "moment,desc",
            },
        )
        for row in rows:
            row_id = row.get("id")
            if row_id:
                rows_by_id[str(row_id)] = row

    rows = list(rows_by_id.values())
    if not rows:
        variants = ", ".join(supply_number_candidates(supply_number))
        raise MoySkladError(f"Приемка не найдена. Проверенные номера: {variants}")

    rows.sort(key=lambda row: str(row.get("moment", "")), reverse=True)
    return rows[0], len(rows)


def update_supply_prices(
    client: MoySkladClient,
    supply_number: str,
    usd_to_rub_rate: Decimal,
    delivery_price_rub: Decimal,
) -> PriceUpdateResult:
    supply, duplicate_count = find_supply(client, supply_number)
    supply_id = str(supply.get("id") or "")
    if not supply_id:
        raise MoySkladError("Найденная приемка не содержит id")

    positions = iter_collection(
        client,
        f"/entity/supply/{supply_id}/positions",
        {"expand": "assortment", "limit": 1000},
    )

    total_old_usd = Decimal("0")
    total_new_rub = Decimal("0")
    updated_count = 0

    for index, position in enumerate(positions, start=1):
        position_id = position.get("id")
        if not position_id:
            raise MoySkladError(f"Позиция #{index} не содержит id")

        old_price_usd = api_money_to_decimal(position.get("price"))
        discount_percent = to_decimal(
            position.get("discount", "0") or "0",
            f"скидки позиции {position_id}",
        )
        old_price_after_discount = discounted_price(old_price_usd, discount_percent)
        new_price_rub_before_round = old_price_after_discount * usd_to_rub_rate + delivery_price_rub
        new_price_rub = round_to_step(new_price_rub_before_round, ROUND_FINAL_PRICE_RUB_TO)
        new_price_raw = decimal_to_api_money(new_price_rub)
        quantity = to_decimal(position.get("quantity", "0"), f"количества позиции {position_id}")

        update_body = {"price": new_price_raw}
        if RESET_DISCOUNT_AFTER_UPDATE:
            update_body["discount"] = 0

        client.put(f"/entity/supply/{supply_id}/positions/{position_id}", update_body)
        updated_count += 1
        total_old_usd += old_price_after_discount * quantity
        total_new_rub += new_price_rub * quantity

    return PriceUpdateResult(
        supply_name=str(supply.get("name") or supply_number),
        supply_id=supply_id,
        positions_found=len(positions),
        positions_updated=updated_count,
        total_old_usd=total_old_usd,
        total_new_rub=total_new_rub,
        duplicate_count=duplicate_count,
    )


class TelegramClient:
    def __init__(self, token: str, ssl_context: ssl.SSLContext) -> None:
        self.base_url = f"{TELEGRAM_API_BASE_URL}/bot{token}"
        self.ssl_context = ssl_context

    def request(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}/{method}"
        request = Request(
            url,
            data=encode_body(payload or {}),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=TIMEOUT_SECONDS, context=self.ssl_context) as response:
                text = response.read().decode("utf-8")
        except HTTPError as error:
            text = error.read().decode("utf-8", errors="replace")
            raise BotError(f"Telegram HTTP {error.code}: {parse_api_error(text)}") from error
        except URLError as error:
            raise BotError(f"Telegram network error: {error.reason}") from error

        data = json.loads(text)
        if not data.get("ok"):
            raise BotError(f"Telegram API error: {data}")
        return data

    def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": 50, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        return self.request("getUpdates", payload).get("result", [])

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        self.request("sendMessage", payload)


def main_keyboard() -> dict[str, Any]:
    return {
        "keyboard": [[{"text": CONVERT_BUTTON}]],
        "resize_keyboard": True,
    }


def parse_allowed_chat_ids(raw_value: str) -> set[int]:
    ids: set[int] = set()
    for chunk in raw_value.split(","):
        text = chunk.strip()
        if not text:
            continue
        ids.add(int(text))
    return ids


def is_authorized(
    chat_id: int,
    allowed_chat_ids: set[int],
    authorized_chat_ids: set[int],
) -> bool:
    return chat_id in allowed_chat_ids or chat_id in authorized_chat_ids


def result_message(result: PriceUpdateResult) -> str:
    duplicate_note = ""
    if result.duplicate_count > 1:
        duplicate_note = (
            f"\nНайдено приемок с таким номером: {result.duplicate_count}. "
            "Обновлена самая новая по дате документа."
        )

    return (
        "Готово, цены в приемке обновлены.\n"
        f"Приемка: {result.supply_name}\n"
        f"Позиции обновлены: {result.positions_updated} из {result.positions_found}\n"
        f"Цена до: {format_money(result.total_old_usd)} $\n"
        f"Цена после: {format_money(result.total_new_rub)} ₽"
        f"{duplicate_note}"
    )


def handle_message(
    telegram: TelegramClient,
    moysklad: MoySkladClient,
    states: dict[int, DialogState],
    chat_id: int,
    text: str,
) -> None:
    normalized_text = text.strip()

    if normalized_text in {"/start", "/help"}:
        states.pop(chat_id, None)
        telegram.send_message(
            chat_id,
            "Выберите действие.",
            reply_markup=main_keyboard(),
        )
        return

    if normalized_text == "/cancel":
        states.pop(chat_id, None)
        telegram.send_message(
            chat_id,
            "Операция отменена.",
            reply_markup=main_keyboard(),
        )
        return

    if normalized_text == CONVERT_BUTTON:
        states[chat_id] = DialogState(step="supply")
        telegram.send_message(chat_id, "Введите номер приемки, например 28.")
        return

    state = states.get(chat_id)
    if state is None:
        telegram.send_message(
            chat_id,
            "Нажмите кнопку, чтобы начать.",
            reply_markup=main_keyboard(),
        )
        return

    if state.step == "supply":
        state.supply_number = normalized_text
        state.step = "rate"
        telegram.send_message(chat_id, "Введите курс доллара, например 75.5.")
        return

    if state.step == "rate":
        try:
            rate = to_decimal(normalized_text, "курса доллара")
        except BotError as error:
            telegram.send_message(chat_id, str(error))
            return
        if rate <= 0:
            telegram.send_message(chat_id, "Курс должен быть больше нуля.")
            return

        state.usd_to_rub_rate = rate
        state.step = "delivery"
        telegram.send_message(
            chat_id,
            "Введите цену доставки в рублях, которая прибавится к каждой позиции.",
        )
        return

    if state.step == "delivery":
        try:
            delivery_price = to_decimal(normalized_text, "цены доставки")
        except BotError as error:
            telegram.send_message(chat_id, str(error))
            return
        if delivery_price < 0:
            telegram.send_message(chat_id, "Цена доставки не может быть отрицательной.")
            return
        if state.supply_number is None or state.usd_to_rub_rate is None:
            states.pop(chat_id, None)
            telegram.send_message(chat_id, "Диалог сброшен. Начните заново.", reply_markup=main_keyboard())
            return

        telegram.send_message(chat_id, "Начинаю обновление цен в МойСклад...")
        try:
            result = update_supply_prices(
                moysklad,
                supply_number=state.supply_number,
                usd_to_rub_rate=state.usd_to_rub_rate,
                delivery_price_rub=delivery_price,
            )
        except (MoySkladError, BotError) as error:
            states.pop(chat_id, None)
            telegram.send_message(
                chat_id,
                f"Не получилось обновить цены: {error}",
                reply_markup=main_keyboard(),
            )
            return

        states.pop(chat_id, None)
        telegram.send_message(chat_id, result_message(result), reply_markup=main_keyboard())
        return

    states.pop(chat_id, None)
    telegram.send_message(chat_id, "Диалог сброшен. Начните заново.", reply_markup=main_keyboard())


def main() -> int:
    load_dotenv(ENV_PATH)

    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    moysklad_token = os.environ.get("MOYSKLAD_TOKEN", "").strip()
    moysklad_base_url = os.environ.get("MOYSKLAD_BASE_URL", DEFAULT_MOYSKLAD_BASE_URL).strip()
    allowed_chat_ids = parse_allowed_chat_ids(os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", ""))
    access_password = os.environ.get(
        "TELEGRAM_ACCESS_PASSWORD",
        DEFAULT_TELEGRAM_ACCESS_PASSWORD,
    ).strip()

    if not telegram_token:
        print("Fill TELEGRAM_BOT_TOKEN in .env first", file=sys.stderr)
        return 1
    if not moysklad_token:
        print("Fill MOYSKLAD_TOKEN in .env first", file=sys.stderr)
        return 1
    if not access_password:
        print("Fill TELEGRAM_ACCESS_PASSWORD in .env first", file=sys.stderr)
        return 1

    ssl_context = create_ssl_context()
    telegram = TelegramClient(telegram_token, ssl_context)
    moysklad = MoySkladClient(moysklad_token, moysklad_base_url, ssl_context)
    authorized_chat_ids = load_authorized_chat_ids(AUTHORIZED_USERS_PATH)
    states: dict[int, DialogState] = {}
    offset: int | None = None

    print("Telegram price bot started. Press Ctrl+C to stop.")
    while True:
        try:
            updates = telegram.get_updates(offset)
            for update in updates:
                offset = int(update["update_id"]) + 1
                message = update.get("message") or {}
                chat = message.get("chat") or {}
                chat_id = chat.get("id")
                text = message.get("text")
                if not isinstance(chat_id, int) or not isinstance(text, str):
                    continue

                if text.strip() == "/id":
                    telegram.send_message(chat_id, f"Ваш chat id: {chat_id}")
                    continue

                if not is_authorized(chat_id, allowed_chat_ids, authorized_chat_ids):
                    if text.strip() == access_password:
                        add_authorized_chat_id(AUTHORIZED_USERS_PATH, authorized_chat_ids, chat_id)
                        telegram.send_message(
                            chat_id,
                            "Доступ открыт.",
                            reply_markup=main_keyboard(),
                        )
                    elif text.strip() in {"/start", "/help"}:
                        telegram.send_message(chat_id, "Введите пароль для доступа.")
                    else:
                        telegram.send_message(chat_id, "Неверный пароль. Попробуйте еще раз.")
                    continue

                handle_message(telegram, moysklad, states, chat_id, text)
        except KeyboardInterrupt:
            print("Bot stopped.")
            return 0
        except Exception as error:
            print(f"Bot loop error: {error}", file=sys.stderr)
            time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
