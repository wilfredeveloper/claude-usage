#!/usr/bin/env bash
# Set up claude-usage on this machine: name it, point it at the hub, push every
# 30 minutes, add the /usage-report command to Claude Code, then show the numbers.
#
#   ./setup.sh                       asks for a name and the hub
#   ./setup.sh "Work laptop" mypa    no questions ("none" as the hub for local only)
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3)"

NAME="${1:-}"
HUB="${2:-}"
if [ -z "$NAME" ]; then
  read -rp "Name for this machine [$(hostname)]: " NAME
  NAME="${NAME:-$(hostname)}"
fi
if [ -z "$HUB" ]; then
  read -rp "Hub SSH target, or none [mypa]: " HUB
  HUB="${HUB:-mypa}"
fi
[ "$HUB" = none ] && HUB=""

"$PY" -I "$REPO/claude_usage.py" config --name "$NAME" --hub "$HUB" >/dev/null

if [ -n "$HUB" ]; then
  if ssh -o BatchMode=yes -o ConnectTimeout=10 "$HUB" true 2>/dev/null; then
    LINE="*/30 * * * * \"$PY\" -I \"$REPO/claude_usage.py\" collect --push-to-hub >> \"$REPO/cron.log\" 2>&1  # claude usage report"
    ( crontab -l 2>/dev/null | grep -v '# claude usage report' || true; echo "$LINE" ) | crontab -
    echo "Scheduled: this machine pushes its usage to $HUB every 30 minutes."
  else
    echo "Could not reach $HUB over SSH without a password, so no schedule was added."
    echo "Fix SSH access (ssh $HUB should work without prompting) and run ./setup.sh again."
  fi
fi

if [ -d "$HOME/.claude" ]; then
  mkdir -p "$HOME/.claude/commands"
  sed "s|__REPO__|$REPO|g" "$REPO/commands/usage-report.md" > "$HOME/.claude/commands/usage-report.md"
  echo "Added the /usage-report command to Claude Code."
fi

echo
"$PY" -I "$REPO/claude_usage.py"
