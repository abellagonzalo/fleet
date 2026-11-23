"""The transcript reader: incremental reads of a worker's Claude Code transcript (JSONL)."""

import json
import os
import shutil
import tempfile
import unittest

import fakes  # noqa: F401  (sets sys.path)
from fakes import fixture

from fleetlib import transcript as tr


def line(**rec):
    return json.dumps(rec) + "\n"


class TempFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fleet-test-")
        self.path = os.path.join(self.dir, "t.jsonl")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def append(self, text):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(text)


class Reader(TempFile):
    def test_reads_only_what_is_new(self):
        self.append(line(type="user", n=1) + line(type="assistant", n=2))
        recs, saved = tr.follow(None, self.path)
        self.assertEqual([r["n"] for r in recs], [1, 2])
        self.append(line(type="user", n=3))
        recs, saved = tr.follow(saved, self.path)
        self.assertEqual([r["n"] for r in recs], [3])
        self.assertEqual(tr.follow(saved, self.path)[0], [])

    def test_a_partial_last_line_waits_until_it_is_complete(self):
        whole = line(type="user", n=1)
        self.append(line(type="user", n=0) + whole[:10])
        recs, saved = tr.follow(None, self.path)
        self.assertEqual([r["n"] for r in recs], [0])
        recs, saved = tr.follow(saved, self.path)
        self.assertEqual(recs, [])
        self.append(whole[10:])
        recs, saved = tr.follow(saved, self.path)
        self.assertEqual([r["n"] for r in recs], [1])

    def test_a_new_transcript_path_is_read_from_the_start(self):
        self.append(line(type="user", n=1) + line(type="user", n=2))
        _, saved = tr.follow(None, self.path)
        other = os.path.join(self.dir, "new.jsonl")
        with open(other, "w", encoding="utf-8") as f:
            f.write(line(type="user", n=9))
        recs, saved = tr.follow(saved, other)
        self.assertEqual([r["n"] for r in recs], [9])
        self.assertEqual(saved["path"], other)

    def test_a_missing_file_is_no_news(self):
        recs, saved = tr.follow({"path": self.path, "offset": 40}, self.path)
        self.assertEqual(recs, [])
        self.assertEqual(saved, {"path": self.path, "offset": 40})
        self.assertEqual(tr.follow(None, None)[0], [])

    def test_lines_that_are_not_json_records_are_skipped(self):
        self.append("not json\n" + "[1, 2]\n" + line(type="user", n=1))
        recs, saved = tr.follow(None, self.path)
        self.assertEqual([r["n"] for r in recs], [1])
        self.assertEqual(tr.follow(saved, self.path)[0], [])

    def test_a_file_that_shrank_is_read_from_the_start(self):
        self.append(line(type="user", n=1) + line(type="user", n=2))
        _, saved = tr.follow(None, self.path)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(line(type="user", n=3))
        self.assertEqual([r["n"] for r in tr.follow(saved, self.path)[0]], [3])


PROMPT = {"type": "user", "promptSource": "typed", "promptId": "p1"}
REPLY = {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}
MARKER = {"type": "user", "promptId": "p1",
          "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]}}


class TurnFacts(TempFile):
    """What catch_up remembers about the latest turn."""

    def write(self, *records):
        self.append("".join(line(**r) for r in records))

    def test_a_prompt_then_a_reply_then_an_interrupt(self):
        self.write(PROMPT)
        seen = tr.catch_up(None, self.path)
        self.assertEqual((seen["prompt"], seen["replied"], seen.get("interrupted")), ("p1", False, None))
        self.write(REPLY)
        seen = tr.catch_up(seen, self.path)
        self.assertTrue(seen["replied"])
        self.write(MARKER)
        self.assertEqual(tr.catch_up(seen, self.path)["interrupted"], "p1")

    def test_a_new_prompt_has_no_reply_yet(self):
        self.write(PROMPT, REPLY, dict(PROMPT, promptId="p2"))
        seen = tr.catch_up(None, self.path)
        self.assertEqual((seen["prompt"], seen["replied"]), ("p2", False))

    def test_a_new_or_shrunk_file_forgets_the_old_turn(self):
        self.write(PROMPT, REPLY, MARKER)
        seen = tr.catch_up(None, self.path)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(line(type="system"))
        self.assertNotIn("prompt", tr.catch_up(seen, self.path))
        other = os.path.join(self.dir, "new.jsonl")
        self.assertNotIn("interrupted", tr.catch_up(seen, other))


class QueuedFacts(TempFile):
    """What catch_up remembers about queued prompts. Fixtures: tests/fixtures/transcript_*.jsonl."""

    def records(self, name):
        return [text + "\n" for text in fixture("transcript_%s.jsonl" % name).splitlines() if text.strip()]

    def facts_after(self, lines):
        self.append("".join(lines))
        return tr.catch_up(None, self.path)

    def test_queued_behind_a_turn_without_tool_calls(self):
        lines = self.records("queued_no_tools")
        seen = self.facts_after(lines[:3])  # prompt, enqueue, reply
        self.assertEqual((seen["queued"], bool(seen.get("dequeued")), seen["source"]), (1, False, "typed"))
        first = seen["prompt"]
        self.append("".join(lines[3:6]))  # dequeue, then the first turn's end records
        seen = tr.catch_up(seen, self.path)
        self.assertEqual((seen["queued"], seen["dequeued"], seen.get("ended")), (0, True, None))
        self.append(lines[6])  # the queued prompt
        seen = tr.catch_up(seen, self.path)
        self.assertEqual((seen["source"], seen["dequeued"], seen["replied"]), ("queued", False, False))
        self.assertNotEqual(seen["prompt"], first)
        self.append("".join(lines[7:]))
        seen = tr.catch_up(seen, self.path)
        self.assertEqual(seen["ended"], seen["prompt"])

    def test_absorbed_into_a_turn_with_tool_calls(self):
        seen = self.facts_after(self.records("queued_tools"))
        self.assertEqual((seen["queued"], bool(seen.get("dequeued")), seen["source"]), (0, False, "typed"))
        self.assertEqual(seen["ended"], seen["prompt"])

    def test_send_now_and_an_interrupt_start_the_queued_prompt(self):
        for name in ("send_now", "interrupt_with_queued"):
            with self.subTest(name):
                lines = self.records(name)
                start = next(i for i, text in enumerate(lines) if json.loads(text).get("promptSource") == "queued")
                seen = self.facts_after(lines[:start])
                self.assertEqual((seen["queued"], seen["dequeued"], seen["source"]), (0, True, "typed"))
                self.append("".join(lines[start:]))
                seen = tr.catch_up(seen, self.path)
                self.assertEqual((seen["source"], seen["dequeued"], seen["ended"]), ("queued", False, seen["prompt"]))
                os.remove(self.path)


if __name__ == "__main__":
    unittest.main()
