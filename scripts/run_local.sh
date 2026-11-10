#!/bin/bash
# Chạy trên máy Mac: tìm video mới (YouTube Data API), lấy phụ đề, soạn cụm từ, rồi đẩy lên GitHub.
# Chạy tay:   ./scripts/run_local.sh
# Tự động:    ./scripts/install_daily_mac.sh  (mỗi sáng, xem file đó)
#
# Khóa API đọc từ file .env.local (đã nằm trong .gitignore, không bao giờ lên GitHub):
#   YOUTUBE_API_KEY=...
#   GEMINI_API_KEY=...
# Thiếu file thì khi chạy tay script sẽ hỏi khóa; khi chạy tự động thì báo lỗi rồi dừng.
set -e
cd "$(dirname "$0")/.."
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH"

if [ -f .env.local ]; then
  set -a; source .env.local; set +a
fi
INTERACTIVE=0; [ -t 0 ] && INTERACTIVE=1

if [ ! -d .venv ]; then
  echo "Tạo môi trường Python lần đầu..."
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q --disable-pip-version-check "youtube-transcript-api>=1.2,<2" requests 2>/dev/null

PROVIDER="${AI_PROVIDER:-gemini}"
ask() {  # ask TÊN_BIẾN "câu hỏi"
  if [ -z "${!1}" ] && [ "$INTERACTIVE" = 1 ]; then read -rsp "$2" "$1"; echo; fi
}
if [ "$PROVIDER" = "claude" ]; then
  ask ANTHROPIC_API_KEY "Dán khóa Claude (ANTHROPIC_API_KEY) rồi Enter: "
  [ -z "$ANTHROPIC_API_KEY" ] && { echo "Thiếu ANTHROPIC_API_KEY (thêm vào .env.local)."; exit 1; }
else
  ask GEMINI_API_KEY "Dán khóa Gemini (GEMINI_API_KEY) rồi Enter: "
  [ -z "$GEMINI_API_KEY" ] && { echo "Thiếu GEMINI_API_KEY (thêm vào .env.local)."; exit 1; }
fi
ask YOUTUBE_API_KEY "Dán khóa YouTube Data API (để trống rồi Enter nếu chưa có): "
export GEMINI_API_KEY ANTHROPIC_API_KEY YOUTUBE_API_KEY AI_PROVIDER="$PROVIDER"

echo "== $(date '+%d/%m %H:%M') Tìm video mới và soạn cụm từ =="
git pull --quiet --rebase --autostash || git pull --quiet
python -u scripts/video_daily.py || true

git add docs/video-data video/state.json
if git diff --cached --quiet; then
  echo "Không có gì mới để lưu."
else
  git commit -q -m "Video mới và cụm từ (chạy trên máy) $(date +%Y-%m-%d)"
  for i in 1 2 3; do
    git pull --quiet --rebase && git push --quiet && { echo "Đã đẩy kết quả lên GitHub."; exit 0; }
    sleep 10
  done
  echo "Chưa đẩy được lên GitHub, lần chạy sau sẽ đẩy tiếp."
fi
