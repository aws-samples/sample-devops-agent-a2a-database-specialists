# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

from __future__ import annotations

import io
import json

import pytest

from agentcore.sqlserver_a2a.delegate import (
    DIAGNOSTIC_INTENTS,
    DelegationResult,
    DelegationUnavailable,
    SqlServerDiagnosticRequest,
    SqlServerRuntimeDelegate,
)

SQLSERVER_RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:us-east-1:123456789012:"
    "runtime/dbops_sqlserver_readonly-AbCd1234"
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
        "ec2_instance_id": "i-0123456789abcdef0",
        "region": "us-east-1",
        "diagnostic_intent": "blocking_sessions",
        "time_range_minutes": 30,
        "aws_evidence": {"CPUUtilization": 85},
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
    delegate = SqlServerRuntimeDelegate(
        SQLSERVER_RUNTIME_ARN,
        expected_db_identifier="i-0123456789abcdef0",
        region_name="us-east-1",
        client=client,
    )
    return delegate, client, body


def test_parses_bounded_structured_request():
    parsed = SqlServerDiagnosticRequest.parse(request())
    assert parsed.ec2_instance_id == "i-0123456789abcdef0"
    assert parsed.diagnostic_intent == "blocking_sessions"
    assert parsed.time_range_minutes == 30
    assert parsed.aws_evidence == {"CPUUtilization": 85.0}


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
    assert call["agentRuntimeArn"] == SQLSERVER_RUNTIME_ARN
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
    delegate = SqlServerRuntimeDelegate(
        SQLSERVER_RUNTIME_ARN,
        expected_db_identifier="i-0123456789abcdef0",
        region_name="us-east-1",
        client=client,
    )

    with pytest.raises(DelegationUnavailable):
        delegate.investigate(request())
    assert body.was_closed


def test_rejects_wrong_runtime_family():
    with pytest.raises(ValueError, match="dbops_sqlserver"):
        SqlServerRuntimeDelegate(
            "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/dbops_postgresql-AbCd1234",
            expected_db_identifier="i-0123456789abcdef0",
            region_name="us-east-1",
            client=FakeClient({}),
        )


def test_rejects_request_for_unmapped_sqlserver_host():
    delegate, client, _ = build_delegate({"response": "unused"})
    with pytest.raises(ValueError, match="not mapped"):
        delegate.investigate(request(ec2_instance_id="i-0fedcba9876543210"))
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




# ── SQL Server specific behaviour ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("TempDB Usage", "tempdb_usage"),
        ("File I/O Latency", "file_io"),
        ("Wait Statistics", "wait_events"),
        ("Missing Index Candidates", "missing_indexes"),
        ("Index Usage", "index_usage"),
        ("Memory Clerks", "memory_pressure"),
        ("Buffer Pool", "memory_pressure"),
        ("Backup History", "backup_status"),
        ("EC2 Host", "instance_health"),
        ("Blocking Chains", "blocking_sessions"),
        ("Top Queries", "top_sql"),
        ("Active Requests", "active_sessions"),
        # Canonical tokens pass through unchanged.
        ("tempdb_usage", "tempdb_usage"),
        ("missing_indexes", "missing_indexes"),
    ],
)
def test_normalizes_sqlserver_display_labels(label, expected):
    assert SqlServerRuntimeDelegate._normalize_category(label)[0] == expected


def test_missing_index_label_is_not_collapsed_into_index_usage():
    """The specific group must win over the broader one."""
    category, _ = SqlServerRuntimeDelegate._normalize_category("Missing Index Candidates")
    assert category == "missing_indexes"


@pytest.mark.parametrize(
    "identifier",
    ["my-oracle-db", "i-123", "i-0123456789ABCDEF0", "i-0123456789abcdef00", "i-zzzzzzzz"],
)
def test_requires_a_well_formed_ec2_instance_id(identifier):
    with pytest.raises(ValueError, match="invalid ec2_instance_id"):
        SqlServerDiagnosticRequest.parse(request(ec2_instance_id=identifier))


def test_accepts_legacy_eight_digit_instance_id():
    parsed = SqlServerDiagnosticRequest.parse(request(ec2_instance_id="i-0123abcd"))
    assert parsed.ec2_instance_id == "i-0123abcd"


@pytest.mark.parametrize(
    "payload",
    [
        "Server=10.0.1.5;Uid=sa;Pwd=hunter2",
        "uid = sa",
        "PWD=x",
        "mssql://sa@host",
        "sqlserver://host:1433",
    ],
)
def test_rejects_sqlserver_connection_strings(payload):
    with pytest.raises(ValueError, match="prohibited sensitive field"):
        SqlServerDiagnosticRequest.parse(request(incident_reference=payload))


@pytest.mark.parametrize("metric", ["DatabaseConnections", "FreeableMemory", "ReadLatency"])
def test_rejects_rds_only_evidence_metrics(metric):
    """AWS/RDS metrics do not exist for an EC2 host and must not be accepted."""
    with pytest.raises(ValueError, match="unsupported AWS evidence metric"):
        SqlServerDiagnosticRequest.parse(request(aws_evidence={metric: 1}))


def test_accepts_ec2_namespace_evidence():
    parsed = SqlServerDiagnosticRequest.parse(
        request(aws_evidence={"CPUUtilization": 91.5, "EBSIOBalance%": 12})
    )
    assert parsed.aws_evidence == {"CPUUtilization": 91.5, "EBSIOBalance%": 12.0}


@pytest.mark.parametrize("intent", ["tempdb_usage", "file_io", "memory_pressure", "missing_indexes", "backup_status"])
def test_accepts_sqlserver_specific_intents(intent):
    assert SqlServerDiagnosticRequest.parse(request(diagnostic_intent=intent)).diagnostic_intent == intent


@pytest.mark.parametrize("intent", ["tablespace_capacity", "redo_pressure"])
def test_rejects_oracle_only_intents(intent):
    with pytest.raises(ValueError, match="unsupported diagnostic_intent"):
        SqlServerDiagnosticRequest.parse(request(diagnostic_intent=intent))


def test_instruction_names_every_sqlserver_mutating_tool():
    """The downstream instruction must forbid each Tier-3 tool the specialist has."""
    from agentcore.sqlserver_a2a.delegate import READ_ONLY_INSTRUCTION

    for tool in (
        "kill_session",
        "create_index",
        "update_statistics",
        "execute_sql",
        "modify_parameter",
        "trigger_backup",
    ):
        assert tool in READ_ONLY_INSTRUCTION
    assert "gather_table_stats" not in READ_ONLY_INSTRUCTION


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

    import agentcore.sqlserver_a2a.delegate as module

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
    with pytest.raises(ValueError, match="dbops_sqlserver_readonly"):
        SqlServerRuntimeDelegate(
            "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/dbops_sqlserver-AbCd1234",
            expected_db_identifier="i-0123456789abcdef0",
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
