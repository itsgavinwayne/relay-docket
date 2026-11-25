"""Opt-in paid acceptance via an authorized relay and separate read credentials."""

import json
import os

import pytest

from relay_audit.cli import main


def test_real_bedrock_probe(capsys):
    if os.environ.get("RELAY_AUDIT_LIVE") != "1":
        pytest.skip("No authorized real AWS sandbox enabled")
    required = ["RELAY_AUDIT_LIVE_CONFIG", "RELAY_AUDIT_LIVE_MODEL"]
    missing = [name for name in required if not os.environ.get(name)]
    assert not missing, f"Missing live test setting names: {missing}"
    code = main(["check", "--config", os.environ["RELAY_AUDIT_LIVE_CONFIG"],
                 "--model", os.environ["RELAY_AUDIT_LIVE_MODEL"], "--profile",
                 os.environ.get("RELAY_AUDIT_LIVE_PROFILE", "default"), "--json"])
    output = capsys.readouterr()
    assert code == 0, output
    result = json.loads(output.out)
    assert result["status"] == "VERIFIED" and result["aws_request_id"]
