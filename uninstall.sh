#!/usr/bin/env bash
# codex-intercom uninstaller: removes our Codex hooks (hooks.json is backed up first), the hook script
# in ~/.codex/hooks/ and the Claude Code skill. Other hooks in hooks.json are left untouched.
#
#   ./uninstall.sh            keep the data dir (~/.codex/intercom: event logs, ownership records)
#   ./uninstall.sh --purge    also delete the data dir
#
# Installed as a Claude Code plugin? Run this for the Codex side, then `/plugin uninstall codex-intercom`.
set -euo pipefail

SKILLS_DIR="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
DEST="$SKILLS_DIR/codex-intercom"
DATA="${CODEX_INTERCOM_DIR:-$HOME/.codex/intercom}"
PURGE=0

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) die "unknown option: $arg" ;;
  esac
done

PY="${CODEX_INTERCOM_PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x /usr/bin/python3 ]; then PY=/usr/bin/python3; else PY="$(command -v python3 || true)"; fi
fi
[ -n "$PY" ] || die "python3 not found"

# 1. Codex hooks + hook script
SCRIPT="$DEST/scripts/codex_intercom.py"
[ -f "$SCRIPT" ] || SCRIPT="$HOME/.codex/hooks/codex_intercom.py"
if [ -f "$SCRIPT" ]; then
  say "codex hooks:"
  "$PY" "$SCRIPT" uninstall --remove-script
else
  say "codex hooks: codex_intercom.py not found; nothing to unregister"
fi

# 2. The skill
if [ -L "$DEST" ]; then
  rm "$DEST"
  say "skill: removed symlink $DEST"
elif [ -d "$DEST" ]; then
  rm -rf "$DEST"
  say "skill: removed $DEST"
else
  say "skill: not installed at $DEST"
fi

# 3. Data
if [ "$PURGE" = 1 ]; then
  rm -rf "$DATA"
  say "data: removed $DATA"
else
  say "data: kept $DATA (run with --purge to delete it)"
fi
