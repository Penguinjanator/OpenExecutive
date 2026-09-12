"""Deterministic policy for what the research pass may put on the watchlist.

The research workflow's watchlist pass used to call ``add_watchlist_entry``
directly: whatever the model proposed landed, live, with no rationale, at
``cadence=15min`` / ``severity_floor=low`` and (for feeds) no keyword
trigger — so one unfiltered competitor blog could out-noise the whole
watchlist. The model now only *proposes* (``propose_watch``); this module
decides, and it decides from company data, not from the model's confidence
in itself.

Two tiers:

- **direct** — added live, quietly (daily cadence, medium floor, keyword
  trigger). Requires the grounding entity to be *in company data* (a named
  competitor / vendor / ticker / initiative / priority / the company itself)
  plus enough corroboration (see :func:`classify`). This is the "high
  likelihood, worth watching — just add it" case.
- **suggest** — inserted in ``dry_run`` (polls, never alerts) with
  ``origin=research_proposed``; the principal approves or declines it on
  ``/watchlist``. This is the "not sure" case.

A proposal with nothing vouching for it (no linked finding, score 0), one
for a source already watched, one past the enabled-watch ceiling or the
per-run budgets, is rejected; a target or entity the principal declined is
refused in the tool handler before the model can route around it with a
new slug. Every decision is audited with its score and reasons.
"""
from __future__ import annotations

import dataclasses
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from openexecutive.alerts.models import AlertSeverity
from openexecutive.audit import log_event as audit_log
from openexecutive.monitoring import store as ms
from openexecutive.monitoring.models import (
    DECLINE_KIND_EXPIRED,
    DECLINE_REASON_EXPIRED,
    DECLINE_REASON_NOT_RELEVANT,
    MODE_ACTIVE,
    MODE_DRY_RUN,
    ORIGIN_RESEARCH,
    ORIGIN_RESEARCH_PROPOSED,
    SOURCE_KIND_EDGAR,
    SOURCE_KIND_PAGE_WATCH,
    SOURCE_KIND_QUERY,
    SOURCE_KIND_RSS,
    SOURCE_KIND_STOCK,
    SOURCE_KIND_VENDOR_STATUS,
    WatchlistItem,
)
from openexecutive.monitoring.research.models import ResearchFinding
from openexecutive.monitoring.sources._http import validate_target_url
from openexecutive.monitoring.validation import normalize_target, registrable_domain

logger = logging.getLogger(__name__)

# Audit event types. The brief's "handled overnight" block renders the first
# two (see briefing.brief_state.HANDLED_EVENT_KINDS).
EVENT_ADDED = "watchlist_research_added"
EVENT_AUTO_DISABLED = "watchlist_auto_disabled"
EVENT_SUGGESTED = "watchlist_research_suggested"
EVENT_REJECTED = "watchlist_research_rejected"
EVENT_SUGGESTION_EXPIRED = "watchlist_suggestion_expired"
EVENT_SUGGESTION_APPROVED = "watchlist_suggestion_approved"
EVENT_SUGGESTION_DECLINED = "watchlist_suggestion_declined"

# Policy outcomes recorded in watchlist_policy_outcomes.
OUTCOME_APPROVED = "approved"
OUTCOME_DECLINED = "declined"
OUTCOME_EXPIRED = "expired"
OUTCOME_AUTO_DISABLED = "auto_disabled"

TIER_DIRECT = "direct"
TIER_SUGGEST = "suggest"
TIER_REJECT = "reject"

# Score needed to add without asking, and the minimum for a suggestion to
# be worth the principal's time at all. See classify() for the ledger.
DIRECT_THRESHOLD = 4
SUGGEST_THRESHOLD = 1
# History only moves the score once the policy has a real sample.
_HISTORY_MIN_SAMPLES = 5

# Hosts that are primary sources in their own right: a watch there is
# "the entity's own source" even though the host isn't the entity's domain.
PRIMARY_SOURCE_HOSTS: frozenset[str] = frozenset({
    "sec.gov", "efts.sec.gov", "data.sec.gov",
    "federalregister.gov", "eur-lex.europa.eu",
})

# Grounding-vocabulary kinds.
KIND_COMPANY = "company"
KIND_COMPETITOR = "competitor"
KIND_VENDOR = "vendor"
KIND_TICKER = "ticker"
KIND_INITIATIVE = "initiative"
KIND_PRIORITY = "priority"
KIND_WATCH = "watch"  # already watched (label/target) — corroborates, never grounds
GROUNDING_KINDS: frozenset[str] = frozenset({
    KIND_COMPANY, KIND_COMPETITOR, KIND_VENDOR, KIND_TICKER, KIND_INITIATIVE, KIND_PRIORITY,
})
# Kinds that name an external ENTITY the company deals with. Only these can
# ground a direct add: an initiative title or a priority sentence is company
# data too, but it is a bag of common words ("growth", "platform") that a
# planted finding can echo, so a watch grounded only in one is a suggestion.
STRONG_GROUNDING_KINDS: frozenset[str] = frozenset({
    KIND_COMPANY, KIND_COMPETITOR, KIND_VENDOR, KIND_TICKER,
})
# When one term appears in several sources the strongest kind wins.
_KIND_RANK: dict[str, int] = {
    KIND_COMPANY: 7, KIND_COMPETITOR: 6, KIND_VENDOR: 5, KIND_TICKER: 4,
    KIND_INITIATIVE: 3, KIND_PRIORITY: 2, KIND_WATCH: 1,
}

