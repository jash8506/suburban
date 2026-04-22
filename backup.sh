#!/bin/sh
set -eu
cd /home/john/Desktop/suburban
git add log/
if git diff --cached --quiet; then
    echo "nothing to back up"
    exit 0
fi
git commit -m "log backup $(date -Iseconds)"
git push
