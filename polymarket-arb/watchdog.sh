#!/usr/bin/env bash
# watchdog.sh — Keeps the arb bot alive. Restarts on crash.
# Usage: nohup bash watchdog.sh > watchdog.log 2>&1 &

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "[watchdog] Starting bot supervisor at $(date)"

while true; do
    # Don't restart if STOP file exists (intentional shutdown)
    if [ -f STOP ]; then
        echo "[watchdog] STOP file found — not restarting. Remove STOP to resume."
        sleep 10
        continue
    fi

    echo "[watchdog] Starting bot at $(date)"
    python main.py >> bot.log 2>&1
    EXIT_CODE=$?
    echo "[watchdog] Bot exited with code $EXIT_CODE at $(date)"

    # If STOP file appeared during run, don't restart
    if [ -f STOP ]; then
        echo "[watchdog] STOP file present — halting watchdog."
        break
    fi

    echo "[watchdog] Restarting in 3 seconds..."
    sleep 3
done

echo "[watchdog] Watchdog stopped at $(date)"
