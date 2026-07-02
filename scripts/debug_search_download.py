#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import telegram_price_bot as price_bot
from telegram_price_bot import (
    DATA_DIR,
    DEFAULT_MOYSKLAD_BASE_URL,
    ENV_PATH,
    SEARCH_MONTHS_BACK,
    MoySkladClient,
    album_match_score,
    create_ssl_context,
    format_moysklad_datetime,
    iter_collection,
    load_dotenv,
    search_assortment_cards,
    subtract_months,
)


MAX_DEBUG_ORDERS = 12
MAX_EXACT_TEST_NAMES = 50


def compact_assortment(row: dict[str, Any]) -> dict[str, Any]:
    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
    return {
        "name": row.get("name"),
        "href": meta.get("href"),
        "type": meta.get("type"),
        "archived": row.get("archived"),
        "quantity": row.get("quantity"),
        "stock": row.get("stock"),
        "reserve": row.get("reserve"),
    }


def compact_order(row: dict[str, Any], positions: list[dict[str, Any]]) -> dict[str, Any]:
    agent = row.get("agent") if isinstance(row.get("agent"), dict) else {}
    return {
        "name": row.get("name"),
        "moment": row.get("moment"),
        "applicable": row.get("applicable"),
        "agent": agent.get("name"),
        "positions": [
            {
                "quantity": position.get("quantity"),
                "assortment": compact_assortment(
                    position.get("assortment") if isinstance(position.get("assortment"), dict) else {}
                ),
            }
            for position in positions
        ],
    }


def write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def preload_search_cache(cards: list[dict[str, Any]]) -> None:
    price_bot._assortment_cache = price_bot.AssortmentCache(
        loaded_at=time.time(),
        cards=[
            price_bot.AssortmentCard(
                name=str(card.get("name") or "").strip(),
                href=str(card.get("href") or "").strip(),
                available_quantity=price_bot.available_quantity_from_assortment(card),
            )
            for card in cards
            if str(card.get("name") or "").strip() and str(card.get("href") or "").strip()
        ],
    )


def main() -> int:
    load_dotenv(ENV_PATH)
    token = os.environ.get("MOYSKLAD_TOKEN", "").strip()
    base_url = os.environ.get("MOYSKLAD_BASE_URL", DEFAULT_MOYSKLAD_BASE_URL).strip()
    if not token:
        print("Fill MOYSKLAD_TOKEN in .env first")
        return 1

    client = MoySkladClient(token, base_url, create_ssl_context())
    output_dir = DATA_DIR / "search_debug" / datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)

    print("Downloading assortment archive...")
    assortment_rows = iter_collection(client, "/entity/assortment", {"limit": 1000})
    cards = [compact_assortment(row) for row in assortment_rows]
    write_json(output_dir / "cards.json", cards)
    preload_search_cache(cards)

    since = subtract_months(datetime.now(), SEARCH_MONTHS_BACK)
    print("Downloading recent open purchase orders...")
    order_rows = iter_collection(
        client,
        "/entity/purchaseorder",
        {
            "filter": f"moment>={format_moysklad_datetime(since)};applicable=false",
            "expand": "agent",
            "limit": 100,
            "order": "moment,desc",
        },
    )

    orders: list[dict[str, Any]] = []
    exact_names: list[str] = []
    seen_names: set[str] = set()
    for row in order_rows[:MAX_DEBUG_ORDERS]:
        order_id = str(row.get("id") or "")
        if not order_id:
            continue

        positions = iter_collection(
            client,
            f"/entity/purchaseorder/{order_id}/positions",
            {"expand": "assortment", "limit": 1000},
        )
        orders.append(compact_order(row, positions))
        for position in positions:
            assortment = position.get("assortment")
            if not isinstance(assortment, dict):
                continue
            name = str(assortment.get("name") or "").strip()
            if name and name not in seen_names:
                exact_names.append(name)
                seen_names.add(name)
            if len(exact_names) >= MAX_EXACT_TEST_NAMES:
                break

    write_json(output_dir / "purchase_orders.json", orders)

    print("Testing exact names against downloaded assortment archive...")
    exact_tests: list[dict[str, Any]] = []
    for name in exact_names[:MAX_EXACT_TEST_NAMES]:
        try:
            candidates = search_assortment_cards(client, name)
        except Exception as error:
            exact_tests.append({"query": name, "error": str(error), "found_exact": False})
            continue

        found_exact = any(candidate.name == name for candidate in candidates)
        exact_tests.append(
            {
                "query": name,
                "found_exact": found_exact,
                "top": [
                    {
                        "name": candidate.name,
                        "score": round(candidate.score, 4),
                    }
                    for candidate in candidates[:5]
                ],
            }
        )

    failures = [test for test in exact_tests if not test.get("found_exact")]
    write_json(output_dir / "exact_search_tests.json", exact_tests)
    write_json(
        output_dir / "summary.json",
        {
            "cards": len(cards),
            "open_purchase_orders_last_months": len(order_rows),
            "downloaded_orders": len(orders),
            "exact_tests": len(exact_tests),
            "exact_failures": len(failures),
        },
    )

    print(f"Saved debug data to {output_dir}")
    print(f"Cards: {len(cards)}")
    print(f"Open purchase orders in last {SEARCH_MONTHS_BACK} months: {len(order_rows)}")
    print(f"Downloaded orders: {len(orders)}")
    print(f"Exact tests: {len(exact_tests)}, failures: {len(failures)}")
    if failures:
        print("First failures:")
        for failure in failures[:10]:
            print(f"- {failure['query']}")

    for name in exact_names[:10]:
        best_score = max((album_match_score(name, card["name"] or "") for card in cards), default=0)
        if best_score < 1:
            print(f"Archive best score for exact order position is {best_score}: {name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
