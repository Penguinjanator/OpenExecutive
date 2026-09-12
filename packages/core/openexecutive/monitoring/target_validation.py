"""Insert-time validation + normalization of watchlist targets.

The watchlist is largely populated by an LLM (the ``executive_research``
workflow) proposing ``rss`` feed URLs it never verified — historically
the great majority were dead (404 / 403 / not-actually-a-feed), producing
a watchlist full of rows that fetch nothing and a permanently-empty
briefing.

This module validates an ``rss`` target at insert time: it must fetch and
parse as a feed with entries. A target the server *answers* but that
isn't a feed is either:

  - converted to a ``page_watch`` when the page the feed check already
    fetched yields readable text (a news or pricing page with no feed), or
  - rejected (:class:`WatchlistTargetError`) when it does not,

so a dead ``rss`` row never reaches the scan loop. A transient / network
blip passes the target through unchanged rather than blocking a
legitimate add on a hiccup. Every non-``rss`` type returns unchanged with
no network I/O.
"""
from __future__ import annotations

import logging
from typing import Any, Literal

import feedparser
import httpx

from openexecutive.config import get_settings
from openexecutive.monitoring.models import (
    SOURCE_KIND_PAGE_WATCH,
    SOURCE_KIND_RSS,
)
from openexecutive.monitoring.sources._http import (
    FetchOverflowError,
    fetch_bounded,
    validate_target_url,
)
from openexecutive.monitoring.sources.page_watch import html_to_text

logger = logging.getLogger(__name__)

# A non-feed page becomes a page_watch only when its visible text is at least
# this long. Rules out empty shells, bare error strings and binary bodies that
# happen to decode to a few characters; a real article, pricing or news page
# clears it easily.
_MIN_PAGE_TEXT_CHARS = 200


class WatchlistTargetError(Exception):
    """A watchlist target failed insert-time validation (definitively bad)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def validate_and_normalize_target(
    signal_type: str, target: str, config: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    """Validate ``target`` for ``signal_type``; may convert the signal type.

    Returns the ``(signal_type, target, config)`` to insert — converted to
    ``page_watch`` when a non-feed ``rss`` target is a readable page. Raises
    :class:`WatchlistTargetError` when an ``rss`` target is definitively
    bad and can't be salvaged. Only ``rss`` is validated; every other type
    is returned unchanged with no network I/O.
    """
    if signal_type != SOURCE_KIND_RSS:
        return signal_type, target, config

    verdict, body = await _feed_check(target)
    if verdict == "feed":
        return signal_type, target, config
    if verdict == "unknown":
        # Transient fetch failure — inserting as-is beats rejecting a feed
        # that's merely unreachable right now; the scan loop retries.
        logger.info(
            "watchlist validate: %r unreachable now — inserting rss unverified",
            target,
        )
        return signal_type, target, config
    if verdict == "blocked":
        # SSRF-rejected / unparseable / non-public. Reject outright.
        raise WatchlistTargetError(
            f"{target!r} is not a fetchable public URL "
            "(blocked by the SSRF guard or unparseable)."
        )

    # verdict == "not_feed": a public server answered, but it is not a feed.
    # The body the feed check already fetched (same SSRF guard, same byte
    # cap the page_watch adapter uses) decides whether the page is worth
    # watching: readable text → page_watch; nothing readable → reject.
    if body is not None and _is_readable_page(body):
        new_config = dict(config)
        # page_watch reads config["label"]; the rss row used "feed_label".
        label = new_config.pop("feed_label", None)
        if label and "label" not in new_config:
            new_config["label"] = label
        logger.info(
            "watchlist validate: %r is not a feed but is a readable page — "
            "converting rss → page_watch", target,
        )
        return SOURCE_KIND_PAGE_WATCH, target, new_config

    raise WatchlistTargetError(
        f"{target!r} is not a valid RSS/Atom feed and yields no readable "
        "page text. Fix the feed URL, or add it as signal_type='page_watch'."
    )


def _is_readable_page(body: bytes) -> bool:
    """Whether a fetched body is a text page with enough visible content to
    watch. A NUL byte in the head marks a binary body (PDF, image, archive);
    the text threshold rejects shells and one-line error pages."""
    if b"\x00" in body[:4096]:
        return False
    return len(html_to_text(body)) >= _MIN_PAGE_TEXT_CHARS


async def _feed_check(
    target: str,
) -> tuple[Literal["feed", "not_feed", "blocked", "unknown"], bytes | None]:
    """Classify ``target``; also return the fetched body when there is one.

    ``feed`` — feedparser recognized it as a feed.
    ``not_feed`` — a public server answered, but it isn't a feed (the body
    is returned so the caller can decide whether it is a readable page;
    ``None`` when the server answered with an error or an oversized body).
    ``blocked`` — SSRF-rejected / unparseable / non-public URL (never fetched).
    ``unknown`` — transient fetch failure; caller should not penalize it.
    """
    ok, _reason = validate_target_url(target)
    if not ok:
        return "blocked", None
    max_bytes = get_settings().external_monitor_max_fetch_bytes
    try:
        body = await fetch_bounded(target, max_bytes)
    except FetchOverflowError:
        return "not_feed", None  # too big to be a sane feed or page
    except httpx.HTTPStatusError:
        return "not_feed", None  # server answered 4xx/5xx → definitively bad
    except (httpx.HTTPError, httpx.InvalidURL):
        return "unknown", None   # connection / timeout — transient
    # feedparser sets `version` (e.g. "rss20", "atom10") for anything it
    # recognizes as a feed — INCLUDING a valid but currently-empty feed. Use
    # it rather than `.entries` so a brand-new / quiet feed isn't misread as a
    # non-feed page and wrongly converted or rejected. Use dict `.get` (not
    # attribute access): an empty body yields a FeedParserDict with no
    # `version` key at all, and `parsed.version` would AttributeError.
    parsed = feedparser.parse(body)
    return ("feed", body) if parsed.get("version") else ("not_feed", body)


__all__ = ["WatchlistTargetError", "validate_and_normalize_target"]
