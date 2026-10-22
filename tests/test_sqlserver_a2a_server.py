# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from agentcore.sqlserver_a2a.delegate import DelegationResult, DelegationUnavailable
from agentcore.sqlserver_a2a.main import build_agent_card, build_app, serialized_agent_card


def valid_request() -> str:
    return json.dumps(
        {
            "ec2_instance_id": "i-0123456789abcdef0",
            "region": "us-east-1",
            "diagnostic_intent": "wait_events",
            "time_range_minutes": 30,
            "aws_evidence": {"CPUUtilization": 82.5},
        }
    )


class FakeDelegate:
    def __init__(self, *, unavailable: bool = False):
        self.calls: list[tuple[str, str | None]] = []
        self.unavailable = unavailable

    def investigate(self, request_text: str, *, context_id: str | None = None):
        self.calls.append((request_text, context_id))
        if request_text == "not-json":
            raise ValueError("invalid request")
        if self.unavailable:
            raise DelegationUnavailable("downstream unavailable")
        return DelegationResult("SQL Server evidence: wait pressure observed")


def request_payload(text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "request-1",
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": text}],
                "messageId": "12345678-1234-1234-1234-123456789012",
            }
        },
    }


def build_client(delegate: FakeDelegate) -> TestClient:
    return TestClient(build_app(delegate, "http://testserver/"))


def test_agent_card_exposes_devops_agent_supported_interfaces():
    card = build_agent_card("https://example.test/sqlserver/")
    value = serialized_agent_card(card)

    assert value["name"] == "sqlserver-db-troubleshooter"
    assert value["url"] == "https://example.test/sqlserver/"
    assert value["capabilities"]["streaming"] is False
    assert value["preferredTransport"] == "JSONRPC"
    assert value["supportedInterfaces"] == [
        {
            "url": "https://example.test/sqlserver/",
            "protocolBinding": "JSONRPC",
            "protocolVersion": "0.3",
        }
    ]
    assert value["defaultInputModes"] == ["application/json"]
    assert len(value["skills"]) == 1
    skill = value["skills"][0]
    assert skill["id"] == "ec2-sqlserver-troubleshooting"
    assert "read-only" in skill["tags"]
    assert "Oracle" not in skill["description"]
    assert json.loads(skill["examples"][0])["diagnostic_intent"] == "blocking_sessions"


def test_health_card_and_message_send_contract():
    delegate = FakeDelegate()
    client = build_client(delegate)

    health = client.get("/ping")
    card = client.get("/.well-known/agent-card.json")
    response = client.post("/", json=request_payload(valid_request()))

    assert health.status_code == 200
    assert health.json()["status"] == "Healthy"
    assert card.status_code == 200
    assert card.json()["name"] == "sqlserver-db-troubleshooter"
    assert "supportedInterfaces" in card.json()
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["kind"] == "message"
    assert result["role"] == "agent"
    assert result["parts"][0]["text"] == "SQL Server evidence: wait pressure observed"
    assert len(delegate.calls) == 1
    assert json.loads(delegate.calls[0][0])["diagnostic_intent"] == "wait_events"
    assert delegate.calls[0][1]


def test_invalid_a2a_message_returns_jsonrpc_invalid_params():
    delegate = FakeDelegate()
    client = build_client(delegate)

    response = client.post("/", json=request_payload("not-json"))

    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32602
    assert delegate.calls == [("not-json", delegate.calls[0][1])]


def test_downstream_failure_returns_jsonrpc_internal_error():
    delegate = FakeDelegate(unavailable=True)
    client = build_client(delegate)

    response = client.post("/", json=request_payload(valid_request()))

    assert response.status_code == 200
    assert response.json()["error"] == {
        "code": -32603,
        "message": "SQL Server specialist unavailable",
    }
