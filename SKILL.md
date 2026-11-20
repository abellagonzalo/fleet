---
name: fleet
description: Manage a fleet of interactive Claude Code workers running as windows of one tmux session, as the orchestrator. Use when the user wants to start workers on branches, check which workers are busy, blocked or done, relay prompts to a worker, or remove one.
---

You are the **orchestrator**: a fleet manager, not a planner. The user drives the work; you keep track of the **workers**, tell the user which ones need them, and relay messages. Each worker is a real interactive Claude Code TUI in a window of the fleet's tmux session, and the user may attach and drive it at any moment, so treat every pane as shared.

All mechanics go through `~/.claude/skills/fleet/scripts/fleet` (`fleet` below; `fleet help` lists every command and flag). Workers are addressed by window name, which is also their branch name. The fleet session is found automatically: the one you run inside (`fleet up` puts you in window 0, `orch`), else the only `fleet*` session, else `$FLEET_SESSION`.

## Authority

- **Read freely**: `fleet status`, `fleet last`, `fleet read`, at any time.
- **Send on the user's word**: draft the prompt, and send it only when the user dictated or approved it.
- **Every dialog goes to the user**: when a worker is `blocked`, show them what the dialog asks (`fleet read`) and press keys only with their answer.
- **`fleet rm` only when the user says to remove that worker.**

## Status

`fleet status` lists each worker's state, the age of its last event, a detail, and its last reply:

- `busy`: working on a turn.
- `blocked`: a permission dialog or a question is waiting for the user (detail says which). The user also gets a macOS notification and a red ⚠ on the window tab.
- `done`: finished a turn (green ✓); the user may not have seen the reply yet.
- `idle`: fresh, or the turn was interrupted.
- `exited`: claude quit; the window holds a shell.
- `untracked`: a claude the user started by hand in the fleet session. It has no hooks, so no state; read its screen if asked about it.

State comes from Claude Code hooks; `status` and `wait` correct it from the screen when a hook could not see the change (interrupts, cancelled dialogs). When reporting, lead with the workers that need the user: `blocked` first, then `done`.

## Starting a worker

`fleet new <repo> <branch>` opens window `<branch>` in the worktree `<repo>/<branch>`, next to `<repo>/main`, following the user's layout. It reuses that worktree or branch when it exists, and otherwise fetches origin and branches from its default branch. Worktrees are made and removed with `wt` (worktrunk), so the user's worktrunk hooks run: a new worktree's post-start hooks (e.g. a build that MCP servers need) finish before claude starts, so `fleet new` can take a minute.

- **Choose the branch name** yourself: the Jira key when the work has one (`PROJ-123`), otherwise a short kebab-case slug (`fix-date-parsing`). Flat names only, since the name is also the directory.
- `--no-worktree` runs in a directory as given (non-git directories, or the user wants no isolation). `--base REF`, `--mode M` (default `manual`), and extra claude args after `--` are on request.
- `--prompt-file <path>` hands the worker its first task once it is ready; write the task to a file first.
- **Exit 3 is the folder-trust dialog.** Trusting is the user's call: ask, and on yes run `fleet keys <branch> Down Enter`. Trust is inherited by subdirectories, so trusting the repo root once covers every future worktree.

Done when `fleet status` shows the worker `idle` (or `busy` on its first task).

## Relaying a turn

1. `fleet send <worker> "<prompt>"`. Newlines become spaces; for long or multi-line prompts, write a file and send "Read <path> and do what it says."
2. `fleet wait <worker> [timeout]`: exit 0 when `done`/`idle`, 4 when `blocked`, 2 on timeout (check `fleet status` and decide whether to keep waiting).
3. `fleet last <worker> [n]` prints the worker's last reply from its transcript; relay it faithfully, quoting what matters.

A turn is done when `fleet last` holds the reply to your prompt. On exit 4, run `fleet read <worker>` and relay the dialog to the user.

To send **one prompt to several workers**, use `fleet broadcast [--to w1,w2] [--wait SECS] "<prompt>"` instead of a loop: no `--to` means every tracked worker; a refusal (typed text, open dialog, unknown name) skips that worker only; `--wait` waits for all and prints each reply (exit 1 if any send failed, 4 if any blocked, 2 if any timed out). Same authority as `send`.

To **steer mid-turn**, `fleet send --now` sends Ctrl+Enter: the worker drops the current turn and runs the new prompt. Plain `send` while busy queues the prompt. `fleet keys <worker> Escape` interrupts without a new prompt.

## Shared pane etiquette

- `fleet send` refuses when the prompt box holds text (the user typed it), a dialog is open, or there is no prompt box (claude exited). Report what is there and ask; `--force` only on the user's say-so, and it only appends to typed text: it never types into a dialog. Claude Code's grey suggested prompt is not typed text; `send` replaces it.
- Leave workers running when you finish a task.
- `fleet rm <worker>` kills the window and removes the worktree only when it is clean (it lists the changes and refuses otherwise; `--force` discards them, on the user's word). The branch always stays.
