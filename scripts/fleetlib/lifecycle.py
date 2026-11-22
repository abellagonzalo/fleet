"""Worker lifecycle: the worker record and every change to it.

The hook (Claude Code events) and the CLI (new, send, keys, reconcile) change a worker's state only
through a Lifecycle, and `wait`/`status` read it only through the functions here. `state.Store` is
the storage underneath. See GLOSSARY.md for the vocabulary.

The record (one JSON file per worker):

    name, session, pane, dir, repo, worktree, branch, mode, created   set by `fleet new`
    state, detail, ts                                                 the worker state; ts: last change
    event, session_id, transcript_path, cwd, prompt, last_message     from the hooks
    sent_at                                                           time of the last `fleet send`
"""

from . import state as st

EVENT_STATE = {
    "SessionStart": "idle",
    "UserPromptSubmit": "busy",
    "PostToolUse": "busy",  # also clears "blocked" once a dialog is answered
    "PermissionRequest": "blocked",  # permission dialog or AskUserQuestion; fires instantly
    "Stop": "done",
    "SessionEnd": "exited",
    "Notification": "",  # no state change; may notify
}
EVENTS = tuple(EVENT_STATE)  # every hook event fleet wires into a worker

STALE = 3  # seconds before hook state may be corrected from the screen
RECHECK_DELAY = 1.5  # a quiet screen is looked at again after this long before calling a turn over
WAIT_FINAL = ("done", "idle", "exited")


class Lifecycle:
    """Every transition of a worker record. `now()` gives epoch seconds; `glyph(pane, value)` mirrors
    the state to the tmux status bar."""

    def __init__(self, store, now, glyph=None, lock_timeout=2.0):
        self.store = store
        self.now = now
        self.glyph = glyph or (lambda pane, value: None)
        self.lock_timeout = lock_timeout

    def path(self, pane):
        return self.store.path(pane)

    def load(self, pane):
        return st.Store.load(self.path(pane))

    def is_tracked(self, pane):
        return st.Store.load(self.path(pane)) is not None

    # ---- transitions ----

    def create(self, pane, **meta):
        """The initial record for a worker `fleet new` is starting in `pane`."""
        now = self.now()
        rec = dict(meta)
        rec.update({"pane": pane, "state": "starting", "detail": "", "ts": now, "created": now})
        self.store.ensure()
        st.Store.write(self.path(pane), rec)
        return rec

    def hook_event(self, pane, event, payload):
        """A Claude Code hook event in a worker's pane. Returns the record, or None when the pane is
        not a worker (or the event is unknown)."""
        if event not in EVENT_STATE:
            return None
        path = self.path(pane)
        if st.Store.load(path) is None:
            return None  # not a fleet worker, or already removed: never create a record here
        fields = _hook_fields(event, payload, self.now())
        rec = st.Store.update(path, fields, lock_timeout=self.lock_timeout)
        if rec is not None and fields.get("state"):
            self._mirror(pane, fields["state"])
        return rec

    def prompt_sent(self, pane):
        """`fleet send` submitted a prompt."""
        return self._set_state(pane, "busy", extra={"sent_at": self.now()})

    def dialog_answered(self, pane):
        """Keys were sent to a worker. Answering a dialog: assume work resumes; reconcile corrects it
        from the screen if not."""
        rec = self.load(pane)
        if rec is not None and rec.get("state") == "blocked":
            return self._set_state(pane, "busy")
        return rec

    def reconcile(self, pane, look, recheck):
        """Correct a stale busy/blocked state from the screen (hooks miss interrupts and cancelled
        dialogs). look/recheck: see reconcile_decision. Returns the record as it stands afterwards."""
        rec = self.load(pane)
        if rec is None:
            return None
        d = reconcile_decision(rec.get("state"), rec.get("ts"), self.now(), look, recheck)
        if d is None:
            return rec
        return self._set_state(pane, d[0], d[1]) or rec

    def _set_state(self, pane, state, detail="", extra=None):
        fields = {"state": state, "detail": detail, "ts": self.now()}
        if extra:
            fields.update(extra)
        rec = st.Store.update(self.path(pane), fields, lock_timeout=self.lock_timeout)
        if rec is not None:
            self._mirror(pane, state)
        return rec

    def _mirror(self, pane, value):
        """The status-bar glyph is a convenience: failing to set it never undoes a transition."""
        try:
            self.glyph(pane, value)
        except Exception:
            pass


def _hook_fields(event, payload, now):
    f = {"ts": now, "event": event}
    new_state = EVENT_STATE[event]
    if new_state:
        f["state"] = new_state
    for k in ("session_id", "transcript_path", "cwd"):
        if payload.get(k) is not None:
            f[k] = payload[k]
    if event == "PermissionRequest":
        tool = payload.get("tool_name")
        f["detail"] = "question" if tool == "AskUserQuestion" else "permission: %s" % (tool or "?")
    elif event == "UserPromptSubmit":
        f["detail"] = ""
        f["prompt"] = (payload.get("prompt") or "")[:200]
    elif event == "Stop":
        f["detail"] = ""
        f["last_message"] = payload.get("last_assistant_message") or ""
    elif event == "SessionStart":
        f["detail"] = ""
    return f


# ---- the rules ----

def reconcile_decision(state, ts, now, look, recheck):
    """What a stale busy/blocked state should become, judged from the screen. Hooks miss interrupts
    and cancelled dialogs, so once the state is STALE seconds old the screen wins.

    look() -> 'dialog' | 'busy' | 'quiet' | None (screen unreadable); recheck() -> the same, after a
    pause: one quiet screen can be a redraw between frames, so it takes two to call a turn over.
    Returns (state, detail), or None to leave the record alone.
    """
    if state not in ("busy", "blocked"):
        return None
    if now - (ts or 0) < STALE:
        return None
    seen = look()
    if seen is None:
        return None
    if seen == "dialog":
        return None if state == "blocked" else ("blocked", "dialog on screen")
    if seen == "busy":
        return None if state == "busy" else ("busy", "")
    seen = recheck()
    if seen is None:
        return None
    if seen == "dialog":
        return ("blocked", "dialog on screen")
    if seen == "busy":
        return ("busy", "")
    return ("idle", "interrupted")


def wait_state(rec):
    """The state `fleet wait` acts on. A "done" counts only if its Stop hook fired at or after the last
    send; an older one is the previous turn's (the hook state lags the send by a moment)."""
    s = rec.get("state", "")
    if s == "done":
        if rec.get("event") != "Stop" or (rec.get("sent_at") or 0) > (rec.get("ts") or 0):
            return "busy"
    return s
