"""The worker lifecycle: transitions of a worker record, and what wait/status see."""

import shutil
import tempfile
import unittest

import fakes  # noqa: F401  (sets sys.path)

from fleetlib import lifecycle as lc
from fleetlib import state as st


class Worker(unittest.TestCase):
    """One worker driven through transitions, with a fake clock, a fake glyph sink and a fake screen."""

    PANE = "%1"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fleet-test-")
        self.t = 1000
        self.glyphs = []
        self.screen = "quiet"  # what the fake screen classifier reports
        self.life = lc.Lifecycle(st.Store(self.dir), lambda: self.t,
                                 glyph=lambda pane, value: self.glyphs.append(value))
        self.life.create(self.PANE, name="w", session="fleet")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def event(self, name, **payload):
        return self.life.hook_event(self.PANE, name, payload)

    def send(self):
        return self.life.prompt_sent(self.PANE)

    def look(self):
        return self.screen

    def wait_view(self):
        return self.life.wait_view(self.PANE, self.look, self.look)

    def rec(self):
        return self.life.load(self.PANE)


class Turns(Worker):
    def test_sent_submitted_and_stopped_is_finished(self):
        self.event("SessionStart")
        self.send()
        self.event("UserPromptSubmit", prompt="go")
        self.assertEqual(self.wait_view(), "busy")
        self.event("Stop", last_assistant_message="ok")
        self.assertEqual(self.wait_view(), "finished")
        self.assertEqual((self.rec()["state"], self.rec()["outcome"]), ("idle", "finished"))

    def test_ordering_does_not_depend_on_time(self):
        # the clock never moves: the previous turn's Stop lands in the same second as the send
        self.event("UserPromptSubmit", prompt="first")
        self.send()
        self.event("Stop", last_assistant_message="first reply")
        self.assertEqual(self.wait_view(), "busy")
        self.event("UserPromptSubmit", prompt="second")
        self.event("Stop", last_assistant_message="second reply")
        self.assertEqual(self.wait_view(), "finished")

    def test_old_stop_after_the_send_is_the_previous_turn(self):
        # sent at t, the previous turn's Stop at t+1, the new prompt submitted at t+2, its Stop at t+5
        self.event("UserPromptSubmit", prompt="first")
        self.send()
        self.t += 1
        self.event("Stop", last_assistant_message="first reply")
        self.assertEqual(self.wait_view(), "busy")
        self.t += 1
        self.event("UserPromptSubmit", prompt="second")
        self.t += 3
        self.event("Stop", last_assistant_message="second reply")
        self.assertEqual(self.wait_view(), "finished")

    def test_prompt_queued_into_a_turn_with_tool_calls(self):
        # Claude Code folds the queued prompt into the running turn at its next tool call: one Stop
        self.event("UserPromptSubmit", prompt="first")
        self.screen = "busy"
        self.send()
        self.event("UserPromptSubmit", prompt="queued")  # fires when queued, not when it runs
        self.event("PostToolUse")
        self.t += 10
        self.screen = "quiet"
        self.event("Stop", last_assistant_message="both done")
        self.t += 2
        self.assertEqual(self.wait_view(), "finished")

    def test_prompt_queued_behind_a_turn_without_tool_calls(self):
        # The queued prompt runs as a turn of its own after the first turn's Stop, and no hook says
        # it started: only the screen shows it is running.
        self.event("UserPromptSubmit", prompt="first")
        self.send()
        self.event("UserPromptSubmit", prompt="queued")
        self.t += 5
        self.event("Stop", last_assistant_message="first reply")
        self.screen = "busy"
        self.t += 1
        self.assertEqual(self.wait_view(), "busy")
        self.screen = "quiet"
        self.event("Stop", last_assistant_message="queued reply")
        self.t += 2
        self.assertEqual(self.wait_view(), "finished")
        self.assertEqual(self.rec()["last_message"], "queued reply")

    def test_a_fresh_stop_is_checked_twice(self):
        # right after a Stop, a queued prompt may not have started: one quiet screen is not enough
        self.send()
        self.event("UserPromptSubmit", prompt="go")
        self.event("Stop")
        looks = iter(["quiet", "busy"])
        self.assertEqual(self.life.wait_view(self.PANE, lambda: next(looks), lambda: next(looks)), "busy")

    def test_send_now_drops_the_running_turn(self):
        # Ctrl+Enter: the dropped turn fires no Stop; the new prompt is submitted and stops
        self.event("UserPromptSubmit", prompt="first")
        self.send()
        self.event("UserPromptSubmit", prompt="now")
        self.event("Stop", last_assistant_message="now reply")
        self.t += 2
        self.assertEqual(self.wait_view(), "finished")

    def test_interrupt_seen_on_screen(self):
        self.send()
        self.event("UserPromptSubmit", prompt="go")
        self.t += lc.STALE
        self.screen = "quiet"
        self.assertEqual(self.wait_view(), "interrupted")
        r = self.rec()
        self.assertEqual((r["state"], r["outcome"]), ("idle", "interrupted"))
        self.assertEqual(self.glyphs[-1], "interrupted")

    def test_a_new_session_has_no_last_turn(self):
        self.event("UserPromptSubmit", prompt="go")
        self.event("Stop")
        self.event("SessionStart")  # /clear
        self.assertEqual((self.rec()["state"], self.rec()["outcome"]), ("idle", "none"))
        self.assertEqual(self.wait_view(), "idle")

    def test_never_sent_returns_on_idle(self):
        self.event("SessionStart")
        self.assertEqual(self.wait_view(), "idle")
        self.event("UserPromptSubmit", prompt="typed by hand")
        self.assertEqual(self.wait_view(), "busy")
        self.event("Stop")
        self.t += 2
        self.assertEqual(self.wait_view(), "finished")

    def test_exited(self):
        self.send()
        self.event("SessionEnd")
        self.assertEqual(self.wait_view(), "exited")

    def test_starting_is_not_over(self):
        self.assertEqual(self.wait_view(), "starting")


