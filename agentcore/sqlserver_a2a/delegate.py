# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Read-only delegation to the AI-DB-OPS SQL Server HTTP runtime.

The target is self-managed SQL Server on EC2, so requests are scoped by EC2
instance ID rather than by RDS DB instance identifier, and the permitted AWS
evidence metrics come from the AWS/EC2 namespace rather than AWS/RDS.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

MAX_REQUEST_CHARS = 4_096
MAX_RESPONSE_BYTES = 256 * 1024
MAX_OUTPUT_CHARS = 20_000
MAX_CONCURRENT_REQUESTS = 4
MAX_FINDINGS = 10
REDACTED_SENSITIVE_TEXT = "[REDACTED_SENSITIVE_TEXT]"
RUNTIME_ARN_PATTERN = re.compile(
    r"^arn:(aws|aws-us-gov|aws-cn):bedrock-agentcore:[a-z0-9-]+:\d{12}:runtime/dbops_sqlserver_readonly-[A-Za-z0-9]+$"
)
# EC2 instance IDs: "i-" followed by 8 (legacy) or 17 lowercase hex digits.
DB_IDENTIFIER_PATTERN = re.compile(r"^i-(?:[0-9a-f]{8}|[0-9a-f]{17})$")
REGION_PATTERN = re.compile(r"^[a-z]{2}(?:-gov)?-[a-z]+-\d$")
REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")
EVIDENCE_KEY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
SENSITIVE_INPUT_PATTERN = re.compile(
    r"(?i)(password|passwd|secret(?:_arn)?|token|connection[_ -]?string|"
    # uid= and pwd= are the credential keys of an ADO.NET / ODBC SQL Server
    # connection string ("Server=...;Uid=sa;Pwd=...").
    r"jdbc:|mssql://|sqlserver://|\buid\s*=|\bpwd\s*=|BEGIN [A-Z ]*PRIVATE KEY)"
)
SENSITIVE_VALUE_PATTERN = re.compile(
    r"(?i)\b(password|passwd|secret|token|connection[_ -]?string)\s*[:=]\s*([^\s,;]+)"
)
SECRET_ARN_PATTERN = re.compile(
    r"arn:(aws|aws-us-gov|aws-cn):secretsmanager:[^\s,;]+"
)
ACCOUNT_ID_PATTERN = re.compile(r"\b\d{12}\b")
EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
IPV4_PATTERN = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
SQL_PATTERN = re.compile(
    r"(?is)\b(SELECT|INSERT|UPDATE|DELETE|MERGE|ALTER|CREATE|DROP|TRUNCATE)\b\s+.{8,}"
)
QUOTED_LITERAL_PATTERN = re.compile(r"'(?:''|[^'])*'")
FENCED_JSON_PATTERN = re.compile(r"```(?:json)?\s*\n(.*?)\n?```", re.S)

DIAGNOSTIC_INTENTS = {
    "active_sessions",
    "wait_events",
    "blocking_sessions",
    "top_sql",
    "sql_plan",
    "index_usage",
    "missing_indexes",
    "tempdb_usage",
    "file_io",
    "memory_pressure",
    "backup_status",
    "instance_health",
    "general_health",
}
ALLOWED_AWS_EVIDENCE = {
    # AWS/EC2 namespace. It has no memory or free-storage metrics; those are
    # guest-level figures the specialist reads from SQL Server DMVs instead.
    "CPUUtilization",
    "CPUCreditBalance",
    "NetworkIn",
    "NetworkOut",
    "EBSReadOps",
    "EBSWriteOps",
    "EBSIOBalance%",
    "StatusCheckFailed",
}
ALLOWED_SEVERITIES = {"critical", "high", "medium", "low", "info"}
ALLOWED_CATEGORIES = DIAGNOSTIC_INTENTS | {
    "availability",
    "capacity",
    "configuration",
    "performance",
}
FALLBACK_CATEGORY = "general_health"

