# Avito ↔ MoySklad Quantity Reconciliation

Date: 2026-07-28

## Problem

Vinyl quantities in MoySklad and Avito drift apart. Workers sometimes forget
to create a MoySklad `demand` document after a sale, or MoySklad's quantity
for a title is otherwise wrong. There is currently no way to detect this
without manually cross-checking both systems by hand.

## Goals

1. On-demand: an admin can press a button in the Telegram bot and get an
   Excel file comparing MoySklad quantity against Avito listing count for
   every title, with mismatches flagged.
2. Automatic: when an Avito order is delivered (i.e. sold) and no matching
   MoySklad `demand` document shows up within 2 days, notify all authorized
   Telegram users (admins and regular users) so someone creates the missing
   paperwork or fixes the quantity.

## Non-goals

- Writing back to Avito or MoySklad automatically (this is detection/alerting
  only, no auto-correction).
- Solving item matching via a shared SKU — confirmed there isn't one. Matching
  is fuzzy by title, same technique as the existing purchase-order search
  (`album_match_score`).

## UPDATE 2026-07-30 — findings from the live API, after Phase 1 shipped

Investigating the real Avito API invalidated two assumptions in this spec and
unblocked Phase 2 by a different route. Read this before the sections below.

**Avito exposes no quantity for this account.** Verified against the live API and
the public site:

- `GET /core/v1/items` returns only `address, category, id, price, status, title, url`.
- `GET /core/v1/accounts/{id}/items/{item_id}` returns only
  `start_time, finish_time, status, url, vas`.
- `GET /autoload/v2/items/...` → **403, "автозагрузка вам недоступна. Для
  подключения необходимо оплатить тариф"**. Avito's Autoload feed is the only
  place its API carries a `Quantity` field, and this account lacks that tariff.
- A live ad page contains no quantity either: the string `шт` appears zero times
  and the page data reports `isCartEnabled: false` (a plain classified ad in
  «Коллекционирование»). So scraping cannot recover the number — it does not exist.

**"One ad = one physical copy" (recorded below as user-confirmed) does not hold in
practice.** Of 1472 active ads covering 1467 unique normalized titles, 1463 titles
have exactly one ad. The account lists one ad per title regardless of how many
copies it holds. Counting ads therefore yields only 0 or 1, which is what the
original Phase 1 report showed. Phase 1 was reframed to compare *presence on
Avito* against *MoySklad stock* rather than two quantities.

**No orders/delivery API was found**, so Phase 2's original trigger ("delivered
order") is not available. Probed and 404: `/cpa/v1/orders`, `/cpa/v2/orders`,
`/core/v1/orders`, `/order-management/v1/orders`, `/delivery/v1/orders`,
`/id/v1/orders`.

**Phase 2 can instead be built on a signal Phase 1 already computes.** Closed ads
are cheaply listable (`status=old` → 1927 ads, `status=removed` → 52, together
~12s), and the combination *MoySklad stock ≥ 1 while the matching Avito ad is
closed* isolated 68 items on live data — exactly the "sold but nobody created the
отгрузка" case Phase 2 exists to alert on. A periodic script can persist these by
item, and alert once an item has been in that state for 2+ days. Caveats to design
around: an ad can also close because its placement expired rather than because it
sold, so the signal is a heuristic; and closed ads are matched on exact normalized
title only (a miss degrades safely into the lower-signal "not listed" bucket).

## Known unknowns (must be resolved during implementation, not assumed)

- Exact Avito API endpoint paths, OAuth2 flow details, and response field
  names for delivered orders and active listings. Nothing below has been
  verified against a live Avito account — unlike the MoySklad integration in
  this project, which was checked against a live token before being trusted.
  The first implementation step is a throwaway spike script hitting the real
  API to confirm shapes, exactly like the ad-hoc scripts run earlier in this
  project against `/report/profit/byproduct` before it was relied on.
- Whether Avito's order data carries a day-precision "delivered at" timestamp
  (needed for the 2-day rule).
- The real-world false-positive/negative rate of fuzzy title matching at
  full-catalog scale (hundreds–thousands of titles) is unverified. The
  design's mitigation (confidence column, threshold, human-reviewable
  "no confident match" bucket) is a starting point, not a guarantee.

## User-confirmed constraints

- Avito API access does not exist yet; the user will request it via
  cabinet.avito.ru → Настройки → API and consult developers.avito.ru.
- No shared identifier between an Avito listing/order and a MoySklad item —
  matching is by title only.
- Avito listing model: one ad = one physical copy. "Sold" is represented by
  a real order going through Avito's delivery flow to a "delivered" status
  (the user's own words: "check orders that was delivered = sold"), not by
  an ad merely disappearing.
- The "sold but no demand" alert goes to **all** authorized users (admins
  and regular users), not just admins.
- The periodic check should run every few hours (not once a day, not only
  on incoming messages).

## Architecture

### New module: `scripts/avito_client.py` (Phase 1, shared, no Telegram/systemd coupling)

- `AvitoClient` — OAuth2 `client_credentials` token fetch, cache, and
  auto-refresh; a generic retrying `get()`/`post()` wrapper mirroring
  `MoySkladClient`'s existing retry/backoff style (429/5xx handling,
  exponential backoff, `MAX_RETRIES`). Raises `AvitoError` on exhaustion.
