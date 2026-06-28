#!/usr/bin/env python3
from __future__ import annotations

import csv
import gzip
import json
import os
import ssl
import sys
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


# Put the MoySklad supply document number here.
SUPPLY_NUMBER = "00028"

# Use "name" for the document number, or "code" if you need to search by code.
SUPPLY_SEARCH_FIELD = "name"

# Formula: new rub price = old dollar price * USD_TO_RUB_RATE + DELIVERY_PRICE_RUB
USD_TO_RUB_RATE = 75.5
DELIVERY_PRICE_RUB = 667
ROUND_FINAL_PRICE_RUB_TO = 1

# MoySklad stores `price` before discount and `discount` separately.
# With True, the formula uses old price after discount:
# old_price_after_discount = old_price * (1 - discount / 100)
USE_DISCOUNTED_PRICE = True

# With True, real updates set discount to 0 because the new price already includes it.
RESET_DISCOUNT_AFTER_UPDATE = True

# First run with False to check the CSV preview. Set to True to write prices to MoySklad.
APPLY_CHANGES = True

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"
OUTPUT_DIR = PROJECT_ROOT / "exports"

DEFAULT_BASE_URL = "https://api.moysklad.ru/api/remap/1.2"
TIMEOUT_SECONDS = 60
MAX_RETRIES = 4


class MoySkladError(RuntimeError):
    pass


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

    return text.strip() or "empty response"


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


def find_supply(client: MoySkladClient, supply_number: str) -> dict[str, Any]:
    rows = iter_collection(
        client,
        "/entity/supply",
        {
            "filter": f"{SUPPLY_SEARCH_FIELD}={supply_number}",
            "limit": 100,
            "order": "moment,desc",
        },
    )

    if not rows:
        raise MoySkladError(
            f"Supply with {SUPPLY_SEARCH_FIELD}={supply_number!r} was not found"
        )

    exact_rows = [row for row in rows if str(row.get(SUPPLY_SEARCH_FIELD, "")) == supply_number]
    if exact_rows:
        rows = exact_rows

    if len(rows) > 1:
        print(
            f"Found {len(rows)} supplies with {SUPPLY_SEARCH_FIELD}={supply_number!r}; "
            "using the newest by moment.",
            file=sys.stderr,
        )

    return rows[0]


def api_money_to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value)) / Decimal("100")
    except (InvalidOperation, ValueError, TypeError) as error:
        raise MoySkladError(f"Invalid price value from API: {value!r}") from error


