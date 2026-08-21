#!/usr/bin/env bash
# Drive tail-cw in a real PTY via tmux and capture frames.
#   drive.sh start <session> <cols>x<rows> <args...>
#   drive.sh keys  <session> <key>...
#   drive.sh cap   <session> [label]
#   drive.sh stop  <session>
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/tui-eval/frames"
cmd=${1:?}; sess=${2:?}; shift 2
mkdir -p "$OUT"
case "$cmd" in
  start)
    geom=${1:?}; shift
    cols=${geom%x*}; rows=${geom#*x}
    tmux kill-session -t "$sess" 2>/dev/null || true
    tmux new-session -d -s "$sess" -x "$cols" -y "$rows" \
      "cd '$ROOT' && TERM=xterm-256color uv run tail-cw $* 2>tui-eval/frames/$sess.stderr; echo EXIT=\$?; sleep 600"
    ;;
  keys) tmux send-keys -t "$sess" "$@" ;;
  cap)
    label=${1:-frame}
    tmux capture-pane -p -t "$sess" > "$OUT/$sess-$label.txt"
    cat "$OUT/$sess-$label.txt"
    ;;
  stop) tmux kill-session -t "$sess" 2>/dev/null || true ;;
  *) echo "unknown: $cmd" >&2; exit 2 ;;
esac
