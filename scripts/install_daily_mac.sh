#!/bin/bash
# Cài lịch chạy tự động mỗi sáng trên máy Mac (launchd): tìm video mới, soạn cụm từ, đẩy lên GitHub.
# Vì sao chạy trên Mac: YouTube chặn việc lấy phụ đề từ máy chủ của GitHub, còn mạng nhà thì không.
# Nếu lúc đó Mac đang ngủ, macOS sẽ chạy bù ngay khi Mac thức dậy.
#
#   ./scripts/install_daily_mac.sh            cài (chạy lúc 6:30 mỗi ngày)
#   ./scripts/install_daily_mac.sh 21:15      cài, chạy lúc 21:15
#   ./scripts/install_daily_mac.sh --remove   gỡ lịch
#   Nhật ký:  ~/Library/Logs/tv-video-daily.log
set -e
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.tvdashboard.video-daily"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/tv-video-daily.log"

if [ "$1" = "--remove" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Đã gỡ lịch chạy tự động."
  exit 0
fi

if [ ! -f "$REPO/.env.local" ]; then
  echo "Chưa có file .env.local. Tạo file $REPO/.env.local với nội dung:"
  echo "  YOUTUBE_API_KEY=..."
  echo "  GEMINI_API_KEY=..."
  echo "rồi chạy lại lệnh này."
  exit 1
fi
chmod 600 "$REPO/.env.local"

TIME="${1:-06:30}"
HOUR=$((10#${TIME%%:*})); MIN=$((10#${TIME##*:}))

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$REPO/scripts/run_local.sh</string></array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MIN</integer></dict>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
printf "Đã cài: chạy lúc %02d:%02d mỗi ngày.\nNhật ký: %s\nChạy thử ngay: launchctl kickstart gui/%s/%s\n" "$HOUR" "$MIN" "$LOG" "$(id -u)" "$LABEL"
