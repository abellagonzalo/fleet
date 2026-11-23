"""Pins what fleet would rely on in Claude Code's (undocumented) JSONL transcript format.

The fixtures under tests/fixtures/transcript_*.jsonl are scrubbed excerpts of real Claude Code
2.1.330 transcripts (see
README, "Tests"). Findings: issue #15. If a test here fails against a fresh capture, Claude Code
changed the transcript format and the transcript-based state checks (#16, #17, #18) need a look.

`turn_view` is a test-only reading of a transcript, not fleet's: it says what the transcript alone
tells about the latest turn. Its "busy" / "finished" / "interrupted" is a turn's progress, not
fleet's worker state and outcome (GLOSSARY.md): mapping one onto the other is for #16-#18.
"""

import json
import unittest

from fakes import fixture

INTERRUPTED = "[Request interrupted by user]"
INTERRUPTED_TOOL = "[Request interrupted by user for tool use]"
REJECTED = "The user doesn't want to proceed with this tool use."


def load(name):
    return [json.loads(line) for line in fixture("transcript_%s.jsonl" % name).splitlines() if line.strip()]


def message_content(rec):
    return (rec.get("message") or {}).get("content")


def texts(rec):
    content = message_content(rec)
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
    return []


def is_prompt(rec):
    return rec.get("type") == "user" and "promptSource" in rec


def is_interrupt(rec):
    return rec.get("type") == "user" and any(t in (INTERRUPTED, INTERRUPTED_TOOL) for t in texts(rec))


def is_turn_end(rec):
    return rec.get("type") == "system" and rec.get("subtype") == "turn_duration"


def tool_denials(records):
    """The "doesn't want to proceed" tool results: a rejected or cancelled dialog, and also an
    interrupt during a tool call."""
    found = []
    for r in records:
        blocks = message_content(r)
        if r.get("type") == "user" and isinstance(blocks, list):
            found += [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"
                      and b.get("is_error") and str(b.get("content", "")).startswith(REJECTED)]
    return found


def turn_view(records):
    """(state, pending): state of the latest turn ("idle" before any prompt, "busy", "finished" or
    "interrupted") and how many queued prompts have not started or been absorbed yet.

    A dequeue means a queued prompt starts its own turn. It is written before the end of the turn
    it waited behind (in the same write), so an end that follows a dequeue closes the old turn."""
    state, pending, queued_starting = "idle", 0, False
    for r in records:
        if r.get("type") == "queue-operation":
            op = r.get("operation")
            pending = pending + 1 if op == "enqueue" else max(0, pending - 1)
            if op == "dequeue":
                state, queued_starting = "busy", True
        elif is_prompt(r):
            state, queued_starting = "busy", False
        elif queued_starting:
            continue
        elif is_interrupt(r):
            state = "interrupted"
        elif is_turn_end(r):
            state = "finished"
    return state, pending


def first_index(records, pred):
    return next(i for i, r in enumerate(records) if pred(r))


class TurnEnds(unittest.TestCase):
    def test_a_finished_turn_ends_with_turn_duration(self):
        recs = load("finished")
        self.assertEqual(turn_view(recs), ("finished", 0))
        self.assertTrue(is_prompt(recs[0]))
        self.assertEqual(recs[0]["promptSource"], "typed")

    def test_every_user_record_of_a_turn_carries_its_prompts_id(self):
        recs = load("permission_rejected")
        ids = {r["promptId"] for r in recs if r.get("type") == "user"}
        self.assertEqual(len(ids), 1)


class Interrupts(unittest.TestCase):
    def test_interrupt_during_text_writes_the_partial_reply_then_a_marker(self):
        recs = load("interrupt_text")
        self.assertEqual(turn_view(recs), ("interrupted", 0))
        self.assertEqual(recs[-2]["type"], "assistant")
        self.assertEqual(texts(recs[-1]), [INTERRUPTED])
        self.assertIn("interruptedMessageId", recs[-1])
        self.assertFalse(any(is_turn_end(r) for r in recs))  # and no Stop hook fires

    def test_interrupt_during_a_tool_call_looks_like_a_rejection(self):
        recs = load("interrupt_tool")
        self.assertEqual(turn_view(recs), ("interrupted", 0))
        self.assertEqual(len(tool_denials(recs)), 1)
        self.assertEqual(texts(recs[-1]), [INTERRUPTED_TOOL])
        self.assertIn("interruptedMessageId", recs[-1])

    def test_an_early_escape_rewinds_the_prompt_and_writes_nothing(self):
        # Escape before any reply streamed: the prompt goes back into the input box, no marker is
        # written, and the next prompt is chained to the record before the dropped one.
        recs = load("rewind")
        dropped = first_index(recs, is_prompt)
        self.assertEqual(turn_view(recs[:dropped + 1]), ("busy", 0))  # the transcript can't tell
        next_prompt = first_index(recs[dropped + 1:], is_prompt) + dropped + 1
        self.assertFalse(any(is_interrupt(r) or is_turn_end(r) for r in recs[dropped:next_prompt]))
        self.assertEqual(recs[next_prompt]["parentUuid"], recs[dropped]["parentUuid"])