# Material-event words every research feed trigger carries, so a competitor
# blog surfaces "we raised / we launched / pricing" and not every post.
_EVENT_KEYWORDS: tuple[str, ...] = (
    "pricing", "launch", "acqui", "funding", "raises", "layoff", "outage",
    "breach", "partnership", "ceo", "lawsuit", "regulat",
)
_STOPWORDS: frozenset[str] = frozenset({
    "the", "and", "for", "with", "our", "into", "from", "that", "this", "year",
    "grow", "growth", "build", "expand", "increase", "improve", "drive", "launch",
    "new", "more", "across", "team", "company", "inc", "corp", "ltd", "llc", "co",
    "group", "labs", "platform", "product", "market", "customer", "customers",
    "revenue", "sales", "enterprise", "global", "digital", "cloud", "data", "api",
    "http", "https", "www", "com", "net", "org", "feed", "blog", "status", "news",
})
# Shortest token that may match on its own inside a multi-word term. Tickers
# and short names still match exactly (whole-term equality is checked first).
_MIN_PARTIAL_TOKEN = 4
_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Auto-add defaults per source kind: (cadence, severity_floor).
_QUIET_DEFAULTS: dict[str, tuple[str, AlertSeverity]] = {
    SOURCE_KIND_STOCK: ("daily", AlertSeverity.MEDIUM),
    SOURCE_KIND_EDGAR: ("daily", AlertSeverity.MEDIUM),
    SOURCE_KIND_RSS: ("daily", AlertSeverity.MEDIUM),
    SOURCE_KIND_VENDOR_STATUS: ("hourly", AlertSeverity.MEDIUM),
    SOURCE_KIND_PAGE_WATCH: ("weekly", AlertSeverity.MEDIUM),
    SOURCE_KIND_QUERY: ("weekly", AlertSeverity.MEDIUM),
}
_STOCK_DEFAULT_PCT = 5

# Deterministic auto-disable thresholds (research-origin rows only).
_DISABLE_MIN_FIRED = 5
_DISABLE_MIN_DISMISSED = 2
_DISABLE_MAX_TRUST = 0.5
# "No signals for N days" is deliberately NOT a retirement rule: page_watch
# and vendor_status emit nothing while the page is unchanged / the vendor
# is healthy, which is exactly a working watch. Only poll failures say a
# source is broken.
_POLL_FAILURES_TO_DISABLE = 3
_POLL_HISTORY_TO_READ = 10
# Nudge the principal only when suggestions have piled up unreviewed.
_NUDGE_MIN_PENDING = 3
_NUDGE_MIN_AGE_DAYS = 7
NUDGE_ALERT_SOURCE = "watchlist_suggestions"


# --------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------- #


@dataclass
class WatchProposal:
    """One validated ``propose_watch`` call, before policy."""

    slug: str
    signal_type: str
    target: str
    normalized_target: str
    rationale: str = ""
    grounding_entity: str = ""
    finding_index: int | None = None
    certainty: str = "unsure"
    config: dict[str, Any] = field(default_factory=dict)
    trigger: dict[str, Any] = field(default_factory=dict)
    route_to_specialist: str = ""
    display_label: str = ""


@dataclass
class PolicySettings:
    max_direct_adds: int = 2
    max_suggestions: int = 2
    max_enabled: int = 40
    suggestion_ttl_days: int = 14

    @classmethod
    def load(cls) -> PolicySettings:
        try:
            from openexecutive.config import get_settings

            s = get_settings()
            return cls(
                max_direct_adds=int(s.watchlist_research_max_direct_adds),
                max_suggestions=int(s.watchlist_research_max_proposals),
                max_enabled=int(s.watchlist_max_enabled),
                suggestion_ttl_days=int(s.watchlist_proposal_ttl_days),
            )
        except Exception:
            logger.debug("watch_policy: settings unavailable — using defaults", exc_info=True)
            return cls()


@dataclass
class PolicyContext:
    """Everything classify() needs, gathered once per run."""

    vocabulary: dict[str, str]  # normalized term -> kind
    priority_terms: list[str]
    existing: list[WatchlistItem]
    outcome_counts: dict[tuple[str, str], dict[str, int]] = field(default_factory=dict)
    settings: PolicySettings = field(default_factory=PolicySettings)
    now: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class Decision:
    tier: str
    score: int
    entity: str
    grounding_kind: str
    reasons: list[str]


# --------------------------------------------------------------------- #
# Grounding vocabulary
# --------------------------------------------------------------------- #


def _norm(term: str) -> str:
    return " ".join(_TOKEN_RE.findall((term or "").lower()))


