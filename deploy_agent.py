#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Create or update the db-ops-devops custom agent in an AWS DevOps Agent space.

A custom agent is stored as an asset of type `custom_agent`: the system prompt is
a file in the asset, and the tool and skill assignments live in the asset
metadata. This script makes that deployment repeatable and reviewable.

The AWS DevOps Agent user guide states that tools can only be assigned through
Chat. That is not accurate for the API: `UpdateAsset` accepts a `tools` list in
metadata, which is what this script uses.

Usage:
    python deploy_agent.py --agent-space-id <id> --region us-east-1
    python deploy_agent.py --agent-space-id <id> --region us-east-1 --dry-run
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

import boto3

HERE = pathlib.Path(__file__).parent


def load_config() -> tuple[dict, str]:
    config = json.loads((HERE / "agent.json").read_text())
    prompt = (HERE / config["systemPromptFile"]).read_text()
    if len(prompt) < 10:
        raise SystemExit("system prompt must be at least 10 characters")
    if len(prompt) > 50_000:
        raise SystemExit("system prompt exceeds the 50,000 character limit")
    return config, prompt


def find_existing(client, agent_space_id: str, name: str) -> str | None:
    paginator = {"agentSpaceId": agent_space_id, "assetType": "custom_agent"}
    while True:
        response = client.list_assets(**paginator)
        for item in response.get("items", []):
            if (item.get("metadata") or {}).get("name") == name:
                return item["assetId"]
        token = response.get("nextToken")
        if not token:
            return None
        paginator["nextToken"] = token


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-space-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config, prompt = load_config()
    metadata = {
        "name": config["name"],
        "status": config.get("status", "ACTIVE"),
        "tools": config.get("tools", []),
        "skills": config.get("skills", []),
    }

    print(f"agent:  {metadata['name']}")
    print(f"prompt: {len(prompt)} characters")
    print(f"tools:  {', '.join(metadata['tools']) or 'none'}")
    if args.dry_run:
        print("dry run: no changes made")
        return 0

    session = boto3.Session(profile_name=args.profile) if args.profile else boto3.Session()
    client = session.client("devops-agent", region_name=args.region)

    content = {"file": {"path": config["systemPromptFile"], "body": {"text": prompt}}}
    existing = find_existing(client, args.agent_space_id, metadata["name"])

    if existing:
        result = client.update_asset(
            agentSpaceId=args.agent_space_id,
            assetId=existing,
            metadata=metadata,
            content=content,
        )
        print(f"updated {existing} to version {result['asset']['version']}")
    else:
        result = client.create_asset(
            agentSpaceId=args.agent_space_id,
            assetType="custom_agent",
            metadata=metadata,
            content=content,
        )
        print(f"created {result['asset']['assetId']}")

    print(json.dumps(result["asset"]["metadata"], indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
