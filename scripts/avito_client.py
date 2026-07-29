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
                        delay = int(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                        time.sleep(delay)
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

    def iter_active_items(self) -> list[AvitoItem]:
        items: list[AvitoItem] = []
        page = 1
        while True:
            data = self.get(
                AVITO_ITEMS_PATH,
                {"page": page, "per_page": AVITO_ITEMS_PER_PAGE, "status": "active"},
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


STATUS_OK = "OK"
STATUS_MISMATCH = "MISMATCH"
STATUS_NO_MATCH = "NO_CONFIDENT_MATCH"


@dataclass
class QuantityDiffRow:
    avito_title: str
    matched_name: str | None
    match_score: float
    avito_active_count: int
    moysklad_quantity: Decimal | None
    status: str


def build_quantity_diff_rows(
    avito_client: AvitoClient,
    cards: list["AssortmentCard"],
) -> list[QuantityDiffRow]:
    from telegram_price_bot import normalize_text

    avito_items = avito_client.iter_active_items()

    exact_index: dict[str, "AssortmentCard"] = {}
    for card in cards:
        key = normalize_text(card.name)
        if key and key not in exact_index:
            exact_index[key] = card

    avito_count_by_href: dict[str, int] = {}
    match_score_by_href: dict[str, float] = {}
    unmatched_counts: dict[str, int] = {}

    for item in avito_items:
        match = match_to_assortment(item.title, cards, exact_index)
        if match is None:
            unmatched_counts[item.title] = unmatched_counts.get(item.title, 0) + 1
            continue

        href = match.card.href
        avito_count_by_href[href] = avito_count_by_href.get(href, 0) + 1
        match_score_by_href[href] = max(match_score_by_href.get(href, 0.0), match.score)

    rows: list[QuantityDiffRow] = []
    for card in cards:
        avito_count = avito_count_by_href.get(card.href, 0)
        moysklad_quantity = card.available_quantity
        if avito_count == 0 and (moysklad_quantity is None or moysklad_quantity == 0):
            continue

        quantities_match = moysklad_quantity is not None and moysklad_quantity == avito_count
        rows.append(
            QuantityDiffRow(
                avito_title=card.name,
                matched_name=card.name,
                match_score=match_score_by_href.get(card.href, 0.0),
                avito_active_count=avito_count,
                moysklad_quantity=moysklad_quantity,
                status=STATUS_OK if quantities_match else STATUS_MISMATCH,
            )
        )

    for title, count in unmatched_counts.items():
        rows.append(
            QuantityDiffRow(
                avito_title=title,
                matched_name=None,
                match_score=0.0,
                avito_active_count=count,
                moysklad_quantity=None,
                status=STATUS_NO_MATCH,
            )
        )

    rows.sort(
        key=lambda row: (
            row.status == STATUS_OK,
            row.avito_title.lower(),
        )
    )
    return rows
