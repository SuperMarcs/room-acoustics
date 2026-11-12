#!/bin/zsh
cd -- "$(dirname -- "$0")" || exit 1
if [[ ! -x .venv/bin/python ]]; then
  echo "Please follow the virtual-environment setup in README.md first."
  read -r "?Press Return to close."
  exit 1
fi
exec .venv/bin/python room_gui_connected.py
