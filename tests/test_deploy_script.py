# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "agentcore" / "oracle_a2a" / "deploy.sh"


def executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def environment(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable(
        bin_dir / "agentcore",
        "#!/usr/bin/env bash\n"
        "if [[ \"$1 $2\" == \"configure --help\" ]]; then\n"
        "  echo --protocol --execution-role --deployment-type\n"
        "else\n"
        "  echo unexpected-live-agentcore-command >&2\n"
        "  exit 91\n"
        "fi\n",
    )
    executable(
        bin_dir / "python",
        "#!/usr/bin/env bash\necho 0.3.9\n",
    )
    executable(
        bin_dir / "aws",
        "#!/usr/bin/env bash\necho 123456789012\n",
    )
    return {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "A2A_REGION": "us-east-1",
        "A2A_RUNTIME_NAME": "oracle_db_troubleshooter_a2a",
        "ORACLE_AGENT_RUNTIME_ARN": (
            "arn:aws:bedrock-agentcore:us-east-1:123456789012:"
            "runtime/dbops_oracle_readonly-AbCd1234"
        ),
        "ORACLE_DB_INSTANCE_IDENTIFIER": "oracle-instance",
        "A2A_RUNTIME_EXECUTION_ROLE_ARN": (
            "arn:aws:iam::123456789012:role/OracleA2ARuntimeRole"
        ),
        "EXPECTED_AWS_ACCOUNT_ID": "123456789012",
        "AWS_PROFILE": "test-profile",
        "AGENTCORE_CLI": str(bin_dir / "agentcore"),
    }


def test_deploy_script_is_plan_only_and_account_bound(tmp_path):
    # Runs the repository's own deploy.sh (fixed path, no shell) in plan mode.
    result = subprocess.run(  # nosec B603  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        [str(SCRIPT)],
        cwd=SCRIPT.parent,
        env=environment(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Caller account: 123456789012" in result.stdout
    assert "starter toolkit 0.3.9" in result.stdout
    assert "--protocol A2A" in result.stdout
    assert "Plan only" in result.stdout
    assert "unexpected-live-agentcore-command" not in result.stderr


def test_deploy_script_rejects_mismatched_target_account(tmp_path):
    env = environment(tmp_path)
    env["EXPECTED_AWS_ACCOUNT_ID"] = "999999999999"

    # Runs the repository's own deploy.sh (fixed path, no shell) in plan mode.
    result = subprocess.run(  # nosec B603  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        [str(SCRIPT)],
        cwd=SCRIPT.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "ORACLE_AGENT_RUNTIME_ARN" in result.stderr
    assert "EXPECTED_AWS_ACCOUNT_ID" in result.stderr