def grounding_vocabulary(
    profile: Any,
    initiatives: list[Any],
    existing: list[WatchlistItem],
) -> dict[str, str]:
    """Normalized term → kind. Company-data kinds win over ``watch`` when a
    term appears in both (a competitor that is already watched still grounds
    a new watch on a *different* source of theirs)."""
    vocab: dict[str, str] = {}

    def add(term: str, kind: str) -> None:
        n = _norm(term)
        if len(n) < 2:
            return
        if n not in vocab or _KIND_RANK[kind] > _KIND_RANK[vocab[n]]:
            vocab[n] = kind

    for item in existing:
        for label_key in ("display_name", "feed_label", "vendor_label", "label"):
            label = item.config_json.get(label_key) if isinstance(item.config_json, dict) else None
            if label:
                add(str(label), KIND_WATCH)
        # A URL target contributes its site label only ("acme" for
        # https://status.acme.com/feed), never its scheme/host/path tokens.
        host = registrable_domain(item.target)
        add(_site_label(host) if host else item.target, KIND_WATCH)
        add(item.slug.replace("-", " "), KIND_WATCH)
    for i in initiatives:
        add(str(getattr(i, "title", "") or ""), KIND_INITIATIVE)
    if profile is not None:
        for p in list(getattr(getattr(profile, "strategic_priorities", None), "current_year", []) or []):
            add(str(p), KIND_PRIORITY)
        for t in list(getattr(profile, "tickers", []) or []):
            add(str(t), KIND_TICKER)
        for v in list(getattr(profile, "vendors", []) or []):
            add(str(v), KIND_VENDOR)
        for c in list(getattr(getattr(profile, "competitive_landscape", None), "primary_competitors", []) or []):
            add(str(c), KIND_COMPETITOR)
        add(str(getattr(profile, "name", "") or ""), KIND_COMPANY)
    return vocab


def priority_terms(profile: Any) -> list[str]:
    """Distinctive words from the current-year priorities (feed triggers)."""
    out: list[str] = []
    for p in list(getattr(getattr(profile, "strategic_priorities", None), "current_year", []) or []):
        for tok in _TOKEN_RE.findall(str(p).lower()):
            if len(tok) >= 5 and tok not in _STOPWORDS and tok not in out:
                out.append(tok)
    return out[:6]


def _distinctive_tokens(text: str, vocabulary: dict[str, str] | None = None) -> set[str]:
    """Tokens that may carry a partial match: not stopwords, and either long
    enough or a whole vocabulary term in their own right (short names such
    as IBM, AWS, SAP)."""
    vocab = vocabulary or {}
    return {
        t for t in text.split()
        if t not in _STOPWORDS and (len(t) >= _MIN_PARTIAL_TOKEN or t in vocab)
    }


def match_entity(entity: str, vocabulary: dict[str, str]) -> tuple[str, str] | None:
    """``(term, kind)`` for the vocabulary term the entity names, else None.

    Token containment both ways, so "Acme Corp" matches "Acme" and "Acme"
    matches "Acme Corp"; single-token matches must be a whole token."""
    n = _norm(entity)
    if len(n) < 2:
        return None
    exact = vocabulary.get(n)
    if exact is not None and exact != KIND_WATCH:
        return n, exact
    # Partial matching only on distinctive tokens: no stopwords, nothing
    # shorter than _MIN_PARTIAL_TOKEN unless the token is itself a whole
    # vocabulary term ("IBM Corp" still matches competitor "IBM"), so
    # "Growth Inc" cannot match the priority "drive growth in EMEA" and
    # "corp" matches nothing.
    tokens = _distinctive_tokens(n, vocabulary)
    # An exact hit on a watch label is only the fallback: a company-data term
    # that contains (or is contained by) the entity still wins.
    best: tuple[str, str] | None = (n, exact) if exact is not None else None
    if not tokens:
        return best
    for term, kind in vocabulary.items():
        term_tokens = _distinctive_tokens(term, vocabulary)
        if not term_tokens or not (term_tokens <= tokens or tokens <= term_tokens):
            continue
        # Prefer a company-data kind over a watch label, then the longer term.
        if best is None or (best[1] == KIND_WATCH and kind != KIND_WATCH) or (
            best[1] == kind and len(term) > len(best[0])
        ):
            best = (term, kind)
    return best


def entity_declined(entity: str, declines: list[Any]) -> bool:
    """True when the principal declined this entity as *not relevant* (the
    reason that blacklists the company/topic, not just one source)."""
    n = _norm(entity)
    if len(n) < 2:
        return False
    tokens = {t for t in n.split() if t not in _STOPWORDS and len(t) >= _MIN_PARTIAL_TOKEN}
    for d in declines:
        if getattr(d, "reason", "") != DECLINE_REASON_NOT_RELEVANT:
            continue
        dn = _norm(str(getattr(d, "entity", "") or ""))
        if len(dn) < 2:
            continue
        if dn == n:
            return True
        d_tokens = {t for t in dn.split() if t not in _STOPWORDS and len(t) >= _MIN_PARTIAL_TOKEN}
        if tokens and d_tokens and (d_tokens <= tokens or tokens <= d_tokens):
            return True
    return False


