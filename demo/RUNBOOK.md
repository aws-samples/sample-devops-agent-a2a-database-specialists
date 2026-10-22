# Demo runbook: AWS DevOps Agent and database specialists over A2A

This runbook stages a real SQL Server blocking incident on a demo host and
shows AWS DevOps Agent diagnosing it through the `sqlserver-db-troubleshooter`
remote agent. Run every command from the repository root.

Use a demo host and demo data only. The incident script takes an exclusive
table lock on `dbo.Orders`, which blocks every reader of that table until it
ends.

## Before you start

You need the deployment described in the [README](../README.md): both A2A
adapters, both read-only specialists, and both remote agents registered in an
Agent Space. Set these in your shell:

```bash
export AWS_PROFILE=<workload-account-profile>
export AWS_REGION=us-east-1
export EXPECTED_AWS_ACCOUNT_ID=<workload-account-id>
export ORACLE_A2A_RUNTIME_ARN=<oracle_db_troubleshooter_a2a runtime ARN>
export SQLSERVER_A2A_RUNTIME_ARN=<sqlserver_db_troubleshooter_a2a runtime ARN>
export ORACLE_SPECIALIST_RUNTIME_ARN=<dbops_oracle_readonly runtime ARN>
export SQLSERVER_SPECIALIST_RUNTIME_ARN=<dbops_sqlserver_readonly runtime ARN>
export ORACLE_DB_INSTANCE_IDENTIFIER=<rds-oracle-instance-id>
export SQLSERVER_EC2_INSTANCE_ID=<sql-server-ec2-instance-id>
export SQLSERVER_SECRET_ARN=<connection secret of the SQL Server host>
```

The incident script runs on the host through SSM Run Command and reads the
host's own connection secret with the instance role, so the password never
reaches your terminal. Grant that read once with the demo-only stack:

```bash
aws cloudformation deploy \
  --stack-name dbops-sqlserver-demo-scenario-access \
  --template-file demo/sqlserver-demo-scenario-access.yaml \
  --parameter-overrides \
      InstanceRoleName=<instance role of the SQL Server host> \
      SqlServerSecretArn="$SQLSERVER_SECRET_ARN"
```

The host needs `sqlcmd` (mssql-tools18), `jq`, the AWS CLI and the SSM agent.
The secret is a JSON object with `username`, `password` and `database` keys,
and that database has a `dbo.Orders` table with a `CustomerId` column.

## 30 minutes before

```bash
demo/preflight.sh                       # expect "10 passed, 0 failed"
```

Preflight confirms the credentials point at the expected account, checks that
every runtime is `READY` and both databases are up, fetches both Agent Cards,
and makes one warm-up call so the first live call is not a cold start.

Then start the incident for long enough to cover the session (1 to 180
minutes):

```bash
demo/sqlserver_blocking.sh start 60
demo/sqlserver_blocking.sh status       # expect three blocked sessions
```

One session holds an exclusive table lock on `dbo.Orders` inside a
transaction, and three ordinary `SELECT` statements queue behind it. No row is
changed: the lock comes from a `SELECT` with `TABLOCKX`, and the transaction is
rolled back when the hold ends.

## Native path: registered remote agents

Open the DevOps Agent web app for your Agent Space
(`https://<agent-space-id>.aidevops.global.app.aws`), start a new chat, and
paste:

> Orders queries on the SQL Server host `<sql-server-ec2-instance-id>`
> (`<region>`, account `<workload-account-id>`) are hanging. Diagnose it here in
> chat, no separate investigation. Use the sqlserver-db-troubleshooter remote
> agent for engine-internal evidence.

DevOps Agent calls the remote agent itself and returns in about 30 to 60
seconds. Expect it to name the root blocking session, the three waiters on
`LCK_M_IS`, the table and lock hints, and a recommendation that a human
operator ends the blocking session. Its first request is sometimes rejected by
the adapter's schema check. The error lists the exact fields, and DevOps Agent
retries correctly. That rejection is the input control working.

For a second engine, start another new chat:

> Check the health of the RDS for Oracle instance `<rds-oracle-instance-id>`
> (`<region>`, account `<workload-account-id>`): what is it waiting on and are
> there any blocking sessions? Diagnose it here in chat, no separate
> investigation. Use the oracle-db-troubleshooter remote agent for
> engine-internal evidence.

Nothing is staged on Oracle, so expect a healthy result. It shows the same
delegation pattern on a second engine.

## Fallback path: call the adapter from a terminal

If remote-agent registration is not available in your account, make the same
signed A2A call that DevOps Agent makes. Show the Agent Card first:

```bash
.venv/bin/python tools/a2a_client.py card \
  --runtime-arn "$SQLSERVER_A2A_RUNTIME_ARN" --region "$AWS_REGION" --profile "$AWS_PROFILE"
```

Then make the delegated call:

```bash
.venv/bin/python tools/a2a_client.py ask \
  --runtime-arn "$SQLSERVER_A2A_RUNTIME_ARN" --region "$AWS_REGION" --profile "$AWS_PROFILE" \
  --request "$(jq -nc --arg host "$SQLSERVER_EC2_INSTANCE_ID" --arg region "$AWS_REGION" \
      '{ec2_instance_id: $host, region: $region, diagnostic_intent: "blocking_sessions", time_range_minutes: 15, incident_reference: "demo-1"}')"
```

Other intents worth showing: `wait_events`, `tempdb_usage`, `memory_pressure`,
`missing_indexes`, `file_io`.

## Why it is safe

- The request is structured JSON only. Free text, credentials, and any host
  other than the one mapped instance are rejected before the database is
  touched.
- The specialist behind each adapter is a read-only deployment. The tools that
  can change anything are not loaded, so it can recommend ending the blocking
  session but cannot do it. Its IAM role has Describe and Get permissions only.
- Output is schema-checked and redacted. SQL text, literals, account IDs, IP
  addresses and email addresses are removed before anything returns to DevOps
  Agent.

## If something goes wrong

| Symptom | Fix |
|---|---|
| `ExpiredToken` or `Unable to locate credentials` | Refresh the credentials for `AWS_PROFILE`, then run preflight again. |
| `A2A error -32603: ... specialist unavailable` | Ask again. Model output varies, and a second call usually succeeds. |
| Findings say there is no blocking | The hold expired. Run `demo/sqlserver_blocking.sh status`, then `start` again. |
| The call takes over a minute | The first call after idle is a cold start. Run preflight again before presenting. |

## After the demo

```bash
demo/sqlserver_blocking.sh stop         # expect blocked_sessions=0
aws cloudformation delete-stack --stack-name dbops-sqlserver-demo-scenario-access
```

Stop the SQL Server host if you no longer need it.