class Dialogs(Worker):
    def test_permission_blocks_and_answering_resumes(self):
        self.send()
        self.event("UserPromptSubmit", prompt="go")
        self.event("PermissionRequest", tool_name="Bash")
        self.assertEqual(self.wait_view(), "blocked")
        self.assertEqual(self.rec()["detail"], "permission: Bash")
        self.life.dialog_answered(self.PANE)
        self.assertEqual((self.rec()["state"], self.rec()["detail"]), ("busy", ""))

    def test_question_detail(self):
        self.event("PermissionRequest", tool_name="AskUserQuestion")
        self.assertEqual(self.rec()["detail"], "question")
        self.event("PermissionRequest")
        self.assertEqual(self.rec()["detail"], "permission: ?")

    def test_answering_leaves_other_states_alone(self):
        self.event("SessionStart")
        self.life.dialog_answered(self.PANE)
        self.assertEqual(self.rec()["state"], "idle")

    def test_dialog_seen_on_screen_blocks(self):
        self.event("UserPromptSubmit", prompt="go")
        self.t += lc.STALE
        self.screen = "dialog"
        self.assertEqual(self.wait_view(), "blocked")
        self.assertEqual(self.rec()["detail"], "dialog on screen")


class Clocks(Worker):
    def test_notification_moves_neither_clock(self):
        self.event("UserPromptSubmit", prompt="go")
        before = (self.rec()["state_since"], self.rec()["heard_at"])
        self.t += 50
        self.event("Notification", notification_type="idle_prompt")
        self.assertEqual((self.rec()["state_since"], self.rec()["heard_at"]), before)
        self.assertEqual(self.rec()["state"], "busy")

    def test_tool_use_is_news_but_not_a_state_change(self):
        self.event("UserPromptSubmit", prompt="go")
        self.t += 50
        self.event("PostToolUse")
        self.assertEqual((self.rec()["state_since"], self.rec()["heard_at"]), (1000, 1050))

    def test_a_busy_worker_heard_from_recently_is_not_checked_on_screen(self):
        self.event("UserPromptSubmit", prompt="go")
        self.t += lc.STALE + 10
        self.event("PostToolUse")
        self.screen = "quiet"
        self.assertEqual(self.wait_view(), "busy")

    def test_every_write_bumps_seq(self):
        seqs = [self.rec()["seq"]]
        for ev in ("SessionStart", "UserPromptSubmit", "Notification", "Stop"):
            self.event(ev)
            seqs.append(self.rec()["seq"])
        self.assertEqual(seqs, [0, 1, 2, 3, 4])


class Glyph(Worker):
    def test_glyph_follows_state_and_outcome(self):
        self.event("SessionStart")
        self.event("UserPromptSubmit", prompt="go")
        self.event("PermissionRequest", tool_name="Bash")
        self.event("PostToolUse")
        self.event("Notification")
        self.event("Stop")
        self.event("SessionEnd")
        self.assertEqual(self.glyphs, ["starting", "idle", "busy", "blocked", "busy", "finished", "exited"])

    def test_a_failing_sink_does_not_undo_the_transition(self):
        def broken(pane, value):
            raise RuntimeError("tmux gone")
        self.life.glyph = broken
        self.event("Stop")
        self.assertEqual(self.rec()["outcome"], "finished")


class Records(Worker):
    def test_initial_record(self):
        r = self.rec()
        self.assertEqual((r["name"], r["pane"], r["state"], r["outcome"], r["seq"]), ("w", "%1", "starting", "none", 0))
        self.assertEqual((r["state_since"], r["heard_at"], r["created"]), (1000, 1000, 1000))

    def test_events_for_other_panes_create_nothing(self):
        self.assertIsNone(self.life.hook_event("%9", "Stop", {}))
        self.assertFalse(self.life.is_tracked("%9"))

    def test_unknown_event(self):
        self.assertIsNone(self.event("Bogus"))

    def test_hook_fields(self):
        self.event("UserPromptSubmit", prompt="x" * 300, session_id="s1", transcript_path="/t.jsonl", cwd="/w")
        r = self.rec()
        self.assertEqual(r["prompt"], "x" * 200)
        self.assertEqual((r["session_id"], r["transcript_path"], r["cwd"]), ("s1", "/t.jsonl", "/w"))
        self.event("PermissionRequest", tool_name="Write")
        self.event("Stop", last_assistant_message="all done")
        self.assertEqual((self.rec()["last_message"], self.rec()["detail"]), ("all done", ""))




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


if __name__ == "__main__":
    unittest.main()