class Dialogs(unittest.TestCase):
    def test_a_rejected_or_cancelled_dialog_ends_the_turn_like_an_interrupt(self):
        for name in ("permission_rejected", "permission_escape", "question_escape"):
            with self.subTest(name):
                recs = load(name)
                self.assertEqual(turn_view(recs), ("interrupted", 0))
                self.assertEqual(len(tool_denials(recs)), 1)
                self.assertEqual(texts(recs[-1]), [INTERRUPTED_TOOL])
                self.assertNotIn("interruptedMessageId", recs[-1])
                denial = recs[-2]
                self.assertEqual((denial["toolDenialKind"], denial["toolDenialBeforeRun"]), ("user-rejected", True))

    def test_the_cancelled_question_fixture_is_an_ask_user_question(self):
        recs = load("question_escape")
        tool = [c for r in recs if r.get("type") == "assistant" for c in r["message"]["content"]
                if c.get("type") == "tool_use"]
        self.assertEqual([t["name"] for t in tool], ["AskUserQuestion"])

    def test_a_rejection_with_feedback_does_not_end_the_turn(self):
        recs = load("permission_rejected_with_feedback")
        first = first_index(recs, lambda r: bool(tool_denials([r])))
        self.assertFalse(is_interrupt(recs[first + 1]))
        self.assertEqual(turn_view(recs[:first + 1]), ("busy", 0))
        self.assertEqual(len(tool_denials(recs)), 2)
        self.assertEqual(turn_view(recs), ("interrupted", 0))


class QueuedPrompts(unittest.TestCase):
    def test_behind_a_turn_without_tool_calls_the_prompt_runs_as_its_own_turn(self):
        recs = load("queued_no_tools")
        ops = [(r["operation"], r.get("content")) for r in recs if r.get("type") == "queue-operation"]
        self.assertEqual(ops, [("enqueue", "Reply with just the word banana."), ("dequeue", None)])
        dequeue = first_index(recs, lambda r: r.get("operation") == "dequeue")
        first_end = first_index(recs, is_turn_end)
        self.assertLess(dequeue, first_end)  # written before the end of the turn it waited behind
        # When the first turn's Stop hook fires, the file holds the open enqueue (the dequeue lands
        # ~0.3s later): fleet's wait must not count that Stop, and the transcript says so.
        self.assertEqual(turn_view(recs[:dequeue]), ("busy", 1))
        self.assertEqual(turn_view(recs[:first_end + 1]), ("busy", 0))
        queued = [r for r in recs if is_prompt(r)][-1]
        self.assertEqual((texts(queued), queued["promptSource"]), (["Reply with just the word banana."], "queued"))
        self.assertEqual(turn_view(recs), ("finished", 0))

    def test_behind_a_turn_with_tool_calls_the_prompt_is_absorbed_into_it(self):
        recs = load("queued_tools")
        ops = [(r["operation"], r.get("reason")) for r in recs if r.get("type") == "queue-operation"]
        self.assertEqual(ops, [("enqueue", None), ("remove", "absorbed_mid_turn")])
        absorbed = [r for r in recs if (r.get("attachment") or {}).get("type") == "queued_command"]
        self.assertEqual(absorbed[0]["attachment"]["prompt"], "Then also reply with the word cherry.")
        self.assertEqual(sum(map(is_prompt, recs)), 1)
        self.assertEqual(sum(map(is_turn_end, recs)), 1)
        self.assertEqual(turn_view(recs), ("finished", 0))

    def test_send_now_drops_the_running_turn_without_a_marker(self):
        recs = load("send_now")
        aborted = [r for r in recs if r.get("isAbortedMidStream")]
        self.assertEqual(len(aborted), 1)
        self.assertFalse(any(is_interrupt(r) for r in recs))
        self.assertEqual([r["promptSource"] for r in recs if is_prompt(r)], ["typed", "queued"])
        self.assertEqual(sum(map(is_turn_end, recs)), 1)
        self.assertEqual(turn_view(recs), ("finished", 0))

    def test_an_interrupt_starts_the_queued_prompt(self):
        recs = load("interrupt_with_queued")
        marker = first_index(recs, is_interrupt)
        self.assertLess(first_index(recs, lambda r: r.get("operation") == "dequeue"), marker)
        self.assertEqual(turn_view(recs[:marker + 1]), ("busy", 0))
        self.assertEqual(turn_view(recs), ("finished", 0))


class Clear(unittest.TestCase):
    def test_a_new_transcript_starts_with_the_clear_command(self):
        recs = load("clear")
        self.assertIn("<command-name>/clear</command-name>", texts(recs[1])[0])
        self.assertEqual(turn_view(recs), ("idle", 0))


if __name__ == "__main__":
    unittest.main()
