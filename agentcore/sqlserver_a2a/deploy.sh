#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Configure and deploy the SQL Server A2A adapter with a pinned AgentCore CLI.
# Defaults to a read-only plan. Confirm with the exact target account ID.
set -euo pipefail

: "${A2A_REGION:=us-east-1}"
: "${A2A_RUNTIME_NAME:=sqlserver_db_troubleshooter_a2a}"
: "${SQLSERVER_AGENT_RUNTIME_ARN:?Set SQLSERVER_AGENT_RUNTIME_ARN to the dbops_sqlserver_readonly runtime ARN}"
: "${SQLSERVER_EC2_INSTANCE_ID:?Set SQLSERVER_EC2_INSTANCE_ID to the one EC2 SQL Server host mapped to that runtime}"
: "${A2A_RUNTIME_EXECUTION_ROLE_ARN:?Set A2A_RUNTIME_EXECUTION_ROLE_ARN from the IAM stack output}"
: "${EXPECTED_AWS_ACCOUNT_ID:?Set EXPECTED_AWS_ACCOUNT_ID to the reviewed 12-digit target account}"
: "${AWS_PROFILE:?Set AWS_PROFILE to an explicit least-privilege deployment profile}"
: "${AGENTCORE_CLI:?Set AGENTCORE_CLI to the absolute path of the pinned agentcore executable}"

if [[ ! "$EXPECTED_AWS_ACCOUNT_ID" =~ ^[0-9]{12}$ ]]; then
  echo "EXPECTED_AWS_ACCOUNT_ID must be a 12-digit AWS account ID" >&2
  exit 2
fi
if [[ "$AGENTCORE_CLI" != /* || ! -x "$AGENTCORE_CLI" ]]; then
  echo "AGENTCORE_CLI must be an executable absolute path" >&2
  exit 2
fi
cli_python="$(dirname "$AGENTCORE_CLI")/python"
if [[ ! -x "$cli_python" ]]; then
  echo "AGENTCORE_CLI must have a sibling Python executable for version validation" >&2
  exit 2
fi
cli_version="$($cli_python -c 'from importlib.metadata import version; print(version("bedrock-agentcore-starter-toolkit"))' 2>/dev/null || true)"
if [[ "$cli_version" != "0.3.9" ]]; then
  echo "AGENTCORE_CLI must come from bedrock-agentcore-starter-toolkit 0.3.9; found '${cli_version:-unknown}'" >&2
  exit 2
fi

if [[ ! "$SQLSERVER_AGENT_RUNTIME_ARN" =~ ^arn:(aws|aws-us-gov|aws-cn):bedrock-agentcore:([a-z0-9-]+):([0-9]{12}):runtime/dbops_sqlserver_readonly-[A-Za-z0-9]+$ ]]; then
  echo "SQLSERVER_AGENT_RUNTIME_ARN must identify a dbops_sqlserver_readonly AgentCore runtime" >&2
  exit 2
fi
runtime_region="${BASH_REMATCH[2]}"
runtime_account="${BASH_REMATCH[3]}"
if [[ "$runtime_region" != "$A2A_REGION" ]]; then
  echo "SQLSERVER_AGENT_RUNTIME_ARN region must match A2A_REGION" >&2
  exit 2
fi
if [[ "$runtime_account" != "$EXPECTED_AWS_ACCOUNT_ID" ]]; then
  echo "SQLSERVER_AGENT_RUNTIME_ARN account must match EXPECTED_AWS_ACCOUNT_ID" >&2
  exit 2
fi

if [[ ! "$SQLSERVER_EC2_INSTANCE_ID" =~ ^i-([0-9a-f]{8}|[0-9a-f]{17})$ ]]; then
  echo "SQLSERVER_EC2_INSTANCE_ID must be one EC2 instance ID (i- followed by 8 or 17 hex digits)" >&2
  exit 2
fi
if [[ ! "$A2A_RUNTIME_EXECUTION_ROLE_ARN" =~ ^arn:(aws|aws-us-gov|aws-cn):iam::([0-9]{12}):role/[A-Za-z0-9+=,.@_/-]+$ ]]; then
  echo "A2A_RUNTIME_EXECUTION_ROLE_ARN must be an IAM role ARN" >&2
  exit 2
fi
role_account="${BASH_REMATCH[2]}"
if [[ "$role_account" != "$EXPECTED_AWS_ACCOUNT_ID" ]]; then
  echo "A2A_RUNTIME_EXECUTION_ROLE_ARN account must match EXPECTED_AWS_ACCOUNT_ID" >&2
  exit 2
fi
if [[ ! "$A2A_RUNTIME_NAME" =~ ^[A-Za-z][A-Za-z0-9_]{0,47}$ ]]; then
  echo "A2A_RUNTIME_NAME must start with a letter and contain at most 48 letters, numbers, or underscores" >&2
  exit 2
fi

caller_account="$(aws --profile "$AWS_PROFILE" --region "$A2A_REGION" sts get-caller-identity --query Account --output text)"
if [[ "$caller_account" != "$EXPECTED_AWS_ACCOUNT_ID" ]]; then
  echo "AWS caller account '$caller_account' does not match EXPECTED_AWS_ACCOUNT_ID" >&2
  exit 2
fi

configure_help="$($AGENTCORE_CLI configure --help 2>&1)"
for required_flag in '--protocol' '--execution-role' '--deployment-type'; do
  if [[ "$configure_help" != *"$required_flag"* ]]; then
    echo "Pinned AgentCore CLI does not support required flag $required_flag" >&2
    exit 2
  fi
done

configure=(
  "$AGENTCORE_CLI" configure
  --entrypoint main.py
  --name "$A2A_RUNTIME_NAME"
  --execution-role "$A2A_RUNTIME_EXECUTION_ROLE_ARN"
  --requirements-file requirements.txt
  --disable-memory
  --protocol A2A
  --deployment-type direct_code_deploy
  --runtime PYTHON_3_12
  --region "$A2A_REGION"
  --non-interactive
)
deploy=(
  "$AGENTCORE_CLI" deploy
  --agent "$A2A_RUNTIME_NAME"
  --env "SQLSERVER_AGENT_RUNTIME_ARN=$SQLSERVER_AGENT_RUNTIME_ARN"
  --env "SQLSERVER_EC2_INSTANCE_ID=$SQLSERVER_EC2_INSTANCE_ID"
)

printf 'Caller account: %s\n' "$caller_account"
printf 'AWS profile: %s\n' "$AWS_PROFILE"
printf 'AgentCore CLI: %s (starter toolkit %s)\n' "$AGENTCORE_CLI" "$cli_version"
printf 'Configure:'
printf ' %q' "${configure[@]}"
printf '\nDeploy:'
printf ' %q' "${deploy[@]}"
printf '\n'

if [[ "${CONFIRM_AWS_MUTATIONS:-}" != "$EXPECTED_AWS_ACCOUNT_ID" ]]; then
  echo "Plan only. To deploy, set CONFIRM_AWS_MUTATIONS to the exact reviewed account ID."
  exit 0
fi

AWS_PROFILE="$AWS_PROFILE" AWS_REGION="$A2A_REGION" "${configure[@]}"
AWS_PROFILE="$AWS_PROFILE" AWS_REGION="$A2A_REGION" "${deploy[@]}"
