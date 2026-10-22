#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Invoke the db-ops-devops custom agent in an AWS DevOps Agent Agent Space.

Opens a chat, dispatches to the custom agent by name, and streams the reply.

    python invoke_agent.py --agent-space-id <id> --prompt "Inventory the RDS fleet"

A custom agent cannot be targeted by asset ID on SendMessage: that parameter
expects an ATTACHMENT. Dispatch is by name in the message text instead.
"""

from __future__ import annotations

import argparse
import sys

import boto3

AGENT_NAME = "db-ops-devops"
DEFAULT_PROMPT = (
    "Inventory the RDS, Aurora, and DynamoDB fleet and report any CloudWatch "
    "alarms currently in ALARM state."
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--agent-space-id", required=True)
    p.add_argument("--region", default="us-east-1")
    p.add_argument("--profile")
    p.add_argument("--prompt", default=DEFAULT_PROMPT)
    p.add_argument(
        "--agent-name",
        default=AGENT_NAME,
        help=f"Custom agent to dispatch to (default: {AGENT_NAME})",
    )
    p.add_argument(
        "--show-activity",
        action="store_true",
        help="Print tool and reasoning blocks to stderr as they start",
    )
    return p.parse_args()


def client(args: argparse.Namespace):
    session = boto3.Session(profile_name=args.profile) if args.profile else boto3.Session()
    return session.client("devops-agent", region_name=args.region)


def main() -> int:
    args = parse_args()
    c = client(args)

    execution_id = c.create_chat(agentSpaceId=args.agent_space_id)["executionId"]
    print(f"execution: {execution_id}", file=sys.stderr)

    message = f"Run the {args.agent_name} custom agent. {args.prompt}"
    response = c.send_message(
        agentSpaceId=args.agent_space_id,
        executionId=execution_id,
        content=message,
    )

    failed = None
    for event in response["events"]:
        if "contentBlockDelta" in event:
            delta = event["contentBlockDelta"].get("delta", {})
            text = delta.get("textDelta", {}).get("text")
            if text:
                print(text, end="", flush=True)
        elif "contentBlockStart" in event and args.show_activity:
            start = event["contentBlockStart"]
            label = start.get("type") or "block"
            print(f"\n[{label}]", file=sys.stderr, flush=True)
        elif "responseFailed" in event:
            failed = event["responseFailed"]
        elif "summary" in event:
            summary = event["summary"].get("content")
            if summary:
                print(f"\n\n--- summary ---\n{summary}")

    print()
    if failed:
        print(
            f"FAILED {failed.get('errorCode')}: {failed.get('errorMessage')}",
            file=sys.stderr,
        )
        return 1

    print(f"execution: {execution_id}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
