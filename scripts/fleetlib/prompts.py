"""A worker's prompts: what `fleet prompts` shows, so an orchestrator can see what a worker was told
(its end-of-task policy included) without having sent it in this session.

The transcript is the source: each turn's prompt as written there (GLOSSARY: *Turn*), plus any
queued prompt absorbed into it. The worker record's list of prompts fleet sent (`sent_prompts`,
see lifecycle.prompt_sent) covers what the transcript no longer shows: prompts sent before the
worker's current session began, e.g. before it ran `/clear`.

A prompt is a prompt record, as transcript.is_prompt defines it for every reader of the transcript:
the first user record of a new promptId that isn't Claude Code's own (isMeta), a tool result or an
interrupt marker, so a slash command (written without promptSource) counts.
"""

import datetime
import os
import re

from . import transcript as tr

KEEP_SENT = 10  # prompts kept on the worker record

READ_FILE = re.compile(r"^Read (.+) and do what it says\.$")
ALSO_READ = re.compile(r" \(also read (.+) and follow it\)$")
COMMAND_NAME = re.compile(r"<command-name>(.*?)</command-name>", re.S)
COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)


def recent(rec, n):
    """The prompts of worker record `rec`'s last n turns, oldest first, each
    {"text": the prompt as typed, "absorbed": [texts of queued prompts absorbed into its turn]}.
    A turn in progress counts. Prompts fleet sent before the transcript's first record come from
    the record's sent_prompts (see _sent_before)."""
    if n <= 0:
        return []
    path = rec.get("transcript_path")
    records = tr.follow(None, path)[0] if path else []
    found = from_records(records)
    return (_sent_before(rec.get("sent_prompts") or [], records) + found)[-n:]


def _sent_before(sent, records):
    """The sent prompts the transcript can't show: all of them with no transcript records, else those
    sent before its first record. A transcript without timestamps can't be placed against them,
    so it is taken to show everything (no prompt printed twice)."""
    if records:
        start = _start(records)
        sent = [] if start is None else [s for s in sent if (s.get("at") or 0) < start]
    return [{"text": s.get("text") or "", "absorbed": []} for s in sent]


def from_records(records):
    """The prompts in transcript `records`, oldest first (see recent)."""
    prompts, current = [], None
    for r in records:
        kind = r.get("type")
        if tr.is_prompt(r, current):
            current = r.get("promptId")
            prompts.append({"text": as_typed("\n".join(tr.texts(r))), "absorbed": []})
        elif kind == "attachment" and prompts:
            att = r.get("attachment") or {}
            if att.get("type") == "queued_command" and att.get("prompt"):
                prompts[-1]["absorbed"].append(att["prompt"])
    return prompts


def as_typed(text):
    """A prompt as it was typed: a slash command's `<command-name>` markup back to `/cmd args`."""
    name = COMMAND_NAME.search(text)
    if not name:
        return text
    args = COMMAND_ARGS.search(text)
    args = args.group(1).strip() if args else ""
    return name.group(1).strip() + (" " + args if args else "")


def referenced_file(text):
    """The prompt file a prompt fleet typed tells the worker to read, or None: `Read <file> and do
    what it says.`, or a slash command ending `(also read <file> and follow it)`."""
    line = text.strip()
    m = READ_FILE.match(line) or (ALSO_READ.search(line) if line.startswith("/") else None)
    return m.group(1) if m else None


def file_text(path):
    """The contents of a prompt file, or None if it can't be read."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _start(records):
    """Epoch seconds of the transcript's first timestamped record, or None (no transcript, or a
    format without timestamps: then nothing in it can be placed against sent_prompts)."""
    for r in records:
        t = _epoch(r.get("timestamp"))
        if t is not None:
            return t
    return None


def _epoch(stamp):
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