# The specialist labels findings for humans ("TempDB Usage", "File I/O
# Latency", "Wait Statistics"). Map those display labels onto the closed
# category set so one unrecognized label cannot discard an otherwise valid
# diagnostic. Each entry is a keyword group; the first group whose keywords are
# all present wins, so more specific groups are listed before broader ones.
CATEGORY_SYNONYMS: tuple[tuple[frozenset[str], str], ...] = (
    (frozenset({"blocking"}), "blocking_sessions"),
    (frozenset({"locking"}), "blocking_sessions"),
    (frozenset({"lock"}), "blocking_sessions"),
    (frozenset({"deadlock"}), "blocking_sessions"),
    (frozenset({"missing", "index"}), "missing_indexes"),
    (frozenset({"missing", "indexes"}), "missing_indexes"),
    (frozenset({"index"}), "index_usage"),
    (frozenset({"indexes"}), "index_usage"),
    (frozenset({"tempdb"}), "tempdb_usage"),
    (frozenset({"temp"}), "tempdb_usage"),
    (frozenset({"version", "store"}), "tempdb_usage"),
    (frozenset({"file", "io"}), "file_io"),
    (frozenset({"i", "o"}), "file_io"),
    (frozenset({"io"}), "file_io"),
    (frozenset({"latency"}), "file_io"),
    (frozenset({"disk"}), "file_io"),
    (frozenset({"storage"}), "file_io"),
    (frozenset({"memory"}), "memory_pressure"),
    (frozenset({"buffer"}), "memory_pressure"),
    (frozenset({"clerk"}), "memory_pressure"),
    (frozenset({"clerks"}), "memory_pressure"),
    (frozenset({"backup"}), "backup_status"),
    (frozenset({"backups"}), "backup_status"),
    (frozenset({"recovery"}), "backup_status"),
    (frozenset({"plan"}), "sql_plan"),
    (frozenset({"query"}), "top_sql"),
    (frozenset({"queries"}), "top_sql"),
    (frozenset({"sql"}), "top_sql"),
    (frozenset({"workload"}), "top_sql"),
    (frozenset({"wait"}), "wait_events"),
    (frozenset({"waits"}), "wait_events"),
    (frozenset({"session"}), "active_sessions"),
    (frozenset({"sessions"}), "active_sessions"),
    (frozenset({"request"}), "active_sessions"),
    (frozenset({"requests"}), "active_sessions"),
    (frozenset({"connection"}), "active_sessions"),
    (frozenset({"connections"}), "active_sessions"),
    (frozenset({"configuration"}), "configuration"),
    (frozenset({"config"}), "configuration"),
    (frozenset({"ec2"}), "instance_health"),
    (frozenset({"host"}), "instance_health"),
    (frozenset({"infrastructure"}), "instance_health"),
    (frozenset({"instance"}), "instance_health"),
    (frozenset({"server"}), "instance_health"),
    (frozenset({"cpu"}), "performance"),
    (frozenset({"capacity"}), "capacity"),
    (frozenset({"availability"}), "availability"),
)

READ_ONLY_INSTRUCTION = """You are handling a delegated, read-only troubleshooting request for SQL Server on Amazon EC2 from AWS DevOps Agent.

Use only read-only and advisory tools. Do not call kill_session, create_index,
update_statistics, execute_sql, modify_parameter, or trigger_backup. Do not
request or accept an approval. Do not return T-SQL text, SQL literals, query
plan XML, login names, hostnames, client program names,
credentials, connection strings, secret values, or customer data rows. Use
aggregates, query hashes, wait types, counts, durations, and resource state.
If the request requires a change, describe a safe manual next step without
executing it.

Keep it short: at most 6 findings; summary at most 600 characters; each
observation and recommendation at most 400 characters; each evidence value
a number or a string of at most 80 characters; at most 5 limitations of at
most 250 characters each.
Return only one JSON object with this shape and no additional keys:
{{
  "summary": "short aggregate diagnosis",
  "findings": [
    {{
      "category": "one allowed diagnostic category",
      "severity": "critical|high|medium|low|info",
      "observation": "sanitized aggregate observation",
      "evidence": {{"short_metric_name": "short value or number"}},
      "recommendation": "read-only next step or manual recommendation"
    }}
  ],
  "limitations": ["missing evidence or scope limitation"]
}}

Approved diagnostic request:
{request_json}
"""


class AgentCoreClient(Protocol):
    def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]: ...


class DelegationUnavailable(RuntimeError):
    """The downstream specialist failed without usable diagnostic evidence."""


