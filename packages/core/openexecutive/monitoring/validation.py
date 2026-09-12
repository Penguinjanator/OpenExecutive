"""Shared validators for watchlist inputs.

Both the Anthropic chat tools in ``orchestrator/watchlist_tools.py`` and
the HTTP routes in ``api/routes/watchlist.py`` accept user-supplied
slugs and severity strings. Keeping the regex and severity set in one
place means the two surfaces can't drift — a slug the chat tool would
reject must be rejected by the HTTP route too.

``is_valid_mode`` / ``is_valid_cadence`` live in ``monitoring/models``
because they sit next to their string constants; import them from
there.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from openexecutive.alerts.models import AlertSeverity

# Kebab-case ASCII slug. Rejects control characters, RTL overrides,
# whitespace, and anything that could embed Markdown / SQL fragments in
# audit text downstream. Uses ``\Z`` instead of ``$`` so a trailing
# newline doesn't sneak past the validator — ``$`` matches before a
# final \n in Python's default mode, ``\Z`` matches absolute end-of-string.
WATCHLIST_SLUG_RE = re.compile(r"\A[a-z0-9][a-z0-9\-]{0,60}\Z")

VALID_SEVERITY_VALUES: frozenset[str] = frozenset(s.value for s in AlertSeverity)


def is_valid_watchlist_slug(slug: str) -> bool:
    return bool(WATCHLIST_SLUG_RE.match(slug))


def is_valid_severity(value: str) -> bool:
    return value in VALID_SEVERITY_VALUES


def normalize_target(signal_type: str, target: str) -> str:
    """Canonical form of a watch target, so two spellings of one source
    compare equal (the declines memory and the duplicate-source guard key
    on this). Tickers / CIKs upper-case; URLs lower-case scheme + host and
    drop the fragment and a trailing slash. Never raises."""
    raw = (target or "").strip()
    if signal_type in ("stock", "edgar"):
        return raw.upper()
    if "://" not in raw:
        return raw.lower()
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw.lower()
    path = parts.path.rstrip("/") if parts.path not in ("", "/") else ""
    return urlunsplit((
        parts.scheme.lower(), parts.netloc.lower(), path, parts.query, "",
    ))


def registrable_domain(url: str) -> str:
    """Host with a leading ``www.`` stripped (``""`` for non-URL targets).
    Good enough to say "same site" for the duplicate-source guard."""
    if "://" not in (url or ""):
        return ""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


__all__ = [
    "VALID_SEVERITY_VALUES",
    "WATCHLIST_SLUG_RE",
    "is_valid_severity",
    "is_valid_watchlist_slug",
    "normalize_target",
    "registrable_domain",
]
