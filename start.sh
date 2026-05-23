#!/bin/bash
cd "$(dirname "$0")"

# Kill any stale Flask
pkill -f main.py 2>/dev/null

# Start Flask backend
.venv/bin/python3 main.py &
FLASK_PID=$!

# Start Vite dev server
cd ui-tauri
npm run dev &
VITE_PID=$!

echo "Flask PID: $FLASK_PID"
echo "Vite PID:  $VITE_PID"
echo ""
echo "Open: http://localhost:1420"
echo ""
echo "Press Ctrl+C to stop everything"

trap "kill $FLASK_PID $VITE_PID 2>/dev/null" EXIT
wait
