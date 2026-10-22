# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A2A adapter that delegates read-only troubleshooting to AI-DB-OPS Oracle."""

from __future__ import annotations

import asyncio
import json
import os

import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    InternalError,
    InvalidParamsError,
    TaskNotCancelableError,
)
from a2a.utils import new_agent_text_message
from a2a.utils.errors import ServerError
from bedrock_agentcore.runtime import build_a2a_app
from fastapi.responses import JSONResponse

try:
    from .delegate import (
        ALLOWED_AWS_EVIDENCE,
        DIAGNOSTIC_INTENTS,
        DelegationUnavailable,
        OracleRuntimeDelegate,
    )
except ImportError:  # Direct AgentCore entrypoint execution from this directory.
    from delegate import (  # type: ignore[no-redef]
        ALLOWED_AWS_EVIDENCE,
        DIAGNOSTIC_INTENTS,
        DelegationUnavailable,
        OracleRuntimeDelegate,
    )

AGENT_NAME = "oracle-db-troubleshooter"
AGENT_VERSION = "0.2.0"
DEFAULT_RUNTIME_URL = "http://localhost:9000/"


# The exact request contract, stated in the Agent Card skill and in the
# validation error so a calling agent can form a valid request. Built only
# from constants: nothing from the rejected request is echoed back.
REQUEST_SCHEMA_HINT = (
    "Send exactly one JSON object and nothing else, with keys "
    "db_instance_identifier (string), region (string, e.g. us-east-1), "
    "diagnostic_intent (one of: " + ", ".join(sorted(DIAGNOSTIC_INTENTS)) + "), "
    "time_range_minutes (integer 5-1440), and optionally aws_evidence "
    "(object of numeric AWS/RDS metrics: " + ", ".join(sorted(ALLOWED_AWS_EVIDENCE)) + ") "
    "and incident_reference (short id). No other keys, no free text, no SQL."
)

class OracleA2AExecutor(AgentExecutor):
    """Translate an A2A diagnostic request into one Oracle specialist call."""

    def __init__(self, delegate: OracleRuntimeDelegate) -> None:
        self._delegate = delegate

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        request_text = context.get_user_input().strip()
        try:
            result = await asyncio.to_thread(
                self._delegate.investigate,
                request_text,
                context_id=context.context_id,
            )
        except (TypeError, ValueError) as error:
            raise ServerError(
                error=InvalidParamsError(
                    message=f"Invalid Oracle diagnostic request. {REQUEST_SCHEMA_HINT}"
                )
            ) from error
        except DelegationUnavailable as error:
            raise ServerError(
                error=InternalError(message="Oracle specialist unavailable")
            ) from error

        await event_queue.enqueue_event(
            new_agent_text_message(
                result.text,
                context_id=context.context_id,
                task_id=context.task_id,
            )
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise ServerError(error=TaskNotCancelableError())


def runtime_url(value: str | None = None) -> str:
    return (
        value or os.environ.get("AGENTCORE_RUNTIME_URL") or DEFAULT_RUNTIME_URL
    ).rstrip("/") + "/"


def build_agent_card(value: str | None = None) -> AgentCard:
    """Build the A2A 0.3 card used by AgentCore's JSON-RPC handler."""

    url = runtime_url(value)
    example = json.dumps(
        {
            "db_instance_identifier": "oracle-instance",
            "region": "us-east-1",
            "diagnostic_intent": "blocking_sessions",
            "time_range_minutes": 30,
            "aws_evidence": {"DatabaseConnections": 120},
            "incident_reference": "incident-123",
        },
        separators=(",", ":"),
    )
    return AgentCard(
        name=AGENT_NAME,
        description=(
            "Read-only RDS for Oracle troubleshooting through the existing "
            "AI-DB-OPS Oracle specialist. It gathers engine-internal aggregate "
            "evidence and never approves or executes database changes."
        ),
        url=url,
        version=AGENT_VERSION,
        protocolVersion="0.3.0",
        preferredTransport="JSONRPC",
        additionalInterfaces=[AgentInterface(transport="JSONRPC", url=url)],
        capabilities=AgentCapabilities(streaming=False),
        defaultInputModes=["application/json"],
        defaultOutputModes=["text/plain"],
        skills=[
            AgentSkill(
                id="rds-oracle-troubleshooting",
                name="RDS Oracle troubleshooting",
                description=(
                    "Investigate "
                    "RDS Oracle sessions, waits, blocking, tablespaces, redo, "
                    "top SQL aggregates, index usage, SQL plans, or instance state."
                    + " " + REQUEST_SCHEMA_HINT
                ),
                tags=[
                    "amazon-rds",
                    "oracle",
                    "database",
                    "troubleshooting",
                    "read-only",
                ],
                examples=[example],
            )
        ],
    )


def serialized_agent_card(card: AgentCard) -> dict:
    """Add the interface field required by the current DevOps Agent contract.

    AgentCore 1.19 uses a2a-sdk 0.3, which serializes `url` plus
    `additionalInterfaces`. DevOps Agent documentation uses the A2A 1.0
    `supportedInterfaces` discovery field. Serving both is forward-compatible:
    A2A clients ignore unrecognized fields, and the JSON-RPC endpoint remains
    protocol version 0.3.
    """

    value = card.model_dump(by_alias=True, exclude_none=True)
    value["supportedInterfaces"] = [
        {
            "url": card.url,
            "protocolBinding": "JSONRPC",
            "protocolVersion": "0.3",
        }
    ]
    return value


def build_app(delegate: OracleRuntimeDelegate, value: str | None = None):
    """Build the AgentCore A2A ASGI app with a compatibility discovery card."""

    url = runtime_url(value)
    card = build_agent_card(url)
    app = build_a2a_app(
        OracleA2AExecutor(delegate),
        card,
        runtime_url=url,
    )

    card_paths = {"/.well-known/agent-card.json", "/.well-known/agent.json"}
    app.router.routes = [
        route for route in app.router.routes if getattr(route, "path", None) not in card_paths
    ]

    async def get_card(request) -> JSONResponse:
        return JSONResponse(serialized_agent_card(card))

    for path in sorted(card_paths):
        app.add_route(path, get_card, methods=["GET", "HEAD"])
    return app


def main() -> None:
    delegate = OracleRuntimeDelegate.from_environment()
    host = "0.0.0.0" if os.path.exists("/.dockerenv") else "127.0.0.1"  # nosec B104
    uvicorn.run(build_app(delegate), host=host, port=9000, log_level="info")


if __name__ == "__main__":
    main()
