"""A worker's replies: the one definition of what `fleet last`, `fleet status` and `fleet watch` show.

A reply is the worker's final message of a turn (GLOSSARY.md). The transcript is the source of truth:
its records are split into turns, matched by promptId, and each finished turn's reply is its last
assistant text.
"""

from . import transcript as tr

TRANSCRIPT_LAG = 2.0  # how long the transcript may trail the Stop hook (turn_duration: ~0.3 s)
POLL = 0.2
NO_REPLY_STATES = ("busy", "blocked", "starting")  # the latest turn has no reply (yet) in these


def latest(rec):
    """The reply of worker record `rec`'s latest turn: "" when that turn has none (busy, blocked,
    interrupted, or no turn yet in this session), else last_finished."""
    return "" if rec.get("state") in NO_REPLY_STATES else last_finished(rec)


def last_finished(rec):
    """The reply of the turn the worker's latest Stop closed ("" when its latest turn outcome isn't
    finished), even if a new turn has started since: what `fleet watch` reports for a turn end it
    sees after the next prompt was sent. Read from the transcript facts on the record (see
    catch_up); the hook's copy stands in when the transcript can't show it: missing, unreadable,
    not caught up yet, or in a format fleet doesn't recognise."""
    if rec.get("outcome") != "finished":
        return ""
    turn = (rec.get("transcript") or {}).get("turn")
    if turn and turn["prompt"] and turn["prompt"] == rec.get("prompt_id") and turn["end"] == "finished":
        return turn["text"] or ""
    return rec.get("last_message") or ""


def catch_up(saved, path):
    """transcript.catch_up, plus "turn": the latest turn, as split() gives it. Saved on the worker
    record, so only the latest turn is kept."""
    return tr.catch_up(saved, path, folds={"turn": advance})


def advance(turn, record):
    """The latest turn after one more transcript record (None before any prompt record)."""
    turns = [dict(turn)] if turn else []
    _step(turns, record)
    return turns[-1] if turns else None


def recent(rec, n, sleep, monotonic):
    """The replies of worker record `rec`'s last n turns, oldest first, read from the whole
    transcript; None in the slot of an interrupted turn. A turn in progress has no slot yet.

    The Stop hook fires before the transcript is complete: re-read for up to TRANSCRIPT_LAG until the
    turn the Stop closed (its prompt_id) has its end record. If it never does, the hook's copy
    stands in as that turn's reply. It also stands in for a finished latest turn when the transcript
    is missing, unreadable or holds no prompt record (an unknown format: fall back, don't guess)."""
    if n <= 0:
        return []
    closed = _closed_turn(rec)
    stopped = closed if rec.get("outcome") == "finished" else None
    deadline = monotonic() + TRANSCRIPT_LAG
    while True:
        turns = _read(rec.get("transcript_path"))
        if not turns or not stopped or _find(turns, stopped, "finished") or monotonic() >= deadline:
            break
        sleep(POLL)
    return [t["text"] if t["end"] == "finished" else None for t in _with_record(rec, turns, closed)][-n:]


def _closed_turn(rec):
    """The prompt id of the turn the worker's latest event closed, or None while one is in progress.
    A busy worker's prompt_id belongs to the turn in progress, not to a Stop."""
    if rec.get("state") in NO_REPLY_STATES:
        return None
    if rec.get("outcome") == "finished":
        return rec.get("prompt_id")
    if rec.get("outcome") == "interrupted":
        return rec.get("submit_prompt_id")
    return None


def _read(path):
    return split(tr.follow(None, path)[0]) if path else []


def _find(turns, prompt_id, end):
    return next((t for t in turns if t["prompt"] == prompt_id and t["end"] == end), None)


def _with_record(rec, turns, closed):
    """`turns` with what the worker record knows and the transcript doesn't (yet): the hook's copy of
    a reply the transcript lags behind, an interrupt that wrote no marker (an early Escape writes no
    records, or only its prompt). A turn in progress is dropped."""
    outcome = rec.get("outcome")
    known = closed and _find(turns, closed, outcome)
    if outcome in ("finished", "interrupted") and not known and (closed or not turns):
        turns = [t for t in turns if t["prompt"] != closed]  # its unfinished copy, if written
        turns.append({"prompt": closed, "end": outcome,
                      "text": (rec.get("last_message") or "") if outcome == "finished" else None})
    return [t for t in turns if t["end"]]


def split(records):
    """The turns in `records`, oldest first: {"prompt": promptId, "end": None (in progress) |
    "finished" | "interrupted", "text": the reply of a finished turn}."""
    turns = []
    for rec in records:
        _step(turns, rec)
    return turns


def _step(turns, rec):
    kind = rec.get("type")
    if kind == "user" and "promptSource" in rec:
        if turns and turns[-1]["end"] is None:  # cut short: `send --now`, or an early Escape
            _interrupted(turns[-1])
        turns.append({"prompt": rec.get("promptId"), "end": None, "text": None})
        return
    turn = turns[-1] if turns else None
    if turn is None or turn["end"] is not None:
        return
    if kind == "assistant":
        texts = tr.texts(rec)
        if texts:
            turn["text"] = "\n".join(texts)
        elif _uses_tools(rec):  # what came before was narration: the turn goes on
            turn["text"] = None
    elif kind == "system" and rec.get("subtype") == "turn_duration":
        turn["end"] = "finished"
    elif kind == "user" and rec.get("promptId") == turn["prompt"] and any(
            t in tr.INTERRUPT_MARKERS for t in tr.texts(rec)):
        _interrupted(turn)


def _interrupted(turn):
    turn["end"], turn["text"] = "interrupted", None


def _uses_tools(rec):
    content = (rec.get("message") or {}).get("content")
    return isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_use" for c in content)
