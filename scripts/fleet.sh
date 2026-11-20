#!/usr/bin/env bash
# fleet: run a fleet of interactive Claude Code workers as windows of one tmux session.
# Usage: fleet <command> [args]   (run `fleet help`)
set -euo pipefail

FLEET="$(readlink -f "${BASH_SOURCE[0]}")"   # resolves the ~/.local/bin symlink
HOOK="$(dirname "$FLEET")/fleet-hook"
STATE_DIR="${FLEET_STATE_DIR:-$HOME/.local/state/fleet}"
DELAY="${FLEET_DELAY:-0.02}"   # seconds between typed characters
STALE=3                        # seconds before hook state may be corrected from the screen

die() { echo "fleet: $*" >&2; exit 1; }

# ---------- tmux / screen helpers ----------

screen() { tmux capture-pane -t "$1" -p; }
# Busy: the footer's "esc to interrupt", or the spinner line ("✻ Razzmatazzing… (3s · ↓ 81 tokens)").
# The footer hint alone is not enough: it can be cut off or replaced while the turn runs.
busy_marker() { grep -qE 'esc to interrupt|…[[:space:]]*\([0-9]'; }
dialog_marker() { grep -qE 'Esc to cancel|Enter to select'; }

# Text currently in the prompt box ('' when empty or showing the placeholder).
input_line() {
  screen "$1" | grep '^❯' | tail -1 | perl -CS -pe 's/^\x{276f}[\s\x{a0}]*//; s/^Try ".*"\s*$//; s/[\s\x{a0}]+$//'
}

type_text() {
  local t="$1" s="$2" i
  [[ -n "$t" ]] || die "internal: empty pane id; refusing to type into the current pane"
  for ((i = 0; i < ${#s}; i++)); do
    tmux send-keys -t "$t" -l "${s:$i:1}"
    sleep "$DELAY"
  done
}

# The fleet session this command acts on.
session() {
  if [[ -n "${FLEET_SESSION:-}" ]]; then echo "$FLEET_SESSION"; return; fi
  if [[ -n "${TMUX:-}" && -n "${TMUX_PANE:-}" ]]; then
    local s; s="$(tmux display -p -t "$TMUX_PANE" '#S')"
    if [[ "$s" == fleet || "$s" == fleet-* ]]; then echo "$s"; return; fi
  fi
  local all; all="$(tmux list-sessions -F '#S' 2>/dev/null | grep -E '^fleet(-|$)' || true)"
  case "$(grep -c . <<<"$all" || true)" in
    0) die "no fleet session running; start one with: fleet up [name]" ;;
    1) echo "$all" ;;
    *) die "several fleet sessions running ($(echo $all)); set FLEET_SESSION=<session>" ;;
  esac
}

# Worker name (window name) -> pane id. Falls back to a raw tmux target.
target() {
  local n="$1" s p=""
  if s="$(session 2>/dev/null)"; then
    p="$(tmux list-panes -s -t "=$s" -F '#{window_name}	#{pane_id}' | awk -F'\t' -v n="$n" '$1==n {print $2; exit}')"
  fi
  # Fallback for raw tmux targets (e.g. %12). An unknown target can make `tmux display` print an empty
  # id and exit 0, and `-t ""` means the current pane: the orchestrator itself. Refuse both.
  [[ -n "$p" ]] || p="$(tmux display -p -t "$n" '#{pane_id}' 2>/dev/null || true)"
  [[ -n "$p" ]] || die "no worker '$n'${s:+ in fleet session '$s'}"
  [[ -z "${TMUX_PANE:-}" || "$p" != "$TMUX_PANE" ]] || die "'$n' resolves to this pane (the orchestrator), not a worker"
  echo "$p"
}

state_file() { echo "$STATE_DIR/workers/${1#%}.json"; }

set_state() {   # set_state <file> <state> [detail]
  local f="$1" st="$2" detail="${3:-}" pane tmp="$1.$$"
  jq --arg st "$st" --arg d "$detail" --argjson now "$(date +%s)" '. + {state: $st, detail: $d, ts: $now}' "$f" > "$tmp" && mv "$tmp" "$f"
  pane="$(jq -r .pane "$f")"
  tmux set -w -t "$pane" @fleet_state "$st" 2>/dev/null || true
}

