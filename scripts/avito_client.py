#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import ssl
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from http.client import HTTPException
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
# Avito unpublishes an ad 30 days after it goes up. Those ads land in
# "неопубликованные" and the seller re-activates them later — the record is
# still held and still for sale, so they count as listed however long they
# have been sitting there. Only an ad the seller actually deleted is treated
# as gone.
AVITO_PENDING_STATUSES = ("old",)
AVITO_CLOSED_STATUSES = ("removed",)
# Rate limits need more patience than a transient 5xx: plain 2**attempt backoff only
# buys ~15s total, which is well short of Avito's window. Bounded so a hostile or
# mistaken Retry-After can never park the single-threaded bot for a long stretch.
AVITO_RATE_LIMIT_BASE_DELAY_SECONDS = 5
AVITO_MAX_RETRY_DELAY_SECONDS = 60

# --- Title matching -------------------------------------------------------
# Titles read "Artist - Album (details)" on both sides, but Avito truncates to a
# character limit and abbreviates ("20th Ann" for "20th Anniversary", "OST" for
# "Original Motion Soundtrack"), while MoySklad carries the colour/pressing in
# brackets. So matching is structural: the artist must match, the album must
# match allowing for omissions, and the remaining details only break ties
# between several pressings of the same album.

# Only spaced dashes separate artist from album — a bare hyphen is usually part
# of a name ("Del-Tones", "9-Bit").
ARTIST_SEP = re.compile(r"\s+[-–—]\s+|\s+[-–—](?=\S)|(?<=\S)[-–—]\s+")
BRACKETS = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
# Leading "!"/"*" flags, plus reservation notes the seller puts in front of a
# name ("(Диме 1) Muse - ..."). Only a bracket containing Cyrillic is dropped,
# so an artist whose name genuinely starts in brackets survives.
LEADING_JUNK = re.compile(r"^(?:[!*\s]+|\([^)]*[А-Яа-яЁё][^)]*\)\s*)+")
SLASH = re.compile(r"\s+/\s+")
# "1 LP", "2 CD", "3 винил" — a disc count, not a sequel number.
FORMAT_QUANTITY = re.compile(r"\b\d+\s*(?:lp|cd|cds|ep|vinyl|винил)s?\b", re.IGNORECASE)

# Dropped from the artist side only, where they join collaborators.
ARTIST_CONNECTORS = {"and", "feat", "ft", "featuring", "with", "vs", "x", "the"}
MEDIA_CD = {"cd", "2cd", "3cd", "cds"}
MEDIA_VINYL = {"lp", "2lp", "3lp", "4lp", "5lp", "ep", "vinyl", "винил"}
MEDIA_OTHER = {"cassette", "mc", "dvd", "blu"}
# Words that never identify which record this is.
ALBUM_NOISE = {
    "ost", "soundtrack", "original", "motion", "picture", "score",
    "limited", "edition", "exclusive", "deluxe", "anniversary", "ann",
    "remastered", "reissue", "version", "colored", "цветной", "coloured", "the",
} | MEDIA_VINYL | MEDIA_OTHER | MEDIA_CD
ALBUM_SYNONYMS = {"ost": "soundtrack"}

ARTIST_MIN_SCORE = 0.88
ALBUM_MIN_SCORE = 0.80
# Overall bar for calling an ad and a card the same record. Passing the artist
# and album gates alone let rubbish through — "Dexter OST Vinyl" reached
# "D.R.I. - But Wait ... There's More!" at 0.72 — and a card wrongly counted as
# listed is worse than one reported as missing, because the seller never learns
# it needs an ad.
MATCH_MIN_SCORE = 0.85
# How many cards an ad may fall back to when its best one is already taken.
MATCH_CANDIDATES_PER_AD = 5
# Nudge towards a card that is actually in stock when the text cannot separate
# two pressings. Sized to the noise in the whole-title similarity term.
STOCK_PREFERENCE_BONUS = 0.05


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
        except json.JSONDecodeError as error:
            raise AvitoError(f"Avito API returned invalid JSON from {url}") from error
        except (URLError, HTTPException, OSError) as error:
            reason = getattr(error, "reason", error)
            raise AvitoError(f"Avito auth network error: {reason}") from error

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
            except json.JSONDecodeError as error:
                raise AvitoError(f"Avito API returned invalid JSON from {url}") from error
            except (URLError, HTTPException, OSError) as error:
                # Avito drops long-running connections ("Remote end closed
                # connection without response"), which surfaces as
                # RemoteDisconnected rather than URLError and would otherwise
                # abort the whole report part-way through.
                if attempt < AVITO_MAX_RETRIES:
                    time.sleep(2**attempt)
                    continue
                reason = getattr(error, "reason", error)
                raise AvitoError(f"Avito network error: {reason}") from error

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

    def iter_pending_items(self) -> list[AvitoItem]:
        """Ads whose 30-day placement lapsed and that wait in
        "неопубликованные" to be re-activated. Still the seller's listings."""
        items: list[AvitoItem] = []
        for status in AVITO_PENDING_STATUSES:
            items.extend(self.iter_items(status))
        return items

    def iter_closed_items(self) -> list[AvitoItem]:
        """Ads the seller deleted outright."""
        items: list[AvitoItem] = []
        for status in AVITO_CLOSED_STATUSES:
            items.extend(self.iter_items(status))
        return items


