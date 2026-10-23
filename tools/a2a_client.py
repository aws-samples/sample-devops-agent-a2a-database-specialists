#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Call a deployed AgentCore A2A adapter directly with SigV4.

This exercises exactly the hop AWS DevOps Agent makes once a remote agent is
registered: fetch the Agent Card, then send a JSON-RPC ``message/send`` whose
single text part is the structured diagnostic request. Use it to verify a
deployment, and to demonstrate a specialist in an account where remote-agent
registration is not yet enabled.

Examples::

    tools/a2a_client.py card --runtime-arn <arn>
    tools/a2a_client.py ask --runtime-arn <arn> --request '{"ec2_instance_id": ...}'
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

SIGNING_SERVICE = "bedrock-agentcore"
REGION_PATTERN = re.compile(r"^[a-z]{2}(-[a-z]+)+-[0-9]$")
ALLOWED_HOST_PATTERN = re.compile(r"^bedrock-agentcore\.[a-z]{2}(-[a-z]+)+-[0-9]\.amazonaws\.com$")


def runtime_base_url(runtime_arn: str, region: str) -> str:
    if not REGION_PATTERN.fullmatch(region):
        raise SystemExit(f"Not an AWS Region name: {region!r}")
    encoded = urllib.parse.quote(runtime_arn, safe="")
    return f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded}/invocations"


def signed_request(
    session: boto3.Session,
    region: str,
    method: str,
    url: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 120,
) -> tuple[int, str]:
    """Send one SigV4-signed request and return (status, body text)."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not ALLOWED_HOST_PATTERN.fullmatch(parsed.hostname or ""):
        raise SystemExit("Refusing to sign a request for a host other than the AgentCore endpoint")
    credentials = session.get_credentials()
    if credentials is None:
        raise SystemExit("No AWS credentials available for the selected profile")
    request = AWSRequest(method=method, url=url, data=body, headers=headers or {})
    SigV4Auth(credentials.get_frozen_credentials(), SIGNING_SERVICE, region).add_auth(request)
    http_request = urllib.request.Request(url, data=body, method=method)
    for key, value in request.headers.items():
        http_request.add_header(key, value)
    try:
        # The scheme and host were checked above: https to the AgentCore endpoint only.
        with urllib.request.urlopen(http_request, timeout=timeout) as response:  # nosec B310 # noqa: S310 # nosemgrep
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


def fetch_card(session: boto3.Session, runtime_arn: str, region: str) -> dict:
    url = runtime_base_url(runtime_arn, region) + "/.well-known/agent-card.json"
    status, text = signed_request(session, region, "GET", url, headers={"Accept": "application/json"})
    if status != 200:
        raise SystemExit(f"Agent Card request failed: HTTP {status}: {text[:400]}")
    return json.loads(text)


def send_message(session: boto3.Session, runtime_arn: str, region: str, request_text: str) -> dict:
    url = runtime_base_url(runtime_arn, region) + "/"
    rpc = {
        "jsonrpc": "2.0",
        "id": uuid4().hex,
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "messageId": uuid4().hex,
                "parts": [{"kind": "text", "text": request_text}],
            }
        },
    }
    body = json.dumps(rpc).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        # AgentCore requires a session id of at least 33 characters.
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": f"a2a-client-{uuid4().hex}",
    }
    status, text = signed_request(session, region, "POST", url, body=body, headers=headers, timeout=180)
    if status != 200:
        raise SystemExit(f"message/send failed: HTTP {status}: {text[:400]}")
    return json.loads(text)


def render_reply(reply: dict) -> str:
    if "error" in reply:
        error = reply["error"]
        return f"A2A error {error.get('code')}: {error.get('message')}"
    result = reply.get("result") or {}
    parts = result.get("parts") or []
    texts = [part.get("text", "") for part in parts if isinstance(part, dict)]
    return "\n".join(texts) if texts else json.dumps(result, indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["card", "ask"])
    parser.add_argument("--runtime-arn", required=True, help="A2A adapter runtime ARN")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--profile")
    parser.add_argument("--request", help="Structured diagnostic request JSON (for ask)")
    args = parser.parse_args(argv)

    session = boto3.Session(profile_name=args.profile) if args.profile else boto3.Session()

    if args.command == "card":
        card = fetch_card(session, args.runtime_arn, args.region)
        print(json.dumps(card, indent=2))
        return 0

    if not args.request:
        parser.error("ask requires --request")
    # Validate locally so a typo fails here rather than as a remote -32602.
    json.loads(args.request)
    reply = send_message(session, args.runtime_arn, args.region, args.request)
    print(render_reply(reply))
    return 1 if "error" in reply else 0


if __name__ == "__main__":
    sys.exit(main())
