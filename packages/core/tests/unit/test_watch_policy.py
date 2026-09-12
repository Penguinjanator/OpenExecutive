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


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """URL validation resolves DNS; keep the policy tests offline."""
    from openexecutive.orchestrator import watchlist_tools as wt

    monkeypatch.setattr(wp, "validate_target_url", lambda url: (True, ""))
    monkeypatch.setattr(wt, "validate_target_url", lambda url: (True, ""))

    async def _passthrough(signal_type: str, target: str, config: dict) -> tuple[str, str, dict]:
        return signal_type, target, config

    monkeypatch.setattr(wt, "validate_and_normalize_target", _passthrough)


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "policy.db"
    monkeypatch.setattr("openexecutive.memory.episodic.DB_PATH", db_path)
    monkeypatch.setattr("openexecutive.alerts.store.DB_PATH", db_path)
    monkeypatch.setattr("openexecutive.departments.store.DB_PATH", db_path)
    monkeypatch.setattr("openexecutive.people.store.DB_PATH", db_path)
    initialize_episodic_db(db_path)
    initialize_alerts_db(db_path)
    ms.initialize_db(db_path)
    from openexecutive.departments import registry as dept_registry
    from openexecutive.departments import store as dept_store
    from openexecutive.people import registry as people_registry
    from openexecutive.people import store as people_store

    dept_store.initialize_db(db_path)
    people_store.initialize_db(db_path)
    dept_registry.invalidate()
    people_registry.invalidate()
    audit = AuditLogger(db_path=db_path)
    audit.initialize_db()
    set_audit_logger(audit)
    yield db_path
    dept_registry.invalidate()
    people_registry.invalidate()


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
    assert vocab["sente labs"].kind == wp.KIND_COMPANY
    assert vocab["acme corp"].kind == wp.KIND_COMPETITOR
    assert vocab["stripe"].kind == wp.KIND_VENDOR
    assert vocab["acme"].kind == wp.KIND_TICKER
    assert vocab["launch helios api"].kind == wp.KIND_INITIATIVE
    assert vocab["hubspot"].kind == wp.KIND_WATCH and vocab["hubs"].kind == wp.KIND_WATCH
    assert all(entry.department == "" for entry in vocab.values())


def test_match_entity_is_token_based_and_prefers_company_data() -> None:
    vocab = {"acme corp": wp.KIND_COMPETITOR, "acme": wp.KIND_WATCH, "stripe": wp.KIND_VENDOR}
    assert wp.match_entity("Acme", vocab) == ("acme corp", wp.KIND_COMPETITOR)
    assert wp.match_entity("ACME CORP.", vocab) == ("acme corp", wp.KIND_COMPETITOR)
    assert wp.match_entity("Stripe Inc", vocab) == ("stripe", wp.KIND_VENDOR)
    assert wp.match_entity("Initech", vocab) is None
    assert wp.match_entity("", vocab) is None


def test_priority_terms_drop_stopwords() -> None:
    # "expand", "into", "enterprise" are stopwords; only distinctive words remain.
    assert wp.priority_terms(_profile()) == ["accounts"]


def test_normalize_target_and_domain() -> None:
    assert normalize_target("stock", " acme ") == "ACME"
    # Respellings of one path collapse: dot segments, percent-encoding, a
    # trailing host dot.
    assert normalize_target("rss", "https://competitor.example./x/../blog/./%66eed") == "https://competitor.example/blog/feed"
    assert normalize_target("query", "site:https://acme.com  Acme   Pricing") == "site:https://acme.com acme pricing"
    # scheme, www., query, fragment, path case and trailing slash never
    # distinguish two spellings of one source.
    assert normalize_target("rss", "HTTP://www.Acme.com/Feed/?x=1#top") == "https://acme.com/feed"
    assert normalize_target("rss", "https://acme.com/") == "https://acme.com"
    assert normalize_target("query", "  Acme   Corp pricing ") == "acme corp pricing"
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


def test_ticker_is_own_source_only_when_it_is_the_entity() -> None:
    p = _proposal(slug="stock-acme", signal_type="stock", target="ACME", grounding_entity="ACME")
    d = wp.classify(p, _finding(title="ACME slides", summary="ACME fell 8% on 2026-09-01."), _ctx())
    assert d.tier == wp.TIER_DIRECT and d.grounding_kind == wp.KIND_TICKER
    # A profile ticker that belongs to someone else lends nothing to "Acme Corp".
    other = _proposal(slug="stock-msft", signal_type="stock", target="MSFT", grounding_entity="Acme Corp")
    profile = CompanyProfile.model_validate({
        "name": "Sente Labs", "competitive_landscape": {"primary_competitors": ["Acme Corp"]},
        "tickers": ["MSFT"],
    })
    ctx = wp.PolicyContext(vocabulary=wp.grounding_vocabulary(profile, [], []), priority_terms=[], existing=[])
    d2 = wp.classify(other, _finding(), ctx)
    assert d2.tier == wp.TIER_SUGGEST and "own source" not in " ".join(d2.reasons)


def _stripe_finding(**kw: Any) -> ResearchFinding:
    return _finding(title="Stripe incident", summary="Stripe reported degraded payments on 2026-09-02.",
                    relevant_urls=["https://status.stripe.com/incidents/abc"], source_specialist="coo", **kw)


def test_vendor_status_page_is_direct() -> None:
    p = _proposal(
        slug="vendor-stripe", signal_type="vendor_status",
        target="https://status.stripe.com/feed.atom", grounding_entity="Stripe",
    )
    d = wp.classify(p, _stripe_finding(), _ctx())
    assert d.tier == wp.TIER_DIRECT and "own source" in " ".join(d.reasons)


