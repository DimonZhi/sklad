#!/usr/bin/env python3
from __future__ import annotations

import gzip
import json
import re
import os
import ssl
import sys
import time
from calendar import monthrange
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from difflib import SequenceMatcher
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
SEARCH_MONTHS_BACK = 6
MAX_SEARCH_GROUPS = 20
MAX_SEARCH_CANDIDATES = 30
MAX_SEARCH_WORKERS = 6
ASSORTMENT_CACHE_SECONDS = 8 * 60 * 60
CONVERT_BUTTON = "Конвертировать цены в приемке"
SEARCH_BUTTON = "Поиск по складу"
DEFAULT_TELEGRAM_ACCESS_PASSWORD = "1821"
DEFAULT_TELEGRAM_USER_ACCESS_PASSWORD = "123"
ROLE_ADMIN = "admin"
ROLE_USER = "user"
VINYL_NOT_FOUND_MESSAGE = "Винил не найден."
MATCH_THRESHOLD = 0.72
TELEGRAM_MESSAGE_LIMIT = 3900
TOKEN_RE = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)


class BotError(RuntimeError):
    pass


class MoySkladError(RuntimeError):
    pass


class VinylNotFound(BotError):
    pass


@dataclass
class DialogState:
    step: str
    supply_number: str | None = None
    usd_to_rub_rate: Decimal | None = None


@dataclass
class AuthorizedUsers:
    admins: set[int]
    users: set[int]


@dataclass
class PriceUpdateResult:
    supply_name: str
    supply_id: str
    positions_found: int
    positions_updated: int
    total_old_usd: Decimal
    total_new_rub: Decimal
    duplicate_count: int


@dataclass
class PurchaseOrderMatch:
    album_name: str
    order_name: str
    moment: str
    agent_name: str
    quantity: Decimal
    available_quantity: Decimal | None
    score: float


@dataclass
class AssortmentCandidate:
    name: str
    href: str
    available_quantity: Decimal | None
    score: float


@dataclass
class AssortmentCard:
    name: str
    href: str
    available_quantity: Decimal | None


@dataclass
class AssortmentCache:
    loaded_at: float
    cards: list[AssortmentCard]


_assortment_cache: AssortmentCache | None = None


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


def parse_chat_id_set(raw_ids: Any) -> set[int]:
    chat_ids: set[int] = set()
    if not isinstance(raw_ids, list):
        return chat_ids

    for raw_id in raw_ids:
        try:
            chat_ids.add(int(raw_id))
        except (TypeError, ValueError):
            continue
    return chat_ids


def load_authorized_users(path: Path) -> AuthorizedUsers:
    if not path.exists():
        return AuthorizedUsers(admins=set(), users=set())

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise BotError(f"Cannot read authorized users file: {path}") from error

    if isinstance(data, dict):
        admins = parse_chat_id_set(data.get("admins", []))
        admins |= parse_chat_id_set(data.get("chat_ids", []))
        users = parse_chat_id_set(data.get("users", []))
    else:
        admins = parse_chat_id_set(data)
        users = set()

    return AuthorizedUsers(admins=admins, users=users - admins)


def load_authorized_chat_ids(path: Path) -> set[int]:
    return load_authorized_users(path).admins


