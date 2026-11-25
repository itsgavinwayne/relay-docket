"""Map explicitly supported JSON fields into the versioned comparison format."""

import json
import math


class NormalizationError(ValueError):
    """The body cannot be compared without discarding or guessing semantics."""


_INFERENCE = ("max_tokens", "stop_sequences", "temperature", "top_p", "top_k")
_CONVERSE_INFERENCE = {
    "maxTokens": "max_tokens", "stopSequences": "stop_sequences",
    "temperature": "temperature", "topP": "top_p",
}
_STOP_REASONS = {
    "end_turn", "max_tokens", "stop_sequence", "tool_use", "pause_turn", "refusal",
    "model_context_window_exceeded",
}
_TRANSPORT_BETAS = {"fine-grained-tool-streaming-2025-05-14"}


def _object(value, allowed=None, required=()):
    if not isinstance(value, dict):
        raise NormalizationError("expected an object")
    if allowed is not None and value.keys() - set(allowed):
        raise NormalizationError("unsupported object field")
    if set(required) - value.keys():
        raise NormalizationError("missing required field")
    return value


def _list(value):
    if not isinstance(value, list):
        raise NormalizationError("expected an array")
    return value


def _string(value):
    if not isinstance(value, str):
        raise NormalizationError("expected a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise NormalizationError("invalid Unicode string") from error
    return value


def _boolean(value):
    if type(value) is not bool:
        raise NormalizationError("expected a boolean")
    return value


def _json(value):
    if isinstance(value, dict):
        return {_string(key): _json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise NormalizationError("invalid JSON number")
    if isinstance(value, str):
        return _string(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise NormalizationError("invalid JSON value")


def parse_json_object(source):
    """Decode an object without silently dropping duplicate keys or invalid numbers."""
    def pairs_to_dict(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise NormalizationError("duplicate JSON key")
        return result

    try:
        return _object(_json(json.loads(source, object_pairs_hook=pairs_to_dict)))
    except (ValueError, RecursionError) as error:
        raise NormalizationError("invalid JSON object") from error


def _text(value):
    return {"type": "text", "text": _string(value)}


def _cache(block):
    if "cache_control" in block:
        control = _object(block["cache_control"], {"type", "ttl"}, {"type"})
        if control["type"] != "ephemeral" or control.get("ttl", "5m") not in ("5m", "1h"):
            raise NormalizationError("unsupported cache control")


def _is_cache_point(raw):
    if "cachePoint" not in raw:
        return False
    _object(raw, {"cachePoint"}, {"cachePoint"})
    point = _object(raw["cachePoint"], {"type", "ttl"}, {"type"})
    if point["type"] != "default" or point.get("ttl", "5m") not in ("5m", "1h"):
        raise NormalizationError("unsupported cache point")
    return True


def _anthropic_text(value):
    if isinstance(value, str):
        return [_text(value)]
    output = []
    for block in _list(value):
        output.append(_anthropic_text_block(block))
    return output


def _anthropic_text_block(block):
    _object(block, {"type", "text", "cache_control", "citations"}, {"type", "text"})
    if block["type"] != "text" or block.get("citations") is not None:
        raise NormalizationError("unsupported text block")
    _cache(block)
    return _text(block["text"])


def _converse_text(value):
    output = []
    for raw in _list(value):
        block = _object(raw)
        if not _is_cache_point(block):
            _object(block, {"text"}, {"text"})
            output.append(_text(block["text"]))
    return output


class _ToolLinks:
    def __init__(self):
        self.positions = {}
        self.returned = set()

    def use(self, identifier, name, arguments, role):
        identifier = _string(identifier)
        if role != "assistant" or not identifier or identifier in self.positions:
            raise NormalizationError("invalid tool call position")
        position = len(self.positions)
        self.positions[identifier] = position
        return {"type": "tool_use", "id": position, "name": _string(name),
                "input": _json(_object(arguments))}

    def result(self, identifier, content, status, role):
        identifier = _string(identifier)
        if role != "user" or identifier not in self.positions or identifier in self.returned:
            raise NormalizationError("unmatched tool result")
        self.returned.add(identifier)
        result = {"type": "tool_result", "tool_use_id": self.positions[identifier], "content": content}
        if status is not None:
            result["is_error"] = status
        return result

    def require_complete(self):
        if self.positions.keys() != self.returned:
            raise NormalizationError("unanswered tool call in request history")


def _anthropic_block(block, role, links):
    kind = _string(block.get("type"))
    if kind == "text":
        return _anthropic_text_block(block)
    _cache(block)
    if kind == "tool_use":
        _object(block, {"type", "id", "name", "input", "cache_control"}, {"id", "name", "input"})
        return links.use(block["id"], block["name"], block["input"], role)
    if kind == "tool_result":
        _object(block, {"type", "tool_use_id", "is_error", "content", "cache_control"},
                {"tool_use_id", "content"})
        status = _boolean(block.get("is_error", False))
        return links.result(block["tool_use_id"], _anthropic_text(block["content"]), status, role)
    if role == "assistant" and kind == "thinking":
        _object(block, {"type", "thinking", "signature"}, {"thinking", "signature"})
        return {"type": kind, "thinking": _string(block["thinking"]), "signature": _string(block["signature"])}
    if role == "assistant" and kind == "redacted_thinking":
        _object(block, {"type", "data"}, {"data"})
        return {"type": kind, "data": _string(block["data"])}
    raise NormalizationError("unsupported content block")


def _converse_block(block, role, links):
    if len(block) != 1:
        raise NormalizationError("invalid Converse content block")
    if "text" in block:
        return _text(block["text"])
    if "toolUse" in block:
        call = _object(block["toolUse"], {"toolUseId", "name", "input"}, {"toolUseId", "name", "input"})
        return links.use(call["toolUseId"], call["name"], call["input"], role)
    if "toolResult" in block:
        result = _object(block["toolResult"], {"toolUseId", "content", "status"}, {"toolUseId", "content"})
        status = None
        if "status" in result:
            status_value = result["status"]
            if status_value not in ("success", "error"):
                raise NormalizationError("invalid tool result status")
            status = status_value == "error"
        content = []
        for raw in _list(result["content"]):
            item = _object(raw, {"text", "json"})
            if len(item) != 1:
                raise NormalizationError("invalid tool result block")
            content.append(_text(item["text"]) if "text" in item else {"type": "json", "json": _json(item["json"])})
        return links.result(result["toolUseId"], content, status, role)
    if role == "assistant" and "reasoningContent" in block:
        reasoning = _object(block["reasoningContent"], {"reasoningText", "redactedContent"})
        if len(reasoning) == 1 and "reasoningText" in reasoning:
            signed = _object(reasoning["reasoningText"], {"text", "signature"}, {"text", "signature"})
            return {"type": "thinking", "thinking": _string(signed["text"]), "signature": _string(signed["signature"])}
    raise NormalizationError("unsupported content block")


def _message_content(value, role, links, dialect):
    if dialect == "anthropic" and isinstance(value, str):
        return [_text(value)]
    content = []
    for item in _list(value):
        block = _object(item)
        if dialect == "converse":
            if not _is_cache_point(block):
                content.append(_converse_block(block, role, links))
        else:
            content.append(_anthropic_block(block, role, links))
    return content


def _messages(value, dialect):
    links = _ToolLinks()
    messages = []
    for raw in _list(value):
        message = _object(raw, {"role", "content"}, {"role", "content"})
        role = message["role"]
        if role not in ("user", "assistant"):
            raise NormalizationError("unsupported message role")
        messages.append({"role": role, "content": _message_content(message["content"], role, links, dialect)})
    links.require_complete()
    return messages


def _settings(values):
    result = {}
    for name, value in values.items():
        if name == "stop_sequences":
            result[name] = [_string(item) for item in _list(value)]
        elif name in ("max_tokens", "top_k"):
            if type(value) is not int or value < 0 or (name == "max_tokens" and value == 0):
                raise NormalizationError("invalid inference setting")
            result[name] = value
        else:
            if type(value) not in (int, float) or not 0 <= value <= (2 if name == "temperature" else 1):
                raise NormalizationError("invalid inference setting")
            result[name] = float(value)
    return result


def _thinking(value):
    config = _object(value, {"type", "budget_tokens", "display"}, {"type"})
    kind = config["type"]
    if kind not in ("enabled", "disabled", "adaptive"):
        raise NormalizationError("unsupported thinking configuration")
    if kind == "enabled":
        budget = config.get("budget_tokens")
        if type(budget) is not int or budget <= 0:
            raise NormalizationError("invalid thinking budget")
    elif "budget_tokens" in config:
        raise NormalizationError("unexpected thinking budget")
    if "display" in config and config["display"] not in ("summarized", "omitted"):
        raise NormalizationError("unsupported thinking display")
    return _json(config)


def _tool(raw):
    tool = _object(raw, {"name", "description", "input_schema", "cache_control", "strict", "type",
                         "eager_input_streaming"},
                   {"name", "input_schema"})
    _cache(tool)
    if "type" in tool and tool["type"] != "custom":
        raise NormalizationError("unsupported tool definition")
    if "eager_input_streaming" in tool:
        _boolean(tool["eager_input_streaming"])
    result = {"name": _string(tool["name"]), "input_schema": _json(_object(tool["input_schema"]))}
    if "description" in tool:
        result["description"] = _string(tool["description"])
    if "strict" in tool:
        result["strict"] = _boolean(tool["strict"])
    return result


def _choice(raw):
    choice = _object(raw, {"type", "name", "disable_parallel_tool_use"}, {"type"})
    kind = choice["type"]
    if kind not in ("auto", "any", "none", "tool") or ("name" in choice) != (kind == "tool"):
        raise NormalizationError("unsupported tool choice")
    if "name" in choice:
        _string(choice["name"])
    if "disable_parallel_tool_use" in choice:
        _boolean(choice["disable_parallel_tool_use"])
    return _json(choice)


def _converse_tools(raw):
    config = _object(raw, {"tools", "toolChoice"}, {"tools"})
    tools = []
    for item in _list(config["tools"]):
        spec = _object(item)
        if _is_cache_point(spec):
            continue
        _object(spec, {"toolSpec"}, {"toolSpec"})
        definition = _object(spec["toolSpec"], {"name", "description", "inputSchema", "strict"},
                             {"name", "inputSchema"})
        schema = _object(definition["inputSchema"], {"json"}, {"json"})
        tool = {key: value for key, value in definition.items() if key != "inputSchema"}
        tool["input_schema"] = schema["json"]
        tools.append(_tool(tool))
    result = {"tools": tools}
    if "toolChoice" in config:
        choices = _object(config["toolChoice"], {"auto", "any", "tool"})
        if len(choices) != 1:
            raise NormalizationError("invalid tool choice")
        kind, value = next(iter(choices.items()))
        options = _object(value, {"name"} if kind == "tool" else set())
        result["tool_choice"] = _choice({"type": kind, **options})
    return result


def _output_config(config):
    normalized = _object(config, {"effort"})
    if "effort" in normalized:
        _string(normalized["effort"])
    return _json(normalized)


def _anthropic_request(body):
    _object(body, {*_INFERENCE, "model", "stream", "anthropic_version", "anthropic_beta", "metadata", "messages",
                   "system", "thinking", "tools", "tool_choice", "output_config", "cache_control"}, {"messages"})
    if "anthropic_version" in body and body["anthropic_version"] not in ("bedrock-2023-05-31", "2023-06-01"):
        raise NormalizationError("unsupported Anthropic body version")
    _cache(body)
    result = {"schema_version": 1, "messages": _messages(body["messages"], "anthropic"),
              "inference": _settings({name: body[name] for name in _INFERENCE if name in body})}
    if "anthropic_beta" in body:
        names = [_string(name) for name in _list(body["anthropic_beta"])]
        if len(names) != len(set(names)) or any(not name or name.strip() != name or "," in name for name in names):
            raise NormalizationError("invalid Anthropic beta names")
        semantic_betas = sorted(set(names) - _TRANSPORT_BETAS)
        if semantic_betas:
            result["anthropic_beta"] = semantic_betas
    for field, normalizer in {"system": _anthropic_text, "thinking": _thinking,
                              "tools": lambda value: [_tool(raw) for raw in _list(value)],
                              "tool_choice": _choice, "output_config": _output_config}.items():
        if field in body:
            result[field] = normalizer(body[field])
    return result


def _converse_request(body):
    _object(body, {"modelId", "requestMetadata", "messages", "system", "inferenceConfig",
                   "toolConfig", "additionalModelRequestFields", "additionalModelResponseFieldPaths",
                   "outputConfig"}, {"messages"})
    inference = _object(body.get("inferenceConfig", {}), _CONVERSE_INFERENCE)
    additional = _object(body.get("additionalModelRequestFields", {}), {"top_k", "thinking", "output_config"})
    settings = {_CONVERSE_INFERENCE[key]: value for key, value in inference.items()}
    if "top_k" in additional:
        settings["top_k"] = additional["top_k"]
    result = {"schema_version": 1, "messages": _messages(body["messages"], "converse"),
              "inference": _settings(settings)}
    if "system" in body:
        result["system"] = _converse_text(body["system"])
    if "thinking" in additional:
        result["thinking"] = _thinking(additional["thinking"])
    if "toolConfig" in body:
        result.update(_converse_tools(body["toolConfig"]))
    if "outputConfig" in body and "output_config" in additional:
        raise NormalizationError("ambiguous output config")
    if "outputConfig" in body:
        result["output_config"] = _output_config(body["outputConfig"])
    if "output_config" in additional:
        result["output_config"] = _output_config(additional["output_config"])
    return result


def _openai_text(value):
    if isinstance(value, str):
        return [_text(value)]
    result = []
    for raw in _list(value):
        part = _object(raw, {"type", "text"}, {"type", "text"})
        if part["type"] != "text":
            raise NormalizationError("unsupported text content")
        result.append(_text(part["text"]))
    return result


def _openai_call(raw, links):
    call = _object(raw, {"type", "id", "function"}, {"type", "id", "function"})
    if call["type"] != "function":
        raise NormalizationError("unsupported tool call")
    function = _object(call["function"], {"name", "arguments"}, {"name", "arguments"})
    return links.use(call["id"], function["name"], parse_json_object(_string(function["arguments"])), "assistant")


def _openai_message(raw, links):
    message = _object(raw)
    role = message.get("role")
    if role == "assistant":
        _object(message, {"role", "content", "tool_calls", "refusal"})
        if message.get("refusal") is not None:
            raise NormalizationError("unsupported refusal content")
        calls = _list(message.get("tool_calls", []))
        if message.get("content") is None:
            if not calls:
                raise NormalizationError("missing assistant content")
            content = []
        else:
            content = _openai_text(message["content"])
        content.extend(_openai_call(call, links) for call in calls)
        return {"role": role, "content": content}
    if role == "tool":
        _object(message, {"role", "content", "tool_call_id"}, {"content", "tool_call_id"})
        result = links.result(message["tool_call_id"], _openai_text(message["content"]), None, "user")
        return {"role": "user", "content": [result]}
    if role == "user":
        _object(message, {"role", "content"}, {"content"})
        return {"role": role, "content": _openai_text(message["content"])}
    raise NormalizationError("unsupported message role")


def _openai_tools(values):
    tools = []
    for raw in _list(values):
        item = _object(raw, {"type", "function"}, {"type", "function"})
        if item["type"] != "function":
            raise NormalizationError("unsupported tool definition")
        function = _object(item["function"], {"name", "description", "parameters", "strict"}, {"name", "parameters"})
        definition = {key: value for key, value in function.items() if key != "parameters"}
        definition["input_schema"] = function["parameters"]
        tools.append(_tool(definition))
    return tools


def _openai_request(body):
    _object(body, {"model", "messages", "stream", "stream_options", "metadata", "max_tokens", "max_completion_tokens",
                   "temperature", "top_p", "stop", "tools", "tool_choice", "parallel_tool_calls", "n"}, {"messages"})
    if "n" in body and (type(body["n"]) is not int or body["n"] != 1):
        raise NormalizationError("multiple choices are unsupported")
    if "max_tokens" in body and "max_completion_tokens" in body:
        raise NormalizationError("ambiguous token limit")
    inference = {key: body[key] for key in ("max_tokens", "temperature", "top_p") if key in body}
    if "max_completion_tokens" in body:
        inference["max_tokens"] = body["max_completion_tokens"]
    if "stop" in body:
        inference["stop_sequences"] = [body["stop"]] if isinstance(body["stop"], str) else body["stop"]
    system = None
    messages = []
    links = _ToolLinks()
    for raw in _list(body["messages"]):
        message = _object(raw)
        if message.get("role") == "system":
            _object(message, {"role", "content"}, {"content"})
            if messages or system is not None:
                raise NormalizationError("cannot translate system message placement")
            system = _openai_text(message["content"])
        else:
            messages.append(_openai_message(message, links))
    links.require_complete()
    result = {"schema_version": 1, "messages": messages, "inference": _settings(inference)}
    if system is not None:
        result["system"] = system
    if "tools" in body:
        result["tools"] = _openai_tools(body["tools"])
    if "tool_choice" in body or "parallel_tool_calls" in body:
        raw = body.get("tool_choice", "auto")
        if isinstance(raw, str):
            choice = {"type": "any" if raw == "required" else raw}
        else:
            function = _object(raw, {"type", "function"}, {"type", "function"})
            if function["type"] != "function":
                raise NormalizationError("unsupported tool choice")
            name = _object(function["function"], {"name"}, {"name"})
            choice = {"type": "tool", "name": name["name"]}
        if "parallel_tool_calls" in body:
            choice["disable_parallel_tool_use"] = not _boolean(body["parallel_tool_calls"])
        result["tool_choice"] = _choice(choice)
    return result


def normalize_request(body: dict, dialect: str) -> dict:
    if dialect == "anthropic":
        return _anthropic_request(body)
    if dialect == "converse":
        return _converse_request(body)
    if dialect == "openai":
        return _openai_request(body)
    raise NormalizationError("unsupported request dialect")


def _response(content, reason, sequence=None, details=None):
    reason = _string(reason)
    if reason not in _STOP_REASONS:
        raise NormalizationError("unsupported stop reason")
    result = {"schema_version": 1, "content": content, "stop_reason": reason}
    if reason == "stop_sequence":
        result["stop_sequence"] = _string(sequence)
    elif sequence is not None:
        raise NormalizationError("unexpected stop sequence")
    if details is not None:
        if reason != "refusal":
            raise NormalizationError("unexpected stop details")
        result["stop_details"] = _json(_object(details))
    return result


def normalize_response(body: dict | list, dialect: str) -> dict:
    if isinstance(body, list):
        from .sse import collect_log_response

        body = collect_log_response(body, dialect)
        dialect = "anthropic"
    links = _ToolLinks()
    if dialect == "anthropic":
        _object(body, {"id", "type", "role", "model", "usage", "content", "stop_reason", "stop_sequence", "stop_details",
                       "amazon-bedrock-invocationMetrics"}, {"content", "stop_reason"})
        if body.get("role", "assistant") != "assistant" or body.get("type", "message") != "message":
            raise NormalizationError("invalid response message")
        content = _message_content(_list(body["content"]), "assistant", links, dialect)
        return _response(content, body["stop_reason"], body.get("stop_sequence"), body.get("stop_details"))
    if dialect == "converse":
        _object(body, {"output", "stopReason", "additionalModelResponseFields", "usage", "metrics",
                       "performanceConfig", "serviceTier"}, {"output", "stopReason"})
        output = _object(body["output"], {"message"}, {"message"})
        message = _object(output["message"], {"role", "content"}, {"role", "content"})
        if message["role"] != "assistant":
            raise NormalizationError("invalid response message")
        extra = _object(body.get("additionalModelResponseFields", {}), {"stop_sequence"})
        content = _message_content(message["content"], "assistant", links, dialect)
        return _response(content, body["stopReason"], extra.get("stop_sequence"))
    if dialect == "openai":
        _object(body, {"id", "object", "created", "model", "usage", "choices",
                       "system_fingerprint", "service_tier"}, {"choices"})
        choices = _list(body["choices"])
        if body.get("object", "chat.completion") != "chat.completion" or len(choices) != 1:
            raise NormalizationError("expected a complete single choice")
        choice = _object(choices[0], {"index", "message", "finish_reason", "logprobs"}, {"message", "finish_reason"})
        message = _object(choice["message"], {"role", "content", "refusal", "tool_calls"}, {"role"})
        if message["role"] != "assistant" or choice.get("index", 0) != 0:
            raise NormalizationError("invalid response choice")
        stop = choice["finish_reason"]
        if stop == "length":
            reason = "max_tokens"
        elif stop == "tool_calls":
            reason = "tool_use"
        else:
            raise NormalizationError("missing comparable stop reason")
        content = _openai_message(message, links)["content"]
        return _response(content, reason)
    raise NormalizationError("unsupported response dialect")
