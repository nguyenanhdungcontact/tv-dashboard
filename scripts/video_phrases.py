"""
Soạn cụm từ tiếng Anh cho từng video, dùng phụ đề có sẵn trên YouTube.

1. Lấy transcript bằng youtube-transcript-api: ưu tiên phụ đề tiếng Anh do kênh làm (manual),
   không có thì dùng phụ đề tự động. Không có phụ đề tiếng Anh thì bỏ qua video.
   Giữ nguyên text, start, duration của từng đoạn, không sửa thời gian.
2. Lưu transcript gốc vào .cache/transcripts/ (KHÔNG commit, không công khai) để khỏi tải lại.
3. AI chỉ xử lý chữ: chọn cụm từ, nghĩa, ví dụ, cấp độ CEFR. AI trả về SỐ THỨ TỰ đoạn chứa cụm từ,
   script lấy start của đoạn đó từ transcript gốc. AI không bao giờ tạo ra mốc thời gian.
4. Repo chỉ lưu docs/video-data/phrases/<id>.json: cụm từ, câu ngắn chứa nó, start, duration.

Biến môi trường:
  AI_PROVIDER        gemini (mặc định) hoặc claude
  GEMINI_API_KEY     cần khi AI_PROVIDER=gemini
  GEMINI_MODEL       mặc định gemini-3.6-flash (có thể đổi sang bản Flash-Lite)
  ANTHROPIC_API_KEY  cần khi AI_PROVIDER=claude
  CLAUDE_MODEL       mặc định claude-haiku-5-5
  MAX_PER_RUN        số video tối đa mỗi lần chạy, mặc định 10
"""
import json
import os
import pathlib
import re
import sys
import time

import requests
from youtube_transcript_api import (
    YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled, VideoUnavailable,
    RequestBlocked, CouldNotRetrieveTranscript,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
LIST_FILE = ROOT / "video" / "english.json"
OUT_DIR = ROOT / "docs" / "video-data" / "phrases"
CACHE_DIR = ROOT / ".cache" / "transcripts"

PROVIDER = (os.environ.get("AI_PROVIDER") or "gemini").strip().lower()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = (os.environ.get("GEMINI_MODEL") or "gemini-3.6-flash").strip()
CLAUDE_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
CLAUDE_MODEL = (os.environ.get("CLAUDE_MODEL") or "claude-haiku-5-5").strip()
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN") or 10)
EN_CODES = ["en", "en-US", "en-GB", "en-AU", "en-CA", "en-IN", "en-IE", "en-NZ"]


class Blocked(Exception):
    """YouTube chặn IP (thường gặp trên máy chủ cloud). Dừng cả lượt chạy."""


class NoEnglish(Exception):
    """Video không có phụ đề tiếng Anh."""


class Temporary(Exception):
    """Lỗi tạm thời của dịch vụ AI: không lưu file, lần sau thử lại."""


# ------------------------------------------------------------------ 1-2. Transcript
def get_transcript(video_id: str) -> dict:
    cache = CACHE_DIR / f"{video_id}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))

    api = YouTubeTranscriptApi()
    try:
        tlist = api.list(video_id)
        try:
            tr = tlist.find_manually_created_transcript(EN_CODES)
        except NoTranscriptFound:
            tr = tlist.find_generated_transcript(EN_CODES)
        fetched = tr.fetch()
    except RequestBlocked as e:          # gồm cả IpBlocked
        raise Blocked(str(e).splitlines()[0] if str(e) else "RequestBlocked")
    except (NoTranscriptFound, TranscriptsDisabled):
        raise NoEnglish()
    except VideoUnavailable:
        raise NoEnglish()

    segments = []
    for s in fetched.to_raw_data():
        text = re.sub(r"\s+", " ", (s.get("text") or "").replace("\n", " ")).strip()
        if text:
            segments.append({"text": text, "start": s["start"], "duration": s.get("duration", 0)})
    data = {
        "id": video_id,
        "language": tr.language_code,
        "kind": "auto" if tr.is_generated else "manual",
        "segments": segments,
    }
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


# ------------------------------------------------------------------ 3. AI chỉ xử lý chữ
PROMPT = """You are an English teacher helping a Vietnamese professional (upper-intermediate level) learn natural English from a video.

Below is the video's subtitle text. Each line starts with [n] = the line number.{note}

<subtitles>
{lines}
</subtitles>

Choose 10 to 14 useful, natural expressions that appear in these lines: idioms, collocations, phrasal verbs, discourse markers, and phrases a learner would want to reuse at work or in daily life. Skip very basic words. Spread them across the whole video.

For each expression give:
- n: the line number where the expression appears (if it spans two lines, the first of them)
- phrase: the expression in base form
- meaning_vi: short Vietnamese meaning
- usage_vi: one short Vietnamese note on when and how to use it
- example_en: one NEW English example sentence, not from the video
- example_vi: Vietnamese translation of example_en
Also give summary_vi (2-sentence Vietnamese summary of the video) and level (CEFR level of the language: B1, B2, C1 or C2).
Do not invent times. Only refer to line numbers."""

PHRASE_PROPS = {
    "n": "integer", "phrase": "string", "meaning_vi": "string", "usage_vi": "string",
    "example_en": "string", "example_vi": "string",
}


def _schema(upper: bool) -> dict:
    T = (lambda t: t.upper()) if upper else (lambda t: t)
    return {
        "type": T("object"),
        "properties": {
            "summary_vi": {"type": T("string")},
            "level": {"type": T("string")},
            "phrases": {"type": T("array"), "items": {
                "type": T("object"),
                "properties": {k: {"type": T(v)} for k, v in PHRASE_PROPS.items()},
                "required": list(PHRASE_PROPS),
            }},
        },
        "required": ["summary_vi", "level", "phrases"],
    }


def ask_gemini(prompt: str) -> dict:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": _schema(True)},
    }
    for attempt in range(3):
        r = requests.post(url, headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=300)
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(20 * (attempt + 1))
            continue
        break
    else:
        raise Temporary(f"Gemini bận ({r.status_code})")
    if r.status_code != 200:
        raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:400]}")
    cand = (r.json().get("candidates") or [{}])[0]
    try:
        return json.loads(cand["content"]["parts"][0]["text"])
    except (KeyError, IndexError, json.JSONDecodeError):
        raise RuntimeError(f"Gemini trả kết quả lạ: finishReason={cand.get('finishReason')}")


