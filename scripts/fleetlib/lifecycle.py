"""Worker lifecycle: the worker record and every change to it.

The hook (Claude Code events) and the CLI (new, send, keys, reconcile) change a worker's state only
through a Lifecycle, and `wait`/`status` read it only through the functions here. `state.Store` is
the storage underneath. Vocabulary: GLOSSARY.md. Why state and outcome are separate, and why events
are ordered by sequence numbers instead of time: docs/adr/0001-worker-state-and-turn-ordering.md.

The record (one JSON file per worker):

    name, session, pane, dir, repo, worktree, branch, mode, created   set by `fleet new`
    state        starting | idle | busy | blocked | exited            what the worker is doing now
    outcome      none | finished | interrupted                        how its last turn ended
    detail       what it is blocked on ("question", "permission: Bash", "dialog on screen")
    state_since  epoch seconds of the last state change (status AGE)
    heard_at     epoch seconds of the last state-setting event or screen correction (staleness)
    seq          bumped by every write; the only clock events are ordered by
    sent_seq     seq of the last `fleet send`
    submit_seq   seq of the last prompt submitted (UserPromptSubmit)
    ended_seq    seq of the last turn end (Stop, or an interrupt seen on screen)
    event, session_id, transcript_path, cwd, prompt, last_message     from the hooks

Records written by older versions of fleet are not read correctly: restart workers after upgrading.
"""

from . import state as st

EVENT_STATE = {
    "SessionStart": "idle",
    "UserPromptSubmit": "busy",
    "PostToolUse": "busy",  # also clears "blocked" once a dialog is answered
    "PermissionRequest": "blocked",  # permission dialog or AskUserQuestion; fires instantly
    "Stop": "idle",
    "SessionEnd": "exited",
    "Notification": "",  # no state change; may notify
}
EVENTS = tuple(EVENT_STATE)  # every hook event fleet wires into a worker

STALE = 3  # seconds before hook state may be corrected from the screen
RECHECK_DELAY = 1.5  # a quiet screen is looked at again after this long before calling a turn over


class Lifecycle:
    """Every transition of a worker record. `now()` gives epoch seconds; `glyph(pane, value)` mirrors
    the worker to the tmux status bar (see glyph_value)."""

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
        return self.load(pane) is not None

    # ---- transitions ----

    def create(self, pane, **meta):
        """The initial record for a worker `fleet new` is starting in `pane`."""
        now = self.now()
        rec = dict(meta)
        rec.update({"pane": pane, "state": "starting", "outcome": "none", "detail": "",
                    "state_since": now, "heard_at": now, "created": now, "seq": 0})
        self.store.ensure()
        st.Store.write(self.path(pane), rec)
        self._mirror(pane, rec)
        return rec

    def hook_event(self, pane, event, payload):
        """A Claude Code hook event in a worker's pane. Returns the record, or None when the pane is
        not a worker (or the event is unknown)."""
        if event not in EVENT_STATE:
            return None
        if self.load(pane) is None:
            return None  # not a fleet worker, or already removed: never create a record here
        now = self.now()

        def change(rec):
            seq = _bump(rec)
            rec["event"] = event
            for k in ("session_id", "transcript_path", "cwd"):
                if payload.get(k) is not None:
                    rec[k] = payload[k]
            state = EVENT_STATE[event]
            if not state:
                return  # Notification: neither a state change nor news about one
            _set(rec, state, now)
            if event == "PermissionRequest":
                tool = payload.get("tool_name")
                rec["detail"] = "question" if tool == "AskUserQuestion" else "permission: %s" % (tool or "?")
            elif event == "UserPromptSubmit":
                rec["submit_seq"] = seq
                rec["prompt"] = (payload.get("prompt") or "")[:200]
            elif event == "Stop":
                rec["ended_seq"] = seq
                rec["outcome"] = "finished"
                rec["last_message"] = payload.get("last_assistant_message") or ""
            elif event == "SessionStart":
                rec["outcome"] = "none"  # a new session has had no turn yet

        return self._change(pane, change, mirror=bool(EVENT_STATE[event]))

    def prompt_sent(self, pane):
        """`fleet send` is about to submit a prompt. Called before the key press, so the prompt's own
        UserPromptSubmit (and a quick turn's Stop) always come after it."""
        now = self.now()

        def change(rec):
            rec["sent_seq"] = _bump(rec)
            _set(rec, "busy", now)

        return self._change(pane, change)

    def dialog_answered(self, pane):
        """Keys were sent to a worker. Answering a dialog: assume work resumes; reconcile corrects it
        from the screen if not."""
        rec = self.load(pane)
        if rec is None or rec.get("state") != "blocked":
            return rec
        now = self.now()

        def change(rec):
            _bump(rec)
            _set(rec, "busy", now)

        return self._change(pane, change)

    def reconcile(self, pane, look, recheck):
        """Correct a stale busy/blocked state from the screen (hooks miss interrupts and cancelled
        dialogs). look/recheck: see reconcile_decision. Returns the record as it stands afterwards.

        The screen is read outside the lock (with a pause before the second look), so the
        correction is dropped if any event was written meanwhile: that event is fresher news."""
        rec = self.load(pane)
        if rec is None:
            return None
        judged_seq = rec.get("seq")
        now = self.now()
        d = reconcile_decision(rec.get("state"), rec.get("heard_at"), now, look, recheck)
        if d is None:
            return rec
        state, detail = d

        def change(rec):
            if rec.get("seq") != judged_seq:
                return
            seq = _bump(rec)
            if state == "idle":  # the turn is over without a Stop: interrupted
                _set(rec, "idle", now)
                rec["outcome"] = "interrupted"
                rec["ended_seq"] = seq
            else:
                _set(rec, state, now, detail)

        return self._change(pane, change) or rec

    # ---- what wait sees ----

    def wait_view(self, pane, look, recheck):
        """What `fleet wait` acts on, after correcting the record from the screen: see wait_view().
        A finished turn is also checked against the screen: a prompt queued behind a turn that made
        no tool calls runs as a turn of its own, and no hook says it started, so while the screen
        shows work the turn `wait` is after is not over. None when the worker is no longer tracked."""
        rec = self.reconcile(pane, look, recheck)
        if rec is None:
            return None
        view = wait_view(rec)
        if view != "finished":
            return view
        seen = look()
        if seen == "quiet" and self.now() - (rec.get("heard_at") or 0) < RECHECK_DELAY:
            seen = recheck()  # right after a Stop, a queued prompt may not have started yet
        return "busy" if seen in ("busy", "dialog") else view

    # ---- internals ----

    def _change(self, pane, fn, mirror=True):
        rec = st.Store.change(self.path(pane), fn, lock_timeout=self.lock_timeout)
        if rec is not None and mirror:
            self._mirror(pane, rec)
        return rec

    def _mirror(self, pane, rec):
        """The status-bar glyph is a convenience: failing to set it never undoes a transition."""
        try:
            self.glyph(pane, glyph_value(rec))
        except Exception:
            pass


