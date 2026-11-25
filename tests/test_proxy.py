import json
import socket
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import anyio
import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient


def config(**overrides):
    values = {
        "relay_url": "https://relay.example/backend",
        "relay_key": "upstream-key",
        "relay_auth": "x-api-key",
        "local_token": "local-key",
        "max_body_bytes": 4096,
        "max_concurrency": 2,
        "upstream_request_id_header": "x-bedrock-id",
        "upstream_timeout": 2.0,
    }
    return SimpleNamespace(**(values | overrides))


def make_app(handler, captures, *, on_capture=None, **overrides):
    from relay_audit.proxy import create_app

    async def capture(**event):
        captures.append(event)

    return create_app(
        config(**overrides),
        on_capture or capture,
        transport=httpx.MockTransport(handler),
    )


AUTH = {"x-api-key": "local-key"}


def test_forwards_json_on_fixed_origin_and_only_injects_upstream_credential():
    captures = []
    request_body = b'{ "model": "sonnet", "messages": [] }'
    response_body = b'{ "type": "message", "id": "provider-id" }'

    def upstream(request):
        assert str(request.url) == "https://relay.example/backend/v1/messages?beta=true"
        assert request.headers["x-api-key"] == "upstream-key"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert request.headers["host"] == "relay.example"
        assert "authorization" not in request.headers
        assert "x-hop" not in request.headers
        assert "connection" not in request.headers
        assert "cookie" not in request.headers
        assert request.content == request_body
        return httpx.Response(
            201,
            headers={
                "content-type": "application/json",
                "connection": "x-hop",
                "x-hop": "removed",
                "x-bedrock-id": "aws-real",
                "request-id": "advertised",
            },
            content=response_body,
        )

    with TestClient(make_app(upstream, captures)) as client:
        response = client.post(
            "/v1/messages?beta=true",
            content=request_body,
            headers=AUTH | {
                "authorization": "Bearer local-key",
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
                "host": "different.example",
                "connection": "x-hop",
                "x-hop": "discard",
                "cookie": "local=session",
                "x-bedrock-id": "client-forged",
            },
        )

    assert response.status_code == 201
    assert response.content == response_body
    assert "x-hop" not in response.headers
    assert response.headers["request-id"] == "advertised"
    assert len(captures) == 1
    event = captures[0]
    assert event["request_body"] == json.loads(request_body)
    assert event["response_body"] == json.loads(response_body)
    assert event["reason"] is None
    assert event["request_id_hint"] == "aws-real"
    assert 0 < event["started_at"] <= event["finished_at"]


def test_local_auth_is_required_except_for_health():
    def no_upstream(request):
        pytest.fail("Unexpected upstream call")

    captures = []
    with TestClient(make_app(no_upstream, captures, max_body_bytes=100)) as client:
        assert client.get("/health").status_code == 200
        assert client.post("/v1/messages", json={}).status_code == 401
        assert client.post("/v1/messages", headers={"x-api-key": "bad"}, json={}).status_code == 401
        assert client.post("/v1/messages", headers=AUTH, content=b"x" * 101).status_code == 413
        assert client.get("/v1/unlisted", headers=AUTH).status_code == 404

    assert len(captures) == 1
    assert captures[0]["reason"] == "REQUEST_TOO_LARGE"


