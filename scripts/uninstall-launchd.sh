#!/bin/bash
# launchd の登録を外す。データ・設定・API キーはそのまま残る。
set -euo pipefail
LABEL="local.mymind-archive"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "登録を外しました (データはそのままです)"