@dataclass
class AssortmentMatch:
    card: "AssortmentCard"
    score: float


@lru_cache(maxsize=None)
def split_artist_album(name: str) -> tuple[str | None, str]:
    """"Artist - Album (details)" -> ("Artist", "Album (details)").
    Returns (None, whole) when there is no usable separator."""
    cleaned = LEADING_JUNK.sub("", name).strip()
    parts = ARTIST_SEP.split(cleaned, 1)
    if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
        return None, cleaned
    return parts[0].strip(), parts[1].strip()


def _canon(tokens: list[str]) -> list[str]:
    return [ALBUM_SYNONYMS.get(token, token) for token in tokens]


@lru_cache(maxsize=None)
def _identity_numbers(text: str) -> frozenset[str]:
    """Numbers that say *which* record this is — a sequel or volume — with disc
    counts ("1 LP") excluded, since those describe the pressing instead."""
    from telegram_price_bot import text_tokens

    return frozenset(t for t in text_tokens(FORMAT_QUANTITY.sub(" ", text)) if t.isdigit())


@lru_cache(maxsize=None)
def _canon_text(text: str) -> str:
    """Same synonyms, applied to raw text for the whole-title fallback, where
    "Baby Driver soundtrack" and "Baby Driver OST" are the same record."""
    def swap(match: re.Match[str]) -> str:
        word = match.group(0)
        return ALBUM_SYNONYMS.get(word.lower(), word)

    return re.sub(r"[A-Za-zА-Яа-яЁё]+", swap, text)


@lru_cache(maxsize=None)
def artist_tokens(artist: str) -> tuple[str, ...]:
    from telegram_price_bot import text_tokens

    return tuple(t for t in text_tokens(artist) if t not in ARTIST_CONNECTORS)


@lru_cache(maxsize=None)
def album_core_tokens(rest: str) -> tuple[str, ...]:
    """The album's identity. Falls back progressively so a title made entirely
    of bracketed text or of noise words still yields something to compare."""
    from telegram_price_bot import text_tokens

    outside = _canon(text_tokens(BRACKETS.sub(" ", rest)))
    core = tuple(t for t in outside if t not in ALBUM_NOISE)
    if core:
        return core
    if outside:  # e.g. "Original Motion Picture Soundtrack"
        return tuple(outside)
    inside = _canon(text_tokens(rest))  # e.g. "(Robot Face)"
    return tuple(t for t in inside if t not in ALBUM_NOISE) or tuple(inside)


@lru_cache(maxsize=None)
def detail_tokens(rest: str) -> tuple[str, ...]:
    from telegram_price_bot import text_tokens

    core = set(album_core_tokens(rest))
    return tuple(t for t in _canon(text_tokens(rest)) if t not in core)


@lru_cache(maxsize=None)
def media_type(full_name: str) -> str | None:
    from telegram_price_bot import text_tokens

    tokens = set(text_tokens(full_name))
    if tokens & MEDIA_VINYL:  # "2LP+CD" is a vinyl release with a bonus disc
        return "vinyl"
    if tokens & MEDIA_CD:
        return "cd"
    if tokens & MEDIA_OTHER:
        return "other"
    return None


def token_similarity(left: str, right: str) -> float:
    if left == right:
        return 1.0
    # Avito abbreviates: "ann" for "anniversary", "philarm" for "philarmonic".
    if len(left) >= 3 and (right.startswith(left) or left.startswith(right)):
        return 0.92
    return SequenceMatcher(None, left, right).ratio()