# Hooks miss interrupts and cancelled dialogs. When hook state is stale, trust the screen.
reconcile() {
  local f="$1" st ts pane scr
  st="$(jq -r .state "$f")"; ts="$(jq -r '.ts // 0' "$f")"; pane="$(jq -r .pane "$f")"
  [[ "$st" == busy || "$st" == blocked ]] || return 0
  (( $(date +%s) - ts >= STALE )) || return 0
  scr="$(screen "$pane" 2>/dev/null)" || return 0
  if dialog_marker <<<"$scr"; then
    [[ "$st" == blocked ]] || set_state "$f" blocked "dialog on screen"
  elif busy_marker <<<"$scr"; then
    [[ "$st" == busy ]] || set_state "$f" busy
  else
    # One quiet screen can be a redraw between frames: look again before calling the turn over.
    sleep 1.5
    scr="$(screen "$pane" 2>/dev/null)" || return 0
    if dialog_marker <<<"$scr"; then set_state "$f" blocked "dialog on screen"
    elif busy_marker <<<"$scr"; then set_state "$f" busy
    else set_state "$f" idle "interrupted"
    fi
  fi
}

# Drop state files whose pane is gone (or was reused by something else).
sweep() {
  local f pane name
  for f in "$STATE_DIR"/workers/*.json; do
    [[ -e "$f" ]] || continue
    pane="$(jq -r .pane "$f")"; name="$(jq -r .name "$f")"
    if [[ "$(tmux display -p -t "$pane" '#{@fleet_name}' 2>/dev/null)" != "$name" ]]; then rm -f "$f"; fi
  done
}

# Status-bar glyph from @fleet_state. Window-scoped options, so set per fleet window
# (keeps the user's global tmux config untouched).
style_window() {
  local w="$1" g
  g='#{?#{==:#{@fleet_state},blocked},#[fg=red]⚠#[default],#{?#{==:#{@fleet_state},busy},⋯,#{?#{==:#{@fleet_state},done},#[fg=green]✓#[default],}}}'
  tmux set -w -t "$w" window-status-format "#I:#W$g "
  tmux set -w -t "$w" window-status-current-format "#[bold]#I:#W#[nobold]$g*"
  tmux set -w -t "$w" allow-rename off
  tmux set -w -t "$w" automatic-rename off
}

# Wait for claude's prompt box in <pane>. Exit 3 on the folder-trust dialog.
await_prompt() {
  local pane="$1" name="$2" dir="$3" i
  for ((i = 0; i < 60; i++)); do
    sleep 1
    if screen "$pane" | grep -q '^❯' && ! screen "$pane" | grep -q 'trust this folder'; then return 0; fi
    if screen "$pane" | grep -q 'trust this folder'; then
      echo "trust-prompt: claude asks whether to trust $dir. Ask the user; on yes run: fleet keys $name Down Enter" >&2
      exit 3
    fi
  done
  screen "$pane" | grep -v '^$' | tail -20
  die "claude did not show a prompt box within 60s (screen above)"
}

# ---------- commands ----------

cmd_up() {
  local name="" detach=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --detach) detach=1; shift ;;
      --) shift; break ;;
      -*) die "unknown option $1" ;;
      *) [[ -z "$name" ]] || die "usage: fleet up [name] [--detach] [-- claude args]"; name="$1"; shift ;;
    esac
  done
  local s="fleet${name:+-$name}"
  tmux has-session -t "=$s" 2>/dev/null && die "session '$s' already exists. Attach with: tmux attach -t $s"
  tmux new-session -d -s "$s" -n orch -c "$PWD" -x 200 -y 50
  style_window "=$s:orch"
  tmux set -p -t "=$s:orch" @fleet_role orch
  local extra=""; [[ $# -gt 0 ]] && extra="$(printf ' %q' "$@")"
  tmux send-keys -t "=$s:orch" "claude /fleet --name $(printf %q "$s") --allowedTools $(printf %q "Bash($FLEET *)")$extra" Enter
  if [[ $detach -eq 0 && -z "${TMUX:-}" && -t 1 ]]; then exec tmux attach -t "=$s"; fi
  echo "fleet '$s' up: orchestrator in window 0 ($PWD)."
  if [[ -n "${TMUX:-}" ]]; then echo "Switch with: tmux switch-client -t $s"; else echo "Attach with: tmux attach -t $s"; fi
}

cmd_new() {
  local src="" name="" worktree=1 base="" mode="manual" pfile=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --no-worktree) worktree=0; shift ;;
      --base) base="$2"; shift 2 ;;
      --mode) mode="$2"; shift 2 ;;
      --prompt-file) pfile="$2"; shift 2 ;;
      --) shift; break ;;
      -*) die "unknown option $1" ;;
      *) if [[ -z "$src" ]]; then src="$1"; elif [[ -z "$name" ]]; then name="$1"; else die "unexpected argument $1"; fi; shift ;;
    esac
  done
  [[ -n "$src" && -n "$name" ]] || die "usage: fleet new <repo|repo/main> <branch> [--no-worktree] [--base REF] [--mode M] [--prompt-file P] [-- claude args]"
  [[ "$name" != */* ]] || die "'$name' contains '/': worktrees live at <repo>/<branch>, so names must be flat"
  [[ "$name" != orch ]] || die "'orch' is the orchestrator's window"
  [[ -z "$pfile" || -f "$pfile" ]] || die "no such prompt file: $pfile"
  [[ -n "$pfile" ]] && pfile="$(cd "$(dirname "$pfile")" && pwd)/$(basename "$pfile")"
  [[ -d "$src" ]] || die "no such directory: $src"
  src="$(cd "$src" && pwd -P)"

  local s; s="$(session)"
  if tmux list-windows -t "=$s" -F '#{window_name}' | grep -qxF -- "$name"; then
    die "a window named '$name' already runs in '$s'"
  fi

  local dir repo="" wt=""
  if [[ $worktree -eq 0 ]]; then
    dir="$src"
  else
    git check-ref-format --branch "$name" >/dev/null 2>&1 || die "'$name' is not a valid branch name"
    if [[ "$(basename "$src")" == main && -e "$src/.git" ]]; then repo="$(dirname "$src")"
    elif [[ -e "$src/main/.git" ]]; then repo="$src"
    else die "$src does not follow <repo>/main (no git checkout at <repo>/main). Pass --no-worktree to run there without a worktree."
    fi
    # Worktrees go through worktrunk (wt): its worktree-path template decides where they live,
    # and its hooks (e.g. a post-start build) make them usable before claude starts.
    command -v wt >/dev/null 2>&1 || die "wt (worktrunk) not found; install it, or pass --no-worktree"
    local g=(git -C "$repo/main") w=(wt -C "$repo/main")
    dir="$("${w[@]}" list --format json 2>/dev/null \
      | jq -r --arg b "$name" '.items[] | select(.branch == $b) | .worktree.path // empty' | head -1 || true)"
    if [[ -n "$dir" ]]; then
      echo "reusing worktree $dir"
    else
      "${g[@]}" fetch --quiet origin 2>/dev/null || echo "warning: git fetch failed; using local refs" >&2
      local sw=(switch "$name") how
      if "${g[@]}" show-ref --verify --quiet "refs/heads/$name"; then
        how="from existing branch $name"
      elif [[ -z "$base" ]] && "${g[@]}" show-ref --verify --quiet "refs/remotes/origin/$name"; then
        how="tracking origin/$name"   # wt switch creates the local tracking branch
      else
        if [[ -z "$base" ]]; then
          base="$("${g[@]}" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || true)"
          [[ -n "$base" ]] || for b in origin/main origin/master; do
            "${g[@]}" rev-parse --verify --quiet "$b" >/dev/null && { base="$b"; break; }
          done
          [[ -n "$base" ]] || die "cannot find origin's default branch; pass --base REF"
        fi
        sw=(switch --create "$name" --base "$base")
        how="on new branch $name from $base"
      fi
      # --no-hooks, then run them here: wt switch would start post-start hooks in the background,
      # and claude must not start (and spawn its MCP servers) before they finish.
      local out
      out="$("${w[@]}" "${sw[@]}" --no-cd --no-hooks --yes --format json)" || die "wt switch failed for $name"
      dir="$(jq -r '.path // .worktree_path // .worktree.path // empty' <<<"$out")"
      [[ -n "$dir" && -d "$dir" ]] || die "wt switch did not report the new worktree's path: $out"
      echo "worktree $dir $how"
      local h
      for h in pre-start post-start post-switch; do
        echo "running wt $h hooks..."
        wt -C "$dir" hook "$h" --yes --foreground >&2 \
          || echo "warning: wt $h hooks failed in $dir" >&2
      done
    fi
    dir="$(cd "$dir" && pwd -P)"
    [[ "$dir" == "$repo/main" ]] || wt="$dir"
  fi

  mkdir -p "$STATE_DIR/workers"
  local hooks="$STATE_DIR/hooks.json"
  jq -n --arg h "$HOOK" '{hooks: ([
      "SessionStart","UserPromptSubmit","PermissionRequest","PostToolUse","Notification","Stop","SessionEnd"
    ] | map({key: ., value: [{hooks: [{type: "command", command: "\($h) \(.)"}]}]}) | from_entries)}' > "$hooks"

  local pane; pane="$(tmux new-window -d -t "=$s:" -n "$name" -c "$dir" -P -F '#{pane_id}')"
  tmux set -p -t "$pane" @fleet_name "$name"
  style_window "$pane"
  jq -n --arg name "$name" --arg s "$s" --arg pane "$pane" --arg dir "$dir" --arg repo "$repo" \
        --arg wt "$wt" --arg mode "$mode" --argjson now "$(date +%s)" \
    '{name: $name, session: $s, pane: $pane, dir: $dir, repo: $repo, worktree: $wt, branch: (if $repo != "" then $name else "" end),
      mode: $mode, state: "starting", detail: "", ts: $now, created: $now}' > "$(state_file "$pane")"

  local extra=""; [[ $# -gt 0 ]] && extra="$(printf ' %q' "$@")"
  tmux send-keys -t "$pane" "claude --name $(printf %q "$name") --permission-mode $(printf %q "$mode") --settings $(printf %q "$hooks")$extra" Enter
  local rc=0; ( await_prompt "$pane" "$name" "$dir" ) || rc=$?
  if [[ $rc -ne 0 ]]; then
    [[ -n "$pfile" ]] && echo "prompt not sent; once the worker is ready run: fleet send $name \"Read $pfile and do what it says.\"" >&2
    exit "$rc"
  fi
  echo "ready: worker '$name' in $dir (mode $mode). Attach: tmux attach -t $s, then select window '$name'"
  if [[ -n "$pfile" ]]; then cmd_send "$name" "Read $pfile and do what it says."; fi
}

cmd_send() {
  local now=0 force=0 name=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --now) now=1; shift ;;
      --force) force=1; shift ;;
      *) name="$1"; shift; break ;;
    esac
  done
  [[ -n "$name" && $# -gt 0 ]] || die "usage: fleet send [--now] [--force] <worker> <text>"
  local t; t="$(target "$name")" || exit 1
  local text="$*"; text="${text//$'\n'/ }"
  if [[ $now -eq 0 ]] && screen "$t" | dialog_marker; then
    screen "$t" | grep -v '^$' | tail -12
    die "'$name' shows a dialog (above); answer it with: fleet keys $name <key>"
  fi
  local pending; pending="$(input_line "$t")"
  if [[ -n "$pending" && $force -eq 0 ]]; then
    die "prompt box already holds: '$pending' (someone typed there). Re-run with --force to append anyway."
  fi
  type_text "$t" "$text"
  sleep 0.3
  if ! screen "$t" | grep -qF -- "${text:0:20}"; then
    screen "$t" | grep -v '^$' | tail -8
    die "typed text not visible in the prompt box; not submitting (screen above)"
  fi
  if [[ $now -eq 1 ]]; then
    tmux send-keys -t "$t" -l $'\e[13;5u'   # Ctrl+Enter as CSI-u: interrupt and run now
  else
    tmux send-keys -t "$t" Enter
  fi
  local f; f="$(state_file "$t")"
  if [[ -f "$f" ]]; then
    set_state "$f" busy
    local tmp="$f.$$"   # wait uses sent_at to ignore a Stop that belongs to an earlier turn
    jq --argjson now "$(date +%s)" '. + {sent_at: $now}' "$f" > "$tmp" && mv "$tmp" "$f"
  fi
  echo "sent$([[ $now -eq 1 ]] && echo ' (now)') to $name: $text"
}

cmd_wait() {
  local name="${1:?usage: fleet wait <worker> [timeout_s]}" timeout="${2:-600}" t f st i
  t="$(target "$name")" || exit 1; f="$(state_file "$t")"
  [[ -f "$f" ]] || die "'$name' is not a tracked worker (no hook state)"
  for ((i = 0; i < timeout; i++)); do
    reconcile "$f"
    st="$(jq -r .state "$f")"
    # A "done" counts only if its Stop hook fired at or after the last send; an older one is
    # the previous turn's (hook state lags the send by a moment).
    if [[ "$st" == done ]] && ! jq -e '(.sent_at // 0) <= (.ts // 0) and .event == "Stop"' "$f" >/dev/null; then
      st=busy
    fi
    case "$st" in
      done|idle|exited) echo "$st"; return 0 ;;
      blocked) echo "blocked: $(jq -r .detail "$f"). See it with: fleet read $name"; return 4 ;;
    esac
    sleep 1
  done
  echo "still $st after ${timeout}s"; return 2
}