def save_authorized_users(path: Path, authorized_users: AuthorizedUsers) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    admins = set(authorized_users.admins)
    users = set(authorized_users.users) - admins
    payload = {
        "admins": sorted(admins),
        "users": sorted(users),
        "chat_ids": sorted(admins),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def add_authorized_chat_id(
    path: Path,
    authorized_users: AuthorizedUsers,
    chat_id: int,
    role: str,
) -> None:
    if role == ROLE_ADMIN:
        authorized_users.admins.add(chat_id)
        authorized_users.users.discard(chat_id)
    elif role == ROLE_USER:
        if chat_id not in authorized_users.admins:
            authorized_users.users.add(chat_id)
    else:
        raise BotError(f"Unknown user role: {role}")

    save_authorized_users(path, authorized_users)


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


def subtract_months(moment: datetime, months: int) -> datetime:
    year = moment.year
    month = moment.month - months
    while month <= 0:
        month += 12
        year -= 1

    day = min(moment.day, monthrange(year, month)[1])
    return moment.replace(
        year=year,
        month=month,
        day=day,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )


def format_moysklad_datetime(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def format_order_moment(value: Any) -> str:
    text = str(value or "").strip()
    if "." in text:
        text = text.split(".", 1)[0]
    return text or "без даты"


def format_quantity(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def normalize_text(value: str) -> str:
    tokens = TOKEN_RE.findall(value.lower().replace("ё", "е"))
    return " ".join(tokens)


def text_tokens(value: str) -> list[str]:
    return TOKEN_RE.findall(value.lower().replace("ё", "е"))


def token_similarity(left: str, right: str) -> float:
    if left == right:
        return 1.0
    if len(left) >= 4 and left in right and len(right) / len(left) <= 2:
        return 0.92
    if len(left) >= 4 and (left[0] != right[0] or left[-1] != right[-1]):
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def album_match_score(query: str, album_name: str) -> float:
    query_tokens = text_tokens(query)
    album_tokens = text_tokens(album_name)
    if not query_tokens or not album_tokens:
        return 0.0

    best_scores: list[float] = []
    for query_token in query_tokens:
        best_scores.append(
            max(token_similarity(query_token, album_token) for album_token in album_tokens)
        )

    if min(best_scores) < MATCH_THRESHOLD:
        return 0.0

    token_score = sum(best_scores) / len(best_scores)
    sequence_score = SequenceMatcher(None, normalize_text(query), normalize_text(album_name)).ratio()
    exact_bonus = 0.08 if all(score >= 0.98 for score in best_scores) else 0.0
    return min(1.0, token_score * 0.86 + sequence_score * 0.14 + exact_bonus)


def resolve_agent_name(
    client: MoySkladClient,
    order: dict[str, Any],
    agent_cache: dict[str, str],
) -> str:
    agent = order.get("agent")
    if not isinstance(agent, dict):
        return "контрагент не указан"

    name = agent.get("name")
    if name:
        return str(name)

    meta = agent.get("meta")
    if not isinstance(meta, dict):
        return "контрагент не указан"

    href = str(meta.get("href") or "")
    if not href:
        return "контрагент не указан"
    if href not in agent_cache:
        data = client.get(href)
        agent_cache[href] = str(data.get("name") or "контрагент не указан")
    return agent_cache[href]


def to_optional_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def available_quantity_from_assortment(assortment: dict[str, Any]) -> Decimal | None:
    quantity = to_optional_decimal(assortment.get("quantity"))
    if quantity is not None:
        return quantity

    stock = to_optional_decimal(assortment.get("stock"))
    reserve = to_optional_decimal(assortment.get("reserve"))
    if stock is not None and reserve is not None:
        return stock - reserve

    return stock


def available_quantity_from_stock_report(client: MoySkladClient, assortment_href: str) -> Decimal | None:
    try:
        data = client.get(
            "/report/stock/all",
            {"filter": f"assortment={assortment_href}", "limit": 100},
        )
    except MoySkladError:
        return None

    rows = data.get("rows")
    if not isinstance(rows, list):
        return None

    total: Decimal | None = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        quantity = available_quantity_from_assortment(row)
        if quantity is None:
            continue
        total = quantity if total is None else total + quantity
    return total


def resolve_assortment_available_quantity(
    client: MoySkladClient,
    assortment: dict[str, Any],
    availability_cache: dict[str, Decimal | None],
) -> Decimal | None:
    available_quantity = available_quantity_from_assortment(assortment)
    if available_quantity is not None:
        return available_quantity

    meta = assortment.get("meta")
    if not isinstance(meta, dict):
        return None

    href = str(meta.get("href") or "")
    if not href:
        return None
    if href not in availability_cache:
        available_quantity = available_quantity_from_assortment(client.get(href))
        if available_quantity is None:
            available_quantity = available_quantity_from_stock_report(client, href)
        availability_cache[href] = available_quantity
    return availability_cache[href]


def assortment_href(assortment: dict[str, Any]) -> str:
    meta = assortment.get("meta")
    if isinstance(meta, dict):
        href = str(meta.get("href") or "")
        if href:
            return href
    return str(assortment.get("href") or "")


def load_assortment_cards(client: MoySkladClient) -> list[AssortmentCard]:
    global _assortment_cache

    now = time.time()
    if (
        _assortment_cache is not None
        and now - _assortment_cache.loaded_at < ASSORTMENT_CACHE_SECONDS
    ):
        return _assortment_cache.cards

    rows = iter_collection(client, "/entity/assortment", {"limit": 1000})
    cards: list[AssortmentCard] = []
    for row in rows:
        name = str(row.get("name") or "").strip()
        href = assortment_href(row)
        if not name or not href:
            continue
        cards.append(
            AssortmentCard(
                name=name,
                href=href,
                available_quantity=available_quantity_from_assortment(row),
            )
        )

    _assortment_cache = AssortmentCache(loaded_at=now, cards=cards)
    return cards


def search_assortment_cards(client: MoySkladClient, query: str) -> list[AssortmentCandidate]:
    availability_cache: dict[str, Decimal | None] = {}

    candidates: list[AssortmentCandidate] = []
    for card in load_assortment_cards(client):
        score = album_match_score(query, card.name)
        if score <= 0:
            continue

        available_quantity = card.available_quantity
        if available_quantity is None and card.href not in availability_cache:
            product = client.get(card.href)
            available_quantity = available_quantity_from_assortment(product)
            if available_quantity is None:
                available_quantity = available_quantity_from_stock_report(client, card.href)
            availability_cache[card.href] = available_quantity

        candidates.append(
            AssortmentCandidate(
                name=card.name,
                href=card.href,
                available_quantity=(
                    available_quantity
                    if available_quantity is not None
                    else availability_cache.get(card.href)
                ),
                score=score,
            )
        )

    return sorted(
        candidates,
        key=lambda candidate: (candidate.score, candidate.name.lower()),
        reverse=True,
    )


def position_assortment(position: dict[str, Any]) -> dict[str, Any]:
    assortment = position.get("assortment")
    if not isinstance(assortment, dict):
        return {}
    return assortment


def purchase_order_positions(
    client: MoySkladClient,
    order: dict[str, Any],
    order_id: str,
) -> list[dict[str, Any]]:
    positions = order.get("positions")
    if isinstance(positions, dict):
        rows = positions.get("rows")
        meta = positions.get("meta") if isinstance(positions.get("meta"), dict) else {}
        size = meta.get("size")
        if isinstance(rows, list) and (not isinstance(size, int) or len(rows) >= size):
            return rows

    return iter_collection(
        client,
        f"/entity/purchaseorder/{order_id}/positions",
        {"expand": "assortment", "limit": 1000},
    )


def find_purchase_orders_for_candidate(
    client: MoySkladClient,
    base_filter: str,
    candidate: AssortmentCandidate,
) -> tuple[AssortmentCandidate, list[dict[str, Any]]]:
    orders = iter_collection(
        client,
        "/entity/purchaseorder",
        {
            "filter": f"{base_filter};assortment={candidate.href}",
            "expand": "agent,positions.assortment",
            "limit": 100,
            "order": "moment,desc",
        },
    )
    return candidate, orders


def search_purchase_orders(client: MoySkladClient, query: str) -> list[PurchaseOrderMatch]:
    query = query.strip()
    if not query:
        raise BotError("Введите название альбома для поиска.")

    candidates = search_assortment_cards(client, query)
    if not candidates:
        raise VinylNotFound(VINYL_NOT_FOUND_MESSAGE)

    since = subtract_months(datetime.now(), SEARCH_MONTHS_BACK)
    base_filter = f"moment>={format_moysklad_datetime(since)};applicable=false"
    agent_cache: dict[str, str] = {}
    matches_by_key: dict[tuple[str, str, str, str], PurchaseOrderMatch] = {}
    limited_candidates = candidates[:MAX_SEARCH_CANDIDATES]

    workers = min(MAX_SEARCH_WORKERS, len(limited_candidates))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(find_purchase_orders_for_candidate, client, base_filter, candidate)
            for candidate in limited_candidates
        ]
        candidate_orders = [future.result() for future in as_completed(futures)]

    for candidate, orders in candidate_orders:
        for order in orders:
            order_id = str(order.get("id") or "")
            if not order_id:
                continue

            order_name = str(order.get("name") or "без номера")
            moment = format_order_moment(order.get("moment"))
            agent_name = resolve_agent_name(client, order, agent_cache)

            for index, position in enumerate(
                purchase_order_positions(client, order, order_id),
                start=1,
            ):
                assortment = position_assortment(position)
                if assortment_href(assortment) != candidate.href:
                    continue

                quantity = to_decimal(
                    position.get("quantity", "0"),
                    f"количества позиции {index} в заказе {order_name}",
                )
                key = (candidate.name, order_name, moment, agent_name)
                existing = matches_by_key.get(key)
                if existing:
                    existing.quantity += quantity
                    if existing.available_quantity is None:
                        existing.available_quantity = candidate.available_quantity
                    existing.score = max(existing.score, candidate.score)
                else:
                    matches_by_key[key] = PurchaseOrderMatch(
                        album_name=candidate.name,
                        order_name=order_name,
                        moment=moment,
                        agent_name=agent_name,
                        quantity=quantity,
                        available_quantity=candidate.available_quantity,
                        score=candidate.score,
                    )

    return sorted(
        matches_by_key.values(),
        key=lambda match: (match.score, match.moment, match.album_name),
        reverse=True,
    )


def format_purchase_order_matches(query: str, matches: list[PurchaseOrderMatch]) -> str:
    if not matches:
        return (
            "Не нашел подходящих непроведенных заказов поставщикам "
            f"за последние {SEARCH_MONTHS_BACK} месяцев по запросу: {query}"
        )

    groups: dict[str, list[PurchaseOrderMatch]] = {}
    for match in matches:
        groups.setdefault(match.album_name, []).append(match)

    sorted_groups = sorted(
        groups.items(),
        key=lambda item: (max(match.score for match in item[1]), item[0].lower()),
        reverse=True,
    )

    blocks: list[str] = []
    for album_name, album_matches in sorted_groups[:MAX_SEARCH_GROUPS]:
        available_quantity = next(
            (
                match.available_quantity
                for match in album_matches
                if match.available_quantity is not None
            ),
            None,
        )
        available_text = (
            format_quantity(available_quantity)
            if available_quantity is not None
            else "неизвестно"
        )
        lines = [f"{album_name}, на склада - {available_text}, заказаны:"]
        for match in sorted(album_matches, key=lambda item: item.moment, reverse=True):
            lines.append(
                f"{match.order_name}, {match.moment}, "
                f"{match.agent_name}, {format_quantity(match.quantity)}"
            )
        blocks.append("\n".join(lines))

    if len(sorted_groups) > MAX_SEARCH_GROUPS:
        blocks.append(f"Показал первые {MAX_SEARCH_GROUPS} карточек из {len(sorted_groups)}.")

    return "\n\n".join(blocks)


def split_telegram_text(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        separator = "\n\n" if current else ""
        if len(current) + len(separator) + len(block) <= limit:
            current = f"{current}{separator}{block}"
            continue

        if current:
            chunks.append(current)
            current = ""

        if len(block) <= limit:
            current = block
            continue

        for line in block.splitlines():
            line_separator = "\n" if current else ""
            if len(current) + len(line_separator) + len(line) <= limit:
                current = f"{current}{line_separator}{line}"
            else:
                if current:
                    chunks.append(current)
                while len(line) > limit:
                    chunks.append(line[:limit])
                    line = line[limit:]
                current = line

    if current:
        chunks.append(current)
    return chunks


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

    def send_long_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        chunks = split_telegram_text(text)
        for index, chunk in enumerate(chunks):
            self.send_message(
                chat_id,
                chunk,
                reply_markup=reply_markup if index == len(chunks) - 1 else None,
            )


def main_keyboard(role: str) -> dict[str, Any]:
    if role == ROLE_ADMIN:
        keyboard = [[{"text": SEARCH_BUTTON}], [{"text": CONVERT_BUTTON}]]
    else:
        keyboard = [[{"text": SEARCH_BUTTON}]]

    return {
        "keyboard": keyboard,
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


def get_authorized_role(
    chat_id: int,
    allowed_chat_ids: set[int],
    authorized_users: AuthorizedUsers,
) -> str | None:
    if chat_id in allowed_chat_ids or chat_id in authorized_users.admins:
        return ROLE_ADMIN
    if chat_id in authorized_users.users:
        return ROLE_USER
    return None


def is_authorized(
    chat_id: int,
    allowed_chat_ids: set[int],
    authorized_users: AuthorizedUsers,
) -> bool:
    return get_authorized_role(chat_id, allowed_chat_ids, authorized_users) is not None


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
    role: str,
) -> None:
    normalized_text = text.strip()

    if normalized_text in {"/start", "/help"}:
        states.pop(chat_id, None)
        telegram.send_message(
            chat_id,
            "Выберите действие.",
            reply_markup=main_keyboard(role),
        )
        return

    if normalized_text == "/cancel":
        states.pop(chat_id, None)
        telegram.send_message(
            chat_id,
            "Операция отменена.",
            reply_markup=main_keyboard(role),
        )
        return

    if normalized_text == SEARCH_BUTTON:
        states[chat_id] = DialogState(step="search")
        telegram.send_message(chat_id, "Введите название альбома для поиска.")
        return

    if normalized_text == CONVERT_BUTTON:
        if role != ROLE_ADMIN:
            states.pop(chat_id, None)
            telegram.send_message(
                chat_id,
                "Эта команда доступна только админу.",
                reply_markup=main_keyboard(role),
            )
            return

        states[chat_id] = DialogState(step="supply")
        telegram.send_message(chat_id, "Введите номер приемки, например 28.")
        return

    state = states.get(chat_id)
    if state is None:
        telegram.send_message(
            chat_id,
            "Нажмите кнопку, чтобы начать.",
            reply_markup=main_keyboard(role),
        )
        return

    if state.step == "search":
        telegram.send_message(chat_id, "Ищу в заказах поставщикам...")
        try:
            matches = search_purchase_orders(moysklad, normalized_text)
            message = format_purchase_order_matches(normalized_text, matches)
        except VinylNotFound as error:
            states.pop(chat_id, None)
            telegram.send_message(
                chat_id,
                str(error),
                reply_markup=main_keyboard(role),
            )
            return
        except (MoySkladError, BotError) as error:
            states.pop(chat_id, None)
            telegram.send_message(
                chat_id,
                f"Не получилось выполнить поиск: {error}",
                reply_markup=main_keyboard(role),
            )
            return

        states.pop(chat_id, None)
        telegram.send_long_message(chat_id, message, reply_markup=main_keyboard(role))
        return

    if state.step == "supply":
        if role != ROLE_ADMIN:
            states.pop(chat_id, None)
            telegram.send_message(
                chat_id,
                "Эта команда доступна только админу.",
                reply_markup=main_keyboard(role),
            )
            return

        state.supply_number = normalized_text
        state.step = "rate"
        telegram.send_message(chat_id, "Введите курс доллара, например 75.5.")
        return

    if state.step == "rate":
        if role != ROLE_ADMIN:
            states.pop(chat_id, None)
            telegram.send_message(
                chat_id,
                "Эта команда доступна только админу.",
                reply_markup=main_keyboard(role),
            )
            return

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
        if role != ROLE_ADMIN:
            states.pop(chat_id, None)
            telegram.send_message(
                chat_id,
                "Эта команда доступна только админу.",
                reply_markup=main_keyboard(role),
            )
            return

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
            telegram.send_message(
                chat_id,
                "Диалог сброшен. Начните заново.",
                reply_markup=main_keyboard(role),
            )
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
                reply_markup=main_keyboard(role),
            )
            return

        states.pop(chat_id, None)
        telegram.send_message(
            chat_id,
            result_message(result),
            reply_markup=main_keyboard(role),
        )
        return

    states.pop(chat_id, None)
    telegram.send_message(
        chat_id,
        "Диалог сброшен. Начните заново.",
        reply_markup=main_keyboard(role),
    )


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
    user_access_password = os.environ.get(
        "TELEGRAM_USER_ACCESS_PASSWORD",
        DEFAULT_TELEGRAM_USER_ACCESS_PASSWORD,
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
    if not user_access_password:
        print("Fill TELEGRAM_USER_ACCESS_PASSWORD in .env first", file=sys.stderr)
        return 1

    ssl_context = create_ssl_context()
    telegram = TelegramClient(telegram_token, ssl_context)
    moysklad = MoySkladClient(moysklad_token, moysklad_base_url, ssl_context)
    authorized_users = load_authorized_users(AUTHORIZED_USERS_PATH)
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

                role = get_authorized_role(chat_id, allowed_chat_ids, authorized_users)
                if role is None:
                    if text.strip() == access_password:
                        add_authorized_chat_id(
                            AUTHORIZED_USERS_PATH,
                            authorized_users,
                            chat_id,
                            ROLE_ADMIN,
                        )
                        telegram.send_message(
                            chat_id,
                            "Доступ открыт: админ.",
                            reply_markup=main_keyboard(ROLE_ADMIN),
                        )
                    elif text.strip() == user_access_password:
                        add_authorized_chat_id(
                            AUTHORIZED_USERS_PATH,
                            authorized_users,
                            chat_id,
                            ROLE_USER,
                        )
                        telegram.send_message(
                            chat_id,
                            "Доступ открыт.",
                            reply_markup=main_keyboard(ROLE_USER),
                        )
                    elif text.strip() in {"/start", "/help"}:
                        telegram.send_message(chat_id, "Введите пароль для доступа.")
                    else:
                        telegram.send_message(chat_id, "Неверный пароль. Попробуйте еще раз.")
                    continue

                handle_message(telegram, moysklad, states, chat_id, text, role)
        except KeyboardInterrupt:
            print("Bot stopped.")
            return 0
        except Exception as error:
            print(f"Bot loop error: {error}", file=sys.stderr)
            time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
