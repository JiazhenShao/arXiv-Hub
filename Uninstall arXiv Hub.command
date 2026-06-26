#!/bin/zsh
set -euo pipefail

APP_SUPPORT="$HOME/Library/Application Support/arXiv Hub"

print "This removes the arXiv Hub application and private Python runtime."
print "Reports and downloaded papers in Documents are preserved."
read "answer?Continue? [y/N] "
if [[ "${answer:l}" != "y" ]]; then
  print "Cancelled."
  exit 0
fi

if [[ -f "$APP_SUPPORT/profile.toml" ]]; then
  mkdir -p "$HOME/Documents/arXiv Hub"
  cp "$APP_SUPPORT/profile.toml" \
    "$HOME/Documents/arXiv Hub/profile.toml.backup"
fi
rm -rf "$APP_SUPPORT"
rm -f "$HOME/Applications/ArXiv Go.command"
rm -f "$HOME/Applications/arXiv Hub.command"
rm -f "$HOME/Applications/Configure arXiv Hub.command"
print "arXiv Hub was removed. Your reports and papers were not deleted."
read -k 1 "?Press any key to close."