def test_auxiliary_routes_use_bearer_auth_without_sampling():
    captures = []
    requests = []

    def upstream(request):
        assert request.headers["authorization"] == "Bearer upstream-key"
        assert "x-api-key" not in request.headers
        requests.append(request.url.path)
        return httpx.Response(400, content=b"upstream body")

    with TestClient(make_app(upstream, captures, relay_auth="bearer")) as client:
        headers = {"authorization": "Bearer local-key"}
        assert client.post("/v1/messages/count_tokens", headers=headers, json={}).content == b"upstream body"
        assert client.get("/v1/models", headers=headers).status_code == 400
    assert requests == ["/backend/v1/messages/count_tokens", "/backend/v1/models"]
    assert captures == []


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [(429, b"quota", "UPSTREAM_HTTP_ERROR"), (200, b"invalid json", "UNSUPPORTED_RESPONSE")],
)
def test_upstream_status_and_raw_errors_survive_capture(status, body, reason):
    captures = []

    def upstream(request):
        return httpx.Response(status, content=body, headers={"request-id": "untrusted"})

    with TestClient(make_app(upstream, captures)) as client:
        response = client.post("/v1/messages", headers=AUTH, json={})
    assert response.status_code == status
    assert response.content == body
    assert len(captures) == 1
    assert captures[0]["reason"] == reason
    assert captures[0]["request_id_hint"] is None


def test_capture_exceptions_do_not_affect_response_or_capacity():
    async def failed_capture(**event):
        raise RuntimeError("capture failed")

    def upstream(request):
        return httpx.Response(200, json={"type": "message"})

    app = make_app(upstream, [], on_capture=failed_capture, max_concurrency=1)
    with TestClient(app) as client:
        assert client.post("/v1/messages", headers=AUTH, json={}).status_code == 200
        assert client.post("/v1/messages", headers=AUTH, json={}).status_code == 200


def test_upstream_redirect_is_forwarded_without_following():
    requests = []

    def upstream(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://other.example"})

    with TestClient(make_app(upstream, []), follow_redirects=False) as client:
        response = client.get("/v1/models", headers=AUTH)
    assert response.status_code == 302
    assert response.headers["location"] == "https://other.example"
    assert requests == ["https://relay.example/backend/v1/models"]


SSE_FRAMES = [
    b'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_test","type":"message","role":"assistant","model":"sonnet","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":1,"output_tokens":0}}}\n\n',
    b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
    'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"好"}}\n\n'.encode(),
    b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n',
    b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":1}}\n\n',
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
]


class ByteChunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


def test_sse_remains_byte_exact_with_fragmented_utf8():
    captures = []
    payload = b"".join(SSE_FRAMES)
    split = payload.index("好".encode()) + 1

    def upstream(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ByteChunks([payload[:split], payload[split:split + 1], payload[split + 1:]]),
        )

    with TestClient(make_app(upstream, captures)) as client:
        response = client.post("/v1/messages", headers=AUTH, json={"stream": True})

    assert response.content == payload
    assert captures[0]["response_body"]["content"] == [{"type": "text", "text": "好"}]
    assert captures[0]["reason"] is None


def test_proxy_forwards_sse_when_capture_budget_is_exhausted():
    captures = []
    payload = b"".join(SSE_FRAMES)

    def upstream(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ByteChunks([payload[:256], payload[256:]]),
        )

    with TestClient(make_app(upstream, captures, max_body_bytes=256)) as client:
        response = client.post("/v1/messages", headers=AUTH, json={"stream": True})

    assert response.content == payload
    assert captures[0]["response_body"] is None
    assert captures[0]["reason"]


def test_upstream_timeout_reports_sanitized_local_error():
    captures = []

    def upstream(request):
        raise httpx.ReadTimeout("https://upstream-secret.example", request=request)

    with TestClient(make_app(upstream, captures)) as client:
        response = client.post("/v1/messages", headers=AUTH, json={})
    assert response.status_code == 504
    assert b"upstream-secret" not in response.content
    assert captures[0]["reason"] == "UPSTREAM_TIMEOUT"


@contextmanager
def loopback_server(app):
    class ReadyServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            ready.set()

    ready = threading.Event()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        server = ReadyServer(uvicorn.Config(
            app, http="h11", lifespan="auto", log_level="critical", timeout_graceful_shutdown=1,
        ))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            assert ready.wait(timeout=5)
            yield f"http://127.0.0.1:{sock.getsockname()[1]}"
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive()


@pytest.mark.parametrize("action", ["complete", "disconnect", "truncate"])
def test_loopback_sends_first_sse_frame_before_stream_finishes(action):
    from relay_audit.proxy import create_app

    allow_remaining = threading.Event()
    captured = threading.Event()
    captures = []

    async def upstream(scope, receive, send):
        await receive()
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/event-stream")],
        })
        await send({"type": "http.response.body", "body": SSE_FRAMES[0], "more_body": True})
        released = await anyio.to_thread.run_sync(allow_remaining.wait, 3)
        assert released
        if action == "truncate":
            raise RuntimeError("Simulated upstream connection failure")
        await send({"type": "http.response.body", "body": b"".join(SSE_FRAMES[1:])})

    async def capture(**event):
        captures.append(event)
        captured.set()

    with loopback_server(upstream) as relay_url:
        app = create_app(config(relay_url=relay_url, max_concurrency=1), capture)
        with loopback_server(app) as base_url, httpx.Client(base_url=base_url, trust_env=False) as client:
            try:
                with client.stream("POST", "/v1/messages", headers=AUTH, json={"stream": True}) as response:
                    chunks = response.iter_raw()
                    assert next(chunks) == SSE_FRAMES[0]
                    assert not captured.is_set()
                    assert client.post("/v1/messages", headers=AUTH, json={}).status_code == 429
                    if action != "disconnect":
                        allow_remaining.set()
                        if action == "complete":
                            assert b"".join(chunks) == b"".join(SSE_FRAMES[1:])
                        else:
                            with pytest.raises(httpx.RemoteProtocolError):
                                list(chunks)
                assert captured.wait(timeout=3)
            finally:
                allow_remaining.set()

    assert len(captures) == 1
    assert captures[0]["reason"] == {
        "complete": None, "disconnect": "CLIENT_CANCELLED", "truncate": "STREAM_INCOMPLETE",
    }[action]
    assert (captures[0]["response_body"] is not None) == (action == "complete")


