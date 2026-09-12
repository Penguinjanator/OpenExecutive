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


def test_non_claude_openrouter_models_keep_web_search_but_local_models_do_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The feature gate used to strip the web_search tool for every
    non-Claude model, so a research specialist pinned to e.g. Gemini ran
    without search. OpenRouter's web plugin works for any model it serves,
    so the gate now keeps the tool there; a self-hosted backend has no
    plugin, so it is still stripped."""
    from openexecutive.providers import registry
    from openexecutive.providers.feature_gate import apply_feature_gates
    from openexecutive.providers.translator import to_openai_request

    monkeypatch.setattr(registry.openrouter_catalog, "supports_reasoning", lambda _m: False)
    resolved = registry._openrouter_model_resolver("google/gemini-2.5-flash")
    assert resolved is not None
    slug, spec = resolved
    assert slug == "google/gemini-2.5-flash" and spec.supports_web_search

    monkeypatch.setattr(registry.openrouter_catalog, "supports_reasoning", lambda _m: True)
    _slug, reasoning_spec = registry._openrouter_model_resolver("deepseek/deepseek-v4-pro")
    assert reasoning_spec.supports_web_search and reasoning_spec.supports_thinking

    assert not registry._LOCAL_FEATURE_SPEC.supports_web_search

    # End to end through the gate + translator: OpenRouter's server tool
    # replaces the Anthropic one with the same cap.
    kwargs = {
        "model": "google/gemini-2.5-flash", "max_tokens": 10, "messages": [],
        "tools": [
            {"name": "emit", "input_schema": {"type": "object"}},
            {"type": "web_search_20250305", "name": "web_search", "max_uses": 3},
        ],
    }
    gated = apply_feature_gates(spec, dict(kwargs))
    body = to_openai_request("google/gemini-2.5-flash", gated)
    assert body["tools"][-1] == {"type": "openrouter:web_search", "parameters": {"max_uses": 3}}
    assert [t["function"]["name"] for t in body["tools"][:-1]] == ["emit"]

    stripped = apply_feature_gates(registry._LOCAL_FEATURE_SPEC, dict(kwargs))
    local_body = to_openai_request("local-model", stripped)
    assert all(t["type"] == "function" for t in local_body["tools"])


def test_local_backend_provider_never_carries_web_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard the live provider, not only the constant: a self-hosted
    OpenAI-compatible backend has no search plugin, so its provider must
    resolve every slug — listed or not — to a spec without web search."""
    from types import SimpleNamespace

    from openexecutive.providers import registry

    monkeypatch.setattr(registry, "get_settings", lambda: SimpleNamespace(
        local_models_enabled=True, local_models=["llama3.3"],
        local_base_url="http://127.0.0.1:11434/v1", local_api_key=None, local_timeout_s=30.0,
    ))
    monkeypatch.setattr(registry, "_local_provider", None)
    provider = registry._local()
    for slug in ("llama3.3", "not-in-the-list"):
        _resolved_slug, spec = provider._resolve(slug)
        assert spec.supports_web_search is False
        assert spec.supports_tool_use is True
