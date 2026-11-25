import json
import unittest

from relay_audit.sse import SSECollector, SSEError


def event(kind, **data):
    payload = json.dumps({"type": kind, **data}, ensure_ascii=False)
    return f"event: {kind}\r\ndata: {payload}\r\n\r\n".encode()


def start():
    return event("message_start", message={"type": "message", "role": "assistant",
                                           "content": [], "model": "example", "stop_reason": None})


def end(reason="end_turn"):
    return event("message_delta", delta={"stop_reason": reason, "stop_sequence": None}, usage={}) + event("message_stop")


class SSETests(unittest.TestCase):
    def test_reconstructs_utf8_thinking_text_and_tool_input_across_byte_chunks(self):
        source = (
            start() + event("ping")
            + event("content_block_start", index=0, content_block={"type": "thinking", "thinking": ""})
            + event("content_block_delta", index=0, delta={"type": "thinking_delta", "thinking": "思"})
            + event("content_block_delta", index=0, delta={"type": "signature_delta", "signature": "sig"})
            + event("content_block_stop", index=0)
            + event("content_block_start", index=1, content_block={"type": "text", "text": ""})
            + event("content_block_delta", index=1, delta={"type": "text_delta", "text": " 開始\n"})
            + event("content_block_stop", index=1)
            + event("content_block_start", index=2, content_block={"type": "tool_use", "id": "test", "name": "lookup", "input": {}})
            + event("content_block_delta", index=2, delta={"type": "input_json_delta", "partial_json": '{"a":'})
            + event("content_block_delta", index=2, delta={"type": "input_json_delta", "partial_json": '"字"}'})
            + event("content_block_stop", index=2)
            + end("tool_use")
        )
        collector = SSECollector(max_bytes=len(source))
        for byte in source:
            collector.feed(bytes([byte]))
        message = collector.finish()
        self.assertEqual(message["model"], "example")
        self.assertEqual(message["stop_reason"], "tool_use")
        self.assertEqual(message["content"], [
            {"type": "thinking", "thinking": "思", "signature": "sig"},
            {"type": "text", "text": " 開始\n"},
            {"type": "tool_use", "id": "test", "name": "lookup", "input": {"a": "字"}},
        ])

    def test_incomplete_message_rejected(self):
        collector = SSECollector(max_bytes=1000)
        collector.feed(start())
        with self.assertRaises(SSEError):
            collector.finish()

    def test_out_of_order_block_rejected(self):
        collector = SSECollector(max_bytes=1000)
        with self.assertRaises(SSEError):
            collector.feed(start() + event("content_block_start", index=2, content_block={"type": "text", "text": "a"}))

    def test_fallback_and_stream_error_rejected(self):
        for bad_event in [event("message_delta", delta={"stop_reason": "model_fallback"}),
                          event("error", error={"type": "overloaded_error"})]:
            with self.subTest(bad_event=bad_event):
                collector = SSECollector(max_bytes=1000)
                with self.assertRaises(SSEError):
                    collector.feed(start() + bad_event)

    def test_partial_event_and_total_limit(self):
        collector = SSECollector(max_bytes=12)
        collector.feed(b"data: 12345")
        with self.assertRaises(SSEError):
            collector.feed(b"67")

    def test_documented_null_stop_details_is_accepted(self):
        collector = SSECollector(max_bytes=1000)
        collector.feed(start() + event("message_delta", delta={"stop_reason": "end_turn", "stop_details": None})
                       + event("message_stop"))
        self.assertIsNone(collector.finish()["stop_details"])

    def test_nullable_citations_in_stream_text_start(self):
        collector = SSECollector(max_bytes=1000)
        collector.feed(start() + event("content_block_start", index=0, content_block={"type": "text", "text": "", "citations": None})
                       + event("content_block_stop", index=0) + end())
        self.assertEqual(collector.finish()["content"][0]["text"], "")
