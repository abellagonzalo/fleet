# fleet

Run several interactive Claude Code sessions side by side, each in its own git worktree, as
windows of one tmux session, with one more Claude Code acting as the **orchestrator** that keeps
track of them for you.

Every worker is a normal Claude Code TUI. You can attach to any window and drive it yourself at
any time. The orchestrator tells you which workers need you, relays your prompts and their replies,
and hands every permission dialog and question back to you. It never decides anything on its own.

```
tmux session "fleet"
├── 0: orch             orchestrator (/fleet-orchestrator)
├── 1: PROJ-123 ⋯       worker in <repo>/PROJ-123, busy
├── 2: fix-dates ⚠      worker in <repo>/fix-dates, waiting on a permission dialog
└── 3: docs-cleanup ✓   worker in <repo>/docs-cleanup, finished a turn
```

## Requirements

- macOS (notifications go through `osascript`), tmux, and Claude Code (`claude`) on PATH.
- Python 3.9+, standard library only. It runs under macOS's `/usr/bin/python3` and needs no install step.
- [worktrunk](https://github.com/max-sixty/worktrunk) (`wt`) to create and remove worktrees. You
  only need it for worktree workers; `--no-worktree` workers run without it.
- Repos laid out as `<repo>/main` (the main checkout), with each worktree as a sibling:
  `<repo>/<branch>`.

## Install

Symlink the CLI and the two skills:

    ln -s <this checkout>/scripts/fleet ~/.local/bin/fleet
    ln -s <this checkout>/skills/fleet-orchestrator ~/.claude/skills/fleet-orchestrator
    ln -s <this checkout>/skills/fleet-worker ~/.claude/skills/fleet-worker

## Quick start

    fleet up                 # tmux session "fleet", orchestrator in window 0, attaches you

Then talk to the orchestrator in plain language, for example "start a worker on ~/github/acme/api
for PROJ-123 and have it fix the date parsing bug". It runs the `fleet` commands for you. You can
also run them yourself from any shell:

    fleet new ~/github/acme/api PROJ-123 --prompt-stdin <<'EOF'
    Fix the date parsing bug described in PROJ-123.
    EOF
    fleet status             # state, age and last reply of every worker
    fleet wait PROJ-123      # block until it finishes or needs you
    fleet watch              # one line per finished, blocked or exited worker, until all are settled
    fleet last PROJ-123      # its last reply
    fleet rm PROJ-123        # close the window, remove the worktree if clean, delete the branch if merged

`fleet help` lists every command and flag.

## How it works

**Sessions and workers.** `fleet up [name]` creates the tmux session `fleet` (or `fleet-<name>`)
and starts the orchestrator in window 0 with `/fleet-orchestrator`. `fleet new <repo> <branch>`
opens a window named `<branch>` in the worktree `<repo>/<branch>`. If that worktree or branch
already exists, fleet reuses it. Otherwise it fetches origin and branches from the default branch.
The worktree is made with `wt`, and its post-start hooks run before claude starts, so `new` can
take a minute. `fleet new <repo> <b1> <b2> ...` starts several workers in parallel, after fetching
origin once, and prints one result line per branch. Workers start with `/fleet-worker` and reply `Ready: ...` when they are up. You
address workers by window name, which is also their branch.

**State.** `fleet new` wires `scripts/fleet-hook` into every worker through `claude --settings`.
The hook records each worker's state under `~/.local/state/fleet` (override it with
`FLEET_STATE_DIR`) and marks the window tab. A worker has a **state**, what it is doing now:

| state      | meaning                                                         | tab |
|------------|-----------------------------------------------------------------|-----|
| `starting` | claude has not shown its prompt yet                             |     |
| `idle`     | at its prompt, not working                                      | see below |
| `busy`     | working on a turn                                               | ⋯   |
| `blocked`  | a permission dialog or question is waiting for you (plus a macOS notification) | ⚠ |
| `exited`   | claude quit; the window holds a shell                           |     |

and the **outcome** of its last turn: `finished` (tab ✓), `interrupted` (tab ↯), or `none` in a
fresh session (also after `/clear`). See `GLOSSARY.md` for the terms and
`docs/adr/0001-worker-state-and-turn-ordering.md` for why they are kept apart.

After upgrading fleet, restart running workers: older state files are not read correctly.

Hooks miss a few changes: Escape, and a rejected or cancelled dialog, fire no hook. `status`, `wait`
and `watch` correct for that from the worker's transcript, which marks such a turn as interrupted
within a fraction of a second. They fall back to reading the screen only when the transcript can't
tell: it can't be read, it doesn't show the latest prompt, or an Escape came before any reply
streamed (Claude Code then writes nothing).

**Hand-offs.** The worker skill makes every reply open with a headline that fits in `fleet status`:
`Done: ...`, `Need input: ...` or `Failed: ...`. A worker whose finished turn says `Need input:` is
waiting on you just as much as a `blocked` one.

