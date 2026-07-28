# Avito Quantity Check (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `"Проверить количество"` admin button to the Telegram bot that compares MoySklad stock quantity against the count of active Avito listings per title (matched by fuzzy title match) and sends the result as an Excel file. Read-only against both Avito and MoySklad — no writes to either system.

**Architecture:** A new standalone module `scripts/avito_client.py` holds all Avito API logic (OAuth2 client_credentials auth, retrying GET, pagination, title matching, diff computation) with zero Telegram/systemd coupling, mirroring the existing `MoySkladClient` pattern in `scripts/telegram_price_bot.py`. `telegram_price_bot.py` imports it for the new button, the same way `scripts/debug_search_download.py` already imports symbols from `telegram_price_bot.py`.

**Tech Stack:** Python 3 stdlib only for `avito_client.py` (`urllib`, no new dependency); `openpyxl` (already a project dependency as of the purchase-report feature) for the workbook.

## Global Constraints

- Read-only only: this plan must not add any Avito or MoySklad write/PUT/POST-mutation calls. Avito's `get()` wrapper should not even have a `put`/`post` method — there is no legitimate use for one in this feature.
- Fuzzy title matching uses a stricter confidence threshold (`0.9`) than the existing interactive search feature's `MATCH_THRESHOLD` (`0.72`), since nothing here is reviewed by a human before being classified — this is `AVITO_MATCH_THRESHOLD` in the new module, kept separate from the existing constant.
- Follow the existing retry/backoff convention already used by `MoySkladClient` (`scripts/telegram_price_bot.py:361-425`) rather than introducing a shared HTTP-client abstraction — accepted duplication, consistent with how this codebase already duplicates this pattern across files.
- Secrets (`AVITO_CLIENT_ID`, `AVITO_CLIENT_SECRET`) come from `.env` only (already added to `.env.example`), loaded via the existing `load_dotenv`/`ENV_PATH` mechanism. Never persist the Avito access token to disk — keep it in memory only, refetched on process restart or expiry.
- Avito credentials are optional at bot startup (the bot must still run if they're unset) — the new button reports a clear configuration error at press-time instead. This avoids bricking the whole bot over one new feature not yet configured on a given deployment.
- This plan is Phase 1 only. No systemd timer, no persistent order-tracking state file, no "sold but no demand" auto-alert — that is a separate, later plan blocked on finding the Avito orders/delivery API endpoint (not yet located).
- No automated test suite exists anywhere in this project. "Tests" in this plan are manual verification runs against the real, live MoySklad and Avito accounts (via the credentials already in `.env`), matching how every other feature in this codebase has been verified so far.

---

### Task 1: `AvitoClient` core — auth, retrying GET, active-listing pagination

**Files:**
- Create: `scripts/avito_client.py`

**Interfaces:**
- Produces: `class AvitoError(RuntimeError)`; `@dataclass class AvitoItem: id: int; title: str; status: str; price: int`; `class AvitoClient.__init__(self, client_id: str, client_secret: str, ssl_context: ssl.SSLContext)`; `AvitoClient.get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]`; `AvitoClient.iter_active_items(self) -> list[AvitoItem]`.

- [ ] **Step 1: Write `scripts/avito_client.py`**

```python
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


AVITO_API_BASE_URL = "https://api.avito.ru"
AVITO_TOKEN_PATH = "/token"
AVITO_ITEMS_PATH = "/core/v1/items"
AVITO_ITEMS_PER_PAGE = 100
AVITO_TIMEOUT_SECONDS = 60
AVITO_MAX_RETRIES = 4
AVITO_MATCH_THRESHOLD = 0.9


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
        request = Request(
            f"{AVITO_API_BASE_URL}{AVITO_TOKEN_PATH}",
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

        access_token = data.get("access_token")
        expires_in = data.get("expires_in")
        if not access_token or not isinstance(expires_in, (int, float)):
            raise AvitoError(f"Avito auth response missing token fields: {data}")

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
```

- [ ] **Step 2: Compile-check**

Run: `python3 -m py_compile scripts/avito_client.py`
Expected: no output, exit code 0.

- [ ] **Step 3: Verify against the real, live Avito account (read-only GET calls only)**

Run this from the project root (`/Users/dimonzhi/Documents/proga/sklad`):

```bash
python3 - <<'EOF'
import sys, os, ssl
sys.path.insert(0, "scripts")
from avito_client import AvitoClient

import telegram_price_bot as bot
bot.load_dotenv(bot.ENV_PATH)

client_id = os.environ["AVITO_CLIENT_ID"]
client_secret = os.environ["AVITO_CLIENT_SECRET"]
ssl_context = bot.create_ssl_context()

client = AvitoClient(client_id, client_secret, ssl_context)
items = client.iter_active_items()
print("active item count:", len(items))
assert len(items) > 0, "expected at least one active listing"
for item in items[:3]:
    print(item)
    assert item.status == "active"
    assert item.title
print("OK")
EOF
```

Expected: `active item count: <a positive number>`, three sample `AvitoItem(...)` rows printed, ends with `OK`.

- [ ] **Step 4: Clean up bytecode cache and commit**

```bash
find scripts -name __pycache__ -exec rm -rf {} + 2>/dev/null
git add scripts/avito_client.py
git commit -m "$(cat <<'EOF'
Add read-only Avito API client (auth + active listings)

Core Items API confirmed live: GET /core/v1/items?status=active
paginates via page/per_page (max 100), auth via POST /token with
client_credentials. No write methods — this client only ever GETs.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Title matching and full quantity-diff computation

**Files:**
- Modify: `scripts/avito_client.py` (append to the file created in Task 1)

**Interfaces:**
- Consumes: `AvitoClient.iter_active_items() -> list[AvitoItem]` (Task 1); `telegram_price_bot.AssortmentCard` (`name: str, href: str, available_quantity: Decimal | None`), `telegram_price_bot.album_match_score(query: str, album_name: str) -> float`, `telegram_price_bot.load_assortment_cards(client: MoySkladClient) -> list[AssortmentCard]`, `telegram_price_bot.MoySkladClient`.
- Produces: `@dataclass class QuantityDiffRow: avito_title: str; matched_name: str | None; match_score: float; avito_active_count: int; moysklad_quantity: Decimal | None; status: str`; constants `STATUS_OK = "OK"`, `STATUS_MISMATCH = "MISMATCH"`, `STATUS_NO_MATCH = "NO_CONFIDENT_MATCH"`; `build_quantity_diff_rows(moysklad_client: MoySkladClient, avito_client: AvitoClient) -> list[QuantityDiffRow]`.

- [ ] **Step 1: Add imports and matching/diff logic to `scripts/avito_client.py`**

Add these imports at the top of the file (after the existing `from urllib.request import Request, urlopen` line):

```python
from decimal import Decimal

from telegram_price_bot import AssortmentCard, MoySkladClient, album_match_score, load_assortment_cards
```

Append to the end of the file:

```python
@dataclass
class AssortmentMatch:
    card: AssortmentCard
    score: float


def match_to_assortment(title: str, cards: list[AssortmentCard]) -> AssortmentMatch | None:
    best_card: AssortmentCard | None = None
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
    moysklad_client: MoySkladClient,
    avito_client: AvitoClient,
) -> list[QuantityDiffRow]:
    cards = load_assortment_cards(moysklad_client)
    avito_items = avito_client.iter_active_items()

    avito_count_by_href: dict[str, int] = {}
    match_score_by_href: dict[str, float] = {}
    unmatched_counts: dict[str, int] = {}

    for item in avito_items:
        match = match_to_assortment(item.title, cards)
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
```

Note the design choice made here: items with zero active Avito listings AND zero/unknown MoySklad quantity are skipped entirely (nothing to report). Everything else — including confident `OK` matches — is included, since the approved spec calls for "an Excel file comparing MoySklad quantity against Avito listing count for every title, with mismatches flagged," not a mismatches-only filter.

- [ ] **Step 2: Compile-check**

Run: `python3 -m py_compile scripts/avito_client.py`
Expected: no output, exit code 0.

- [ ] **Step 3: Verify against real, live MoySklad + Avito data (read-only)**

```bash
python3 - <<'EOF'
import sys, os
sys.path.insert(0, "scripts")
from avito_client import AvitoClient, build_quantity_diff_rows, STATUS_OK, STATUS_MISMATCH, STATUS_NO_MATCH

