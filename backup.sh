#!/bin/sh
set -eu
cd /home/john/Desktop/suburban
python3 backfill_utility.py
python3 energy.py
git add log/ daily.json
if git diff --cached --quiet; then
    echo "nothing to back up"
    exit 0
fi
git commit -m "log backup $(date -Iseconds)"
git push
