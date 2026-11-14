import contextlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

import cbor2

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import relay


class BackplaneRelayTests(unittest.TestCase):
    config = {"thread": "t-design", "provider": "claude"}

    @staticmethod
    def socket(rows):
        ws = MagicMock()
        ws.recv.side_effect = [cbor2.dumps(row) for row in rows]
        return ws

    def test_loopback_pin(self):
        self.assertEqual(relay.origin_url("http://127.0.0.1:5163/"), "http://127.0.0.1:5163")
        for origin in ["http://localhost:5163", "https://127.0.0.1:443", "http://127.0.0.1:123?secret=wrong",
                       "http://127.0.0.1@host:5163", "http://127.0.0.1:5163/mcp"]:
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                relay.origin_url(origin)

    def test_chunked_project_replay_checks_the_final_state(self):
        tag = lambda value: cbor2.CBORTag(6, value)
        log = {1: tag(14), "more": True, 7: [{0: tag(26), 2: "t-design", 14: "p", 22: "codex"}]}
        update = {1: tag(3), "boot": False, 7: [{0: tag(45), 2: "t-design", 22: "claude"}]}
        relay.wait_for_target(self.socket([log, update]), self.config)
        for change in [{0: tag(28), 2: "t-design", 49: True}, {0: tag(30), 2: "t-design"},
                       {0: tag(45), 2: "different-thread", 22: "claude"}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                relay.wait_for_target(self.socket([log, {**update, 7: [change]}]), self.config)

    def test_distinct_receivers_and_stable_replay(self):
        ws = self.socket([{1: cbor2.CBORTag(6, 5), 2: 1, 5: True}] * 3)
        event = {"sender_id": "sender", "sequence": 19, "source_id": "review-1",
                 "in_reply_to": None, "text": "Quotes, a newline\n and `$(kept as text)`"}
        with patch.object(relay, "hub", side_effect=lambda cfg: contextlib.nullcontext(ws)):
            for peer in ("first", "first", "second"):
                relay.deliver(self.config, event, "example", peer)
        items = [cbor2.loads(call.args[0])["p"] for call in ws.send_binary.call_args_list]
        self.assertEqual(items[0]["msg"], items[1]["msg"])
        self.assertNotEqual(items[1]["msg"], items[2]["msg"])
        self.assertEqual(items[0]["thread"], self.config["thread"])
        self.assertEqual(json.loads(items[0]["text"].split("\n", 1)[1])["text"], event["text"])

    def test_hub_rejection_is_not_a_receipt(self):
        ws = self.socket([{1: "reply", 2: 1, 5: False}])
        event = {"sender_id": "a", "source_id": "b", "sequence": 1, "in_reply_to": None, "text": "hi"}
        with patch.object(relay, "hub", return_value=contextlib.nullcontext(ws)), self.assertRaises(ValueError):
            relay.deliver(self.config, event, "example", "recipient")


if __name__ == '__main__':
    unittest.main()
