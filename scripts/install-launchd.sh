#!/bin/bash
# macOS の launchd に登録して、毎日 1 回 backup.py を動かす。
# データや API キーには触らない。時刻は環境変数で変えられる:
#   MYMIND_ARCHIVE_HOUR=4 MYMIND_ARCHIVE_MINUTE=20 ./scripts/install-launchd.sh
set -euo pipefail

LABEL="local.mymind-archive"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/mymind-archive"
HOUR="${MYMIND_ARCHIVE_HOUR:-4}"
MINUTE="${MYMIND_ARCHIVE_MINUTE:-20}"

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for c in /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if [ -x "$c" ]; then PY="$c"; break; fi
  done
fi
if [ -z "$PY" ]; then
  echo "python3 が見つかりません。PYTHON=/path/to/python3 を付けて実行してください" >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$REPO/backup.py</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>$HOUR</integer>
    <key>Minute</key><integer>$MINUTE</integer>
  </dict>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONUNBUFFERED</key><string>1</string>
    <key>PYTHONUTF8</key><string>1</string>
  </dict>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$LOG_DIR/backup.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/backup.log</string>
</dict>
</plist>
PLIST_EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

printf '登録しました: 毎日 %02d:%02d に実行\n' "$HOUR" "$MINUTE"
echo "  スクリプト: $REPO/backup.py"
echo "  ログ:       $LOG_DIR/backup.log"
echo "いますぐ 1 回動かす:  launchctl kickstart gui/$(id -u)/$LABEL"