# --------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------- #


def _is_own_source(proposal: WatchProposal, entity_term: str, vocabulary: dict[str, str]) -> bool:
    """The target is the entity's own primary source: its ticker for
    stock/edgar, its own domain for a URL, or an allowlisted primary host."""
    if proposal.signal_type in (SOURCE_KIND_STOCK, SOURCE_KIND_EDGAR):
        # Only when the grounding entity IS this ticker. A ticker the profile
        # tracks for someone else (MSFT) says nothing about "Acme Corp"; the
        # model grounds a ticker watch with the ticker itself.
        return _norm(proposal.target) == entity_term
    host = registrable_domain(proposal.target)
    if not host:
        return False
    if host in PRIMARY_SOURCE_HOSTS or any(host.endswith("." + h) for h in PRIMARY_SOURCE_HOSTS):
        return True
    # Only the site's own label counts ("acme" in status.acme.com / acme.co.uk);
    # subdomain labels and the TLD never do, so "api" or ".cloud" cannot
    # make an unrelated host look like the entity's.
    entity_tokens = {
        t for t in entity_term.split() if len(t) >= _MIN_PARTIAL_TOKEN and t not in _STOPWORDS
    }
    return _site_label(host) in entity_tokens


# Second-level labels under which the real site label sits one step deeper
# (acme.co.uk → "acme").
_PUBLIC_SECOND_LEVEL: frozenset[str] = frozenset({
    "co", "com", "org", "net", "gov", "ac", "edu", "or", "ne", "gob",
})


def _site_label(host: str) -> str:
    """The label that names the site: "acme" for acme.com, status.acme.com
    and acme.co.uk. Empty for a bare TLD / IP-like host."""
    labels = [label for label in host.split(".") if label]
    if len(labels) < 2:
        return ""
    if len(labels) >= 3 and labels[-2] in _PUBLIC_SECOND_LEVEL and len(labels[-1]) == 2:
        return labels[-3]
    return labels[-2]


def _finding_supports(proposal: WatchProposal, finding: ResearchFinding, entity_term: str) -> bool:
    """Does the cited finding actually concern this target? A URL target
    must share its site with one of the finding's cited URLs; a ticker or
    query target must appear in the finding's text or be about the entity
    the finding names."""
    text = _norm(f"{finding.title} {finding.summary}")
    text_tokens = set(text.split())
    entity_tokens = {
        t for t in entity_term.split() if len(t) >= _MIN_PARTIAL_TOKEN and t not in _STOPWORDS
    }
    names_entity = bool(entity_term) and (entity_term in text or bool(entity_tokens & text_tokens))
    if proposal.signal_type in (SOURCE_KIND_STOCK, SOURCE_KIND_EDGAR):
        return _norm(proposal.target) in text_tokens or names_entity
    if proposal.signal_type == SOURCE_KIND_QUERY:
        return names_entity
    # A URL target must share its site with a cited URL: a finding that
    # merely names the entity cannot vouch for an arbitrary page about it.
    host = registrable_domain(proposal.target)
    cited = {registrable_domain(u) for u in finding.relevant_urls}
    cited.discard("")
    return bool(host) and any(
        host == c or host.endswith("." + c) or c.endswith("." + host) for c in cited
    )


def _history_adjustment(
    signal_type: str, grounding_kind: str, counts: dict[tuple[str, str], dict[str, int]],
) -> tuple[int, str]:
    tally = counts.get((signal_type, grounding_kind)) or {}
    good = tally.get(OUTCOME_APPROVED, 0)
    bad = tally.get(OUTCOME_DECLINED, 0) + tally.get(OUTCOME_AUTO_DISABLED, 0)
    n = good + bad
    if n < _HISTORY_MIN_SAMPLES:
        return 0, ""
    rate = good / n
    if rate >= 0.7:
        return 1, f"history {good}/{n} approved"
    if rate <= 0.3:
        return -1, f"history {good}/{n} approved"
    return 0, ""


def _same_source_already_watched(proposal: WatchProposal, existing: list[WatchlistItem]) -> bool:
    host = registrable_domain(proposal.target)
    for item in existing:
        # Disabled rows count: a source the sweep retired as noisy or dead
        # must not come back under a new slug with reset counters.
        if normalize_target(item.signal_type, item.target) == proposal.normalized_target:
            return True
        if host and item.signal_type == proposal.signal_type and registrable_domain(item.target) == host:
            return True
    return False


