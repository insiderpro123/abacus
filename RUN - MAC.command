#!/bin/bash
# ---- Abacus Work Package Tracker launcher (macOS) ----
# Most reliable way to run: open Terminal, type "bash " (with a space),
# drag this file into the window, and press Return. See SETUP-MAC.txt.
# To make it double-clickable instead: right-click -> Open once
# (Gatekeeper), or run  chmod +x "RUN - MAC.command"  in Terminal.

cd "$(dirname "$0")"

# Find a Python 3 interpreter
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "Python 3 is not installed."
  echo "Install it from https://www.python.org/downloads/ (or run: brew install python)"
  echo "then double-click this file again."
  read -n 1 -s -r -p "Press any key to close..."
  exit 1
fi

echo "Starting Abacus Work Package Tracker..."
echo "Your browser will open automatically at http://127.0.0.1:5010"
echo "(Close this window or press Ctrl+C to stop)"

# Install dependencies (quiet; first run may take a moment)
"$PY" -m pip install --user -r requirements.txt >/dev/null 2>&1

"$PY" app.py
