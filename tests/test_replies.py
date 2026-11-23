"""Replies: a worker's reply per turn, read from its transcript (GLOSSARY.md: Reply, Turn).

Run on the transcript fixtures (tests/fixtures/transcript_*.jsonl, pinned by
test_transcript_fixtures.py) and temp files, without the CLI."""

import json
import os
import shutil
import tempfile
import unittest

import fakes  # noqa: F401  (sets sys.path)
from fakes import fixture

from fleetlib import replies as rp

INTERRUPTED = None  # a slot of an interrupted turn: no reply


def records(name):
    return [json.loads(l) for l in fixture("transcript_%s.jsonl" % name).splitlines() if l.strip()]


def replies(name):
    """(promptId prefix, reply or None) for each ended turn in a fixture."""
    return [(t["prompt"][:8], t["text"] if t["end"] == "finished" else INTERRUPTED)
            for t in rp.split(records(name)) if t["end"]]


def prompt(pid, source="typed"):
    return {"type": "user", "promptSource": source, "promptId": pid, "message": {"content": "go"}}


def text(t):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": t}]}}


def tool_use():
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}}


def end():
    return {"type": "system", "subtype": "turn_duration"}


class Split(unittest.TestCase):
    def test_a_finished_turn_has_one_reply(self):
        self.assertEqual(replies("finished"), [("d7574598", "lime")])

    def test_narration_between_tool_calls_is_not_a_reply(self):
        recs = [prompt("p1"), text("Let me check."), tool_use(), text("Checked: all good."), end(),
                prompt("p2"), text("Looking."), tool_use(), end()]
        self.assertEqual([(t["prompt"], t["text"]) for t in rp.split(recs)],
                         [("p1", "Checked: all good."), ("p2", None)])


    def test_an_interrupt_leaves_the_turn_without_a_reply(self):
        for name, pid in (("interrupt_text", "e0cc0724"), ("interrupt_tool", "50172770")):
            with self.subTest(name):
                self.assertEqual(replies(name), [(pid, INTERRUPTED)])

    def test_a_rejected_or_cancelled_dialog_leaves_no_reply(self):
        for name, pid in (("permission_rejected", "ea34411d"), ("permission_escape", "71e7166b"),
                          ("question_escape", "9786f20f")):
            with self.subTest(name):
                self.assertEqual(replies(name), [(pid, INTERRUPTED)])

    def test_a_rejection_with_feedback_goes_on_to_one_reply_at_the_end(self):
        recs = records("permission_rejected_with_feedback")
        denial = next(i for i, r in enumerate(recs) if r.get("toolDenialKind"))
        recs = recs[:denial + 1] + [text("Fine, I won't write it."), tool_use(), text("Done another way."), end()]
        self.assertEqual([(t["end"], t["text"]) for t in rp.split(recs)], [("finished", "Done another way.")])

    def test_send_now_leaves_the_aborted_turn_without_a_reply(self):
        self.assertEqual(replies("send_now"), [("0ccaec96", INTERRUPTED), ("fb8780e3", "plum")])

    def test_an_interrupt_that_starts_a_queued_prompt(self):
        self.assertEqual(replies("interrupt_with_queued"), [("de03aaee", INTERRUPTED), ("3b218f6e", "fig")])

    def test_an_early_escape_leaves_the_rewound_prompt_without_a_reply(self):
        self.assertEqual(replies("rewind"), [("dc2460b8", INTERRUPTED), ("d7574598", "lime")])

    def test_a_marker_for_another_prompt_does_not_end_the_turn(self):
        marker = {"type": "user", "promptId": "old", "message": {"content": [
            {"type": "text", "text": "[Request interrupted by user]"}]}}
        self.assertEqual(rp.split([prompt("p1"), marker, text("ok"), end()])[-1]["text"], "ok")

    def test_queued_prompts(self):
        self.assertEqual([p for p, _ in replies("queued_tools")], ["35fa3482"])  # absorbed: one turn
        self.assertTrue(replies("queued_tools")[0][1].startswith("I read `notes.txt`"))
        no_tools = replies("queued_no_tools")
        self.assertEqual([p for p, _ in no_tools], ["a12a2d4c", "efd6ec66"])  # two turns
        self.assertTrue(no_tools[0][1].startswith("Rain arrives"))
        self.assertEqual(no_tools[1][1], "banana")

    def test_a_cleared_session_has_no_turn_yet(self):
        self.assertEqual(rp.split(records("clear")), [])

    def test_a_running_turn_has_no_end_yet(self):
        self.assertEqual(rp.split([prompt("p1"), text("Let me look.")]),
                         [{"prompt": "p1", "end": None, "text": "Let me look."}])


