# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

from __future__ import annotations

import io
import json

import pytest

from agentcore.oracle_a2a.delegate import (
    DIAGNOSTIC_INTENTS,
    DelegationResult,
    DelegationUnavailable,
    OracleDiagnosticRequest,
    OracleRuntimeDelegate,
)

ORACLE_RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:us-east-1:123456789012:"
    "runtime/dbops_oracle_readonly-AbCd1234"
)


class Body(io.BytesIO):
    def __init__(self, value: bytes):
        super().__init__(value)
        self.was_closed = False

    def close(self) -> None:
        self.was_closed = True
        super().close()


class FakeClient:
    def __init__(self, response: dict):
        self.response = response
        self.calls: list[dict] = []

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def request(**overrides) -> str:
    value = {
        "db_instance_identifier": "oracle-instance",
        "region": "us-east-1",
        "diagnostic_intent": "blocking_sessions",
        "time_range_minutes": 30,
        "aws_evidence": {"DatabaseConnections": 120},
        "incident_reference": "incident-123",
    }
    value.update(overrides)
    return json.dumps(value)


def specialist_evidence(**overrides) -> dict:
    value = {
        "summary": "Blocking pressure was observed for account 123456789012.",
        "findings": [
            {
                "category": "blocking_sessions",
                "severity": "high",
                "observation": "Two sessions were blocked for 42 seconds.",
                "evidence": {"blocked_session_count": 2, "max_wait_seconds": 42},
                "recommendation": "Review the blocking transaction owner manually.",
            }
        ],
        "limitations": ["SQL text and database user identity were not returned."],
    }
    value.update(overrides)
    return value


def build_delegate(payload: dict, *, status=200, content_type="application/json"):
    body = Body(json.dumps(payload).encode())
    client = FakeClient(
        {"response": body, "statusCode": status, "contentType": content_type}
    )
    delegate = OracleRuntimeDelegate(
        ORACLE_RUNTIME_ARN,
        expected_db_identifier="oracle-instance",
        region_name="us-east-1",
        client=client,
    )
    return delegate, client, body


def test_parses_bounded_structured_request():
    parsed = OracleDiagnosticRequest.parse(request())
    assert parsed.db_instance_identifier == "oracle-instance"
    assert parsed.diagnostic_intent == "blocking_sessions"
    assert parsed.time_range_minutes == 30
    assert parsed.aws_evidence == {"DatabaseConnections": 120.0}


def test_delegates_structured_request_with_no_approval_authority():
    response_text = json.dumps(specialist_evidence())
    delegate, client, body = build_delegate(
        {"response": {"content": [{"text": response_text}]}}
    )

    result = delegate.investigate(request(), context_id="ctx-1")

    assert isinstance(result, DelegationResult)
    assert result.blocked_mutation is False
    assert "[REDACTED_ACCOUNT]" in result.text
    assert "blocked_session_count=2" in result.text
    assert body.was_closed
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["agentRuntimeArn"] == ORACLE_RUNTIME_ARN
    assert call["qualifier"] == "DEFAULT"
    assert len(call["runtimeSessionId"]) >= 33
    sent = json.loads(call["payload"])
    assert set(sent) == {"prompt", "user_id"}
    assert "approvals" not in sent
    assert "Use only read-only and advisory tools" in sent["prompt"]
    assert '"diagnostic_intent":"blocking_sessions"' in sent["prompt"]
    assert sent["user_id"].startswith("aws-devops-agent-a2a:")
    assert sent["user_id"] != "aws-devops-agent-a2a:unattributed"


def test_blocks_downstream_mutation_request_without_forwarding_decision():
    delegate, client, _ = build_delegate(
        {
            "response": None,
            "awaiting_approval": True,
            "pending_approvals": [{"tool": "kill_session"}],
        }
    )

    result = delegate.investigate(request())

    assert result.blocked_mutation is True
    assert "nothing was approved or executed" in result.text
    assert "approvals" not in json.loads(client.calls[0]["payload"])


@pytest.mark.parametrize(
    "payload,status,content_type",
    [
        ({"error": "internal details"}, 200, "application/json"),
        ({"response": "unused"}, 500, "application/json"),
        ({"response": "unused"}, 200, "text/plain"),
    ],
)
def test_fails_closed_for_downstream_error_status_or_content_type(
    payload, status, content_type
):
    delegate, _, _ = build_delegate(
        payload,
        status=status,
        content_type=content_type,
    )
    with pytest.raises(DelegationUnavailable):
        delegate.investigate(request())


