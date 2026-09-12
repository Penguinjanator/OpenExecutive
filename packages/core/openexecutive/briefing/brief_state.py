"""Delivered-brief state: what the last morning/EoD brief covered.

The principal briefs used to re-list the whole unread queue every day and
call the "activity" block "since last brief" when it was really just the
latest N rows. This module gives each recurring brief kind a memory of what
was last *delivered* so the workflow can:

- bound "what changed" to the window since the previous delivery (`since`),
- split proposals into NEW (created inside that window) vs CARRIED OVER,
- list what the Executive's alert review handled inside the window, and
- skip the model call entirely when the fingerprint of the inputs is
  unchanged, sending a one-line "nothing new" instead.

Storage reuses the ``briefing_narrative`` table (``briefing/narrative_cache``)
under a ``brief:<kind>`` scope: ``input_hash`` holds the fingerprint,
``narrative_text`` the delivered artifact and ``generated_at`` the delivery
time. Scopes are only ever read by exact key, so the namespace cannot
collide with the per-viewer header cache. Only the scheduler records a
delivery (after a successful send), so manual workflow runs never advance
the window.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from openexecutive.alerts.lifecycle import parse_aware
from openexecutive.briefing import narrative_cache

logger = logging.getLogger(__name__)

SCOPE_PREFIX = "brief:"
DEFAULT_WINDOW = timedelta(hours=24)

SUPPRESSED_TEMPLATE = (
    "Nothing new since yesterday's brief — {n} item{s} still waiting on you."
)

# Audit event types the alert review job emits for autonomous moves. The
# brief's "handled overnight" block is built from these (see handled_since).
REVIEW_EVENT_TYPES: tuple[str, ...] = (
    "alert_review_closed",
    "alert_review_routed",
    "alert_review_nudged",
    "alert_review_escalated",
    "alert_review_drafted",
    "alert_review_merged",
    "alert_review_suggested_workflow",
    "alert_review_changed",
)

# Every audit event type the handled block reads, mapped to the short kind
# the brief and the /today rail render. The research watch policy's
# autonomous moves ride alongside the alert review's.
HANDLED_EVENT_KINDS: dict[str, str] = {
    **{t: t.removeprefix("alert_review_") for t in REVIEW_EVENT_TYPES},
    "watchlist_research_added": "watching",
    "watchlist_auto_disabled": "stopped_watching",
}


def scope_for(kind: str) -> str:
    return f"{SCOPE_PREFIX}{kind}"


def last_delivered(kind: str) -> narrative_cache.BriefingNarrative | None:
    """The last delivered brief of this kind, or None on a cold store."""
    try:
        return narrative_cache.get(scope_for(kind))
    except Exception:
        logger.exception("brief_state: read failed for %s", kind)
        return None


def since_for(kind: str, now: datetime | None = None) -> datetime:
    """Start of the "what changed" window: the previous delivery, else 24 h ago.

    Bounded to at most 7 days back so a brief that stopped firing for a while
    does not replay a month of history when it resumes.
    """
    now = now or datetime.now(UTC)
    prev = last_delivered(kind)
    delivered_at = parse_aware(prev.generated_at) if prev else None
    if delivered_at is None:
        return now - DEFAULT_WINDOW
    # Never in the future (clock skew / a scheduler `now` earlier than the
    # recorded delivery would otherwise empty the window and suppress).
    return min(now, max(delivered_at, now - timedelta(days=7)))


def record_delivered(kind: str, fingerprint: str, text: str) -> None:
    """Persist the delivered brief so the next run can diff against it."""
    try:
        narrative_cache.put(narrative_cache.BriefingNarrative(
            scope=scope_for(kind),
            input_hash=fingerprint,
            narrative_text=text,
            generated_at=narrative_cache.utc_now_iso(),
        ))
    except Exception:
        logger.exception("brief_state: write failed for %s", kind)


def split_proposals(
    proposals: list[dict[str, Any]], since: datetime | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(new, carried)``: proposals created at/after ``since`` vs earlier.

    With ``since`` None everything is "new" (the legacy single-list view).
    """
    if since is None:
        return list(proposals), []
    new: list[dict[str, Any]] = []
    carried: list[dict[str, Any]] = []
    for p in proposals:
        created = parse_aware(p.get("created_at"))
        (new if created is None or created >= since else carried).append(p)
    return new, carried


