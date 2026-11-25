"""Fixed-upstream HTTP forwarding with bounded, local-only capture."""

from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from http.cookiejar import CookieJar, DefaultCookiePolicy
from secrets import compare_digest
from time import time

import anyio
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.requests import ClientDisconnect
from starlette.types import Receive, Scope, Send

from .normalize import parse_json_object
from .sse import SSECollector, SSEError

_MESSAGES = "/v1/messages"
_HOP_HEADERS = {
    b"connection", b"keep-alive", b"proxy-authenticate", b"proxy-authorization",
    b"proxy-connection", b"te", b"trailer", b"transfer-encoding", b"upgrade",
}
_CLIENT_HEADERS = {
    b"accept-encoding", b"authorization", b"content-length", b"cookie", b"expect",
    b"forwarded", b"host", b"x-api-key", b"x-forwarded-for", b"x-forwarded-host",
    b"x-forwarded-port", b"x-forwarded-proto",
}
_SSE_REASONS = {
    "incomplete": "STREAM_INCOMPLETE",
    "unsupported": "STREAM_UNSUPPORTED",
    "limit": "RESPONSE_TOO_LARGE",
    "fallback": "STREAM_FALLBACK",
    "stream_error": "STREAM_ERROR",
    "malformed": "STREAM_MALFORMED",
}


def _headers(headers, removed=frozenset()):
    excluded = _HOP_HEADERS | removed
    for name, value in headers:
        if name.lower() == b"connection":
            excluded.update(token.strip().lower() for token in value.split(b","))
    return [(name.lower(), value) for name, value in headers if name.lower() not in excluded]


def _authorized(request: Request, token: bytes) -> bool:
    api_keys = request.headers.getlist("x-api-key")
    authorizations = request.headers.getlist("authorization")
    if len(api_keys) > 1 or len(authorizations) > 1:
        return False
    credentials = [key.encode("latin-1") for key in api_keys]
    for authorization in authorizations:
        scheme, _, credential = authorization.partition(" ")
        if scheme.lower() != "bearer":
            return False
        credentials.append(credential.encode("latin-1"))
    return bool(credentials) and all(compare_digest(credential, token) for credential in credentials)


def _error(status: int, error_type: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"type": "error", "error": {"type": error_type, "message": message}}, status_code=status
    )


def _json_object(content, content_encoding):
    if content_encoding.lower().strip() not in ("", "identity"):
        return None
    try:
        return parse_json_object(content)
    except (ValueError, RecursionError):
        return None


def _request_hint(headers: httpx.Headers, header_name: str | None) -> str | None:
    values = headers.get_list(header_name) if header_name else []
    if len(values) == 1:
        value = values[0]
        if 0 < len(value) <= 256 and all(33 <= ord(character) <= 126 and character != "," for character in value):
            return value
    return None


class _ResponseCapture:
    def __init__(self, upstream: httpx.Response, limit: int) -> None:
        content_type = upstream.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        self.is_sse = content_type == "text/event-stream"
        self.limit = limit
        self.reason = None
        self.body = bytearray()
        self.sse = None
        if not upstream.is_success:
            self.reason = "UPSTREAM_HTTP_ERROR"
        elif upstream.headers.get("content-encoding", "").lower().strip() not in ("", "identity"):
            self.reason = "UNSUPPORTED_RESPONSE"
        elif self.is_sse:
            self.sse = SSECollector(limit)

    def feed(self, chunk: bytes) -> None:
        if self.reason:
            return
        if self.sse is not None:
            try:
                self.sse.feed(chunk)
            except SSEError as exc:
                self.reason = _SSE_REASONS.get(exc.reason, "STREAM_MALFORMED")
                self.sse = None
        elif len(self.body) + len(chunk) > self.limit:
            self.reason = "RESPONSE_TOO_LARGE"
            self.body.clear()
        else:
            self.body.extend(chunk)

    def finish(self, request_body: dict | None) -> tuple[dict | None, str | None]:
        if self.reason:
            return None, self.reason
        if request_body is None:
            return None, "UNSUPPORTED_REQUEST"
        if request_body.get("stream") is True and not self.is_sse:
            return None, "STREAM_FALLBACK"
        if self.sse is not None:
            try:
                return self.sse.finish(), None
            except SSEError as exc:
                return None, _SSE_REASONS.get(exc.reason, "STREAM_MALFORMED")
        response_body = _json_object(self.body, "")
        return (None, "UNSUPPORTED_RESPONSE") if response_body is None else (response_body, None)


