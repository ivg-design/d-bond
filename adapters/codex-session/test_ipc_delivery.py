"""Isolated Desktop delivery protocol tests; never contact a running host."""
import unittest
from unittest.mock import patch, Mock
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


class DiscoveryTests(unittest.TestCase):
    def test_auto_resolves_durable_when_local_is_cold(self):
        def discover(thread, host, timeout, path):
            self.assertEqual(thread, THREAD)
            if host == "local":
                raise ipc.CodexWakeError("response timed out")
            return DISCOVERY
        with patch.object(ipc, "_discover_host", side_effect=discover) as probe:
            owner = ipc.discover_owner(THREAD, timeout=0.2)
        self.assertEqual(owner["host_id"], "durable")
        self.assertEqual(probe.call_count, 2)

    def test_auto_preserves_local_owner(self):
        def discover(thread, host, timeout, path):
            if host == "durable":
                raise ipc.IPCRejected("thread-owner-discovery", "no-client-found")
            return DISCOVERY
        with patch.object(ipc, "_discover_host", side_effect=discover):
            self.assertEqual(ipc.discover_owner(THREAD)["host_id"], "local")

    def test_known_host_never_probes_or_redirects_to_another_host(self):
        with patch.object(ipc, "_discover_host", return_value=DISCOVERY) as probe:
            owner = ipc.discover_owner(THREAD, host_id="durable")
        self.assertEqual(owner["host_id"], "durable")
        probe.assert_called_once_with(THREAD, "durable", ipc.DEFAULT_TIMEOUT, ipc.SOCKET_PATH)
        with patch.object(ipc, "_discover_host", side_effect=ipc.CodexWakeError("cold")) as probe:
            with self.assertRaises(ipc.OwnerUnavailable):
                ipc.discover_owner(THREAD, host_id="durable")
        probe.assert_called_once()

    def test_multiple_host_owners_are_ambiguous_even_if_same_client(self):
        with patch.object(ipc, "_discover_host", return_value=DISCOVERY):
            with self.assertRaisesRegex(ipc.OwnerUnavailable, "ambiguous"):
                ipc.discover_owner(THREAD)

    def test_cold_and_missing_hosts_are_unavailable_without_delivery(self):
        with patch.object(ipc, "_discover_host", side_effect=ipc.CodexWakeError("timed out")):
            with self.assertRaises(ipc.OwnerUnavailable):
                ipc.discover_owner(THREAD)

    def test_known_host_requires_untrusted_capability(self):
        for response in [{"handledByClientId": "owner", "result": {}},
                         {"handledByClientId": "owner", "result": {"supportsUntrustedAppInput": False}}]:
            with self.subTest(response=response), patch.object(ipc, "_discover_host", return_value=response):
                with self.assertRaises(ipc.CodexWakeError):
                    ipc.discover_owner(THREAD, host_id="durable")

    def test_discovery_host_is_inside_params_without_follower_version_increment(self):
        with patch.object(ipc, "_Client") as factory:
            factory.return_value.request.return_value = DISCOVERY
            ipc.discover_owner(THREAD, host_id="durable")
        factory.return_value.request.assert_called_once_with("thread-owner-discovery",
            {"hostId": "durable", "conversationId": THREAD}, version=1)

    def test_unsupported_host_rejected_before_connection(self):
        with patch.object(ipc, "_Client") as factory:
            with self.assertRaises(ValueError):
                ipc.discover_owner(THREAD, host_id="unregistered")
        factory.assert_not_called()


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

    def test_durable_steer_targets_host_in_envelope_at_incremented_version(self):
        result, client = self.deliver([DISCOVERY, RECEIPT], dict(OWNER, host_id="durable"))
        self.assertEqual(result["host_id"], "durable")
        discovery, steer = client.request.call_args_list
        self.assertEqual(discovery.args[1]["hostId"], "durable")
        self.assertEqual(discovery.kwargs, {"version": 1, "target_client_id": "desktop-owner"})
        self.assertEqual(steer.kwargs,
            {"version": 2, "target_client_id": "desktop-owner", "host_id": "durable"})
        self.assertNotIn("hostId", steer.args[1])

    def test_durable_start_keeps_host_and_uses_version_three(self):
        result, client = self.deliver([DISCOVERY, ipc.IPCRejected("steer", ENDED), START_RECEIPT],
                                      dict(OWNER, host_id="durable"))
        self.assertEqual(result["phase"], "started")
        self.assertEqual(client.request.call_args.kwargs,
            {"version": 3, "target_client_id": "desktop-owner", "host_id": "durable"})

    def test_durable_stale_owner_never_dispatches_or_falls_back_to_local(self):
        with patch.object(ipc, "_Client") as factory:
            client = factory.return_value
            client.request.return_value = dict(DISCOVERY, handledByClientId="changed")
            with self.assertRaises(ipc.OwnerUnavailable):
                ipc.deliver_to_owner(THREAD, "hello", dict(OWNER, host_id="durable"))
        client.request.assert_called_once()
        self.assertEqual(client.request.call_args.args[1]["hostId"], "durable")

    def test_request_serializes_host_in_envelope(self):
        client = object.__new__(ipc._Client)
        client.timeout = 1
        client.client_id = "sender"
        client._send_frame = Mock()
        def response():
            sent = client._send_frame.call_args.args[0]
            return {"type": "response", "requestId": sent["requestId"], "resultType": "success"}
        client._read_frame = response
        client.request("thread-follower-read-model-settings", {"conversationId": THREAD},
                       version=2, target_client_id="desktop-owner", host_id="durable")
        sent = client._send_frame.call_args.args[0]
        self.assertEqual(sent["hostId"], "durable")
        self.assertEqual(sent["version"], 2)
        self.assertNotIn("hostId", sent["params"])

    def test_raw_string_turn_receipt(self):
        result, _ = self.deliver([DISCOVERY, {"result": {"result": "turn-string"}}])
        self.assertEqual(result["receipt"], "turn-string")


if __name__ == "__main__":
    unittest.main()
