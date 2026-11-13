import json
import unittest
from unittest.mock import patch

from session_start import context


SESSION = "11111111-1111-4111-8111-111111111111"
SERVICE = "22222222-2222-4222-8222-222222222222"
ALIAS = "example-agent"
EVENT = {"hook_event_name": "SessionStart", "source": "resume", "session_id": SESSION}


def response(value):
    return type("Result", (), {"stdout": json.dumps(value)})()


class SessionHookTests(unittest.TestCase):
    def call(self, event=EVENT):
        return context(event, dbond_script="/fixture/dbond.py")

    @patch("session_start.subprocess.run")
    def test_only_session_start_events_query_binding(self, run):
        for event in ({**EVENT, "session_id": "not-a-uuid"}, {**EVENT, "source": "fork"},
                      {**EVENT, "source": "compact"}, {**EVENT, "hook_event_name": "Stop"}):
            self.assertIsNone(self.call(event))
        run.assert_not_called()

    @patch("session_start.subprocess.run")
    def test_pending_bound_session_yields_context(self, run):
        run.side_effect = [response({"alias": ALIAS, "service_id": SERVICE}),
                           response({"alias": ALIAS, "service_id": SERVICE,"state": "pending_listener"})]
        result = self.call()
        self.assertIn(f"dbond session-listen {ALIAS}", result["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(run.call_args_list[0].args[0][2:], ["session-binding", SESSION, "--client", "claude"])
        self.assertEqual(run.call_args_list[1].args[0][2:], ["session-status", ALIAS])

    @patch("session_start.subprocess.run")
    def test_unbound_or_ambiguous_session_is_silent(self, run):
        run.side_effect = OSError("not uniquely enrolled")
        self.assertIsNone(self.call())
        self.assertEqual(run.call_count, 1)

    @patch("session_start.subprocess.run")
    def test_active_or_uncertain_never_rearms(self, run):
        for state in ("subscriber_present", "unknown", "paused", "revoked"):
            run.side_effect = [response({"alias": ALIAS, "service_id": SERVICE}),
                               response({"alias": ALIAS, "service_id": SERVICE,"state": state})]
            self.assertIsNone(self.call())

    @patch("session_start.subprocess.run")
    def test_mismatched_service_is_silent(self, run):
        run.side_effect = [response({"alias": ALIAS, "service_id": SERVICE}),
                           response({"alias": ALIAS, "service_id": SESSION,"state": "pending_listener"})]
        self.assertIsNone(self.call())


if __name__ == "__main__":
    unittest.main()