@dataclass(frozen=True)
class SqlServerDiagnosticRequest:
    ec2_instance_id: str
    region: str
    diagnostic_intent: str
    time_range_minutes: int
    aws_evidence: dict[str, float]
    incident_reference: str | None = None

    @classmethod
    def parse(cls, raw: str) -> "SqlServerDiagnosticRequest":
        if not isinstance(raw, str):
            raise TypeError("A2A input must be a JSON string")
        text = raw.strip()
        if not text or len(text) > MAX_REQUEST_CHARS or "\x00" in text:
            raise ValueError(f"A2A input must be 1-{MAX_REQUEST_CHARS} characters")
        if SENSITIVE_INPUT_PATTERN.search(text):
            raise ValueError("A2A input contains a prohibited sensitive field")
        try:
            value = json.loads(text)
        except json.JSONDecodeError as error:
            raise ValueError("A2A input must be one JSON object") from error
        if not isinstance(value, dict):
            raise ValueError("A2A input must be one JSON object")
        allowed = {
            "ec2_instance_id",
            "region",
            "diagnostic_intent",
            "time_range_minutes",
            "aws_evidence",
            "incident_reference",
        }
        required = allowed - {"aws_evidence", "incident_reference"}
        if set(value) - allowed or not required.issubset(value):
            raise ValueError("A2A input fields do not match the diagnostic schema")

        identifier = value["ec2_instance_id"]
        region = value["region"]
        intent = value["diagnostic_intent"]
        minutes = value["time_range_minutes"]
        reference = value.get("incident_reference")
        if not isinstance(identifier, str) or not DB_IDENTIFIER_PATTERN.fullmatch(identifier):
            raise ValueError("invalid ec2_instance_id")
        if not isinstance(region, str) or not REGION_PATTERN.fullmatch(region):
            raise ValueError("invalid region")
        if intent not in DIAGNOSTIC_INTENTS:
            raise ValueError("unsupported diagnostic_intent")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 5 <= minutes <= 1_440:
            raise ValueError("time_range_minutes must be an integer from 5 to 1440")
        if reference is not None and (
            not isinstance(reference, str) or not REFERENCE_PATTERN.fullmatch(reference)
        ):
            raise ValueError("invalid incident_reference")

        evidence = value.get("aws_evidence") or {}
        if not isinstance(evidence, dict) or len(evidence) > 8:
            raise ValueError("aws_evidence must contain at most eight metrics")
        normalized: dict[str, float] = {}
        for key, metric_value in evidence.items():
            if key not in ALLOWED_AWS_EVIDENCE:
                raise ValueError(f"unsupported AWS evidence metric: {key}")
            if isinstance(metric_value, bool) or not isinstance(metric_value, (int, float)):
                raise ValueError(f"AWS evidence metric {key} must be numeric")
            number = float(metric_value)
            if not math.isfinite(number):
                raise ValueError(f"AWS evidence metric {key} must be finite")
            normalized[key] = number

        return cls(
            ec2_instance_id=identifier,
            region=region,
            diagnostic_intent=intent,
            time_range_minutes=minutes,
            aws_evidence=normalized,
            incident_reference=reference,
        )

    def to_json(self) -> str:
        value: dict[str, Any] = {
            "ec2_instance_id": self.ec2_instance_id,
            "region": self.region,
            "diagnostic_intent": self.diagnostic_intent,
            "time_range_minutes": self.time_range_minutes,
            "aws_evidence": self.aws_evidence,
        }
        if self.incident_reference:
            value["incident_reference"] = self.incident_reference
        return json.dumps(value, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class DelegationResult:
    text: str
    blocked_mutation: bool = False


class SqlServerRuntimeDelegate:
    """Invoke one configured SQL Server runtime without forwarding action approvals."""

    _concurrency = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)

    def __init__(
        self,
        runtime_arn: str,
        *,
        expected_db_identifier: str,
        region_name: str,
        client: AgentCoreClient | None = None,
    ) -> None:
        if not RUNTIME_ARN_PATTERN.fullmatch(runtime_arn):
            raise ValueError(
                "SQLSERVER_AGENT_RUNTIME_ARN must identify a dbops_sqlserver_readonly AgentCore runtime"
            )
        if not DB_IDENTIFIER_PATTERN.fullmatch(expected_db_identifier):
            raise ValueError("expected SQL Server EC2 instance ID is invalid")
        self._runtime_arn = runtime_arn
        self._expected_db_identifier = expected_db_identifier
        self._region = region_name
        self._client = client or boto3.client(
            "bedrock-agentcore",
            region_name=region_name,
            config=Config(
                connect_timeout=5,
                read_timeout=45,
                retries={"mode": "standard", "total_max_attempts": 1},
            ),
        )

    @classmethod
    def from_environment(cls) -> "SqlServerRuntimeDelegate":
        runtime_arn = os.environ.get("SQLSERVER_AGENT_RUNTIME_ARN", "").strip()
        db_identifier = os.environ.get("SQLSERVER_EC2_INSTANCE_ID", "").strip()
        region_name = os.environ.get("AWS_REGION", "us-east-1").strip()
        if not runtime_arn:
            raise RuntimeError("SQLSERVER_AGENT_RUNTIME_ARN is required")
        if not db_identifier:
            raise RuntimeError("SQLSERVER_EC2_INSTANCE_ID is required")
        return cls(
            runtime_arn,
            expected_db_identifier=db_identifier,
            region_name=region_name,
        )

    def investigate(self, request_text: str, *, context_id: str | None = None) -> DelegationResult:
        request = SqlServerDiagnosticRequest.parse(request_text)
        if request.ec2_instance_id != self._expected_db_identifier:
            raise ValueError(
                "ec2_instance_id is not mapped to this SQL Server specialist"
            )
        if request.region != self._region:
            raise ValueError("request region is not mapped to this SQL Server specialist")
        if not self._concurrency.acquire(blocking=False):
            raise DelegationUnavailable("SQL Server delegation concurrency limit reached")
        try:
            return self._investigate(request, context_id=context_id)
        finally:
            self._concurrency.release()

    def _investigate(
        self,
        request: SqlServerDiagnosticRequest,
        *,
        context_id: str | None,
    ) -> DelegationResult:
        prompt = READ_ONLY_INSTRUCTION.format(request_json=request.to_json())
        session_id = self._session_id(context_id)
        payload = json.dumps(
            {
                "prompt": prompt,
                "user_id": self._downstream_identity(context_id),
            },
            separators=(",", ":"),
        ).encode("utf-8")

        try:
            response = self._client.invoke_agent_runtime(
                agentRuntimeArn=self._runtime_arn,
                qualifier="DEFAULT",
                runtimeSessionId=session_id,
                payload=payload,
            )
            parsed = self._read_response(response)
        except (BotoCoreError, ClientError, OSError, UnicodeError, ValueError) as error:
            raise DelegationUnavailable("SQL Server specialist unavailable") from error

        if parsed.get("awaiting_approval") or parsed.get("pending_approvals"):
            return DelegationResult(
                "The SQL Server specialist proposed a mutating operation. This A2A path is "
                "read-only, so nothing was approved or executed. Present the proposed "
                "change, impact, rollback, and manual verification steps to the operator.",
                blocked_mutation=True,
            )
        if parsed.get("error"):
            raise DelegationUnavailable("SQL Server specialist returned an error")

        text = self._extract_text(parsed.get("response"))
        if not text:
            raise DelegationUnavailable("SQL Server specialist returned no diagnostic evidence")
        try:
            evidence = self._parse_evidence(text)
        except (TypeError, ValueError) as error:
            # The caller's request was valid; the specialist's reply was not.
            # Surface this as an unavailable dependency (JSON-RPC -32603), not
            # as invalid params (-32602), so the caller does not conclude its
            # own request was wrong and retry with altered arguments.
            raise DelegationUnavailable(
                "specialist returned evidence that did not match the output schema"
            ) from error
        return DelegationResult(self._render_evidence(evidence, request))

    @staticmethod
    def _session_id(context_id: str | None) -> str:
        suffix = uuid4().hex
        if context_id:
            safe = re.sub(r"[^A-Za-z0-9._:-]", "-", context_id)[:40]
            if safe:
                return f"sqlserver-a2a-{safe}-{suffix}"[:128]
        return f"sqlserver-a2a-{suffix}"

    @staticmethod
    def _downstream_identity(context_id: str | None) -> str:
        # This is a correlation identity, not an end-user identity. Human
        # attribution remains a documented production gap until AgentCore exposes
        # a trusted DevOps Agent principal through the runtime context.
        source = (context_id or "unattributed").encode("utf-8")
        digest = hashlib.sha256(source).hexdigest()[:16]
        return f"aws-devops-agent-a2a:{digest}"

    @staticmethod
    def _read_response(response: dict[str, Any]) -> dict[str, Any]:
        status = response.get("statusCode")
        content_type = response.get("contentType", "")
        if not isinstance(status, int) or not 200 <= status < 300:
            raise ValueError("AgentCore returned a non-success status")
        if not isinstance(content_type, str) or not content_type.lower().startswith(
            "application/json"
        ):
            raise ValueError("AgentCore returned an unexpected content type")
        body = response.get("response")
        if body is None or not hasattr(body, "read"):
            raise ValueError("AgentCore response body is missing")
        try:
            raw = body.read(MAX_RESPONSE_BYTES + 1)
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("SQL Server specialist response exceeded the size limit")
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("SQL Server specialist response must be a JSON object")
        return parsed

    @staticmethod
    def _extract_text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if not isinstance(value, dict):
            return ""
        content = value.get("content")
        if not isinstance(content, list):
            return ""
        return "\n".join(
            item["text"]
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )

    @staticmethod
    def _evidence_json(text: str) -> str:
        """Return the JSON document from a specialist reply.

        The model is told to return one JSON object, but sometimes prefixes a
        sentence of narration ("I now have all the data needed...") before a
        fenced block. Take the last ```json fenced block when there is one,
        otherwise the whole reply. Any narration is discarded rather than
        forwarded, and the strict schema validation below still applies.
        """
        clean = text.strip()
        fenced = FENCED_JSON_PATTERN.findall(clean)
        if fenced:
            return fenced[-1].strip()
        return clean

    @classmethod
    def _parse_evidence(cls, text: str) -> dict[str, Any]:
        value = json.loads(cls._evidence_json(text))
        if not isinstance(value, dict) or set(value) != {
            "summary",
            "findings",
            "limitations",
        }:
            raise ValueError("SQL Server evidence does not match the output schema")
        if not isinstance(value["summary"], str) or not value["summary"].strip():
            raise ValueError("SQL Server evidence summary is missing")
        findings = value["findings"]
        limitations = value["limitations"]
        if not isinstance(findings, list) or not isinstance(limitations, list):
            raise ValueError("SQL Server findings and limitations must be lists")
        dropped_findings = max(0, len(findings) - MAX_FINDINGS)
        findings = findings[:MAX_FINDINGS]
        limitations = limitations[:MAX_FINDINGS]

        normalized_findings = []
        coerced_categories = 0
        for finding in findings:
            if not isinstance(finding, dict) or set(finding) != {
                "category",
                "severity",
                "observation",
                "evidence",
                "recommendation",
            }:
                raise ValueError("SQL Server finding does not match the output schema")
            category, coerced = cls._normalize_category(finding["category"])
            coerced_categories += int(coerced)
            if finding["severity"] not in ALLOWED_SEVERITIES:
                raise ValueError("SQL Server finding severity is unsupported")
            evidence = finding["evidence"]
            if not isinstance(evidence, dict) or len(evidence) > 10:
                raise ValueError("SQL Server finding evidence is invalid")
            normalized_evidence: dict[str, str | float | int | bool | None] = {}
            for key, item in evidence.items():
                if not isinstance(key, str) or not EVIDENCE_KEY_PATTERN.fullmatch(key):
                    raise ValueError("SQL Server evidence key is invalid")
                if isinstance(item, list) and len(item) <= 20 and all(
                    isinstance(x, (str, int, float, bool)) for x in item
                ):
                    # e.g. a list of blocked session IDs; flatten to one value.
                    item = ", ".join(str(x) for x in item) or "none"
                if isinstance(item, str):
                    normalized_evidence[key] = cls._safe_text(item, 100)
                elif item is None or isinstance(item, (int, bool)):
                    normalized_evidence[key] = item
                elif isinstance(item, float) and math.isfinite(item):
                    normalized_evidence[key] = item
                else:
                    raise ValueError("SQL Server evidence value is invalid")
            normalized_findings.append(
                {
                    "category": category,
                    "severity": finding["severity"],
                    "observation": cls._safe_text(finding["observation"], 500),
                    "evidence": normalized_evidence,
                    "recommendation": cls._safe_text(finding["recommendation"], 500),
                }
            )
        normalized_limitations = [cls._safe_text(item, 300) for item in limitations]
        if dropped_findings:
            normalized_limitations.append(
                f"{dropped_findings} additional finding(s) were omitted to keep the "
                "reply within the output limit."
            )
        if coerced_categories:
            normalized_limitations.append(
                f"{coerced_categories} finding label(s) were normalized to the "
                "supported category set; read the observation text for specifics."
            )
        return {
            "summary": cls._safe_text(value["summary"], 700),
            "findings": normalized_findings,
            "limitations": normalized_limitations,
        }

    @staticmethod
    def _normalize_category(value: Any) -> tuple[str, bool]:
        """Map a finding label onto the closed category set.

        Returns the canonical category and whether it had to be coerced. The
        raw label is never echoed downstream, so an unrecognized label degrades
        to FALLBACK_CATEGORY instead of discarding the whole diagnostic.
        """

        if not isinstance(value, str) or not value.strip() or len(value) > 100:
            raise ValueError("SQL Server finding category is invalid")
        collapsed = re.sub(r"[^a-z0-9]+", " ", value.strip().lower()).strip()
        if not collapsed:
            raise ValueError("SQL Server finding category is invalid")

        canonical = collapsed.replace(" ", "_")
        if canonical in ALLOWED_CATEGORIES:
            return canonical, False

        words = set(collapsed.split())
        for keywords, category in CATEGORY_SYNONYMS:
            if keywords <= words:
                return category, True
        return FALLBACK_CATEGORY, True

    @classmethod
    def _safe_text(cls, value: Any, limit: int) -> str:
        if not isinstance(value, str):
            raise ValueError("SQL Server evidence text field is invalid")
        text = value.strip()
        if not text:
            raise ValueError("SQL Server evidence text is empty")
        # One bad field must not discard an otherwise valid diagnosis, so a
        # field that mentions credential material is replaced whole, and an
        # over-long field is cut after redaction. Both only ever remove text.
        if SENSITIVE_INPUT_PATTERN.search(text):
            return REDACTED_SENSITIVE_TEXT
        text = SECRET_ARN_PATTERN.sub("[REDACTED_SECRET_ARN]", text)
        text = SENSITIVE_VALUE_PATTERN.sub(r"\1=[REDACTED]", text)
        text = ACCOUNT_ID_PATTERN.sub("[REDACTED_ACCOUNT]", text)
        text = EMAIL_PATTERN.sub("[REDACTED_EMAIL]", text)
        text = IPV4_PATTERN.sub("[REDACTED_IP]", text)
        text = QUOTED_LITERAL_PATTERN.sub("'[REDACTED_LITERAL]'", text)
        if SQL_PATTERN.search(text):
            return "[REDACTED_SQL_TEXT]"
        if len(text) > limit:
            text = text[: limit - 1].rstrip() + "…"
        return text

    @staticmethod
    def _render_evidence(
        evidence: dict[str, Any],
        request: SqlServerDiagnosticRequest,
    ) -> str:
        lines = [
            "SQL Server specialist evidence",
            f"Scope: {request.ec2_instance_id} in {request.region}; "
            f"last {request.time_range_minutes} minutes",
            f"Summary: {evidence['summary']}",
        ]
        for finding in evidence["findings"]:
            metrics = ", ".join(
                f"{key}={value}" for key, value in finding["evidence"].items()
            ) or "no aggregate values returned"
            lines.append(
                f"- {finding['severity']} / {finding['category']}: "
                f"{finding['observation']} Evidence: {metrics}. "
                f"Next step: {finding['recommendation']}"
            )
        if evidence["limitations"]:
            lines.append("Limitations: " + "; ".join(evidence["limitations"]))
        return "\n".join(lines)[:MAX_OUTPUT_CHARS]