# Names of the tracked workers in this fleet session.
worker_names() {
  local s f; s="$(session)"
  mkdir -p "$STATE_DIR/workers"; sweep
  for f in "$STATE_DIR"/workers/*.json; do
    [[ -e "$f" ]] || continue
    [[ "$(jq -r .session "$f")" == "$s" ]] && jq -r .name "$f"
  done
  return 0
}

cmd_broadcast() {
  local flags=() to="" waitt=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --now|--force) flags+=("$1"); shift ;;
      --to) to="${2:?--to needs a comma-separated list of workers}"; shift 2 ;;
      --wait) waitt="${2:?--wait needs a timeout in seconds}"; shift 2 ;;
      --) shift; break ;;
      -*) die "unknown option $1" ;;
      *) break ;;
    esac
  done
  [[ $# -gt 0 ]] || die "usage: fleet broadcast [--to w1,w2] [--now] [--force] [--wait SECS] <text>"
  local text="$*" workers=() w
  if [[ -n "$to" ]]; then IFS=, read -r -a workers <<<"$to"
  else while IFS= read -r w; do [[ -n "$w" ]] && workers+=("$w"); done < <(worker_names)
  fi
  [[ ${#workers[@]} -gt 0 ]] || die "no workers to send to"
  # Each send runs in a subshell: send dies on a refusal, and one refusal must not stop the rest.
  local sent=() rc=0
  for w in "${workers[@]}"; do
    if ( cmd_send ${flags[@]+"${flags[@]}"} "$w" "$text" ) >/dev/null; then sent+=("$w"); echo "sent to $w"
    else echo "NOT sent to $w (reason above)"; rc=1
    fi
  done
  [[ -n "$waitt" ]] || return "$rc"
  # Workers run concurrently, so waiting on each in turn costs no more than the slowest.
  local wr
  for w in ${sent[@]+"${sent[@]}"}; do
    wr=0; ( cmd_wait "$w" "$waitt" ) >/dev/null || wr=$?
    case "$wr" in
      0) echo "##### $w: done"; cmd_last "$w" ;;
      4) echo "##### $w: BLOCKED, see it with: fleet read $w"; [[ $rc -ne 0 ]] || rc=4 ;;
      2) echo "##### $w: still busy after ${waitt}s"; [[ $rc -ne 0 || $rc -eq 4 ]] || rc=2 ;;
      *) echo "##### $w: wait failed ($wr)"; rc=1 ;;
    esac
  done
  return "$rc"
}

cmd_read() {
  local name="${1:?usage: fleet read <worker> [lines]}" n="${2:-60}"
  local t; t="$(target "$name")" || exit 1
  tmux capture-pane -t "$t" -p -S "-$n" | grep -v '^$' || true
}

cmd_last() {
  local name="${1:?usage: fleet last <worker> [n]}" n="${2:-1}" t f tp
  t="$(target "$name")" || exit 1; f="$(state_file "$t")"
  [[ -f "$f" ]] || die "'$name' is not a tracked worker (no hook state)"
  tp="$(jq -r '.transcript_path // empty' "$f")"
  if [[ -z "$tp" || ! -f "$tp" ]]; then jq -r '.last_message // "(no reply yet)"' "$f"; return; fi
  local out
  out="$(jq -c 'select(.type == "assistant") | [.message.content[]? | select(.type == "text") | .text] | select(length > 0) | join("\n")' "$tp" | tail -n "$n")"
  if [[ -z "$out" ]]; then echo "(no reply yet)"; else jq -r '., "----"' <<<"$out"; fi
}

cmd_keys() {
  local name="${1:?usage: fleet keys <worker> <tmux-key>...}"; shift
  local t f; t="$(target "$name")" || exit 1; f="$(state_file "$t")"
  tmux send-keys -t "$t" "$@"
  # Answering a dialog: assume work resumes; reconcile corrects it from the screen if not.
  if [[ -f "$f" && "$(jq -r .state "$f")" == blocked ]]; then set_state "$f" busy; fi
}

age() { local d=$(( $(date +%s) - $1 )); if ((d < 60)); then echo "${d}s"; elif ((d < 3600)); then echo "$((d/60))m"; else echo "$((d/3600))h"; fi; }

cmd_status() {
  local json=0; [[ "${1:-}" == --json ]] && json=1
  local s; s="$(session)"
  mkdir -p "$STATE_DIR/workers"; sweep
  local f files=()
  for f in "$STATE_DIR"/workers/*.json; do
    [[ -e "$f" ]] || continue
    [[ "$(jq -r .session "$f")" == "$s" ]] || continue
    reconcile "$f"; files+=("$f")
  done
  local untracked
  untracked="$(tmux list-panes -s -t "=$s" -F '#{window_name}	#{pane_current_command}	#{@fleet_name}	#{@fleet_role}' \
    | awk -F'\t' '$3=="" && $4=="" && ($2 ~ /claude/ || $2 ~ /^[0-9]+\.[0-9]+\.[0-9]+$/) {print $1}')"
  if [[ $json -eq 1 ]]; then
    { [[ ${#files[@]} -gt 0 ]] && cat "${files[@]}"; true; } | jq -s --arg u "$untracked" '{workers: ., untracked: ($u | split("\n") | map(select(. != "")))}'
    return
  fi
  echo "fleet session: $s"
  if [[ ${#files[@]} -eq 0 && -z "$untracked" ]]; then echo "(no workers)"; return; fi
  printf '%-24s %-8s %-5s %-22s %s\n' WORKER STATE AGE DETAIL "LAST MESSAGE"
  for f in "${files[@]}"; do
    printf '%-24s %-8s %-5s %-22s %s\n' "$(jq -r .name "$f")" "$(jq -r .state "$f")" "$(age "$(jq -r .ts "$f")")" \
      "$(jq -r '.detail // ""' "$f" | cut -c1-22)" "$(jq -r '(.last_message // "") | gsub("\\s+"; " ")' "$f" | cut -c1-80)"
  done
  local u; while IFS= read -r u; do if [[ -n "$u" ]]; then printf '%-24s %-8s\n' "$u" untracked; fi; done <<<"$untracked"
}

cmd_rm() {
  local force=0 name=""
  while [[ $# -gt 0 ]]; do
    case "$1" in --force) force=1; shift ;; *) name="$1"; shift ;; esac
  done
  [[ -n "$name" ]] || die "usage: fleet rm <worker> [--force]"
  [[ "$name" != orch ]] || die "refusing to remove the orchestrator"
  local t f wt="" repo="" branch=""; t="$(target "$name")" || exit 1; f="$(state_file "$t")"
  if [[ -f "$f" ]]; then wt="$(jq -r .worktree "$f")"; repo="$(jq -r .repo "$f")"; branch="$(jq -r .branch "$f")"; fi
  if [[ -n "$wt" && -d "$wt" && $force -eq 0 && -n "$(git -C "$wt" status --porcelain)" ]]; then
    git -C "$wt" status --short | head -20
    die "uncommitted changes in $wt (above). Commit them, or re-run with --force to discard them."
  fi
  tmux kill-window -t "$t"
  rm -f "$f"
  echo "killed window '$name'"
  if [[ -n "$wt" && -d "$wt" ]]; then
    # wt remove runs the remove hooks; --no-delete-branch keeps the branch, --foreground so it is gone on return.
    local rmf=(); [[ $force -eq 1 ]] && rmf=(--force)
    wt -C "$repo/main" remove "$wt" --no-delete-branch --foreground --yes ${rmf[@]+"${rmf[@]}"} >&2 \
      || die "wt remove failed for $wt"
    echo "removed worktree $wt; branch $branch kept"
  fi
}

case "${1:-help}" in
  up)     shift; cmd_up "$@" ;;
  new)    shift; cmd_new "$@" ;;
  status) shift; cmd_status "$@" ;;
  send)   shift; cmd_send "$@" ;;
  wait)   shift; cmd_wait "$@" ;;
  read)   shift; cmd_read "$@" ;;
  last)   shift; cmd_last "$@" ;;
  keys)   shift; cmd_keys "$@" ;;
  rm)     shift; cmd_rm "$@" ;;
  broadcast) shift; cmd_broadcast "$@" ;;
  *) cat <<'H'
fleet up [name] [--detach] [-- claude args]   create tmux session fleet[-name], orchestrator claude in window 0 'orch'
fleet new <repo|repo/main> <branch> [opts]     worker window <branch> in worktree <repo>/<branch> (reused if present;
                                               else new branch from fetched origin default), made with wt; its hooks
                                               run in the foreground before claude starts. Exit 3: folder-trust dialog.
    --no-worktree        run in <dir> as given; <branch> is then just the worker name
    --base REF           branch from REF instead of origin's default branch
    --mode M             permission mode (default manual)
    --prompt-file P      once ready, send "Read P and do what it says."
    -- args              extra claude args
fleet status [--json]                          every worker: state (busy|blocked|done|idle|exited), age, detail, last reply
fleet send [--now] [--force] <worker> <text>   type text like a human, verify it landed, press Enter
                                               --now: Ctrl+Enter (interrupts a running turn, runs immediately)
fleet broadcast [--to w1,w2] [--now] [--force] [--wait SECS] <text>
                                               send to every tracked worker (or only --to); a refusal skips that
                                               worker, not the rest. --wait: wait for all, print each reply.
                                               Exit 1 if any send failed, else 4 if any blocked, 2 if any timed out
fleet wait <worker> [timeout_s]                block until done/idle (exit 0), blocked (exit 4) or timeout (exit 2)
fleet last <worker> [n]                        last n assistant replies from the transcript (default 1)
fleet read <worker> [lines]                    the screen plus <lines> of scrollback (default 60)
fleet keys <worker> <tmux-key>...              raw keys for dialogs: Enter, Escape, Up, Down, BTab, 1, 2...
fleet rm <worker> [--force]                    kill the window; remove its worktree if clean (--force discards
                                               changes); the branch is always kept
Workers are addressed by window name. Session: $FLEET_SESSION, else the fleet session you are in, else the only one.
H
  ;;
esac
