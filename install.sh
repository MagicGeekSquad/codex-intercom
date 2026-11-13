#!/usr/bin/env bash
# codex-intercom installer: installs the Claude Code skill and registers the Codex hooks.
#
#   From a clone:  ./install.sh
#   One-liner:     curl -fsSL https://raw.githubusercontent.com/MagicGeekSquad/codex-intercom/main/install.sh | bash
#
# Environment (all optional):
#   CODEX_INTERCOM_REF     branch or tag to download when not run from a clone (default: main)
#   CLAUDE_SKILLS_DIR      where Claude Code skills live (default: ~/.claude/skills)
#   CODEX_INTERCOM_PYTHON  interpreter written into Codex's hooks.json (default: /usr/bin/python3,
#                          else python3 on PATH). If you set it, keep it set in your shell profile.
#
# Re-running it updates an existing install. Your hooks.json is backed up before it is changed.
set -euo pipefail

REPO="MagicGeekSquad/codex-intercom"
REF="${CODEX_INTERCOM_REF:-main}"
SKILLS_DIR="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
DEST="$SKILLS_DIR/codex-intercom"

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# 1. Python 3.9+ (standard library only)
PY="${CODEX_INTERCOM_PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x /usr/bin/python3 ]; then PY=/usr/bin/python3; else PY="$(command -v python3 || true)"; fi
fi
[ -n "$PY" ] || die "python3 not found (codex-intercom needs Python 3.9+)"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "$PY is older than Python 3.9"

# 2. Codex CLI (only warn: the hooks can be registered before Codex is installed)
if ! command -v codex >/dev/null 2>&1; then
  say "warning: 'codex' is not on PATH. Install the Codex CLI (npm i -g @openai/codex) before using codex-intercom."
fi

# 3. Source: this checkout, or a downloaded copy of the repo
SRC=""
SELF="${BASH_SOURCE[0]:-}"
if [ -n "$SELF" ] && [ -f "$SELF" ]; then
  HERE="$(cd "$(dirname "$SELF")" && pwd)"
  [ -f "$HERE/skills/codex-intercom/SKILL.md" ] && SRC="$HERE/skills/codex-intercom"
fi
if [ -z "$SRC" ]; then
  command -v curl >/dev/null 2>&1 || die "curl is required"
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  say "downloading $REPO@$REF ..."
  curl -fsSL "https://github.com/$REPO/archive/$REF.tar.gz" | tar -xz -C "$TMP" \
    || die "download failed (check CODEX_INTERCOM_REF=$REF)"
  SRC="$(find "$TMP" -mindepth 3 -maxdepth 3 -type d -path '*/skills/codex-intercom' | head -n 1)"
  [ -n "$SRC" ] && [ -f "$SRC/SKILL.md" ] || die "downloaded archive has no skills/codex-intercom"
fi

# 4. Install the skill
mkdir -p "$SKILLS_DIR"
if [ -L "$DEST" ]; then
  say "skill: $DEST is a symlink; leaving it alone"
else
  STAGE="$DEST.new.$$"
  rm -rf "$STAGE"
  cp -R "$SRC" "$STAGE"
  rm -rf "$DEST"
  mv "$STAGE" "$DEST"
  say "skill: installed to $DEST"
fi
chmod +x "$DEST/scripts/codex_intercom.py"

# 5. Copy the hook script to ~/.codex/hooks/ and register the hooks in ~/.codex/hooks.json
say "codex hooks:"
"$PY" "$DEST/scripts/codex_intercom.py" install

cat <<EOF

Done. Next:
  1. Trust the new hooks in Codex once:
       Codex App:  Settings > Hooks
       Codex CLI:  it asks "Hooks need review" at startup (or use /hooks)
     Re-running this installer only asks again if the hook commands changed.
  2. Start a new Claude Code session (skills are loaded when a session starts).
  3. Check the setup any time:
       $PY $DEST/scripts/codex_intercom.py doctor
  4. Try it: give Claude Code a codex://threads/<id> link and ask it to send Codex a message
     and tell you when Codex is done.
EOF
