#!/usr/bin/env python3
"""
Daily content update for the TV dashboard.

  python scripts/daily_update.py news    -> docs/brief.json from RSS feeds, written by Claude Sonnet
  python scripts/daily_update.py words   -> +10 words in docs/words.json, chosen by Claude Haiku

News: articles come from the RSS feeds in scripts/feeds.json (free). Claude only picks and
rewrites them – no web search – and the source/date always come from the feed itself.
If anything goes wrong, the existing files are left untouched.
"""
import html
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

API_URL = "https://api.anthropic.com/v1/messages"
NEWS_MODEL = os.environ.get("CLAUDE_NEWS_MODEL", "claude-sonnet-5-5")
WORDS_MODEL = os.environ.get("CLAUDE_WORDS_MODEL", "claude-haiku-5-5")
WORDS_PER_DAY = int(os.environ.get("WORDS_PER_DAY", "10"))
MAX_AGE_HOURS = int(os.environ.get("NEWS_MAX_AGE_HOURS", "48"))
PER_FEED = 25

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIEF_PATH = os.path.join(ROOT, "docs", "brief.json")
WORDS_PATH = os.path.join(ROOT, "docs", "words.json")
FEEDS_PATH = os.path.join(ROOT, "scripts", "feeds.json")

VN = timezone(timedelta(hours=7))
WEEKDAYS = ["thứ Hai", "thứ Ba", "thứ Tư", "thứ Năm", "thứ Sáu", "thứ Bảy", "Chủ nhật"]
UA = "Mozilla/5.0 (TV-dashboard RSS reader)"


# ---------------------------------------------------------------- Claude
def call_claude(prompt, model, max_tokens=6000):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ANTHROPIC_API_KEY is not set")
    body = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.loads(r.read().decode("utf-8"))
    usage = data.get("usage", {})
    print(f"  {model}: {usage.get('input_tokens', '?')} in / {usage.get('output_tokens', '?')} out tokens")
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def extract_json(text, opener, closer):
    text = re.sub(r"```(?:json)?", "", text)
    start, end = text.find(opener), text.rfind(closer)
    if start < 0 or end <= start:
        raise ValueError("no JSON found in the reply")
    return json.loads(text[start:end + 1])


# ---------------------------------------------------------------- RSS
def clean_text(s, limit=220):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[:limit].rsplit(" ", 1)[0] + "…"


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def local(tag):
    return tag.rsplit("}", 1)[-1]