def _bump(rec):
    rec["seq"] = int(rec.get("seq") or 0) + 1
    return rec["seq"]


def _set(rec, state, now, detail=""):
    if rec.get("state") != state:
        rec["state_since"] = now
    rec["state"] = state
    rec["heard_at"] = now
    rec["detail"] = detail


# ---- the rules ----

def reconcile_decision(state, heard_at, now, look, recheck):
    """What a stale busy/blocked state should become, judged from the screen. Hooks miss interrupts
    and cancelled dialogs, so once the state is STALE seconds old the screen wins.

    look() -> 'dialog' | 'busy' | 'quiet' | None (screen unreadable); recheck() -> the same, after a
    pause: one quiet screen can be a redraw between frames, so it takes two to call a turn over.
    Returns (state, detail), or None to leave the record alone; ("idle", "interrupted") means the
    turn ended without a Stop.
    """
    if state not in ("busy", "blocked"):
        return None
    if now - (heard_at or 0) < STALE:
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


def wait_view(rec):
    """What `fleet wait` acts on, from the record alone: "finished" or "interrupted" once the turn
    after the last send is over, "idle" for a worker never sent to that has had no turn, else the
    state ("busy" also while the sent turn is not over yet).

    The turn after a send is over once a turn ended after a prompt submitted after the send
    (ended_seq > submit_seq > sent_seq): a Stop from the turn the prompt was queued behind, or from
    the turn `send --now` dropped, comes before the new prompt's UserPromptSubmit."""
    state = rec.get("state") or ""
    if state != "idle":
        return state
    outcome = rec.get("outcome") or "none"
    sent = rec.get("sent_seq")
    if sent is None:
        return "idle" if outcome == "none" else outcome
    if (rec.get("ended_seq") or 0) > (rec.get("submit_seq") or 0) > sent:
        return outcome
    return "busy"


def glyph_value(rec):
    """The @fleet_state value the status bar shows: the state, with an idle worker's outcome."""
    state = rec.get("state") or ""
    if state == "idle" and rec.get("outcome") in ("finished", "interrupted"):
        return rec["outcome"]
    return state
