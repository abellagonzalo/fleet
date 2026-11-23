# Worker state is a state plus a last turn outcome; turns are ordered by sequence numbers

A worker record holds two separate things: its **state** (starting, idle, busy, blocked, exited: what it is doing now) and its **last turn outcome** (none, finished, interrupted: how its latest turn ended). There is no `done` state: a finished turn is an idle worker whose outcome is finished. We split them because "done" and "idle" both meant "at the prompt", and an interrupted turn was only told apart from a finished one by a free-text detail, so `fleet wait` returned 0 for both.

Events on a record are ordered by a per-record sequence number, bumped on every write under the record's lock, not by timestamps. `fleet wait` counts a turn as over only when it ended after a prompt was submitted that itself came after the last `fleet send` (`ended_seq > submit_seq > sent_seq`), so a Stop from the previous turn never answers a new prompt, including prompts queued behind a busy turn or sent with `--now`.

Corrections for changes no hook reports (Escape, a rejected or cancelled dialog) follow the same rule. Whether they come from the transcript or, as a fallback, the screen, they are written under the lock with a seq bump, like any event, and are dropped if the seq moved while they were being decided. A transcript interrupt marker is matched to the turn by its `promptId` against the `prompt_id` of the latest UserPromptSubmit, never by order or time, so a marker from an earlier turn never ends the current one. The reader's saved position on the record is not an event and does not bump the seq.

## Considered Options

- **Sub-second timestamps** instead of whole seconds: smaller change, but still a clock comparison, and does not handle a Stop that belongs to the turn before a queued send.
- **A turn counter bumped by `fleet send`**: the hook cannot tell which turn a Stop closes, and prompts typed into the pane by hand bypass `send`.
- **Keeping a flat state list with `done`**: no schema change, but interrupted and finished turns stay indistinguishable to `fleet wait`.

## Consequences

- `fleet wait` gains exit code 5 for an interrupted turn; `status` gains an OUTCOME column and the tab glyph a yellow `↯` for interrupted.
- No compatibility with older state files: workers are restarted after upgrading.
