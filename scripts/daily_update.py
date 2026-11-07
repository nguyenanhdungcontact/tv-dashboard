#!/usr/bin/env python3
"""
Daily content update for the TV dashboard (runs once a day from GitHub Actions).

  python scripts/daily_update.py [news] [words] [--force] [--dry-run]

Pipeline
  A. claude-haiku-5-5 (normal call): ranks the de-duplicated RSS list -> id, group, score.
     The script keeps the 3 best per group (max 12 stories).
  B. claude-sonnet-5-5 (Message Batches API, 50% cheaper): writes title + summary for the
     picked stories. The daily 10 words (Haiku) go into the same batch.
  Fixed instructions are system prompts marked with cache_control.

Safety
  - If today's brief.json / today's words already exist, the task is skipped (use --force).
  - A batch that is still running when the job times out is saved in scripts/state.json and
    collected on the next run instead of being paid for twice.
  - Anything that fails leaves the existing files untouched.
"""
import argparse
import difflib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

API = "https://api.anthropic.com/v1"
SELECT_MODEL = os.environ.get("CLAUDE_SELECT_MODEL", "claude-haiku-5-5")
NEWS_MODEL = os.environ.get("CLAUDE_NEWS_MODEL", "claude-sonnet-5-5")
WORDS_MODEL = os.environ.get("CLAUDE_WORDS_MODEL", "claude-haiku-5-5")
WORDS_PER_DAY = int(os.environ.get("WORDS_PER_DAY", "10"))
PER_GROUP = int(os.environ.get("NEWS_PER_GROUP", "3"))
MAX_STORIES = int(os.environ.get("NEWS_MAX_STORIES", "12"))
MAX_AGE_HOURS = int(os.environ.get("NEWS_MAX_AGE_HOURS", "36"))
DESC_LIMIT = 300
PER_FEED = 25
BATCH_TIMEOUT_MIN = int(os.environ.get("BATCH_TIMEOUT_MIN", "100"))
# Optional: also write 2 key points + a "why it matters" line per story (≈ +60% output on step B).
NEWS_EXTRAS = os.environ.get("NEWS_EXTRAS", "0") == "1"

# USD per million tokens (input, output). Assumed list prices – check the pricing page and adjust.
PRICES = {"claude-sonnet-5-5": (3.0, 15.0), "claude-haiku-5-5": (1.0, 5.0)}
BATCH_DISCOUNT = 0.5

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIEF_PATH = os.path.join(ROOT, "docs", "brief.json")
WORDS_PATH = os.path.join(ROOT, "docs", "words.json")
FEEDS_PATH = os.path.join(ROOT, "scripts", "feeds.json")
STATE_PATH = os.path.join(ROOT, "scripts", "state.json")

VN = timezone(timedelta(hours=7))
WEEKDAYS = ["thứ Hai", "thứ Ba", "thứ Tư", "thứ Năm", "thứ Sáu", "thứ Bảy", "Chủ nhật"]
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
SECTION_META = {
    "hot": ("Nổi bật", "--c-hot"),
    "work": ("Công việc", "--c-work"),
    "life": ("Cuộc sống cần biết", "--c-life"),
    "heart": ("Ý nghĩa", "--c-heart"),
}

DRY_RUN = False
COST_LOG = []   # (step, model, usage, batch)


# ================================================================ API helpers
def api(path, body=None, method=None, raw=False):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ANTHROPIC_API_KEY is not set")
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"},
        method=method or ("POST" if body is not None else "GET"),
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        data = r.read().decode("utf-8")
    return data if raw else json.loads(data)


def system_block(text):
    # Fixed instructions, marked for prompt caching. Note: caching only kicks in above the
    # model's minimum prompt length, so short prompts are simply sent uncached.
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def message_params(model, system, user, max_tokens):
    return {"model": model, "max_tokens": max_tokens, "system": system_block(system),
            "messages": [{"role": "user", "content": user}]}


