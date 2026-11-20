"""Screen parsing against captures of real Claude Code 2.1.329 screens (tests/fixtures/*.ansi from
`tmux capture-pane -e -p`, *.txt from `tmux capture-pane -p`)."""

import unittest

from fakes import box_screen, fixture

from fleetlib.screen import Screen, shows_typed


def real(name, ansi=True):
    return Screen(fixture(name + (".ansi" if ansi else ".txt")), ansi=ansi)


class RealCaptures(unittest.TestCase):
    """Each fixture, in both capture forms: what reconcile, send and await_prompt see."""

    CASES = {
        # name: (classify, ready, trust, typed, ghost)  typed/ghost None = no prompt box
        "busy": ("busy", True, False, "", ""),
        "done": ("quiet", True, False, "", ""),
        "interrupted": ("quiet", True, False, "", ""),
        "question_cancelled": ("quiet", True, False, "", ""),
        "empty_prompt": ("quiet", True, False, "", 'Try "fix lint errors"'),
        "typed_text": ("quiet", True, False, "hello typed text", ""),
        "typed_after_suggestion": ("quiet", True, False, "run", ""),
        "suggested_prompt": ("quiet", True, False, "", "run it with python3"),
        "permission_dialog": ("dialog", False, False, None, None),
        "permission_dialog_bash": ("dialog", False, False, None, None),
        "ask_user_question": ("dialog", False, False, None, None),
        "trust_dialog": ("dialog", False, True, None, None),
        # `claude --name w1` labels the rule above the box: "──────── w1 ─"
        "named_session": ("quiet", True, False, "", 'Try "fix typecheck errors"'),
    }

    def check(self, name, ansi):
        cls, ready, trust, typed, ghost = self.CASES[name]
        s = real(name, ansi)
        self.assertEqual(s.classify(), cls)
        self.assertEqual(s.is_ready(), ready)
        self.assertEqual(s.is_trust_dialog(), trust)
        box = s.prompt_box()
        if typed is None:
            self.assertIsNone(box)
        else:
            self.assertIsNotNone(box)
            if ansi:
                self.assertEqual((box.typed, box.ghost), (typed, ghost))
            else:
                # Without colours only the "Try ..." placeholder is recognisable as not typed.
                self.assertEqual(box.typed + box.ghost, typed + ghost)

    def test_ansi_captures(self):
        for name in self.CASES:
            with self.subTest(name):
                self.check(name, True)

    def test_plain_captures(self):
        for name in self.CASES:
            with self.subTest(name):
                self.check(name, False)

    def test_wrapped_typed_text_is_joined(self):
        box = real("typed_wrapped").prompt_box()
        self.assertEqual(box.typed, "This is a long line of typed text that should wrap around inside the "
                                    "prompt box because it is wider than the pane which is one hundred columns wide.")
        self.assertEqual(box.ghost, "")


class SuggestedPrompt(unittest.TestCase):
    """The known gap of the bash version: a grey suggestion looked like typed text."""

    def test_suggestion_is_ghost_not_typed(self):
        box = real("suggested_prompt").prompt_box()
        self.assertEqual(box.typed, "")
        self.assertEqual(box.ghost, "run it with python3")

    def test_plain_capture_cannot_tell_them_apart(self):
        # Why fleet captures with -e: the same screen without attributes reads as typed text.
        self.assertEqual(real("suggested_prompt", ansi=False).prompt_box().typed, "run it with python3")

    def test_typed_and_dim_on_one_row(self):
        box = Screen(box_screen(typed="abc", ghost=" def"), ansi=True).prompt_box()
        self.assertEqual((box.typed, box.ghost), ("abc", "def"))

    def test_dim_ends_at_sgr_22_and_reset(self):
        raw = box_screen(typed="\x1b[2mghost\x1b[22mreal")
        self.assertEqual(Screen(raw, ansi=True).prompt_box().typed, "real")
        raw = box_screen(typed="\x1b[1;2mghost\x1b[mreal")
        self.assertEqual(Screen(raw, ansi=True).prompt_box().typed, "real")

    def test_colour_arguments_are_not_dim(self):
        # 38;5;2 is green, not "dim"; 38;2;2;2;2 is an RGB colour.
        for sgr in ("38;5;2", "48;5;2", "38;2;2;2;2", "38:5:2"):
            raw = box_screen(typed="\x1b[%smtyped" % sgr)
            self.assertEqual(Screen(raw, ansi=True).prompt_box().typed, "typed", sgr)

    def test_dim_carries_across_rows(self):
        # tmux only emits changes, so a dim run started on one row continues on the next.
        raw = RULE_ONLY + "\x1b[39m❯ abc\x1b[2m\n  def\n" + RULE_ONLY + "\x1b[0m  footer\n"
        box = Screen(raw, ansi=True).prompt_box()
        self.assertEqual((box.typed, box.ghost), ("abc", "def"))


