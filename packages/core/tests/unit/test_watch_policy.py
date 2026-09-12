"""The research watchlist policy: grounded proposals go straight in, uncertain
ones become suggestions, declines are remembered, research watches retire."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.alerts.store import initialize_db as initialize_alerts_db
from openexecutive.audit import AuditLogger, set_audit_logger
from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.memory.episodic import initialize_db as initialize_episodic_db
from openexecutive.monitoring import store as ms
from openexecutive.monitoring.models import (
    DECLINE_KIND_EXPIRED,
    DECLINE_KIND_EXPLICIT,
    MODE_ACTIVE,
    MODE_DRY_RUN,
    ORIGIN_RESEARCH,
    ORIGIN_RESEARCH_PROPOSED,
)
from openexecutive.monitoring.research import watch_policy as wp
from openexecutive.monitoring.research.models import ResearchFinding
from openexecutive.monitoring.validation import normalize_target, registrable_domain


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "policy.db"
    monkeypatch.setattr("openexecutive.memory.episodic.DB_PATH", db_path)
    monkeypatch.setattr("openexecutive.alerts.store.DB_PATH", db_path)
    initialize_episodic_db(db_path)
    initialize_alerts_db(db_path)
    ms.initialize_db(db_path)
    audit = AuditLogger(db_path=db_path)
    audit.initialize_db()
    set_audit_logger(audit)
    return db_path


def _profile() -> CompanyProfile:
    return CompanyProfile.model_validate({
        "name": "Sente Labs",
        "competitive_landscape": {"primary_competitors": ["Acme Corp", "Globex"]},
        "strategic_priorities": {"current_year": ["Expand into EU enterprise accounts"]},
        "vendors": ["Stripe"],
        "tickers": ["ACME"],
    })


def _finding(**kw: Any) -> ResearchFinding:
    base: dict[str, Any] = {
        "title": "Acme cut prices",
        "summary": "Acme Corp cut list prices 20% on 2026-09-01.",
        "confidence": "high",
        "relevant_urls": ["https://www.acme.com/blog/pricing"],
        "source_specialist": "cso",
    }
    base.update(kw)
    return ResearchFinding(**base)


def _proposal(**kw: Any) -> wp.WatchProposal:
    base: dict[str, Any] = {
        "slug": "rss-acme-blog", "signal_type": "rss",
        "target": "https://www.acme.com/blog/feed.xml",
        "grounding_entity": "Acme Corp", "rationale": "Competitor pricing moves",
        "finding_index": 0, "certainty": "confident",
    }
    base.update(kw)
    base["normalized_target"] = normalize_target(base["signal_type"], base["target"])
    return wp.WatchProposal(**base)


def _ctx(existing: list[Any] | None = None, **kw: Any) -> wp.PolicyContext:
    profile = _profile()
    initiatives = [SimpleNamespace(title="Launch Helios API", status="active")]
    existing = existing or []
    return wp.PolicyContext(
        vocabulary=wp.grounding_vocabulary(profile, initiatives, existing),
        priority_terms=wp.priority_terms(profile),
        existing=existing,
        **kw,
    )


# --------------------------------------------------------------------- #
# Vocabulary + matching
# --------------------------------------------------------------------- #


def test_vocabulary_covers_profile_initiatives_and_watch_labels(db: Path) -> None:
    ms.insert_watchlist_item(
        slug="stock-hubs", signal_type="stock", target="HUBS",
        config={"display_name": "HubSpot"}, db_path=db,
    )
    vocab = wp.grounding_vocabulary(
        _profile(), [SimpleNamespace(title="Launch Helios API")], ms.list_watchlist(db_path=db),
    )
    assert vocab["sente labs"] == wp.KIND_COMPANY
    assert vocab["acme corp"] == wp.KIND_COMPETITOR
    assert vocab["stripe"] == wp.KIND_VENDOR
    assert vocab["acme"] == wp.KIND_TICKER
    assert vocab["launch helios api"] == wp.KIND_INITIATIVE
    assert vocab["hubspot"] == wp.KIND_WATCH and vocab["hubs"] == wp.KIND_WATCH


def test_match_entity_is_token_based_and_prefers_company_data() -> None:
    vocab = {"acme corp": wp.KIND_COMPETITOR, "acme": wp.KIND_WATCH, "stripe": wp.KIND_VENDOR}
    assert wp.match_entity("Acme", vocab) == ("acme corp", wp.KIND_COMPETITOR)
    assert wp.match_entity("ACME CORP.", vocab) == ("acme corp", wp.KIND_COMPETITOR)
    assert wp.match_entity("Stripe Inc", vocab) == ("stripe", wp.KIND_VENDOR)
    assert wp.match_entity("Initech", vocab) is None
    assert wp.match_entity("", vocab) is None


def test_priority_terms_drop_stopwords() -> None:
    assert wp.priority_terms(_profile()) == ["enterprise", "accounts"]


def test_normalize_target_and_domain() -> None:
    assert normalize_target("stock", " acme ") == "ACME"
    assert normalize_target("rss", "HTTPS://www.Acme.com/feed/#top") == "https://www.acme.com/feed"
    assert normalize_target("rss", "https://acme.com/") == "https://acme.com"
    assert registrable_domain("https://www.acme.com/blog") == "acme.com"
    assert registrable_domain("ACME") == ""


# --------------------------------------------------------------------- #
# Classification ledger
# --------------------------------------------------------------------- #


def test_grounded_own_source_high_confidence_is_direct() -> None:
    d = wp.classify(_proposal(), _finding(), _ctx())
    # +2 competitor, +1 own domain, +1 high confidence = 4
    assert d.tier == wp.TIER_DIRECT and d.score == 4
    assert d.grounding_kind == wp.KIND_COMPETITOR


def test_ticker_in_profile_is_own_source_for_stock() -> None:
    p = _proposal(slug="stock-acme", signal_type="stock", target="ACME", grounding_entity="Acme Corp")
    d = wp.classify(p, _finding(), _ctx())
    assert d.tier == wp.TIER_DIRECT


def test_vendor_status_page_is_direct() -> None:
    p = _proposal(
        slug="vendor-stripe", signal_type="vendor_status",
        target="https://status.stripe.com/feed.atom", grounding_entity="Stripe",
    )
    d = wp.classify(p, _finding(), _ctx())
    assert d.tier == wp.TIER_DIRECT and "own source" in " ".join(d.reasons)


def test_entity_not_in_company_data_is_never_direct() -> None:
    p = _proposal(grounding_entity="Initech", target="https://initech.com/feed.xml")
    d = wp.classify(p, _finding(verification="confirmed", source_specialist="cso,cfo"), _ctx())
    assert d.tier == wp.TIER_SUGGEST
    assert d.score == 3  # own-source +1 needs an entity; high +1, verified +1, consensus +1


def test_medium_confidence_grounded_needs_more_corroboration() -> None:
    d = wp.classify(_proposal(), _finding(confidence="medium"), _ctx())
    assert d.tier == wp.TIER_SUGGEST and d.score == 3
    d2 = wp.classify(_proposal(), _finding(confidence="medium", verification="confirmed"), _ctx())
    assert d2.tier == wp.TIER_DIRECT and d2.score == 4


def test_unsure_only_downgrades() -> None:
    d = wp.classify(_proposal(certainty="unsure"), _finding(), _ctx())
    assert d.tier == wp.TIER_SUGGEST and "unsure" in d.reasons[-1]
    # 'confident' cannot lift an ungrounded proposal.
    d2 = wp.classify(_proposal(grounding_entity="Initech", certainty="confident"), _finding(), _ctx())
    assert d2.tier == wp.TIER_SUGGEST


def test_query_is_always_a_suggestion() -> None:
    p = _proposal(slug="q-acme", signal_type="query", target="Acme Corp pricing", grounding_entity="Acme Corp")
    d = wp.classify(p, _finding(verification="confirmed"), _ctx())
    assert d.tier == wp.TIER_SUGGEST and "standing web queries" in d.reasons[-1]


def test_same_site_already_watched_is_rejected(db: Path) -> None:
    ms.insert_watchlist_item(
        slug="rss-acme-news", signal_type="rss", target="https://acme.com/news/feed", db_path=db,
    )
    existing = ms.list_watchlist(db_path=db)
    d = wp.classify(_proposal(), _finding(), _ctx(existing))
    assert d.tier == wp.TIER_REJECT and "already watched" in d.reasons[-1]


def test_ceiling_turns_direct_into_suggestion(db: Path) -> None:
    for i in range(3):
        ms.insert_watchlist_item(slug=f"stock-x{i}", signal_type="stock", target=f"X{i}", db_path=db)
    ctx = _ctx(ms.list_watchlist(db_path=db), settings=wp.PolicySettings(max_enabled=3))
    d = wp.classify(_proposal(), _finding(), ctx)
    assert d.tier == wp.TIER_SUGGEST and "ceiling" in d.reasons[-1]


def test_history_adjusts_only_with_enough_samples() -> None:
    counts = {("rss", wp.KIND_COMPETITOR): {"approved": 1, "declined": 4}}
    d = wp.classify(_proposal(), _finding(), _ctx(outcome_counts=counts))
    assert d.score == 3 and d.tier == wp.TIER_SUGGEST  # −1 from history
    few = {("rss", wp.KIND_COMPETITOR): {"approved": 0, "declined": 3}}
    assert wp.classify(_proposal(), _finding(), _ctx(outcome_counts=few)).score == 4


# --------------------------------------------------------------------- #
# Quiet defaults
# --------------------------------------------------------------------- #


def test_quiet_defaults_add_keywords_and_medium_floor() -> None:
    cadence, floor, trigger = wp.quiet_defaults(_proposal(), "acme corp", ["enterprise"])
    assert cadence == "daily" and floor.value == "medium"
    kws = trigger["keywords"]
    assert kws[:2] == ["acme", "corp"] and "pricing" in kws and "enterprise" in kws
    _, _, stock_trigger = wp.quiet_defaults(
        _proposal(signal_type="stock", target="ACME"), "acme", [],
    )
    assert stock_trigger == {"abs_change_pct_gte": 5}
    cadence_pw, _, _ = wp.quiet_defaults(
        _proposal(signal_type="page_watch", target="https://acme.com/pricing"), "acme", [],
    )
    assert cadence_pw == "weekly"


# --------------------------------------------------------------------- #
# apply_proposals
# --------------------------------------------------------------------- #


def test_apply_adds_direct_and_files_suggestion(db: Path) -> None:
    findings = [_finding(), _finding(title="Initech raised", summary="Initech raised.", confidence="medium")]
    proposals = [
        _proposal(),
        _proposal(slug="rss-initech", target="https://initech.com/feed.xml",
                  grounding_entity="Initech", finding_index=1, certainty="unsure"),
    ]
    out = wp.apply_proposals(proposals, findings, _ctx(), db_path=db)
    assert [o["outcome"] for o in out] == ["added", "suggested"]
    rows = {w.slug: w for w in ms.list_watchlist(db_path=db)}
    acme, initech = rows["rss-acme-blog"], rows["rss-initech"]
    assert acme.origin == ORIGIN_RESEARCH and acme.mode == MODE_ACTIVE
    assert acme.notes == "Competitor pricing moves" and acme.severity_floor.value == "medium"
    assert acme.config_json["_policy"]["grounding_kind"] == wp.KIND_COMPETITOR
    assert initech.origin == ORIGIN_RESEARCH_PROPOSED and initech.mode == MODE_DRY_RUN
    assert ms.list_pending_suggestions(db_path=db)[0].slug == "rss-initech"


def test_apply_honours_budgets_and_duplicate_targets(db: Path) -> None:
    findings = [_finding()]
    proposals = [
        _proposal(slug="stock-acme", signal_type="stock", target="ACME"),
        _proposal(slug="stock-acme-2", signal_type="stock", target="acme"),  # same target
        _proposal(slug="vendor-stripe", signal_type="vendor_status",
                  target="https://status.stripe.com/feed.atom", grounding_entity="Stripe"),
        _proposal(slug="rss-acme-blog"),  # third direct → budget → suggestion
        _proposal(slug="rss-initech", target="https://initech.com/feed.xml", grounding_entity="Initech"),
        _proposal(slug="rss-globex", target="https://globex.com/feed.xml", grounding_entity="Globex",
                  certainty="unsure"),  # suggestion budget spent → rejected
    ]
    ctx = _ctx(settings=wp.PolicySettings(max_direct_adds=2, max_suggestions=2))
    out = wp.apply_proposals(proposals, findings, ctx, db_path=db)
    assert [o["outcome"] for o in out] == [
        "added", "rejected", "added", "suggested", "suggested", "rejected",
    ]
    assert "duplicate" in out[1]["result_preview"]
    assert "budget" in out[5]["result_preview"]


# --------------------------------------------------------------------- #
# Declines memory
# --------------------------------------------------------------------- #


def test_explicit_decline_is_permanent_and_expiry_retries_after_90_days(db: Path) -> None:
    ms.insert_decline(normalized_target="https://initech.com/feed.xml",
                      kind=DECLINE_KIND_EXPLICIT, reason="not_relevant", db_path=db)
    ms.insert_decline(normalized_target="https://globex.com/feed.xml",
                      kind=DECLINE_KIND_EXPIRED, reason="expired", db_path=db)
    assert ms.is_declined("https://initech.com/feed.xml", db_path=db)
    assert ms.is_declined("https://globex.com/feed.xml", db_path=db)
    later = datetime.now(UTC) + timedelta(days=91)
    assert ms.is_declined("https://initech.com/feed.xml", now=later, db_path=db)
    assert not ms.is_declined("https://globex.com/feed.xml", now=later, db_path=db)
    # An expiry never downgrades an explicit decline.
    ms.insert_decline(normalized_target="https://initech.com/feed.xml",
                      kind=DECLINE_KIND_EXPIRED, reason="expired", db_path=db)
    assert ms.list_declines(db_path=db)[-1].kind == DECLINE_KIND_EXPLICIT or any(
        d.normalized_target == "https://initech.com/feed.xml" and d.kind == DECLINE_KIND_EXPLICIT
        for d in ms.list_declines(db_path=db)
    )


@pytest.mark.asyncio
async def test_propose_handler_refuses_declined_target_under_new_slug(
    db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.orchestrator import watchlist_tools as wt

    async def _passthrough(signal_type: str, target: str, config: dict) -> tuple[str, str, dict]:
        return signal_type, target, config

    monkeypatch.setattr(wt, "validate_and_normalize_target", _passthrough)
    ms.insert_decline(normalized_target="https://initech.com/feed.xml",
                      kind=DECLINE_KIND_EXPLICIT, reason="not_relevant", db_path=db)
    collector: list[Any] = []
    out = await wt.handle_propose_watch({
        "slug": "rss-initech-again", "signal_type": "rss",
        "target": "HTTPS://initech.com/feed.xml/", "grounding_entity": "Initech",
        "rationale": "x", "certainty": "confident",
    }, collector)
    assert "declined" in out and collector == []
    ok = await wt.handle_propose_watch({
        "slug": "stock-acme", "signal_type": "stock", "target": "ACME",
        "grounding_entity": "Acme Corp", "rationale": "y", "certainty": "confident",
        "finding_index": 1,
    }, collector)
    assert '"queued": "stock-acme"' in ok and collector[0].finding_index == 0


# --------------------------------------------------------------------- #
# Sweep
# --------------------------------------------------------------------- #


def _backdate(db: Path, slug: str, days: int) -> None:
    import sqlite3

    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE watchlist SET created_at = ? WHERE slug = ?",
            ((datetime.now(UTC) - timedelta(days=days)).isoformat(), slug),
        )


def test_sweep_expires_old_suggestions_as_unreviewed(db: Path) -> None:
    ms.insert_watchlist_item(
        slug="rss-old", signal_type="rss", target="https://old.com/feed", mode=MODE_DRY_RUN,
        origin=ORIGIN_RESEARCH_PROPOSED, db_path=db,
    )
    ms.insert_watchlist_item(
        slug="rss-new", signal_type="rss", target="https://new.com/feed", mode=MODE_DRY_RUN,
        origin=ORIGIN_RESEARCH_PROPOSED, db_path=db,
    )
    _backdate(db, "rss-old", 15)
    counts = wp.sweep(datetime.now(UTC), db_path=db)
    assert counts["expired"] == 1
    assert [w.slug for w in ms.list_pending_suggestions(db_path=db)] == ["rss-new"]
    decl = ms.list_declines(db_path=db)
    assert decl[0].normalized_target == "https://old.com/feed" and decl[0].kind == DECLINE_KIND_EXPIRED


def test_sweep_expires_a_suggestion_that_already_recorded_signals(db: Path) -> None:
    """Dry-run suggestions poll and record signals; the FK on external_signals
    must not make them un-expirable."""
    from openexecutive.monitoring.models import Signal

    wid = ms.insert_watchlist_item(
        slug="rss-polled", signal_type="rss", target="https://polled.com/feed", mode=MODE_DRY_RUN,
        origin=ORIGIN_RESEARCH_PROPOSED, db_path=db,
    )
    ms.insert_signal(Signal(
        watchlist_id=wid, source_kind="rss", source_external_id="e1",
        captured_at=datetime.now(UTC).isoformat(), normalized_summary="s",
        provenance_url="https://polled.com/1", dedup_key="k1",
    ), db_path=db)
    _backdate(db, "rss-polled", 15)
    assert wp.sweep(datetime.now(UTC), db_path=db)["expired"] == 1
    assert ms.get_watchlist_item(wid, db_path=db) is None
    assert ms.list_signals_for_watchlist(wid, db_path=db) == []


def test_sweep_auto_disables_noisy_and_dead_research_watches(db: Path) -> None:
    import sqlite3

    ms.insert_watchlist_item(slug="rss-noisy", signal_type="rss", target="https://noisy.com/feed",
                             origin=ORIGIN_RESEARCH, db_path=db)
    ms.insert_watchlist_item(slug="rss-dead", signal_type="rss", target="https://dead.com/feed",
                             origin=ORIGIN_RESEARCH, db_path=db)
    ms.insert_watchlist_item(slug="rss-mine", signal_type="rss", target="https://mine.com/feed",
                             db_path=db)  # manual: never auto-disabled
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE watchlist SET fired_count = 6, dismiss_count = 3, trust_score = 0.4 "
            "WHERE slug IN ('rss-noisy', 'rss-mine')"
        )
    _backdate(db, "rss-dead", 31)
    _backdate(db, "rss-mine", 31)
    counts = wp.sweep(datetime.now(UTC), db_path=db)
    assert counts["disabled"] == 2
    enabled = {w.slug: w.enabled for w in ms.list_watchlist(db_path=db)}
    assert enabled == {"rss-noisy": False, "rss-dead": False, "rss-mine": True}


def test_sweep_nudges_once_suggestions_pile_up(db: Path) -> None:
    from openexecutive.alerts.store import list_alerts

    for i in range(3):
        ms.insert_watchlist_item(
            slug=f"rss-s{i}", signal_type="rss", target=f"https://s{i}.com/feed",
            mode=MODE_DRY_RUN, origin=ORIGIN_RESEARCH_PROPOSED, db_path=db,
        )
    assert wp.sweep(datetime.now(UTC), db_path=db)["nudged"] == 0  # too fresh
    _backdate(db, "rss-s0", 8)
    assert wp.sweep(datetime.now(UTC), db_path=db)["nudged"] == 1
    assert wp.sweep(datetime.now(UTC), db_path=db)["nudged"] == 0  # coalesced
    alerts = [a for a in list_alerts(limit=10, db_path=db) if a.source == wp.NUDGE_ALERT_SOURCE]
    assert len(alerts) == 1 and "3 watch suggestions" in alerts[0].headline