def _initech_finding(**kw: Any) -> ResearchFinding:
    base: dict[str, Any] = dict(title="Initech raised", summary="Initech raised a Series B on 2026-09-03.",
                                relevant_urls=["https://initech.com/blog/series-b"])
    base.update(kw)
    return _finding(**base)


def test_entity_not_in_company_data_is_never_direct() -> None:
    p = _proposal(grounding_entity="Initech", target="https://initech.com/feed.xml")
    d = wp.classify(p, _initech_finding(verification="confirmed", source_specialist="cso,cfo"), _ctx())
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


def test_ceiling_rejects_adds_and_suggestions(db: Path) -> None:
    for i in range(3):
        ms.insert_watchlist_item(slug=f"stock-x{i}", signal_type="stock", target=f"X{i}", db_path=db)
    ctx = _ctx(ms.list_watchlist(db_path=db), settings=wp.PolicySettings(max_enabled=3))
    d = wp.classify(_proposal(), _finding(), ctx)
    assert d.tier == wp.TIER_REJECT and "ceiling" in d.reasons[-1]
    unsure = _proposal(grounding_entity="Initech", target="https://initech.com/feed.xml", certainty="unsure")
    assert wp.classify(unsure, _finding(), ctx).tier == wp.TIER_REJECT


def test_nothing_vouching_is_rejected_not_suggested() -> None:
    bare = _proposal(grounding_entity="", target="https://randomblog.example/feed", certainty="unsure",
                     finding_index=None)
    assert wp.classify(bare, None, _ctx()).tier == wp.TIER_REJECT
    weak = _proposal(grounding_entity="Initech", target="https://initech.com/feed.xml", certainty="unsure")
    assert wp.classify(weak, _initech_finding(confidence="medium"), _ctx()).tier == wp.TIER_REJECT
    assert wp.classify(weak, _initech_finding(confidence="high"), _ctx()).tier == wp.TIER_SUGGEST


def test_own_source_uses_the_site_label_only() -> None:
    assert wp._site_label("status.acme.com") == "acme"
    assert wp._site_label("acme.co.uk") == "acme"
    assert wp._site_label("api.unrelated-vendor.com") == "unrelated-vendor"
    # The label must be the entity's leading name or its words run together,
    # never a generic trailing word: "Acme Payments" is not payments.io.
    vocab = {"acme payments": wp.KIND_VENDOR}
    generic = _proposal(slug="rss-pay", target="https://payments.io/feed", grounding_entity="Acme Payments")
    assert not wp._is_own_source(generic, "acme payments", vocab)
    for host in ("https://acme.com/feed", "https://status.acmepayments.com/feed"):
        own = _proposal(slug="rss-own", target=host, grounding_entity="Acme Payments")
        assert wp._is_own_source(own, "acme payments", vocab)
    p = _proposal(slug="pw", signal_type="page_watch", target="https://api.unrelated.com/pricing",
                  grounding_entity="Launch Helios API")
    assert "own source" not in " ".join(wp.classify(p, _finding(), _ctx()).reasons)
    p2 = _proposal(slug="stock-msft", signal_type="stock", target="MSFT", grounding_entity="Acme Corp")
    assert "own source" not in " ".join(wp.classify(p2, _finding(), _ctx()).reasons)


def test_vocabulary_prefers_the_strongest_kind() -> None:
    profile = CompanyProfile.model_validate({"name": "Acme", "vendors": ["Acme"], "tickers": ["ACME"]})
    vocab = wp.grounding_vocabulary(profile, [SimpleNamespace(title="Acme")], [])
    assert vocab["acme"].kind == wp.KIND_COMPANY


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
    cadence, floor, trigger = wp.quiet_defaults(_proposal(), ["accounts"])
    assert cadence == "daily" and floor.value == "medium"
    kws = trigger["keywords"]
    assert "pricing" in kws and "accounts" in kws
    # The entity's own name is never a keyword: feed summaries carry the
    # feed label, so it would match every entry.
    assert "acme" not in kws and "corp" not in kws
    _, _, stock_trigger = wp.quiet_defaults(_proposal(signal_type="stock", target="ACME"), [])
    assert stock_trigger == {"abs_change_pct_gte": 5}
    cadence_pw, _, _ = wp.quiet_defaults(
        _proposal(signal_type="page_watch", target="https://acme.com/pricing"), [],
    )
    assert cadence_pw == "weekly"


# --------------------------------------------------------------------- #
# apply_proposals
# --------------------------------------------------------------------- #