def classify(
    proposal: WatchProposal, finding: ResearchFinding | None, ctx: PolicyContext,
) -> Decision:
    """Score a proposal. The ledger:

    +2 grounding entity names something in company data (mandatory for direct)
    +1 entity is already watched under another source (corroboration)
    +1 target is the entity's own source (its ticker / its domain / a primary host)
    +1 finding confidence is high
    +1 finding verification is 'confirmed'
    +1 cross-specialist consensus on the finding
    ±1 policy history for (signal_type, grounding kind) at >=5 samples

    Finding points count only when the cited finding concerns this source
    (shares its site with a cited URL, or names the entity). Direct at
    >= DIRECT_THRESHOLD with the mandatory entity match, only for the
    entity's OWN source, and only when the entity is a named competitor /
    vendor / ticker / the company (STRONG_GROUNDING_KINDS); the model's own
    ``certainty`` can only downgrade. ``query`` is never direct.
    """
    reasons: list[str] = []
    score = 0
    match = match_entity(proposal.grounding_entity, ctx.vocabulary)
    entity_term, kind = (match if match else ("", ""))
    grounded = kind in GROUNDING_KINDS
    if grounded:
        score += 2
        reasons.append(f"entity '{proposal.grounding_entity}' is a company {kind}")
    elif kind == KIND_WATCH:
        score += 1
        reasons.append(f"entity '{proposal.grounding_entity}' matches an existing watch")
    else:
        reasons.append(f"entity '{proposal.grounding_entity}' is not in company data")

    own_source = bool(entity_term) and _is_own_source(proposal, entity_term, ctx.vocabulary)
    if own_source:
        score += 1
        reasons.append("target is the entity's own source")
    supported = finding is not None and _finding_supports(proposal, finding, entity_term)
    if finding is not None and not supported:
        # The cited finding says nothing about THIS source, so it lends it
        # no credit — the model cannot borrow a strong finding's points for
        # an unrelated URL. (Company-data grounding still counts.)
        reasons.append("cited finding does not mention this source")
    if finding is not None and supported:
        if finding.confidence == "high":
            score += 1
            reasons.append("finding confidence high")
        if finding.verification == "confirmed":
            score += 1
            reasons.append("source verified")
        if "," in (finding.source_specialist or ""):
            score += 1
            reasons.append("cross-specialist consensus")
    adj, why = _history_adjustment(proposal.signal_type, kind, ctx.outcome_counts)
    if adj:
        score += adj
        reasons.append(why)

    if _same_source_already_watched(proposal, ctx.existing):
        reasons.append("same source already watched")
        return Decision(TIER_REJECT, score, entity_term, kind, reasons)

    enabled_count = sum(1 for i in ctx.existing if i.enabled)
    if enabled_count >= ctx.settings.max_enabled:
        # Suggestions poll too, so the ceiling has to stop them as well or
        # it would latch: every run would add polling rows and no proposal
        # could ever be direct again.
        reasons.append(f"watchlist at its ceiling ({enabled_count} enabled)")
        return Decision(TIER_REJECT, score, entity_term, kind, reasons)
    if finding is None or score < SUGGEST_THRESHOLD:
        # Nothing vouches for it: no linked finding, or neither company data
        # nor evidence scored a point. Not worth the principal's time.
        reasons.append("no evidence to put in front of the principal")
        return Decision(TIER_REJECT, score, entity_term, kind, reasons)

    tier = TIER_SUGGEST
    if grounded and score >= DIRECT_THRESHOLD:
        tier = TIER_DIRECT
    if tier == TIER_DIRECT and proposal.signal_type == SOURCE_KIND_QUERY:
        tier = TIER_SUGGEST
        reasons.append("standing web queries are always suggestions")
    if tier == TIER_DIRECT and not own_source:
        # Adding on its own is reserved for the entity's OWN source (its
        # ticker, its site, its status page); a third-party page about a
        # competitor, however well corroborated, is the principal's call.
        tier = TIER_SUGGEST
        reasons.append("not the entity's own source")
    if tier == TIER_DIRECT and kind not in STRONG_GROUNDING_KINDS:
        tier = TIER_SUGGEST
        reasons.append(f"grounded only in a {kind} — needs a named competitor, vendor or ticker")
    if tier == TIER_DIRECT and proposal.certainty != "confident":
        tier = TIER_SUGGEST
        reasons.append("model marked it unsure")
    return Decision(tier, score, entity_term, kind, reasons)


# --------------------------------------------------------------------- #
# Quiet defaults
# --------------------------------------------------------------------- #


def quiet_defaults(
    proposal: WatchProposal, priority_words: list[str],
) -> tuple[str, AlertSeverity, dict[str, Any]]:
    """``(cadence, severity_floor, trigger)`` a research row lands with.

    The model's cadence/floor are ignored: a research watch must be quiet
    by construction and the principal can loosen it on /watchlist."""
    cadence, floor = _QUIET_DEFAULTS.get(proposal.signal_type, ("daily", AlertSeverity.MEDIUM))
    trigger: dict[str, Any] = dict(proposal.trigger or {})
    if proposal.signal_type == SOURCE_KIND_STOCK:
        if not isinstance(trigger.get("abs_change_pct_gte"), (int, float)):
            trigger["abs_change_pct_gte"] = _STOCK_DEFAULT_PCT
    elif proposal.signal_type in (SOURCE_KIND_RSS, SOURCE_KIND_QUERY):
        # Material-event words + priority terms only, and only for sources
        # with prose to match (feed titles, search snippets). A page_watch
        # summary is "[label] page changed — <diff>" — a keyword filter
        # there would mean the watch could never fire. The entity's own
        # name must NOT be a keyword either: feed summaries carry the feed
        # label, so it would match every entry.
        existing_kw = trigger.get("keywords")
        keywords: list[str] = [str(k) for k in existing_kw] if isinstance(existing_kw, list) else []
        for kw in list(_EVENT_KEYWORDS) + priority_words:
            if kw not in keywords:
                keywords.append(kw)
        trigger["keywords"] = keywords[:20]
    return cadence, floor, trigger


