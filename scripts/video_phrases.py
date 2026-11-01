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


class Temporary(Exception):
    """Lỗi tạm thời (máy chủ quá tải...): không lưu file, lần chạy sau tự thử lại."""
    pass


class Recitation(Exception):
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


# Dùng khi Gemini từ chối chép nguyên văn (RECITATION): chỉ trích các câu ngắn chứa cách diễn đạt đáng học
EXCERPT_PROMPT = """Listen to this video. List 25 to 35 short excerpts where the speaker uses a useful or natural English expression
(idioms, collocations, phrasal verbs, discourse markers). Spread them across the whole video.
For each excerpt give t = the second (integer) when it begins, and text = the speaker's words, at most 12 words.
Do not output a full transcript."""


def _gemini_call(video_id: str, prompt: str, low_res: bool = True) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    body = {
        "contents": [{"parts": [
            {"file_data": {"file_uri": f"https://www.youtube.com/watch?v={video_id}"}},
            {"text": prompt},
        ]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": TRANSCRIPT_SCHEMA,
        },
    }
    if low_res:
        # Hình độ phân giải thấp để tốn ít token, âm thanh vẫn đầy đủ
        body["generationConfig"]["mediaResolution"] = "MEDIA_RESOLUTION_LOW"

    for attempt in range(3):
        r = requests.post(url, headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=600)
        if r.status_code in (500, 502, 503, 504):
            wait = 30 * (attempt + 1)
            print(f"  Gemini đang bận ({r.status_code}), thử lại sau {wait} giây…")
            time.sleep(wait)
            continue
        break
    else:
        raise Temporary(f"Gemini quá tải ({r.status_code})")

    if r.status_code == 429:
        raise RateLimited("Gemini: " + r.text[:300])
    if r.status_code == 400 and low_res and "mediaResolution" in r.text:
        return _gemini_call(video_id, prompt, low_res=False)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:500]}")

    data = r.json()
    cand = (data.get("candidates") or [{}])[0]
    if cand.get("finishReason") == "RECITATION":
        raise Recitation()
    try:
        return cand["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise RuntimeError(f"Gemini không trả nội dung: {json.dumps(data)[:500]}")


def gemini_transcript(video_id: str):
    """Trả về (danh sách câu, chế độ). Chế độ 'full' = lời thoại đầy đủ, 'excerpts' = trích đoạn ngắn."""
    mode = "full"
    try:
        text = _gemini_call(video_id, TRANSCRIPT_PROMPT)
    except Recitation:
        print("  Gemini không chép nguyên văn được (bản quyền), chuyển sang trích đoạn ngắn")
        mode = "excerpts"
        try:
            text = _gemini_call(video_id, EXCERPT_PROMPT)
        except Recitation:
            raise RuntimeError("Gemini từ chối cả trích đoạn ngắn vì bản quyền")

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
    return segs, mode


# ---------------------------------------------------------------- Bước 2: Claude chọn cụm từ
PHRASE_PROMPT = """You are an English teacher helping a Vietnamese professional (upper-intermediate level) learn natural English from a video.

Below is the video transcript. Each line starts with [t] = the second when that sentence begins.

<transcript>
{transcript}
</transcript>

Choose 10 to 14 useful, natural expressions that appear in the transcript: idioms, collocations, phrasal verbs, discourse markers, and phrases a learner would want to reuse at work or in daily life. Skip very basic words. Spread them across the whole video.

{note}
Save the result with the save_phrases tool. For each phrase, t must be the [t] of the line containing it, and quote must be copied exactly from that line (at most 20 words). Keep Vietnamese notes short. Order phrases by t."""

PHRASE_TOOL = {
    "name": "save_phrases",
    "description": "Save the selected expressions for this video.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary_vi": {"type": "string", "description": "2-sentence Vietnamese summary of the video"},
            "level": {"type": "string", "enum": ["B1", "B2", "C1", "C2"]},
            "phrases": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "t": {"type": "integer"},
                        "phrase": {"type": "string", "description": "expression in base form"},
                        "quote": {"type": "string"},
                        "meaning_vi": {"type": "string", "description": "short Vietnamese meaning"},
                        "usage_vi": {"type": "string", "description": "one short Vietnamese note on when/how to use it"},
                        "example_en": {"type": "string", "description": "a NEW English example, not from the video"},
                        "example_vi": {"type": "string", "description": "Vietnamese translation of example_en"},
                    },
                    "required": ["t", "phrase", "quote", "meaning_vi", "usage_vi", "example_en", "example_vi"],
                },
            },
        },
        "required": ["summary_vi", "level", "phrases"],
    },
}


def claude_phrases(segments: list, mode: str = "full") -> dict:
    transcript = "\n".join(f"[{s['t']}] {s['text']}" for s in segments)
    note = ("" if mode == "full" else
            "Note: these are only short excerpts from the video, not the full transcript. "
            "Base the summary on what the excerpts suggest and keep it general.\n")
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": CLAUDE_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": CLAUDE_MODEL,
            "max_tokens": 12000,
            "tools": [PHRASE_TOOL],
            "tool_choice": {"type": "tool", "name": "save_phrases"},
            "messages": [{"role": "user", "content": PHRASE_PROMPT.format(transcript=transcript, note=note)}],
        },
        timeout=300,
    )
    if r.status_code == 429:
        raise RateLimited("Claude: " + r.text[:300])
    if r.status_code in (500, 529):
        raise Temporary(f"Claude quá tải ({r.status_code})")
    if r.status_code != 200:
        raise RuntimeError(f"Claude HTTP {r.status_code}: {r.text[:500]}")

    data = r.json()
    if data.get("stop_reason") == "max_tokens":
        raise RuntimeError("Câu trả lời của Claude quá dài, bị cắt")
    for block in data.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "save_phrases":
            return block.get("input", {})
    raise RuntimeError("Claude không trả kết quả qua save_phrases")


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
    def needs_work(vid):
        f = OUT_DIR / f"{vid}.json"
        if not f.exists():
            return True
        try:
            old = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return True
        return bool(old.get("error")) and old.get("attempts", 1) < 3

    todo = [v for v in ids if needs_work(v)]
    print(f"Gemini: {GEMINI_MODEL}, Claude: {CLAUDE_MODEL}. "
          f"Có {len(ids)} video, cần soạn {len(todo)}, lần này tối đa {MAX_PER_RUN}.")

    done = 0
    for vid in todo[:MAX_PER_RUN]:
        print(f"→ {vid}")
        f = OUT_DIR / f"{vid}.json"
        attempts = 1
        if f.exists():
            try:
                attempts = json.loads(f.read_text(encoding="utf-8")).get("attempts", 1) + 1
            except Exception:
                pass
        meta = {"id": vid, "created": time.strftime("%Y-%m-%d"), "attempts": attempts,
                "models": {"transcript": GEMINI_MODEL, "phrases": CLAUDE_MODEL}}
        try:
            segments, mode = gemini_transcript(vid)
            print(f"  {'Lời thoại' if mode == 'full' else 'Trích đoạn'}: {len(segments)} câu")
            result = snap_times(claude_phrases(segments, mode), segments)
            print(f"  Cụm từ: {len(result['phrases'])}")
            out = {**meta, "source": mode, **result}
        except RateLimited as e:
            print(f"  Hết hạn mức tạm thời, dừng và để lần sau làm tiếp. {e}")
            break
        except Temporary as e:
            print(f"  {e}. Bỏ qua, lần chạy sau sẽ thử lại.")
            time.sleep(15)
            continue
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