- `get_delivered_orders(since: datetime) -> list[AvitoOrder]` — orders
  delivered on/after `since`. Endpoint/fields unverified (see Unknowns).
- `get_active_listings() -> list[AvitoListing]` — currently active (unsold)
  ad titles, used to count how many copies of a title are still listed.
- `match_to_assortment(title: str, cards: list[AssortmentCard]) -> MatchResult | None`
  — reuses `telegram_price_bot.album_match_score` (imported, not
  duplicated) to fuzzy-match an Avito title against MoySklad assortment
  cards. Returns the best match and its score, or `None` if nothing scores
  above zero.
- `build_quantity_diff_rows(moysklad_client, avito_client) -> list[QuantityDiffRow]`
  — Phase 1 core. For each MoySklad assortment item: count matched active
  Avito listings, compare to MoySklad `available_quantity`, and emit a row
  with a match-confidence column and a status of `OK` / `MISMATCH` /
  `NO CONFIDENT MATCH`. A stricter confidence threshold than interactive
  search is used here (proposed: 0.9 vs. the search feature's 0.72) since
  no human reviews each match individually before it's presented.

### Phase 1 changes to `telegram_price_bot.py`

- New button `"Проверить количество"`, admin-only (same tier as
  `"Отчет по закупкам"`).
- Handler calls `avito_client.build_quantity_diff_rows(...)` synchronously
  and sends the result as an `.xlsx` via the existing `send_document` /
  workbook-building pattern already used by the purchase report. No new
  background infrastructure needed — this runs in-process on button press.
- New env vars in `.env.example` and README: `AVITO_CLIENT_ID`,
  `AVITO_CLIENT_SECRET`, and any account/user-scoping identifier Avito's
  API requires (exact name TBD once the developer cabinet is accessible).

### New script: `scripts/avito_stock_check.py` (Phase 2, standalone + systemd timer)

- Imports `avito_client.py` plus `telegram_price_bot`'s `TelegramClient`,
  `MoySkladClient`, `load_authorized_users`, `load_dotenv`.
- Owns `data/avito_order_state.json`:
  `{order_id: {matched_name, matched_href, delivered_at, quantity, notified}}`,
  so orders are never reprocessed or re-alerted once resolved.
- New systemd `.timer` + `.service` pair under `deploy/systemd/`, following
  the existing example-unit convention, running every 4 hours
  (`OnUnitActiveSec=4h`, configurable via a constant in the script).

### Data flow (Phase 2 run)

1. Load `data/avito_order_state.json`.
2. Pull Avito orders delivered in a fixed rolling window — the last 14
   days, every run, regardless of when the script last ran successfully.
   This is simpler than tracking a separate "last checked" cursor and is
   self-healing if a run is missed or fails: the state file's `order_id`
   keys make re-fetching already-seen orders a no-op.
3. New order IDs not yet in state → fuzzy-match title → record with
   `delivered_at`, `notified: false`.
4. Every state entry ≥2 days old and not yet notified → check MoySklad
   `demand` documents for the matched item in the window → if a matching
   demand is found, mark `notified: true` silently (resolved, no alert);
   if not, send a Telegram message to all authorized users (admins +
   regular) and mark `notified: true` (fires once, never repeats for the
   same order).
5. Persist the updated state file in one write (whole-file replace, same
   pattern as `save_authorized_users`) so a crash mid-run can't corrupt it.

## Error handling

- `AvitoClient` follows the same retry/backoff convention as
  `MoySkladClient` rather than a shared abstraction — accepted duplication,
  consistent with how this codebase already duplicates this pattern across
  `telegram_price_bot.py` and `update_supply_prices_to_rub.py`.
- `avito_stock_check.py` wraps each run in one top-level try/except,
  logging to stderr and exiting non-zero on failure (systemd/journalctl
  visible). A failed run leaves state untouched; the next timer tick
  retries the same lookback window.
- Low-confidence matches are never auto-alerted — they only ever appear in
  the on-demand report's "no confident match" bucket for human review. This
  is the primary defense against false alarms given there is no shared SKU.

## Testing plan

No automated test suite exists anywhere in this project; verification is
manual, matching the project's existing convention (e.g. the purchase
report was validated by running it against the live MoySklad account and
inspecting real output before being trusted).

1. Spike script against the real Avito API to confirm endpoint
   paths/fields before writing `avito_client.py` for real.
2. Run `build_quantity_diff_rows` against production data, manually
   spot-check the Excel output against titles with known true stock/listing
   counts, tune the match threshold if needed.
3. Add a dry-run flag to `avito_stock_check.py` (mirrors
   `update_supply_prices_to_rub.py`'s `APPLY_CHANGES` pattern) that prints
   what it would alert without sending Telegram messages or writing state,
   so Phase 2 can be observed for several cycles before going live
   unsupervised.

## Rollout phasing

1. **Phase 1**: `avito_client.py` + `"Проверить количество"` button. Ships
   first, used to validate Avito API access and matching accuracy on real
   data before anything runs unsupervised.
2. **Phase 2**: `avito_stock_check.py` + systemd timer + the automatic
   "sold but no demand" alert. Built on top of the matching/lookup logic
   already proven correct in Phase 1.
