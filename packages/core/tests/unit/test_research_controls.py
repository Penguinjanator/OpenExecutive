"""Unit tests for the research run's own controls: its web-search cap,
the specialist filter, the withheld workflow-start tool, and the periodic
cadence default."""
from __future__ import annotations

import os

import pytest

from openexecutive.config import get_settings
from openexecutive.orchestrator.web_search_tool import build_web_search_tool
from openexecutive.workflows import executive_research as er

_ENV = (
    "ENABLE_WEB_SEARCH",
    "WEB_SEARCH_MAX_USES",
    "RESEARCH_WEB_SEARCH_MAX_USES",
    "RESEARCH_SPECIALISTS",
    "WATCHLIST_RESEARCH_INTERVAL_MINUTES",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _ENV:
        monkeypatch.delenv(key, raising=False)


def test_defaults_are_reasonable() -> None:
    s = get_settings()
    assert s.research_web_search_max_uses == 3
    assert s.research_specialists == []
    assert s.watchlist_research_interval_minutes == 360


def test_research_search_cap_is_independent_of_the_chat_knob(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_WEB_SEARCH", "true")
    monkeypatch.setenv("WEB_SEARCH_MAX_USES", "8")
    monkeypatch.setenv("RESEARCH_WEB_SEARCH_MAX_USES", "2")
    chat_tool = build_web_search_tool()
    research_tool = build_web_search_tool(max_uses=get_settings().research_web_search_max_uses)
    assert chat_tool is not None and chat_tool["max_uses"] == 8
    assert research_tool is not None and research_tool["max_uses"] == 2
    assert research_tool["type"] == "web_search_20250305"


def test_research_search_cap_rejects_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEARCH_WEB_SEARCH_MAX_USES", "0")
    with pytest.raises(ValueError):
        get_settings()


def test_specialist_filter_parses_and_keeps_registry_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESEARCH_SPECIALISTS", " coo, CSO ,cfo ")
    assert get_settings().research_specialists == ["coo", "CSO", "cfo"]
    assert er.active_research_specialists() == ("cso", "cfo", "coo")


def test_specialist_filter_drops_unknown_and_never_runs_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESEARCH_SPECIALISTS", "cfo,ceo")
    assert er.active_research_specialists() == ("cfo",)
    monkeypatch.setenv("RESEARCH_SPECIALISTS", "ceo,cto")
    assert er.active_research_specialists() == er.RESEARCH_SPECIALISTS
    monkeypatch.setenv("RESEARCH_SPECIALISTS", "")
    assert er.active_research_specialists() == er.RESEARCH_SPECIALISTS


def test_routing_pass_cannot_start_a_workflow() -> None:
    assert "run_workflow" in er._SYNTHESIS_EXCLUDED_TOOLS
    assert "run_executive_research" in er._SYNTHESIS_EXCLUDED_TOOLS
    # Suggesting one for a human to start is still allowed.
    assert "suggest_workflow" not in er._SYNTHESIS_EXCLUDED_TOOLS


def test_env_example_documents_the_controls() -> None:
    root = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
    with open(os.path.join(root, ".env.example"), encoding="utf-8") as fh:
        text = fh.read()
    for key in ("RESEARCH_WEB_SEARCH_MAX_USES", "RESEARCH_SPECIALISTS", "WATCHLIST_RESEARCH_INTERVAL_MINUTES"):
        assert key in text
