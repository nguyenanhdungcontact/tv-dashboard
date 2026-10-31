# TV Dashboard

Dashboard “mưa sao băng từ vựng” + bản tin, chạy toàn màn hình trên Google TV.

- `docs/index.html` – trang dashboard (được host bằng GitHub Pages)
- `docs/brief.json` – nội dung bản tin; sửa file này để cập nhật tin, TV tự lấy tin mới mỗi 30 phút
- `app/` – app Android TV (WebView toàn màn hình), được GitHub Actions build thành APK

## Phím remote trong app
- ← vào bản tin, ↑ ↓ chuyển tin, → quay lại sao băng
- OK xem từ vựng, Space/Play-Pause tạm dừng, ↑ ↓ (ở phần sao băng) chỉnh tốc độ
- Back đóng thẻ đang mở; nút Home để thoát app
