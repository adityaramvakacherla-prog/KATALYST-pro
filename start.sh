#!/bin/bash
# ╔══════════════════════════════════════╗
# ║  KATALYST — Start Script             ║
# ╚══════════════════════════════════════╝

cd ~/codespace/KATALYST
source venv/bin/activate

# ── Create required folders ───────────────────────
mkdir -p logs memory output

# ── Kill any old instances ────────────────────────
pkill -f "server.py" 2>/dev/null
pkill -f "ngrok" 2>/dev/null
sleep 1

echo ""
echo "╔══════════════════════════════════════╗"
echo "║  ⚡ KATALYST — Starting Up           ║"
echo "╚══════════════════════════════════════╝"
echo ""

# ── Start Flask server in background ─────────────
python3 server.py &
SERVER_PID=$!
echo "  [✓] Flask server started (PID: $SERVER_PID)"
sleep 2

# ── Start ngrok for public access ────────────────
# Requires: pip install ngrok  OR  brew install ngrok
# Sign up free at ngrok.com and run: ngrok config add-authtoken YOUR_TOKEN

if command -v ngrok &> /dev/null; then
    ngrok http 5000 --log=stdout > logs/ngrok.log 2>&1 &
    NGROK_PID=$!
    echo "  [✓] ngrok started (PID: $NGROK_PID)"
    sleep 3

    # Extract the public URL from ngrok log
    NGROK_URL=$(grep -o 'https://[a-z0-9-]*\.ngrok-free\.app' logs/ngrok.log | head -1)
    if [ -z "$NGROK_URL" ]; then
        # Try the API instead
        NGROK_URL=$(curl -s http://localhost:4040/api/tunnels 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['tunnels'][0]['public_url'])" 2>/dev/null)
    fi

    echo ""
    echo "  ┌─────────────────────────────────────┐"
    echo "  │  LOCAL:   http://localhost:5000       │"
    if [ -n "$NGROK_URL" ]; then
        echo "  │  PUBLIC:  $NGROK_URL     │"
    else
        echo "  │  PUBLIC:  check http://localhost:4040│"
    fi
    echo "  └─────────────────────────────────────┘"
else
    # Get local network IP
    LOCAL_IP=$(hostname -I | awk '{print $1}' 2>/dev/null || ipconfig getifaddr en0 2>/dev/null || echo "unknown")
    echo ""
    echo "  ┌─────────────────────────────────────┐"
    echo "  │  LOCAL:   http://localhost:5000       │"
    echo "  │  NETWORK: http://$LOCAL_IP:5000    │"
    echo "  │                                      │"
    echo "  │  ngrok not found. For public access: │"
    echo "  │  pip install ngrok                   │"
    echo "  │  ngrok config add-authtoken <token>  │"
    echo "  │  Then re-run this script             │"
    echo "  └─────────────────────────────────────┘"
fi

echo ""
echo "  Press Ctrl+C to stop everything"
echo ""

# ── Open browser ──────────────────────────────────
sleep 1
if command -v xdg-open &> /dev/null; then
    xdg-open http://localhost:5000 2>/dev/null &
elif command -v open &> /dev/null; then
    open http://localhost:5000 2>/dev/null &
fi

# ── Wait and cleanup on Ctrl+C ───────────────────
trap 'echo ""; echo "  Shutting down..."; kill $SERVER_PID $NGROK_PID 2>/dev/null; exit 0' INT TERM

wait $SERVER_PID