def handled_since(since: datetime, limit: int = 20) -> list[dict[str, Any]]:
    """Autonomous alert-review moves recorded in the audit log since ``since``.

    Each item: ``{"kind": event_type sans prefix, "summary": str, "at": iso,
    "alert_id": int | None}``, newest first. Empty when the audit store is unavailable.
    """
    try:
        from openexecutive.audit.logger import get_audit_logger

        logger_ = get_audit_logger()
        out: list[dict[str, Any]] = []
        for event_type, kind in HANDLED_EVENT_KINDS.items():
            for ev in logger_.query(event_type=event_type, since=since.isoformat(), limit=limit):
                details = ev.details if isinstance(ev.details, dict) else {}
                out.append({
                    "kind": kind,
                    "summary": ev.summary,
                    "at": ev.ts,
                    "alert_id": details.get("alert_id"),
                })
        out.sort(key=lambda e: e["at"], reverse=True)
        return out[:limit]
    except Exception:
        logger.debug("brief_state: handled_since unavailable", exc_info=True)
        return []


def build_brief_fingerprint(
    *,
    today_data: dict[str, Any],
    activity: list[dict[str, Any]],
    handled: list[dict[str, Any]],
    since: datetime | None,
    pending_watch_suggestions: int = 0,
) -> str:
    """Stable hash of everything the brief would say. Deliberately free of
    dates and timestamps so an unchanged day yields the same fingerprint
    tomorrow (activity is keyed by kind + summary, never by its stamp)."""
    new, carried = split_proposals(today_data.get("proposals", []), since)
    payload = {
        "new": sorted(int(p.get("alert_id", 0)) for p in new),
        "carried": sorted(int(p.get("alert_id", 0)) for p in carried),
        "likely_stale": sum(1 for p in carried if p.get("review_verdict") == "likely_stale"),
        "activity": sorted(
            (str(a.get("kind", "")), str(a.get("summary", ""))[:80]) for a in activity
        ),
        "handled": sorted((h["kind"], h["summary"][:80]) for h in handled),
        "depts": sorted(
            (d.get("slug", ""), d.get("at_risk_count", 0), d.get("off_track_count", 0))
            for d in today_data.get("departments", [])
            if d.get("at_risk_count", 0) or d.get("off_track_count", 0)
        ),
        "awaiting": sorted(
            int(p.get("id", 0)) for p in today_data.get("people", []) if p.get("awaiting_count", 0)
        ),
        "watch_suggestions": int(pending_watch_suggestions),
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def suppress_unchanged_enabled() -> bool:
    """`PRINCIPAL_BRIEF_SUPPRESS_UNCHANGED`, defaulting to on when settings
    cannot be built (bare test DB with no env)."""
    try:
        from openexecutive.config import get_settings

        return bool(get_settings().principal_brief_suppress_unchanged)
    except Exception:
        return True


def suppressed_line(n_waiting: int) -> str:
    return SUPPRESSED_TEMPLATE.format(n=n_waiting, s="" if n_waiting == 1 else "s")


def pending_watch_suggestions() -> int:
    """Research watch suggestions awaiting the principal on /watchlist.
    Zero when the monitoring store is unavailable."""
    try:
        from openexecutive.monitoring import store as monitoring_store

        return len(monitoring_store.list_pending_suggestions())
    except Exception:
        logger.debug("brief_state: pending_watch_suggestions unavailable", exc_info=True)
        return 0


__all__ = [
    "HANDLED_EVENT_KINDS",
    "REVIEW_EVENT_TYPES",
    "SUPPRESSED_TEMPLATE",
    "build_brief_fingerprint",
    "handled_since",
    "last_delivered",
    "pending_watch_suggestions",
    "record_delivered",
    "scope_for",
    "since_for",
    "split_proposals",
    "suppress_unchanged_enabled",
    "suppressed_line",
]
