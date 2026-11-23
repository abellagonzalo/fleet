"""The worker lifecycle: transitions of a worker record, and what wait/status see."""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from fakes import fixture

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

    def test_a_stop_during_the_screen_check_wins(self):
        # reconcile pauses before its second look; a Stop landing then must not be overwritten
        self.send()
        self.event("UserPromptSubmit", prompt="go")
        self.t += lc.STALE

        def recheck():
            self.event("Stop", last_assistant_message="made it")
            return "quiet"
        self.life.reconcile(self.PANE, lambda: "quiet", recheck)
        self.assertEqual((self.rec()["state"], self.rec()["outcome"]), ("idle", "finished"))
        self.assertEqual(self.wait_view(), "finished")

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


class TranscriptWorker(Worker):
    """A worker whose transcript the test writes. Fixtures: tests/fixtures/transcript_*.jsonl (#15)."""

    def setUp(self):
        super().setUp()
        self.tpath = os.path.join(self.dir, "transcript.jsonl")
        self.event("SessionStart", transcript_path=self.tpath)

    def records(self, name):
        return [json.loads(text) for text in fixture("transcript_%s.jsonl" % name).splitlines() if text.strip()]

    def write(self, records):
        with open(self.tpath, "a", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def submit(self, records):
        """`fleet send` and the UserPromptSubmit of the first prompt in `records`."""
        prompt = next(r for r in records if r.get("promptSource"))
        self.send()
        self.event("UserPromptSubmit", prompt="go", prompt_id=prompt["promptId"], transcript_path=self.tpath)
        return prompt["promptId"]


class TranscriptInterrupts(TranscriptWorker):
    """Escape and rejected or cancelled dialogs fire no hook: the transcript's interrupt marker ends
    the turn."""

    def setUp(self):
        super().setUp()
        self.screen = "busy"  # the screen never decides here

    def assert_interrupted(self):
        self.assertEqual(self.wait_view(), "interrupted")
        r = self.rec()
        self.assertEqual((r["state"], r["outcome"], r["ended_seq"]), ("idle", "interrupted", r["seq"]))

    def test_interrupt_during_text(self):
        recs = self.records("interrupt_text")
        self.submit(recs)
        self.write(recs[:-1])
        self.assertEqual(self.wait_view(), "busy")
        self.write(recs[-1:])
        self.assert_interrupted()

    def test_interrupt_during_a_tool_call(self):
        recs = self.records("interrupt_tool")
        self.submit(recs)
        self.write(recs)
        self.assert_interrupted()

    def test_a_rejected_or_cancelled_dialog_ends_the_turn_from_blocked(self):
        for name in ("permission_rejected", "permission_escape", "question_escape"):
            with self.subTest(name):
                recs = self.records(name)
                pid = self.submit(recs)
                tool = next(i for i, r in enumerate(recs) if r["type"] == "assistant"
                            and any(c.get("type") == "tool_use" for c in r["message"]["content"]))
                self.write(recs[:tool + 1])
                self.event("PermissionRequest", tool_name="Write", prompt_id=pid)
                self.assertEqual(self.wait_view(), "blocked")
                self.write(recs[tool + 1:])
                self.assert_interrupted()

    def test_a_rejection_with_feedback_goes_on(self):
        recs = self.records("permission_rejected_with_feedback")
        pid = self.submit(recs)
        second_try = [i for i, r in enumerate(recs) if r["type"] == "assistant"][-1]
        self.write(recs[:second_try - 1])
        self.event("PermissionRequest", tool_name="Write", prompt_id=pid)
        self.write(recs[second_try - 1:second_try + 1])  # the denial with feedback; Claude tries again
        self.t += lc.STALE + 10
        self.assertEqual(self.wait_view(), "blocked")
        self.assertEqual(self.rec()["outcome"], "none")

    def test_a_marker_from_an_earlier_turn_never_ends_the_current_one(self):
        old = self.records("interrupt_text")
        self.submit(old)
        self.write(old[:-1])
        self.assertEqual(self.wait_view(), "busy")
        new = self.records("finished")
        self.submit(new)  # the next prompt is submitted before the old turn's marker is read
        self.write(old[-1:] + new[:2])
        self.t += lc.STALE + 10
        self.assertEqual(self.wait_view(), "busy")
        self.assertEqual(self.rec()["outcome"], "none")

    def test_an_escape_before_any_reply_falls_back_to_the_screen(self):
        recs = self.records("rewind")
        self.submit(recs)
        self.write(recs[:2])  # the dropped prompt, and nothing after it
        self.screen = "quiet"
        self.assertEqual(self.wait_view(), "busy")  # not stale yet
        self.t += lc.STALE
        self.assertEqual(self.wait_view(), "interrupted")

    def test_an_unreadable_transcript_falls_back_to_the_screen(self):
        self.submit(self.records("interrupt_text"))  # nothing is ever written to the file
        self.screen = "quiet"
        self.t += lc.STALE
        self.assertEqual(self.wait_view(), "interrupted")

    def test_a_transcript_without_prompt_records_falls_back_to_the_screen(self):
        self.submit(self.records("interrupt_text"))
        self.write([{"type": "something-else"}])
        self.screen = "quiet"
        self.t += lc.STALE
        self.assertEqual(self.wait_view(), "interrupted")

    def test_a_blocked_worker_whose_prompt_is_not_in_the_transcript_falls_back_to_the_screen(self):
        self.write(self.records("finished"))  # an earlier turn; this one's prompt never shows up
        self.submit(self.records("interrupt_tool"))
        self.event("PermissionRequest", tool_name="Bash")
        self.screen = "quiet"
        self.t += lc.STALE
        self.assertEqual(self.wait_view(), "interrupted")

    def test_after_clear_only_the_new_transcript_counts(self):
        old = self.records("interrupt_text")
        self.submit(old)
        self.write(old[:-1])
        self.assertEqual(self.wait_view(), "busy")
        self.event("SessionEnd", prompt_id=old[0]["promptId"])
        self.write(old[-1:])  # never read: the session is over
        self.tpath = os.path.join(self.dir, "after-clear.jsonl")
        self.event("SessionStart", transcript_path=self.tpath)
        self.write(self.records("clear"))
        self.life.reconcile(self.PANE, self.look, self.look)
        self.assertEqual((self.rec()["state"], self.rec()["outcome"]), ("idle", "none"))
        recs = self.records("interrupt_tool")
        self.submit(recs)
        self.assertEqual(self.wait_view(), "busy")
        self.write(recs)
        self.assert_interrupted()
        self.assertEqual(self.rec()["transcript"]["path"], self.tpath)


class TranscriptQueuedPrompts(TranscriptWorker):
    """A prompt sent while a worker is busy is queued. Its UserPromptSubmit fires when it is queued,
    carrying the running turn's prompt id; when it later runs as a turn of its own no hook fires,
    so the transcript says when it started. The screen stays quiet: only the transcript can keep
    `wait` waiting."""

    def setUp(self):
        super().setUp()
        self.screen = "quiet"

    def first_index(self, recs, pred):
        return next(i for i, r in enumerate(recs) if pred(r))

    def queue(self, old_pid):
        """`fleet send` while busy: the UserPromptSubmit at queue time names the running turn."""
        self.send()
        self.event("UserPromptSubmit", prompt="queued", prompt_id=old_pid, transcript_path=self.tpath)

    def queued_prompt(self, recs):
        return self.first_index(recs, lambda r: r.get("promptSource") == "queued")

    def test_behind_a_turn_without_tool_calls(self):
        recs = self.records("queued_no_tools")
        old = self.submit(recs)
        self.write(recs[:2])  # the first prompt and the enqueue
        self.queue(old)
        self.write(recs[2:3])
        self.event("Stop", last_assistant_message="poem", prompt_id=old)
        self.assertEqual(self.wait_view(), "busy")  # open enqueue
        start = self.queued_prompt(recs)
        self.write(recs[3:start])  # dequeue and the old turn's end records
        self.assertEqual(self.wait_view(), "busy")  # dequeued, not started yet
        self.write(recs[start:start + 2])
        self.assertEqual(self.wait_view(), "busy")
        r = self.rec()
        self.assertEqual((r["state"], r["submit_prompt_id"]), ("busy", recs[start]["promptId"]))
        self.write(recs[start + 2:])
        self.event("Stop", last_assistant_message="banana", prompt_id=recs[start]["promptId"])
        self.assertEqual(self.wait_view(), "finished")
        self.assertEqual(self.rec()["last_message"], "banana")

    def test_absorbed_into_a_turn_with_tool_calls(self):
        recs = self.records("queued_tools")
        old = self.submit(recs)
        enqueue = self.first_index(recs, lambda r: r.get("operation") == "enqueue")
        self.write(recs[:enqueue + 1])
        self.queue(old)
        self.assertEqual(self.wait_view(), "busy")
        self.write(recs[enqueue + 1:])
        seq = self.rec()["seq"]
        self.event("Stop", last_assistant_message="both", prompt_id=old)
        self.assertEqual(self.wait_view(), "finished")
        r = self.rec()
        self.assertEqual((r["submit_prompt_id"], r["seq"]), (old, seq + 1))  # no queued start written

    def test_send_now_drops_the_running_turn(self):
        recs = self.records("send_now")
        old = self.submit(recs)
        start = self.queued_prompt(recs)
        self.write(recs[:2])  # the enqueue
        self.queue(old)  # no Stop ever fires for the dropped turn
        self.write(recs[2:start])  # dequeue and the aborted partial reply
        self.assertEqual(self.wait_view(), "busy")
        self.write(recs[start:start + 1])
        self.assertEqual(self.wait_view(), "busy")
        self.assertEqual(self.rec()["submit_prompt_id"], recs[start]["promptId"])
        self.write(recs[start + 1:])
        self.event("Stop", last_assistant_message="plum", prompt_id=recs[start]["promptId"])
        self.assertEqual(self.wait_view(), "finished")

    def test_an_interrupt_starts_the_queued_prompt(self):
        recs = self.records("interrupt_with_queued")
        old = self.submit(recs)
        self.write(recs[:2])
        self.queue(old)
        start = self.queued_prompt(recs)
        self.write(recs[2:start])  # dequeue, partial reply, the old turn's interrupt marker
        self.assertEqual(self.wait_view(), "busy")  # the old turn is over; the queued one hasn't started
        self.write(recs[start:start + 2])
        self.assertEqual(self.wait_view(), "busy")
        self.assertEqual((self.rec()["state"], self.rec()["submit_prompt_id"]), ("busy", recs[start]["promptId"]))
        self.write(recs[start + 2:])
        self.event("Stop", last_assistant_message="fig", prompt_id=recs[start]["promptId"])
        self.assertEqual(self.wait_view(), "finished")

    def test_escape_during_a_queued_turn(self):
        recs = self.records("queued_no_tools")
        old = self.submit(recs)
        self.write(recs[:2])
        self.queue(old)
        start = self.queued_prompt(recs)
        self.write(recs[2:start])
        self.event("Stop", last_assistant_message="poem", prompt_id=old)
        queued = recs[start]["promptId"]
        marker = dict(self.records("interrupt_text")[-1], promptId=queued)
        self.write(recs[start:start + 2])  # the queued prompt and its (partial) reply
        self.assertEqual(self.wait_view(), "busy")
        self.write([marker])
        self.assertEqual(self.wait_view(), "interrupted")
        r = self.rec()
        self.assertEqual((r["state"], r["outcome"], r["ended_seq"]), ("idle", "interrupted", r["seq"]))

    def test_a_queued_turn_that_ended_before_anyone_looked_is_not_started_again(self):
        # both Stops land before a poll reads the queued prompt: its Stop is already on the record
        recs = self.records("queued_no_tools")
        old = self.submit(recs)
        self.write(recs[:2])
        self.queue(old)
        self.write(recs[2:3])
        self.event("Stop", last_assistant_message="poem", prompt_id=old)
        self.write(recs[3:])
        self.event("Stop", last_assistant_message="banana")
        self.assertEqual(self.wait_view(), "finished")
        self.assertEqual((self.rec()["state"], self.rec()["last_message"]), ("idle", "banana"))

    def test_a_queued_start_is_dropped_if_an_event_landed_while_reading(self):
        recs = self.records("queued_no_tools")
        old = self.submit(recs)
        self.write(recs[:2])
        self.queue(old)
        self.write(recs[2:3])
        self.event("Stop", last_assistant_message="poem", prompt_id=old)
        start = self.queued_prompt(recs)
        self.write(recs[3:start + 2])  # the queued prompt and its reply, not its end yet
        read = lc.tr.catch_up

        def stop_while_reading(saved, path):
            facts = read(saved, path)
            self.event("Stop", last_assistant_message="banana")  # the queued turn's Stop
            return facts
        with mock.patch.object(lc.tr, "catch_up", stop_while_reading):
            self.life.reconcile(self.PANE, self.look, self.look)
        r = self.rec()
        self.assertEqual((r["state"], r["outcome"], r["last_message"]), ("idle", "finished", "banana"))

    def test_a_queued_start_is_written_once(self):
        recs = self.records("queued_no_tools")
        old = self.submit(recs)
        self.write(recs[:2])
        self.queue(old)
        self.write(recs[2:3])
        self.event("Stop", last_assistant_message="poem", prompt_id=old)
        start = self.queued_prompt(recs)
        self.write(recs[3:start + 1])
        self.assertEqual(self.wait_view(), "busy")
        seq = self.rec()["seq"]
        self.assertEqual(self.wait_view(), "busy")
        self.assertEqual(self.rec()["seq"], seq)  # nothing new to write
        self.write(recs[start + 1:])
        self.event("Stop", last_assistant_message="banana")
        # the next prompt is submitted before its record is written: the transcript's latest prompt
        # is still the queued one, which must not be started again
        self.event("UserPromptSubmit", prompt="next", prompt_id="p-next")
        seq = self.rec()["seq"]
        self.assertEqual(self.wait_view(), "busy")
        r = self.rec()
        self.assertEqual((r["seq"], r["submit_prompt_id"]), (seq, "p-next"))

    def test_a_transcript_without_prompt_records_falls_back_to_the_screen(self):
        self.write([{"type": "something-else"}])
        self.send()
        self.event("UserPromptSubmit", prompt="go")
        self.event("Stop")
        self.screen = "busy"
        self.assertEqual(self.wait_view(), "busy")
        self.screen = "quiet"
        self.t += 2
        self.assertEqual(self.wait_view(), "finished")

    def test_a_stop_that_beat_the_queued_start_still_ends_the_turn(self):
        # The queued turn's Stop lands, then a poll reads its prompt before its end record is
        # written, so the start is written after the Stop. The end record repairs it.
        recs = self.records("queued_no_tools")
        old = self.submit(recs)
        self.write(recs[:2])
        self.queue(old)
        self.write(recs[2:3])
        self.event("Stop", last_assistant_message="poem", prompt_id=old)
        start = self.queued_prompt(recs)
        self.write(recs[3:start + 2])  # up to the queued prompt's reply
        self.event("Stop", last_assistant_message="banana")
        self.assertEqual(self.wait_view(), "busy")
        self.write(recs[start + 2:])  # its stop_hook_summary and turn_duration
        self.assertEqual(self.wait_view(), "finished")
        r = self.rec()
        self.assertEqual((r["state"], r["outcome"], r["last_message"]), ("idle", "finished", "banana"))

    def test_an_interrupted_queued_turn_is_not_started_again_by_the_next_prompt(self):
        recs = self.records("queued_no_tools")
        old = self.submit(recs)
        self.write(recs[:2])
        self.queue(old)
        start = self.queued_prompt(recs)
        self.write(recs[2:start + 2])
        self.event("Stop", last_assistant_message="poem", prompt_id=old)
        self.assertEqual(self.wait_view(), "busy")
        self.write([dict(self.records("interrupt_text")[-1], promptId=recs[start]["promptId"])])
        self.assertEqual(self.wait_view(), "interrupted")
        self.send()
        self.event("UserPromptSubmit", prompt="next", prompt_id="p-next")  # its record not written yet
        self.assertEqual(self.wait_view(), "busy")
        self.assertEqual((self.rec()["submit_prompt_id"], self.rec()["outcome"]), ("p-next", "interrupted"))


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

    def test_hooks_record_the_turns_prompt_id(self):
        self.event("UserPromptSubmit", prompt="go", prompt_id="p1")
        self.assertEqual((self.rec()["prompt_id"], self.rec()["submit_prompt_id"]), ("p1", "p1"))
        self.event("PermissionRequest", tool_name="Bash", prompt_id="p0")
        self.assertEqual((self.rec()["prompt_id"], self.rec()["submit_prompt_id"]), ("p0", "p1"))
        self.event("Stop", prompt_id="p1")
        self.assertEqual(self.rec()["prompt_id"], "p1")




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