def ask_claude(prompt: str) -> dict:
    tool = {"name": "save_phrases", "description": "Save the selected expressions.", "input_schema": _schema(False)}
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": CLAUDE_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": CLAUDE_MODEL, "max_tokens": 12000, "tools": [tool],
              "tool_choice": {"type": "tool", "name": "save_phrases"},
              "messages": [{"role": "user", "content": prompt}]},
        timeout=300,
    )
    if r.status_code in (429, 500, 529):
        raise Temporary(f"Claude bận ({r.status_code})")
    if r.status_code != 200:
        raise RuntimeError(f"Claude HTTP {r.status_code}: {r.text[:400]}")
    for b in r.json().get("content", []):
        if b.get("type") == "tool_use":
            return b.get("input", {})
    raise RuntimeError("Claude không trả kết quả")


# ------------------------------------------------------------------ Gắn cụm từ với đoạn phụ đề gốc
STOP = {"the", "and", "for", "your", "one", "one's", "someone", "somebody", "something",
        "sth", "sb", "with", "from", "into", "onto", "their", "his", "her", "its", "our"}


def _words(s: str) -> list:
    return re.findall(r"[a-z0-9']+", s.lower())


def _score(phrase: str, text: str) -> float:
    keys = [w for w in _words(phrase) if len(w) > 2 and w not in STOP] or _words(phrase)
    if not keys:
        return 0.0
    tw = _words(text)
    hit = sum(1 for k in keys if any(t.startswith(k[:max(3, len(k) - 2)]) for t in tw))
    return hit / len(keys)


def locate(phrase: str, n: int, segs: list):
    """Tìm đoạn thật sự chứa cụm từ quanh dòng AI chỉ ra. Trả về (chỉ số đoạn, câu trích) hoặc None."""
    best = None
    for j in range(max(0, n - 5), min(len(segs), n + 6)):
        one = segs[j]["text"]
        two = one + " " + segs[j + 1]["text"] if j + 1 < len(segs) else one
        s1, s2 = _score(phrase, one), _score(phrase, two)
        cand = (s1, j, one) if s1 >= s2 else (s2 - 0.01, j, two)
        # Ưu tiên điểm cao, rồi gần dòng AI chỉ ra
        key = (round(cand[0], 2), -abs(j - n))
        if best is None or key > best[0]:
            best = (key, cand)
    if not best or best[1][0] < 0.5:
        return None
    _, j, quote = best[1]
    words = quote.split()
    return j, " ".join(words[:25]) + ("…" if len(words) > 25 else "")


