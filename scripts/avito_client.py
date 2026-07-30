#!/usr/bin/env python3
from __future__ import annotations

import json
import ssl
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram_price_bot import AssortmentCard


AVITO_API_BASE_URL = "https://api.avito.ru"
AVITO_TOKEN_PATH = "/token"
AVITO_ITEMS_PATH = "/core/v1/items"
AVITO_ITEMS_PER_PAGE = 100
AVITO_TIMEOUT_SECONDS = 60
AVITO_MAX_RETRIES = 4
AVITO_MATCH_THRESHOLD = 0.9
AVITO_MAX_PAGES = 200
AVITO_CLOSED_STATUSES = ("old", "removed")
# Rate limits need more patience than a transient 5xx: plain 2**attempt backoff only
# buys ~15s total, which is well short of Avito's window. Bounded so a hostile or
# mistaken Retry-After can never park the single-threaded bot for a long stretch.
AVITO_RATE_LIMIT_BASE_DELAY_SECONDS = 5
AVITO_MAX_RETRY_DELAY_SECONDS = 60


class AvitoError(RuntimeError):
    pass


@dataclass
class AvitoItem:
    id: int
    title: str
    status: str
    price: int


def parse_avito_error(text: str) -> str:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text.strip() or "empty response"

    error = data.get("error")
    if isinstance(error, dict) and error.get("message"):
        fields = error.get("fields")
        if fields:
            return f"{error['message']}: {fields}"
        return str(error["message"])

    message = data.get("message")
    if message:
        return str(message)

    return text.strip() or "empty response"


class AvitoClient:
    def __init__(self, client_id: str, client_secret: str, ssl_context: ssl.SSLContext) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.ssl_context = ssl_context
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    def _fetch_token(self) -> None:
        body = urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        ).encode("utf-8")
        url = f"{AVITO_API_BASE_URL}{AVITO_TOKEN_PATH}"
        request = Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urlopen(request, timeout=AVITO_TIMEOUT_SECONDS, context=self.ssl_context) as response:
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            text = error.read().decode("utf-8", errors="replace")
            raise AvitoError(f"Avito auth HTTP {error.code}: {parse_avito_error(text)}") from error
        except URLError as error:
            raise AvitoError(f"Avito auth network error: {error.reason}") from error
        except json.JSONDecodeError as error:
            raise AvitoError(f"Avito API returned invalid JSON from {url}") from error

        access_token = data.get("access_token")
        expires_in = data.get("expires_in")
        if not access_token or not isinstance(expires_in, (int, float)):
            raise AvitoError(
                f"Avito auth response missing expected fields; got keys: {list(data.keys())}"
            )

        self._token = str(access_token)
        self._token_expires_at = time.time() + float(expires_in) - 60

    def _get_token(self) -> str:
        if self._token is None or time.time() >= self._token_expires_at:
            self._fetch_token()
        assert self._token is not None
        return self._token

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{AVITO_API_BASE_URL}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"

        for attempt in range(AVITO_MAX_RETRIES + 1):
            request = Request(
                url,
                method="GET",
                headers={
                    "Authorization": f"Bearer {self._get_token()}",
                    "Accept": "application/json",
                },
            )
            try:
                with urlopen(request, timeout=AVITO_TIMEOUT_SECONDS, context=self.ssl_context) as response:
                    text = response.read().decode("utf-8")
                    return json.loads(text) if text.strip() else {}
            except HTTPError as error:
                text = error.read().decode("utf-8", errors="replace")
                if error.code == 401 and attempt < AVITO_MAX_RETRIES:
                    self._token = None
                    continue
                if error.code == 429 or 500 <= error.code < 600:
                    if attempt < AVITO_MAX_RETRIES:
                        retry_after = error.headers.get("Retry-After")
                        if retry_after and retry_after.isdigit():
                            delay = int(retry_after)
                        elif error.code == 429:
                            delay = AVITO_RATE_LIMIT_BASE_DELAY_SECONDS * 2**attempt
                        else:
                            delay = 2**attempt
                        time.sleep(min(delay, AVITO_MAX_RETRY_DELAY_SECONDS))
                        continue
                raise AvitoError(f"Avito HTTP {error.code}: {parse_avito_error(text)}") from error
            except URLError as error:
                if attempt < AVITO_MAX_RETRIES:
                    time.sleep(2**attempt)
                    continue
                raise AvitoError(f"Avito network error: {error.reason}") from error
            except json.JSONDecodeError as error:
                raise AvitoError(f"Avito API returned invalid JSON from {url}") from error

        raise AvitoError("Avito request failed after retries")

    def iter_items(self, status: str) -> list[AvitoItem]:
        items: list[AvitoItem] = []
        page = 1
        while True:
            data = self.get(
                AVITO_ITEMS_PATH,
                {"page": page, "per_page": AVITO_ITEMS_PER_PAGE, "status": status},
            )
            resources = data.get("resources")
            if not isinstance(resources, list):
                raise AvitoError(f"Expected 'resources' list from {AVITO_ITEMS_PATH}, got: {data}")

            for row in resources:
                item_id = row.get("id")
                title = row.get("title")
                status = row.get("status")
                price = row.get("price")
                if item_id is None or not title or not status:
                    continue
                items.append(
                    AvitoItem(
                        id=int(item_id),
                        title=str(title),
                        status=str(status),
                        price=int(price) if isinstance(price, (int, float)) else 0,
                    )
                )

            if len(resources) < AVITO_ITEMS_PER_PAGE:
                return items
            page += 1
            if page > AVITO_MAX_PAGES:
                raise AvitoError(
                    f"Avito pagination exceeded {AVITO_MAX_PAGES} pages without terminating — possible API issue"
                )

    def iter_active_items(self) -> list[AvitoItem]:
        return self.iter_items("active")

    def iter_closed_items(self) -> list[AvitoItem]:
        # "old" = placement expired, "removed" = deleted by the seller. Either way the
        # ad existed and no longer runs, which is what distinguishes "was listed and is
        # now gone" from "never listed at all".
        items: list[AvitoItem] = []
        for status in AVITO_CLOSED_STATUSES:
            items.extend(self.iter_items(status))
        return items


