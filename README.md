# fleet

Two skills and a CLI to run a fleet of interactive Claude Code workers as windows of one tmux session.
`skills/fleet-orchestrator/SKILL.md` (`/fleet-orchestrator`) is how the orchestrator uses it; `skills/fleet-worker/SKILL.md`
(`/fleet-worker`) is what every worker loads as its first prompt; `scripts/fleet help` lists every command.

Install:

    ln -s <this checkout>/scripts/fleet ~/.local/bin/fleet
    ln -s <this checkout>/skills/fleet-orchestrator ~/.claude/skills/fleet-orchestrator
    ln -s <this checkout>/skills/fleet-worker ~/.claude/skills/fleet-worker

- `scripts/fleet`: the CLI. `scripts/fleet-hook`: the Claude Code hook `fleet new` wires into workers.
- `scripts/fleetlib/`: the code. `screen.py` reads Claude Code's TUI from tmux captures, `state.py`
  holds the state files and the rules (reconcile, wait), `system.py` is the only place that runs
  tmux, git or wt, `cli.py` the commands, `hook.py` the hook.

Python 3.9+ and the standard library only (runs under macOS's `/usr/bin/python3`); no install step.

## Tests

    python3 -m unittest discover -s tests

`tests/fixtures/` holds captures of real Claude Code screens (`*.ansi` from `tmux capture-pane -e -p`,
`*.txt` from `tmux capture-pane -p`). When Claude Code's UI changes, capture the new screen from a
throwaway worker and add it there.

## Live checks

Never against the fleet you are working in. Use a session and state dir of your own:

    export FLEET_SESSION=fleet-test FLEET_STATE_DIR=/tmp/fleet-test/state
    tmux new-session -d -s fleet-test -n orch && tmux set -p -t =fleet-test:orch @fleet_role orch
    scripts/fleet new --no-worktree /tmp/somewhere w1 -- --model haiku
    ...
    tmux kill-session -t =fleet-test

Never run `scripts/fleet-hook` by hand from inside a fleet worker without `FLEET_STATE_DIR` set to a
scratch dir: it would write into that worker's live state file.