import telegram_price_bot as bot
bot.load_dotenv(bot.ENV_PATH)

ssl_context = bot.create_ssl_context()
moysklad = bot.MoySkladClient(
    os.environ["MOYSKLAD_TOKEN"],
    os.environ.get("MOYSKLAD_BASE_URL", bot.DEFAULT_MOYSKLAD_BASE_URL),
    ssl_context,
)
avito = AvitoClient(os.environ["AVITO_CLIENT_ID"], os.environ["AVITO_CLIENT_SECRET"], ssl_context)

rows = build_quantity_diff_rows(moysklad, avito)
print("total rows:", len(rows))
by_status = {}
for row in rows:
    by_status[row.status] = by_status.get(row.status, 0) + 1
print("by status:", by_status)

assert len(rows) > 0, "expected at least one row given live data on both sides"
assert set(by_status).issubset({STATUS_OK, STATUS_MISMATCH, STATUS_NO_MATCH})

for row in rows[:5]:
    print(row)
print("OK")
EOF
```

Expected: `total rows: <positive number>`, a `by status: {...}` breakdown across the three known status values, five sample rows printed, ends with `OK`. Sanity-check by eye: the `NO_CONFIDENT_MATCH` count shouldn't be close to the total active-listing count (that would indicate the matching threshold or logic is broken) — spot check a couple of those titles manually against MoySklad to see whether they're genuinely unmatched (e.g. a non-vinyl category item) or a matching bug.

- [ ] **Step 4: Clean up bytecode cache and commit**

```bash
find scripts -name __pycache__ -exec rm -rf {} + 2>/dev/null
git add scripts/avito_client.py
git commit -m "$(cat <<'EOF'
Add Avito/MoySklad title matching and quantity-diff computation

