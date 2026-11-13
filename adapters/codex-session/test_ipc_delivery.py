"""Isolated Desktop delivery protocol tests; never contact a running host."""
import unittest
from unittest.mock import patch
import ipc

THREAD = "11111111-1111-4111-8111-111111111111"
OWNER = {"thread_id": THREAD, "handled_by_client_id": "desktop-owner",
         "supports_untrusted_app_input": True}
DISCOVERY = {"handledByClientId": "desktop-owner",
             "result": {"supportsUntrustedAppInput": True}}
RECEIPT = {"result": {"result": {"turnId": "turn-exact"}}}
STARTED_ID = "22222222-2222-4222-8222-222222222222"
START_RECEIPT = {"result": {"result": {"turn": {"id": STARTED_ID, "status": "inProgress"}}}}
ENDED = f"Cannot steer conversation {THREAD} because its active turn already ended"


class DeliveryTests(unittest.TestCase):
    def deliver(self, results, owner=OWNER):
        with patch.object(ipc, "_Client") as factory:
            client = factory.return_value
            client.request.side_effect = results
            result = ipc.deliver_to_owner(THREAD, "[D-Bond external message] hello", owner)
            return result, client

    def test_busy_steers_exact_owner_without_permission_changes(self):
        result, client = self.deliver([DISCOVERY, RECEIPT])
        self.assertEqual(result["phase"], "steered")
        self.assertEqual(result["receipt"], "turn-exact")
        call = client.request.call_args_list[1]
        self.assertEqual(call.args[0], "thread-follower-steer-turn")
        self.assertEqual(call.args[1]["conversationId"], THREAD)
        self.assertEqual(call.kwargs, {"version": 1, "target_client_id": "desktop-owner"})
        self.assertEqual(call.args[1]["restoreMessage"]["context"], {})
        self.assertNotIn("permissions", repr(call))
        client.initialize.assert_called_once()
        client.close.assert_called_once()

    def test_idle_starts_only_after_exact_pre_dispatch_inactive_error(self):
        result, client = self.deliver([DISCOVERY,
            ipc.IPCRejected("thread-follower-steer-turn", ENDED), START_RECEIPT])
        self.assertEqual(result["phase"], "started")
        self.assertEqual(result["receipt"], STARTED_ID)
        call = client.request.call_args_list[-1]
        self.assertEqual(call.args[0], "thread-follower-start-turn")
        self.assertEqual(call.kwargs["version"], 2)
        request = call.args[1]["turnStart"]["request"]
        self.assertEqual(request["threadId"], THREAD)
        self.assertEqual(request["clientUserMessageId"], result["client_user_message_id"])
        self.assertEqual(set(request), {"threadId", "input", "clientUserMessageId"})

    def test_idle_rejects_wrong_or_malformed_receipt_shapes(self):
        for result in [{"turnId": STARTED_ID}, STARTED_ID,
                       {"turn": {"id": "not-a-uuid"}}, {"turn": {}}, {}]:
            with self.subTest(result=result), self.assertRaises(ipc.DeliveryUncertain):
                self.deliver([DISCOVERY, ipc.IPCRejected("steer", ENDED),
                              {"result": {"result": result}}])

    def test_owner_untrusted_capability_is_never_assumed(self):
        for result in [{}, {"supportsUntrustedAppInput": False}]:
            with self.subTest(result=result), self.assertRaises(ipc.OwnerUnavailable):
                self.deliver([{"handledByClientId": "desktop-owner", "result": result}])

    def test_owner_disappears_before_idle_start(self):
        with self.assertRaises(ipc.OwnerUnavailable):
            self.deliver([DISCOVERY, ipc.IPCRejected("steer", ENDED),
                          ipc.IPCRejected("start", "no-client-found")])

    def test_ambiguous_steer_never_starts(self):
        with patch.object(ipc, "_Client") as factory:
            client = factory.return_value
            client.request.side_effect = [DISCOVERY, ipc.CodexWakeError("timed out")]
            with self.assertRaises(ipc.DeliveryUncertain):
                ipc.deliver_to_owner(THREAD, "hello", OWNER)
            self.assertEqual(client.request.call_count, 2)
            client.close.assert_called_once()

    def test_permission_rejection_never_falls_back(self):
        with self.assertRaises(ipc.DeliveryUncertain):
            self.deliver([DISCOVERY, ipc.IPCRejected("steer", "permission denied")])

    def test_stale_owner_does_not_dispatch(self):
        with self.assertRaises(ipc.OwnerUnavailable):
            self.deliver([{"handledByClientId": "another", "result": DISCOVERY["result"]}])

    def test_router_unavailable_is_explicit(self):
        with self.assertRaises(ipc.OwnerUnavailable):
            self.deliver([DISCOVERY, ipc.IPCRejected("steer", "no-client-found")])

    def test_missing_receipt_is_uncertain(self):
        with self.assertRaises(ipc.DeliveryUncertain):
            self.deliver([DISCOVERY, {"result": {"result": {}}}])

    def test_thread_mismatch_rejected_before_connection(self):
        with patch.object(ipc, "_Client") as factory:
            with self.assertRaises(ValueError):
                ipc.deliver_to_owner(THREAD, "hello", dict(OWNER, thread_id="other"))
            factory.assert_not_called()

    def test_raw_string_turn_receipt(self):
        result, _ = self.deliver([DISCOVERY, {"result": {"result": "turn-string"}}])
        self.assertEqual(result["receipt"], "turn-string")


if __name__ == "__main__":
    unittest.main()
