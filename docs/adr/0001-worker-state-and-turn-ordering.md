# Worker state is a state plus a last turn outcome; turns are ordered by sequence numbers

A worker record holds two separate things: its **state** (starting, idle, busy, blocked, exited: what it is doing now) and its **last turn outcome** (none, finished, interrupted: how its latest turn ended). There is no `done` state: a finished turn is an idle worker whose outcome is finished. We split them because "done" and "idle" both meant "at the prompt", and an interrupted turn was only told apart from a finished one by a free-text detail, so `fleet wait` returned 0 for both.

Events on a record are ordered by a per-record sequence number, bumped on every write under the record's lock, not by timestamps. `fleet wait` counts a turn as over only when it ended after a prompt was submitted that itself came after the last `fleet send` (`ended_seq > submit_seq > sent_seq`), so a Stop from the previous turn never answers a new prompt, including prompts queued behind a busy turn or sent with `--now`.

Corrections for changes no hook reports (Escape, a rejected or cancelled dialog) follow the same rule. Whether they come from the transcript or, as a fallback, the screen, they are written under the lock with a seq bump, like any event, and are dropped if the seq moved while they were being decided. A transcript interrupt marker is matched to the turn by its `promptId` against the `prompt_id` of the latest UserPromptSubmit, never by order or time, so a marker from an earlier turn never ends the current one. The reader's saved position on the record is not an event and does not bump the seq.

A queued prompt that runs as a turn of its own (behind a turn without tool calls, or started early by `send --now` or an Escape) fires no hook when it starts: its UserPromptSubmit fired when it was queued, with the running turn's `prompt_id`. When the transcript shows its prompt record, the start is written like a UserPromptSubmit: under the lock, seq-bumped (`submit_seq`, and `submit_prompt_id` = its `promptId`, so interrupt markers in it match), dropped if the seq moved since the transcript was read, and never twice for the same `promptId`. So `ended_seq > submit_seq > sent_seq` covers queued prompts and `send --now` without the screen. Until the queued prompt starts (an open `enqueue`, or a `dequeue` whose prompt record isn't written yet), `wait` counts the turn as not over. A queued turn the transcript already shows ended is not started again: its end record is written after its Stop hook ran, so that Stop is already on the record. For the same reason, if a queued start was written after its own Stop (the Stop landed while the transcript showed the prompt but not yet its end), the end record finishes the turn. Judging corrections against the seq from before the transcript is read (rather than after the reader's position is saved) applies to the interrupt corrections too. Only when the transcript can't be read (or holds no prompt record) does `wait` look at the screen after a finished turn, as before.

## Considered Options

- **Sub-second timestamps** instead of whole seconds: smaller change, but still a clock comparison, and does not handle a Stop that belongs to the turn before a queued send.
- **A turn counter bumped by `fleet send`**: the hook cannot tell which turn a Stop closes, and prompts typed into the pane by hand bypass `send`.
- **Keeping a flat state list with `done`**: no schema change, but interrupted and finished turns stay indistinguishable to `fleet wait`.

## Consequences

- `fleet wait` gains exit code 5 for an interrupted turn; `status` gains an OUTCOME column and the tab glyph a yellow `↯` for interrupted.
- No compatibility with older state files: workers are restarted after upgrading.