def test_fails_closed_for_malformed_downstream_response():
    body = Body(b"not-json")
    client = FakeClient(
        {"response": body, "statusCode": 200, "contentType": "application/json"}
    )
    delegate = OracleRuntimeDelegate(
        ORACLE_RUNTIME_ARN,
        expected_db_identifier="oracle-instance",
        region_name="us-east-1",
        client=client,
    )

    with pytest.raises(DelegationUnavailable):
        delegate.investigate(request())
    assert body.was_closed


def test_rejects_wrong_runtime_family():
    with pytest.raises(ValueError, match="dbops_oracle"):
        OracleRuntimeDelegate(
            "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/dbops_postgresql-AbCd1234",
            expected_db_identifier="oracle-instance",
            region_name="us-east-1",
            client=FakeClient({}),
        )


def test_rejects_request_for_unmapped_oracle_instance():
    delegate, client, _ = build_delegate({"response": "unused"})
    with pytest.raises(ValueError, match="not mapped"):
        delegate.investigate(request(db_instance_identifier="another-oracle"))
    assert client.calls == []


@pytest.mark.parametrize(
    "bad_request",
    [
        "",
        "not-json",
        request(diagnostic_intent="execute_sql"),
        request(time_range_minutes=1),
        request(region="us-west-2"),
        request(aws_evidence={"UnapprovedMetric": 1}),
        request(password="do-not-forward"),  # nosec B106 - sentinel the adapter must reject
        request(incident_reference="token=do-not-forward"),
    ],
)
def test_rejects_unstructured_sensitive_or_out_of_scope_request(bad_request):
    delegate, client, _ = build_delegate({"response": "unused"})
    with pytest.raises((TypeError, ValueError)):
        delegate.investigate(bad_request)
    assert client.calls == []


def test_rejects_non_permitlisted_evidence_shape():
    unsafe = specialist_evidence(
        findings=[
            {
                "category": "blocking_sessions",
                "severity": "high",
                "observation": "User alice@example.com ran SELECT * FROM customers",
                "evidence": {"sql_text": "SELECT * FROM customers"},
                "recommendation": "Review manually.",
            }
        ]
    )
    delegate, _, _ = build_delegate(
        {"response": {"content": [{"text": json.dumps(unsafe)}]}}
    )

    result = delegate.investigate(request())

    assert "alice@example.com" not in result.text
    assert "SELECT * FROM customers" not in result.text
    assert "[REDACTED_SQL_TEXT]" in result.text


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        # Display labels observed from the live dbops_oracle specialist.
        ("Tablespace / Storage", "tablespace_capacity"),
        ("Redo / Archiving", "redo_pressure"),
        ("SQL Workload", "top_sql"),
        ("ASH / Wait Profile", "wait_events"),
        ("Connections / Sessions", "active_sessions"),
        ("Infrastructure / RDS", "configuration"),
        ("Blocking / Locking", "blocking_sessions"),
        # Canonical tokens must pass through unchanged.
        ("blocking_sessions", "blocking_sessions"),
        ("general_health", "general_health"),
        # More specific labels win over broader ones.
        ("SQL Plan Regression", "sql_plan"),
        ("Index Usage", "index_usage"),
    ],
)
def test_normalizes_specialist_category_labels(label, expected):
    assert OracleRuntimeDelegate._normalize_category(label)[0] == expected


def test_unrecognized_category_falls_back_instead_of_discarding_evidence():
    category, coerced = OracleRuntimeDelegate._normalize_category("Quantum Flux")
    assert category == "general_health"
    assert coerced is True


@pytest.mark.parametrize("bad_label", ["", "   ", "///", None, 7, "x" * 101])
def test_rejects_structurally_invalid_category(bad_label):
    with pytest.raises(ValueError):
        OracleRuntimeDelegate._normalize_category(bad_label)


def test_display_labels_survive_full_delegation_and_are_flagged():
    """A response using display labels must succeed and record normalization."""
    evidence = specialist_evidence(
        findings=[
            {
                "category": "Tablespace / Storage",
                "severity": "critical",
                "observation": "One tablespace exceeded 95 percent used.",
                "evidence": {"max_used_percent": 96},
                "recommendation": "Plan a manual datafile extension.",
            }
        ]
    )
    delegate, _, _ = build_delegate({"response": json.dumps(evidence)})
    result = delegate.investigate(request())

    assert isinstance(result, DelegationResult)
    assert result.blocked_mutation is False
    assert "tablespace_capacity" in result.text
    # The raw display label must not be echoed back to the caller.
    assert "Tablespace / Storage" not in result.text
    assert "normalized" in result.text