class Regions(unittest.TestCase):
    """Markers count only where Claude Code draws them, not anywhere in the conversation."""

    def test_spinner_alone_is_busy(self):
        raw = fixture("busy.ansi").replace("esc to interrupt", "")
        s = Screen(raw, ansi=True)
        self.assertNotIn("esc to interrupt", s.text)
        self.assertEqual(s.classify(), "busy")

    def test_footer_alone_is_busy(self):
        lines = fixture("busy.ansi").split("\n")
        lines = [l for l in lines if "Elucidating" not in l]
        s = Screen("\n".join(lines), ansi=True)
        self.assertEqual(s.classify(), "busy")

    def test_markers_in_conversation_do_not_count(self):
        convo = ("  the footer says esc to interrupt while busy\n"
                 "  and dialogs end with Esc to cancel or Enter to select\n"
                 "⏺ Loading… (3 items)\n") + "\n" * 20
        s = Screen(box_screen(above=convo), ansi=True)
        self.assertEqual(s.classify(), "quiet")
        self.assertTrue(s.is_ready())

    def test_tool_line_with_ellipsis_is_not_a_spinner(self):
        s = Screen(box_screen(above="⏺ Reading… (2 files)\n"), ansi=True)
        self.assertFalse(s.is_busy())

    def test_spinner_above_a_todo_list(self):
        todo = "".join("     ◻ task %d\n" % i for i in range(8))
        s = Screen(box_screen(above="✻ Baking… (12s · ↓ 1.2k tokens)\n" + todo), ansi=True)
        self.assertTrue(s.is_busy())

    def test_past_user_message_is_not_the_prompt_box(self):
        # A dialog hides the box; the last ❯ row on screen is the user's earlier message.
        s = real("permission_dialog")
        self.assertIn("❯ Create hello.py", s.text)
        self.assertIsNone(s.prompt_box())

    def test_no_claude_no_prompt_box(self):
        s = Screen("user@host ~ $ \n\n", ansi=True)
        self.assertIsNone(s.prompt_box())
        self.assertFalse(s.is_ready())
        self.assertEqual(s.classify(), "quiet")

    def test_unclosed_box_is_not_ready(self):
        s = Screen(RULE_ONLY + "\x1b[39m❯ \n", ansi=True)
        self.assertIsNone(s.prompt_box())


RULE_ONLY = "\x1b[38;5;244m" + "─" * 60 + "\n"


class ShowsTyped(unittest.TestCase):
    def test_wrapped_text_matches(self):
        text = ("This is a long line of typed text that should wrap around inside the prompt box because "
                "it is wider than the pane which is one hundred columns wide.")
        self.assertTrue(shows_typed(real("typed_wrapped"), text))

    def test_text_elsewhere_on_screen_does_not_count(self):
        # The text is in the conversation (a past message), but the box is empty.
        s = real("permission_dialog_bash")
        self.assertFalse(shows_typed(s, "Run hello.py with python3"))
        s = real("done")
        self.assertFalse(shows_typed(s, "Write a 400 word story about a lighthouse keeper."))

    def test_suggestion_does_not_count_as_typed(self):
        self.assertFalse(shows_typed(real("suggested_prompt"), "run it with python3"))


class Tail(unittest.TestCase):
    def test_tail_skips_blank_rows(self):
        s = real("trust_dialog", ansi=False)
        self.assertEqual(s.tail(1), [" Enter to confirm · Esc to cancel"])
        self.assertEqual(s.tail(0), [])


if __name__ == "__main__":
    unittest.main()
