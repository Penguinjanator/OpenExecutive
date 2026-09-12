"""Every model call site records a ``cache_event`` usage row: specialist
tool calls, triage, and the chat memory extractor (the Executive's own
turns and the research loops are covered by their module tests)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openexecutive.agents.base import BaseAgent
from openexecutive.audit.logger import AuditLogger, set_audit_logger


@pytest.fixture
def audit(tmp_path: Path) -> AuditLogger:
    logger = AuditLogger(tmp_path / "audit.db")
    set_audit_logger(logger)
    yield logger  # type: ignore[misc]
    set_audit_logger(None)


def _response(**usage: object) -> SimpleNamespace:
    return SimpleNamespace(
        content=[], stop_reason="end_turn", usage=SimpleNamespace(**usage),
    )


class _Agent(BaseAgent):
    name = "unit_agent"
    domain = "unit"
    model = "claude-test"

    def get_system_prompt(self) -> str:
        return "prompt"


def test_analyze_with_tools_records_usage_under_the_callers_actor(
    audit: AuditLogger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("openexecutive.agents.overrides.get_override", lambda _name: None)
    provider = SimpleNamespace(messages_create=AsyncMock(return_value=_response(
        input_tokens=70, output_tokens=9,
        server_tool_use=SimpleNamespace(web_search_requests=3),
    )))
    monkeypatch.setattr("openexecutive.agents.base.get_provider", lambda _m: provider)

    asyncio.run(_Agent().analyze_with_tools(
        "ctx", tools=[{"name": "t", "input_schema": {"type": "object"}}],
        model_override="claude-research", actor="specialist_research",
    ))
    rows = audit.query(event_type="cache_event")
    assert len(rows) == 1
    assert rows[0].actor == "specialist_research"
    assert rows[0].details["model"] == "claude-research"
    assert rows[0].details["web_search_requests"] == 3


def test_triage_records_usage(audit: AuditLogger) -> None:
    from openexecutive.agents.triage import TriageAgent
    from openexecutive.alerts.models import AlertEvent

    block = SimpleNamespace(type="tool_use", name="emit_alert_decision", input={
        "alert": False, "severity": "low", "channels": ["persisted"], "headline": "h",
        "body": "b", "suggested_action": "", "topic_tags": [], "dedup_key": "k",
        "reason_if_suppressed": "noise",
    })
    response = SimpleNamespace(content=[block], stop_reason="tool_use",
                               usage=SimpleNamespace(input_tokens=33, output_tokens=5))
    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=response)))
    asyncio.run(TriageAgent().triage(
        AlertEvent(source="email", external_id="m1", subject="s", body="b"), client=client,
    ))
    rows = audit.query(event_type="cache_event")
    assert len(rows) == 1 and rows[0].actor == "triage"
    assert rows[0].details["input_tokens"] == 33


def test_memory_extractor_records_usage(
    audit: AuditLogger, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.memory import episodic as ep

    db = tmp_path / "episodic.db"
    ep.initialize_db(db)
    provider = SimpleNamespace(messages_create=AsyncMock(return_value=_response(
        input_tokens=12, output_tokens=2,
    )))
    monkeypatch.setattr("openexecutive.providers.get_provider", lambda _m: provider)
    monkeypatch.setattr(
        "openexecutive.config.get_settings", lambda: SimpleNamespace(routing_model="claude-test"),
    )
    asyncio.run(ep.extract_and_store("user message", "assistant response", db_path=db))
    rows = audit.query(event_type="cache_event")
    assert len(rows) == 1 and rows[0].actor == "memory_extractor"
    assert rows[0].details["model"] == "claude-test"
