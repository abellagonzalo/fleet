"""Reading a Claude Code TUI from a tmux capture.

Pure functions over the text of `tmux capture-pane -p [-e]`; nothing here talks to tmux, so all of it
is tested against saved captures of real screens (tests/fixtures).

The landmarks, as Claude Code 2.1 draws them:

    ✶ Elucidating… (2s · ↓ 186 tokens · thinking)      <- spinner line while a turn runs
    ────────────────────────────────────────────────    <- rule
    ❯ typed text, or a dim placeholder / suggestion     <- prompt box (may wrap onto indented lines)
    ────────────────────────────────────────────────    <- rule
      ⏸ manual mode on · esc to interrupt               <- footer; "esc to interrupt" while busy

A dialog (permission, AskUserQuestion, folder trust) replaces the prompt box and ends with a footer
such as "Esc to cancel · Tab to amend" or "Enter to select · ↑/↓ to navigate · Esc to cancel".

Only those regions are searched, never the conversation above them: a worker that prints fleet's own
source (or any text with "esc to interrupt" in it) must not look busy.
"""

import re

PROMPT = "❯"  # ❯
NBSP = " "
RULE_CHAR = "─"  # ─

DIALOG_FOOTER = re.compile(r"Esc to cancel|Enter to select")
BUSY_FOOTER = "esc to interrupt"
# "✶ Elucidating… (2s · ↓ 186 tokens)": a glyph, a gerund, an ellipsis, then "(<digit>".
# ⏺ and ⎿ start conversation lines (tool calls, results), so they never count as a spinner.
SPINNER = re.compile(r"^[^\s\w⏺⎿]\s+\w[\w '-]*…\s*\(\d")
SPINNER_REACH = 15  # lines above the prompt box to look for the spinner (todo lists sit in between)
FOOTER_LINES = 3  # trailing non-empty lines that hold a dialog footer
PLACEHOLDER = re.compile(r'^Try ".*"$')

_CSI = re.compile(r"\x1b\[([0-9;:?<=>]*)([ -/]*)([@-~])")
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


class Line:
    """One screen row: its plain text and, per character, whether it was drawn dim (SGR 2)."""

    __slots__ = ("text", "dim")

    def __init__(self, text, dim=None):
        self.text = text
        self.dim = dim if dim is not None else [False] * len(text)

    def __repr__(self):
        return "Line(%r)" % self.text


def _parse_ansi(raw):
    """Split a `capture-pane -e` dump into Lines. SGR state carries across rows, as tmux emits it."""
    raw = _OSC.sub("", raw)
    lines, text, dim = [], [], []
    is_dim = False
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if c == "\x1b":
            m = _CSI.match(raw, i)
            if m:
                if m.group(3) == "m" and not m.group(2):
                    is_dim = _apply_sgr(m.group(1), is_dim)
                i = m.end()
            else:
                i += 2  # a lone two-byte escape (e.g. ESC ( B); nothing we track
            continue
        if c == "\n":
            lines.append(Line("".join(text), dim))
            text, dim = [], []
        else:
            text.append(c)
            dim.append(is_dim)
        i += 1
    if text or not raw.endswith("\n"):
        lines.append(Line("".join(text), dim))
    return lines


def _apply_sgr(params, is_dim):
    """Fold one SGR sequence into the dim flag (the only attribute that matters here)."""
    codes = [p for p in re.split("[;:]", params)] if params else ["0"]
    i = 0
    while i < len(codes):
        code = codes[i]
        if code in ("", "0", "22"):
            is_dim = False
        elif code == "2":
            is_dim = True
        elif code in ("38", "48", "58"):  # extended colour: skip its arguments
            if i + 1 < len(codes) and codes[i + 1] == "5":
                i += 2
            elif i + 1 < len(codes) and codes[i + 1] == "2":
                i += 4
        i += 1
    return is_dim


class PromptBox:
    """The prompt box's content. `typed` is what a person (or fleet) typed; `ghost` is dim text that
    Claude Code drew there itself: the `Try "..."` placeholder or a suggested next prompt."""

    def __init__(self, top, bottom, typed, ghost):
        self.top = top  # index of the rule above
        self.bottom = bottom  # index of the rule below
        self.typed = typed
        self.ghost = ghost

    def __repr__(self):
        return "PromptBox(typed=%r, ghost=%r)" % (self.typed, self.ghost)


