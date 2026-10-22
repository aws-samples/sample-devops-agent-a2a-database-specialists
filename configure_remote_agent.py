#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Register an engine's AgentCore A2A runtime with an AWS DevOps Agent space.

Each database engine is a separate remote agent. ``--engine`` selects which
one; the runtime ARN must belong to that engine's adapter, so an Oracle
registration cannot be pointed at the SQL Server runtime or the reverse.

The default mode prints the exact RegisterService and AssociateService payloads
without changing AWS. Pass --apply only after the runtime and IAM role have been
reviewed and deployed.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import time
from typing import Any
from urllib.parse import quote

import boto3

HERE = pathlib.Path(__file__).parent
ENGINES = {
    "oracle": "oracle_db_troubleshooter_a2a",
    "sqlserver": "sqlserver_db_troubleshooter_a2a",
}
DEFAULT_ENGINE = "oracle"


def config_path(engine: str) -> pathlib.Path:
    # Validate before building a path: the engine name becomes part of a
    # filename, so an unchecked value could point outside remote_agents/.
    if engine not in ENGINES:
        raise ValueError(f"unsupported engine {engine!r}; expected one of {sorted(ENGINES)}")
    return HERE / "remote_agents" / f"{engine}.json"


def runtime_arn_pattern(engine: str) -> re.Pattern[str]:
    if engine not in ENGINES:
        raise ValueError(f"unsupported engine {engine!r}; expected one of {sorted(ENGINES)}")
    return re.compile(
        r"^arn:(aws|aws-us-gov|aws-cn):bedrock-agentcore:([a-z0-9-]+):(\d{12}):"
        rf"runtime/{ENGINES[engine]}-[A-Za-z0-9]+$"
    )


# Kept for callers that predate --engine.
CONFIG_PATH = config_path(DEFAULT_ENGINE)
RUNTIME_ARN_PATTERN = runtime_arn_pattern(DEFAULT_ENGINE)
ROLE_ARN_PATTERN = re.compile(
    r"^arn:(aws|aws-us-gov|aws-cn):iam::(\d{12}):role/[A-Za-z0-9+=,.@_/-]+$"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=sorted(ENGINES), default=DEFAULT_ENGINE)
    parser.add_argument("--agent-space-id", required=True)
    parser.add_argument("--agent-runtime-arn", required=True)
    parser.add_argument("--role-arn", required=True)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--profile")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--allow-cross-account",
        action="store_true",
        help="Allow the invocation role to live in a different account from the "
        "runtime (operations account calling a workload account). The runtime "
        "then needs a resource policy that allows the role.",
    )
    return parser.parse_args()


def load_config(engine: str = DEFAULT_ENGINE) -> dict[str, str]:
    path = config_path(engine)
    value = json.loads(path.read_text())
    required = {"name", "serviceType", "description", "signingService"}
    if not required.issubset(value):
        raise ValueError(f"{path} is missing required fields")
    if value["serviceType"] != "remoteagentsigv4":
        raise ValueError(f"{engine} remote agent must use remoteagentsigv4")
    if value["signingService"] != "bedrock-agentcore":
        raise ValueError(f"{engine} remote agent must use bedrock-agentcore signing")
    return value


def agent_card_url(runtime_arn: str, region: str, engine: str = DEFAULT_ENGINE) -> str:
    match = runtime_arn_pattern(engine).fullmatch(runtime_arn)
    if not match:
        raise ValueError(f"agent runtime ARN must identify {ENGINES[engine]}")
    arn_region = match.group(2)
    if arn_region != region:
        raise ValueError("runtime ARN region does not match --region")
    return (
        f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/"
        f"{quote(runtime_arn, safe='')}/invocations/.well-known/agent-card.json"
    )


def validate_role(
    runtime_arn: str,
    role_arn: str,
    engine: str = DEFAULT_ENGINE,
    *,
    allow_cross_account: bool = False,
) -> None:
    runtime = runtime_arn_pattern(engine).fullmatch(runtime_arn)
    role = ROLE_ARN_PATTERN.fullmatch(role_arn)
    if not role:
        raise ValueError("--role-arn must be an IAM role ARN")
    if runtime and runtime.group(3) != role.group(2) and not allow_cross_account:
        raise ValueError("runtime and role must belong to the same AWS account")