def build(video_id: str, tr: dict) -> dict:
    segs = tr["segments"]
    lines = "\n".join(f"[{i}] {s['text']}" for i, s in enumerate(segs))
    note = ("" if tr["kind"] == "manual" else
            "\nThese are auto-generated subtitles: punctuation may be missing and lines may cut sentences.")
    prompt = PROMPT.format(lines=lines, note=note)
    result = ask_gemini(prompt) if PROVIDER == "gemini" else ask_claude(prompt)

    phrases, seen = [], set()
    for p in result.get("phrases", []):
        try:
            n = int(p.get("n"))
        except (TypeError, ValueError):
            continue
        hit = locate(p.get("phrase", ""), n, segs)
        if not hit or hit[0] in seen and p.get("phrase") in [x["phrase"] for x in phrases]:
            continue
        j, quote = hit
        seen.add(j)
        phrases.append({
            "t": round(segs[j]["start"], 2),          # lấy nguyên từ phụ đề YouTube
            "d": round(segs[j]["duration"], 2),
            "phrase": p.get("phrase", ""), "quote": quote,
            "meaning_vi": p.get("meaning_vi", ""), "usage_vi": p.get("usage_vi", ""),
            "example_en": p.get("example_en", ""), "example_vi": p.get("example_vi", ""),
        })
    phrases.sort(key=lambda x: x["t"])
    return {
        "id": video_id, "created": time.strftime("%Y-%m-%d"),
        "source": f"youtube-{tr['kind']}", "language": tr["language"],
        "ai": GEMINI_MODEL if PROVIDER == "gemini" else CLAUDE_MODEL,
        "summary_vi": result.get("summary_vi", ""), "level": result.get("level", ""),
        "phrases": phrases,
    }


# ------------------------------------------------------------------ Chạy
def needs_work(vid: str) -> bool:
    f = OUT_DIR / f"{vid}.json"
    if not f.exists():
        return True
    try:
        old = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return True
    if old.get("error") == "no_english_captions":
        return False
    # File cũ do Gemini nghe video (mốc thời gian ước lượng) hoặc file lỗi: làm lại
    return not str(old.get("source", "")).startswith("youtube") or bool(old.get("error"))


def main() -> int:
    key_ok = GEMINI_KEY if PROVIDER == "gemini" else CLAUDE_KEY
    if not key_ok:
        print(f"Thiếu khóa cho AI_PROVIDER={PROVIDER}. Hãy đặt GEMINI_API_KEY hoặc ANTHROPIC_API_KEY.")
        return 1

    ids = json.loads(LIST_FILE.read_text(encoding="utf-8")).get("videos", [])
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    todo = [v for v in ids if needs_work(v)]
    print(f"AI: {PROVIDER}. Có {len(ids)} video, cần soạn {len(todo)}, lần này tối đa {MAX_PER_RUN}.", flush=True)

    done, blocked = 0, False
    for vid in todo[:MAX_PER_RUN]:
        print(f"→ {vid}", flush=True)
        try:
            tr = get_transcript(vid)
            print(f"  Phụ đề {tr['kind']} ({tr['language']}): {len(tr['segments'])} đoạn", flush=True)
            out = build(vid, tr)
            print(f"  Cụm từ: {len(out['phrases'])}", flush=True)
        except Blocked as e:
            print(f"  YouTube đang chặn IP của máy này: {e}")
            print("  Hãy chạy script trên máy Mac (xem scripts/run_local.sh) rồi commit kết quả.")
            blocked = True
            break
        except NoEnglish:
            print("  Không có phụ đề tiếng Anh, bỏ qua.")
            out = {"id": vid, "created": time.strftime("%Y-%m-%d"), "error": "no_english_captions", "phrases": []}
        except Temporary as e:
            print(f"  {e}. Lần chạy sau sẽ thử lại.")
            continue
        except CouldNotRetrieveTranscript as e:
            print(f"  Không lấy được phụ đề: {type(e).__name__}. Lần chạy sau sẽ thử lại.")
            continue
        except Exception as e:
            print(f"  Lỗi: {e}")
            out = {"id": vid, "created": time.strftime("%Y-%m-%d"), "error": str(e)[:300], "phrases": []}
        (OUT_DIR / f"{vid}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        done += 1
        time.sleep(3)

    print(f"Xong {done} video.")
    return 2 if blocked and done == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
