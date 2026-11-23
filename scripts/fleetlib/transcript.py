"""Reading a worker's Claude Code transcript (JSONL) incrementally.

- `follow(saved, path)`: the complete records written since the last read, and the position to save
  for the next one. The general reader: every kind of record goes through it.
- `catch_up(saved, path)`: follow, plus what the records so far say about the latest turn (the
  facts lifecycle.py decides interrupts from). Saved on the worker record between reads.

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
        replied      whether an assistant record came after that prompt
        interrupted  promptId of the latest interrupt marker: Escape during text or a tool call, or
                     a rejected or cancelled dialog. A rejection with feedback writes none.

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
        facts["prompt"], facts["replied"] = rec.get("promptId"), False
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
