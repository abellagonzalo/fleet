"""Commands against fake tmux/git/wt: argument parsing, target resolution, send, wait, broadcast,
status, keys, rm, new, up."""

import io
import json
import os
import shutil
import tempfile
import threading
import unittest

from fakes import Clock, FakeTmux, box_screen, fixture, make_ctx

from fleetlib import args as A
from fleetlib import cli
from fleetlib import lifecycle as lc
from fleetlib import state as st
from fleetlib.errors import FleetError


class Args(unittest.TestCase):
    def test_up(self):
        self.assertEqual(A.parse_up([]), {"name": "", "detach": False, "mode": "auto", "extra": []})
        self.assertEqual(A.parse_up(["x", "--detach", "--mode", "manual", "--", "--model", "opus"]),
                         {"name": "x", "detach": True, "mode": "manual", "extra": ["--model", "opus"]})
        for bad in (["a", "b"], ["--nope"], ["--mode"]):
            with self.assertRaises(A.UsageError):
                A.parse_up(bad)

    def test_new(self):
        o = A.parse_new(["repo", "feat", "--base", "origin/dev", "--mode", "auto", "--prompt-file", "p.md",
                         "--no-worktree", "--", "--model", "haiku", "--base"])
        self.assertEqual(o, {"src": "repo", "names": ["feat"], "worktree": False, "base": "origin/dev",
                             "mode": "auto", "prompt_file": "p.md", "prompt_stdin": False,
                             "extra": ["--model", "haiku", "--base"]})
        self.assertEqual(A.parse_new(["--mode", "plan", "r", "b"])["mode"], "plan")
        self.assertTrue(A.parse_new(["r", "b", "--prompt-stdin"])["prompt_stdin"])
        for bad in (["repo"], [], ["r", "b", "--base"], ["r", "b", "--zap"], ["r", "b", "--mode", ""],
                    ["r", "b", "--prompt-stdin", "--prompt-file", "p.md"]):
            with self.assertRaises(A.UsageError, msg=bad):
                A.parse_new(bad)

    def test_new_several_branches(self):
        o = A.parse_new(["repo", "a", "--base", "origin/dev", "b", "c", "--mode", "plan", "--", "--model", "haiku"])
        self.assertEqual((o["src"], o["names"], o["base"], o["mode"], o["extra"]),
                         ("repo", ["a", "b", "c"], "origin/dev", "plan", ["--model", "haiku"]))
        for bad, msg in ((["r", "a", "b", "--prompt-stdin"], "fleet send"),
                         (["r", "a", "b", "--prompt-file", "p.md"], "fleet send"),
                         (["r", "a", "b", "--no-worktree"], "--no-worktree"),
                         (["r", "a", "b", "a"], "'a' is given twice")):
            with self.assertRaises(A.UsageError, msg=bad) as e:
                A.parse_new(bad)
            self.assertIn(msg, str(e.exception), bad)

    def test_status(self):
        self.assertEqual(A.parse_status([]), {"json": False})
        self.assertEqual(A.parse_status(["--json"]), {"json": True})
        with self.assertRaises(A.UsageError):
            A.parse_status(["--yaml"])

    def test_send(self):
        self.assertEqual(A.parse_send(["w", "hello", "world"]),
                         {"now": False, "force": False, "prompt_file": "", "prompt_stdin": False, "name": "w",
                          "text": "hello world"})
        self.assertEqual(A.parse_send(["--now", "--force", "w", "a\nb"]),
                         {"now": True, "force": True, "prompt_file": "", "prompt_stdin": False, "name": "w",
                          "text": "a b"})
        self.assertEqual(A.parse_send(["--prompt-stdin", "--now", "w"]),
                         {"now": True, "force": False, "prompt_file": "", "prompt_stdin": True, "name": "w",
                          "text": ""})
        self.assertEqual(A.parse_send(["--prompt-file", "p.md", "w"])["prompt_file"], "p.md")
        # after the worker name, everything is text, flags included
        self.assertEqual(A.parse_send(["w", "--now", "please"])["text"], "--now please")
        self.assertFalse(A.parse_send(["w", "--now", "please"])["now"])
        for bad in ([], ["w"], ["--now", "w"], ["--zap", "w", "x"], ["w", "   "], ["--prompt-stdin"],
                    ["--prompt-file"], ["--prompt-file", "", "w"], ["--prompt-file", "p.md"]):
            with self.assertRaises(A.UsageError, msg=bad):
                A.parse_send(bad)
        for bad in (["--prompt-stdin", "w", "text"], ["--prompt-file", "p.md", "w", "text"],
                    ["--prompt-file", "p.md", "--prompt-stdin", "w"]):
            with self.assertRaisesRegex(A.UsageError, "one way only", msg=bad):
                A.parse_send(bad)

    def test_broadcast(self):
        o = A.parse_broadcast(["--to", "a,b,", "--wait", "30", "--now", "do", "it"])
        self.assertEqual(o, {"now": True, "force": False, "prompt_file": "", "prompt_stdin": False,
                             "to": ["a", "b"], "wait": 30, "text": "do it"})
        self.assertEqual(A.parse_broadcast(["--prompt-file", "p.md"])["prompt_file"], "p.md")
        o = A.parse_broadcast(["--prompt-stdin", "--to", "a"])
        self.assertEqual((o["prompt_stdin"], o["to"], o["text"]), (True, ["a"], ""))
        self.assertEqual(A.parse_broadcast(["--", "--to", "x"])["text"], "--to x")
        self.assertIsNone(A.parse_broadcast(["x"])["wait"])
        for bad in ([], ["--to"], ["--to", ","], ["--wait"], ["--wait", "soon", "x"], ["--wait", "-1", "x"],
                    ["--zap", "x"], ["--now"], ["--prompt-stdin", "x"], ["--prompt-file", "p.md", "x"],
                    ["--prompt-file", "p.md", "--prompt-stdin"], ["--prompt-file"]):
            with self.assertRaises(A.UsageError, msg=bad):
                A.parse_broadcast(bad)

    def test_wait_read_last(self):
        self.assertEqual(A.parse_wait(["w"]), {"name": "w", "timeout": 600})
        self.assertEqual(A.parse_wait(["w", "5"]), {"name": "w", "timeout": 5})
        self.assertEqual(A.parse_read(["w"]), {"name": "w", "n": 60})
        self.assertEqual(A.parse_last(["w", "3"]), {"name": "w", "n": 3})
        for fn in (A.parse_wait, A.parse_read, A.parse_last):
            for bad in ([], [""], ["w", "x"], ["w", "1", "2"], ["w", "-3"]):
                with self.assertRaises(A.UsageError, msg=(fn.__name__, bad)):
                    fn(bad)

    def test_watch(self):
        self.assertEqual(A.parse_watch([]), {"to": [], "timeout": 1800})
        self.assertEqual(A.parse_watch(["--to", "a,b", "60"]), {"to": ["a", "b"], "timeout": 60})
        self.assertEqual(A.parse_watch(["60", "--to", "a"]), {"to": ["a"], "timeout": 60})
        for bad in (["--to"], ["--to", ","], ["x"], ["-3"], ["1", "2"], ["--nope"]):
            with self.assertRaises(A.UsageError, msg=bad):
                A.parse_watch(bad)

    def test_keys_rm(self):
        self.assertEqual(A.parse_keys(["w", "Down", "Enter"]), {"name": "w", "keys": ["Down", "Enter"]})
        with self.assertRaises(A.UsageError):
            A.parse_keys(["w"])
        self.assertEqual(A.parse_rm(["--force", "w"]), {"force": True, "branch": "merged", "name": "w"})
        self.assertEqual(A.parse_rm(["w", "--force"]), {"force": True, "branch": "merged", "name": "w"})
        self.assertEqual(A.parse_rm(["w", "--keep-branch"]), {"force": False, "branch": "keep", "name": "w"})
        for d in ("-D", "--force-delete"):
            self.assertEqual(A.parse_rm([d, "--force", "w"]), {"force": True, "branch": "delete", "name": "w"})
        for bad in ([], ["--force"], ["a", "b"], ["--zap", "a"], ["a", "-D", "--keep-branch"],
                    ["a", "--keep-branch", "--force-delete"]):
            with self.assertRaises(A.UsageError, msg=bad):
                A.parse_rm(bad)

    def test_help_and_unknown_command(self):
        for argv in ([], ["help"], ["bogus"]):
            ctx = make_ctx()
            self.assertEqual(cli.run(ctx, argv), 0)
            self.assertTrue(ctx.out.getvalue().startswith("fleet up [name]"))

    def test_usage_error_exit_code_and_message(self):
        ctx = make_ctx()
        self.assertEqual(cli.run(ctx, ["send", "w"]), 1)
        self.assertEqual(ctx.err.getvalue(), "fleet: usage: fleet send [--now] [--force] [--prompt-file P | --prompt-stdin] <worker> [<text>]\n")


