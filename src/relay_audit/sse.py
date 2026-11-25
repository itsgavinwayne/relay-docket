"""Bounded reconstruction of complete Anthropic Server-Sent Events."""

import re

from .normalize import NormalizationError, _json, _object, normalize_response, parse_json_object


class SSEError(ValueError):
    def __init__(self, reason, detail):
        super().__init__(detail)
        self.reason = reason


_LINE_ENDING = re.compile(rb"[\r\n]")
_BLOCK_FIELDS = {
    "text": {"type", "text"},
    "thinking": {"type", "thinking", "signature"},
    "redacted_thinking": {"type", "data"},
    "tool_use": {"type", "id", "name", "input"},
}
_DELTAS = {
    "text_delta": ("text", "text"),
    "thinking_delta": ("thinking", "thinking"),
    "signature_delta": ("thinking", "signature"),
    "input_json_delta": ("tool_use", "partial_json"),
}
_EVENT_FIELDS = {
    "message_start": {"type", "message"},
    "ping": {"type"},
    "content_block_start": {"type", "index", "content_block"},
    "content_block_delta": {"type", "index", "delta"},
    "content_block_stop": {"type", "index"},
    "message_delta": {"type", "delta", "usage"},
    "message_stop": {"type"},
}


class SSECollector:
    def __init__(self, max_bytes: int = 4 * 1024 * 1024, *, max_event_bytes: int = 1024 * 1024):
        if max_bytes <= 0 or max_event_bytes <= 0:
            raise ValueError("SSE limits must be positive")
        self._max_bytes = max_bytes
        self._max_event_bytes = min(max_event_bytes, max_bytes)
        self._received = 0
        self._event_size = 0
        self._line = bytearray()
        self._skip_lf = False
        self._first_line = True
        self._event_name = None
        self._data_lines = []
        self._message = None
        self._block = None
        self._deltas = None
        self._message_delta_seen = False
        self._stopped = False
        self._error = None

    def _fail(self, reason, detail):
        self._error = SSEError(reason, detail)
        raise self._error

    def feed(self, data: bytes) -> None:
        if self._error is not None:
            raise self._error
        self._received += len(data)
        if self._received > self._max_bytes:
            self._fail("limit", "SSE stream exceeds audit limit")

        position = 0
        for delimiter in _LINE_ENDING.finditer(data):
            end = delimiter.start()
            char = data[end]
            if self._skip_lf and char == 10 and end == position:
                position = end + 1
                self._skip_lf = False
                continue
            self._append_line(data[position:end], complete=True)
            self._skip_lf = char == 13
            position = end + 1
        if position < len(data):
            self._skip_lf = False
            self._append_line(data[position:], complete=False)

    def _append_line(self, data, *, complete):
        self._event_size += len(data) + complete
        if self._event_size > self._max_event_bytes:
            self._fail("limit", "SSE event exceeds audit limit")
        self._line.extend(data)
        if complete:
            self._handle_line()
            self._line.clear()

    def _handle_line(self):
        try:
            line = self._line.decode("utf-8-sig" if self._first_line else "utf-8")
        except UnicodeDecodeError:
            self._fail("malformed", "invalid SSE text encoding")
        self._first_line = False
        if not line:
            if self._data_lines:
                self._dispatch()
            self._event_name = None
            self._data_lines.clear()
            self._event_size = 0
            return
        if line.startswith(":"):
            return
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            self._data_lines.append(value)
        elif field == "event":
            self._event_name = value

    def _dispatch(self):
        try:
            event = parse_json_object("\n".join(self._data_lines))
        except NormalizationError:
            self._fail("malformed", "invalid SSE event JSON")
        kind = event.get("type")
        if not isinstance(kind, str) or (self._event_name is not None and self._event_name != kind):
            self._fail("malformed", "SSE event type does not match its data")
        self._consume_event(event)

    def _consume_event(self, event):
        kind = event.get("type")
        if not isinstance(kind, str):
            self._fail("malformed", "SSE event has no type")
        if kind == "error":
            self._fail("stream_error", "provider reported an SSE error")
        if kind == "fallback":
            self._fail("fallback", "provider reported model fallback")
        fields = _EVENT_FIELDS.get(kind)
        if fields is None or event.keys() - fields or (fields - {"usage"}) - event.keys():
            self._fail("unsupported", "unsupported SSE event")
        if kind == "ping":
            return
        if self._stopped:
            self._fail("malformed", "SSE event follows message end")
        if kind == "message_start":
            self._start_message(event["message"])
            return
        if self._message is None:
            self._fail("malformed", "SSE event precedes message start")
        if kind.startswith("content_block_"):
            self._handle_block_event(kind, event)
        elif kind == "message_delta":
            self._update_message(event)
        elif self._block is not None or not self._message_delta_seen:
            self._fail("incomplete", "message ended before all SSE events arrived")
        else:
            self._stopped = True

    def _start_message(self, message):
        if (self._message is not None or not isinstance(message, dict)
                or message.get("content") != [] or message.get("stop_reason") is not None
                or message.get("stop_sequence") is not None):
            self._fail("malformed", "invalid SSE message start")
        if "fallback" in message:
            self._fail("fallback", "provider reported model fallback")
        self._message = message

    def _handle_block_event(self, kind, event):
        index = event["index"]
        if self._message_delta_seen or type(index) is not int or index != len(self._message["content"]):
            self._fail("malformed", "invalid SSE block index")
        if kind == "content_block_start":
            self._start_block(event["content_block"])
            return
        if self._block is None:
            self._fail("malformed", "SSE block event precedes block start")
        if kind == "content_block_delta":
            self._update_block(event["delta"])
        else:
            self._finish_block()

    def _start_block(self, block):
        if self._block is not None or not isinstance(block, dict):
            self._fail("malformed", "invalid SSE block start")
        kind = block.get("type")
        if kind == "fallback":
            self._fail("fallback", "provider reported model fallback")
        if kind == "text" and block.get("citations") is None:
            block.pop("citations", None)
        fields = _BLOCK_FIELDS.get(kind) if isinstance(kind, str) else None
        if fields is None or block.keys() - fields:
            self._fail("unsupported", "unsupported SSE content block")
        required = fields - {"signature"} if kind == "thinking" else fields
        if required - block.keys() or any(not isinstance(block[key], str) for key in block.keys() - {"input"}):
            self._fail("malformed", "invalid SSE block fields")
        if kind == "tool_use" and not isinstance(block["input"], dict):
            self._fail("malformed", "invalid SSE tool input")
        self._block = block
        self._deltas = {}

    def _update_block(self, delta):
        if not isinstance(delta, dict) or not isinstance(delta.get("type"), str):
            self._fail("malformed", "invalid SSE block delta")
        delta_type = delta["type"]
        block_type, field = _DELTAS.get(delta_type, (None, None))
        if (block_type != self._block["type"] or delta.keys() != {"type", field}
                or not isinstance(delta[field], str)):
            self._fail("unsupported", "unsupported SSE block delta")
        self._deltas.setdefault(field, []).append(delta[field])

    def _finish_block(self):
        for field, parts in self._deltas.items():
            if field == "partial_json":
                if self._block["input"]:
                    self._fail("malformed", "SSE tool input has conflicting sources")
                try:
                    self._block["input"] = parse_json_object("".join(parts))
                except NormalizationError:
                    self._fail("malformed", "incomplete or invalid SSE tool input")
            else:
                self._block[field] = self._block.get(field, "") + "".join(parts)
        self._message["content"].append(self._block)
        self._block = None
        self._deltas = None

    def _update_message(self, event):
        delta = event["delta"]
        if self._block is not None or not isinstance(delta, dict):
            self._fail("malformed", "invalid SSE message delta")
        if "fallback" in delta or delta.get("stop_reason") == "model_fallback":
            self._fail("fallback", "provider reported model fallback")
        if delta.keys() - {"stop_reason", "stop_sequence", "stop_details"}:
            self._fail("unsupported", "unsupported SSE message update")
        for field, value in delta.items():
            previous = self._message.get(field)
            if previous is not None and value != previous:
                self._fail("malformed", "conflicting SSE stop fields")
            self._message[field] = value
        if "usage" in event:
            usage = event["usage"]
            if not isinstance(usage, dict) or not isinstance(self._message.get("usage", {}), dict):
                self._fail("malformed", "invalid SSE usage")
            self._message.setdefault("usage", {}).update(usage)
        self._message_delta_seen = True

    def _finish_message(self):
        if self._error is not None:
            raise self._error
        if not self._stopped or self._line or self._data_lines or self._event_name is not None:
            self._fail("incomplete", "SSE message has no complete end event")
        return self._message

    def finish(self) -> dict:
        message = self._finish_message()
        try:
            normalize_response(message, "anthropic")
        except NormalizationError:
            self._fail("unsupported", "SSE message cannot be compared")
        return message


