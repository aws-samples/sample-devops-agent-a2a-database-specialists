#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Demo preflight for the workload account: confirm the credentials point at the
# expected account, every runtime is READY, both databases are up, both Agent
# Cards can be fetched, and warm the SQL Server path with one real call so the
# first call in front of an audience is not a cold start.
#
# Required environment:
#   AWS_PROFILE                  profile for the workload account
#   EXPECTED_AWS_ACCOUNT_ID      12-digit workload account ID
#   ORACLE_A2A_RUNTIME_ARN       oracle_db_troubleshooter_a2a runtime ARN
#   SQLSERVER_A2A_RUNTIME_ARN    sqlserver_db_troubleshooter_a2a runtime ARN
#   ORACLE_SPECIALIST_RUNTIME_ARN     dbops_oracle_readonly runtime ARN
#   SQLSERVER_SPECIALIST_RUNTIME_ARN  dbops_sqlserver_readonly runtime ARN
#   ORACLE_DB_INSTANCE_IDENTIFIER     the RDS for Oracle instance
#   SQLSERVER_EC2_INSTANCE_ID         the SQL Server on EC2 host
# Optional: AWS_REGION (default us-east-1)
set -uo pipefail
: "${AWS_PROFILE:?Set AWS_PROFILE}"
: "${EXPECTED_AWS_ACCOUNT_ID:?Set EXPECTED_AWS_ACCOUNT_ID}"
: "${ORACLE_A2A_RUNTIME_ARN:?Set ORACLE_A2A_RUNTIME_ARN}"
: "${SQLSERVER_A2A_RUNTIME_ARN:?Set SQLSERVER_A2A_RUNTIME_ARN}"
: "${ORACLE_SPECIALIST_RUNTIME_ARN:?Set ORACLE_SPECIALIST_RUNTIME_ARN}"
: "${SQLSERVER_SPECIALIST_RUNTIME_ARN:?Set SQLSERVER_SPECIALIST_RUNTIME_ARN}"
: "${ORACLE_DB_INSTANCE_IDENTIFIER:?Set ORACLE_DB_INSTANCE_IDENTIFIER}"
: "${SQLSERVER_EC2_INSTANCE_ID:?Set SQLSERVER_EC2_INSTANCE_ID}"
export AWS_PROFILE AWS_REGION="${AWS_REGION:-us-east-1}"

if [[ ! "$SQLSERVER_EC2_INSTANCE_ID" =~ ^i-[0-9a-f]{8,17}$ ]]; then
  echo "SQLSERVER_EC2_INSTANCE_ID is not an EC2 instance ID" >&2
  exit 2
fi

HERE="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-$HERE/.venv/bin/python}"
ok=0; fail=0
check() { if "$@" >/dev/null 2>&1; then echo "  ok    $LABEL"; ok=$((ok+1)); else echo "  FAIL  $LABEL"; fail=$((fail+1)); fi; }

LABEL="credentials for $EXPECTED_AWS_ACCOUNT_ID"
check test "$(aws sts get-caller-identity --query Account --output text --cli-read-timeout 20 --cli-connect-timeout 10)" = "$EXPECTED_AWS_ACCOUNT_ID"
for arn in "$ORACLE_A2A_RUNTIME_ARN" "$SQLSERVER_A2A_RUNTIME_ARN" \
           "$ORACLE_SPECIALIST_RUNTIME_ARN" "$SQLSERVER_SPECIALIST_RUNTIME_ARN"; do
  id="${arn##*/}"
  LABEL="runtime $id READY"
  check test "$(aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$id" --query status --output text)" = READY
done
LABEL="SQL Server host running"
check test "$(aws ec2 describe-instances --instance-ids "$SQLSERVER_EC2_INSTANCE_ID" --query 'Reservations[0].Instances[0].State.Name' --output text)" = running
LABEL="Oracle instance available"
check test "$(aws rds describe-db-instances --db-instance-identifier "$ORACLE_DB_INSTANCE_IDENTIFIER" --query 'DBInstances[0].DBInstanceStatus' --output text)" = available
for arn in "$ORACLE_A2A_RUNTIME_ARN" "$SQLSERVER_A2A_RUNTIME_ARN"; do
  LABEL="Agent Card ${arn##*/}"
  check "$PY" "$HERE/tools/a2a_client.py" card --runtime-arn "$arn" --region "$AWS_REGION" --profile "$AWS_PROFILE"
done
request=$(jq -nc --arg host "$SQLSERVER_EC2_INSTANCE_ID" --arg region "$AWS_REGION" \
  '{ec2_instance_id: $host, region: $region, diagnostic_intent: "wait_events", time_range_minutes: 15, incident_reference: "preflight"}')
LABEL="warm-up call through the SQL Server adapter"
check "$PY" "$HERE/tools/a2a_client.py" ask --runtime-arn "$SQLSERVER_A2A_RUNTIME_ARN" \
  --region "$AWS_REGION" --profile "$AWS_PROFILE" --request "$request"
echo "$ok passed, $fail failed"
exit $(( fail > 0 ))
