"""fleet-hook <Event>: the Claude Code hook fleet wires into each worker (via `claude --settings`).

Parses the event and hands it to the worker's Lifecycle, which records it and mirrors the state to
the tmux window option @fleet_state (the status-bar glyph). Raises a macOS notification when the
worker needs the user. Must never block or fail Claude: prints nothing, always exits 0.
"""

import json
import os
import time

from . import lifecycle as lc
from . import state as st
from .system import Tmux, notify

NOTIFY_TYPES = ("permission_prompt", "elicitation_dialog")  # fire ~6s after the dialog opens
LOCK_TIMEOUT = 0.5
TMUX_TIMEOUT = 5


def run(argv, stdin_text, environ, now=None, tmux_set=None, notifier=notify):
    event = argv[1] if len(argv) > 1 else ""
    if event not in lc.EVENTS:
        return
    pane = environ.get("TMUX_PANE", "")
    if not pane:
        return
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    clock = (lambda: int(now)) if now is not None else (lambda: int(time.time()))
    life = lc.Lifecycle(st.Store(st.state_dir(environ)), clock, glyph=tmux_set or _tmux_glyph,
                        lock_timeout=LOCK_TIMEOUT)
    rec = life.hook_event(pane, event, payload)
    if rec is None:
        return
    if event == "Notification" and payload.get("notification_type") in NOTIFY_TYPES:
        notifier("fleet: %s needs you" % (rec.get("name") or "worker"), rec.get("detail") or "needs you")


def _tmux_glyph(pane, value):
    Tmux(own_pane="").set_window_option(pane, "@fleet_state", value, check=False, timeout=TMUX_TIMEOUT)


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