@dataclass
class AssortmentMatch:
    card: "AssortmentCard"
    score: float


def match_to_assortment(
    title: str,
    cards: list["AssortmentCard"],
    exact_index: dict[str, "AssortmentCard"] | None = None,
) -> AssortmentMatch | None:
    from telegram_price_bot import album_match_score, normalize_text

    if exact_index is not None:
        normalized_title = normalize_text(title)
        if normalized_title:
            exact_card = exact_index.get(normalized_title)
            if exact_card is not None:
                return AssortmentMatch(card=exact_card, score=1.0)

    best_card: "AssortmentCard | None" = None
    best_score = 0.0
    for card in cards:
        score = album_match_score(title, card.name)
        if score > best_score:
            best_score = score
            best_card = card

    if best_card is None or best_score < AVITO_MATCH_THRESHOLD:
        return None
    return AssortmentMatch(card=best_card, score=best_score)


# Avito listings here carry no quantity: one ad means "this title is listed", not
# "one copy in stock" (the Autoload API that would expose a real Quantity field
# requires a paid tariff this account does not have, and the public ad pages show no
# quantity either). So the report compares presence on Avito against MoySklad stock
# instead of comparing two quantities.
STATUS_MAYBE_SOLD = "Вероятно продано, нет отгрузки"
STATUS_LISTED_NO_STOCK = "На Avito, но склад 0"
STATUS_NOT_LISTED = "Не выставлено на Avito"
STATUS_NO_MATCH = "Нет совпадения в МойСклад"
STATUS_UNKNOWN_QUANTITY = "Остаток неизвестен"

AVITO_STATE_ACTIVE = "активно"
AVITO_STATE_CLOSED = "закрыто"
AVITO_STATE_NONE = "нет объявления"

