"""
Tạo danh sách video tiếng Anh theo chủ đề cho trang video (docs/video-data/catalog.json).

Với mỗi chủ đề trong video/english.json:
  1. Đọc RSS của từng kênh (15 video mới nhất), bỏ Shorts.
  2. Lần lượt xen kẽ các kênh, kiểm tra từng video mới:
     - nhúng được trên trang khác (oEmbed trả 200)
     - có phụ đề tiếng Anh (của kênh hoặc tự động theo giọng gốc tiếng Anh)
     - dài trong khoảng min_minutes..max_minutes (tính theo phụ đề)
  3. Giữ lại video cũ đã đạt, thêm video mới, mỗi hàng tối đa per_row video, mới nhất trước.

Kết quả kiểm tra lưu ở .cache/catalog_checks.json (không commit) để lần sau khỏi kiểm tra lại.
Không cần khóa API. Chạy trên máy Mac (YouTube hay chặn IP của GitHub Actions).
"""
import json
import pathlib
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import video_phrases as vp  # noqa: E402  (dùng chung hàm lấy phụ đề + bộ nhớ đệm)

ROOT = vp.ROOT
LIST_FILE = ROOT / "video" / "english.json"
OUT_FILE = ROOT / "docs" / "video-data" / "catalog.json"
CHECKS_FILE = ROOT / ".cache" / "catalog_checks.json"
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
UA = {"User-Agent": "Mozilla/5.0 (tv-dashboard catalog)", "Accept-Language": "en"}


def fmt_dur(sec: float) -> str:
    s = int(round(sec))
    h, m, x = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{x:02d}" if h else f"{m}:{x:02d}"


AGO = {"second": 1 / 86400, "minute": 1 / 1440, "hour": 1 / 24, "day": 1, "week": 7, "month": 30, "year": 365}