Reuses telegram_price_bot's existing fuzzy matcher (album_match_score)
at a stricter threshold (0.9 vs the search feature's 0.72) since
nothing here is reviewed by a human before being classified.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Wire the `"Проверить количество"` button into the bot

**Files:**
- Modify: `scripts/telegram_price_bot.py:52` (button constants), `scripts/telegram_price_bot.py:1176-1189` (`main_keyboard`), `scripts/telegram_price_bot.py:1040-1050` (near `build_purchase_report_workbook`, add sibling workbook builder), `scripts/telegram_price_bot.py:1247-1254` (`handle_message` signature), `scripts/telegram_price_bot.py:1298` area (new button handler, alongside the existing `REPORT_BUTTON` block), `scripts/telegram_price_bot.py:1482-1567` (`main`)
- Modify: `README.md`

**Interfaces:**
- Consumes: `avito_client.AvitoClient`, `avito_client.AvitoError`, `avito_client.build_quantity_diff_rows`, `avito_client.QuantityDiffRow` (Tasks 1–2).
- Produces: nothing consumed by later tasks — this is the last task in Phase 1.

- [ ] **Step 1: Add the import and button constant**

In `scripts/telegram_price_bot.py`, near the top after the `from openpyxl import Workbook` line, add:

```python
from avito_client import AvitoClient, AvitoError, QuantityDiffRow, build_quantity_diff_rows
```

Change line 52 from:

```python
REPORT_BUTTON = "Отчет по закупкам"
```

to:

```python
REPORT_BUTTON = "Отчет по закупкам"
CHECK_QUANTITY_BUTTON = "Проверить количество"
```

- [ ] **Step 2: Add the button to the admin keyboard**

In `main_keyboard` (currently at line 1176), change:

```python
    if role == ROLE_ADMIN:
        keyboard = [
            [{"text": SEARCH_BUTTON}],
            [{"text": CONVERT_BUTTON}],
            [{"text": REPORT_BUTTON}],
        ]
```

to:

```python
    if role == ROLE_ADMIN:
        keyboard = [
            [{"text": SEARCH_BUTTON}],
            [{"text": CONVERT_BUTTON}],
            [{"text": REPORT_BUTTON}],
            [{"text": CHECK_QUANTITY_BUTTON}],
        ]
```

- [ ] **Step 3: Add a workbook builder for the diff rows**

Immediately after the existing `build_purchase_report_workbook` function (ends around line 1050 with `return buffer.getvalue()`), add:

```python
def build_quantity_diff_workbook(rows: list[QuantityDiffRow]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Проверка количества"
    sheet.append(
        ["Название", "Кол-во на Avito", "Кол-во в МойСклад", "Статус", "Уверенность совпадения"]
    )
    for row in rows:
        moysklad_quantity = (
            float(row.moysklad_quantity) if row.moysklad_quantity is not None else "неизвестно"
        )
        sheet.append(
            [
                row.avito_title,
                row.avito_active_count,
                moysklad_quantity,
                row.status,
                round(row.match_score, 3),
            ]
        )

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
```

- [ ] **Step 4: Update `handle_message`'s signature and add the button handler**

Change the `handle_message` signature (currently at line 1247) from:

```python
def handle_message(
    telegram: TelegramClient,
    moysklad: MoySkladClient,
    states: dict[int, DialogState],
    chat_id: int,
    text: str,
    role: str,
) -> None:
```

to:

```python
def handle_message(
    telegram: TelegramClient,
    moysklad: MoySkladClient,
    avito: AvitoClient | None,
    states: dict[int, DialogState],
    chat_id: int,
    text: str,
    role: str,
) -> None:
```

Immediately after the existing `REPORT_BUTTON` handler block (it ends with the `return` following the `send_document` call, right before the `state = states.get(chat_id)` line), add:

```python
    if normalized_text == CHECK_QUANTITY_BUTTON:
        if role != ROLE_ADMIN:
            states.pop(chat_id, None)
            telegram.send_message(
                chat_id,
                "Эта команда доступна только админу.",
                reply_markup=main_keyboard(role),
            )
            return

        states.pop(chat_id, None)
        if avito is None:
            telegram.send_message(
                chat_id,
                "Avito не настроен: заполните AVITO_CLIENT_ID и AVITO_CLIENT_SECRET в .env.",
                reply_markup=main_keyboard(role),
            )
            return

        telegram.send_message(chat_id, "Проверяю количество в Avito и МойСклад...")
        try:
            rows = build_quantity_diff_rows(moysklad, avito)
        except (MoySkladError, AvitoError, BotError) as error:
            telegram.send_message(
                chat_id,
                f"Не получилось проверить количество: {error}",
                reply_markup=main_keyboard(role),
            )
            return

        if not rows:
            telegram.send_message(
                chat_id,
                "Расхождений не найдено.",
                reply_markup=main_keyboard(role),
            )
            return

        workbook_bytes = build_quantity_diff_workbook(rows)
        telegram.send_document(
            chat_id,
            filename=f"quantity_check_{datetime.now():%Y%m%d_%H%M}.xlsx",
            file_bytes=workbook_bytes,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            caption=f"Проверено позиций: {len(rows)}",
            reply_markup=main_keyboard(role),
        )
        return
```

- [ ] **Step 5: Wire `AvitoClient` construction and the updated call site into `main()`**

In `main()`, immediately after the existing block:

```python
    user_access_password = os.environ.get(
        "TELEGRAM_USER_ACCESS_PASSWORD",
        DEFAULT_TELEGRAM_USER_ACCESS_PASSWORD,
    ).strip()
```

add:

```python
    avito_client_id = os.environ.get("AVITO_CLIENT_ID", "").strip()
    avito_client_secret = os.environ.get("AVITO_CLIENT_SECRET", "").strip()
```

Then, after the existing line:

```python
    moysklad = MoySkladClient(moysklad_token, moysklad_base_url, ssl_context)
```

add:

```python
    avito = (
        AvitoClient(avito_client_id, avito_client_secret, ssl_context)
        if avito_client_id and avito_client_secret
        else None
    )
```

Finally, update the `handle_message` call site (currently `handle_message(telegram, moysklad, states, chat_id, text, role)`) to:

```python
                handle_message(telegram, moysklad, avito, states, chat_id, text, role)
```

- [ ] **Step 6: Update README**

In `README.md`, change:

```markdown
Required values:

- `MOYSKLAD_TOKEN` - MoySklad API token.
- `TELEGRAM_BOT_TOKEN` - service Telegram bot token.
- `TELEGRAM_ACCESS_PASSWORD` - admin password users enter in Telegram.
- `TELEGRAM_USER_ACCESS_PASSWORD` - regular user password for stock search only.
```

to:

```markdown
Required values:

- `MOYSKLAD_TOKEN` - MoySklad API token.
- `TELEGRAM_BOT_TOKEN` - service Telegram bot token.
- `TELEGRAM_ACCESS_PASSWORD` - admin password users enter in Telegram.
- `TELEGRAM_USER_ACCESS_PASSWORD` - regular user password for stock search only.

Optional (needed only for the `Проверить количество` button):

- `AVITO_CLIENT_ID` / `AVITO_CLIENT_SECRET` - Avito API credentials from
  avito.ru/professionals/api. If unset, the bot still runs; the button
  replies with a configuration error instead of a report.
```

- [ ] **Step 7: Compile-check**

Run: `python3 -m py_compile scripts/telegram_price_bot.py scripts/avito_client.py`
Expected: no output, exit code 0.

- [ ] **Step 8: Verify the full path end-to-end against real, live data (still read-only)**

This exercises exactly what the button handler does, without needing a live Telegram chat:

```bash
python3 - <<'EOF'
import sys, os
sys.path.insert(0, "scripts")
import telegram_price_bot as bot
from avito_client import AvitoClient, build_quantity_diff_rows

bot.load_dotenv(bot.ENV_PATH)
ssl_context = bot.create_ssl_context()
moysklad = bot.MoySkladClient(
    os.environ["MOYSKLAD_TOKEN"],
    os.environ.get("MOYSKLAD_BASE_URL", bot.DEFAULT_MOYSKLAD_BASE_URL),
    ssl_context,
)
avito = AvitoClient(os.environ["AVITO_CLIENT_ID"], os.environ["AVITO_CLIENT_SECRET"], ssl_context)

rows = build_quantity_diff_rows(moysklad, avito)
data = bot.build_quantity_diff_workbook(rows)
print("rows:", len(rows), "xlsx bytes:", len(data))

out_path = "/private/tmp/claude-501/-Users-dimonzhi-Documents-proga-sklad/d1b2dfa1-8461-4c1b-a759-558b61ccded0/scratchpad/quantity_check_test.xlsx"
with open(out_path, "wb") as f:
    f.write(data)

import openpyxl
wb = openpyxl.load_workbook(out_path)
ws = wb.active
print("sheet title:", ws.title)
print("dims:", ws.dimensions)
for r in list(ws.iter_rows(values_only=True))[:5]:
    print(r)
EOF
```

Expected: `rows: <n> xlsx bytes: <n>`, sheet title `Проверка количества`, a `dims` range covering all rows, and five sample rows printed with sensible-looking title/count/status values.

Note: this verifies every internal function the button calls, but not the literal Telegram button tap (no live chat available in this environment) — do one real end-to-end press in Telegram after deploying to confirm the message/file actually arrives as expected.

- [ ] **Step 9: Clean up bytecode cache and commit**

```bash
find scripts -name __pycache__ -exec rm -rf {} + 2>/dev/null
git add scripts/telegram_price_bot.py README.md
git commit -m "$(cat <<'EOF'
Add "Проверить количество" button for Avito/MoySklad reconciliation

Admin-only button sends an Excel file comparing MoySklad stock against
active Avito listing counts per matched title. Avito credentials are
optional at startup — the button reports a config error if unset
rather than the whole bot refusing to start.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```
