from dataclasses import replace
from datetime import datetime, timezone
import time

from fastapi.testclient import TestClient
import httpx
import pytest

from relay_audit.cli import create_server
from relay_audit.config import Profile
from relay_audit.core import capture, reconcile
from relay_audit.probe import send_probe
from relay_audit.store import Store

KEY = b"a" * 32
SONNET = "anthropic.claude-sonnet-4-6"
PROFILE = Profile(relay_url="https://relay.invalid", relay_key="upstream-key",
                  local_token="local-token-for-testing-only", aws_account="123456789012",
                  aws_region="us-east-1", allowed_roles=("arn:aws:iam::123456789012:role/Relay",),
                  model_map={"test": (SONNET,)}, sample_basis_points=10000, log_wait=0)
REQUEST = {"model": "test", "max_tokens": 256, "messages": [{"role": "user", "content": "Hello"}]}
REPLY = {"type": "message", "role": "assistant", "model": "declared-by-relay",
         "content": [{"type": "text", "text": "Hi."}], "stop_reason": "end_turn"}


def record(body, text="Hi.", model=SONNET, request_id="aws-123", operation="Converse"):
    return {"schemaType": "ModelInvocationLog", "schemaVersion": "1.0",
            "timestamp": datetime.now(timezone.utc).isoformat(), "accountId": PROFILE.aws_account,
            "region": PROFILE.aws_region, "identity": {
                "arn": f"arn:aws:sts::{PROFILE.aws_account}:assumed-role/Relay/session"},
            "requestId": request_id, "modelId": model, "operation": operation,
            "input": {"inputBodyJson": {
                "messages": [{"role": msg["role"], "content": [{"text": msg["content"]}]}
                             for msg in body["messages"]],
                "inferenceConfig": {"maxTokens": body["max_tokens"]},
            }}, "output": {"outputBodyJson": {
                "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
                "stopReason": "end_turn",
            }}}


class FakeAWS:
    def __init__(self):
        self.records = []

    def scan(self, start, end, *, not_before):
        assert not_before is None or time.time() >= not_before
        return list(self.records)

    def hydrate(self, value, **kwargs):
        return value


def await_result(store):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        rows = store.report("default")
        if rows and rows[0]["status"] != "PENDING":
            return rows[0]
        time.sleep(0.02)
    raise AssertionError("audit worker did not produce a result")


def test_proxy_sampling_worker_and_report(tmp_path):
    store, reader = Store(tmp_path / "audit.db"), FakeAWS()

    def upstream(req):
        assert req.headers["x-api-key"] == PROFILE.relay_key
        assert PROFILE.local_token not in str(req.headers)
        assert b"Audit tag:" not in req.content
        assert len(store.report("default")) == 0
        reader.records.append(record(REQUEST, model="deepseek.v3.2"))
        return httpx.Response(200, json=REPLY)

    with TestClient(create_server(PROFILE, store, KEY, reader, transport=httpx.MockTransport(upstream))) as client:
        response = client.post("/v1/messages", json=REQUEST, headers={"x-api-key": PROFILE.local_token})
        assert response.json() == REPLY
        result = await_result(store)
        assert (result["status"], result["reason"], result["aws_model_id"]) == (
            "MISMATCH", "MODEL_MISMATCH", "deepseek.v3.2")
        assert store.stats("default", time.time())["sampling_rate"] == 1


@pytest.mark.parametrize("model", [
    SONNET, "anthropic.claude-opus-4-7", "anthropic.claude-haiku-4-5-20251001-v1:0",
    "deepseek.v3.2", "zai.glm-5", "qwen.qwen3-235b-a22b-2507-v1:0",
    "moonshotai.kimi-k2.5", "minimax.minimax-m2.5",
])
def test_provider_converse_contract(model):
    profile = replace(PROFILE, model_map={"test": (model,)})
    now = time.time()
    item = capture(profile, KEY, REQUEST, REPLY, now, now)
    agrees = reconcile(item, profile, KEY, [record(REQUEST, model=model)], FakeAWS().hydrate)
    assert agrees["status"] == "VERIFIED", agrees
    differs = reconcile(item, profile, KEY, [record(REQUEST, "Changed", model)], FakeAWS().hydrate)
    assert differs["status"] == "UNVERIFIED"
    assert differs["response_match"] is False


def test_active_probe_detects_edited_answer_and_duplicate_candidate():
    reader = FakeAWS()

    def upstream(req):
        import json
        body = json.loads(req.content)
        reader.records.append(record(body, "different output"))
        return httpx.Response(200, json=REPLY)

    item = send_probe(PROFILE, KEY, "test", transport=httpx.MockTransport(upstream))
    result = reconcile(item, PROFILE, KEY, reader.records, reader.hydrate)
    assert result["status"] == "MISMATCH", result
    assert result["reason"] == "RESPONSE_MISMATCH"
    duplicate = reader.records[0] | {"requestId": "aws-retry"}
    result = reconcile(item, PROFILE, KEY, [*reader.records, duplicate], reader.hydrate)
    assert result["status"] == "UNVERIFIED" and result["reason"] == "AMBIGUOUS_MATCH"


def test_sampling_zero_still_relays_and_counts(tmp_path):
    store = Store(tmp_path / "audit.db")
    reader = FakeAWS()
    profile = replace(PROFILE, sample_basis_points=0)
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=REPLY))
    with TestClient(create_server(profile, store, KEY, reader, transport=transport)) as client:
        assert client.post("/v1/messages", json=REQUEST, headers={"x-api-key": profile.local_token}).json() == REPLY
    assert not store.report("default")
    stats = store.stats("default", time.time())
    assert stats["observed"] == 1 and stats["sampled"] == 0


def test_queue_overflow_records_unverified_and_preserves_first_job(tmp_path):
    store = Store(tmp_path / "audit.db")
    now = time.time()
    items = [capture(PROFILE, KEY, REQUEST, REPLY, now, now) for _ in range(2)]
    assert store.enqueue(items[0], now, limit=1, passive=True)
    assert not store.enqueue(items[1], now, limit=1, passive=True)
    assert store.report("default", audit_id=items[0]["audit_id"])[0]["status"] == "PENDING"
    assert store.report("default", audit_id=items[1]["audit_id"])[0]["reason"] == "QUEUE_FULL"
    assert store.stats("default", now)["dropped"] == 1


def test_aws_stream_fixture_is_reconstructed_before_comparison():
    now = time.time()
    item = capture(PROFILE, KEY, REQUEST, REPLY, now, now)
    stream = [
        {"type": "message_start", "message": {"type": "message", "role": "assistant", "content": [],
                                            "stop_reason": None, "stop_sequence": None}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi."}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}},
        {"type": "message_stop"},
    ]
    row = record(REQUEST, operation="InvokeModelWithResponseStream") | {
        "input": {"inputBodyJson": REQUEST | {"anthropic_version": "bedrock-2023-05-31"}},
        "output": {"outputBodyJson": stream},
    }
    result = reconcile(item, PROFILE, KEY, [row], FakeAWS().hydrate)
    assert result["status"] == "VERIFIED", result
    row["output"]["outputBodyJson"] = stream[:-1]
    result = reconcile(item, PROFILE, KEY, [row], FakeAWS().hydrate)
    assert result["status"] == "UNVERIFIED", result