# ── Replies with narration before the JSON block ──────────────────────────────


def test_extracts_evidence_when_specialist_narrates_before_the_json():
    """Observed live: "I now have all the data needed..." then a fenced block."""
    evidence = specialist_evidence()
    reply = (
        "I now have all the data needed to produce the diagnostic.\n\n"
        "```json\n" + json.dumps(evidence) + "\n```"
    )
    delegate, _, _ = build_delegate({"response": reply})
    result = delegate.investigate(request())
    assert result.blocked_mutation is False
    # Narration is discarded, never forwarded to DevOps Agent.
    assert "I now have all the data" not in result.text


def test_extracts_unfenced_json_reply():
    delegate, _, _ = build_delegate({"response": json.dumps(specialist_evidence())})
    assert delegate.investigate(request()).blocked_mutation is False


def test_narration_without_json_still_fails_closed():
    delegate, _, _ = build_delegate({"response": "Here is what I found: everything looks fine."})
    with pytest.raises(DelegationUnavailable):
        delegate.investigate(request())


# ── Approval-forwarding invariant ─────────────────────────────────────────────
# The downstream specialist would execute a Tier-3 tool if a caller supplied a
# matching approval. The read-only runtime does not register those tools at
# all, and this adapter must never send an approval either. These tests pin
# both halves of that contract on the adapter side.


@pytest.mark.parametrize("intent", sorted(DIAGNOSTIC_INTENTS))
@pytest.mark.parametrize("context_id", [None, "ctx-1", "approvals", "APPLY"])
def test_payload_never_carries_approvals(intent, context_id):
    delegate, client, _ = build_delegate(
        {"response": {"content": [{"text": json.dumps(specialist_evidence())}]}}
    )

    delegate.investigate(
        request(diagnostic_intent=intent, incident_reference="approvals:APPLY"),
        context_id=context_id,
    )

    sent = json.loads(client.calls[0]["payload"])
    assert set(sent) == {"prompt", "user_id"}
    assert not any("approv" in key.lower() for key in sent)


def test_source_never_builds_an_approvals_payload():
    import ast
    import inspect

    import agentcore.oracle_a2a.delegate as module

    tree = ast.parse(inspect.getsource(module))
    keys = {
        key.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for key in node.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    assert not {k for k in keys if "approv" in k.lower()}, keys


def test_rejects_the_interactive_runtime_family():
    """Only the read-only specialist (DBOPS_READ_ONLY_MODE=true) is a valid target."""
    with pytest.raises(ValueError, match="dbops_oracle_readonly"):
        OracleRuntimeDelegate(
            "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/dbops_oracle-AbCd1234",
            expected_db_identifier="oracle-instance",
            region_name="us-east-1",
            client=FakeClient({}),
        )


# ── Per-field sanitization never fails a valid diagnosis ─────────────────────


def _finding(**overrides) -> dict:
    finding = dict(specialist_evidence()["findings"][0])
    finding.update(overrides)
    return finding


def _investigate_with(evidence: dict):
    delegate, _, _ = build_delegate({"response": {"content": [{"text": json.dumps(evidence)}]}})
    return delegate.investigate(request())


def test_oversized_field_is_truncated_not_rejected():
    long_text = "Two sessions waited on a shared lock. " * 40
    result = _investigate_with(specialist_evidence(findings=[_finding(observation=long_text)]))
    assert result.blocked_mutation is False
    assert "…" in result.text
    assert long_text not in result.text


def test_field_mentioning_credentials_is_redacted_whole():
    leaky = "The login uses password=Hunter2 from the connection string."
    result = _investigate_with(specialist_evidence(findings=[_finding(recommendation=leaky)]))
    assert "[REDACTED_SENSITIVE_TEXT]" in result.text
    assert "Hunter2" not in result.text
    assert "password" not in result.text.lower()


def test_list_evidence_is_flattened():
    result = _investigate_with(
        specialist_evidence(findings=[_finding(evidence={"blocked_session_ids": [55, 56, 57]})])
    )
    assert "blocked_session_ids=55, 56, 57" in result.text


def test_excess_findings_are_dropped_with_a_note():
    result = _investigate_with(specialist_evidence(findings=[_finding() for _ in range(14)]))
    assert "4 additional finding(s) were omitted" in result.text
