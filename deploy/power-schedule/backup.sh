#!/usr/bin/env bash
# Daily backup of the power schedule bot to GitHub.
set -euo pipefail

REPO_DIR="/home/u0_a566/bot_skaner_saitov"
cd "$REPO_DIR"

git add power_schedule.py deploy/power-schedule .gitignore

if git diff --cached --quiet; then
    echo "No changes to back up."
    exit 0
fi

git commit -m "auto: backup power schedule bot $(date +%F)"
git push origin master
echo "Backup pushed."
