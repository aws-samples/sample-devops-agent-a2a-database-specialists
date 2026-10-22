#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Demo only: create, inspect, or clear a real blocking chain on the SQL Server
# demo host, so the read-only specialist has something to find.
#
#   demo/sqlserver_blocking.sh start [minutes]   # default 15, max 180
#
# Requires AWS_PROFILE, SQLSERVER_EC2_INSTANCE_ID and SQLSERVER_SECRET_ARN.
# The host needs sqlcmd (mssql-tools18), jq and the AWS CLI, a dbo.Orders
# table in the secret's database, and the SSM agent. Use a demo host only.
#   demo/sqlserver_blocking.sh status
#   demo/sqlserver_blocking.sh stop
#
# How it works
# ------------
# The commands run on the host through SSM Run Command. One session takes an
# exclusive table lock on dbo.Orders inside a transaction and holds it; three
# more sessions run ordinary SELECTs against the table and queue behind it.
# No row is modified: the lock comes from a SELECT with TABLOCKX, and the
# transaction is rolled back when the hold ends or the session is killed.
#
# The password never leaves the instance. The on-host script reads the host's
# own secret with the instance role (see sqlserver-demo-scenario-access.yaml)
# and hands it to sqlcmd through SQLCMDPASSWORD, so it is not on any command
# line, in the SSM command text, or in this terminal.
set -euo pipefail
: "${AWS_PROFILE:?Set AWS_PROFILE}"
: "${AWS_REGION:=us-east-1}"
: "${SQLSERVER_EC2_INSTANCE_ID:?Set SQLSERVER_EC2_INSTANCE_ID to the SQL Server demo host}"
: "${SQLSERVER_SECRET_ARN:?Set SQLSERVER_SECRET_ARN to the connection secret of that host}"

action="${1:-}"
minutes="${2:-15}"
if [[ ! "$minutes" =~ ^[0-9]+$ ]] || (( minutes < 1 || minutes > 180 )); then
  echo "minutes must be 1-180" >&2
  exit 2
fi
if [[ ! "$SQLSERVER_EC2_INSTANCE_ID" =~ ^i-[0-9a-f]{8,17}$ ]]; then
  echo "SQLSERVER_EC2_INSTANCE_ID is not an EC2 instance ID" >&2
  exit 2
fi
if [[ ! "$SQLSERVER_SECRET_ARN" =~ ^arn:aws:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+$ ]]; then
  echo "SQLSERVER_SECRET_ARN is not a Secrets Manager secret ARN" >&2
  exit 2
fi
if [[ ! "$AWS_REGION" =~ ^[a-z]{2}(-[a-z]+)+-[0-9]$ ]]; then
  echo "AWS_REGION is not an AWS Region name" >&2
  exit 2
fi

# Shared prologue for every on-host script. Values are fixed above and
# validated, so they are safe to embed.
prologue=$(cat <<EOF
set -eu
SQLCMD=/opt/mssql-tools18/bin/sqlcmd
SECRET="\$(aws secretsmanager get-secret-value --region $AWS_REGION --secret-id '$SQLSERVER_SECRET_ARN' --query SecretString --output text)"
export SQLCMDPASSWORD="\$(printf '%s' "\$SECRET" | jq -r .password)"
DBUSER="\$(printf '%s' "\$SECRET" | jq -r .username)"
DB="\$(printf '%s' "\$SECRET" | jq -r .database)"
unset SECRET
EOF
)

case "$action" in
  start)
    hold="$(printf '%02d:%02d:00' $(( minutes / 60 )) $(( minutes % 60 )))"
    body=$(cat <<EOF
pkill -f 'dbops-demo-blocking' || true
nohup "\$SQLCMD" -S localhost -U "\$DBUSER" -C -d "\$DB" -l 30 -t 0 -Q "/* dbops-demo-blocking holder */ BEGIN TRAN; SELECT TOP (1) OrderId FROM dbo.Orders WITH (TABLOCKX, HOLDLOCK); WAITFOR DELAY '$hold'; ROLLBACK;" >/tmp/dbops-demo-holder.log 2>&1 &
sleep 3
for n in 1 2 3; do
  nohup "\$SQLCMD" -S localhost -U "\$DBUSER" -C -d "\$DB" -l 30 -t $(( minutes * 60 + 120 )) -Q "/* dbops-demo-blocking waiter */ SELECT COUNT(*) FROM dbo.Orders WHERE CustomerId = \$n;" >/tmp/dbops-demo-waiter-\$n.log 2>&1 &
done
sleep 5
"\$SQLCMD" -S localhost -U "\$DBUSER" -C -d "\$DB" -l 30 -h -1 -W -Q "SET NOCOUNT ON; SELECT 'blocked_sessions=' + CAST(COUNT(*) AS varchar(10)) FROM sys.dm_exec_requests WHERE blocking_session_id <> 0;"
echo "holding for $minutes minute(s)"
EOF
)
    ;;
  status)
    body=$(cat <<'EOF'
"$SQLCMD" -S localhost -U "$DBUSER" -C -d "$DB" -l 30 -h -1 -W -Q "SET NOCOUNT ON; SELECT 'session=' + CAST(r.session_id AS varchar(10)) + ' blocked_by=' + CAST(r.blocking_session_id AS varchar(10)) + ' wait=' + ISNULL(r.wait_type,'') + ' wait_ms=' + CAST(r.wait_time AS varchar(20)) FROM sys.dm_exec_requests r WHERE r.blocking_session_id <> 0;"
echo "demo client processes: $(pgrep -fc 'dbops-demo-blocking' || true)"
EOF
)
    ;;
  stop)
    body=$(cat <<'EOF'
pkill -f 'dbops-demo-blocking' || true
sleep 3
"$SQLCMD" -S localhost -U "$DBUSER" -C -d "$DB" -l 30 -h -1 -W -Q "SET NOCOUNT ON; SELECT 'blocked_sessions=' + CAST(COUNT(*) AS varchar(10)) FROM sys.dm_exec_requests WHERE blocking_session_id <> 0;"
EOF
)
    ;;
  *)
    echo "usage: $0 start [minutes] | status | stop" >&2
    exit 2
    ;;
esac

script="$prologue
$body"
params=$(jq -nc --arg s "$script" '{commands: [$s]}')
command_id=$(aws ssm send-command \
  --instance-ids "$SQLSERVER_EC2_INSTANCE_ID" \
  --document-name AWS-RunShellScript \
  --comment "dbops demo blocking: $action" \
  --parameters "$params" \
  --query Command.CommandId --output text)
for _ in $(seq 1 30); do
  status=$(aws ssm get-command-invocation --command-id "$command_id" \
    --instance-id "$SQLSERVER_EC2_INSTANCE_ID" --query Status --output text 2>/dev/null || echo Pending)
  case "$status" in
    Success|Failed|Cancelled|TimedOut) break ;;
  esac
  sleep 2
done
aws ssm get-command-invocation --command-id "$command_id" \
  --instance-id "$SQLSERVER_EC2_INSTANCE_ID" \
  --query '[Status, StandardOutputContent, StandardErrorContent]' --output text
