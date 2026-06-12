#!/bin/zsh
set -euo pipefail

APP_SUPPORT="$HOME/Library/Application Support/arXiv Hub"
APP_DIR="$APP_SUPPORT/app"
PYTHON="$APP_SUPPORT/venv/bin/python"
PROFILE="$APP_SUPPORT/profile.toml"

if [[ ! -x "$PYTHON" || ! -f "$APP_DIR/scripts/launch_viewer.py" ]]; then
  print -u2 "arXiv Hub is not installed. Run Install arXiv Hub.command first."
  read -k 1 "?Press any key to close."
  exit 2
fi

if [[ ! -f "$PROFILE" ]]; then
  "$PYTHON" "$APP_DIR/scripts/setup_profile.py" --profile "$PROFILE"
fi

exec "$PYTHON" "$APP_DIR/scripts/launch_viewer.py" --profile "$PROFILE"