# Order the actionable sheet puts statuses in — the sold-but-not-shipped cases are
# the ones worth acting on first.
_ACTION_STATUS_ORDER = (
    STATUS_MAYBE_SOLD,
    STATUS_LISTED_NO_STOCK,
    STATUS_UNKNOWN_QUANTITY,
)


@dataclass
class ListingReportRow:
    name: str
    moysklad_quantity: Decimal | None
    avito_state: str
    status: str
    match_score: float
    avito_ad_count: int = 1


@dataclass
class ListingReport:
    action_required: list[ListingReportRow]
    not_listed: list[ListingReportRow]
    unmatched: list[ListingReportRow]

    def total_rows(self) -> int:
        return len(self.action_required) + len(self.not_listed) + len(self.unmatched)


def build_listing_report(
    avito_client: AvitoClient,
    cards: list["AssortmentCard"],
) -> ListingReport:
    from telegram_price_bot import normalize_text

    active_items = avito_client.iter_active_items()
    closed_items = avito_client.iter_closed_items()

    exact_index: dict[str, "AssortmentCard"] = {}
    for card in cards:
        key = normalize_text(card.name)
        if key and key not in exact_index:
            exact_index[key] = card

    active_hrefs: set[str] = set()
    score_by_href: dict[str, float] = {}
    unmatched_counts: dict[str, int] = {}

    for item in active_items:
        match = match_to_assortment(item.title, cards, exact_index)
        if match is None:
            unmatched_counts[item.title] = unmatched_counts.get(item.title, 0) + 1
            continue
        active_hrefs.add(match.card.href)
        score_by_href[match.card.href] = max(
            score_by_href.get(match.card.href, 0.0), match.score
        )

    # Closed ads are matched on exact normalized title only, not the full fuzzy scan:
    # this is a cheap refinement over thousands of extra ads, and a miss degrades
    # safely (the card lands in "not listed" rather than producing a false alert).
    closed_titles: set[str] = set()
    for item in closed_items:
        key = normalize_text(item.title)
        if key:
            closed_titles.add(key)

    action_required: list[ListingReportRow] = []
    not_listed: list[ListingReportRow] = []

    for card in cards:
        quantity = card.available_quantity
        is_active = card.href in active_hrefs
        is_closed = not is_active and normalize_text(card.name) in closed_titles

        if is_active:
            state = AVITO_STATE_ACTIVE
        elif is_closed:
            state = AVITO_STATE_CLOSED
        else:
            state = AVITO_STATE_NONE

        row = ListingReportRow(
            name=card.name,
            moysklad_quantity=quantity,
            avito_state=state,
            status="",
            match_score=score_by_href.get(card.href, 0.0),
        )

        if quantity is None:
            row.status = STATUS_UNKNOWN_QUANTITY
            action_required.append(row)
        elif is_active and quantity == 0:
            row.status = STATUS_LISTED_NO_STOCK
            action_required.append(row)
        elif is_closed and quantity >= 1:
            row.status = STATUS_MAYBE_SOLD
            action_required.append(row)
        elif state == AVITO_STATE_NONE and quantity >= 1:
            row.status = STATUS_NOT_LISTED
            not_listed.append(row)
        # Remaining combinations are consistent (listed and in stock, or absent and
        # out of stock) and are left out of the report entirely.

    unmatched = [
        ListingReportRow(
            name=title,
            moysklad_quantity=None,
            avito_state=AVITO_STATE_ACTIVE,
            status=STATUS_NO_MATCH,
            match_score=0.0,
            avito_ad_count=count,
        )
        for title, count in unmatched_counts.items()
    ]

    action_required.sort(
        key=lambda row: (_ACTION_STATUS_ORDER.index(row.status), row.name.lower())
    )
    not_listed.sort(key=lambda row: row.name.lower())
    unmatched.sort(key=lambda row: row.name.lower())

    return ListingReport(
        action_required=action_required,
        not_listed=not_listed,
        unmatched=unmatched,
    )