def test_proxy_does_not_prefetch_when_downstream_send_is_blocked():
    made_second_chunk = False
    captures = []

    class TrackedChunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal made_second_chunk
            yield SSE_FRAMES[0]
            made_second_chunk = True
            yield b"".join(SSE_FRAMES[1:])

    def upstream(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=TrackedChunks())

    app = make_app(upstream, captures)

    async def delay_send(scope, receive, send):
        async def slow_send(message):
            if message.get("body") == SSE_FRAMES[0]:
                await anyio.sleep(0.01)
                assert not made_second_chunk
            await send(message)

        await app(scope, receive, slow_send)

    with TestClient(delay_send) as client:
        response = client.post("/v1/messages", headers=AUTH, json={"stream": True})

    assert made_second_chunk
    assert response.content == b"".join(SSE_FRAMES)
    assert captures[0]["reason"] is None


def test_closed_downstream_send_is_captured_as_client_cancellation():
    captures = []

    def upstream(request):
        return httpx.Response(200, json={"type": "message"})

    app = make_app(upstream, captures)

    async def close_send(scope, receive, send):
        async def failing_send(message):
            if message["type"] == "http.response.body":
                raise BrokenPipeError
            await send(message)

        await app(scope, receive, failing_send)

    with TestClient(close_send) as client:
        response = client.post("/v1/messages", headers=AUTH, json={})

    assert response.status_code == 200
    assert captures[0]["reason"] == "CLIENT_CANCELLED"
    assert captures[0]["response_body"] is None


def test_stream_requested_but_successful_json_response_is_unverified_fallback():
    captures = []

    def upstream(request):
        return httpx.Response(200, json={"type": "message", "content": []})

    with TestClient(make_app(upstream, captures)) as client:
        response = client.post("/v1/messages", headers=AUTH, json={"stream": True})
    assert response.json()["type"] == "message"
    assert captures[0]["reason"] == "STREAM_FALLBACK"