class Fleet(unittest.TestCase):
    """A fake tmux server: session 'fleet' with the orchestrator (%0, our own pane) and two workers,
    plus another session 'other' with a pane of its own."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fleet-test-")
        self.tmux = FakeTmux(own_pane="%0")
        self.orch = self.tmux.add("fleet", "orch", "%0", role="orch")
        self.a = self.tmux.add("fleet", "alpha", "%1", fleet_name="alpha")
        self.b = self.tmux.add("fleet", "beta", "%2", fleet_name="beta")
        self.x = self.tmux.add("other", "shell", "%9", command="zsh")
        self.clock = Clock()
        self.ctx = make_ctx(self.tmux, self.dir, clock=self.clock)
        self.ctx.store.ensure()
        for p in (self.a, self.b):
            self.track(p, state="idle")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def track(self, pane, **fields):
        rec = {"name": pane.window, "session": pane.session, "pane": pane.id, "dir": "/w", "repo": "",
               "worktree": "", "branch": "", "mode": "manual", "state": "idle", "outcome": "none",
               "detail": "", "state_since": self.clock.now(), "heard_at": self.clock.now(),
               "created": self.clock.now(), "seq": 0}
        rec.update(fields)
        st.Store.write(self.ctx.store.path(pane.id), rec)

    def rec(self, pane):
        return st.Store.load(self.ctx.store.path(pane.id))

    def rest_file(self, sent, command, rest):
        """The path in `sent`, checked to be `command (also read <path> and follow it)` with `rest` in it."""
        prefix, suffix = command + " (also read ", " and follow it)"
        self.assertTrue(sent.startswith(prefix) and sent.endswith(suffix), sent)
        path = sent[len(prefix):-len(suffix)]
        self.assertEqual(os.path.dirname(path), os.path.join(self.dir, "prompts"))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), rest)
        return path

    def run_cmd(self, *argv, ctx=None):
        ctx = ctx or self.ctx
        ctx.out.seek(0)
        ctx.out.truncate()
        ctx.err.seek(0)
        ctx.err.truncate()
        code = cli.run(ctx, list(argv))
        return code, ctx.out.getvalue(), ctx.err.getvalue()


class Targets(Fleet):
    """End to end: commands resolve targets through the Roster (tests/test_roster.py has the rules),
    and fleet never types into an orchestrator."""

    def test_orchestrator_of_another_fleet(self):
        # The live slip that shaped this rule: a test fleet (FLEET_SESSION=fleet-test) driven from a
        # worker pane (%3) of the real fleet; "%0" is the real fleet's orchestrator.
        tmux = FakeTmux(own_pane="%3")
        tmux.add("fleet", "orch", "%0", role="orch")
        tmux.add("fleet", "me", "%3", fleet_name="me")
        tmux.add("fleet-test", "orch", "%4", role="orch")
        tmux.add("fleet-test", "w1", "%5", fleet_name="w1")
        ctx = make_ctx(tmux, self.dir, environ={"TMUX_PANE": "%3", "FLEET_SESSION": "fleet-test"})
        for argv in (["send", "%0", "hi"], ["keys", "%0", "Enter"], ["broadcast", "--to", "%0,orch,%3,%4", "hi"],
                     ["send", "me", "hi"], ["keys", "orch", "Enter"]):
            self.assertNotEqual(cli.run(ctx, argv), 0, argv)
        self.assertEqual(tmux.intents(), [])
        self.assertEqual(cli.run(ctx, ["send", "w1", "hi"]), 0)

    def test_split_window_sends_to_the_worker_pane(self):
        self.tmux.add("fleet", "alpha", "%5", command="zsh")  # the user split alpha's window...
        panes = self.tmux.panes_
        self.tmux.panes_ = {"%5": panes.pop("%5"), **panes}  # ...and tmux lists the new pane first
        code, _, err = self.run_cmd("send", "alpha", "hi")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.a.submitted, [("hi", "enter")])
        self.assertEqual(self.tmux.panes_["%5"].intents, [])

    def test_broadcast_discovers_the_session_once(self):
        asked = []
        for m in ("sessions", "session_of", "panes"):
            real = getattr(self.tmux, m)
            setattr(self.tmux, m, lambda *a, _m=m, _real=real: asked.append(_m) or _real(*a))
        del self.ctx.environ["TMUX_PANE"]  # from outside: discovery lists the sessions
        self.ctx.environ.pop("TMUX")
        _, out, _ = self.run_cmd("broadcast", "--wait", "5", "go")
        self.assertTrue(out.startswith("sent to alpha\nsent to beta\n"), out)
        self.assertEqual(asked.count("sessions"), 1)
        self.assertEqual(asked.count("panes"), 1)

    def test_no_command_types_into_the_orchestrator(self):
        for argv in (["send", "orch", "hi"], ["send", "%0", "hi"], ["keys", "orch", "Enter"],
                     ["keys", "%0", "Enter"], ["broadcast", "--to", "orch,%0", "hi"], ["rm", "%0"],
                     ["send", "nobody", "hi"], ["keys", "", "Enter"], ["read", "orch"]):
            code, _, err = self.run_cmd(*argv)
            self.assertNotEqual(code, 0, argv)
            self.assertTrue(err.startswith("fleet: "), argv)
        self.assertEqual(self.orch.intents, [])


class Send(Fleet):
    def test_types_verifies_and_submits(self):
        code, out, err = self.run_cmd("send", "alpha", "fix", "the", "bug")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "sent to alpha: fix the bug\n")
        self.assertEqual(self.a.submitted, [("fix the bug", "enter")])
        self.assertEqual(self.a.intents, [("type", "fix the bug", True), ("submit", False)])
        r = self.rec(self.a)
        self.assertEqual((r["state"], r["sent_seq"]), ("busy", r["seq"]))
        self.assertEqual(self.tmux.window_options[("%1", "@fleet_state")], "busy")

    def test_prompt_is_recorded_before_it_is_submitted(self):
        # the prompt's UserPromptSubmit can fire the moment Enter lands: it must order after the send
        seen = []
        self.a.on_submit = lambda: seen.append(self.rec(self.a).get("sent_seq"))
        self.run_cmd("send", "alpha", "go")
        self.assertEqual(seen, [1])

    def test_now_sends_ctrl_enter(self):
        code, out, _ = self.run_cmd("send", "--now", "alpha", "stop that")
        self.assertEqual(code, 0)
        self.assertEqual(out, "sent (now) to alpha: stop that\n")
        self.assertEqual(self.a.intents, [("type", "stop that", True), ("submit", True)])

    def test_refuses_typed_text(self):
        self.a.shown = fixture("typed_text.ansi")
        code, _, err = self.run_cmd("send", "alpha", "hi")
        self.assertEqual(code, 1)
        self.assertIn("prompt box already holds: 'hello typed text'", err)
        self.assertEqual(self.a.intents, [])

    def test_force_appends_to_typed_text(self):
        self.a.typed = "draft "
        code, _, _ = self.run_cmd("send", "--force", "alpha", "and more")
        self.assertEqual(code, 0)
        self.assertEqual(self.a.submitted, [("draft and more", "enter")])

    def test_suggested_prompt_is_not_typed_text(self):
        self.a.screens = [fixture("suggested_prompt.ansi")]
        code, out, err = self.run_cmd("send", "alpha", "next task")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.a.submitted, [("next task", "enter")])

    def test_refuses_dialogs(self):
        for fx in ("permission_dialog.ansi", "ask_user_question.ansi", "trust_dialog.ansi"):
            self.a.shown = fixture(fx)
            for flags in ([], ["--now"], ["--force"]):
                code, out, err = self.run_cmd("send", *(flags + ["alpha", "hi"]))
                self.assertEqual(code, 1, (fx, flags))
                self.assertIn("shows a dialog", err)
                self.assertIn("Esc to cancel", out)  # the dialog is shown
        self.assertEqual(self.a.intents, [])

    def test_refuses_without_a_prompt_box(self):
        self.a.shown = "user@host ~ $ \n"
        code, _, err = self.run_cmd("send", "alpha", "hi")
        self.assertEqual(code, 1)
        self.assertIn("no claude prompt box", err)
        self.assertEqual(self.a.intents, [])

    def test_does_not_submit_unseen_text(self):
        self.a.echo = False
        code, _, err = self.run_cmd("send", "alpha", "hello")
        self.assertEqual(code, 1)
        self.assertIn("typed text not visible", err)
        self.assertEqual(self.a.submitted, [])
        self.assertEqual(self.rec(self.a)["state"], "idle")

    def test_text_shown_late_is_accepted(self):
        # the box redraws slowly: first capture after typing still shows it empty
        self.a.screens = [box_screen(), box_screen(), box_screen()]
        code, _, _ = self.run_cmd("send", "alpha", "hello")
        self.assertEqual(code, 0)

    def test_untracked_worker_gets_no_state(self):
        c = self.tmux.add("fleet", "manual", "%5")
        code, _, _ = self.run_cmd("send", "manual", "hi")
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.ctx.store.path("%5")))
        self.assertEqual(c.submitted, [("hi", "enter")])


class SlashCommand(unittest.TestCase):
    """cli.slash_command: a prompt whose first line is a slash command, split into that line and the rest."""

    def test_slash_commands(self):
        for content, expected in (
            ("/review", ("/review", "")),
            ("/implement GitHub issue #62\n", ("/implement GitHub issue #62", "")),
            ("/plugin:skill x", ("/plugin:skill x", "")),
            ("/my_cmd-2", ("/my_cmd-2", "")),
            ("\n\n  /implement #7  \n\n  \n", ("/implement #7", "")),  # blank lines and padding don't count
            ("/implement #7\nPolicy:\n\n- open a PR\n", ("/implement #7", "Policy:\n\n- open a PR\n")),
            ("\n/implement #7\n  \nPolicy: merge\n \n", ("/implement #7", "Policy: merge\n")),
        ):
            self.assertEqual(cli.slash_command(content), expected, content)

    def test_not_slash_commands(self):
        for content in ("/tmp/spec.md has the details", "Fix the bug", "# Task\n\nRebase.\n",
                        "Run /implement #7", "/", "/ x", "/a.b", "\nFix it\n/implement #7\n"):
            self.assertIsNone(cli.slash_command(content), content)


class PromptStdin(Fleet):
    PROMPT = "# Task\n\nRebase on main.\nThen run the tests.\n"

    def saved_prompt(self, sent):
        prefix, suffix = "Read ", " and do what it says."
        self.assertTrue(sent.startswith(prefix) and sent.endswith(suffix), sent)
        path = sent[len(prefix):-len(suffix)]
        self.assertEqual(os.path.dirname(path), os.path.join(self.dir, "prompts"))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), self.PROMPT)  # newlines kept
        return path

    def test_send(self):
        self.ctx.stdin = io.StringIO(self.PROMPT)
        code, _, err = self.run_cmd("send", "--prompt-stdin", "alpha")
        self.assertEqual((code, err), (0, ""))
        [(sent, how)] = self.a.submitted
        self.assertEqual(how, "enter")
        self.saved_prompt(sent)

    def test_broadcast_shares_one_file(self):
        self.ctx.stdin = io.StringIO(self.PROMPT)
        code, out, err = self.run_cmd("broadcast", "--prompt-stdin")
        self.assertEqual((code, err, out), (0, "", "sent to alpha\nsent to beta\n"))
        self.assertEqual(self.a.submitted, self.b.submitted)
        self.saved_prompt(self.a.submitted[0][0])
        self.assertEqual(len(os.listdir(os.path.join(self.dir, "prompts"))), 1)

    def test_prompt_file(self):
        pf = os.path.join(self.dir, "task.md")
        open(pf, "w").close()
        rel = os.path.relpath(pf)
        code, _, err = self.run_cmd("send", "--prompt-file", rel, "alpha")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.a.submitted, [("Read %s and do what it says." % pf, "enter")])  # made absolute
        code, out, err = self.run_cmd("broadcast", "--prompt-file", pf)
        self.assertEqual((code, err, out), (0, "", "sent to alpha\nsent to beta\n"))
        self.assertEqual(self.b.submitted, [("Read %s and do what it says." % pf, "enter")])

    def test_missing_prompt_file(self):
        for argv in (["send", "--prompt-file", "/nope.md", "alpha"], ["broadcast", "--prompt-file", "/nope.md"]):
            code, _, err = self.run_cmd(*argv)
            self.assertEqual((code, err), (1, "fleet: no such prompt file: /nope.md\n"), argv)
        self.assertEqual(self.a.intents, [])

    def test_empty_stdin(self):
        for argv in (["send", "--prompt-stdin", "alpha"], ["broadcast", "--prompt-stdin"]):
            self.ctx.stdin = io.StringIO("  \n")
            code, _, err = self.run_cmd(*argv)
            self.assertEqual((code, err), (1, "fleet: --prompt-stdin: stdin is empty; nothing to send\n"), argv)
        self.assertEqual(self.a.intents, [])

    def test_slash_command_is_typed_as_is(self):
        self.ctx.stdin = io.StringIO("/implement GitHub issue #62\n\n")
        code, out, err = self.run_cmd("send", "--prompt-stdin", "alpha")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.a.submitted, [("/implement GitHub issue #62", "enter")])
        self.assertEqual(out, "sent to alpha (slash command): /implement GitHub issue #62\n")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "prompts")))  # nothing else to read

    def test_slash_command_with_more_lines(self):
        self.ctx.stdin = io.StringIO("/implement GitHub issue #62\nPolicy:\n- open a PR\n")
        code, out, err = self.run_cmd("send", "--prompt-stdin", "alpha")
        self.assertEqual((code, err), (0, ""))
        [(sent, _)] = self.a.submitted
        path = self.rest_file(sent, "/implement GitHub issue #62", "Policy:\n- open a PR\n")
        self.assertEqual(out, "sent to alpha (slash command): /implement GitHub issue #62 (also read %s and follow it)\n"
                         % path)

    def test_broadcast_types_one_slash_command_to_all(self):
        self.ctx.stdin = io.StringIO("/implement #7\nPolicy: merge\n")
        code, out, err = self.run_cmd("broadcast", "--prompt-stdin")
        self.assertEqual((code, err, out), (0, "", "sent to alpha (slash command)\nsent to beta (slash command)\n"))
        self.assertEqual(self.a.submitted, self.b.submitted)
        self.rest_file(self.a.submitted[0][0], "/implement #7", "Policy: merge\n")
        self.assertEqual(len(os.listdir(os.path.join(self.dir, "prompts"))), 1)

    def test_prompt_file_slash_command(self):
        pf = os.path.join(self.dir, "task.md")
        content = "/implement #7\n\nPolicy: merge\n"
        with open(pf, "w", encoding="utf-8") as f:
            f.write(content)
        code, out, err = self.run_cmd("send", "--prompt-file", pf, "alpha")
        self.assertEqual((code, err), (0, ""))
        path = self.rest_file(self.a.submitted[0][0], "/implement #7", "Policy: merge\n")
        self.assertNotEqual(path, pf)
        with open(pf, encoding="utf-8") as f:
            self.assertEqual(f.read(), content)  # the user's file is left alone
        with open(pf, "w", encoding="utf-8") as f:
            f.write("/review\n")
        code, out, err = self.run_cmd("broadcast", "--prompt-file", pf)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.b.submitted, [("/review", "enter")])

    def test_terminal_stdin(self):
        tty = io.StringIO("")
        tty.isatty = lambda: True
        self.ctx.stdin = tty
        code, _, err = self.run_cmd("send", "--prompt-stdin", "alpha")
        self.assertEqual(code, 1)
        self.assertIn("stdin is a terminal", err)


class Wait(Fleet):
    def hook(self, pane, event, **payload):
        self.ctx.lifecycle.hook_event(pane.id, event, payload)

    def test_finished_after_send(self):
        self.track(self.a, state="busy", sent_seq=1, seq=1)
        self.a.shown = fixture("busy.ansi")

        def tick(t):
            if self.clock.now() >= 1_000_005 and self.rec(self.a)["state"] == "busy":
                self.hook(self.a, "UserPromptSubmit", prompt="go")
                self.hook(self.a, "Stop", last_assistant_message="ok")
                self.a.shown = fixture("done.ansi")
        self.clock.on_sleep = tick
        code, out, _ = self.run_cmd("wait", "alpha", "60")
        self.assertEqual((code, out), (0, "finished\n"))

    def test_blocked(self):
        self.track(self.a, state="blocked", detail="permission: Bash")
        code, out, _ = self.run_cmd("wait", "alpha", "5")
        self.assertEqual((code, out), (4, "blocked: permission: Bash. See it with: fleet read alpha\n"))

    def test_interrupt_is_seen(self):
        self.track(self.a, state="busy", heard_at=self.clock.now() - 10, sent_seq=1, submit_seq=2, seq=2)
        self.a.shown = fixture("interrupted.ansi")
        code, out, _ = self.run_cmd("wait", "alpha", "5")
        self.assertEqual((code, out), (5, "interrupted\n"))
        self.assertEqual(self.rec(self.a)["outcome"], "interrupted")

    def test_exited(self):
        self.track(self.a, state="exited")
        self.assertEqual(self.run_cmd("wait", "alpha")[:2], (0, "exited\n"))

    def test_untracked(self):
        self.tmux.add("fleet", "manual", "%5")
        code, _, err = self.run_cmd("wait", "manual")
        self.assertEqual((code, err), (1, "fleet: 'manual' is not a tracked worker (no hook state)\n"))

    def test_timeout_is_wall_clock(self):
        self.track(self.a, state="busy")
        self.a.shown = fixture("busy.ansi")
        start = self.clock.t
        code, _, _ = self.run_cmd("wait", "alpha", "30")
        self.assertEqual(code, 2)
        self.assertLess(self.clock.t - start, 32)


class Watch(Fleet):
    """`fleet watch`: one line per turn end, change to blocked, exit or untracking, until every
    watched worker is settled or the timeout passes."""

    def hook(self, pane, event, **payload):
        self.ctx.lifecycle.hook_event(pane.id, event, payload)

    def finish(self, pane, reply):
        self.hook(pane, "UserPromptSubmit", prompt="go")
        self.hook(pane, "Stop", last_assistant_message=reply)

    def at(self, *steps):
        """Run each (seconds after start, fn) once the fake clock reaches it."""
        start = self.clock.t
        todo = list(steps)

        def tick(t):
            while todo and t - start >= todo[0][0]:
                todo.pop(0)[1]()
        self.clock.on_sleep = tick

    def test_all_settled_at_start(self):
        self.assertEqual(self.run_cmd("watch"), (0, "watch: all settled\n", ""))

    def test_reports_turn_ends_and_exits_once_all_settled(self):
        self.track(self.a, state="busy", sent_seq=1, seq=1)
        self.track(self.b, state="busy", sent_seq=1, seq=1)
        self.a.shown = self.b.shown = fixture("busy.ansi")

        def a_done():
            self.finish(self.a, "Done: rebased\n\nDetails follow.")
            self.a.shown = fixture("done.ansi")

        def b_done():
            self.hook(self.b, "UserPromptSubmit", prompt="go")
            self.b.shown = fixture("interrupted.ansi")  # Escape: no Stop
        self.at((3, a_done), (5, b_done))
        code, out, err = self.run_cmd("watch")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "alpha finished: Done: rebased\nbeta interrupted\nwatch: all settled\n")

    def test_blocked_then_answered_then_finished(self):
        self.track(self.a, state="busy", sent_seq=1, seq=1)
        self.a.shown = fixture("busy.ansi")
        steps = [
            (2, lambda: self.hook(self.a, "UserPromptSubmit", prompt="go")),
            (3, lambda: self.hook(self.a, "PermissionRequest", tool_name="Bash")),
            (6, lambda: self.hook(self.a, "PostToolUse")),
            (9, lambda: self.hook(self.a, "Stop", last_assistant_message="Done: ok")),
            (9, lambda: setattr(self.a, "shown", fixture("done.ansi"))),
        ]
        self.at(*steps)
        code, out, _ = self.run_cmd("watch", "--to", "alpha")
        self.assertEqual(code, 0)
        self.assertEqual(out, "alpha blocked: permission: Bash. See it with: fleet read alpha\n"
                              "alpha finished: Done: ok\nwatch: all settled\n")

    def test_new_work_after_a_finish_keeps_the_worker_watched(self):
        self.track(self.a, state="busy", sent_seq=1, seq=1)
        self.a.shown = fixture("busy.ansi")

        def resend():
            self.ctx.lifecycle.prompt_sent(self.a.id)
        self.at((2, lambda: self.finish(self.a, "Done: one")), (2, resend),
                (8, lambda: self.finish(self.a, "Done: two")),
                (8, lambda: setattr(self.a, "shown", fixture("done.ansi"))))
        code, out, _ = self.run_cmd("watch", "--to", "alpha")
        self.assertEqual(code, 0)
        self.assertEqual(out, "alpha finished: Done: one\nalpha finished: Done: two\nwatch: all settled\n")

    def test_a_queued_prompt_keeps_the_worker_unsettled(self):
        # sent while busy: the earlier turn's Stop comes before the queued prompt is submitted
        self.track(self.a, state="busy", submit_seq=1, sent_seq=2, seq=2)
        self.a.shown = fixture("busy.ansi")
        self.at((2, lambda: self.hook(self.a, "Stop", last_assistant_message="Done: earlier")),
                (6, lambda: self.finish(self.a, "Done: queued")),
                (6, lambda: setattr(self.a, "shown", fixture("done.ansi"))))
        code, out, _ = self.run_cmd("watch", "--to", "alpha")
        self.assertEqual(code, 0)
        self.assertEqual(out, "alpha finished: Done: earlier\nalpha finished: Done: queued\nwatch: all settled\n")

    def test_turn_starts_and_startup_print_nothing(self):
        self.track(self.a, state="starting")
        self.at((2, lambda: self.hook(self.a, "SessionStart")))
        self.assertEqual(self.run_cmd("watch", "--to", "alpha"), (0, "watch: all settled\n", ""))

    def test_an_interrupt_from_before_start_is_not_reported(self):
        self.track(self.a, state="busy", heard_at=self.clock.now() - 10, submit_seq=1, seq=1)
        self.a.shown = fixture("interrupted.ansi")  # interrupted before watch started; nobody reconciled yet
        self.assertEqual(self.run_cmd("watch", "--to", "alpha"), (0, "watch: all settled\n", ""))

    def test_exited_and_gone(self):
        self.track(self.a, state="busy", sent_seq=1, seq=1)
        self.track(self.b, state="busy", sent_seq=1, seq=1)
        self.a.shown = self.b.shown = fixture("busy.ansi")
        self.at((2, lambda: self.hook(self.a, "SessionEnd")),
                (4, lambda: self.ctx.lifecycle.forget(self.b.id)))
        code, out, err = self.run_cmd("watch")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "alpha exited\nbeta gone\nwatch: all settled\n")

    def test_timeout_lists_unsettled_workers(self):
        self.track(self.a, state="busy", sent_seq=1, seq=1)
        self.track(self.b, state="blocked", detail="question")
        self.a.shown = fixture("busy.ansi")
        self.b.shown = fixture("ask_user_question.ansi")
        start = self.clock.t
        code, out, _ = self.run_cmd("watch", "30")
        self.assertEqual((code, out), (2, "watch: timeout after 30s, still busy: alpha, beta\n"))
        self.assertLess(self.clock.t - start, 32)

    def test_default_timeout(self):
        self.track(self.a, state="busy", sent_seq=1, seq=1)
        self.a.shown = fixture("busy.ansi")
        code, out, _ = self.run_cmd("watch", "--to", "alpha")
        self.assertEqual((code, out), (2, "watch: timeout after 1800s, still busy: alpha\n"))

    def test_cannot_start(self):
        self.tmux.add("fleet", "manual", "%5")
        for argv in (["--to", "ghost"], ["--to", "alpha,manual"]):
            code, out, err = self.run_cmd("watch", *argv)
            self.assertEqual((code, out), (1, ""), argv)
            self.assertTrue(err.startswith("fleet: "), err)
        for p in ("%1", "%2"):
            st.Store.remove(self.ctx.store.path(p))
        self.assertEqual(self.run_cmd("watch"), (1, "", "fleet: no workers to watch\n"))

    def test_each_line_is_flushed(self):
        flushed = []

        class Out(io.StringIO):
            def flush(self):
                flushed.append(self.getvalue())
        self.ctx.out = Out()
        self.track(self.a, state="busy", sent_seq=1, seq=1)
        self.a.shown = fixture("busy.ansi")
        self.at((2, lambda: self.finish(self.a, "Done: x")),
                (2, lambda: setattr(self.a, "shown", fixture("done.ansi"))))
        self.run_cmd("watch", "--to", "alpha")
        self.assertIn("alpha finished: Done: x\n", flushed)


class Broadcast(Fleet):
    def test_to_all_tracked_workers(self):
        code, out, err = self.run_cmd("broadcast", "rebase on main")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "sent to alpha\nsent to beta\n")
        self.assertEqual(self.a.submitted, [("rebase on main", "enter")])
        self.assertEqual(self.b.submitted, [("rebase on main", "enter")])

    def test_a_refusal_skips_only_that_worker(self):
        self.a.shown = fixture("typed_text.ansi")
        code, out, err = self.run_cmd("broadcast", "--to", "alpha,ghost,beta", "go")
        self.assertEqual(code, 1)
        self.assertEqual(out, "NOT sent to alpha (reason above)\nNOT sent to ghost (reason above)\nsent to beta\n")
        self.assertIn("fleet: prompt box already holds", err)
        self.assertIn("fleet: no worker 'ghost'", err)
        self.assertEqual(self.b.submitted, [("go", "enter")])

    def test_a_dialog_refusal_shows_the_dialog(self):
        self.a.shown = fixture("permission_dialog.ansi")
        code, out, err = self.run_cmd("broadcast", "go")
        self.assertEqual(code, 1)
        self.assertIn("fleet: 'alpha' shows a dialog (above)", err)
        self.assertIn("Esc to cancel", out.split("NOT sent to alpha")[0])  # the "above" is there
        self.assertTrue(out.endswith("NOT sent to alpha (reason above)\nsent to beta\n"), out)
        self.assertEqual(self.b.submitted, [("go", "enter")])

    def test_a_missing_prompt_box_shows_the_screen(self):
        self.a.shown = "user@host ~ $ \n"
        code, out, err = self.run_cmd("broadcast", "go")
        self.assertEqual(code, 1)
        self.assertIn("no claude prompt box on 'alpha' (screen above)", err)
        self.assertEqual(out, "user@host ~ $ \nNOT sent to alpha (reason above)\nsent to beta\n")
        self.assertEqual(self.b.submitted, [("go", "enter")])

    def test_orchestrator_is_never_a_recipient(self):
        code, out, _ = self.run_cmd("broadcast", "--to", "orch", "go")
        self.assertEqual(code, 1)
        self.assertEqual(self.orch.intents, [])

    def test_no_workers(self):
        for p in ("%1", "%2"):
            st.Store.remove(self.ctx.store.path(p))
        code, _, err = self.run_cmd("broadcast", "go")
        self.assertEqual((code, err), (1, "fleet: no workers to send to\n"))

    def test_exit_code_priority(self):
        self.assertEqual(cli.broadcast_code(False, False, False), 0)
        self.assertEqual(cli.broadcast_code(False, False, True), 2)
        self.assertEqual(cli.broadcast_code(False, True, True), 4)  # bash got 2 if the timeout came first
        self.assertEqual(cli.broadcast_code(True, True, True), 1)
        self.assertEqual(cli.broadcast_code(False, False, True, True), 5)
        self.assertEqual(cli.broadcast_code(False, True, False, True), 4)

    def test_wait_prints_replies_and_codes(self):
        tp = os.path.join(self.dir, "t.jsonl")
        with open(tp, "w") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "rebased"}]}}) + "\n")
        self.a.shown = fixture("busy.ansi")
        self.b.shown = fixture("busy.ansi")

        life = self.ctx.lifecycle

        def tick(t):
            if self.a.submitted and self.rec(self.a)["state"] != "idle":
                life.hook_event("%1", "UserPromptSubmit", {"prompt": "rebase"})
                life.hook_event("%1", "Stop", {"last_assistant_message": "rebased", "transcript_path": tp})
            if self.b.submitted and self.rec(self.b)["state"] != "blocked":
                life.hook_event("%2", "PermissionRequest", {"tool_name": "AskUserQuestion"})
        self.a.screens = [box_screen()]
        self.b.screens = [box_screen()]
        self.a.shown = None
        self.b.shown = None
        self.clock.on_sleep = tick
        code, out, _ = self.run_cmd("broadcast", "--wait", "20", "rebase")
        self.assertEqual(code, 4)
        self.assertEqual(out, "sent to alpha\nsent to beta\n##### alpha: finished\nrebased\n----\n"
                              "##### beta: BLOCKED, see it with: fleet read beta\n")


    def test_wait_reports_interrupted_and_finished(self):
        life = self.ctx.lifecycle

        def tick(t):
            if self.a.submitted and self.rec(self.a).get("submit_seq") is None:
                life.hook_event("%1", "UserPromptSubmit", {"prompt": "rebase"})
                self.a.shown = fixture("interrupted.ansi")  # Escape; no Stop fires
            if self.b.submitted and self.rec(self.b)["state"] != "idle":
                life.hook_event("%2", "UserPromptSubmit", {"prompt": "rebase"})
                life.hook_event("%2", "Stop", {"last_assistant_message": "rebased"})
        self.clock.on_sleep = tick
        code, out, _ = self.run_cmd("broadcast", "--wait", "20", "rebase")
        self.assertEqual(code, 5)
        self.assertEqual(out, "sent to alpha\nsent to beta\n"
                              "##### alpha: INTERRUPTED, see it with: fleet read alpha\n"
                              "##### beta: finished\nrebased\n")

    def go_busy_once_sent(self, *panes):
        def tick(t):
            for p in panes:
                if p.submitted:
                    p.shown = fixture("busy.ansi")
        self.clock.on_sleep = tick

    def test_wait_shares_one_deadline(self):
        self.go_busy_once_sent(self.a, self.b)
        start = self.clock.t
        code, out, _ = self.run_cmd("broadcast", "--wait", "30", "rebase")
        self.assertEqual(code, 2)
        self.assertEqual(out, "sent to alpha\nsent to beta\n"
                              "##### alpha: still busy after 30s\n##### beta: still busy after 30s\n")
        self.assertLess(self.clock.t - start, 40)  # one deadline, not 30 s per worker

    def test_wait_reports_a_late_finish_and_a_timeout_within_one_deadline(self):
        # alpha finishes late, beta never does: beta's timeout runs alongside alpha's turn, not after
        busy = fixture("busy.ansi")
        life = self.ctx.lifecycle
        start = self.clock.t

        def tick(t):
            if self.b.submitted:
                self.b.shown = busy
            if self.a.submitted and self.a.shown is None:
                self.a.shown = busy
            if t - start >= 25 and self.rec(self.a)["state"] != "idle":
                life.hook_event("%1", "UserPromptSubmit", {"prompt": "rebase"})
                life.hook_event("%1", "Stop", {"last_assistant_message": "rebased"})
                self.a.shown = fixture("done.ansi")
        self.clock.on_sleep = tick
        code, out, _ = self.run_cmd("broadcast", "--wait", "30", "rebase")
        self.assertEqual(code, 2)
        self.assertEqual(out, "sent to alpha\nsent to beta\n"
                              "##### alpha: finished\nrebased\n##### beta: still busy after 30s\n")
        self.assertLess(self.clock.t - start, 40)


class Status(Fleet):
    def test_table(self):
        self.track(self.a, state="idle", outcome="finished", state_since=self.clock.now() - 125,
                   last_message="all\n  good")
        self.track(self.b, state="blocked", detail="permission: Bash and a very long detail",
                   state_since=self.clock.now() - 1)
        self.b.shown = fixture("permission_dialog.ansi")
        self.tmux.add("fleet", "byhand", "%6")  # a claude started by hand
        self.tmux.add("fleet", "sh", "%7", command="zsh")
        code, out, _ = self.run_cmd("status")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            "fleet session: fleet",
            "WORKER                   STATE    OUTCOME     AGE   DETAIL                 LAST MESSAGE",
            "alpha                    idle     finished    2m                           all good",
            "beta                     blocked              1s    permission: Bash and a ",
            "byhand                   untracked",
        ])

    def test_json(self):
        code, out, _ = self.run_cmd("status", "--json")
        d = json.loads(out)
        self.assertEqual([w["name"] for w in d["workers"]], ["alpha", "beta"])
        self.assertEqual([w["outcome"] for w in d["workers"]], ["none", "none"])
        self.assertEqual(d["untracked"], [])

    def test_reconciles_and_sweeps(self):
        self.track(self.a, state="busy", heard_at=self.clock.now() - 10)
        self.a.shown = fixture("question_cancelled.ansi")
        # beta's pane was reused by something that is not beta
        self.b.fleet_name = ""
        # a worker of another session is left alone
        self.track(self.x, name="shell", session="other", state="busy")
        self.x.fleet_name = "shell"
        code, out, _ = self.run_cmd("status", "--json")
        d = json.loads(out)
        self.assertEqual([(w["name"], w["state"], w["outcome"]) for w in d["workers"]],
                         [("alpha", "idle", "interrupted")])
        self.assertFalse(os.path.exists(self.ctx.store.path("%2")))
        self.assertTrue(os.path.exists(self.ctx.store.path("%9")))

    def test_no_sweep_when_tmux_cannot_be_asked(self):
        self.tmux.server_up = False
        cli.sweep(self.ctx)
        self.assertTrue(os.path.exists(self.ctx.store.path("%1")))

    def test_empty(self):
        for p in (self.a, self.b):  # both exited and were swept: their windows hold a shell
            st.Store.remove(self.ctx.store.path(p.id))
            p.command = "zsh"
        self.assertEqual(self.run_cmd("status")[1], "fleet session: fleet\n(no workers)\n")

    def test_age(self):
        self.assertEqual([cli.age(s) for s in (0, 59, 60, 3599, 3600, -5)], ["0s", "59s", "1m", "59m", "1h", "0s"])


class Glyph(Fleet):
    def test_status_bar_shows_finished_and_interrupted(self):
        win = self.tmux.window_options
        cli.style_window(self.ctx, "%1")
        fmt = win[("%1", "window-status-format")]
        self.assertIn("#{==:#{@fleet_state},finished},#[fg=green]✓", fmt)
        self.assertIn("#{==:#{@fleet_state},interrupted},#[fg=yellow]↯", fmt)
        self.assertIn("#{==:#{@fleet_state},blocked},#[fg=red]⚠", fmt)
        self.assertIn("#{==:#{@fleet_state},busy},⋯", fmt)


class Keys(Fleet):
    def test_keys_unblock(self):
        self.track(self.a, state="blocked")
        code, _, _ = self.run_cmd("keys", "alpha", "Down", "Enter")
        self.assertEqual(code, 0)
        self.assertEqual(self.a.intents, [("press", ("Down", "Enter"))])
        self.assertEqual(self.rec(self.a)["state"], "busy")

class LastAndRead(Fleet):
    def test_last_from_transcript(self):
        tp = os.path.join(self.dir, "t.jsonl")
        lines = [
            {"type": "user", "message": {"content": "hi"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "one"}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "two"},
                                                          {"type": "text", "text": "lines"}]}},
        ]
        with open(tp, "w") as f:
            f.write("\n".join(json.dumps(l) for l in lines) + "\nnot json\n")
        self.track(self.a, transcript_path=tp)
        self.assertEqual(self.run_cmd("last", "alpha")[1], "two\nlines\n----\n")
        self.assertEqual(self.run_cmd("last", "alpha", "5")[1], "one\n----\ntwo\nlines\n----\n")

    def test_last_waits_for_the_transcript_to_catch_up(self):
        # Seen live: Stop fired (last_message "new") ~0.2s before the transcript had the reply.
        tp = os.path.join(self.dir, "t.jsonl")
        entry = lambda t: json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": t}]}}) + "\n"
        with open(tp, "w") as f:
            f.write(entry("old"))
        self.track(self.a, transcript_path=tp, outcome="finished", last_message="new")

        def flush(t):
            with open(tp, "a") as f:
                f.write(entry("new"))
            self.clock.on_sleep = None
        self.clock.on_sleep = flush
        self.assertEqual(self.run_cmd("last", "alpha")[1], "new\n----\n")

    def test_last_falls_back_to_the_hook_copy(self):
        tp = os.path.join(self.dir, "t.jsonl")
        with open(tp, "w") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "old"}]}}) + "\n")
        self.track(self.a, transcript_path=tp, outcome="finished", last_message="new")
        self.assertEqual(self.run_cmd("last", "alpha", "2")[1], "old\n----\nnew\n----\n")

    def test_last_without_transcript(self):
        self.assertEqual(self.run_cmd("last", "alpha")[1], "(no reply yet)\n")
        self.track(self.a, last_message="from the hook")
        self.assertEqual(self.run_cmd("last", "alpha")[1], "from the hook\n")

    def test_read_drops_blank_lines(self):
        self.a.shown = "a\n\nb\n\n"
        self.assertEqual(self.run_cmd("read", "alpha")[1], "a\nb\n")


class Rm(Fleet):
    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.dir, "repo")
        self.wt = os.path.join(self.repo, "alpha")
        self.track(self.a, repo=self.repo, worktree=self.wt, branch="alpha")
        self.wts = self.ctx.worktrees

    def test_clean_worktree(self):
        code, out, _ = self.run_cmd("rm", "alpha")
        self.assertEqual(code, 0)
        self.assertEqual(out, "killed window 'alpha'\nremoved worktree %s\nbranch alpha deleted (merged)\n" % self.wt)
        self.assertNotIn("%1", self.tmux.panes_)
        self.assertEqual(self.wts.calls, [("check_removable", self.wt), ("remove", self.repo, self.wt, False, "merged")])
        self.assertFalse(os.path.exists(self.ctx.store.path("%1")))

    def test_refused_worktree_keeps_the_window(self):
        self.wts.errors["check_removable"] = FleetError("uncommitted changes in %s (above)" % self.wt)
        code, out, err = self.run_cmd("rm", "alpha")
        self.assertEqual((code, err), (1, "fleet: uncommitted changes in %s (above)\n" % self.wt))
        self.assertIn("%1", self.tmux.panes_)
        self.assertTrue(os.path.exists(self.ctx.store.path("%1")))
        self.assertEqual([c[0] for c in self.wts.calls], ["check_removable"])

    def test_force_skips_the_check(self):
        code, _, _ = self.run_cmd("rm", "--force", "alpha")
        self.assertEqual(code, 0)
        self.assertEqual(self.wts.calls, [("remove", self.repo, self.wt, True, "merged")])

    def test_branch_flags_pass_through(self):
        for flags, branch in ((["--keep-branch"], "keep"), (["-D"], "delete"), (["--force-delete", "--force"], "delete")):
            self.a = self.tmux.add("fleet", "alpha", "%1", fleet_name="alpha")
            self.track(self.a, repo=self.repo, worktree=self.wt, branch="alpha")
            self.wts.calls = []
            code, _, _ = self.run_cmd("rm", "alpha", *flags)
            self.assertEqual(code, 0)
            self.assertEqual(self.wts.calls[-1], ("remove", self.repo, self.wt, "--force" in flags, branch))

    def test_keep_branch_with_force_delete_is_a_usage_error(self):
        code, _, err = self.run_cmd("rm", "alpha", "--keep-branch", "-D")
        self.assertEqual((code, err), (1, "fleet: --keep-branch and -D (--force-delete) contradict each other\n"))
        self.assertIn("%1", self.tmux.panes_)
        self.assertEqual(self.wts.calls, [])

    def test_worktree_already_gone(self):
        self.wts.removed = None
        code, out, _ = self.run_cmd("rm", "alpha")
        self.assertEqual((code, out), (0, "killed window 'alpha'\n"))

    def test_without_worktree(self):
        self.track(self.a, repo="", worktree="", branch="")
        code, out, _ = self.run_cmd("rm", "alpha")
        self.assertEqual((code, out), (0, "killed window 'alpha'\n"))
        self.assertEqual(self.wts.calls, [])

    def test_orchestrator(self):
        self.assertEqual(self.run_cmd("rm", "orch")[2], "fleet: 'orch' is the orchestrator's pane, not a worker\n")
        self.assertIn("%0", self.tmux.panes_)


class New(Fleet):
    def setUp(self):
        super().setUp()
        self.repo = os.path.realpath(os.path.join(self.dir, "repo"))
        self.new_wt = os.path.join(self.repo, "feat")
        os.makedirs(self.new_wt)
        self.wts = self.ctx.worktrees
        self.wts.ensured = (self.new_wt, self.repo)

    def test_validation(self):
        for argv, msg in (
            (["new", self.repo, "a/b"], "contains '/'"),
            (["new", self.repo, "orch"], "orchestrator's window"),
            (["new", self.repo, "%3"], "cannot be a worker name"),
            (["new", self.repo, "x", "--prompt-file", "/nope.md"], "no such prompt file"),
            (["new", "/nope", "x"], "no such directory"),
            (["new", self.repo, "alpha"], "already runs in 'fleet'"),
        ):
            code, _, err = self.run_cmd(*argv)
            self.assertEqual(code, 1, argv)
            self.assertIn(msg, err, argv)
        self.assertEqual(self.wts.calls, [])

    def test_worktree_errors_pass_through(self):
        self.wts.errors["ensure"] = FleetError("%s does not follow <repo>/main" % self.dir)
        code, _, err = self.run_cmd("new", self.dir, "x")
        self.assertEqual((code, err), (1, "fleet: %s does not follow <repo>/main\n" % self.dir))
        self.assertEqual([p.window for p in self.tmux.panes_.values() if p.window == "x"], [])

    def test_in_a_worktree(self):
        pf = os.path.join(self.dir, "task.md")
        open(pf, "w").close()
        code, out, err = self.run_cmd("new", self.repo, "feat", "--base", "origin/dev", "--prompt-file", pf,
                                      "--", "--model", "haiku")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(self.wts.calls, [("ensure", self.repo, "feat", "origin/dev")])
        pane = [p for p in self.tmux.panes_.values() if p.window == "feat"][0]
        rec = st.Store.load(self.ctx.store.path(pane.id))
        self.assertEqual({k: rec[k] for k in ("name", "session", "pane", "dir", "repo", "worktree", "branch", "mode", "state")},
                         {"name": "feat", "session": "fleet", "pane": pane.id, "dir": self.new_wt, "repo": self.repo,
                          "worktree": self.new_wt, "branch": "feat", "mode": "auto", "state": "busy"})
        self.assertEqual(self.tmux.pane_options[(pane.id, "@fleet_name")], "feat")
        launched, submitted = pane.submitted[0][0], pane.submitted[1][0]
        self.assertEqual(pane.intents[:2], [("type", launched, False), ("submit", False)])  # the shell line in one go
        hooks = os.path.join(self.dir, "hooks.json")
        self.assertEqual(launched, "FLEET_STATE_DIR=%s claude /fleet-worker --name feat --permission-mode auto --settings %s --model haiku"
                         % (self.dir, hooks))
        self.assertEqual(submitted, "Read %s and do what it says." % pf)
        settings = json.load(open(hooks))
        self.assertEqual(sorted(settings["hooks"]), sorted(lc.EVENTS))
        self.assertEqual(settings["hooks"]["Stop"][0]["hooks"][0]["command"],
                         "FLEET_STATE_DIR=%s /opt/fleet/scripts/fleet-hook Stop" % self.dir)

    def test_on_the_main_checkout(self):
        main = os.path.join(self.repo, "main")
        os.makedirs(main)
        self.wts.ensured = (main, self.repo)
        code, out, err = self.run_cmd("new", main, "main")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(self.wts.calls, [("ensure", main, "main", "")])
        pane = [p for p in self.tmux.panes_.values() if p.window == "main"][0]
        rec = st.Store.load(self.ctx.store.path(pane.id))
        self.assertEqual((rec["dir"], rec["repo"], rec["worktree"], rec["branch"]), (main, self.repo, "", "main"))

    def test_no_worktree(self):
        code, out, err = self.run_cmd("new", "--no-worktree", self.dir, "plain")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(self.wts.calls, [])
        pane = [p for p in self.tmux.panes_.values() if p.window == "plain"][0]
        rec = st.Store.load(self.ctx.store.path(pane.id))
        self.assertEqual((rec["dir"], rec["repo"], rec["worktree"], rec["branch"]),
                         (os.path.realpath(self.dir), "", "", ""))

    def test_prompt_stdin(self):
        self.ctx.stdin = io.StringIO("line 1\nline 2\n")
        code, out, err = self.run_cmd("new", self.repo, "feat", "--prompt-stdin")
        self.assertEqual((code, err), (0, ""), out)
        pane = [p for p in self.tmux.panes_.values() if p.window == "feat"][0]
        sent = pane.submitted[1][0]
        path = sent[len("Read "):-len(" and do what it says.")]
        self.assertEqual(sent, "Read %s and do what it says." % path)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "line 1\nline 2\n")

    def test_prompt_stdin_slash_command(self):
        self.ctx.stdin = io.StringIO("/implement GitHub issue #62\n")
        code, out, err = self.run_cmd("new", self.repo, "feat", "--prompt-stdin")
        self.assertEqual((code, err), (0, ""), out)
        pane = [p for p in self.tmux.panes_.values() if p.window == "feat"][0]
        self.assertEqual(pane.submitted[1], ("/implement GitHub issue #62", "enter"))
        self.assertIn("sent to feat (slash command): /implement GitHub issue #62\n", out)

    def test_prompt_stdin_slash_command_with_more_lines(self):
        self.ctx.stdin = io.StringIO("/implement GitHub issue #62\nPolicy: merge\n")
        code, out, err = self.run_cmd("new", self.repo, "feat", "--prompt-stdin")
        self.assertEqual((code, err), (0, ""), out)
        pane = [p for p in self.tmux.panes_.values() if p.window == "feat"][0]
        self.rest_file(pane.submitted[1][0], "/implement GitHub issue #62", "Policy: merge\n")

    def test_unsent_prompt_hint_types_the_same_text(self):
        def new_window(session, name, cwd):
            p = self.tmux.add(session, name)
            p.shown = fixture("trust_dialog.ansi")
            return p.id
        self.tmux.new_window = new_window
        self.ctx.stdin = io.StringIO("/implement GitHub issue #62\n")
        code, out, err = self.run_cmd("new", "--no-worktree", self.dir, "plain", "--prompt-stdin")
        self.assertEqual(code, 3)
        self.assertIn("prompt not sent; once the worker is ready run: fleet send plain '/implement GitHub issue #62'\n",
                      err)

    def test_trust_dialog_exits_3(self):
        def new_window(session, name, cwd):
            p = self.tmux.add(session, name)
            p.shown = fixture("trust_dialog.ansi")
            return p.id
        self.tmux.new_window = new_window
        pf = os.path.join(self.dir, "task.md")
        open(pf, "w").close()
        code, out, err = self.run_cmd("new", "--no-worktree", self.dir, "plain", "--prompt-file", pf)
        self.assertEqual(code, 3)
        self.assertIn("trust-prompt: claude asks whether to trust %s." % os.path.realpath(self.dir), err)
        self.assertIn("fleet keys plain Down Enter", err)
        self.assertIn("prompt not sent", err)

    def test_prompt_box_timeout(self):
        def new_window(session, name, cwd):
            p = self.tmux.add(session, name)
            p.shown = "$ claude\n"
            return p.id
        self.tmux.new_window = new_window
        code, out, err = self.run_cmd("new", "--no-worktree", self.dir, "plain")
        self.assertEqual(code, 1)
        self.assertIn("did not show a prompt box within 60s", err)
        self.assertEqual(out, "$ claude\n")

    def test_prompt_box_timeout_is_wall_clock(self):
        # each screen capture takes a second: the 60 s budget must include that time
        def new_window(session, name, cwd):
            p = self.tmux.add(session, name)
            p.shown = "$ claude\n"
            p.on_screen = lambda: setattr(self.clock, "t", self.clock.t + 1)
            return p.id
        self.tmux.new_window = new_window
        start = self.clock.t
        code, _, _ = self.run_cmd("new", "--no-worktree", self.dir, "plain")
        self.assertEqual(code, 1)
        self.assertLessEqual(self.clock.t - start, 62)


class NewSeveral(Fleet):
    """`fleet new <repo> a b c`: validated up front, origin fetched once, workers started concurrently."""

    def setUp(self):
        super().setUp()
        self.repo = os.path.realpath(os.path.join(self.dir, "repo"))
        self.wts = self.ctx.worktrees
        self.wts.repo = self.repo
        self.wts.ensured = {}
        for b in ("a", "b", "c"):
            os.makedirs(os.path.join(self.repo, b))
            self.wts.ensured[b] = (os.path.join(self.repo, b), self.repo)

    def windows(self):
        return sorted(p.window for p in self.tmux.panes_.values() if p.window in ("a", "b", "c"))

    def trust_dialog_on(self, *names):
        add = self.tmux.add

        def new_window(session, name, cwd):
            p = add(session, name)
            if name in names:
                p.shown = fixture("trust_dialog.ansi")
            return p.id
        self.tmux.new_window = new_window

    def test_all_ready(self):
        code, out, err = self.run_cmd("new", self.repo, "a", "b", "c", "--mode", "plan", "--", "--model", "haiku")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(self.wts.calls[:4], [("check_branch", "a"), ("check_branch", "b"), ("check_branch", "c"),
                                              ("fetch", self.repo)])
        self.assertEqual(sorted(self.wts.calls[4:]), [("ensure", self.repo, b, "", "no-fetch") for b in "abc"])
        self.assertEqual(self.windows(), ["a", "b", "c"])
        for p in self.tmux.panes_.values():
            if p.window in ("a", "b", "c"):
                rec = self.rec(p)
                d = os.path.join(self.repo, p.window)
                self.assertEqual((rec["dir"], rec["worktree"], rec["branch"], rec["mode"]), (d, d, p.window, "plan"))
                self.assertIn("--permission-mode plan --settings %s --model haiku" % os.path.join(self.dir, "hooks.json"),
                              p.submitted[0][0])
        self.assertTrue(out.endswith("ready: worker 'a' in %s\nready: worker 'b' in %s\nready: worker 'c' in %s\n"
                                     "Attach: tmux attach -t fleet, then select a worker's window\n"
                                     % tuple(os.path.join(self.repo, b) for b in "abc")), out)

    def test_provisioning_runs_concurrently(self):
        barrier = threading.Barrier(3, timeout=5)  # sequential provisioning would never get all three here
        self.wts.on_ensure = lambda branch: barrier.wait()
        code, out, err = self.run_cmd("new", self.repo, "a", "b", "c")
        self.assertEqual((code, err), (0, ""), out)

    def test_one_failure_does_not_stop_the_others(self):
        self.wts.errors[("ensure", "b")] = FleetError("wt switch failed for b")
        code, out, err = self.run_cmd("new", self.repo, "a", "b", "c")
        self.assertEqual(code, 1)
        self.assertIn("failed: worker 'b': wt switch failed for b\n", out)
        self.assertIn("ready: worker 'a'", out)
        self.assertIn("ready: worker 'c'", out)
        self.assertEqual(self.windows(), ["a", "c"])
        self.assertEqual(sorted(st.Store.load(f)["name"] for f in self.ctx.store.files()), ["a", "alpha", "beta", "c"])

    def test_trust_dialog_on_some(self):
        self.trust_dialog_on("b", "c")
        code, out, err = self.run_cmd("new", self.repo, "a", "b", "c")
        self.assertEqual((code, err), (3, ""), out)
        self.assertIn("ready: worker 'a'", out)
        for b in "bc":
            self.assertIn("trust: worker '%s' is showing the folder-trust dialog; answer with fleet keys %s Down Enter\n"
                          % (b, b), out)

    def test_a_failure_outranks_the_trust_dialog(self):
        self.trust_dialog_on("c")
        self.wts.errors[("ensure", "a")] = FleetError("wt switch failed for a")
        code, out, _ = self.run_cmd("new", self.repo, "a", "b", "c")
        self.assertEqual(code, 1)
        self.assertIn("trust: worker 'c'", out)

    def test_refusals_start_nothing(self):
        for argv, msg in (
            (["new", self.repo, "a", "alpha"], "already runs in 'fleet'"),
            (["new", self.repo, "a", "orch"], "orchestrator's window"),
            (["new", self.repo, "a", "b", "a"], "given twice"),
            (["new", self.repo, "a", "b", "--prompt-stdin"], "fleet send"),
            (["new", self.repo, "a", "b", "--no-worktree"], "--no-worktree"),
            (["new", "/nope", "a", "b"], "no such directory"),
        ):
            code, _, err = self.run_cmd(*argv)
            self.assertEqual(code, 1, argv)
            self.assertIn(msg, err, argv)
        self.assertEqual(self.wts.calls, [])
        self.assertEqual(self.windows(), [])

    def test_bad_branch_or_layout_starts_nothing(self):
        for key, error in (("check_branch", FleetError("'a..b' is not a valid branch name")),
                           ("fetch", FleetError("%s does not follow <repo>/main" % self.repo))):
            self.wts.errors = {key: error}
            self.wts.calls = []
            code, _, err = self.run_cmd("new", self.repo, "a", "b")
            self.assertEqual((code, err), (1, "fleet: %s\n" % error))
            self.assertNotIn("ensure", [c[0] for c in self.wts.calls])
        self.assertEqual(self.windows(), [])


class Up(unittest.TestCase):
    def test_up(self):
        tmux = FakeTmux(own_pane="")
        ctx = make_ctx(tmux, environ={"TMUX": "", "TMUX_PANE": ""})
        code = cli.run(ctx, ["up", "x", "--", "--model", "opus"])
        self.assertEqual(code, 0)
        self.assertEqual(ctx.out.getvalue(), "fleet 'fleet-x' up: orchestrator in window 0 (/work).\n"
                                             "Attach with: tmux attach -t fleet-x\n")
        pane = list(tmux.panes_.values())[0]
        self.assertEqual((pane.session, pane.window), ("fleet-x", "orch"))
        self.assertEqual(tmux.pane_options[(pane.id, "@fleet_role")], "orch")
        self.assertEqual(pane.opened, [True])
        self.assertEqual([i[2] for i in pane.intents if i[0] == "type"], [False])  # the shell line in one go
        self.assertEqual(pane.submitted[0][0],
                         "claude /fleet-orchestrator --name fleet-x --permission-mode auto --allowedTools 'Bash(/opt/fleet/scripts/fleet *)' --model opus")
        self.assertEqual(cli.run(ctx, ["up", "x"]), 1)
        self.assertIn("session 'fleet-x' already exists", ctx.err.getvalue())

    def test_attach_from_a_terminal(self):
        tmux = FakeTmux(own_pane="")
        ctx = make_ctx(tmux, environ={"TMUX": "", "TMUX_PANE": ""})
        ctx.isatty = lambda: True
        cli.run(ctx, ["up"])
        self.assertIn(("attach", "fleet"), tmux.calls)


if __name__ == "__main__":
    unittest.main()