class _RelayResponse(Response):
    """Keep the upstream connection open across ASGI response sends."""

    def __init__(self, forward: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        super().__init__()
        self.forward = forward

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.forward(scope, receive, send)


def create_app(config, on_capture, *, transport=None, lifespan=None) -> FastAPI:
    """Create the loopback service; its runner supplies the bind address."""
    base_url = str(config.relay_url).rstrip("/")
    local_token = config.local_token.encode()
    credential = config.relay_key.encode()
    auth_header = (b"x-api-key", credential)
    if config.relay_auth == "bearer":
        auth_header = (b"authorization", b"Bearer " + credential)
    hint_header = config.upstream_request_id_header
    removed = _CLIENT_HEADERS | ({hint_header.lower().encode("ascii")} if hint_header else set())

    @asynccontextmanager
    async def resources(app: FastAPI):
        async with httpx.AsyncClient(
            cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=())),
            timeout=config.upstream_timeout,
            limits=httpx.Limits(max_connections=config.max_concurrency),
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            app.state.upstream = client
            app.state.capacity = anyio.CapacityLimiter(config.max_concurrency)
            if lifespan is None:
                yield
            else:
                async with lifespan(app) as state:
                    yield state

    app = FastAPI(
        lifespan=resources, docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False,
    )

    @app.get("/health")
    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.post(_MESSAGES)
    @app.post("/v1/messages/count_tokens")
    @app.get("/v1/models")
    async def relay(request: Request):
        if not _authorized(request, local_token):
            return _error(401, "authentication_error", "A valid local access token is required.")

        async def forward(scope: Scope, receive: Receive, send: Send) -> None:
            client = app.state.upstream
            capacity = app.state.capacity
            try:
                capacity.acquire_nowait()
            except anyio.WouldBlock:
                await _error(429, "rate_limit_error", "Local relay concurrency limit reached.")(scope, receive, send)
                return

            started_at = time()
            audit = request.url.path == _MESSAGES
            body = request_id_hint = parser = None
            reason = "CLIENT_CANCELLED"
            upstream = None
            try:
                buffer = bytearray()
                async for part in request.stream():
                    if len(buffer) + len(part) > config.max_body_bytes:
                        reason = "REQUEST_TOO_LARGE"
                        await _error(413, "invalid_request_error", "Local request size limit exceeded.")(
                            scope, receive, send
                        )
                        return
                    buffer.extend(part)
                body = bytes(buffer)
                del buffer

                async with anyio.create_task_group() as group:
                    async def watch_disconnect():
                        while (await receive())["type"] != "http.disconnect":
                            pass
                        group.cancel_scope.cancel()

                    async def send_downstream(message):
                        try:
                            await send(message)
                        except OSError:
                            group.cancel_scope.cancel()
                            await anyio.lowlevel.checkpoint()

                    group.start_soon(watch_disconnect)
                    try:
                        headers = _headers(request.headers.raw, removed)
                        headers.extend([auth_header, (b"accept-encoding", b"identity")])
                        url = httpx.URL(base_url + request.url.path).copy_with(query=scope["query_string"] or None)
                        # A standalone request avoids pooled client headers and cookies.
                        outbound = httpx.Request(request.method, url, headers=headers, content=body)
                        upstream = await client.send(outbound, stream=True)
                    except httpx.TimeoutException:
                        await _error(504, "api_error", "Upstream request timed out.")(
                            scope, receive, send_downstream
                        )
                        reason = "UPSTREAM_TIMEOUT"
                        group.cancel_scope.cancel()
                        return
                    except httpx.HTTPError:
                        await _error(502, "api_error", "Upstream request failed.")(
                            scope, receive, send_downstream
                        )
                        reason = "UPSTREAM_CONNECTION_ERROR"
                        group.cancel_scope.cancel()
                        return

                    response_headers = _headers(upstream.headers.raw)
                    if audit:
                        request_id_hint = _request_hint(httpx.Headers(response_headers), hint_header)
                        parser = _ResponseCapture(upstream, config.max_body_bytes)

                    await send_downstream({
                        "type": "http.response.start",
                        "status": upstream.status_code,
                        "headers": response_headers,
                    })

                    try:
                        async for part in _response_parts(upstream):
                            await send_downstream({"type": "http.response.body", "body": part, "more_body": True})
                            if parser is not None:
                                parser.feed(part)
                    except httpx.HTTPError:
                        reason = "UPSTREAM_RESPONSE_INCOMPLETE"
                        if parser is not None and parser.is_sse:
                            reason = "STREAM_INCOMPLETE"
                        # Omit the ASGI end marker so the HTTP connection reports truncation.
                        raise RuntimeError("Upstream response ended unexpectedly.") from None

                    await send_downstream({"type": "http.response.body", "body": b"", "more_body": False})
                    reason = None
                    group.cancel_scope.cancel()
            except (ClientDisconnect, OSError):
                reason = "CLIENT_CANCELLED"
                pass
            finally:
                finished_at = time()
                try:
                    if upstream is not None:
                        with anyio.CancelScope(shield=True):
                            await upstream.aclose()
                finally:
                    capacity.release()
                if audit:
                    request_body = None
                    response_body = None
                    if body is not None:
                        request_body = _json_object(body, request.headers.get("content-encoding", ""))
                    if reason is None and parser is not None:
                        response_body, reason = parser.finish(request_body)
                    with anyio.move_on_after(1.0, shield=True):
                        try:
                            await on_capture(
                                request_body=request_body,
                                response_body=response_body,
                                started_at=started_at,
                                finished_at=finished_at,
                                request_id_hint=request_id_hint,
                                reason=reason,
                            )
                        except Exception:
                            # The callback receives sensitive transient data, so exceptions stay local.
                            pass

        return _RelayResponse(forward)

    return app


async def _response_parts(response: httpx.Response):
    if response.is_stream_consumed:
        if response.content:
            yield response.content
        return
    async for part in response.aiter_raw():
        yield part
