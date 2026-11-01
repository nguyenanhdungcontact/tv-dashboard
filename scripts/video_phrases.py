"""
Soạn cụm từ tiếng Anh hay cho từng video YouTube.

Bước 1 - Gemini: nghe video qua link YouTube, trả về lời thoại kèm mốc thời gian.
Bước 2 - Claude: đọc lời thoại, chọn cụm từ hay, viết nghĩa tiếng Việt, cách dùng, ví dụ.

- Đọc danh sách ID trong video/english.json
- Video nào chưa có docs/video-data/phrases/<id>.json thì xử lý
- Lời thoại đầy đủ chỉ dùng tạm trong lúc chạy, KHÔNG lưu vào repo công khai.
  Repo chỉ lưu các cụm từ và câu ngắn chứa chúng.

Biến môi trường:
  GEMINI_API_KEY     bắt buộc
  ANTHROPIC_API_KEY  bắt buộc
  GEMINI_MODEL       tùy chọn, mặc định gemini-3.6-flash
  CLAUDE_MODEL       tùy chọn, mặc định claude-haiku-5-5
  MAX_PER_RUN        tùy chọn, mặc định 4
"""
import json
import os
import pathlib
import re
import sys
import time

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
LIST_FILE = ROOT / "video" / "english.json"
OUT_DIR = ROOT / "docs" / "video-data" / "phrases"

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
CLAUDE_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
GEMINI_MODEL = (os.environ.get("GEMINI_MODEL") or "gemini-3.6-flash").strip()
CLAUDE_MODEL = (os.environ.get("CLAUDE_MODEL") or "claude-haiku-5-5").strip()
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN") or 4)


class RateLimited(Exception):
    pass


# ---------------------------------------------------------------- Bước 1: Gemini lấy lời thoại
TRANSCRIPT_PROMPT = """Transcribe the spoken English in this video verbatim.
Split it into segments of one sentence (or one short clause for long sentences).
For each segment give t = the second (integer, from the start of the video) when it begins, and text = the exact words.
Do not summarize, do not skip parts, do not add descriptions of visuals or music."""

TRANSCRIPT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "segments": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {"t": {"type": "INTEGER"}, "text": {"type": "STRING"}},
                "required": ["t", "text"],
            },
        }
    },
    "required": ["segments"],
}


def gemini_transcript(video_id: str, low_res: bool = True) -> list:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    body = {
        "contents": [{"parts": [
            {"file_data": {"file_uri": f"https://www.youtube.com/watch?v={video_id}"}},
            {"text": TRANSCRIPT_PROMPT},
        ]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": TRANSCRIPT_SCHEMA,
        },
    }
    if low_res:
        # Hình độ phân giải thấp để tốn ít token, âm thanh vẫn đầy đủ
        body["generationConfig"]["mediaResolution"] = "MEDIA_RESOLUTION_LOW"

    r = requests.post(url, headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=600)
    if r.status_code == 429:
        raise RateLimited("Gemini: " + r.text[:300])
    if r.status_code == 400 and low_res and "mediaResolution" in r.text:
        return gemini_transcript(video_id, low_res=False)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:500]}")

    data = r.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise RuntimeError(f"Gemini không trả nội dung: {json.dumps(data)[:500]}")

    segs = []
    for s in json.loads(text).get("segments", []):
        try:
            t = max(0, int(s.get("t", 0)))
        except (TypeError, ValueError):
            continue
        txt = (s.get("text") or "").strip()
        if txt:
            segs.append({"t": t, "text": txt})
    segs.sort(key=lambda s: s["t"])
    if len(segs) < 5:
        raise RuntimeError("Lời thoại quá ngắn hoặc rỗng")
    return segs


