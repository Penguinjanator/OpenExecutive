"""Watchlist HTTP routes: research suggestions (approve / decline), decline
memory on delete, and the origin field on responses."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.alerts import store as alert_store
from openexecutive.api.routes import watchlist as watchlist_route
from openexecutive.audit import AuditLogger, set_audit_logger
from openexecutive.memory import episodic
from openexecutive.monitoring import store as ms


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "watchlist.db"
    monkeypatch.setattr(alert_store, "DB_PATH", db_path)
    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    alert_store.initialize_db(db_path)
    episodic.initialize_db(db_path)
    ms.initialize_db(db_path)
    audit = AuditLogger(db_path=db_path)
    audit.initialize_db()
    set_audit_logger(audit)
    return db_path


@pytest.fixture()
def client(db: Path) -> TestClient:
    app = FastAPI()
    app.include_router(watchlist_route.router)
    return TestClient(app)


def _suggest(db: Path, slug: str = "rss-initech", target: str = "https://initech.com/feed") -> int:
    return ms.insert_watchlist_item(
        slug=slug, signal_type="rss", target=target, mode="dry_run",
        origin="research_proposed", notes="Adjacent competitor blog",
        config={"_policy": {"entity": "Initech", "grounding_kind": "", "specialist": "cso"}},
        db_path=db,
    )


def test_list_carries_origin(client: TestClient, db: Path) -> None:
    ms.insert_watchlist_item(slug="stock-aapl", signal_type="stock", target="AAPL", db_path=db)
    _suggest(db)
    rows = {r["slug"]: r for r in client.get("/watchlist").json()}
    assert rows["stock-aapl"]["origin"] == "manual"
    assert rows["rss-initech"]["origin"] == "research_proposed" and rows["rss-initech"]["mode"] == "dry_run"


def test_approve_turns_suggestion_live_and_records_outcome(client: TestClient, db: Path) -> None:
    _suggest(db)
    res = client.post("/watchlist/rss-initech/approve")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["origin"] == "research" and body["mode"] == "active" and body["enabled"] is True
    assert ms.list_pending_suggestions(db_path=db) == []
    assert ms.policy_outcome_counts(db_path=db) == {("rss", ""): {"approved": 1}}
    assert ms.specialist_outcome_counts(db_path=db) == {"cso": {"approved": 1}}
    # Not a pending suggestion any more → 409; unknown → 404.
    assert client.post("/watchlist/rss-initech/approve").status_code == 409
    assert client.post("/watchlist/nope/approve").status_code == 404


def test_approve_rejects_manual_rows(client: TestClient, db: Path) -> None:
    ms.insert_watchlist_item(slug="stock-aapl", signal_type="stock", target="AAPL", db_path=db)
    assert client.post("/watchlist/stock-aapl/approve").status_code == 409


def test_decline_not_relevant_removes_and_remembers(client: TestClient, db: Path) -> None:
    _suggest(db)
    res = client.post("/watchlist/rss-initech/decline", json={"reason": "not_relevant"})
    assert res.status_code == 200, res.text
    assert res.json() == {"slug": "rss-initech", "reason": "not_relevant", "result": "removed"}
    assert ms.get_watchlist_item_by_slug("rss-initech", db_path=db) is None
    declines = ms.list_declines(db_path=db)
    assert declines[0].normalized_target == "https://initech.com/feed"
    assert declines[0].kind == "declined_explicit" and declines[0].entity == "Initech"
    assert ms.is_declined("https://initech.com/feed", db_path=db)
    assert ms.policy_outcome_counts(db_path=db) == {("rss", ""): {"declined": 1}}


def test_decline_defaults_to_not_relevant_without_a_body(client: TestClient, db: Path) -> None:
    _suggest(db)
    res = client.post("/watchlist/rss-initech/decline")
    assert res.status_code == 200 and res.json()["reason"] == "not_relevant"


def test_decline_too_noisy_keeps_it_live_at_a_high_floor(client: TestClient, db: Path) -> None:
    _suggest(db)
    res = client.post("/watchlist/rss-initech/decline", json={"reason": "too_noisy"})
    assert res.status_code == 200 and res.json()["result"] == "kept_quiet"
    row = ms.get_watchlist_item_by_slug("rss-initech", db_path=db)
    assert row is not None
    assert row.mode == "active" and row.origin == "research" and row.severity_floor.value == "high"
    assert ms.list_declines(db_path=db) == []
    assert ms.policy_outcome_counts(db_path=db) == {("rss", ""): {"approved": 1}}


def test_decline_validates_reason_and_state(client: TestClient, db: Path) -> None:
    _suggest(db)
    assert client.post("/watchlist/rss-initech/decline", json={"reason": "meh"}).status_code == 400
    ms.insert_watchlist_item(slug="stock-aapl", signal_type="stock", target="AAPL", db_path=db)
    assert client.post("/watchlist/stock-aapl/decline", json={"reason": "not_relevant"}).status_code == 409
    assert client.post("/watchlist/nope/decline").status_code == 404


def test_delete_research_row_records_decline_with_reason(client: TestClient, db: Path) -> None:
    ms.insert_watchlist_item(
        slug="stock-acme", signal_type="stock", target="acme", origin="research", db_path=db,
    )
    ms.insert_watchlist_item(slug="stock-mine", signal_type="stock", target="MINE", db_path=db)
    assert client.delete("/watchlist/stock-acme?reason=wrong_source").status_code == 204
    declines = ms.list_declines(db_path=db)
    assert [(d.normalized_target, d.reason) for d in declines] == [("ACME", "wrong_source")]
    assert client.delete("/watchlist/stock-mine").status_code == 204
    assert len(ms.list_declines(db_path=db)) == 1  # manual rows are not declines
    assert client.delete("/watchlist/stock-mine?reason=bogus").status_code == 404


def test_delete_rejects_unknown_reason(client: TestClient, db: Path) -> None:
    ms.insert_watchlist_item(slug="stock-acme", signal_type="stock", target="ACME", origin="research", db_path=db)
    assert client.delete("/watchlist/stock-acme?reason=bogus").status_code == 400
    assert ms.get_watchlist_item_by_slug("stock-acme", db_path=db) is not None


def test_delete_and_decline_work_after_the_watch_recorded_signals(client: TestClient, db: Path) -> None:
    from openexecutive.monitoring.models import Signal

    wid = ms.insert_watchlist_item(
        slug="stock-acme", signal_type="stock", target="ACME", origin="research", db_path=db,
    )
    sid = _suggest(db)
    for w, key in ((wid, "k1"), (sid, "k2")):
        ms.insert_signal(Signal(
            watchlist_id=w, source_kind="stock", source_external_id=key,
            captured_at="2026-09-12T00:00:00+00:00", normalized_summary="s",
            provenance_url="https://x/1", dedup_key=key,
        ), db_path=db)
    assert client.delete("/watchlist/stock-acme?reason=not_relevant").status_code == 204
    assert client.post("/watchlist/rss-initech/decline", json={"reason": "wrong_source"}).status_code == 200
    assert ms.list_watchlist(db_path=db) == []
    assert ms.list_signals_for_watchlist(wid, db_path=db) == []
