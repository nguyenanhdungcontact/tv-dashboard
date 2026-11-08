#!/bin/bash
# Chạy soạn cụm từ trên máy Mac (dùng khi YouTube chặn IP của GitHub Actions).
# Khóa API chỉ nhập lúc chạy, không lưu vào file nào.
set -e
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "Tạo môi trường Python lần đầu..."
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q "youtube-transcript-api>=1.2,<2" requests

PROVIDER="${AI_PROVIDER:-gemini}"
if [ "$PROVIDER" = "claude" ]; then
  [ -z "$ANTHROPIC_API_KEY" ] && read -rsp "Dán khóa Claude (ANTHROPIC_API_KEY) rồi Enter: " ANTHROPIC_API_KEY && echo
  export ANTHROPIC_API_KEY
else
  [ -z "$GEMINI_API_KEY" ] && read -rsp "Dán khóa Gemini (GEMINI_API_KEY) rồi Enter: " GEMINI_API_KEY && echo
  export GEMINI_API_KEY
fi
export AI_PROVIDER="$PROVIDER"

git pull --quiet
python -u scripts/video_phrases.py || true

git add docs/video-data
if git diff --cached --quiet; then
  echo "Không có gì mới để lưu."
else
  git commit -m "Cập nhật cụm từ video (chạy trên máy) $(date +%Y-%m-%d)"
  git push
  echo "Đã đẩy kết quả lên GitHub."
fi
