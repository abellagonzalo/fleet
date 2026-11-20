"""fleet-hook <Event>: the Claude Code hook fleet wires into each worker (via `claude --settings`).

Records the worker's state in its state file, mirrors it to the tmux window option @fleet_state (the
status-bar glyph), and raises a macOS notification when the worker needs the user.
Must never block or fail Claude: prints nothing, always exits 0.
"""

import json
import os
import subprocess
import time

from . import state as st
from .system import notify

EVENT_STATE = {
    "SessionStart": "idle",
    "UserPromptSubmit": "busy",
    "PostToolUse": "busy",  # also clears "blocked" once a dialog is answered
    "PermissionRequest": "blocked",  # permission dialog or AskUserQuestion; fires instantly
    "Stop": "done",
    "SessionEnd": "exited",
    "Notification": "",  # no state change; may notify
}
NOTIFY_TYPES = ("permission_prompt", "elicitation_dialog")  # fire ~6s after the dialog opens
LOCK_TIMEOUT = 0.5


def fields_for(event, payload, now):
    """The fields an event merges into the state file. Pure, for the tests."""
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


def run(argv, stdin_text, environ, now=None, tmux_set=None, notifier=notify):
    event = argv[1] if len(argv) > 1 else ""
    if event not in EVENT_STATE:
        return
    pane = environ.get("TMUX_PANE", "")
    if not pane:
        return
    store = st.Store(st.state_dir(environ))
    path = store.path(pane)
    if not os.path.isfile(path):
        return  # not a fleet worker, or already removed
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    fields = fields_for(event, payload, int(now if now is not None else time.time()))
    rec = st.Store.update(path, fields, lock_timeout=LOCK_TIMEOUT)
    if rec is None:
        return
    if fields.get("state"):
        (tmux_set or _tmux_set_state)(pane, fields["state"])
    if event == "Notification" and payload.get("notification_type") in NOTIFY_TYPES:
        notifier("fleet: %s needs you" % (rec.get("name") or "worker"), rec.get("detail") or "needs you")


def _tmux_set_state(pane, value):
    subprocess.run(["tmux", "set", "-w", "-t", pane, "@fleet_state", value],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=5)


def main(argv):
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
    except OSError:
        pass
    try:
        import sys
        data = sys.stdin.read()
    except BaseException:
        data = ""
    try:
        run(argv, data, os.environ)
    except BaseException:
        pass
    return 0
