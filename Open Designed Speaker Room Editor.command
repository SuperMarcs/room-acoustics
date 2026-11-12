#!/bin/zsh
cd -- "${0:A:h}" || exit 1
if [[ ! -x .venv/bin/python ]]; then
  print "Please follow the virtual-environment setup in README.md first."
  read -r '?Press Return to close.'
  exit 1
fi
exec .venv/bin/python room_gui_designed_speakers.py