# ---------------------------------------------------------------- Bước 2: Claude chọn cụm từ
PHRASE_PROMPT = """You are an English teacher helping a Vietnamese professional (upper-intermediate level) learn natural English from a video.

Below is the video transcript. Each line starts with [t] = the second when that sentence begins.

<transcript>
{transcript}
</transcript>

Choose 10 to 14 useful, natural expressions that appear in the transcript: idioms, collocations, phrasal verbs, discourse markers, and phrases a learner would want to reuse at work or in daily life. Skip very basic words. Spread them across the whole video.

Return ONLY a JSON object, no markdown, in this shape:
{{
  "summary_vi": "2-sentence Vietnamese summary of the video",
  "level": "B1 | B2 | C1 | C2",
  "phrases": [
    {{
      "t": <the [t] of the line containing the expression, copied exactly>,
      "phrase": "expression in base form",
      "quote": "the words from that line containing it, at most 20 words, copied exactly",
      "meaning_vi": "short Vietnamese meaning",
      "usage_vi": "one short Vietnamese note on when and how to use it",
      "example_en": "one NEW English example sentence, not from the video",
      "example_vi": "Vietnamese translation of example_en"
    }}
  ]
}}
Order phrases by t."""


def claude_phrases(segments: list) -> dict:
    transcript = "\n".join(f"[{s['t']}] {s['text']}" for s in segments)
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": CLAUDE_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": CLAUDE_MODEL,
            "max_tokens": 4000,
            "messages": [{"role": "user", "content": PHRASE_PROMPT.format(transcript=transcript)}],
        },
        timeout=300,
    )
    if r.status_code == 429:
        raise RateLimited("Claude: " + r.text[:300])
    if r.status_code != 200:
        raise RuntimeError(f"Claude HTTP {r.status_code}: {r.text[:500]}")

    text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])


def snap_times(result: dict, segments: list) -> dict:
    """Đối chiếu mốc thời gian Claude chọn với lời thoại thật, sửa về câu gần nhất chứa cụm từ."""
    starts = [s["t"] for s in segments]
    phrases = []
    for p in result.get("phrases", []):
        if not p.get("phrase"):
            continue
        try:
            t = int(p.get("t", 0))
        except (TypeError, ValueError):
            t = 0
        quote = (p.get("quote") or "").lower()[:40]
        match = next((s["t"] for s in segments if quote and quote in s["text"].lower()), None)
        if match is None:
            match = min(starts, key=lambda x: abs(x - t))
        p["t"] = match
        phrases.append({k: p.get(k, "") for k in
                        ["t", "phrase", "quote", "meaning_vi", "usage_vi", "example_en", "example_vi"]})
    phrases.sort(key=lambda p: p["t"])
    return {"summary_vi": result.get("summary_vi", ""), "level": result.get("level", ""), "phrases": phrases}


# ---------------------------------------------------------------- Chạy
def main() -> int:
    missing = [n for n, v in [("GEMINI_API_KEY", GEMINI_KEY), ("ANTHROPIC_API_KEY", CLAUDE_KEY)] if not v]
    if missing:
        print("Thiếu khóa: " + ", ".join(missing) + ". Hãy kiểm tra GitHub Secrets.")
        return 1

    ids = json.loads(LIST_FILE.read_text(encoding="utf-8")).get("videos", [])
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    todo = [v for v in ids if not (OUT_DIR / f"{v}.json").exists()]
    print(f"Gemini: {GEMINI_MODEL}, Claude: {CLAUDE_MODEL}. "
          f"Có {len(ids)} video, cần soạn {len(todo)}, lần này tối đa {MAX_PER_RUN}.")

    done = 0
    for vid in todo[:MAX_PER_RUN]:
        print(f"→ {vid}")
        meta = {"id": vid, "created": time.strftime("%Y-%m-%d"),
                "models": {"transcript": GEMINI_MODEL, "phrases": CLAUDE_MODEL}}
        try:
            segments = gemini_transcript(vid)
            print(f"  Lời thoại: {len(segments)} câu")
            result = snap_times(claude_phrases(segments), segments)
            print(f"  Cụm từ: {len(result['phrases'])}")
            out = {**meta, **result}
        except RateLimited as e:
            print(f"  Hết hạn mức tạm thời, dừng và để lần sau làm tiếp. {e}")
            break
        except Exception as e:
            # Lưu lỗi để không thử lại mãi một video hỏng
            print(f"  Lỗi: {e}")
            out = {**meta, "error": str(e)[:300], "summary_vi": "", "level": "", "phrases": []}
        (OUT_DIR / f"{vid}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        done += 1
        time.sleep(15)

    print(f"Xong {done} video.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
