"""
Mỗi ngày: tìm video tiếng Anh mới theo chủ đề trong video/sources.json, giữ lại video có phụ đề
tiếng Anh thật, soạn cụm từ, rồi THÊM vào docs/video-data/catalog.json. Không bao giờ xoá video cũ.

Quy trình:
  1. Bù cụm từ cho video đã có trong danh mục mà chưa có cụm từ (vài video mỗi lần).
  2. Tìm ứng viên bằng YouTube Data API: video mới của các kênh (danh sách tải lên, rất rẻ)
     + vài lệnh tìm kiếm theo chủ đề (xoay vòng mỗi ngày).
  3. Lọc theo thông tin video: cho nhúng, công khai, tiếng Anh, dài min..max phút, video ngang.
  4. Lần lượt lấy phụ đề + soạn cụm từ cho tới khi đủ target_per_day video ĐẠT trong ngày.
     Video không đạt được ghi vào video/state.json để không thử lại.
  5. Ghi danh mục, file phụ đề (captions/) và chỉ mục huy hiệu (phrases/index.json).

Biến môi trường:
  YOUTUBE_API_KEY    khóa YouTube Data API v3 (tìm video mới; thiếu thì chỉ làm bước 1)
  GEMINI_API_KEY / ANTHROPIC_API_KEY, AI_PROVIDER   giống scripts/video_phrases.py
"""
import datetime as dt
import json
import os
import pathlib
import re
import sys
import time

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import video_phrases as vp  # noqa: E402

ROOT = vp.ROOT
SOURCES = ROOT / "video" / "sources.json"
STATE = ROOT / "video" / "state.json"
CATALOG = ROOT / "docs" / "video-data" / "catalog.json"
YT_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
TODAY = dt.date.today().isoformat()
PAUSE = 4          # giây nghỉ giữa hai lần lấy phụ đề, để YouTube không chặn IP


class QuotaExceeded(Exception):
    pass