def read_channel_page(handle: str, channel_id: str) -> list:
    """Dự phòng khi RSS của YouTube lỗi: đọc tab Videos của kênh (không gồm Shorts)."""
    url = f"https://www.youtube.com/channel/{channel_id}/videos"
    try:
        r = requests.get(url, headers=UA, timeout=20)
        r.raise_for_status()
        m = re.search(r"var ytInitialData = (\{.*?\});</script>", r.text, re.S)
        data = json.loads(m.group(1)) if m else {}
    except Exception as e:
        print(f"  ! Không đọc được trang kênh @{handle}: {type(e).__name__}")
        return []
    lockups = []

    def walk(o):
        if isinstance(o, dict):
            if "lockupViewModel" in o:
                lockups.append(o["lockupViewModel"])
            for x in o.values():
                walk(x)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(data)
    name = (((data.get("metadata") or {}).get("channelMetadataRenderer") or {}).get("title") or handle).strip()

    now = datetime.now(timezone.utc)
    out = []
    for L in lockups:
        if L.get("contentType") != "LOCKUP_CONTENT_TYPE_VIDEO" or not L.get("contentId"):
            continue
        meta = (L.get("metadata") or {}).get("lockupMetadataViewModel") or {}
        title = ((meta.get("title") or {}).get("content") or "").strip()
        days = None
        for row in (((meta.get("metadata") or {}).get("contentMetadataViewModel") or {}).get("metadataRows") or []):
            for part in row.get("metadataParts") or []:
                mm = re.match(r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", part.get("accessibilityLabel") or "")
                if mm:
                    days = int(mm.group(1)) * AGO[mm.group(2)]
        published = (now - timedelta(days=days)).isoformat() if days is not None else now.isoformat()
        out.append({"id": L["contentId"], "title": title, "channel": name, "published": published})
    return out


def read_feed(handle: str, channel_id: str) -> list:
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        r = requests.get(url, headers=UA, timeout=20)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        print(f"  ! RSS của @{handle} lỗi ({type(e).__name__}), đọc trang kênh thay thế")
        return read_channel_page(handle, channel_id)
    out = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", default="", namespaces=NS)
        link = (e.find("a:link", NS).get("href") if e.find("a:link", NS) is not None else "")
        if not vid or "/shorts/" in link:
            continue
        out.append({
            "id": vid,
            "title": (e.findtext("a:title", default="", namespaces=NS) or "").strip(),
            "channel": (e.findtext("a:author/a:name", default="", namespaces=NS) or handle).strip(),
            "published": e.findtext("a:published", default="", namespaces=NS),
        })
    out.sort(key=lambda v: v["published"], reverse=True)
    return out


def embeddable(vid: str) -> bool:
    try:
        r = requests.get("https://www.youtube.com/oembed",
                         params={"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"},
                         headers=UA, timeout=15)
        return r.status_code == 200
    except Exception:
        return False


def check(vid: str) -> dict:
    """Trả về {'ok': bool, 'why': str, 'sec': float, 'kind': str}."""
    if not embeddable(vid):
        return {"ok": False, "why": "not_embeddable"}
    try:
        tr = vp.get_transcript(vid)
    except vp.NoEnglish:
        return {"ok": False, "why": "no_english_captions"}
    segs = tr["segments"]
    if len(segs) < 20:
        return {"ok": False, "why": "too_little_speech"}
    last = segs[-1]
    sec = float(last["start"]) + float(last.get("duration") or 0)
    return {"ok": True, "why": "", "sec": round(sec, 1), "kind": tr["kind"]}


def round_robin(feeds: list) -> list:
    out, i = [], 0
    while any(i < len(f) for f in feeds):
        for f in feeds:
            if i < len(f):
                out.append(f[i])
        i += 1
    return out


def main() -> int:
    cfg = json.loads(LIST_FILE.read_text(encoding="utf-8"))
    st = {"per_row": 10, "min_minutes": 8, "max_minutes": 25, "max_age_days": 730, "checks_per_topic": 6}
    st.update(cfg.get("settings") or {})

    checks = {}
    if CHECKS_FILE.exists():
        try:
            checks = json.loads(CHECKS_FILE.read_text(encoding="utf-8"))
        except Exception:
            checks = {}
    old_rows = {}
    if OUT_FILE.exists():
        try:
            old_rows = {r["key"]: r for r in json.loads(OUT_FILE.read_text(encoding="utf-8")).get("rows", [])}
        except Exception:
            old_rows = {}

    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(st["max_age_days"]))).isoformat()
    rows, blocked = [], False

    for topic in cfg.get("topics", []):
        key, title = topic["key"], topic["title"]
        lo = float(topic.get("min_minutes", st["min_minutes"])) * 60
        hi = float(topic.get("max_minutes", st["max_minutes"])) * 60
        print(f"[{title}]", flush=True)

        kept = {v["id"]: v for v in old_rows.get(key, {}).get("items", [])}
        feeds = [read_feed(h, cid) for h, cid in (topic.get("channels") or {}).items()]
        budget = int(st["checks_per_topic"])
        added = 0

        for v in round_robin(feeds):
            if blocked or v["id"] in kept or v["published"] < cutoff:
                continue
            c = checks.get(v["id"])
            if c is None:
                if budget <= 0:
                    continue
                budget -= 1
                try:
                    c = check(v["id"])
                except vp.Blocked as e:
                    print(f"  YouTube đang chặn IP: {e}. Dừng kiểm tra, giữ danh sách cũ.")
                    blocked = True
                    break
                except Exception as e:  # lỗi tạm thời: không lưu, lần sau thử lại
                    print(f"  ? {v['id']}: {type(e).__name__}")
                    continue
                checks[v["id"]] = c
                time.sleep(5)   # chậm lại để YouTube không chặn IP
            if not c.get("ok"):
                continue
            if not (lo <= c["sec"] <= hi):
                continue
            kept[v["id"]] = {
                "id": v["id"], "title": v["title"], "channel": v["channel"],
                "dur": fmt_dur(c["sec"]), "published": v["published"][:10], "captions": c["kind"],
            }
            added += 1
            print(f"  + {v['channel']}: {v['title']} ({fmt_dur(c['sec'])})", flush=True)

        items = sorted(kept.values(), key=lambda x: x.get("published", ""), reverse=True)[: int(st["per_row"])]
        print(f"  → {len(items)} video (mới thêm {added})", flush=True)
        if items:
            rows.append({"key": key, "title": title, "items": items})

    CHECKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CHECKS_FILE.write_text(json.dumps(checks, ensure_ascii=False), encoding="utf-8")
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps({"updated": time.strftime("%Y-%m-%d"), "rows": rows},
                                   ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Đã ghi {OUT_FILE.relative_to(ROOT)}: {sum(len(r['items']) for r in rows)} video trong {len(rows)} hàng.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