def text_of(msg):
    return "".join(b.get("text", "") for b in msg.get("content", []) if b.get("type") == "text")


def log_usage(step, msg, batch):
    usage = msg.get("usage", {}) or {}
    COST_LOG.append((step, msg.get("model", "?"), usage, batch))
    cut = "  (CUT OFF: hit max_tokens)" if msg.get("stop_reason") == "max_tokens" else ""
    print(f"  [{step}] {msg.get('model')}: in {usage.get('input_tokens', 0)} "
          f"(cache read {usage.get('cache_read_input_tokens', 0)}, write {usage.get('cache_creation_input_tokens', 0)}) "
          f"/ out {usage.get('output_tokens', 0)}{cut}")


def cost_of(model, usage, batch):
    key = next((k for k in PRICES if model.startswith(k)), None)
    if not key:
        return 0.0
    pin, pout = PRICES[key]
    c = (usage.get("input_tokens", 0) * pin
         + usage.get("cache_creation_input_tokens", 0) * pin * 1.25
         + usage.get("cache_read_input_tokens", 0) * pin * 0.1
         + usage.get("output_tokens", 0) * pout) / 1e6
    return c * (BATCH_DISCOUNT if batch else 1)


def extract_json(text, opener, closer):
    text = re.sub(r"```(?:json)?", "", text)
    start, end = text.find(opener), text.rfind(closer)
    if start < 0 or end <= start:
        raise ValueError("no JSON found in the reply")
    return json.loads(text[start:end + 1])


# ================================================================ RSS
def clean_text(s, limit):
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


def first(fields, *names):
    for n in names:
        if fields.get(n) is not None:
            return fields[n]
    return None


