import unittest

from relay_audit.normalize import NormalizationError, normalize_request, normalize_response


class NormalizationTests(unittest.TestCase):
    def test_native_and_converse_request_compares_explicit_settings_and_text(self):
        native = {
            "anthropic_version": "bedrock-2023-05-31", "max_tokens": 25,
            "stop_sequences": ["STOP"], "temperature": 0.2, "system": "Style",
            "messages": [{"role": "user", "content": " Hello\n"}],
        }
        converse = {
            "inferenceConfig": {"maxTokens": 25, "stopSequences": ["STOP"], "temperature": 0.2},
            "system": [{"text": "Style"}],
            "messages": [{"role": "user", "content": [{"text": " Hello\n"}]}],
        }
        self.assertEqual(normalize_request(native, "anthropic"), normalize_request(converse, "converse"))
        converse["messages"][0]["content"] = [{"text": " "}, {"text": "Hello\n"}]
        self.assertNotEqual(normalize_request(native, "anthropic"), normalize_request(converse, "converse"))

    def test_tools_and_results_use_complete_positional_correspondence(self):
        schema = {"type": "object"}
        native = {
            "messages": [
                {"role": "user", "content": "question"},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "left", "name": "lookup", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "left", "is_error": False, "content": "ok"}]},
            ],
            "tools": [{"name": "lookup", "description": "lookup things", "input_schema": schema}],
            "tool_choice": {"type": "auto"},
        }
        converse = {
            "messages": [
                {"role": "user", "content": [{"text": "question"}]},
                {"role": "assistant", "content": [{"toolUse": {"toolUseId": "right", "name": "lookup", "input": {}}}]},
                {"role": "user", "content": [{"toolResult": {"toolUseId": "right", "status": "success", "content": [{"text": "ok"}]}}]},
            ],
            "toolConfig": {"tools": [{"toolSpec": {"name": "lookup", "description": "lookup things", "inputSchema": {"json": schema}}}], "toolChoice": {"auto": {}}},
        }
        self.assertEqual(normalize_request(native, "anthropic"), normalize_request(converse, "converse"))
        converse["messages"][2]["content"][0]["toolResult"]["toolUseId"] = "orphan"
        with self.assertRaises(NormalizationError):
            normalize_request(converse, "converse")

    def test_request_rejects_unknown_semantics(self):
        request = {"messages": [{"role": "user", "content": "hello"}], "guardrailConfig": {}}
        with self.assertRaises(NormalizationError):
            normalize_request(request, "converse")

    def test_absent_and_explicit_inference_value_differ(self):
        body = {"messages": [{"role": "user", "content": "hello"}]}
        self.assertNotEqual(
            normalize_request(body, "anthropic"),
            normalize_request({**body, "temperature": 1}, "anthropic"),
        )

    def test_converse_response_can_compare_signed_thinking(self):
        native = {"content": [{"type": "thinking", "thinking": "why", "signature": "sig"},
                              {"type": "text", "text": "answer"}],
                  "stop_reason": "max_tokens"}
        converse = {"output": {"message": {"role": "assistant", "content": [
            {"reasoningContent": {"reasoningText": {"text": "why", "signature": "sig"}}},
            {"text": "answer"},
        ]}}, "stopReason": "max_tokens"}
        self.assertEqual(normalize_response(native, "anthropic"), normalize_response(converse, "converse"))
        converse["output"]["message"]["content"][0]["reasoningContent"]["reasoningText"]["signature"] = "other"
        self.assertNotEqual(normalize_response(native, "anthropic"), normalize_response(converse, "converse"))

    def test_openai_request_maps_stop_and_completion_length(self):
        body = {"messages": [{"role": "system", "content": "Style"}, {"role": "user", "content": "question"}],
                "stop": "STOP", "max_completion_tokens": 25}
        expected = {"system": "Style", "messages": [{"role": "user", "content": "question"}],
                    "stop_sequences": ["STOP"], "max_tokens": 25}
        self.assertEqual(normalize_request(body, "openai"), normalize_request(expected, "anthropic"))

    def test_openai_stop_lacks_anthropic_stop_reason_detail(self):
        body = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "done"}}]}
        with self.assertRaises(NormalizationError):
            normalize_response(body, "openai")
        body["choices"][0]["finish_reason"] = "length"
        expected = {"content": [{"type": "text", "text": "done"}], "stop_reason": "max_tokens"}
        self.assertEqual(normalize_response(body, "openai"), normalize_response(expected, "anthropic"))

    def test_missing_content_and_fallback_are_rejected(self):
        for body in [
            {"stop_reason": "end_turn"},
            {"content": [{"type": "text", "text": "done"}], "stop_reason": "model_fallback"},
            {"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn", "fallback": {}},
        ]:
            with self.subTest(body=body), self.assertRaises(NormalizationError):
                normalize_response(body, "anthropic")

    def test_unanswered_historical_tool_call_cannot_have_its_id_remapped(self):
        body = {"messages": [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "first", "name": "lookup", "input": {}}]},
            {"role": "user", "content": "no result"},
        ]}
        with self.assertRaises(NormalizationError):
            normalize_request(body, "anthropic")

    def test_omitted_anthropic_result_error_means_success(self):
        body = {"messages": [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "first", "name": "lookup", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "first", "content": "ok"}]},
        ]}
        expected = normalize_request(body, "anthropic")
        body["messages"][1]["content"][0]["is_error"] = False
        self.assertEqual(normalize_request(body, "anthropic"), expected)
        body["messages"][1]["content"][0]["is_error"] = True
        self.assertNotEqual(normalize_request(body, "anthropic"), expected)

    def test_documented_context_window_stop_and_null_stop_details(self):
        body = {"content": [], "stop_reason": "model_context_window_exceeded", "stop_details": None}
        expected = {"output": {"message": {"role": "assistant", "content": []}}, "stopReason": "model_context_window_exceeded"}
        self.assertEqual(normalize_response(body, "anthropic"), normalize_response(expected, "converse"))

    def test_duplicate_keys_and_unpaired_surrogates_are_malformed(self):
        from relay_audit.normalize import parse_json_object

        for body in [b'{"text":"one","text":"two"}', br'{"text":"\uD800"}']:
            with self.subTest(body=body), self.assertRaises(NormalizationError):
                parse_json_object(body)

    def test_native_bedrock_stream_log_requires_the_whole_message_and_ignores_metrics(self):
        log = [
            {"type": "message_start", "message": {"content": [], "role": "assistant", "stop_reason": None,
                                                 "stop_details": None, "usage": {}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "answer"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {}},
            {"type": "message_stop", "amazon-bedrock-invocationMetrics": {"outputTokenCount": 1}},
        ]
        expected = {"content": [{"type": "text", "text": "answer"}], "stop_reason": "end_turn"}
        self.assertEqual(normalize_response(log, "anthropic"), normalize_response(expected, "anthropic"))
        self.assertEqual(log[0]["message"]["content"], [])
        with self.assertRaises(NormalizationError):
            normalize_response(log[:-1], "anthropic")

    def test_converse_stream_sdk_event_array_preserves_order_and_stop_sequence(self):
        log = [
            {"messageStart": {"role": "assistant"}},
            {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "answer "}}},
            {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "here"}}},
            {"contentBlockStop": {"contentBlockIndex": 0}},
            {"messageStop": {"stopReason": "stop_sequence", "additionalModelResponseFields": {"stop_sequence": "STOP"}}},
            {"metadata": {"usage": {}, "metrics": {}}},
        ]
        expected = {"content": [{"type": "text", "text": "answer here"}],
                    "stop_reason": "stop_sequence", "stop_sequence": "STOP"}
        self.assertEqual(normalize_response(log, "converse"), normalize_response(expected, "anthropic"))
        with self.assertRaises(NormalizationError):
            normalize_response(log[:4], "converse")
        with self.assertRaises(NormalizationError):
            normalize_response([*log, {"modelStreamErrorException": {}}], "converse")

    def test_converse_stream_reconstructs_tool_json_and_signed_thinking(self):
        log = [
            {"messageStart": {"role": "assistant"}},
            {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"reasoningContent": {"text": "why"}}}},
            {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"reasoningContent": {"signature": "sig"}}}},
            {"contentBlockStop": {"contentBlockIndex": 0}},
            {"contentBlockStart": {"contentBlockIndex": 1, "start": {"toolUse": {"toolUseId": "bedrock", "name": "look"}}}},
            {"contentBlockDelta": {"contentBlockIndex": 1, "delta": {"toolUse": {"input": '{"arg":'}}}},
            {"contentBlockDelta": {"contentBlockIndex": 1, "delta": {"toolUse": {"input": '"value"}'}}}},
            {"contentBlockStop": {"contentBlockIndex": 1}},
            {"messageStop": {"stopReason": "tool_use"}},
        ]
        expected = {"content": [{"type": "thinking", "thinking": "why", "signature": "sig"},
                                {"type": "tool_use", "id": "relay", "name": "look", "input": {"arg": "value"}}],
                    "stop_reason": "tool_use"}
        self.assertEqual(normalize_response(log, "converse"), normalize_response(expected, "anthropic"))

    def test_nullable_citations_are_empty_and_nonempty_citations_are_unsupported(self):
        body = {"content": [{"type": "text", "text": "answer", "citations": None}], "stop_reason": "end_turn"}
        expected = {"content": [{"type": "text", "text": "answer"}], "stop_reason": "end_turn"}
        self.assertEqual(normalize_response(body, "anthropic"), normalize_response(expected, "anthropic"))
        body["content"][0]["citations"] = [{"type": "page_location", "document_index": 0}]
        with self.assertRaises(NormalizationError):
            normalize_response(body, "anthropic")

    def test_bedrock_tool_streaming_flag_only_changes_chunking(self):
        body = {"messages": [{"role": "user", "content": "question"}],
                "tools": [{"name": "lookup", "input_schema": {"type": "object"}}]}
        expected = normalize_request(body, "anthropic")
        body["tools"][0]["eager_input_streaming"] = True
        body["anthropic_beta"] = ["fine-grained-tool-streaming-2025-05-14"]
        self.assertEqual(normalize_request(body, "anthropic"), expected)
        body["anthropic_beta"] += ["context-management-2025-06-27", "interleaved-thinking-2025-05-14"]
        normalized = normalize_request(body, "anthropic")
        self.assertEqual(normalized["anthropic_beta"],
                         ["context-management-2025-06-27", "interleaved-thinking-2025-05-14"])
        body["anthropic_beta"].reverse()
        self.assertEqual(normalize_request(body, "anthropic"), normalized)
        body["anthropic_beta"].append("context-management-2025-06-27")
        with self.assertRaises(NormalizationError):
            normalize_request(body, "anthropic")
