"""The worker lifecycle: transitions of a worker record, and what wait/status see."""

import unittest

import fakes  # noqa: F401  (sets sys.path)

from fleetlib import lifecycle as lc


class ReconcileDecision(unittest.TestCase):
    NOW = 1000

    def decide(self, state, age, first, second=None):
        looks = []

        def look():
            looks.append("look")
            return first

        def recheck():
            looks.append("recheck")
            return second

        d = lc.reconcile_decision(state, self.NOW - age, self.NOW, look, recheck)
        return d, looks

    def test_only_busy_and_blocked_are_corrected(self):
        for s in ("starting", "idle", "done", "exited"):
            d, looks = self.decide(s, 100, "dialog")
            self.assertIsNone(d, s)
            self.assertEqual(looks, [], s)

    def test_fresh_state_is_trusted(self):
        d, looks = self.decide("busy", lc.STALE - 1, "quiet", "quiet")
        self.assertIsNone(d)
        self.assertEqual(looks, [])

    def test_stale_state_is_checked(self):
        d, _ = self.decide("busy", lc.STALE, "quiet", "quiet")
        self.assertEqual(d, ("idle", "interrupted"))

    def test_busy_with_dialog_becomes_blocked(self):
        self.assertEqual(self.decide("busy", 10, "dialog")[0], ("blocked", "dialog on screen"))

    def test_blocked_with_dialog_stays(self):
        self.assertIsNone(self.decide("blocked", 10, "dialog")[0])

    def test_blocked_without_dialog_but_busy_becomes_busy(self):
        # the dialog was answered by hand; PostToolUse has not fired yet
        self.assertEqual(self.decide("blocked", 10, "busy")[0], ("busy", ""))

    def test_busy_and_busy_on_screen_stays(self):
        d, looks = self.decide("busy", 10, "busy")
        self.assertIsNone(d)
        self.assertEqual(looks, ["look"])

    def test_one_quiet_screen_is_not_enough(self):
        # a redraw between frames: still busy (the record is rewritten, refreshing ts, as in bash)
        d, looks = self.decide("busy", 10, "quiet", "busy")
        self.assertEqual(d, ("busy", ""))
        self.assertEqual(looks, ["look", "recheck"])

    def test_quiet_then_dialog(self):
        self.assertEqual(self.decide("busy", 10, "quiet", "dialog")[0], ("blocked", "dialog on screen"))

    def test_cancelled_dialog_becomes_idle(self):
        self.assertEqual(self.decide("blocked", 10, "quiet", "quiet")[0], ("idle", "interrupted"))

    def test_unreadable_screen_changes_nothing(self):
        self.assertIsNone(self.decide("busy", 10, None)[0])
        self.assertIsNone(self.decide("busy", 10, "quiet", None)[0])

    def test_quiet_then_busy_from_blocked(self):
        self.assertEqual(self.decide("blocked", 10, "quiet", "busy")[0], ("busy", ""))


class WaitRule(unittest.TestCase):
    """A done only counts if its Stop came at or after the last send."""

    def test_stop_after_send_counts(self):
        self.assertEqual(lc.wait_state({"state": "done", "event": "Stop", "ts": 105, "sent_at": 100}), "done")

    def test_stop_same_second_counts(self):
        self.assertEqual(lc.wait_state({"state": "done", "event": "Stop", "ts": 100, "sent_at": 100}), "done")

    def test_stop_before_send_is_the_previous_turn(self):
        self.assertEqual(lc.wait_state({"state": "done", "event": "Stop", "ts": 99, "sent_at": 100}), "busy")

    def test_done_without_a_stop_event_does_not_count(self):
        # e.g. a Notification after the Stop moved `event` on; state stays done but ts is the
        # notification's. The bash version required event == Stop too.
        self.assertEqual(lc.wait_state({"state": "done", "event": "Notification", "ts": 105, "sent_at": 100}), "busy")

    def test_never_sent(self):
        self.assertEqual(lc.wait_state({"state": "done", "event": "Stop", "ts": 5}), "done")

    def test_other_states_pass_through(self):
        for s in ("idle", "busy", "blocked", "exited", "starting"):
            self.assertEqual(lc.wait_state({"state": s, "event": "Stop", "ts": 1, "sent_at": 9}), s)


if __name__ == "__main__":
    unittest.main()