# --------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------- #


def _safe_source_url(finding: ResearchFinding | None) -> str:
    """First cited URL that is a public http(s) URL — it is rendered as a
    link beside the Approve button, so anything else is dropped."""
    for url in (finding.relevant_urls if finding else []):
        candidate = str(url or "").strip()
        if not candidate.lower().startswith(("http://", "https://")):
            continue
        ok, _reason = validate_target_url(candidate)
        if ok:
            return candidate[:500]
    return ""


def _policy_stamp(decision: Decision, proposal: WatchProposal, finding: ResearchFinding | None) -> dict[str, Any]:
    return {
        # The matched company-data term, else the model's own entity string,
        # so a not_relevant decline on an ungrounded suggestion still
        # blacklists the entity the model named.
        "entity": decision.entity or _norm(proposal.grounding_entity),
        "grounding_kind": decision.grounding_kind,
        "specialist": (finding.source_specialist if finding else ""),
        "score": decision.score,
        "reasons": decision.reasons[:6],
        "source_url": _safe_source_url(finding),
    }


def policy_stamp_of(item: WatchlistItem) -> dict[str, Any]:
    stamp = item.config_json.get("_policy") if isinstance(item.config_json, dict) else None
    return dict(stamp) if isinstance(stamp, dict) else {}