def token_coverage(needles, haystack) -> float:
    """How well every token in `needles` is represented in `haystack`."""
    if not needles or not haystack:
        return 0.0
    # Guard: without a single shared word the coverage cannot clear the bar,
    # and the loop below is the hot path (SequenceMatcher per token pair).
    if not set(needles) & set(haystack):
        cheap = max(
            max(token_similarity(n, h) for h in haystack) for n in needles
        )
        if cheap < ALBUM_MIN_SCORE:
            return 0.0
    return sum(max(token_similarity(n, h) for h in haystack) for n in needles) / len(needles)


def artist_similarity(left: list[str], right: list[str]) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    # One side truncated or missing a collaborator: "elton john brandi" within
    # "elton john brandi carlile", "blue stones" within "the blue stones".
    if set(left) <= set(right) or set(right) <= set(left):
        # A bare number is a sequel, not a dropped collaborator: "The Devil
        # Wears Prada 2" is a different film from "The Devil Wears Prada".
        if any(token.isdigit() for token in set(left) ^ set(right)):
            return 0.0
        return 0.95
    short, long_ = (left, right) if len(left) <= len(right) else (right, left)
    score = token_coverage(short, long_)
    return score if score >= ARTIST_MIN_SCORE else 0.0


def score_pair(avito_title: str, moysklad_name: str) -> float:
    """0 means "not the same record". Higher is a better match."""
    from telegram_price_bot import album_match_score, normalize_text, text_tokens

    avito_media, moysklad_media = media_type(avito_title), media_type(moysklad_name)
    if avito_media and moysklad_media and avito_media != moysklad_media:
        return 0.0
    # Avito ads are vinyl unless they say otherwise, and MoySklad marks its CDs,
    # so a CD card facing a non-CD ad is a different product.
    if moysklad_media == "cd" and avito_media != "cd":
        return 0.0

    avito_artist, avito_rest = split_artist_album(avito_title)
    moysklad_artist, moysklad_rest = split_artist_album(moysklad_name)
    if avito_artist is None or moysklad_artist is None:
        # No parseable artist on one side (compilations, soundtracks): fall back
        # to whole-title similarity rather than losing the match entirely.
        # Compilations and soundtracks carry no artist. Compare identity words
        # only: album_match_score demands that *every* word of the ad appear on
        # the card, which "Aerosmith's Greatest Hits (1 LP)" fails against
        # "Aerosmith's Greatest Hits" purely because of the format suffix.
        # Same sequel rule as the artist gate, which this path skips: without
        # it "The Devil Wears Prada (Music From Motion Picture)" reaches
        # "The Devil Wears Prada 2 - ...", and "Guardians of the Galaxy"
        # reaches "Guardians of the Galaxy Vol. 2".
        if _identity_numbers(avito_title) != _identity_numbers(moysklad_name):
            return 0.0

        avito_core = album_core_tokens(avito_title)
        moysklad_all = _canon(text_tokens(moysklad_name))
        if not avito_core or not moysklad_all:
            return 0.0
        core_score = token_coverage(avito_core, moysklad_all)
        if core_score < ALBUM_MIN_SCORE:
            return 0.0
        full_similarity = SequenceMatcher(
            None, normalize_text(_canon_text(avito_title)), normalize_text(_canon_text(moysklad_name))
        ).ratio()
        return 0.55 * core_score + 0.45 * full_similarity

    artist_score = artist_similarity(
        artist_tokens(avito_artist), artist_tokens(moysklad_artist)
    )
    if artist_score < ARTIST_MIN_SCORE:
        return 0.0

    avito_album = album_core_tokens(avito_rest)
    moysklad_album = album_core_tokens(moysklad_rest)
    if not avito_album or not moysklad_album:
        return 0.0
    # Score the ad into the card, never the reverse: the ad is the truncated
    # side, and a one-word card name must not trivially "cover" a longer ad.
    # Compare against every card token because Avito writes details inline
    # ("No Exit Clear 2LP") where MoySklad brackets them ("No Exit [Clear 2LP]").
    moysklad_all = _canon(text_tokens(moysklad_rest))
    album_score = token_coverage(avito_album, moysklad_all or moysklad_album)
    if album_score < ALBUM_MIN_SCORE:
        return 0.0

    # Details (colour, pressing, "signed") separate one pressing from another,
    # so they carry the tiebreak. Asymmetric on purpose: what the AD states
    # should appear on the CARD, never the reverse.
    avito_details, moysklad_details = detail_tokens(avito_rest), detail_tokens(moysklad_rest)
    if not avito_details:
        detail_score = 1.0
    elif not moysklad_details:
        # The ad adds something the card never carries ("(USA)", "+автографы").
        # Mildly discouraging, not damning: sellers routinely add such notes.
        detail_score = 0.75
    else:
        detail_score = token_coverage(avito_details, moysklad_details)

    full_similarity = SequenceMatcher(
        None, normalize_text(avito_title), normalize_text(moysklad_name)
    ).ratio()
    return (
        0.30 * album_score
        + 0.10 * artist_score
        + 0.25 * detail_score
        + 0.35 * full_similarity
    )