def read_feed(name, url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8"})
    with urllib.request.urlopen(req, timeout=30) as r:
        root = ET.fromstring(r.read())
    items = []
    for node in root.iter():
        if local(node.tag) not in ("item", "entry"):
            continue
        fields = {local(c.tag): c for c in node}
        title_node = fields.get("title")
        title = clean_text(title_node.text if title_node is not None else "", 200)
        desc_node = first(fields, "description", "summary", "content")
        desc = clean_text("".join(desc_node.itertext()) if desc_node is not None else "", DESC_LIMIT)
        link_node, link = fields.get("link"), ""
        if link_node is not None:
            link = (link_node.text or link_node.attrib.get("href", "")).strip()
        date_node = first(fields, "pubDate", "published", "updated", "date")
        when = parse_date(date_node.text if date_node is not None else "")
        if title:
            items.append({"source": name, "title": title, "desc": desc, "link": link, "when": when})
        if len(items) >= PER_FEED:
            break
    return items


def norm_link(u):
    p = urllib.parse.urlsplit(u.strip())
    return (p.netloc.lower().removeprefix("www.") + p.path.rstrip("/")).lower()


def norm_title(t):
    return re.sub(r"[^\w\s]", "", t.lower()).strip()


def collect_articles():
    with open(FEEDS_PATH, encoding="utf-8") as f:
        feeds = json.load(f)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    articles, links, titles = [], set(), []
    dupes = 0
    for group, entries in feeds.items():
        for feed in entries:
            try:
                got = read_feed(feed["name"], feed["url"])
            except Exception as e:
                print(f"  feed failed: {feed['url']} ({e})")
                continue
            kept = 0
            for it in got:
                if it["when"] and it["when"] < cutoff:
                    continue
                lk, nt = norm_link(it["link"]) if it["link"] else "", norm_title(it["title"])
                if (lk and lk in links) or any(difflib.SequenceMatcher(None, nt, t).ratio() >= 0.82 for t in titles):
                    dupes += 1
                    continue
                if lk:
                    links.add(lk)
                titles.append(nt)
                it["group"] = group
                articles.append(it)
                kept += 1
            print(f"  {feed['name']}: {kept} recent items")
    for i, it in enumerate(articles):
        it["id"] = i
    print(f"  {len(articles)} articles after removing {dupes} duplicates")
    return articles


def article_line(a):
    d = a["when"].astimezone(VN).strftime("%d/%m %H:%M") if a["when"] else "?"
    return f"[{a['id']}] ({a['group']}) {a['source']} | {d} | {a['title']} — {a['desc']}"


# ================================================================ prompts (fixed parts = system)
GROUPS_DEF = """4 nhóm tin:
- "hot" – Nổi bật: tin quan trọng ở Việt Nam và thế giới (kinh tế, chính sách, tài chính, công nghệ, sự kiện lớn).
- "work" – Công việc: Google Ads, quảng cáo tìm kiếm, YouTube Ads, AI trong marketing, đo lường, chính sách quảng cáo, đối thủ (Meta, TikTok, ChatGPT ads…).
- "life" – Cuộc sống cần biết: tin thiết thực cho người sống ở TP.HCM (thời tiết, giao thông, giá cả, lãi suất, chính sách mới, nhà ở, sức khỏe cộng đồng).
- "heart" – Ý nghĩa: câu chuyện tích cực, nhân văn hoặc truyền cảm hứng."""

SELECT_SYSTEM = f"""Bạn chọn tin cho bản tin trên TV của một người làm quảng cáo số (Google Ads) sống ở TP.HCM.

{GROUPS_DEF}

Với mỗi nhóm, chọn tối đa 5 bài đáng quan tâm nhất và chấm điểm 1–10 theo mức quan trọng và hữu ích với người đọc. Bỏ bài giật gân, trùng chủ đề, án mạng, tai nạn thương tâm, bạo hành. Mỗi bài chỉ thuộc một nhóm.

Chỉ trả về JSON, không lời dẫn, không markdown:
{{"items": [{{"id": 12, "group": "hot", "score": 9}}]}}"""

WRITE_SYSTEM = """Bạn viết bản tin tiếng Việt hiển thị trên TV lớn cho một người làm quảng cáo số (Google Ads) sống ở TP.HCM.

Với mỗi bài được đưa vào, viết bằng lời của bạn (kể cả khi bài gốc là tiếng Anh), không chép nguyên văn, chỉ dùng thông tin có trong bài, không thêm số liệu hay ngày tháng. Dùng “ ” và ’, không dùng dấu " bên trong nội dung.
- "ref": số id của bài
- "title": tiêu đề tiếng Việt rõ ràng, tối đa 12 từ
- "summary": 2–3 câu kể lại nội dung chính
__EXTRAS__
Chỉ trả về một mảng JSON thuần, không lời dẫn, không markdown:
[{"ref": 3, "title": "", "summary": ""__EXTRAS_JSON__}]"""
EXTRAS_RULES = """- "points": 2 ý ngắn (mỗi ý tối đa 12 từ) nêu chi tiết đáng nhớ có trong bài
- "why": 1 câu nói vì sao tin này đáng chú ý với người đọc
"""
WRITE_SYSTEM = WRITE_SYSTEM.replace("__EXTRAS__", EXTRAS_RULES if NEWS_EXTRAS else "") \
                           .replace("__EXTRAS_JSON__", ', "points": ["", ""], "why": ""' if NEWS_EXTRAS else "")

WORDS_SYSTEM = """Bạn là gia sư tiếng Anh cho một người Việt làm quảng cáo số (Google Ads), trình độ khoảng B2, muốn học từ vựng dùng được trong công việc và đời sống. Chọn từ trình độ B2–C1, thông dụng, tự nhiên, người bản xứ hay dùng; khoảng 60% từ công việc (marketing, quảng cáo, dữ liệu, giao tiếp với khách hàng, họp hành), còn lại là từ đời sống, cảm xúc, quan hệ. Không lặp lại từ người học đã có.

Mỗi từ gồm:
- "w": từ hoặc cụm từ (chữ thường, trừ danh từ riêng)
- "ipa": phiên âm IPA kiểu Anh, có dấu gạch chéo
- "pos": loại từ bằng tiếng Anh
- "vi": nghĩa tiếng Việt ngắn gọn
- "ex": một câu ví dụ tự nhiên bằng tiếng Anh, tối đa 14 từ, dùng ’ thay cho '
- "col": 3 cụm từ phổ biến chứa từ đó

Chỉ trả về một mảng JSON, không lời dẫn, không markdown."""


# ================================================================ helpers for "already done today"
def today_vn():
    return datetime.now(VN)


def news_done_today():
    try:
        with open(BRIEF_PATH, encoding="utf-8") as f:
            return json.load(f).get("date") == today_vn().strftime("%Y-%m-%d")
    except (OSError, ValueError):
        return False


def load_words():
    try:
        with open(WORDS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def words_done_today(words):
    t = today_vn().strftime("%Y-%m-%d")
    return any(isinstance(w, dict) and w.get("added") == t for w in words)


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ================================================================ step A (Haiku, direct call)
def step_a_select(articles):
    today = today_vn()
    user = f"Hôm nay là {WEEKDAYS[today.weekday()]}, {today:%d/%m/%Y}. Danh sách bài:\n" + \
           "\n".join(article_line(a) for a in articles)
    msg = api("/messages", message_params(SELECT_MODEL, SELECT_SYSTEM, user, 2500))
    log_usage("A select", msg, batch=False)
    reply = text_of(msg)
    try:
        data = extract_json(reply, "{", "}")
    except ValueError:
        # Reply cut off or wrapped in text: keep every complete {...} object that has an id and a group.
        items = []
        for chunk in re.findall(r"\{[^{}]*\}", reply):
            try:
                obj = json.loads(chunk)
            except ValueError:
                continue
            if isinstance(obj, dict) and "id" in obj and "group" in obj:
                items.append(obj)
        found = items
        data = {"items": items}
        print(f"  step A reply was not clean JSON, salvaged {len(found)} items. Start of reply: {reply[:200]!r}")
    by_id = {a["id"]: a for a in articles}
    ranked = {g: [] for g in SECTION_META}
    seen = set()
    for it in data.get("items", []):
        try:
            i, g, s = int(it.get("id")), str(it.get("group")), float(it.get("score", 0))
        except (TypeError, ValueError):
            continue
        if i in by_id and g in ranked and i not in seen:
            seen.add(i)
            ranked[g].append((s, i))
    picks = []
    for g in SECTION_META:
        for s, i in sorted(ranked[g], reverse=True)[:PER_GROUP]:
            picks.append({"id": i, "group": g, "score": s})
    picks = picks[:MAX_STORIES]
    if len(picks) < 6:
        raise ValueError(f"only {len(picks)} stories picked, keeping the old brief")
    print(f"  picked {len(picks)} stories: " + ", ".join(f"{g}={sum(p['group'] == g for p in picks)}" for g in SECTION_META))
    return picks


def news_request(picks, by_id):
    lines = []
    for g in SECTION_META:
        group_items = [p for p in picks if p["group"] == g]
        if group_items:
            lines.append(f"## Mục “{SECTION_META[g][0]}”")
            lines += [article_line(by_id[p["id"]]) for p in group_items]
    per_story = 330 if NEWS_EXTRAS else 230          # ≈170 tokens used per story, plus headroom
    return message_params(NEWS_MODEL, WRITE_SYSTEM, "\n".join(lines), 500 + per_story * len(picks))


def words_request(words):
    existing = sorted({w.get("w", "").strip().lower() for w in words if isinstance(w, dict) and w.get("w")})
    user = f"Các từ đã có: {', '.join(existing) or '(chưa có)'}\n\nHãy chọn đúng {WORDS_PER_DAY} từ mới."
    return message_params(WORDS_MODEL, WORDS_SYSTEM, user, 400 * WORDS_PER_DAY + 400)


# ================================================================ batch
def run_batch(requests, state):
    """Create (or resume) a batch, wait for it, return {custom_id: message}."""
    pending = state.get("pending")
    if pending:
        batch_id = pending["batch_id"]
        print(f"  resuming batch {batch_id} from the previous run")
    else:
        batch = api("/messages/batches", {"requests": [{"custom_id": cid, "params": p} for cid, p in requests]})
        batch_id = batch["id"]
        print(f"  created batch {batch_id} with {len(requests)} requests")
    deadline = time.time() + BATCH_TIMEOUT_MIN * 60
    delay = 15
    while True:
        b = api(f"/messages/batches/{batch_id}")
        status = b.get("processing_status")
        if status == "ended":
            break
        if time.time() > deadline:
            raise TimeoutError(f"batch {batch_id} still {status} after {BATCH_TIMEOUT_MIN} min; it will be collected next run")
        time.sleep(delay)
        delay = min(delay * 1.5, 120)
    out = {}
    raw = api(f"/messages/batches/{batch_id}/results", raw=True)
    for line in raw.splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        res = r.get("result", {})
        if res.get("type") == "succeeded":
            out[r["custom_id"]] = res["message"]
        else:
            print(f"  batch item {r.get('custom_id')} {res.get('type')}: {res.get('error')}")
    return out


# ================================================================ apply results
def apply_news(msg, picks, by_id_meta):
    items_by_ref = {}
    for it in extract_json(text_of(msg), "[", "]"):
        try:
            items_by_ref[int(it.get("ref"))] = it
        except (TypeError, ValueError, AttributeError):
            pass
    now = today_vn()
    sections = []
    for g in SECTION_META:
        items = []
        for p in [p for p in picks if p["group"] == g]:
            it, src = items_by_ref.get(p["id"]), by_id_meta[str(p["id"])]
            title = str((it or {}).get("title", "")).strip()
            summary = str((it or {}).get("summary", "")).strip()
            if not title or not summary:
                continue
            items.append({
                "title": title,
                "summary": summary,
                "points": [str(x).strip() for x in ((it or {}).get("points") or []) if str(x).strip()][:3],
                "why": str((it or {}).get("why", "")).strip(),
                "source": src["source"],          # always from the feed, never from the model
                "date": src["date"],
                "link": src["link"],
            })
        if items:
            name, color = SECTION_META[g]
            sections.append({"id": g, "name": name, "color": color, "items": items})
    if sum(len(s["items"]) for s in sections) < 6:
        raise ValueError("brief looks incomplete, keeping the old one")
    brief = {
        "date": now.strftime("%Y-%m-%d"),
        "updated": f"Cập nhật {now:%H:%M} {WEEKDAYS[now.weekday()]}, {now:%d/%m/%Y}",
        "sections": sections,
    }
    with open(BRIEF_PATH, "w", encoding="utf-8") as f:
        json.dump(brief, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"brief.json updated: {sum(len(s['items']) for s in sections)} stories")


def apply_words(msg, words):
    existing = {w.get("w", "").strip().lower() for w in words if isinstance(w, dict)}
    today = today_vn().strftime("%Y-%m-%d")
    added = []
    for it in extract_json(text_of(msg), "[", "]"):
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
    with open(WORDS_PATH, "w", encoding="utf-8") as f:
        json.dump(words, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"words.json: +{len(added[:WORDS_PER_DAY])} words ({len(words)} total)")


# ================================================================ main
def main():
    global DRY_RUN
    ap = argparse.ArgumentParser()
    ap.add_argument("tasks", nargs="*", default=["news", "words"])
    ap.add_argument("--force", action="store_true", help="run even if today's content already exists")
    ap.add_argument("--dry-run", action="store_true", help="build requests and print sizes, no API calls")
    args = ap.parse_args()
    DRY_RUN = args.dry_run
    tasks = [t for t in args.tasks if t in ("news", "words")]

    state = load_state()
    words = load_words()
    today = today_vn().strftime("%Y-%m-%d")
    if state.get("pending") and state["pending"].get("date") != today:
        print("  dropping a pending batch from an earlier day")
        state = {}

    want_news = "news" in tasks and (args.force or not news_done_today())
    want_words = "words" in tasks and (args.force or not words_done_today(words))
    if not state.get("pending") and not want_news and not want_words:
        print("Today's content already exists – nothing to do (use --force to run again).")
        return 0

    requests, meta = [], {}
    if state.get("pending"):
        meta = state["pending"]
    else:
        if want_news:
            try:
                print("== news: collecting RSS")
                articles = collect_articles()
                if len(articles) < 10:
                    raise ValueError(f"only {len(articles)} recent articles")
                by_id = {a["id"]: a for a in articles}
                if DRY_RUN:
                    picks = [{"id": a["id"], "group": g, "score": 5}
                             for g in SECTION_META for a in [x for x in articles][:PER_GROUP]][:MAX_STORIES]
                else:
                    print("== step A: selecting with Haiku")
                    picks = step_a_select(articles)
                requests.append(("news", news_request(picks, by_id)))
                meta["picks"] = picks
                meta["articles"] = {str(p["id"]): {
                    "source": by_id[p["id"]]["source"], "link": by_id[p["id"]]["link"],
                    "date": (by_id[p["id"]]["when"].astimezone(VN).strftime("%d/%m") if by_id[p["id"]]["when"]
                             else today_vn().strftime("%d/%m"))} for p in picks}
            except Exception as e:
                print(f"[news] skipped: {e}", file=sys.stderr)
        if want_words:
            requests.append(("words", words_request(words)))
        if not requests:
            return 1
        if DRY_RUN:
            for cid, p in requests:
                chars = len(p["system"][0]["text"]) + len(p["messages"][0]["content"])
                print(f"  [{cid}] {p['model']}: ~{chars} prompt chars, max_tokens {p['max_tokens']}")
            return 0

    try:
        if not meta.get("batch_id"):
            ids = [cid for cid, _ in requests]
            pending = {"date": today, "ids": ids, **meta}
            # create the batch, remember it before waiting
            batch = api("/messages/batches", {"requests": [{"custom_id": cid, "params": p} for cid, p in requests]})
            pending["batch_id"] = batch["id"]
            state["pending"] = pending
            save_state(state)
            print(f"== step B: batch {batch['id']} ({', '.join(ids)})")
        results = run_batch(requests, state)
    except TimeoutError as e:
        print(f"[batch] {e}", file=sys.stderr)
        return 0
    except Exception as e:
        print(f"[batch] failed: {e}", file=sys.stderr)
        state.pop("pending", None)
        save_state(state)
        return 1

    pending = state.get("pending", {})
    state.pop("pending", None)
    save_state(state)
    ok = True
    if "news" in results:
        log_usage("B news", results["news"], batch=True)
        try:
            apply_news(results["news"], pending["picks"], pending["articles"])
        except Exception as e:
            ok = False
            print(f"[news] skipped: {e}", file=sys.stderr)
    if "words" in results:
        log_usage("words", results["words"], batch=True)
        try:
            apply_words(results["words"], words)
        except Exception as e:
            ok = False
            print(f"[words] skipped: {e}", file=sys.stderr)

    total = 0.0
    print("== cost (estimate, assumed list prices)")
    for step, model, usage, batch in COST_LOG:
        c = cost_of(model, usage, batch)
        total += c
        print(f"  {step:<9} {model:<18} in {usage.get('input_tokens', 0):>6}  out {usage.get('output_tokens', 0):>6}"
              f"  {'batch ' if batch else ''}${c:.4f}")
    print(f"  total ${total:.4f}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