**Choosing work.** Ask the orchestrator to pick issues to run in parallel and it finds the ready,
unblocked ones through the repo's own issue-tracker docs, ranks them (your priorities first), skips
any whose predicted changes collide with a pick already made, and proposes a merge policy for each
from its risk. No worker starts until you approve the picks.

**End-of-task policy.** Before a worker starts on a task, the orchestrator agrees with you on what
"done" means, who merges, and what verification a merge needs. It then puts that policy in full
into the worker's first prompt. Its default proposal is that the worker merges its own PR once the
conditions hold; it takes effect only when you agree to it in the session. To set a policy for a
project, add a `## Fleet` section to the repo's `CLAUDE.md` or `AGENTS.md`. For a policy across all
projects, add it to `~/.claude/CLAUDE.md`. What you say in the session beats the repo's section,
and the repo's section beats the global one. The section is plain markdown that agents read; the
CLI doesn't parse it:

```markdown
## Fleet

- **Done means:** tests pass, PR opened, linked issue closed.
- **Merging:** the worker merges its own PR when the tests and the verification gate pass; otherwise it replies `Need input:`.
- **Verification:** no CI here; the gate is `python3 -m unittest discover -s tests` passing locally.
```

**Sending prompts safely.** Panes are shared with you, so `fleet send` types like a human, checks
that the text landed, and then presses Enter. It refuses if the prompt box holds text you typed,
if a dialog is open, or if claude has exited. Long prompts go through `--prompt-stdin` or
`--prompt-file`: the worker is told "Read <file> and do what it says.", so newlines, quotes and
length all survive. A prompt whose first line is a slash command is typed as that command instead,
with any lines after it passed to the command through a file. `fleet broadcast` sends one prompt to
many workers, and `send --now` interrupts a running turn.

## Repository layout

- `skills/fleet-orchestrator/SKILL.md`: how the orchestrator uses fleet, including what it may do
  without asking you.
- `skills/fleet-worker/SKILL.md`: what every worker loads as its first prompt (worktree rules,
  hand-off format).
- `scripts/fleet`: the CLI. `scripts/fleet-hook`: the Claude Code hook. It prints nothing and
  always exits 0, so it can never block Claude.
- `scripts/fleetlib/`: the code.
  - `cli.py`: the commands.
  - `screen.py`: reads Claude Code's TUI from tmux captures.
  - `lifecycle.py`: the worker record and every change to it (hook events, send, keys, reconcile
    from the transcript or, as a fallback, the screen), and what `wait` and `status` see.
  - `transcript.py`: reads a worker's Claude Code transcript (JSONL) incrementally, and what it
    says about the latest turn.
  - `state.py`: storage for the worker records.
  - `worktrees.py`: the `<repo>/main` layout rule, find-or-create a worktree for a branch (for
    `new`), and removing one only when it is clean, with its branch once merged (for `rm`).
  - `system.py`: the only place that runs tmux, git or wt. A pane's keyboard and screen are a
    `Terminal` (type, submit, press, screen, scrollback); its tmux adapter owns key encoding,
    typing pace and the guards that keep keys off the orchestrator's pane and your own.
  - `hook.py`: the hook: parses the event and hands it to `lifecycle.py`.

## Development

### Tests

    python3 -m unittest discover -s tests

`tests/fixtures/` holds captures of real Claude Code screens: `*.ansi` from
`tmux capture-pane -e -p` and `*.txt` from `tmux capture-pane -p`. When Claude Code's UI changes,
capture the new screen from a throwaway worker and add it there.

`tests/fixtures/transcript_*.jsonl` are excerpts of real Claude Code transcripts, one per situation
the hooks miss (interrupts, rejected or cancelled dialogs, queued prompts, `send --now`, `/clear`).
They were captured from Claude Code 2.1.330 on a throwaway worker in default permission mode (see
Live checks) and cut down to the records that matter. The working dir is `/WORKDIR`; `cwd`, session
IDs, git branch, usage and hook commands are dropped; thinking text is emptied; record uuids are kept,
since tests follow their `parentUuid` chain. `tests/test_transcript_fixtures.py` pins the format a
transcript-based state check would rely on, so when a fresh capture fails it, Claude Code's
transcript format changed. Findings: issue #15.

### Live checks

Never test against the fleet you are working in. Use a session and state dir of your own:

    export FLEET_SESSION=fleet-test FLEET_STATE_DIR=/tmp/fleet-test/state
    tmux new-session -d -s fleet-test -n orch && tmux set -p -t =fleet-test:orch @fleet_role orch
    scripts/fleet new --no-worktree /tmp/somewhere w1 -- --model haiku
    ...
    tmux kill-session -t =fleet-test

Never run `scripts/fleet-hook` by hand from inside a fleet worker unless `FLEET_STATE_DIR` points
to a scratch dir. Otherwise it writes into that worker's live state file.