def title_variants(title: str) -> list[str]:
    """Bilingual ads ("Home Alone / Один дома") and alternate album names
    ("Weezer / Green Album") carry two names in one title; try each side."""
    variants = [title]
    base = BRACKETS.sub(" ", title)
    if SLASH.search(base):
        for part in SLASH.split(base):
            part = part.strip()
            if len(part) >= 3:
                variants.append(part)
    return variants


def build_artist_index(
    cards: list["AssortmentCard"],
) -> tuple[dict[str, list["AssortmentCard"]], list["AssortmentCard"], dict[str, list["AssortmentCard"]]]:
    """Index cards for lookup.

    By artist token, so a lookup still hits when one side carries extra words
    ("City of Prague ..." vs "Prague ..."), and by every word of the name, which
    is how titles carrying no artist at all (compilations, soundtracks) find
    candidates without scanning the whole catalogue.
    """
    from telegram_price_bot import text_tokens

    artist_index: dict[str, list["AssortmentCard"]] = {}
    unparsed: list["AssortmentCard"] = []
    token_index: dict[str, list["AssortmentCard"]] = {}
    for card in cards:
        artist, _ = split_artist_album(card.name)
        tokens = artist_tokens(artist) if artist is not None else ()
        if not tokens:
            unparsed.append(card)
        else:
            for token in set(tokens):
                artist_index.setdefault(token, []).append(card)
        for token in set(_canon(text_tokens(card.name))):
            token_index.setdefault(token, []).append(card)
    return artist_index, unparsed, token_index


def _match_one(
    title: str,
    index: dict[str, list["AssortmentCard"]],
    unparsed: list["AssortmentCard"],
    cards: list["AssortmentCard"],
    token_index: dict[str, list["AssortmentCard"]],
) -> AssortmentMatch | None:
    artist, _ = split_artist_album(title)
    tokens = artist_tokens(artist) if artist is not None else []

    if artist is None or not tokens:
        seen_hrefs: set[str] = set()
        candidates = []
        for token in set(album_core_tokens(title)):
            for card in token_index.get(token, []):
                if card.href not in seen_hrefs:
                    seen_hrefs.add(card.href)
                    candidates.append(card)
    else:
        candidates, seen = [], set()
        for token in set(tokens):
            for card in index.get(token, []):
                if card.href not in seen:
                    seen.add(card.href)
                    candidates.append(card)
        candidates.extend(unparsed)
        if not candidates:
            candidates = cards

    scored: list[AssortmentMatch] = []
    for card in candidates:
        score = score_pair(title, card.name)
        if score >= MATCH_MIN_SCORE:
            scored.append(AssortmentMatch(card=card, score=score))
    scored.sort(key=lambda m: -m.score)
    return scored[:MATCH_CANDIDATES_PER_AD]


def rank_matches(
    title: str,
    index: dict[str, list["AssortmentCard"]],
    unparsed: list["AssortmentCard"],
    cards: list["AssortmentCard"],
    token_index: dict[str, list["AssortmentCard"]],
) -> list[AssortmentMatch]:
    """Cards this ad could be for, best first. More than one is kept so an ad
    can fall back when a better-scoring ad has already claimed its first pick."""
    best_by_href: dict[str, AssortmentMatch] = {}
    for variant in title_variants(title):
        for match in _match_one(variant, index, unparsed, cards, token_index):
            existing = best_by_href.get(match.card.href)
            if existing is None or match.score > existing.score:
                best_by_href[match.card.href] = match
    ranked = sorted(best_by_href.values(), key=lambda m: -m.score)
    return ranked[:MATCH_CANDIDATES_PER_AD]


def match_to_assortment(
    title: str,
    index: dict[str, list["AssortmentCard"]],
    unparsed: list["AssortmentCard"],
    cards: list["AssortmentCard"],
    token_index: dict[str, list["AssortmentCard"]],
) -> AssortmentMatch | None:
    ranked = rank_matches(title, index, unparsed, cards, token_index)
    return ranked[0] if ranked else None