def desired_payloads(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    engine = getattr(args, "engine", DEFAULT_ENGINE) or DEFAULT_ENGINE
    config = load_config(engine)
    validate_role(
        args.agent_runtime_arn,
        args.role_arn,
        engine,
        allow_cross_account=bool(getattr(args, "allow_cross_account", False)),
    )
    endpoint = agent_card_url(args.agent_runtime_arn, args.region, engine)
    register = {
        "service": config["serviceType"],
        "serviceDetails": {
            "remoteagentsigv4": {
                "name": config["name"],
                "endpoint": endpoint,
                "description": config["description"],
                "authorizationConfig": {
                    "region": args.region,
                    "service": config["signingService"],
                    "roleArn": args.role_arn,
                },
            }
        },
        "name": config["name"],
    }
    associate = {
        "agentSpaceId": args.agent_space_id,
        "serviceId": "<service-id-from-register-or-existing>",
        "configuration": {"remoteagentsigv4": {}},
    }
    return register, associate


def find_service(client, name: str) -> dict[str, Any] | None:
    token = None
    while True:
        request: dict[str, Any] = {
            "filterServiceType": "remoteagentsigv4",
            "maxResults": 50,
        }
        if token:
            request["nextToken"] = token
        response = client.list_services(**request)
        for item in response.get("services", []):
            detail = client.get_service(serviceId=item["serviceId"])["service"]
            remote = (detail.get("additionalServiceDetails") or {}).get(
                "remoteagentsigv4"
            ) or {}
            if remote.get("name") == name:
                return detail
        token = response.get("nextToken")
        if not token:
            return None


def find_association(
    client, agent_space_id: str, service_id: str
) -> dict[str, Any] | None:
    token = None
    while True:
        request: dict[str, Any] = {
            "agentSpaceId": agent_space_id,
            "filterServiceTypes": "remoteagentsigv4",
            "maxResults": 50,
        }
        if token:
            request["nextToken"] = token
        response = client.list_associations(**request)
        for item in response.get("associations", []):
            if item.get("serviceId") == service_id:
                return item
        token = response.get("nextToken")
        if not token:
            return None


def wait_for_valid_association(
    client,
    agent_space_id: str,
    association: dict[str, Any],
    *,
    attempts: int = 10,
) -> dict[str, Any]:
    current = association
    for _ in range(attempts):
        status = current.get("status")
        # Remote-agent associations may carry no status field at all; the
        # association exists, and there is nothing further to wait for.
        if status in {"valid", None}:
            return current
        if status not in {"pending", "pending-confirmation", "validating"}:
            raise RuntimeError(f"remote-agent association status is {status!r}")
        time.sleep(2)
        current = client.get_association(
            agentSpaceId=agent_space_id,
            associationId=current["associationId"],
        )["association"]
    raise RuntimeError("remote-agent association did not become valid")


def assert_existing_matches(existing: dict[str, Any], register: dict[str, Any]) -> None:
    actual = (existing.get("additionalServiceDetails") or {}).get(
        "remoteagentsigv4"
    ) or {}
    expected = register["serviceDetails"]["remoteagentsigv4"]
    comparable = ("name", "endpoint", "description", "region", "service", "roleArn")
    flattened_expected = {
        "name": expected["name"],
        "endpoint": expected["endpoint"],
        "description": expected["description"],
        **expected["authorizationConfig"],
    }
    differences = [
        key for key in comparable if actual.get(key) != flattened_expected.get(key)
    ]
    if differences:
        raise RuntimeError(
            "a remote agent with this name is already registered with different "
            f"configuration fields: {', '.join(differences)}; review and "
            "replace it manually rather than deregistering automatically"
        )


def main() -> int:
    args = parse_args()
    register, associate = desired_payloads(args)
    print("RegisterService payload:")
    print(json.dumps(register, indent=2))
    print("AssociateService payload:")
    print(json.dumps(associate, indent=2))
    if not args.apply:
        print("Plan only. Re-run with --apply after reviewing the runtime and role.")
        return 0

    session = (
        boto3.Session(profile_name=args.profile) if args.profile else boto3.Session()
    )
    client = session.client("devops-agent", region_name=args.region)
    name = register["name"]
    existing = find_service(client, name)
    if existing:
        assert_existing_matches(existing, register)
        service_id = existing["serviceId"]
        print(f"Reusing remote agent service {service_id}")
    else:
        try:
            service_id = client.register_service(**register)["serviceId"]
            print(f"Registered remote agent service {service_id}")
        except client.exceptions.ConflictException:
            # RegisterService has no idempotency token. Recover a concurrent
            # create only when the resulting service matches every desired field.
            existing = find_service(client, name)
            if not existing:
                raise
            assert_existing_matches(existing, register)
            service_id = existing["serviceId"]
            print(f"Reusing concurrently registered service {service_id}")

    association = find_association(client, args.agent_space_id, service_id)
    if association:
        association = wait_for_valid_association(
            client,
            args.agent_space_id,
            association,
        )
        print(f"Remote agent is already associated as {association['associationId']}")
    else:
        associate["serviceId"] = service_id
        result = client.associate_service(**associate)
        association = wait_for_valid_association(
            client,
            args.agent_space_id,
            result["association"],
        )
        print(f"Associated remote agent as {association['associationId']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
