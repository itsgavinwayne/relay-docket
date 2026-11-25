"""Independent tagged synthetic invocation."""

import secrets
import time

import httpx

from .core import capture
from .normalize import parse_json_object


def send_probe(config, key, model, *, transport=None):
    tag = secrets.token_hex(16)
    body = {
        "model": model, "max_tokens": 256, "stream": False, "messages": [{
            "role": "user", "content": f"Audit tag: {tag}\nExplain the difference between TCP and UDP in two sentences."
        }],
    }
    auth = {"x-api-key": config.relay_key} if config.relay_auth == "x-api-key" else {
        "Authorization": f"Bearer {config.relay_key}"
    }
    reason, result, hint = None, None, None
    started = time.time()
    try:
        with httpx.Client(timeout=config.upstream_timeout, trust_env=False, transport=transport) as client:
            with client.stream("POST", config.relay_url.rstrip("/") + "/v1/messages", json=body,
                               headers={**auth, "anthropic-version": "2023-06-01"}) as response:
                hint = response.headers.get(config.upstream_request_id_header) if config.upstream_request_id_header else None
                if not response.is_success:
                    reason = f"UPSTREAM_HTTP_{response.status_code}"
                else:
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        if len(data) + len(chunk) > config.max_body_bytes:
                            reason = "RESPONSE_TOO_LARGE"
                            break
                        data.extend(chunk)
                    if not reason:
                        result = parse_json_object(data)
    except (ValueError, UnicodeError):
        reason = "INVALID_UPSTREAM_JSON"
    except httpx.HTTPError:
        reason = "UPSTREAM_NETWORK_ERROR"
    return capture(config, key, body, result, started, time.time(), request_id_hint=hint,
                   reason=reason, probe_tag=tag)
