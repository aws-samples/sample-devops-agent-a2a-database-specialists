# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

from __future__ import annotations

from argparse import Namespace

import pytest
from botocore.session import get_session
from botocore.validate import validate_parameters

from configure_remote_agent import (
    agent_card_url,
    desired_payloads,
    wait_for_valid_association,
)

A2A_RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:us-east-1:123456789012:"
    "runtime/oracle_db_troubleshooter_a2a-AbCd1234"
)
ROLE_ARN = "arn:aws:iam::123456789012:role/DevOpsAgentOracleA2AInvoke"


def arguments(**overrides) -> Namespace:
    values = {
        "agent_space_id": "11111111-2222-3333-4444-555555555555",
        "agent_runtime_arn": A2A_RUNTIME_ARN,
        "role_arn": ROLE_ARN,
        "region": "us-east-1",
        "profile": None,
        "apply": False,
    }
    values.update(overrides)
    return Namespace(**values)


def test_builds_sigv4_remote_agent_registration_and_association():
    register, associate = desired_payloads(arguments())

    assert register["service"] == "remoteagentsigv4"
    remote = register["serviceDetails"]["remoteagentsigv4"]
    assert remote["name"] == "oracle-db-troubleshooter"
    assert remote["authorizationConfig"] == {
        "region": "us-east-1",
        "service": "bedrock-agentcore",
        "roleArn": ROLE_ARN,
    }
    assert "%3A" in remote["endpoint"]
    assert remote["endpoint"].endswith(
        "/invocations/.well-known/agent-card.json"
    )
    assert associate == {
        "agentSpaceId": "11111111-2222-3333-4444-555555555555",
        "serviceId": "<service-id-from-register-or-existing>",
        "configuration": {"remoteagentsigv4": {}},
    }

    service = get_session().get_service_model("devops-agent")
    validate_parameters(
        register,
        service.operation_model("RegisterService").input_shape,
    )
    validate_parameters(
        {**associate, "serviceId": "service-123"},
        service.operation_model("AssociateService").input_shape,
    )


def test_rejects_runtime_from_different_region():
    with pytest.raises(ValueError, match="region"):
        agent_card_url(A2A_RUNTIME_ARN, "us-west-2")


def test_rejects_role_from_different_account():
    with pytest.raises(ValueError, match="same AWS account"):
        desired_payloads(
            arguments(
                role_arn="arn:aws:iam::999999999999:role/DevOpsAgentOracleA2AInvoke"
            )
        )


def test_allows_role_from_different_account_only_when_explicit():
    cross = "arn:aws:iam::999999999999:role/DevOpsAgentOracleA2AInvoke"
    register, _ = desired_payloads(arguments(role_arn=cross, allow_cross_account=True))
    assert register["serviceDetails"]["remoteagentsigv4"]["authorizationConfig"]["roleArn"] == cross


def test_rejects_non_oracle_a2a_runtime():
    bad = (
        "arn:aws:bedrock-agentcore:us-east-1:123456789012:"
        "runtime/sqlserver_db_troubleshooter_a2a-AbCd1234"
    )
    with pytest.raises(ValueError, match="oracle_db_troubleshooter_a2a"):
        desired_payloads(arguments(agent_runtime_arn=bad))


class AssociationClient:
    def __init__(self, statuses):
        self.statuses = iter(statuses)
        self.calls = 0

    def get_association(self, **kwargs):
        self.calls += 1
        return {
            "association": {
                "associationId": kwargs["associationId"],
                "status": next(self.statuses),
            }
        }


def test_waits_for_remote_agent_association_to_become_valid(monkeypatch):
    monkeypatch.setattr("configure_remote_agent.time.sleep", lambda _: None)
    client = AssociationClient(["valid"])

    result = wait_for_valid_association(
        client,
        "space-1",
        {"associationId": "assoc-1", "status": "pending-confirmation"},
        attempts=2,
    )

    assert result["status"] == "valid"
    assert client.calls == 1


def test_rejects_invalid_existing_association():
    with pytest.raises(RuntimeError, match="invalid"):
        wait_for_valid_association(
            AssociationClient([]),
            "space-1",
            {"associationId": "assoc-1", "status": "invalid"},
            attempts=1,
        )


# ── Engine selection ──────────────────────────────────────────────────────────

SQLSERVER_RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:us-east-1:123456789012:"
    "runtime/sqlserver_db_troubleshooter_a2a-AbCd1234"
)


def test_builds_sqlserver_registration_when_engine_selected():
    register, _ = desired_payloads(
        arguments(engine="sqlserver", agent_runtime_arn=SQLSERVER_RUNTIME_ARN)
    )
    remote = register["serviceDetails"]["remoteagentsigv4"]
    assert remote["name"] == "sqlserver-db-troubleshooter"
    assert register["name"] == "sqlserver-db-troubleshooter"
    assert "sqlserver_db_troubleshooter_a2a" in remote["endpoint"]
    assert remote["authorizationConfig"]["service"] == "bedrock-agentcore"

    service = get_session().get_service_model("devops-agent")
    validate_parameters(register, service.operation_model("RegisterService").input_shape)


def test_sqlserver_engine_rejects_the_oracle_runtime():
    """Registering SQL Server against the Oracle adapter would misroute questions."""
    with pytest.raises(ValueError, match="sqlserver_db_troubleshooter_a2a"):
        desired_payloads(arguments(engine="sqlserver", agent_runtime_arn=A2A_RUNTIME_ARN))


def test_engine_defaults_to_oracle_for_existing_callers():
    """Namespaces built before --engine existed keep Oracle behaviour."""
    register, _ = desired_payloads(arguments())
    assert register["name"] == "oracle-db-troubleshooter"


def test_rejects_unknown_engine():
    with pytest.raises(ValueError, match="unsupported engine"):
        desired_payloads(arguments(engine="db2"))