def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save(path, data, indent=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf-8")


# ------------------------------------------------------------------ YouTube Data API
def yt(path, **params):
    params["key"] = YT_KEY
    r = requests.get(f"https://www.googleapis.com/youtube/v3/{path}", params=params, timeout=30)
    if r.status_code == 403 and "quota" in r.text.lower():
        raise QuotaExceeded()
    if r.status_code != 200:
        raise RuntimeError(f"YouTube API {path} HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


def channel_uploads(channel_id, n):
    """Video mới nhất của kênh. Danh sách tải lên của kênh UCxxx là UUxxx (1 đơn vị hạn mức)."""
    data = yt("playlistItems", part="contentDetails", playlistId="UU" + channel_id[2:], maxResults=n)
    return [i["contentDetails"]["videoId"] for i in data.get("items", [])]


def search(query, days):
    after = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
    data = yt("search", part="id", type="video", q=query, maxResults=20, order="relevance",
              videoEmbeddable="true", videoCaption="closedCaption", videoDuration="medium",
              relevanceLanguage="en", safeSearch="strict", publishedAfter=after)
    return [i["id"]["videoId"] for i in data.get("items", []) if i.get("id", {}).get("videoId")]


def details(ids):
    out = {}
    for i in range(0, len(ids), 50):
        data = yt("videos", part="snippet,contentDetails,status,player", maxWidth=1280,
                  id=",".join(ids[i:i + 50]))
        for it in data.get("items", []):
            out[it["id"]] = it
    return out


def seconds(iso):
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def clock(sec):
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def reject_reason(item, cfg, row=None):
    """row có thể đặt min_minutes / max_minutes riêng (ví dụ vlog du lịch thường dài hơn)."""
    if row:
        cfg = dict(cfg, **{k: row[k] for k in ("min_minutes", "max_minutes") if k in row})
    sn, st, cd = item.get("snippet", {}), item.get("status", {}), item.get("contentDetails", {})
    dur = seconds(cd.get("duration"))
    if not st.get("embeddable", False):
        return "không cho nhúng"
    if st.get("privacyStatus") != "public":
        return "không công khai"
    if sn.get("liveBroadcastContent", "none") != "none":
        return "phát trực tiếp"
    if dur < cfg["min_minutes"] * 60:
        return "quá ngắn"
    if dur > cfg["max_minutes"] * 60:
        return "quá dài"
    lang = (sn.get("defaultAudioLanguage") or sn.get("defaultLanguage") or "en").lower()
    if not lang.startswith("en"):
        return f"không phải tiếng Anh ({lang})"
    if "#shorts" in (sn.get("title", "") + sn.get("description", "")[:300]).lower():
        return "Shorts"
    pl = item.get("player", {})
    try:
        w, h = float(pl.get("embedWidth", 0)), float(pl.get("embedHeight", 0))
    except (TypeError, ValueError):
        w = h = 0
    if w and h and w / h < 1.3:
        return "video dọc"
    return None


# ------------------------------------------------------------------ Tìm ứng viên
def find_candidates(cfg, skip):
    """Danh sách (video_id, row_id), xen kẽ các hàng theo weight để nội dung đa dạng."""
    doy = dt.date.today().timetuple().tm_yday
    per_row = {r["id"]: [] for r in cfg["rows"]}
    for row in cfg["rows"]:
        for handle, cid in (row.get("channels") or {}).items():
            try:
                per_row[row["id"]] += channel_uploads(cid, int(cfg.get("uploads_per_channel", 12)))
            except QuotaExceeded:
                raise
            except Exception as e:
                print(f"  ! Kênh @{handle}: {e}")

    # Vài lệnh tìm kiếm mỗi ngày, xoay vòng theo ngày để mỗi hôm một chủ đề khác
    plan = []
    rows_q = [r for r in cfg["rows"] if r.get("queries")]
    for k in range(max((len(r["queries"]) for r in rows_q), default=0)):
        for row in rows_q:
            q = row["queries"][(doy + k) % len(row["queries"])]
            if (row["id"], q) not in plan:
                plan.append((row["id"], q))
    for row_id, q in plan[: int(cfg["max_searches_per_day"])]:
        try:
            found = search(q, int(cfg["search_recent_days"]))
            per_row[row_id] += found
            print(f"  Tìm [{row_id}] “{q}”: {len(found)} kết quả")
        except QuotaExceeded:
            raise
        except Exception as e:
            print(f"  ! Tìm “{q}”: {e}")

    queues, used = [], set(skip)
    for row in cfg["rows"]:
        q = []
        for vid in per_row.get(row["id"], []):
            if vid not in used:
                used.add(vid)
                q.append((vid, row["id"]))
        if q:
            queues.append([max(1, int(row.get("weight", 1))), q])
    mixed = []
    while queues:
        for entry in list(queues):
            weight, q = entry
            for _ in range(weight):
                if q:
                    mixed.append(q.pop(0))
            if not q:
                queues.remove(entry)
    return mixed


# ------------------------------------------------------------------ Một video
def make_phrases(vid):
    """Lấy phụ đề + soạn cụm từ, lưu file. Trả về kết quả. Ném lỗi của video_phrases nếu không được."""
    old = load(vp.OUT_DIR / f"{vid}.json", {})
    if old.get("phrases") and len(old["phrases"]) >= 5 and str(old.get("source", "")).startswith("youtube"):
        return old                    # đã soạn ở lần chạy trước (bị ngắt giữa chừng): dùng lại, không gọi AI nữa
    tr = vp.get_transcript(vid)
    if len(tr["segments"]) < 30:
        raise vp.NoEnglish()
    out = vp.build(vid, tr)
    if len(out["phrases"]) < 5:
        raise vp.NoEnglish()
    save(vp.OUT_DIR / f"{vid}.json", out)
    return out


def phrase_info(vid):
    d = load(vp.OUT_DIR / f"{vid}.json", {})
    if d.get("phrases") and str(d.get("source", "")).startswith("youtube"):
        return {"n": len(d["phrases"]), "level": d.get("level", "")}
    return None


def main() -> int:
    cfg = load(SOURCES, None)
    if not cfg:
        print("Không đọc được video/sources.json")
        return 1
    ai_key = vp.GEMINI_KEY if vp.PROVIDER == "gemini" else vp.CLAUDE_KEY
    if not ai_key:
        print(f"Thiếu khóa AI cho AI_PROVIDER={vp.PROVIDER}.")
        return 1

    catalog = load(CATALOG, {"rows": [], "videos": []})
    catalog.setdefault("videos", [])
    state = load(STATE, {})
    rejected = state.setdefault("rejected", {})
    retries = state.setdefault("retries", {})
    days = state.setdefault("added_per_day", {})
    by_id = {v["id"]: v for v in catalog["videos"]}
    added_today = int(days.get(TODAY, 0))
    target = max(0, int(cfg["target_per_day"]) - added_today)
    added = 0
    ai_busy = 0
    stop = None

    def attempt(vid):
        """Trả về kết quả nếu đạt; None nếu không. Ném vp.Blocked khi YouTube chặn IP."""
        nonlocal ai_busy
        try:
            out = make_phrases(vid)
            ai_busy = 0
            retries.pop(vid, None)
            print(f"    ✓ {len(out['phrases'])} cụm từ, phụ đề {out['source'].split('-')[-1]}", flush=True)
            return out
        except vp.Blocked:
            raise
        except vp.NoEnglish:
            rejected[vid] = "không có phụ đề tiếng Anh dùng được"
            print("    ✗ không có phụ đề tiếng Anh dùng được", flush=True)
        except vp.Temporary as e:
            ai_busy += 1
            print(f"    … {e}. Để lần sau.", flush=True)
        except vp.CouldNotRetrieveTranscript as e:
            rejected[vid] = f"không lấy được phụ đề ({type(e).__name__})"
            print(f"    ✗ không lấy được phụ đề ({type(e).__name__})", flush=True)
        except Exception as e:
            retries[vid] = retries.get(vid, 0) + 1
            if retries[vid] >= 2:
                rejected[vid] = f"lỗi: {str(e)[:120]}"
            print(f"    ! lỗi: {str(e)[:160]}", flush=True)
        return None

    try:
        # 1) Bù cụm từ cho video có trong danh mục mà chưa có cụm từ
        backlog = [v for v in catalog["videos"] if not phrase_info(v["id"]) and v["id"] not in rejected]
        if backlog:
            n = min(int(cfg.get("backfill_per_run", 4)), len(backlog))
            print(f"Bù cụm từ cho video cũ: {n}/{len(backlog)}", flush=True)
            for v in backlog[:n]:
                print(f"  → {v['id']} {v['title'][:70]}", flush=True)
                attempt(v["id"])
                time.sleep(PAUSE)

        # 2) Tìm và thêm video mới
        if not YT_KEY:
            print("Chưa có YOUTUBE_API_KEY nên bỏ qua bước tìm video mới.")
        elif target == 0:
            print(f"Hôm nay đã thêm đủ {added_today} video. Mai chạy tiếp.")
        else:
            print(f"Tìm video mới (cần thêm {target} video hôm nay)…", flush=True)
            cands = find_candidates(cfg, set(by_id) | set(rejected))
            info = details([c[0] for c in cands][:300])
            rows_by_id = {r["id"]: r for r in cfg["rows"]}
            per = {}
            for vid, row_id in cands:
                item = info.get(vid)
                if not item:
                    continue
                why = reject_reason(item, cfg, rows_by_id.get(row_id))
                if why:
                    rejected[vid] = why
                    continue
                per.setdefault(row_id, []).append((vid, row_id, item))
            # Xen kẽ các hàng (theo weight) SAU khi lọc, để chủ đề nào cũng có video mới
            ok, queues = [], [[max(1, int(rows_by_id[r].get("weight", 1))), q] for r, q in per.items()]
            while queues:
                for entry in list(queues):
                    for _ in range(entry[0]):
                        if entry[1]:
                            ok.append(entry[1].pop(0))
                    if not entry[1]:
                        queues.remove(entry)
            print("Qua vòng lọc theo hàng: " + ", ".join(f"{r} {len(q)}" for r, q in
                  ((r, [x for x in ok if x[1] == r]) for r in per)), flush=True)
            print(f"Có {len(cands)} ứng viên, {len(ok)} qua vòng lọc. Bắt đầu lấy phụ đề…", flush=True)
            tried = 0
            for vid, row_id, item in ok:
                if added >= target or tried >= int(cfg["max_transcript_tries"]):
                    break
                if ai_busy >= 2:
                    stop = "AI đang bận hoặc hết lượt miễn phí hôm nay"
                    break
                sn = item["snippet"]
                tried += 1
                print(f"  → [{row_id}] {vid} {sn['title'][:70]}", flush=True)
                out = attempt(vid)
                if out:
                    entry = {
                        "id": vid, "row": row_id, "title": sn["title"],
                        "channel": sn.get("channelTitle", ""),
                        "dur": clock(seconds(item["contentDetails"]["duration"])),
                        "added": TODAY, "published": sn.get("publishedAt", "")[:10],
                        "level": out.get("level", ""), "n": len(out["phrases"]),
                    }
                    catalog["videos"].append(entry)
                    by_id[vid] = entry
                    added += 1
                    days[TODAY] = added_today + added
                    save(CATALOG, catalog)            # lưu ngay: lỡ bị ngắt cũng không mất video đã làm
                    save(STATE, state)
                time.sleep(PAUSE)
    except vp.Blocked as e:
        stop = f"YouTube đang chặn IP khi lấy phụ đề ({e})"
    except QuotaExceeded:
        stop = "hết hạn mức YouTube Data API hôm nay"

    # 3) Cập nhật thông tin cụm từ cho toàn bộ danh mục, ghi file
    for v in catalog["videos"]:
        pi = phrase_info(v["id"])
        v["n"] = pi["n"] if pi else 0
        if pi:
            v["level"] = pi["level"]
    rows_cfg = [{"id": r["id"], "title": r["title"]} for r in cfg["rows"]]
    known = {r["id"] for r in rows_cfg}
    extra = [r for r in catalog.get("rows", []) if r["id"] not in known
             and any(v["row"] == r["id"] for v in catalog["videos"])]
    catalog["rows"] = rows_cfg + extra
    catalog["updated"] = TODAY
    days[TODAY] = added_today + added
    for d in sorted(days)[:-30]:
        days.pop(d)
    save(CATALOG, catalog)
    save(STATE, state)
    vp.write_captions()
    vp.write_index()

    total = len(catalog["videos"])
    ready = sum(1 for v in catalog["videos"] if v["n"])
    print(f"\nHôm nay thêm {added} video mới (tổng trong ngày {days[TODAY]}). "
          f"Danh mục: {total} video, {ready} video có cụm từ.")
    if stop:
        print(f"Dừng sớm: {stop}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
