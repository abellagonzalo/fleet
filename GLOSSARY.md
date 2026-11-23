# fleet

fleet runs several Claude Code sessions side by side in one tmux session, so an orchestrator can hand them work, wait for them, and read their replies.

## Language

### Sessions and panes

**Worker**:
A Claude Code session that fleet started with `fleet new` and tracks with a worker record.
_Avoid_: agent, instance, window

**Orchestrator**:
The Claude Code session that drives the workers through the `fleet` commands.

**Untracked pane**:
A Claude Code session inside the fleet session that has no worker record. It is not a worker and has no worker state.
_Avoid_: untracked worker

### Worker state

What a worker is doing right now. Exactly one at a time.

**starting**:
The worker exists but Claude Code has not shown its prompt yet.

**idle**:
The worker is at its prompt and not working. Whether a reply is ready is the last turn outcome's business, not the state's.
_Avoid_: waiting (clashes with `fleet wait`), ready

**busy**:
A turn is in progress.
_Avoid_: running, working

**blocked**:
A turn is paused on a dialog that needs the user: a permission prompt or a question.
_Avoid_: waiting for input

**exited**:
The Claude Code session has ended; the window holds a shell.

_Avoid_ "done" as a state: a finished turn is an idle worker whose last turn outcome is finished.

### Work

**Turn**:
One prompt received by a worker, whoever typed or sent it, and the work it triggers, up to the point where the worker is back at its prompt.
_Avoid_: task, job, run

**Last turn outcome**:
How the worker's most recent turn ended: none (no turn yet in this session), finished, or interrupted. Kept separately from the worker's state; a new session starts at none.
_Avoid_: result, status

**Settled**:
A worker with nothing left to do until someone sends it a prompt: idle with no sent prompt still waiting to start, or exited. A blocked worker is not settled.
_Avoid_: done, finished (that's a turn outcome)

**Reconcile**:
Correcting a worker's state when the hooks missed a change (interrupts, rejected or cancelled dialogs). The transcript decides first: an interrupt marker for the worker's latest turn ends it as interrupted at once. The screen is the fallback, once the state is old enough, when the transcript can't tell.