def read_feed(name, url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        root = ET.fromstring(r.read())
    items = []
    for node in root.iter():
        if local(node.tag) not in ("item", "entry"):
            continue
        fields = {local(c.tag): c for c in node}
        title = clean_text((fields.get("title").text if fields.get("title") is not None else ""), 200)
        desc_node = fields.get("description") or fields.get("summary") or fields.get("content")
        desc = clean_text("".join(desc_node.itertext()) if desc_node is not None else "")
        link_node = fields.get("link")
        link = ""
        if link_node is not None:
            link = (link_node.text or link_node.attrib.get("href", "")).strip()
        date_node = fields.get("pubDate") or fields.get("published") or fields.get("updated") or fields.get("date")
        when = parse_date(date_node.text if date_node is not None else "")
        if title:
            items.append({"source": name, "title": title, "desc": desc, "link": link, "when": when})
        if len(items) >= PER_FEED:
            break
    return items


def collect_articles():
    with open(FEEDS_PATH, encoding="utf-8") as f:
        feeds = json.load(f)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    articles, seen = [], set()
    for group, entries in feeds.items():
        for feed in entries:
            try:
                got = read_feed(feed["name"], feed["url"])
            except Exception as e:
                print(f"  feed failed: {feed['url']} ({e})")
                continue
            kept = 0
            for it in got:
                key = it["title"].lower()
                if key in seen or (it["when"] and it["when"] < cutoff):
                    continue
                seen.add(key)
                it["group"] = group
                articles.append(it)
                kept += 1
            print(f"  {feed['name']}: {kept} recent items")
    for i, it in enumerate(articles):
        it["id"] = i
    return articles


# ---------------------------------------------------------------- news
NEWS_PROMPT = """Hôm nay là {today} (giờ Việt Nam). Dưới đây là các bài báo đăng trong 48 giờ qua, lấy từ RSS. Hãy làm bản tin cho một người làm quảng cáo số (Google Ads) sống ở TP.HCM. Bản tin hiển thị trên TV lớn, đọc thong thả trong ngày.

Chọn TẤT CẢ các bài mà người đọc thực sự nên quan tâm, không giới hạn số lượng; bỏ những bài không liên quan, giật gân hoặc trùng chủ đề (khi trùng, giữ bài đầy đủ nhất). Xếp vào 4 mục:
- "hot" – Nổi bật: tin quan trọng ở Việt Nam và thế giới (kinh tế, chính sách, tài chính, công nghệ, sự kiện lớn).
- "work" – Công việc: Google Ads, quảng cáo tìm kiếm, YouTube Ads, AI trong marketing, đo lường, chính sách quảng cáo, đối thủ (Meta, TikTok, ChatGPT ads…).
- "life" – Cuộc sống cần biết: tin thiết thực cho người sống ở TP.HCM (thời tiết, giao thông, giá cả, lãi suất, chính sách mới, nhà ở, sức khỏe cộng đồng).
- "heart" – Ý nghĩa: câu chuyện tích cực, nhân văn hoặc truyền cảm hứng. Nếu không có thì để trống.
Trong mỗi mục, xếp tin quan trọng nhất lên trước.

Quy tắc:
- Chỉ dùng các bài trong danh sách. Mỗi tin ghi "ref" là số id của bài gốc. Không thêm chi tiết, số liệu hay ngày tháng không có trong bài.
- Viết tiếng Việt bằng lời của bạn (kể cả khi bài gốc là tiếng Anh), không chép nguyên văn câu của bài báo.
- "title": rõ ràng, tối đa khoảng 18 từ.
- "summary": 2–4 câu kể lại nội dung chính của bài.
- "points": 2–3 ý ngắn (mỗi ý tối đa khoảng 15 từ) nêu chi tiết đáng nhớ có trong bài: con số, mốc thời gian, ai bị ảnh hưởng.
- "why": 1–2 câu nói vì sao tin này đáng chú ý với người đọc, hoặc gợi ý nên làm gì.
- Bỏ qua tin án mạng, tai nạn thương tâm, bạo hành và các tin gây ám ảnh.
- Dùng “ ” và ’ trong văn bản, không dùng dấu " bên trong nội dung.

Chỉ trả về đúng một đối tượng JSON, không có lời dẫn hay markdown:
{{"sections": [
  {{"id": "hot", "items": [{{"ref": 0, "title": "", "summary": "", "points": ["", ""], "why": ""}}]}},
  {{"id": "work", "items": [...]}},
  {{"id": "life", "items": [...]}},
  {{"id": "heart", "items": [...]}}
]}}

Danh sách bài:
{articles}"""

SECTION_META = {
    "hot": ("Nổi bật", "--c-hot"),
    "work": ("Công việc", "--c-work"),
    "life": ("Cuộc sống cần biết", "--c-life"),
    "heart": ("Ý nghĩa", "--c-heart"),
}


def update_news():
    articles = collect_articles()
    if len(articles) < 10:
        raise ValueError(f"only {len(articles)} recent articles from RSS, keeping the old brief")

    lines = []
    for a in articles:
        d = a["when"].astimezone(VN).strftime("%d/%m %H:%M") if a["when"] else "?"
        lines.append(f"[{a['id']}] ({a['group']}) {a['source']} | {d} | {a['title']} — {a['desc']}")

    now = datetime.now(VN)
    today = f"{WEEKDAYS[now.weekday()]}, {now:%d/%m/%Y}"
    reply = call_claude(NEWS_PROMPT.format(today=today, articles="\n".join(lines)), NEWS_MODEL, 16000)
    data = extract_json(reply, "{", "}")

    by_id = {a["id"]: a for a in articles}
    used, sections = set(), []
    for sec in data.get("sections", []):
        sid = sec.get("id")
        if sid not in SECTION_META:
            continue
        items = []
        for it in sec.get("items", []):
            try:
                ref = int(it.get("ref"))
            except (TypeError, ValueError):
                continue
            src = by_id.get(ref)
            title, summary = str(it.get("title", "")).strip(), str(it.get("summary", "")).strip()
            if not src or ref in used or not title or not summary:
                continue
            used.add(ref)
            when = src["when"].astimezone(VN).strftime("%d/%m") if src["when"] else now.strftime("%d/%m")
            items.append({
                "title": title,
                "summary": summary,
                "points": [str(p).strip() for p in (it.get("points") or []) if str(p).strip()][:4],
                "why": str(it.get("why", "")).strip(),
                "source": src["source"],          # always taken from the feed, never from the model
                "date": when,
                "link": src["link"],
            })
        if items:
            name, color = SECTION_META[sid]
            sections.append({"id": sid, "name": name, "color": color, "items": items})

    if len(sections) < 3 or sum(len(s["items"]) for s in sections) < 6:
        raise ValueError("brief looks incomplete, keeping the old one")

    brief = {"updated": f"Cập nhật {now:%H:%M} {today}", "sections": sections}
    with open(BRIEF_PATH, "w", encoding="utf-8") as f:
        json.dump(brief, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"brief.json updated: {sum(len(s['items']) for s in sections)} stories from {len(articles)} articles")


# ---------------------------------------------------------------- words
WORDS_PROMPT = """Bạn là gia sư tiếng Anh cho một người Việt làm quảng cáo số (Google Ads), trình độ khoảng B2, muốn học từ vựng dùng được trong công việc và đời sống.

Đây là các từ người học đã có, KHÔNG được lặp lại: {existing}

Hãy chọn đúng {n} từ hoặc cụm từ mới, trình độ B2–C1: khoảng {n_work} từ dùng trong công việc (marketing, quảng cáo, dữ liệu, giao tiếp với khách hàng, họp hành) và {n_life} từ đời sống, cảm xúc, quan hệ. Ưu tiên từ thông dụng, tự nhiên, người bản xứ hay dùng.

Với mỗi từ, trả về:
- "w": từ hoặc cụm từ (chữ thường, trừ danh từ riêng)
- "ipa": phiên âm IPA kiểu Anh, có dấu gạch chéo, ví dụ "/ˈbentʃ.mɑːk/"
- "pos": loại từ bằng tiếng Anh (noun, verb, adj, adv, phrasal verb, idiom…)
- "vi": nghĩa tiếng Việt ngắn gọn
- "ex": một câu ví dụ tự nhiên bằng tiếng Anh, tối đa 14 từ, dùng dấu nháy ’ thay cho '
- "col": 3 cụm từ (collocations) phổ biến nhất có chứa từ đó, thể hiện cách dùng thường gặp, ví dụ với "pragmatic": ["pragmatic approach", "pragmatic solution", "take a pragmatic view"]

Chỉ trả về một mảng JSON, không có lời dẫn hay markdown."""

COL_PROMPT = """Với mỗi từ tiếng Anh dưới đây, đưa ra 3 cụm từ (collocations) phổ biến nhất có chứa từ đó, thể hiện cách người bản xứ hay dùng. Ví dụ "pragmatic" -> ["pragmatic approach", "pragmatic solution", "take a pragmatic view"].

Các từ: {words}

Chỉ trả về một đối tượng JSON dạng {{"từ": ["cụm 1", "cụm 2", "cụm 3"], ...}}, không có lời dẫn hay markdown."""


def backfill_collocations(words, limit=40):
    missing = [w for w in words if isinstance(w, dict) and not w.get("col")][:limit]
    if not missing:
        return 0
    reply = call_claude(COL_PROMPT.format(words=", ".join(w["w"] for w in missing)), WORDS_MODEL, 3000)
    data = extract_json(reply, "{", "}")
    lookup = {k.strip().lower(): v for k, v in data.items() if isinstance(v, list)}
    done = 0
    for w in missing:
        cols = [str(c).strip() for c in lookup.get(w["w"].strip().lower(), []) if str(c).strip()]
        if cols:
            w["col"] = cols[:3]
            done += 1
    return done


def update_words():
    try:
        with open(WORDS_PATH, encoding="utf-8") as f:
            words = json.load(f)
    except (OSError, ValueError):
        words = []
    existing = {w.get("w", "").strip().lower() for w in words if isinstance(w, dict)}

    n_work = round(WORDS_PER_DAY * 0.6)
    reply = call_claude(WORDS_PROMPT.format(
        existing=", ".join(sorted(existing)) or "(chưa có)",
        n=WORDS_PER_DAY, n_work=n_work, n_life=WORDS_PER_DAY - n_work,
    ), WORDS_MODEL, 3000)
    new = extract_json(reply, "[", "]")

    today = datetime.now(VN).strftime("%Y-%m-%d")
    added = []
    for it in new:
        if not isinstance(it, dict):
            continue
        w = str(it.get("w", "")).strip()
        if not w or w.lower() in existing or not str(it.get("vi", "")).strip():
            continue
        entry = {k: str(it.get(k, "")).strip() for k in ("w", "ipa", "pos", "vi", "ex")}
        entry["col"] = [str(c).strip() for c in (it.get("col") or []) if str(c).strip()][:3]
        entry["added"] = today
        added.append(entry)
        existing.add(w.lower())

    if not added:
        raise ValueError("no new words returned")
    words.extend(added[:WORDS_PER_DAY])
    try:
        filled = backfill_collocations(words)
        if filled:
            print(f"  collocations added to {filled} older words")
    except Exception as e:
        print(f"  collocation backfill skipped: {e}")
    with open(WORDS_PATH, "w", encoding="utf-8") as f:
        json.dump(words, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"words.json: +{len(added[:WORDS_PER_DAY])} words ({len(words)} total)")


if __name__ == "__main__":
    tasks = sys.argv[1:] or ["news", "words"]
    failed = []
    for task in tasks:
        print(f"== {task}")
        try:
            {"news": update_news, "words": update_words}[task]()
        except Exception as e:  # keep the old file, report and move on
            failed.append(task)
            print(f"[{task}] skipped: {e}", file=sys.stderr)
    sys.exit(1 if failed and len(failed) == len(tasks) else 0)