def to_decimal(value: Any, name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise MoySkladError(f"Invalid {name}: {value!r}") from error


def decimal_to_api_money(value: Decimal) -> int:
    return int((value * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise MoySkladError(f"ROUND_FINAL_PRICE_RUB_TO must be greater than zero: {step}")
    return (value / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step


def discounted_price(price: Decimal, discount_percent: Decimal) -> Decimal:
    multiplier = (Decimal("100") - discount_percent) / Decimal("100")
    return price * multiplier


def get_assortment_name(
    client: MoySkladClient,
    assortment: dict[str, Any],
    cache: dict[str, dict[str, Any]],
) -> str:
    name = assortment.get("name")
    if name:
        return str(name)

    href = assortment.get("meta", {}).get("href")
    if not href:
        return ""

    if href not in cache:
        cache[href] = client.get(href)

    return str(cache[href].get("name") or "")


def update_supply_prices(client: MoySkladClient, supply_number: str) -> Path:
    supply = find_supply(client, supply_number)
    supply_id = supply.get("id")
    if not supply_id:
        raise MoySkladError("Found supply does not contain id")

    positions = iter_collection(
        client,
        f"/entity/supply/{supply_id}/positions",
        {"expand": "assortment", "limit": 1000},
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    safe_number = "".join(
        char if char.isalnum() or char in ("-", "_") else "_" for char in supply_number
    )
    report_path = OUTPUT_DIR / f"supply_{safe_number}_rub_price_update.csv"
    assortment_cache: dict[str, dict[str, Any]] = {}
    usd_to_rub_rate = to_decimal(USD_TO_RUB_RATE, "USD_TO_RUB_RATE")
    delivery_price_rub = to_decimal(DELIVERY_PRICE_RUB, "DELIVERY_PRICE_RUB")
    round_final_price_rub_to = to_decimal(
        ROUND_FINAL_PRICE_RUB_TO,
        "ROUND_FINAL_PRICE_RUB_TO",
    )

    updated_count = 0
    total_old = Decimal("0")
    total_new = Decimal("0")

    with report_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "status",
                "product_name",
                "quantity",
                "old_price_usd_before_discount",
                "discount_percent",
                "old_price_usd_after_discount",
                "new_price_rub",
                "old_price_raw",
                "new_price_raw",
                "new_discount",
                "position_id",
            ],
        )
        writer.writeheader()

        for index, position in enumerate(positions, start=1):
            position_id = position.get("id")
            if not position_id:
                raise MoySkladError(f"Position #{index} does not contain id")

            old_price_usd = api_money_to_decimal(position.get("price"))
            discount_percent = to_decimal(
                position.get("discount", "0") or "0",
                f"discount for position {position_id}",
            )
            old_price_usd_after_discount = discounted_price(old_price_usd, discount_percent)
            price_for_formula = (
                old_price_usd_after_discount if USE_DISCOUNTED_PRICE else old_price_usd
            )
            new_price_rub_before_round = price_for_formula * usd_to_rub_rate + delivery_price_rub
            new_price_rub = round_to_step(new_price_rub_before_round, round_final_price_rub_to)
            new_price_raw = decimal_to_api_money(new_price_rub)
            quantity = to_decimal(position.get("quantity", "0"), f"quantity for position {position_id}")
            total_old += price_for_formula * quantity
            total_new += new_price_rub * quantity

            status = "preview"
            update_body = {"price": new_price_raw}
            new_discount: str | Decimal = discount_percent
            if USE_DISCOUNTED_PRICE and RESET_DISCOUNT_AFTER_UPDATE:
                update_body["discount"] = 0
                new_discount = Decimal("0")

            if APPLY_CHANGES:
                client.put(
                    f"/entity/supply/{supply_id}/positions/{position_id}",
                    update_body,
                )
                status = "updated"
                updated_count += 1

            assortment = position.get("assortment") or {}
            writer.writerow(
                {
                    "status": status,
                    "product_name": get_assortment_name(client, assortment, assortment_cache),
                    "quantity": position.get("quantity", ""),
                    "old_price_usd_before_discount": format_money(old_price_usd),
                    "discount_percent": format_money(discount_percent),
                    "old_price_usd_after_discount": format_money(old_price_usd_after_discount),
                    "new_price_rub": format_money(new_price_rub),
                    "old_price_raw": position.get("price", ""),
                    "new_price_raw": new_price_raw,
                    "new_discount": format_money(to_decimal(new_discount, "new_discount")),
                    "position_id": position_id,
                }
            )

    mode = "UPDATED" if APPLY_CHANGES else "PREVIEW ONLY"
    print(f"Mode: {mode}")
    print(f"Supply: {supply.get('name', supply_number)}")
    print(f"Positions found: {len(positions)}")
    if APPLY_CHANGES:
        print(f"Positions updated: {updated_count}")
    source = "old price after discount" if USE_DISCOUNTED_PRICE else "old price before discount"
    print(f"Formula source: {source}")
    print(f"Formula: source_price * {usd_to_rub_rate} + {delivery_price_rub}")
    print(f"Final RUB price is rounded to: {round_final_price_rub_to}")
    if USE_DISCOUNTED_PRICE and RESET_DISCOUNT_AFTER_UPDATE:
        print("Real update will set discount to 0 because the new price already includes it.")
    print(f"Old total used for conversion: {format_money(total_old)}")
    print(f"New total in RUB: {format_money(total_new)}")
    print(f"Report: {report_path}")
    return report_path


def main() -> int:
    load_dotenv(ENV_PATH)

    token = os.environ.get("MOYSKLAD_TOKEN", "").strip()
    base_url = os.environ.get("MOYSKLAD_BASE_URL", DEFAULT_BASE_URL).strip()

    if not token:
        print("Fill MOYSKLAD_TOKEN in .env first", file=sys.stderr)
        return 1

    client = MoySkladClient(
        token=token,
        base_url=base_url,
        ssl_context=create_ssl_context(),
    )

    try:
        update_supply_prices(client, SUPPLY_NUMBER)
    except MoySkladError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