def _converse_delta(delta):
    delta = _object(delta, {"text", "toolUse", "reasoningContent"})
    if len(delta) != 1:
        raise NormalizationError("invalid Converse stream delta")
    if "text" in delta:
        return "text", {"type": "text_delta", "text": delta["text"]}
    if "toolUse" in delta:
        tool = _object(delta["toolUse"], {"input"}, {"input"})
        return "tool_use", {"type": "input_json_delta", "partial_json": tool["input"]}
    thinking = _object(delta["reasoningContent"], {"text", "signature"})
    if len(thinking) != 1:
        raise NormalizationError("invalid Converse reasoning delta")
    if "text" in thinking:
        return "thinking", {"type": "thinking_delta", "thinking": thinking["text"]}
    return "thinking", {"type": "signature_delta", "signature": thinking["signature"]}


def _converse_log_events(events):
    block_active = False
    message_stopped = False
    metadata_seen = False
    for raw in events:
        event = _object(raw)
        if len(event) != 1 or metadata_seen:
            raise NormalizationError("invalid Converse stream event")
        kind, payload = next(iter(event.items()))
        if kind == "metadata":
            if not message_stopped:
                raise NormalizationError("Converse stream metadata precedes message stop")
            _object(payload, {"usage", "metrics", "performanceConfig", "serviceTier"})
            metadata_seen = True
        elif message_stopped:
            raise NormalizationError("Converse stream event follows message stop")
        elif kind == "messageStart":
            message = _object(payload, {"role"}, {"role"})
            yield {"type": "message_start", "message": {"role": message["role"], "content": []}}
        elif kind == "contentBlockStart":
            start = _object(payload, {"contentBlockIndex", "start"}, {"contentBlockIndex", "start"})
            block = _object(start["start"], {"toolUse"}, {"toolUse"})
            tool = _object(block["toolUse"], {"toolUseId", "name"}, {"toolUseId", "name"})
            yield {
                "type": "content_block_start", "index": start["contentBlockIndex"],
                "content_block": {"type": "tool_use", "id": tool["toolUseId"], "name": tool["name"], "input": {}},
            }
            block_active = True
        elif kind == "contentBlockDelta":
            change = _object(payload, {"contentBlockIndex", "delta"}, {"contentBlockIndex", "delta"})
            index = change["contentBlockIndex"]
            block_type, delta = _converse_delta(change["delta"])
            if not block_active and block_type in {"text", "thinking"}:
                yield {
                    "type": "content_block_start", "index": index,
                    "content_block": {"type": block_type, block_type: ""},
                }
                block_active = True
            yield {"type": "content_block_delta", "index": index, "delta": delta}
        elif kind == "contentBlockStop":
            end = _object(payload, {"contentBlockIndex"}, {"contentBlockIndex"})
            yield {"type": "content_block_stop", "index": end["contentBlockIndex"]}
            block_active = False
        elif kind == "messageStop":
            end = _object(payload, {"stopReason", "additionalModelResponseFields"}, {"stopReason"})
            extra = _object(end.get("additionalModelResponseFields", {}), {"stop_sequence"})
            yield {"type": "message_delta", "delta": {"stop_reason": end["stopReason"], **extra}}
            yield {"type": "message_stop"}
            message_stopped = True
        else:
            raise NormalizationError("unsupported Converse stream event")


def collect_log_response(events: list, dialect: str) -> dict:
    """Collect a complete event array from already size-limited AWS evidence."""
    if dialect == "converse":
        events = _converse_log_events(events)
    elif dialect != "anthropic":
        raise NormalizationError("unsupported stream log dialect")
    collector = SSECollector()
    try:
        for raw in events:
            event = _object(_json(raw))
            if event.get("type") == "message_stop" and "amazon-bedrock-invocationMetrics" in event:
                _object(event.pop("amazon-bedrock-invocationMetrics"))
            collector._consume_event(event)
        return collector._finish_message()
    except (SSEError, RecursionError) as error:
        raise NormalizationError("invalid or incomplete stream log") from error