class TempTranscript(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fleet-test-")
        self.path = os.path.join(self.dir, "t.jsonl")
        self.t = 0.0
        self.on_sleep = None

    def tearDown(self):
        shutil.rmtree(self.dir)

    def write(self, *recs):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("".join(json.dumps(r) + "\n" for r in recs))

    def sleep(self, s):
        self.t += s
        if self.on_sleep:
            self.on_sleep()

    def monotonic(self):
        return self.t

    def worker(self, **fields):
        rec = {"state": "idle", "outcome": "finished", "transcript_path": self.path}
        rec.update(fields)
        return rec


class Recent(TempTranscript):
    """`fleet last n`: the replies of the last n turns, oldest first; None for an interrupted turn."""

    def recent(self, rec, n):
        return rp.recent(rec, n, self.sleep, self.monotonic)

    def test_the_last_n_turns_each_give_their_final_message(self):
        self.write(prompt("p1"), text("Let me look."), tool_use(), text("first"), end(),
                   prompt("p2"), text("Checking."), tool_use(), text("second"), end())
        rec = self.worker(prompt_id="p2", last_message="second")
        self.assertEqual(self.recent(rec, 3), ["first", "second"])
        self.assertEqual(self.recent(rec, 1), ["second"])
        self.assertEqual(self.t, 0)  # nothing to wait for

    def test_an_interrupted_turn_has_a_slot_without_a_reply(self):
        self.write(prompt("p1"), text("first"), end(), *records("interrupt_text"))
        rec = self.worker(outcome="interrupted", submit_prompt_id=records("interrupt_text")[0]["promptId"],
                          prompt_id="p1", last_message="first")
        self.assertEqual(self.recent(rec, 2), ["first", INTERRUPTED])

    def test_an_early_escape_leaves_no_record_but_still_takes_the_newest_slot(self):
        self.write(prompt("p1"), text("first"), end())
        rec = self.worker(outcome="interrupted", submit_prompt_id="p2", prompt_id="p2", last_message="first")
        self.assertEqual(self.recent(rec, 2), ["first", INTERRUPTED])

    def test_a_rewound_prompt_the_worker_knows_was_interrupted(self):
        self.write(prompt("p1"), text("first"), end(), prompt("p2"))
        rec = self.worker(outcome="interrupted", submit_prompt_id="p2", prompt_id="p2", last_message="first")
        self.assertEqual(self.recent(rec, 2), ["first", INTERRUPTED])

    def test_a_running_turn_is_not_shown_yet(self):
        self.write(prompt("p1"), text("first"), end(), prompt("p2"), text("Let me look."))
        rec = self.worker(state="busy", submit_prompt_id="p2", prompt_id="p2", last_message="first")
        self.assertEqual(self.recent(rec, 2), ["first"])
        self.assertEqual(self.t, 0)  # a busy worker's prompt_id is not a Stop's: nothing to wait for

    def test_waits_for_the_turn_the_stop_hook_closed(self):
        # Stop fires before the transcript has the turn's end (~0.3 s later).
        self.write(prompt("p1"), text("first"), end(), prompt("p2"), text("second"))
        self.on_sleep = lambda: self.t >= 0.5 and not self.write(end()) and setattr(self, "on_sleep", None)
        rec = self.worker(prompt_id="p2", last_message="second (hook copy)")
        self.assertEqual(self.recent(rec, 2), ["first", "second"])
        self.assertLess(self.t, 1)

    def test_the_hook_copy_stands_in_if_the_transcript_never_catches_up(self):
        self.write(prompt("p1"), text("first"), end(), prompt("p2"), text("Let me look."))
        rec = self.worker(prompt_id="p2", last_message="second")
        self.assertEqual(self.recent(rec, 2), ["first", "second"])
        self.assertGreaterEqual(self.t, rp.TRANSCRIPT_LAG)
        self.write()  # the turn's prompt record not written at all: still the hook copy
        self.assertEqual(self.recent(self.worker(prompt_id="p3", last_message="third"), 2), ["first", "third"])

    def test_without_a_readable_transcript_the_hook_copy_stands_in(self):
        for path in ("", os.path.join(self.dir, "missing.jsonl")):
            with self.subTest(path=path):
                self.assertEqual(self.recent(self.worker(transcript_path=path, last_message="hi"), 3), ["hi"])
                self.assertEqual(self.recent(self.worker(transcript_path=path, outcome="interrupted"), 3),
                                 [INTERRUPTED])
                self.assertEqual(self.recent(self.worker(transcript_path=path, outcome="none"), 3), [])
        self.assertEqual(self.t, 0)

    def test_records_fleet_does_not_recognise_are_no_transcript(self):
        self.write(text("an assistant record without any prompt record"))
        self.assertEqual(self.recent(self.worker(prompt_id="p1", last_message="hi"), 3), ["hi"])
        self.assertEqual(self.t, 0)

    def test_a_cleared_session_has_no_reply_yet(self):
        with open(self.path, "w") as f:
            f.write(fixture("transcript_clear.jsonl"))
        self.assertEqual(self.recent(self.worker(outcome="none", last_message="before /clear"), 3), [])


class Latest(TempTranscript):
    """`fleet status` and `fleet watch`: the latest turn's reply, read incrementally (the reader's
    saved position and facts live on the worker record), or blank when that turn has none."""

    def worker(self, **fields):
        rec = super().worker(**fields)
        rec["transcript"] = rp.catch_up(getattr(self, "saved", None), self.path)
        self.saved = rec["transcript"]
        return rec

    def test_a_finished_turn_shows_its_reply_read_across_several_reads(self):
        self.write(prompt("p1"), text("first"), end(), prompt("p2"), text("Let me look."))
        self.assertEqual(rp.latest(self.worker(state="busy", prompt_id="p2", last_message="first")), "")
        self.write(tool_use(), text("second"), end())
        self.assertEqual(rp.latest(self.worker(prompt_id="p2", last_message="hook copy")), "second")

    def test_blank_when_the_latest_turn_has_no_reply(self):
        self.write(prompt("p1"), text("first"), end(), *records("interrupt_tool"))
        pid = records("interrupt_tool")[0]["promptId"]
        self.assertEqual(rp.latest(self.worker(outcome="interrupted", submit_prompt_id=pid,
                                               prompt_id="p1", last_message="first")), "")
        self.assertEqual(rp.latest(self.worker(state="blocked", prompt_id=pid, last_message="first")), "")
        self.assertEqual(rp.latest(self.worker(state="starting", outcome="none")), "")

    def test_blank_after_clear(self):
        self.write(prompt("p1"), text("first"), end())
        self.worker(prompt_id="p1", last_message="first")
        self.path = os.path.join(self.dir, "cleared.jsonl")
        with open(self.path, "w") as f:
            f.write(fixture("transcript_clear.jsonl"))
        self.assertEqual(rp.latest(self.worker(outcome="none", prompt_id="p1", last_message="first")), "")

    def test_the_hook_copy_stands_in_while_the_transcript_lags_or_cannot_be_read(self):
        self.write(prompt("p1"), text("first"), end(), prompt("p2"), text("second"))
        self.assertEqual(rp.latest(self.worker(prompt_id="p2", last_message="hook copy")), "hook copy")
        self.path = os.path.join(self.dir, "missing.jsonl")
        self.assertEqual(rp.latest(self.worker(prompt_id="p2", last_message="hook copy")), "hook copy")

    def test_an_exited_worker_keeps_its_last_reply(self):
        self.write(prompt("p1"), text("first"), end())
        self.assertEqual(rp.latest(self.worker(state="exited", prompt_id="p1", last_message="x")), "first")


if __name__ == "__main__":
    unittest.main()
