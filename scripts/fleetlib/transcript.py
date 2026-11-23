"""Reading a worker's Claude Code transcript (JSONL) incrementally.

- `follow(saved, path)`: the complete records written since the last read, and the position to save
  for the next one. The general reader: every kind of record goes through it.
- `catch_up(saved, path)`: follow, plus what the records so far say about the latest turn and the
  prompt queue (the facts lifecycle.py decides interrupts and queued starts from). Saved on the
  worker record between reads.

A missing or unreadable file, or a line that isn't a JSON object, is no news, never an error. The
format is Claude Code's and undocumented: tests/test_transcript_fixtures.py pins what fleet relies on.
"""

import json
import os

INTERRUPT_MARKERS = ("[Request interrupted by user]", "[Request interrupted by user for tool use]")


def follow(saved, path):
    """(new records, position to save). `saved` is what the previous call returned, or None. A
    different path (`/clear`, a new session) or a file shorter than the saved offset is read from
    the start."""
    offset = _resume_offset(saved, path)
    position = {"path": path, "offset": offset}
    if not path:
        return [], position
    try:
        with open(path, "rb") as f:
            if os.fstat(f.fileno()).st_size < offset:  # replaced or truncated: a new file
                offset = 0
            f.seek(offset)
            data = f.read()
    except OSError:
        return [], position
    complete = data[:data.rfind(b"\n") + 1]  # a last line without \n may still be being written
    position["offset"] = offset + len(complete)
    return [r for r in map(_parse, complete.splitlines()) if r is not None], position


def catch_up(saved, path):
    """The worker's saved transcript facts after reading what is new in `path`: the position (see
    follow) plus what the records so far say about the latest turn:

        prompt       promptId of the latest prompt record (absent: none seen, format unknown)
        source       that record's promptSource: "typed", or "queued" for a queued prompt that runs
                     as a turn of its own (behind a turn without tool calls, or after `send --now`
                     or an Escape)
        replied      whether an assistant record came after that prompt
        interrupted  promptId of the latest interrupt marker: Escape during text or a tool call, or
                     a rejected or cancelled dialog. A rejection with feedback writes none.
        queued       how many queued prompts are still waiting: enqueues not yet matched by a
                     dequeue (it starts a turn of its own) or a remove (absorbed into the running
                     turn at a tool call)
        dequeued     whether a dequeued prompt has not started yet: its prompt record comes after
                     the end record (or interrupt marker) of the turn it waited behind
        ended        promptId of the latest prompt whose turn ended (system/turn_duration, written
                     after the Stop hook has run). An end record after a dequeue closes the turn
                     the dequeued prompt waited behind, not the dequeued prompt's.

    A file read from the start again starts from no facts: the old file says nothing about it."""
    records, position = follow(saved, path)
    same_file = position["offset"] >= _resume_offset(saved, path) > 0
    facts = dict(saved) if same_file else {}
    facts.update(position)
    for rec in records:
        _observe(facts, rec)
    return facts


def _resume_offset(saved, path):
    saved = saved or {}
    return (saved.get("offset") or 0) if saved.get("path") == path else 0


def _parse(raw):
    try:
        rec = json.loads(raw)
    except ValueError:  # includes undecodable bytes
        return None
    return rec if isinstance(rec, dict) else None


def _observe(facts, rec):
    kind = rec.get("type")
    if kind == "user" and "promptSource" in rec:
        facts["prompt"], facts["source"], facts["replied"] = rec.get("promptId"), rec.get("promptSource"), False
        facts["dequeued"] = False
    elif kind == "queue-operation":
        op = rec.get("operation")
        queued = facts.get("queued") or 0
        facts["queued"] = queued + 1 if op == "enqueue" else max(0, queued - 1)
        if op == "dequeue":
            facts["dequeued"] = True
    elif kind == "system" and rec.get("subtype") == "turn_duration" and not facts.get("dequeued"):
        facts["ended"] = facts.get("prompt")
    elif kind == "assistant":
        facts["replied"] = True
    elif kind == "user" and any(t in INTERRUPT_MARKERS for t in _texts(rec)):
        facts["interrupted"] = rec.get("promptId")


def _texts(rec):
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
    return []
