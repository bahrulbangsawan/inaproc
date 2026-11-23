#!/usr/bin/env bash
# Install the `inaproc` skill.
#   ./install.sh claude     → Claude Code, all projects (~/.claude/skills/inaproc, or $CLAUDE_CONFIG_DIR/skills)
#   ./install.sh project    → Claude Code, this project only (./.claude/skills/inaproc)
#   ./install.sh codex      → Codex CLI (~/.codex/skills/inaproc + /prompts:inaproc)
#   ./install.sh zip        → dist/inaproc.zip for claude.ai / Claude Desktop upload
#   ./install.sh all        → claude + codex + zip
#   ./install.sh uninstall  → remove the Claude Code and Codex installs
set -euo pipefail

REPO="bahrulbangsawan/inaproc"
BRANCH="${INAPROC_BRANCH:-main}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || pwd)"
SRC="$HERE/skills/inaproc"

# Piped from curl (no local checkout)? Fetch the repository into a temp folder first.
if [ ! -f "$SRC/SKILL.md" ]; then
  command -v curl >/dev/null 2>&1 || { echo "curl is required for the one-line install"; exit 1; }
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  echo "Downloading $REPO ($BRANCH)…"
  curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH" | tar -xz -C "$TMP"
  HERE="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -1)"
  SRC="$HERE/skills/inaproc"
  DIST_DIR="$PWD/dist"
fi
DIST_DIR="${DIST_DIR:-$HERE/dist}"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"

say() { printf '\033[1m%s\033[0m\n' "$*"; }

check_python() {
  if command -v python3 >/dev/null 2>&1; then PY=python3
  elif command -v python >/dev/null 2>&1; then PY=python
  else echo "Python 3.8+ is required (https://www.python.org/downloads/)"; exit 1; fi
  "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' || { echo "Python 3.8+ is required"; exit 1; }
}

copy_skill() {  # $1 = destination folder
  mkdir -p "$(dirname "$1")"
  rm -rf "$1"
  cp -R "$SRC" "$1"
  rm -rf "$1/scripts/__pycache__"
  chmod +x "$1/scripts/inaproc.py"
}

smoke() {  # $1 = installed skill folder
  if "$PY" "$1/scripts/inaproc.py" ppn >/dev/null 2>&1; then say "  ✓ live check passed (katalog.inaproc.id reachable)"
  else echo "  ! installed, but the live check failed (offline or rate-limited?). Try: $PY $1/scripts/inaproc.py ppn"; fi
}

install_claude() {
  check_python
  copy_skill "$CLAUDE_DIR/skills/inaproc"
  say "Claude Code: installed to $CLAUDE_DIR/skills/inaproc"
  echo "  Use it with /inaproc <request> or just ask about INAPROC products."
  smoke "$CLAUDE_DIR/skills/inaproc"
}

install_project() {
  check_python
  copy_skill "$PWD/.claude/skills/inaproc"
  say "Claude Code (project): installed to $PWD/.claude/skills/inaproc"
  smoke "$PWD/.claude/skills/inaproc"
}

install_codex() {
  check_python
  copy_skill "$CODEX_DIR/skills/inaproc"
  mkdir -p "$CODEX_DIR/prompts"
  cp "$HERE/codex/prompts/inaproc.md" "$CODEX_DIR/prompts/inaproc.md"
  say "Codex: skill at $CODEX_DIR/skills/inaproc, prompt at $CODEX_DIR/prompts/inaproc.md"
  echo "  Use it with /prompts:inaproc <request> (or \$inaproc / just ask). Restart Codex to load it."
  smoke "$CODEX_DIR/skills/inaproc"
}

make_zip() {
  mkdir -p "$DIST_DIR"
  rm -f "$DIST_DIR/inaproc.zip"
  (cd "$HERE/skills" && zip -qr "$DIST_DIR/inaproc.zip" inaproc -x '*/__pycache__/*' '*.DS_Store' 'inaproc/scripts/queries.discovered.json')
  say "Zip: $DIST_DIR/inaproc.zip"
  echo "  claude.ai / Claude Desktop → Settings → Capabilities → Skills → Upload skill."
  echo "  Also enable code execution and allow network access to katalog.inaproc.id."
}

uninstall() {
  rm -rf "$CLAUDE_DIR/skills/inaproc" "$CODEX_DIR/skills/inaproc" "$CODEX_DIR/prompts/inaproc.md"
  say "Removed inaproc from Claude Code ($CLAUDE_DIR) and Codex ($CODEX_DIR)."
}

case "${1:-}" in
  claude) install_claude ;;
  project) install_project ;;
  codex) install_codex ;;
  zip) make_zip ;;
  all) install_claude; install_codex; make_zip ;;
  uninstall) uninstall ;;
  *) cat <<'USAGE'
Usage: install.sh <target>
  claude     Claude Code, all projects (~/.claude/skills/inaproc or $CLAUDE_CONFIG_DIR/skills)
  project    Claude Code, current project only (./.claude/skills/inaproc)
  codex      Codex CLI (~/.codex/skills/inaproc + /prompts:inaproc)
  zip        dist/inaproc.zip for claude.ai / Claude Desktop
  all        claude + codex + zip
  uninstall  remove the Claude Code and Codex installs
USAGE
     exit 1 ;;
esac
