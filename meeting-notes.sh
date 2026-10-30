#!/bin/bash
# Launcher: run the TUI from the repository root, so relative paths such as
# recordings/ always resolve the same way. Safe to symlink into ~/.local/bin.
REPO_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
cd "$REPO_DIR" || exit 1
exec venv/bin/python run.py "$@"
