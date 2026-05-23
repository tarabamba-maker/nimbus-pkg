#!/bin/bash
source "$HOME/.cargo/env"
cd "$(dirname "$0")"
pkill -f main.py 2>/dev/null
sleep 0.3
.venv/bin/python3 main.py &
cd ui-tauri && npm run tauri dev