class Screen:
    """A captured screen. Build it with Screen(text, ansi=True) from `capture-pane -e -p` output so
    dim text can be told apart; with ansi=False every character counts as typed."""

    def __init__(self, raw, ansi=False):
        self.ansi = ansi
        if ansi:
            self.lines = _parse_ansi(raw)
        else:
            self.lines = [Line(t) for t in raw.split("\n")]
            if raw.endswith("\n"):
                self.lines.pop()

    @property
    def text(self):
        return "\n".join(l.text for l in self.lines)

    def nonempty(self):
        return [l.text for l in self.lines if l.text.strip()]

    def tail(self, n):
        """The last n non-empty rows, for showing the user what the screen says."""
        return self.nonempty()[-n:] if n > 0 else []

    # ---- landmarks ----

    def prompt_box(self):
        """The prompt box (a ❯ row right under a rule, closed by another rule), or None when there is
        none on screen: a dialog replaced it, claude is not running, or the UI changed."""
        rows = self.lines
        for i in range(len(rows) - 1, 0, -1):
            if not rows[i].text.startswith(PROMPT) or not _is_rule(rows[i - 1].text):
                continue
            j = i + 1
            while j < len(rows) and not _is_rule(rows[j].text):
                j += 1
            if j == len(rows):
                return None  # no closing rule: half-drawn or not a prompt box
            typed, ghost = _box_text(rows[i:j])
            if not self.ansi and PLACEHOLDER.match(typed):
                typed, ghost = "", typed  # without colours, only the placeholder is recognisable
            return PromptBox(i - 1, j, typed, ghost)
        return None

    def has_dialog(self):
        return any(DIALOG_FOOTER.search(t) for t in self.tail(FOOTER_LINES))

    def is_trust_dialog(self):
        return self.has_dialog() and any("trust this folder" in l.text for l in self.lines)

    def is_busy(self):
        """A turn is running: the footer says "esc to interrupt", or the spinner line is up. The footer
        hint alone is not enough: it can be cut off or replaced while the turn runs."""
        box = self.prompt_box()
        if box is None:
            footer = self.tail(FOOTER_LINES)
            above = self.tail(SPINNER_REACH)
        else:
            footer = [l.text for l in self.lines[box.bottom + 1:]]
            above = [l.text for l in self.lines[max(0, box.top - SPINNER_REACH):box.top]]
        if any(BUSY_FOOTER in t for t in footer):
            return True
        return any(SPINNER.search(t) for t in above)

    def is_ready(self):
        """Claude's prompt box is up and no dialog covers it."""
        return self.prompt_box() is not None and not self.has_dialog()

    def classify(self):
        """'dialog', 'busy' or 'quiet': what reconcile needs to know."""
        if self.has_dialog():
            return "dialog"
        if self.is_busy():
            return "busy"
        return "quiet"


def _is_rule(text):
    t = text.strip()
    return len(t) >= 10 and t == RULE_CHAR * len(t)


def _box_text(rows):
    """(typed, ghost) text of the prompt box rows: first row after "❯ ", wrapped rows indented by 2."""
    typed, ghost = [], []
    for k, row in enumerate(rows):
        text, dim = row.text, row.dim
        start = 1 if k == 0 else 0
        while start < len(text) and text[start] in (" ", NBSP):
            start += 1
        t_chars = "".join(c for c, d in zip(text[start:], dim[start:]) if not d)
        g_chars = "".join(c for c, d in zip(text[start:], dim[start:]) if d)
        typed.append(t_chars.replace(NBSP, " ").strip())
        ghost.append(g_chars.replace(NBSP, " ").strip())
    typed_s = " ".join(t for t in typed if t)
    ghost_s = " ".join(g for g in ghost if g)
    return typed_s, ghost_s


def compact(s):
    """Text without any whitespace: wrapping in the prompt box adds and drops spaces."""
    return re.sub(r"\s+", "", s.replace(NBSP, " "))


def shows_typed(screen, text):
    """Did `text` land in the prompt box? Checks the end of the text, where the cursor is: a long
    prompt may scroll its start out of the box."""
    box = screen.prompt_box()
    if box is None:
        return False
    want = compact(text)[-20:]
    return want in compact(box.typed)