def test_apply_adds_direct_and_files_suggestion(db: Path) -> None:
    findings = [_finding(), _initech_finding(confidence="high")]
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
    findings = [_finding(title="ACME cuts prices"), _stripe_finding(), _initech_finding(confidence="high")]
    proposals = [
        _proposal(slug="stock-acme", signal_type="stock", target="ACME", grounding_entity="ACME"),
        _proposal(slug="stock-acme-2", signal_type="stock", target="acme", grounding_entity="ACME"),  # same target
        _proposal(slug="vendor-stripe", signal_type="vendor_status",
                  target="https://status.stripe.com/feed.atom", grounding_entity="Stripe", finding_index=1),
        _proposal(slug="rss-acme-blog"),  # third direct → budget → suggestion
        _proposal(slug="rss-initech", target="https://initech.com/feed.xml", grounding_entity="Initech",
                  finding_index=2),
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
    # The caller's context is not mutated by the run.
    assert ctx.existing == []


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
    # An expiry never downgrades an explicit decline; an explicit decline
    # always overwrites an expiry.
    ms.insert_decline(normalized_target="https://initech.com/feed.xml",
                      kind=DECLINE_KIND_EXPIRED, reason="expired", db_path=db)
    ms.insert_decline(normalized_target="https://globex.com/feed.xml",
                      kind=DECLINE_KIND_EXPLICIT, reason="wrong_source", db_path=db)
    kinds = {d.normalized_target: (d.kind, d.reason) for d in ms.list_declines(db_path=db)}
    assert kinds["https://initech.com/feed.xml"] == (DECLINE_KIND_EXPLICIT, "not_relevant")
    assert kinds["https://globex.com/feed.xml"] == (DECLINE_KIND_EXPLICIT, "wrong_source")
    assert ms.is_declined("https://globex.com/feed.xml", now=later, db_path=db)


@pytest.mark.asyncio
async def test_propose_handler_refuses_declined_target_under_new_slug(
    db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.orchestrator import watchlist_tools as wt

    ms.insert_decline(normalized_target="https://initech.com/feed.xml",
                      kind=DECLINE_KIND_EXPLICIT, reason="wrong_source", db_path=db)
    collector: list[Any] = []
    out = await wt.handle_propose_watch({
        "slug": "rss-initech-again", "signal_type": "rss",
        "target": "HTTP://www.initech.com/Feed.xml/?utm=1", "grounding_entity": "Initech",
        "rationale": "x", "certainty": "confident",
    }, collector)
    assert "declined" in out and collector == []
    # wrong_source blacklists only that target; another Initech source may be proposed.
    other = await wt.handle_propose_watch({
        "slug": "rss-initech-status", "signal_type": "vendor_status",
        "target": "https://status.initech.com/history.atom", "grounding_entity": "Initech",
        "rationale": "x", "certainty": "unsure",
    }, collector)
    assert '"queued"' in other and len(collector) == 1
    # not_relevant blacklists the entity: any Initech source is refused.
    ms.insert_decline(normalized_target="https://initech.com/other", entity="Initech Inc",
                      kind=DECLINE_KIND_EXPLICIT, reason="not_relevant", db_path=db)
    blocked = await wt.handle_propose_watch({
        "slug": "stock-initech", "signal_type": "stock", "target": "INTC",
        "grounding_entity": "initech", "rationale": "x", "certainty": "confident",
    }, collector)
    assert "declined watching" in blocked and len(collector) == 1
    ok = await wt.handle_propose_watch({
        "slug": "stock-acme", "signal_type": "stock", "target": "ACME",
        "grounding_entity": "Acme Corp", "rationale": "y", "certainty": "confident",
        "finding_index": 1,
    }, collector)
    assert '"queued": "stock-acme"' in ok
    assert len(collector) == 2 and collector[-1].finding_index == 0
    dup = await wt.handle_propose_watch({
        "slug": "stock-acme", "signal_type": "stock", "target": "ACME2",
        "grounding_entity": "Acme Corp", "rationale": "y", "certainty": "confident", "finding_index": 0,
    }, collector)
    assert "already proposed" in dup and len(collector) == 2


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


def test_sweep_auto_disables_noisy_research_watches_only(db: Path) -> None:
    import sqlite3

    ms.insert_watchlist_item(slug="rss-noisy", signal_type="rss", target="https://noisy.com/feed",
                             origin=ORIGIN_RESEARCH, db_path=db)
    ms.insert_watchlist_item(slug="pw-quiet", signal_type="page_watch", target="https://quiet.com/pricing",
                             origin=ORIGIN_RESEARCH, db_path=db)  # silent = unchanged page, still working
    ms.insert_watchlist_item(slug="rss-mine", signal_type="rss", target="https://mine.com/feed",
                             db_path=db)  # manual: never auto-disabled
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE watchlist SET fired_count = 6, dismiss_count = 3, trust_score = 0.4 "
            "WHERE slug IN ('rss-noisy', 'rss-mine')"
        )
    _backdate(db, "pw-quiet", 45)
    counts = wp.sweep(datetime.now(UTC), db_path=db)
    assert counts["disabled"] == 1
    enabled = {w.slug: w.enabled for w in ms.list_watchlist(db_path=db)}
    assert enabled == {"rss-noisy": False, "pw-quiet": True, "rss-mine": True}


def test_sweep_auto_disables_after_consecutive_poll_failures(db: Path) -> None:
    from openexecutive.audit import log_event

    ms.insert_watchlist_item(slug="rss-broken", signal_type="rss", target="https://broken.com/feed",
                             origin=ORIGIN_RESEARCH, db_path=db)
    for _ in range(2):
        log_event("external_monitor_poll", "Polled rss-broken (rss) — FAILED", actor="external_monitor",
                  details={"watchlist_slug": "rss-broken", "failed": True})
    assert wp.sweep(datetime.now(UTC), db_path=db)["disabled"] == 0
    log_event("external_monitor_poll", "Polled rss-broken (rss) — FAILED", actor="external_monitor",
              details={"watchlist_slug": "rss-broken", "failed": True})
    assert wp.sweep(datetime.now(UTC), db_path=db)["disabled"] == 1


def test_sweep_nudges_once_suggestions_pile_up(db: Path) -> None:
    from openexecutive.alerts.store import list_alerts

    for i in range(3):
        ms.insert_watchlist_item(
            slug=f"rss-s{i}", signal_type="rss", target=f"https://s{i}.com/feed",
            mode=MODE_DRY_RUN, origin=ORIGIN_RESEARCH_PROPOSED, db_path=db,
        )
    assert wp.sweep(datetime.now(UTC), db_path=db)["nudged"] == 0  # too fresh
    _backdate(db, "rss-s0", 8)
    _backdate(db, "rss-s1", 8)
    ms.insert_watchlist_item(
        slug="rss-s3", signal_type="rss", target="https://s3.com/feed",
        mode=MODE_DRY_RUN, origin=ORIGIN_RESEARCH_PROPOSED, db_path=db,
    )
    assert wp.sweep(datetime.now(UTC), db_path=db)["nudged"] == 1
    assert wp.sweep(datetime.now(UTC), db_path=db)["nudged"] == 0  # coalesced into the open card
    # Acting on the oldest suggestion must not spawn another nudge.
    first = ms.get_watchlist_item_by_slug("rss-s0", db_path=db)
    assert first is not None and first.id is not None and ms.approve_suggestion(first.id, db_path=db)
    assert wp.sweep(datetime.now(UTC), db_path=db)["nudged"] == 0
    alerts = [a for a in list_alerts(limit=10, db_path=db) if a.source == wp.NUDGE_ALERT_SOURCE]
    assert len(alerts) == 1 and "4 watch suggestions" in alerts[0].headline


def test_direct_add_needs_a_finding_that_cites_the_source() -> None:
    # Department entity + own source + a decision mention reach the score
    # bar without any finding evidence about the target; that is still only
    # a suggestion — nothing goes live on hearsay.
    decided = SimpleNamespace(summary="Move expense cards to Brex", department="finance", timestamp="")
    unrelated = _finding(confidence="high")  # about Acme, cites acme.com
    d = wp.classify(_brex_proposal(), unrelated, _dept_ctx(recent_decisions=[decided]))
    assert d.score >= wp.DIRECT_THRESHOLD and d.tier == wp.TIER_SUGGEST
    assert "no finding cites this source" in d.reasons
    assert wp.classify(_brex_proposal(), _brex_finding(), _dept_ctx(recent_decisions=[decided])).tier == wp.TIER_DIRECT


def test_finding_points_need_a_finding_about_this_source() -> None:
    # A strong finding about Acme lends nothing to an unrelated attacker URL.
    p = _proposal(target="https://totally-unrelated.attacker.net/feed", grounding_entity="Acme Corp")
    d = wp.classify(p, _finding(verification="confirmed", source_specialist="cso,cfo"), _ctx())
    assert d.score == 2 and d.tier == wp.TIER_SUGGEST
    assert "does not mention this source" in " ".join(d.reasons)
    # A finding that names the ticker does support a ticker watch on it.
    p2 = _proposal(slug="stock-acme", signal_type="stock", target="ACME", grounding_entity="ACME")
    assert wp.classify(p2, _finding(title="ACME slides 8%"), _ctx()).tier == wp.TIER_DIRECT


def test_direct_add_requires_the_entitys_own_source() -> None:
    # A well-corroborated third-party page about a competitor is the
    # principal's call, never a direct add.
    p = _proposal(target="https://news.example.com/acme-feed.xml", grounding_entity="Acme Corp")
    f = _finding(verification="confirmed", source_specialist="cso,cfo",
                 relevant_urls=["https://news.example.com/acme-pricing"])
    d = wp.classify(p, f, _ctx())
    assert d.score >= wp.DIRECT_THRESHOLD and d.tier == wp.TIER_SUGGEST
    assert "not the entity's own source" in d.reasons


def test_initiative_or_priority_grounding_is_never_direct() -> None:
    p = _proposal(slug="rss-helios", target="https://helios.com/feed.xml", grounding_entity="Helios API")
    f = _finding(title="Helios API launch", summary="Launch Helios API shipped.",
                 verification="confirmed", relevant_urls=["https://helios.com/launch"])
    d = wp.classify(p, f, _ctx())
    assert d.grounding_kind == wp.KIND_INITIATIVE and d.score >= wp.DIRECT_THRESHOLD
    assert d.tier == wp.TIER_SUGGEST and "needs a named competitor" in d.reasons[-1]


def test_generic_tokens_do_not_ground() -> None:
    # "enterprise" appears in a priority but is a stopword; "Corp" is too.
    assert wp.match_entity("Enterprise Corp", _ctx().vocabulary) is None
    assert wp.match_entity("Growth", {"drive growth in emea": wp.KIND_PRIORITY}) is None
    # Short names that are whole vocabulary terms still match in longer forms.
    assert wp.match_entity("IBM Corp", {"ibm": wp.KIND_COMPETITOR}) == ("ibm", wp.KIND_COMPETITOR)
    assert wp.match_entity("AWS Lambda", {"aws": wp.KIND_VENDOR}) == ("aws", wp.KIND_VENDOR)


def test_vocabulary_from_url_targets_uses_the_site_label(db: Path) -> None:
    ms.insert_watchlist_item(slug="vendor-stripe", signal_type="vendor_status",
                             target="https://status.stripe.com/history.atom", db_path=db)
    vocab = wp.grounding_vocabulary(CompanyProfile(name="X"), [], ms.list_watchlist(db_path=db))
    assert vocab["stripe"].kind == wp.KIND_WATCH
    assert not any(t.startswith("https") for t in vocab)
    assert wp.match_entity("Status Labs", vocab) is None


def test_quiet_defaults_never_put_keywords_on_a_page_watch() -> None:
    _, _, trigger = wp.quiet_defaults(
        _proposal(signal_type="page_watch", target="https://acme.com/pricing"), ["accounts"],
    )
    assert "keywords" not in trigger


def test_not_relevant_decline_remembers_the_models_entity_when_ungrounded(db: Path) -> None:
    findings = [_initech_finding(confidence="high")]
    out = wp.apply_proposals(
        [_proposal(slug="rss-initech", target="https://initech.com/feed.xml",
                   grounding_entity="Initech Industries", certainty="unsure")],
        findings, _ctx(), db_path=db,
    )
    assert out[0]["outcome"] == "suggested"
    row = ms.get_watchlist_item_by_slug("rss-initech", db_path=db)
    assert row is not None and row.config_json["_policy"]["entity"] == "initech industries"


def test_own_source_ignores_generic_host_tokens() -> None:
    p = _proposal(target="https://labs.example.com/feed.xml", grounding_entity="Sente Labs")
    d = wp.classify(p, _finding(), _ctx())
    assert "own source" not in " ".join(d.reasons)


def test_retired_source_is_not_re_added(db: Path) -> None:
    ms.insert_watchlist_item(slug="rss-acme-old", signal_type="rss", target="https://acme.com/blog/feed.xml",
                             origin=ORIGIN_RESEARCH, enabled=False, db_path=db)
    d = wp.classify(_proposal(), _finding(), _ctx(ms.list_watchlist(db_path=db)))
    assert d.tier == wp.TIER_REJECT


def test_auto_disable_records_a_retryable_decline(db: Path) -> None:
    import sqlite3

    ms.insert_watchlist_item(slug="rss-noisy", signal_type="rss", target="https://noisy.com/feed",
                             origin=ORIGIN_RESEARCH, db_path=db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE watchlist SET fired_count = 6, dismiss_count = 3, trust_score = 0.4")
    assert wp.sweep(datetime.now(UTC), db_path=db)["disabled"] == 1
    assert ms.is_declined("https://noisy.com/feed", db_path=db)
    assert not ms.is_declined("https://noisy.com/feed", now=datetime.now(UTC) + timedelta(days=91), db_path=db)


def test_source_url_stamp_only_keeps_public_http_urls(db: Path) -> None:
    f = _finding(relevant_urls=["javascript:alert(1)", "ftp://x/y", "https://www.acme.com/blog/pricing"])
    assert wp._safe_source_url(f) == "https://www.acme.com/blog/pricing"
    out = wp.apply_proposals([_proposal()], [f], _ctx(), db_path=db)
    assert out[0]["outcome"] == "added"
    row = ms.get_watchlist_item_by_slug("rss-acme-blog", db_path=db)
    assert row is not None and row.config_json["_policy"]["source_url"] == "https://www.acme.com/blog/pricing"


def test_insert_failure_is_reported_without_exception_text(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**kw: Any) -> int:
        raise RuntimeError("sqlite3.OperationalError: /secret/path locked")

    monkeypatch.setattr(ms, "insert_watchlist_item", boom)
    out = wp.apply_proposals([_proposal()], [_finding()], _ctx(), db_path=db)
    assert out[0]["outcome"] == "rejected" and "see server log" in out[0]["result_preview"]
    assert "/secret/path" not in out[0]["result_preview"]


# --------------------------------------------------------------------- #
# Departments + episodic decisions
# --------------------------------------------------------------------- #


def _department(slug: str, title: str, *, watched: list[str] | None = None,
                scope: list[str] | None = None, head: int | None = None,
                goals: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(
            slug=slug, title=title, head_person_id=head,
            watched_entities=list(watched or []),
            charter=SimpleNamespace(scope=list(scope or [])),
        ),
        goals=[SimpleNamespace(key_result=g) for g in (goals or [])],
    )


def _finance(head: int | None = 7) -> SimpleNamespace:
    return _department(
        "finance", "Finance", watched=["Brex"], scope=["Expense card programs"],
        goals=["Close the books in 5 days"], head=head,
    )


def _dept_ctx(*departments: Any, existing: list[Any] | None = None, **kw: Any) -> wp.PolicyContext:
    profile = _profile()
    departments = departments or (_finance(),)
    existing = existing or []
    return wp.PolicyContext(
        vocabulary=wp.grounding_vocabulary(profile, [], existing, list(departments)),
        priority_terms=wp.priority_terms(profile),
        existing=existing,
        departments=wp.department_refs(list(departments)),
        **kw,
    )


def _brex_finding(**kw: Any) -> ResearchFinding:
    return _finding(
        title="Brex outage", summary="Brex card authorizations failed for 2h on 2026-09-01.",
        relevant_urls=["https://status.brex.com/incidents/1"], source_specialist="cfo", **kw,
    )


def _brex_proposal(**kw: Any) -> wp.WatchProposal:
    return _proposal(
        slug="vendor-brex", signal_type="vendor_status", target="https://status.brex.com",
        grounding_entity="Brex", rationale="Finance is on Brex cards", **kw,
    )


def test_department_vocabulary_kinds_and_department_merge() -> None:
    finance = _department("finance", "Finance", watched=["Brex", "Acme Corp"],
                          scope=["Expense card programs"], goals=["Close the books in 5 days"])
    vocab = wp.grounding_vocabulary(_profile(), [], [], [finance])
    assert vocab["brex"] == wp.VocabEntry(wp.KIND_DEPARTMENT_ENTITY, "finance")
    assert vocab["expense card programs"] == wp.VocabEntry(wp.KIND_DEPARTMENT_SCOPE, "finance")
    assert vocab["close the books in 5 days"].kind == wp.KIND_DEPARTMENT_SCOPE
    # A profile competitor that Finance also lists keeps the stronger kind
    # and still routes to Finance; so does a profile vendor (profile kinds
    # outrank department kinds, so the vendor history keeps applying).
    assert vocab["acme corp"] == wp.VocabEntry(wp.KIND_COMPETITOR, "finance")
    stripe_vocab = wp.grounding_vocabulary(_profile(), [], [], [_department("finance", "Finance", watched=["Stripe"])])
    assert stripe_vocab["stripe"] == wp.VocabEntry(wp.KIND_VENDOR, "finance")
    # Two departments: the one supplying the winning kind owns the term.
    eng = _department("eng", "Engineering", scope=["Acme Migration"])
    fin = _department("finance", "Finance", watched=["Acme Migration"])
    both = wp.grounding_vocabulary(None, [], [], [eng, fin])
    assert both["acme migration"] == wp.VocabEntry(wp.KIND_DEPARTMENT_ENTITY, "finance")
    # Legacy plain-kind dicts still match.
    assert wp.match_entity("Brex Inc", vocab) == ("brex", wp.KIND_DEPARTMENT_ENTITY)
    assert wp.match_entity("Brex", {"brex": wp.KIND_VENDOR}) == ("brex", wp.KIND_VENDOR)
    assert wp.department_refs([_finance(head=7)]) == {"finance": wp.DepartmentRef("Finance", 7)}


def test_department_watched_entity_grounds_a_direct_add_and_routes(db: Path) -> None:
    d = wp.classify(_brex_proposal(), _brex_finding(), _dept_ctx())
    assert d.tier == wp.TIER_DIRECT and d.grounding_kind == wp.KIND_DEPARTMENT_ENTITY
    assert d.department == "finance" and "Finance watched entity" in d.reasons[0]
    out = wp.apply_proposals([_brex_proposal()], [_brex_finding()], _dept_ctx(), db_path=db)
    assert [o["outcome"] for o in out] == ["added"]
    row = ms.get_watchlist_item_by_slug("vendor-brex", db_path=db)
    assert row is not None and row.mode == MODE_ACTIVE
    assert row.route_to_department == "finance" and row.route_to_person_id == 7
    assert wp.policy_stamp_of(row)["department"] == "finance"


def test_department_scope_grounding_is_suggestion_only(db: Path) -> None:
    p = _proposal(slug="rss-expense", signal_type="rss", target="https://expensecards.example/feed",
                  grounding_entity="Expense card programs")
    f = _finding(title="Expense card programs shift", summary="New expense card programs launched.",
                 relevant_urls=["https://expensecards.example/feed"], verification="confirmed",
                 source_specialist="cfo,coo")
    d = wp.classify(p, f, _dept_ctx())
    assert d.tier == wp.TIER_SUGGEST and d.department == "finance"
    assert d.grounding_kind == wp.KIND_DEPARTMENT_SCOPE and "Finance scope item" in d.reasons[0]
    # Even on its own source a scope item cannot be direct.
    own = _proposal(slug="rss-programs", signal_type="rss", target="https://expense.example/feed",
                    grounding_entity="Expense card programs")
    own_f = _finding(title="programs", summary="s", relevant_urls=["https://expense.example/feed"],
                     verification="confirmed", source_specialist="cfo,coo")
    own_d = wp.classify(own, own_f, _dept_ctx())
    assert own_d.tier == wp.TIER_SUGGEST and any("grounded only in a department_scope" in r for r in own_d.reasons)
    out = wp.apply_proposals([p], [f], _dept_ctx(), db_path=db)
    assert out[0]["outcome"] == "suggested"
    row = ms.get_watchlist_item_by_slug("rss-expense", db_path=db)
    assert row is not None and row.mode == MODE_DRY_RUN and row.route_to_department == "finance"


def test_recent_decision_adds_a_point_and_a_routing_hint() -> None:
    decided = SimpleNamespace(summary="Evaluate Stripe as the EU payments vendor",
                              department="finance", timestamp="2026-09-01T00:00:00+00:00")
    p = _proposal(slug="vendor-stripe", signal_type="vendor_status", target="https://status.stripe.com",
                  grounding_entity="Stripe")
    f = _finding(title="Stripe incident", summary="Stripe API errors on 2026-09-01.",
                 relevant_urls=["https://status.stripe.com/"], confidence="medium")
    base = wp.classify(p, f, _dept_ctx())
    with_decision = wp.classify(p, f, _dept_ctx(recent_decisions=[decided]))
    assert with_decision.score == base.score + 1 and "named in a recent decision" in with_decision.reasons
    # Stripe is a profile vendor with no department; the decision's is used.
    assert base.department == "" and with_decision.department == "finance"
    # A decision for an unknown department is not a routing hint, and one
    # that names nothing relevant scores nothing.
    other = SimpleNamespace(summary="Move Stripe to annual billing", department="ops", timestamp="")
    assert wp.classify(p, f, _dept_ctx(recent_decisions=[other])).department == ""
    unrelated = SimpleNamespace(summary="Hire two SDRs in Q4", department="finance", timestamp="")
    assert wp.classify(p, f, _dept_ctx(recent_decisions=[unrelated])).score == base.score
    assert wp.named_in_decision("acme corp", SimpleNamespace(summary="Acme Corp pricing review"))
    assert wp.named_in_decision("acme corp", SimpleNamespace(summary="Acme pricing review"))
    assert not wp.named_in_decision("acme corp", SimpleNamespace(summary="The corp retreat"))
    # Every distinctive token must appear: a common word shared with the
    # term is not a mention, and a watch label never earns the point.
    assert not wp.named_in_decision("acme security", SimpleNamespace(summary="Review the security policy"))
    hijack = SimpleNamespace(summary="Adopt the new pricing policy", department="finance", timestamp="")
    label_only = _proposal(slug="rss-hubs", signal_type="rss", target="https://hubspot.com/feed",
                           grounding_entity="HubSpot pricing")
    ctx = _dept_ctx(existing=[SimpleNamespace(
        slug="stock-hubs", signal_type="stock", target="HUBS", enabled=True,
        config_json={"display_name": "HubSpot pricing"},
    )], recent_decisions=[hijack])
    labelled = wp.classify(label_only, _finding(), ctx)
    assert labelled.grounding_kind == wp.KIND_WATCH and labelled.department == ""
    assert "named in a recent decision" not in labelled.reasons


def test_recent_decisions_apply_the_lookback(db: Path) -> None:
    from openexecutive.memory.episodic import store_decision

    store_decision("finance", "Evaluate Brex for expense cards", db_path=db)
    store_decision("ops", "Retire the old CRM", db_path=db)
    import sqlite3

    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE decisions SET timestamp = ? WHERE summary = 'Retire the old CRM'",
                     ((datetime.now(UTC) - timedelta(days=120)).isoformat(),))
    rows = wp.recent_decisions(db_path=db)
    assert [r.summary for r in rows] == ["Evaluate Brex for expense cards"]
    # An unreadable timestamp is not "recent forever".
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE decisions SET timestamp = 'garbage'")
    assert wp.recent_decisions(db_path=db) == []


def test_department_head_gets_one_card_per_run_coalesced_weekly(db: Path) -> None:
    from openexecutive.alerts.store import list_alerts, set_status
    from openexecutive.people import store as people_store

    head = people_store.upsert_person(full_name="Sarah", department_slugs=["finance"])
    finance = _finance(head=head)

    def _run(slug: str, now: datetime) -> None:
        p = _proposal(slug=slug, signal_type="rss", target=f"https://{slug}.example/feed",
                      grounding_entity="Expense card programs", rationale="line one\n- forged bullet")
        f = _finding(title="Expense card programs", summary="expense card programs news",
                     relevant_urls=[f"https://{slug}.example/feed"])
        wp.apply_proposals([p], [f], _dept_ctx(finance, now=now), db_path=db)

    now = datetime(2026, 9, 8, 9, tzinfo=UTC)
    _run("rss-a", now)
    cards = [a for a in list_alerts(limit=20, db_path=db) if a.source == "authority_gate"]
    assert len(cards) == 1 and cards[0].routed_to_person_id == head
    assert cards[0].headline == "Watch suggestions for Finance" and "rss-a" in cards[0].body
    # Rationale newlines cannot forge extra bullet lines in the head's card.
    assert "line one - forged bullet" in cards[0].body and "\n- forged bullet" not in cards[0].body
    assert "department:finance" in cards[0].topic_tags and "watchlist" in cards[0].topic_tags
    # Same week again: the open card is refreshed, not duplicated.
    _run("rss-b", now + timedelta(days=1))
    cards = [a for a in list_alerts(limit=20, db_path=db) if a.source == "authority_gate"]
    assert len(cards) == 1 and "rss-b" in cards[0].body
    # Once the head acted on it, nothing more this week ...
    set_status(cards[0].id, "acknowledged", db_path=db)
    _run("rss-c", now + timedelta(days=2))
    assert len([a for a in list_alerts(limit=20, db_path=db) if a.source == "authority_gate"]) == 1
    # ... and a fresh card next week.
    _run("rss-d", now + timedelta(days=7))
    assert len([a for a in list_alerts(limit=20, db_path=db) if a.source == "authority_gate"]) == 2


def test_department_card_falls_back_to_the_principal(db: Path) -> None:
    from openexecutive.alerts.store import list_alerts
    from openexecutive.people import store as people_store

    principal = people_store.upsert_person(full_name="Jo", is_principal=True)
    p = _proposal(slug="rss-x", signal_type="rss", target="https://x.example/feed",
                  grounding_entity="Expense card programs")
    f = _finding(title="Expense card programs", summary="s", relevant_urls=["https://x.example/feed"])
    wp.apply_proposals([p], [f], _dept_ctx(_finance(head=None)), db_path=db)
    cards = [a for a in list_alerts(limit=20, db_path=db) if a.source == "authority_gate"]
    assert len(cards) == 1 and cards[0].routed_to_person_id == principal
    row = ms.get_watchlist_item_by_slug("rss-x", db_path=db)
    assert row is not None and row.route_to_department == "finance" and row.route_to_person_id is None


def test_nudge_counts_only_suggestions_no_head_received(db: Path) -> None:
    from openexecutive.alerts.store import list_alerts

    for i in range(3):
        ms.insert_watchlist_item(
            slug=f"rss-d{i}", signal_type="rss", target=f"https://d{i}.com/feed",
            mode=MODE_DRY_RUN, origin=ORIGIN_RESEARCH_PROPOSED, route_to_department="finance",
            route_to_person_id=7, db_path=db,
        )
        _backdate(db, f"rss-d{i}", 8)
    assert wp.sweep(datetime.now(UTC), db_path=db)["nudged"] == 0
    assert not [a for a in list_alerts(limit=10, db_path=db) if a.source == wp.NUDGE_ALERT_SOURCE]
    # A department without a head is still the principal's pile: those rows
    # count, so they can never become invisible.
    for i in range(3):
        ms.insert_watchlist_item(
            slug=f"rss-n{i}", signal_type="rss", target=f"https://n{i}.com/feed",
            mode=MODE_DRY_RUN, origin=ORIGIN_RESEARCH_PROPOSED, route_to_department="ops",
            db_path=db,
        )
        _backdate(db, f"rss-n{i}", 8)
    assert wp.sweep(datetime.now(UTC), db_path=db)["nudged"] == 1


# --------------------------------------------------------------------- #
# Profile entry parsing, short names, finding auto-link
# --------------------------------------------------------------------- #


def test_entity_names_parse_profile_entries() -> None:
    assert wp.entity_names("Tesla (TSLA) — Model Y is the volume benchmark") == (["Tesla"], ["TSLA"])
    assert wp.entity_names("GM / Chevrolet (Equinox EV, Blazer EV) — direct competitor") == (["GM", "Chevrolet"], [])
    assert wp.entity_names("BYD — global cost leader; tariff-gated out of the US") == (["BYD"], [])
    assert wp.entity_names("Hyundai / Kia (Ioniq 5, EV6, EV9) - strong value") == (["Hyundai", "Kia"], [])
    assert wp.entity_names("Rivian (RIVN, 1211.HK)") == (["Rivian"], ["RIVN", "1211.HK"])
    assert wp.entity_names("Stripe: payments") == (["Stripe"], [])
    assert wp.entity_names("  ") == ([], [])


def _descriptive_profile() -> CompanyProfile:
    return CompanyProfile.model_validate({
        "name": "Halcyon Motors",
        "competitive_landscape": {"primary_competitors": [
            "Tesla (TSLA) — Model Y is the volume benchmark and price-cut pace-setter",
            "BYD — global cost leader; tariff-gated out of the US for now",
            "GM / Chevrolet (Equinox EV, Blazer EV) — direct mainstream-price competitor",
            "Hyundai / Kia (Ioniq 5, EV6, EV9) — strong value + charging speed",
        ]},
        "vendors": ["CATL — sole LFP cell source"],
        "tickers": ["1211.HK"],
    })


def test_descriptive_profile_entries_ground_by_name_only() -> None:
    vocab = wp.grounding_vocabulary(_descriptive_profile(), [], [])
    assert vocab["tesla"].kind == wp.KIND_COMPETITOR and vocab["tsla"].kind == wp.KIND_TICKER
    assert vocab["byd"].kind == wp.KIND_COMPETITOR and vocab["gm"].kind == wp.KIND_COMPETITOR
    assert vocab["chevrolet"].kind == wp.KIND_COMPETITOR and vocab["kia"].kind == wp.KIND_COMPETITOR
    assert vocab["catl"].kind == wp.KIND_VENDOR and vocab["1211 hk"].kind == wp.KIND_TICKER
    # The description never becomes a term, so "Model Y" or "global" ground nothing.
    assert wp.match_entity("Model Y", vocab) is None and wp.match_entity("global cost leader", vocab) is None
    # Short names match as whole words, in longer forms too.
    assert wp.match_entity("BYD", vocab) == ("byd", wp.KIND_COMPETITOR)
    assert wp.match_entity("BYD Auto", vocab) == ("byd", wp.KIND_COMPETITOR)
    assert wp.match_entity("GM", vocab) == ("gm", wp.KIND_COMPETITOR)
    assert wp.match_entity("General Motors", vocab) is None  # a different name is not a match


def test_short_names_get_own_source_and_finding_support() -> None:
    profile = _descriptive_profile()
    vocab = wp.grounding_vocabulary(profile, [], [])
    ctx = wp.PolicyContext(vocabulary=vocab, priority_terms=[], existing=[])
    f = _finding(title="BYD enters Mexico", summary="BYD launched the Seal in Mexico on 2026-09-05.",
                 relevant_urls=["https://www.byd.com/news/mexico"], source_specialist="cso")
    p = _proposal(slug="rss-byd", target="https://www.byd.com/news/feed", grounding_entity="BYD")
    d = wp.classify(p, f, ctx)
    assert d.tier == wp.TIER_DIRECT and "target is the entity's own source" in d.reasons
    # A dotted ticker: own source when grounded as the ticker, and named in
    # text as a phrase.
    f2 = _finding(title="BYD (1211.HK) slides", summary="1211.HK fell 6% on 2026-09-05.", relevant_urls=[])
    stock = _proposal(slug="stock-byd", signal_type="stock", target="1211.HK", grounding_entity="1211.HK")
    d2 = wp.classify(stock, f2, ctx)
    assert d2.tier == wp.TIER_DIRECT and d2.grounding_kind == wp.KIND_TICKER
    # Grounded as the competitor name instead, the ticker is not its own
    # source, so it is a suggestion.
    assert wp.classify(_proposal(slug="stock-byd2", signal_type="stock", target="1211.HK",
                                 grounding_entity="BYD"), f2, ctx).tier == wp.TIER_SUGGEST
    decided = SimpleNamespace(summary="Benchmark C1 pricing against BYD monthly", department="", timestamp="")
    assert wp.named_in_decision("byd", decided, vocab)
    # A short name inside a longer term needs the vocabulary to count.
    two_word = SimpleNamespace(summary="Benchmark against BYD Auto monthly", department="", timestamp="")
    assert wp.named_in_decision("byd auto", two_word, vocab)
    # Every distinctive word must appear: "BYD Auto" is not named by "BYD pricing".
    assert not wp.named_in_decision("byd auto", SimpleNamespace(summary="Watch BYD pricing"), vocab)
    assert wp.entity_declined("BYD Auto", [SimpleNamespace(reason="not_relevant", entity="BYD")])
    assert not wp.entity_declined("Rivian", [SimpleNamespace(reason="not_relevant", entity="BYD")])


def test_proposal_without_finding_index_is_linked_to_the_citing_finding(db: Path) -> None:
    findings = [
        _finding(title="Unrelated", summary="Globex raised prices.", relevant_urls=["https://globex.com/blog"]),
        _finding(title="Acme cut prices", summary="Acme Corp cut list prices 20%.",
                 relevant_urls=["https://www.acme.com/blog/pricing"]),
        _finding(title="ACME slides", summary="ACME fell 8% on 2026-09-01.", relevant_urls=[]),
    ]
    ctx = _ctx()
    unlinked = _proposal(finding_index=None)
    assert wp.auto_link_finding(unlinked, findings, ctx) == 1
    stock = _proposal(slug="stock-acme", signal_type="stock", target="ACME", grounding_entity="ACME",
                      finding_index=None)
    assert wp.auto_link_finding(stock, findings, ctx) == 1  # names the entity first
    nowhere = _proposal(slug="rss-else", target="https://elsewhere.com/feed", grounding_entity="Acme Corp",
                        finding_index=None)
    assert wp.auto_link_finding(nowhere, findings, ctx) is None
    out = wp.apply_proposals([unlinked, nowhere], findings, ctx, db_path=db)
    assert [o["outcome"] for o in out] == ["added", "rejected"]
    row = ms.get_watchlist_item_by_slug("rss-acme-blog", db_path=db)
    assert row is not None
    assert wp.policy_stamp_of(row)["reasons"][0] == "linked to finding #2 by its cited source"
    assert wp.policy_stamp_of(row)["specialist"] == "cso"
