#!/usr/bin/env bash
# fleet-hook <Event>: Claude Code hook for fleet workers (wired in via `claude --settings`).
# Records the worker's state in its state file, mirrors it to the tmux window option
# @fleet_state (status-bar glyph), and raises a macOS notification when the worker needs you.
# Must never block or fail Claude: prints nothing, always exits 0.
exec >/dev/null 2>&1

ev="${1:-}"
input="$(cat)"
[[ -n "${TMUX_PANE:-}" ]] || exit 0
STATE_DIR="${FLEET_STATE_DIR:-$HOME/.local/state/fleet}"
f="$STATE_DIR/workers/${TMUX_PANE#%}.json"
[[ -f "$f" ]] || exit 0   # not a fleet worker, or already removed

st="" notify=""
case "$ev" in
  SessionStart)           st=idle ;;
  UserPromptSubmit)       st=busy ;;
  PostToolUse)            st=busy ;;        # also clears "blocked" once a dialog is answered
  PermissionRequest)      st=blocked ;;     # permission dialog or AskUserQuestion; fires instantly
  Stop)                   st=done ;;
  SessionEnd)             st=exited ;;
  Notification)
    case "$(jq -r '.notification_type // empty' <<<"$input")" in
      permission_prompt|elicitation_dialog) notify=1 ;;   # fires ~6s after the dialog opens
    esac ;;
  *) exit 0 ;;
esac

now="$(date +%s)"
tmp="$f.$$"
jq --arg ev "$ev" --arg st "$st" --argjson now "$now" --argjson in "$input" '
  . + {ts: $now, event: $ev}
  + (if $st != "" then {state: $st} else {} end)
  + ($in | {session_id, transcript_path, cwd} | with_entries(select(.value != null)))
  + (if $ev == "PermissionRequest" then
       {detail: (if $in.tool_name == "AskUserQuestion" then "question" else "permission: \($in.tool_name // "?")" end)}
     elif $ev == "UserPromptSubmit" then {detail: "", prompt: (($in.prompt // "")[0:200])}
     elif $ev == "Stop" then {detail: "", last_message: ($in.last_assistant_message // "")}
     elif $ev == "SessionStart" then {detail: ""}
     else {} end)
' "$f" > "$tmp" && mv "$tmp" "$f" || rm -f "$tmp"

[[ -n "$st" ]] && tmux set -w -t "$TMUX_PANE" @fleet_state "$st"

if [[ -n "$notify" ]]; then
  name="$(jq -r '.name // "worker"' "$f")"
  msg="$(jq -r '.detail // "needs you"' "$f")"
  osascript -e 'on run argv' -e 'display notification (item 1 of argv) with title (item 2 of argv)' -e 'end run' \
    "$msg" "fleet: $name needs you" &
fi
exit 0
