#!/bin/zsh
set -euo pipefail

APP_SUPPORT="$HOME/Library/Application Support/arXiv Hub"
APP_DIR="$APP_SUPPORT/app"
VENV_DIR="$APP_SUPPORT/venv"
UV_DIR="$APP_SUPPORT/bin"
UV_PYTHON_INSTALL_DIR="$APP_SUPPORT/python"
PROFILE_PATH="$APP_SUPPORT/profile.toml"
LAUNCHER_DIR="$HOME/Applications"
LAUNCHER_PATH="$LAUNCHER_DIR/arXiv Hub.command"
CONFIGURE_PATH="$LAUNCHER_DIR/Configure arXiv Hub.command"
SOURCE_DIR="${0:A:h}"
UV_VERSION="0.11.21"

export UV_INSTALL_DIR="$UV_DIR"
export UV_PYTHON_INSTALL_DIR
export UV_NO_MODIFY_PATH=1

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  print -u2 "arXiv Hub currently supports Apple Silicon Macs only."
  read -k 1 "?Press any key to close."
  exit 2
fi

print "Installing arXiv Hub..."
mkdir -p "$APP_SUPPORT" "$LAUNCHER_DIR"

if [[ "${ARXIV_HUB_INSTALL_DRY_RUN:-0}" == "1" ]]; then
  print "Dry run: would install into $APP_SUPPORT"
  exit 0
fi

TEMP_ROOT="$(mktemp -d "$APP_SUPPORT/.install.XXXXXX")"
SWITCH_STARTED=0
SWITCH_COMPLETE=0
cleanup() {
  if [[ "$SWITCH_STARTED" == "1" && "$SWITCH_COMPLETE" != "1" ]]; then
    rm -rf "$APP_DIR" "$VENV_DIR"
    if [[ -d "$APP_DIR.previous" ]]; then
      mv "$APP_DIR.previous" "$APP_DIR"
    fi
    if [[ -d "$VENV_DIR.previous" ]]; then
      mv "$VENV_DIR.previous" "$VENV_DIR"
    fi
  fi
  rm -rf "$TEMP_ROOT"
}
trap cleanup EXIT

if [[ ! -x "$UV_DIR/uv" ]]; then
  print "Downloading pinned uv $UV_VERSION..."
  curl --proto '=https' --tlsv1.2 -LsSf \
    "https://astral.sh/uv/$UV_VERSION/install.sh" \
    -o "$TEMP_ROOT/install-uv.sh"
  /bin/sh "$TEMP_ROOT/install-uv.sh"
fi

UV="$UV_DIR/uv"
print "Installing managed Python 3.12..."
"$UV" python install 3.12

print "Updating application files..."
NEW_APP="$TEMP_ROOT/app"
NEW_VENV="$TEMP_ROOT/venv"
mkdir -p "$NEW_APP"
for item in arxiv_daily scripts config viewer-assets requirements.lock pyproject.toml; do
  ditto "$SOURCE_DIR/$item" "$NEW_APP/$item"
done

print "Creating the private environment..."
"$UV" venv --python 3.12 "$NEW_VENV"
"$UV" pip sync --python "$NEW_VENV/bin/python" "$NEW_APP/requirements.lock"

if [[ ! -f "$PROFILE_PATH" ]]; then
  print "Opening the setup wizard in your default browser..."
  "$NEW_VENV/bin/python" "$NEW_APP/scripts/setup_profile.py" \
    --profile "$PROFILE_PATH"
fi

print "Downloading and verifying the pinned SPECTER2 model..."
"$NEW_VENV/bin/python" "$NEW_APP/scripts/verify_model.py" \
  --profile "$PROFILE_PATH"

SWITCH_STARTED=1
rm -rf "$APP_DIR.previous" "$VENV_DIR.previous"
if [[ -d "$APP_DIR" ]]; then
  mv "$APP_DIR" "$APP_DIR.previous"
fi
if [[ -d "$VENV_DIR" ]]; then
  mv "$VENV_DIR" "$VENV_DIR.previous"
fi
mv "$NEW_APP" "$APP_DIR"
mv "$NEW_VENV" "$VENV_DIR"
SWITCH_COMPLETE=1

ditto "$SOURCE_DIR/launcher/arXiv Hub.command" "$LAUNCHER_PATH"
ditto "$SOURCE_DIR/launcher/Configure arXiv Hub.command" "$CONFIGURE_PATH"
chmod 755 "$LAUNCHER_PATH"
chmod 755 "$CONFIGURE_PATH"

rm -rf "$APP_DIR.previous" "$VENV_DIR.previous"
print ""
print "arXiv Hub is ready."
print "Open Spotlight and type: arXiv Hub"
read -k 1 "?Press any key to close."