def apply_proposals(
    proposals: list[WatchProposal],
    findings: list[ResearchFinding],
    ctx: PolicyContext,
    *,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Classify and persist. Returns tool-call-shaped summaries
    (``tool='propose_watch'``, plus ``outcome`` = added | suggested | rejected)
    so the run's artifact and audit accounting stay uniform."""
    summaries: list[dict[str, Any]] = []
    direct_left = max(0, ctx.settings.max_direct_adds)
    suggest_left = max(0, ctx.settings.max_suggestions)
    seen_targets: set[str] = set()
    # Rows inserted earlier in this loop must count as "already watched" for
    # later proposals; work on a copy so the caller's context is untouched.
    live_existing = list(ctx.existing)
    ctx = dataclasses.replace(ctx, existing=live_existing)

    for proposal in proposals:
        finding = (
            findings[proposal.finding_index]
            if proposal.finding_index is not None and 0 <= proposal.finding_index < len(findings)
            else None
        )
        if proposal.normalized_target in seen_targets:
            summaries.append(_summary(proposal, "rejected", "duplicate target in this run"))
            continue
        seen_targets.add(proposal.normalized_target)

        decision = classify(proposal, finding, ctx)
        if decision.tier == TIER_DIRECT and direct_left <= 0:
            decision.tier = TIER_SUGGEST
            decision.reasons.append("direct-add budget spent this run")
        if decision.tier == TIER_SUGGEST and suggest_left <= 0:
            decision.tier = TIER_REJECT
            decision.reasons.append("suggestion budget spent this run")

        if decision.tier == TIER_REJECT:
            audit_log(
                EVENT_REJECTED,
                f"Research watch rejected: {proposal.slug} — {'; '.join(decision.reasons[-2:])}",
                actor="executive",
                details={"slug": proposal.slug, "target": proposal.target,
                         "signal_type": proposal.signal_type, **_policy_stamp(decision, proposal, finding)},
            )
            summaries.append(_summary(proposal, "rejected", decision.reasons[-1] if decision.reasons else ""))
            continue

        cadence, floor, trigger = quiet_defaults(proposal, ctx.priority_terms)
        config = dict(proposal.config)
        config["_policy"] = _policy_stamp(decision, proposal, finding)
        is_direct = decision.tier == TIER_DIRECT
        try:
            new_id = ms.insert_watchlist_item(
                slug=proposal.slug,
                signal_type=proposal.signal_type,
                target=proposal.target,
                config=config,
                trigger=trigger,
                cadence=cadence,
                severity_floor=floor,
                severity_ceiling=AlertSeverity.URGENT,
                route_to_specialist=proposal.route_to_specialist,
                mode=MODE_ACTIVE if is_direct else MODE_DRY_RUN,
                notes=proposal.rationale[:500],
                origin=ORIGIN_RESEARCH if is_direct else ORIGIN_RESEARCH_PROPOSED,
                db_path=db_path,
            )
        except Exception:
            logger.exception("watch_policy: insert failed for %s", proposal.slug)
            summaries.append(_summary(proposal, "rejected", "insert failed (see server log)"))
            continue

        inserted = ms.get_watchlist_item(new_id, db_path=db_path)
        if inserted is not None:
            live_existing.append(inserted)
        details = {
            "watchlist_id": new_id, "slug": proposal.slug, "target": proposal.target,
            "signal_type": proposal.signal_type, "rationale": proposal.rationale[:240],
            "cadence": cadence, "severity_floor": floor.value, "trigger": trigger,
            **_policy_stamp(decision, proposal, finding),
        }
        if is_direct:
            direct_left -= 1
            audit_log(
                EVENT_ADDED,
                f"Started watching {proposal.slug} — {proposal.rationale[:120] or decision.entity}",
                actor="executive", details=details,
            )
            summaries.append(_summary(
                proposal, "added",
                f"added (score {decision.score}; {decision.reasons[0] if decision.reasons else ''})",
            ))
        else:
            suggest_left -= 1
            audit_log(
                EVENT_SUGGESTED,
                f"Suggested watching {proposal.slug} — {'; '.join(decision.reasons[-2:])}",
                actor="executive", details=details,
            )
            summaries.append(_summary(
                proposal, "suggested",
                f"suggested for approval (score {decision.score}; {decision.reasons[-1] if decision.reasons else ''})",
            ))
    return summaries


def _summary(proposal: WatchProposal, outcome: str, detail: str) -> dict[str, Any]:
    return {
        "tool": "propose_watch",
        "input_preview": f"{proposal.slug} [{proposal.signal_type}] {proposal.target}"[:120],
        "result_preview": f"{proposal.slug}: {detail}"[:160],
        "ok": outcome != "rejected",
        "outcome": outcome,
        "slug": proposal.slug,
    }


# --------------------------------------------------------------------- #
# Approve / decline (called by the API routes)
# --------------------------------------------------------------------- #


def record_outcome_for(item: WatchlistItem, outcome: str, *, db_path: Path | None = None) -> None:
    stamp = policy_stamp_of(item)
    try:
        ms.record_policy_outcome(
            signal_type=item.signal_type,
            grounding_kind=str(stamp.get("grounding_kind", "")),
            specialist=str(stamp.get("specialist", "")),
            outcome=outcome,
            db_path=db_path,
        )
    except Exception:
        logger.debug("watch_policy: outcome record failed", exc_info=True)


# --------------------------------------------------------------------- #
# Sweep (scheduler, every 15 min alongside the alert expiry sweep)
# --------------------------------------------------------------------- #


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _consecutive_poll_failures(slug: str) -> int:
    """How many of the most recent polls of ``slug`` failed, counting back
    from the newest until one succeeded."""
    try:
        from openexecutive.audit.logger import get_audit_logger

        rows = get_audit_logger().query(
            event_type="external_monitor_poll", q=f"Polled {slug} (", limit=_POLL_HISTORY_TO_READ,
        )
    except Exception:
        return 0
    n = 0
    for ev in rows:
        details = ev.details if isinstance(ev.details, dict) else {}
        if details.get("watchlist_slug") not in (None, slug):
            continue
        if not details.get("failed"):
            break
        n += 1
    return n


def _auto_disable_reason(item: WatchlistItem) -> str:
    if (
        item.fired_count >= _DISABLE_MIN_FIRED
        and item.dismiss_count >= _DISABLE_MIN_DISMISSED
        and item.trust_score <= _DISABLE_MAX_TRUST
    ):
        return f"dismissed {item.dismiss_count} of {item.fired_count} alerts (trust {item.trust_score:.2f})"
    if _consecutive_poll_failures(item.slug) >= _POLL_FAILURES_TO_DISABLE:
        return f"{_POLL_FAILURES_TO_DISABLE} consecutive poll failures"
    return ""


def sweep(now: datetime | None = None, *, db_path: Path | None = None) -> dict[str, int]:
    """Expire stale suggestions, auto-disable research watches that proved
    noisy or dead, and nudge the principal when suggestions pile up. Never
    raises. Returns counts for logging."""
    now = now or datetime.now(UTC)
    counts = {"expired": 0, "disabled": 0, "nudged": 0}
    settings = PolicySettings.load()
    try:
        counts["expired"] = _expire_suggestions(now, settings, db_path)
    except Exception:
        logger.exception("watch_policy.sweep: expire failed")
    try:
        counts["disabled"] = _auto_disable(now, db_path)
    except Exception:
        logger.exception("watch_policy.sweep: auto-disable failed")
    try:
        counts["nudged"] = _nudge_if_piled_up(now, db_path)
    except Exception:
        logger.exception("watch_policy.sweep: nudge failed")
    return counts


def _expire_suggestions(now: datetime, settings: PolicySettings, db_path: Path | None) -> int:
    ttl = timedelta(days=max(1, settings.suggestion_ttl_days))
    n = 0
    for item in ms.list_pending_suggestions(db_path=db_path):
        created = _parse(item.created_at)
        if created is None or now - created < ttl or item.id is None:
            continue
        # Compare-and-delete: a principal approving this very row a moment
        # ago must win over the sweep.
        if not ms.delete_pending_suggestion(item.id, db_path=db_path):
            continue
        ms.insert_decline(
            normalized_target=normalize_target(item.signal_type, item.target),
            kind=DECLINE_KIND_EXPIRED, reason=DECLINE_REASON_EXPIRED,
            entity=str(policy_stamp_of(item).get("entity", "")),
            signal_type=item.signal_type, slug=item.slug, db_path=db_path,
        )
        record_outcome_for(item, OUTCOME_EXPIRED, db_path=db_path)
        audit_log(
            EVENT_SUGGESTION_EXPIRED,
            f"Watch suggestion {item.slug} expired unreviewed after {settings.suggestion_ttl_days}d",
            actor="scheduler",
            details={"slug": item.slug, "target": item.target, "signal_type": item.signal_type},
        )
        n += 1
    return n


def _auto_disable(now: datetime, db_path: Path | None) -> int:
    n = 0
    for item in ms.list_watchlist(enabled_only=True, db_path=db_path):
        if item.origin != ORIGIN_RESEARCH or item.mode != MODE_ACTIVE or item.id is None:
            continue
        reason = _auto_disable_reason(item)
        if not reason:
            continue
        ms.set_enabled(item.id, False, db_path=db_path)
        record_outcome_for(item, OUTCOME_AUTO_DISABLED, db_path=db_path)
        # Remember the target (retryable after 90 days, like an unreviewed
        # expiry) so the next research run cannot re-add the same source
        # under a new slug with fresh counters.
        ms.insert_decline(
            normalized_target=normalize_target(item.signal_type, item.target),
            kind=DECLINE_KIND_EXPIRED, reason=reason[:120],
            entity=str(policy_stamp_of(item).get("entity", "")),
            signal_type=item.signal_type, slug=item.slug, db_path=db_path,
        )
        audit_log(
            EVENT_AUTO_DISABLED,
            f"Stopped watching {item.slug} — {reason}",
            actor="scheduler",
            details={"watchlist_id": item.id, "slug": item.slug, "target": item.target,
                     "signal_type": item.signal_type, "reason": reason},
        )
        n += 1
    return n


def _nudge_if_piled_up(now: datetime, db_path: Path | None) -> int:
    pending = ms.list_pending_suggestions(db_path=db_path)
    if len(pending) < _NUDGE_MIN_PENDING:
        return 0
    created = _parse(pending[0].created_at)
    if created is None or now - created < timedelta(days=_NUDGE_MIN_AGE_DAYS):
        return 0
    from openexecutive.alerts.store import coalesce_alert, insert_alert

    # One card for the whole pile: an open card is refreshed in place
    # (coalesce), and a card the principal already dismissed is not
    # re-minted until next week — acting on one suggestion must never
    # spawn another nudge.
    dedup_key = f"{NUDGE_ALERT_SOURCE}:pending"
    year, week, _ = now.isocalendar()
    external_id = f"{dedup_key}:{year}-W{week:02d}"
    lines = [f"- {i.slug} ({i.signal_type}) — {i.notes or i.target}"[:200] for i in pending[:10]]
    body = (
        "The Executive suggested these sources to monitor and is not sure "
        "enough to add them on its own. Approve or decline them on the "
        "Watch list page.\n\n" + "\n".join(lines)
    )
    if coalesce_alert(
        source=NUDGE_ALERT_SOURCE, dedup_key=dedup_key, severity="medium", body=body, db_path=db_path,
    ):
        return 0
    alert_id = insert_alert(
        source=NUDGE_ALERT_SOURCE,
        external_id=external_id,
        severity="medium",
        headline=f"{len(pending)} watch suggestions are waiting for you",
        body=body,
        suggested_action="Review the suggestions on /watchlist",
        topic_tags=["watchlist"],
        dedup_key=dedup_key,
        db_path=db_path,
    )
    return 1 if alert_id else 0


__all__ = [
    "DIRECT_THRESHOLD",
    "SUGGEST_THRESHOLD",
    "EVENT_ADDED",
    "EVENT_AUTO_DISABLED",
    "EVENT_REJECTED",
    "EVENT_SUGGESTED",
    "EVENT_SUGGESTION_APPROVED",
    "EVENT_SUGGESTION_DECLINED",
    "EVENT_SUGGESTION_EXPIRED",
    "OUTCOME_APPROVED",
    "OUTCOME_AUTO_DISABLED",
    "OUTCOME_DECLINED",
    "OUTCOME_EXPIRED",
    "TIER_DIRECT",
    "TIER_REJECT",
    "TIER_SUGGEST",
    "STRONG_GROUNDING_KINDS",
    "Decision",
    "PolicyContext",
    "PolicySettings",
    "WatchProposal",
    "apply_proposals",
    "classify",
    "entity_declined",
    "grounding_vocabulary",
    "match_entity",
    "policy_stamp_of",
    "priority_terms",
    "quiet_defaults",
    "record_outcome_for",
    "sweep",
]