def assign_ads(
    items: list[AvitoItem],
    index: dict[str, list["AssortmentCard"]],
    unparsed: list["AssortmentCard"],
    cards: list["AssortmentCard"],
    token_index: dict[str, list["AssortmentCard"]],
) -> tuple[dict[str, float], list[AvitoItem]]:
    """Assign each ad to at most one card, and each card to at most one ad.

    The seller runs one ad per item, so letting a single ad mark several cards
    as listed hides the ones that genuinely need an ad — "Imagine (Limited
    Edition 2LP)" would otherwise also cover the plain "Imagine". Proposals are
    taken best-score-first, which lets the most specific ad win the card it
    describes before a vaguer one can take it.

    Returns (score by claimed card href, ads that claimed nothing).
    """
    proposals: list[tuple[float, int, "AssortmentCard"]] = []
    for position, item in enumerate(items):
        for match in rank_matches(item.title, index, unparsed, cards, token_index):
            proposals.append((match.score, position, match.card))
    def preference(proposal: tuple[float, int, "AssortmentCard"]) -> float:
        score, _, card = proposal
        has_stock = card.available_quantity is not None and card.available_quantity > 0
        return score + (STOCK_PREFERENCE_BONUS if has_stock else 0.0)

    # When an ad names no pressing, the text alone separates the candidates only
    # by how much trailing detail each card happens to carry — "(B/W Swirl)"
    # beat "(Amazon Exclusive)" purely for being shorter. Stock is the better
    # signal: a live ad is far more likely to be for a record the seller holds.
    # The bonus is sized to noise, so a clearly better text match still wins.
    proposals.sort(key=lambda proposal: -preference(proposal))

    claimed: dict[str, float] = {}
    used_positions: set[int] = set()
    for score, position, card in proposals:
        if position in used_positions or card.href in claimed:
            continue
        used_positions.add(position)
        claimed[card.href] = score

    unclaimed = [item for position, item in enumerate(items) if position not in used_positions]
    return claimed, unclaimed


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
# Placement expired; sitting in "неопубликованные" awaiting re-activation.
# Counts as listed, because the seller still holds and sells the record.
AVITO_STATE_PENDING = "не опубликовано"
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
    active_items = avito_client.iter_active_items()
    pending_items = avito_client.iter_pending_items()
    closed_items = avito_client.iter_closed_items()

    index, unparsed, token_index = build_artist_index(cards)

    # One ad per card, decided independently for each pool: a card may be
    # covered by a running ad, by one waiting in "неопубликованные", or by a
    # deleted one, and those mean different things.
    active_scores, unmatched_ads = assign_ads(active_items, index, unparsed, cards, token_index)
    pending_scores, _ = assign_ads(pending_items, index, unparsed, cards, token_index)
    closed_scores, _ = assign_ads(closed_items, index, unparsed, cards, token_index)

    action_required: list[ListingReportRow] = []
    not_listed: list[ListingReportRow] = []

    for card in cards:
        quantity = card.available_quantity
        has_active = card.href in active_scores
        has_pending = card.href in pending_scores
        has_closed = card.href in closed_scores
        is_listed = has_active or has_pending

        if has_active:
            state = AVITO_STATE_ACTIVE
        elif has_pending:
            state = AVITO_STATE_PENDING
        elif has_closed:
            state = AVITO_STATE_CLOSED
        else:
            state = AVITO_STATE_NONE

        row = ListingReportRow(
            name=card.name,
            moysklad_quantity=quantity,
            avito_state=state,
            status="",
            match_score=max(
                active_scores.get(card.href, 0.0),
                pending_scores.get(card.href, 0.0),
                closed_scores.get(card.href, 0.0),
            ),
        )

        if quantity is None:
            row.status = STATUS_UNKNOWN_QUANTITY
            action_required.append(row)
        elif has_active and quantity == 0:
            # Only a running ad can take an order that cannot be filled.
            row.status = STATUS_LISTED_NO_STOCK
            action_required.append(row)
        elif (not is_listed) and has_closed and quantity >= 1:
            row.status = STATUS_MAYBE_SOLD
            action_required.append(row)
        elif (not is_listed) and quantity >= 1:
            # In stock with no ad at all: the seller wants to know so it can
            # be listed.
            row.status = STATUS_NOT_LISTED
            not_listed.append(row)
        # Anything else is consistent and stays out of the report.

    unmatched_counts: dict[str, int] = {}
    for item in unmatched_ads:
        unmatched_counts[item.title] = unmatched_counts.get(item.title, 0) + 1

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
