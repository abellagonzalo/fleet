"""The state-file store and the hook."""

import json
import os
import shutil
import tempfile
import unittest

import fakes  # noqa: F401  (sets sys.path)

from fleetlib import hook
from fleetlib import state as st


class TempStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fleet-test-")
        self.store = st.Store(self.dir)
        self.store.ensure()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def put(self, pane, **rec):
        base = {"name": "w", "session": "fleet", "pane": pane, "state": "starting", "outcome": "none",
                "detail": "", "state_since": 1, "heard_at": 1, "seq": 0}
        base.update(rec)
        path = self.store.path(pane)
        st.Store.write(path, base)
        return path


class StoreTest(TempStore):
    def test_path_strips_percent(self):
        self.assertEqual(self.store.path("%12"), os.path.join(self.dir, "workers", "12.json"))
        with self.assertRaises(ValueError):
            self.store.path("")

    def test_update_merges(self):
        p = self.put("%3", state="busy")
        rec = st.Store.update(p, {"state": "done", "ts": 9})
        self.assertEqual(rec["state"], "done")
        self.assertEqual(st.Store.load(p)["name"], "w")

    def test_update_never_recreates_a_removed_worker(self):
        p = self.put("%3")
        st.Store.remove(p)
        self.assertIsNone(st.Store.update(p, {"state": "busy"}))
        self.assertFalse(os.path.exists(p))

    def test_format_is_jq_compatible(self):
        p = self.put("%3", last_message="héllo")
        with open(p, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(json.loads(text)["last_message"], "héllo")
        self.assertIn('  "name": "w"', text)

    def test_load_of_garbage_is_none(self):
        p = os.path.join(self.store.workers, "9.json")
        with open(p, "w") as f:
            f.write("{not json")
        self.assertIsNone(st.Store.load(p))

    def test_stale_lock_does_not_wedge(self):
        import fcntl
        p = self.put("%3")
        fd = os.open(p + ".lock", os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            # same process: flock is per open file description, so this second lock conflicts
            rec = st.Store.update(p, {"state": "busy"}, lock_timeout=0.1)
        finally:
            os.close(fd)
        self.assertEqual(rec["state"], "busy")


class HookTest(TempStore):
    def fire(self, event, payload, pane="%7", now=500, raw=None):
        sets, notes = [], []
        env = {"TMUX_PANE": pane, "FLEET_STATE_DIR": self.dir}
        text = raw if raw is not None else json.dumps(payload)
        hook.run(["fleet-hook", event], text, env, now=now,
                 tmux_set=lambda p, v: sets.append((p, v)),
                 notifier=lambda t, m: notes.append((t, m)))
        return sets, notes

    def rec(self, pane="%7"):
        return st.Store.load(self.store.path(pane))

    def test_transitions(self):
        self.put("%7")
        cases = [("SessionStart", {}, "idle"), ("UserPromptSubmit", {"prompt": "hi"}, "busy"),
                 ("PermissionRequest", {"tool_name": "Bash"}, "blocked"), ("PostToolUse", {}, "busy"),
                 ("Stop", {"last_assistant_message": "ok"}, "idle"), ("SessionEnd", {}, "exited")]
        for ev, payload, want in cases:
            sets, _ = self.fire(ev, payload)
            self.assertEqual(self.rec()["state"], want, ev)
            self.assertEqual(self.rec()["event"], ev)
            self.assertEqual(sets, [("%7", "finished" if ev == "Stop" else want)])

    def test_fields(self):
        self.put("%7")
        self.fire("UserPromptSubmit", {"prompt": "x" * 300, "session_id": "s1", "transcript_path": "/t.jsonl",
                                       "cwd": "/w", "other": 1})
        r = self.rec()
        self.assertEqual(r["prompt"], "x" * 200)
        self.assertEqual((r["session_id"], r["transcript_path"], r["cwd"]), ("s1", "/t.jsonl", "/w"))
        self.assertNotIn("other", r)
        self.assertEqual(r["heard_at"], 500)

    def test_permission_detail(self):
        self.put("%7")
        self.fire("PermissionRequest", {"tool_name": "AskUserQuestion"})
        self.assertEqual(self.rec()["detail"], "question")
        self.fire("PermissionRequest", {"tool_name": "Write"})
        self.assertEqual(self.rec()["detail"], "permission: Write")
        self.fire("PermissionRequest", {})
        self.assertEqual(self.rec()["detail"], "permission: ?")

    def test_stop_records_last_message_and_clears_detail(self):
        self.put("%7", detail="permission: Bash")
        self.fire("Stop", {"last_assistant_message": "all done"})
        self.assertEqual((self.rec()["last_message"], self.rec()["detail"]), ("all done", ""))

    def test_notification_keeps_state_and_notifies_for_dialogs(self):
        self.put("%7", state="blocked", detail="question", name="feat-x")
        sets, notes = self.fire("Notification", {"notification_type": "elicitation_dialog"})
        self.assertEqual(self.rec()["state"], "blocked")
        self.assertEqual(sets, [])
        self.assertEqual(notes, [("fleet: feat-x needs you", "question")])
        _, notes = self.fire("Notification", {"notification_type": "idle_prompt"})
        self.assertEqual(notes, [])

    def test_ignores_untracked_panes_and_unknown_events(self):
        sets, _ = self.fire("Stop", {})
        self.assertEqual(sets, [])
        self.assertEqual(os.listdir(self.store.workers), [])
        self.put("%7")
        sets, _ = self.fire("Bogus", {})
        self.assertEqual(sets, [])
        self.assertEqual(self.rec()["state"], "starting")

    def test_no_tmux_pane_does_nothing(self):
        self.put("%7")
        sets, _ = self.fire("Stop", {}, pane="")
        self.assertEqual(sets, [])
        self.assertEqual(self.rec()["state"], "starting")

    def test_bad_input_still_records_the_event(self):
        self.put("%7")
        self.fire("Stop", None, raw="not json")
        self.assertEqual(self.rec()["outcome"], "finished")

    def test_main_prints_nothing_and_exits_0(self):
        import subprocess
        import sys
        script = os.path.join(os.path.dirname(fakes.HERE), "scripts", "fleet-hook")
        self.put("%7")
        env = {"PATH": "/usr/bin:/bin", "TMUX_PANE": "%7", "FLEET_STATE_DIR": self.dir}
        for ev, stdin in (("Stop", '{"last_assistant_message": "hi"}'), ("Stop", "garbage"), ("", ""),
                          ("Nope", "{}")):
            p = subprocess.run([sys.executable, script, ev] if ev else [sys.executable, script],
                               input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True, env=env, timeout=10)
            self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "", ""), ev)
        self.assertEqual(self.rec()["outcome"], "finished")
        # a state dir that cannot be written to: still silent, still 0
        env["FLEET_STATE_DIR"] = "/nonexistent/x"
        p = subprocess.run([sys.executable, script, "Stop"], input="{}", stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, env=env, timeout=10)
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "", ""))


if __name__ == "__main__":
    unittest.main()
