#!/usr/bin/env python3
"""video_worker.py — DeepSearch Pro learning-video worker (home server).

Polls the Render service's job queue over outbound HTTPS (the home
server is never exposed to the internet — no inbound port, no public
URL), processes one video at a time, and posts the signed result back:

  1. Transcript — YouTube captions via yt-dlp (free); faster-whisper
     on the local GPU/CPU when captions are missing.
  2. Summary + chapters — local llama3.1:8b via Ollama ($0).
  3. Claims — the same model extracts verifiable claims, then each
     claim is checked against DeepSearch Pro's own search engines and
     judged SUPPORTED / CONTRADICTED / UNVERIFIED, always with
     sources and the timestamp in the video. Never a binary verdict.

Auth: every request to Render is signed with the worker Ed25519 key
(the verifier is video_auth.py in the Render repo; the canonical
message format below must match it exactly). The seed lives only in
~/.config/deepsearch-pro/video-worker-key.json (mode 600).

No ad-blocking, no stream stripping, no playback circumvention of any
kind — the worker only reads captions/audio that YouTube serves.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, "/home/cliodonvito/clio-work")
sys.path.insert(0, "/home/cliodonvito/clio-work/axiom")

import ed25519  # noqa: E402  (pure-Python RFC 8032, from Axiom-0)
from search_engines import (search_all, WEB_ENGINES,  # noqa: E402
                            ACADEMIC_ENGINES, NEWS_ENGINES)

API_BASE = os.environ.get("VIDEO_API_BASE",
                          "https://deepsearch-pro.onrender.com").rstrip("/")
KEY_FILE = os.path.expanduser(
    "~/.config/deepsearch-pro/video-worker-key.json")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
POLL_INTERVAL = int(os.environ.get("VIDEO_POLL_INTERVAL", "30"))
MAX_DURATION_SECONDS = 3600  # 60 min: longer videos are refused
MAX_CLAIMS = 8
WORK_DIR = "/tmp/video-worker"
MESSAGE_PREFIX = "deepsearch-pro-video-v1"
SIG_WINDOW = 300


def log(msg: str):
    print(f"{datetime.now(timezone.utc).isoformat()} {msg}", flush=True)


# ------------------------------------------------------------------ auth

def _load_seed() -> str:
    with open(KEY_FILE, encoding="utf-8") as f:
        return json.load(f)["seed_hex"]


SEED_HEX = _load_seed()
PUBKEY_HEX = ed25519.publickey(bytes.fromhex(SEED_HEX)).hex()


def _canonical(method: str, job_id: str, timestamp: int,
               body_sha256: str) -> bytes:
    return ("\n".join([MESSAGE_PREFIX, method, job_id or "-",
                       str(timestamp), body_sha256 or "-"])).encode()


def _signed_headers(method: str, job_id: str, body: bytes) -> dict:
    ts = int(time.time())
    body_hash = hashlib.sha256(body or b"").hexdigest()
    msg = _canonical(method, job_id, ts, body_hash)
    seed = bytes.fromhex(SEED_HEX)
    sig = ed25519.sign(msg, seed, ed25519.publickey(seed)).hex()
    return {
        "X-Worker-Timestamp": str(ts),
        "X-Worker-Signature": sig,
        "X-Worker-Pubkey": PUBKEY_HEX,
    }


def _http(method: str, path: str, body: bytes = b"",
          headers: dict = None, timeout: int = 120):
    req = urllib.request.Request(
        API_BASE + path, data=body if method == "POST" else None,
        headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def poll_pending():
    """Claim the next job (or None). Returns the job dict or None."""
    headers = _signed_headers("pending", "-", b"")
    try:
        _status, raw = _http("GET", "/api/v1/video/jobs/pending",
                             headers=headers, timeout=120)
        return json.loads(raw.decode()).get("job")
    except urllib.error.HTTPError as e:
        log(f"pending: HTTP {e.code}")
    except Exception as e:
        log(f"pending: {e}")
    return None


def post_complete(job_id: str, result: dict):
    body = json.dumps(result).encode()
    headers = _signed_headers("complete", job_id, body)
    headers["Content-Type"] = "application/json"
    _status, _raw = _http(
        "POST", f"/api/v1/video/jobs/{job_id}/complete",
        body=body, headers=headers, timeout=60)
    log(f"job {job_id}: result posted")


def post_fail(job_id: str, error: str):
    body = json.dumps({"error": error}).encode()
    headers = _signed_headers("fail", job_id, body)
    headers["Content-Type"] = "application/json"
    try:
        _status, _raw = _http(
            "POST", f"/api/v1/video/jobs/{job_id}/fail",
            body=body, headers=headers, timeout=60)
        log(f"job {job_id}: failure posted: {error[:120]}")
    except Exception as e:
        log(f"job {job_id}: could not post failure: {e}")


# -------------------------------------------------------------- transcript

def _run(cmd, timeout):
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout)


def video_duration(url: str):
    """Video length in seconds (None if unknown/live)."""
    r = _run([sys.executable, "-m", "yt_dlp", "--no-warnings",
              "--print", "%(duration)s", url], timeout=120)
    out = (r.stdout or "").strip()
    try:
        return int(float(out))
    except (ValueError, TypeError):
        return None


def _parse_json3(path: str):
    """Parse a yt-dlp json3 subtitle file into segments."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    segments = []
    for ev in data.get("events", []):
        segs = ev.get("segs") or []
        text = "".join(s.get("utf8", "") for s in segs).strip()
        if not text:
            continue
        start = (ev.get("tStartMs", 0) or 0) / 1000.0
        segments.append({"start": round(start, 1), "text": text})
    return _merge_segments(segments)


def _merge_segments(segments, max_gap=1.0, max_words=40):
    """Merge caption events into readable ~10s chunks."""
    merged = []
    cur = None
    for seg in segments:
        if cur is None:
            cur = dict(seg)
            continue
        gap = seg["start"] - cur["start"]
        words = len(cur["text"].split())
        if gap <= 12 and words < max_words:
            cur["text"] = (cur["text"] + " " + seg["text"]).strip()
        else:
            merged.append(cur)
            cur = dict(seg)
    if cur:
        merged.append(cur)
    return merged


def fetch_captions(url: str, video_id: str, workdir: str):
    """YouTube captions via yt-dlp. English first, any language next.

    Returns (segments, language) or (None, None).
    """
    for langs in ("en", "all"):
        outtmpl = os.path.join(workdir, "%(id)s.%(ext)s")
        cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings",
               "--skip-download", "--write-subs", "--write-auto-subs",
               "--sub-langs", langs, "--sub-format", "json3",
               "-o", outtmpl, url]
        r = _run(cmd, timeout=180)
        if r.returncode != 0:
            log(f"captions ({langs}): yt-dlp rc={r.returncode}")
            continue
        files = sorted(f for f in os.listdir(workdir)
                       if f.endswith(".json3"))
        if not files:
            continue
        # Prefer an English file when several languages came back.
        pick = next((f for f in files if ".en." in f), files[0])
        lang = pick.split(".")[-2] if "." in pick else "?"
        try:
            segments = _parse_json3(os.path.join(workdir, pick))
        except Exception as e:
            log(f"captions: could not parse {pick}: {e}")
            continue
        if segments:
            return segments, lang
    return None, None


def transcribe_audio(url: str, video_id: str, workdir: str):
    """Whisper fallback: download audio, transcribe with faster-whisper.

    Returns segments or None. Runs on CPU (int8) — slower than the
    GPU path, but captions cover most videos so this is the fallback.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log("whisper: faster-whisper not installed")
        return None
    audio = os.path.join(workdir, "audio.%(ext)s")
    r = _run([sys.executable, "-m", "yt_dlp", "--no-warnings",
              "-f", "bestaudio", "-o", audio, url], timeout=600)
    if r.returncode != 0:
        log(f"whisper: audio download rc={r.returncode}")
        return None
    files = [f for f in os.listdir(workdir)
             if f.startswith("audio.")]
    if not files:
        return None
    model = WhisperModel("base", device="cpu", compute_type="int8")
    segments_out, _info = model.transcribe(
        os.path.join(workdir, files[0]), beam_size=1,
        word_timestamps=True, language="en")
    segments = []
    for seg in segments_out:
        segments.append({"start": round(seg.start, 1),
                         "text": seg.text.strip()})
    return _merge_segments([s for s in segments if s["text"]])


# ----------------------------------------------------------------- ollama

def ollama(prompt: str, timeout: int = 600) -> str:
    """One non-streaming generation from the local model."""
    payload = json.dumps({
        "model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
        "options": {"temperature": 0.2, "num_ctx": 16384},
    }).encode()
    req = urllib.request.Request(
        OLLAMA_URL + "/api/generate", data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode()).get("response", "")


def _extract_json(text: str):
    """Pull the first {...} or [...] out of model output.

    The structure whose opener appears first in the text is tried
    first: a claims list starts with '[', a summary/verdict object
    with '{'. (Trying '{' first always returned only the first claim
    object out of a list — the bug that emptied claim extraction.)
    """
    text = text.strip()
    first_brace = text.find("{")
    first_bracket = text.find("[")
    if first_bracket >= 0 and (first_brace < 0
                               or first_bracket < first_brace):
        order = (("[", "]"), ("{", "}"))
    else:
        order = (("{", "}"), ("[", "]"))
    for opener, closer in order:
        start = text.find(opener)
        if start < 0:
            continue
        depth = 0
        for i in range(start, len(text)):
            if text[i] == opener:
                depth += 1
            elif text[i] == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except ValueError:
                        break
    return None


def summarize(transcript_text: str):
    """Summary + chapters. Returns (summary, chapters)."""
    prompt = (
        "You are summarizing a video transcript for someone who wants "
        "to learn from it. Write a plain-language summary of 3-5 "
        "sentences, then list the video's chapters with their start "
        "times in seconds. Reply with JSON ONLY, no other text:\n"
        '{"summary": "...", "chapters": [{"start": 0, "title": "..."}]}\n\n'
        "TRANSCRIPT (timestamps in seconds, [start] text):\n"
        + transcript_text[:24000])
    out = ollama(prompt)
    data = _extract_json(out)
    if isinstance(data, dict) and data.get("summary"):
        chapters = data.get("chapters") or []
        chapters = [c for c in chapters
                    if isinstance(c, dict) and "title" in c][:10]
        return str(data["summary"]), chapters
    return out.strip()[:2000], []


def extract_claims(transcript_text: str):
    """Verifiable factual claims with timestamps. Max MAX_CLAIMS."""
    prompt = (
        "From this video transcript, extract the verifiable factual "
        "claims — statements that could be checked against a source "
        "(dates, numbers, names, events, scientific facts). Skip "
        "opinions, advice, jokes, greetings, and commentary about the "
        "video itself. Reply with JSON ONLY, a list of at most "
        f"{MAX_CLAIMS} items:\n"
        '[{"claim": "...", "timestamp": 0}]\n\n'
        "TRANSCRIPT (timestamps in seconds, [start] text):\n"
        + transcript_text[:24000])
    out = ollama(prompt)
    data = _extract_json(out)
    if isinstance(data, list):
        claims = []
        for item in data[:MAX_CLAIMS]:
            if isinstance(item, dict) and item.get("claim"):
                claims.append({
                    "claim": str(item["claim"])[:500],
                    "timestamp": float(item.get("timestamp") or 0),
                })
        return claims
    return []


def verify_claim(claim: dict):
    """Check one claim against DeepSearch Pro's engines.

    Returns {claim, timestamp, verdict, reason, sources}. Sources
    come only from the search results — never invented.
    """
    text = claim["claim"]
    try:
        results = search_all(
            text, engines=WEB_ENGINES + ACADEMIC_ENGINES + NEWS_ENGINES,
            max_per_engine=3)[:5]
    except Exception as e:
        log(f"verify: search failed for {text[:60]!r}: {e}")
        results = []
    if not results:
        return {**claim, "verdict": "UNVERIFIED",
                "reason": "No search results spoke to this claim.",
                "sources": []}
    evidence = "\n".join(
        f"- {r.get('title', '')} ({r.get('url', '')}): "
        f"{r.get('snippet', '')[:300]}" for r in results)
    prompt = (
        "Judge this claim against the search results below. Verdicts:\n"
        "- SUPPORTED: a result directly confirms the claim.\n"
        "- CONTRADICTED: a result directly refutes the claim.\n"
        "- UNVERIFIED: no result confirms or refutes it.\n"
        "Reply with JSON ONLY:\n"
        '{"verdict": "SUPPORTED", "reason": "one sentence", '
        '"sources": [{"title": "...", "url": "..."}]}\n'
        "Use ONLY the URLs given below as sources. If none speaks to "
        "the claim, say UNVERIFIED with no sources.\n\n"
        f"CLAIM: {text}\n\nSEARCH RESULTS:\n{evidence}")
    try:
        out = ollama(prompt)
        data = _extract_json(out)
    except Exception as e:
        log(f"verify: ollama failed for {text[:60]!r}: {e}")
        data = None
    if isinstance(data, dict) and data.get("verdict") in (
            "SUPPORTED", "CONTRADICTED", "UNVERIFIED"):
        sources = []
        allowed = {r.get("url", "") for r in results}
        for s in data.get("sources") or []:
            if isinstance(s, dict) and s.get("url") in allowed:
                sources.append({"title": str(s.get("title") or s["url"]),
                                "url": s["url"]})
        verdict = data["verdict"]
        if verdict in ("SUPPORTED", "CONTRADICTED") and not sources:
            verdict = "UNVERIFIED"
        return {**claim, "verdict": verdict,
                "reason": str(data.get("reason") or "")[:300],
                "sources": sources[:3]}
    return {**claim, "verdict": "UNVERIFIED",
            "reason": "The check did not return a usable verdict.",
            "sources": []}


# ------------------------------------------------------------------- jobs

def process_job(job: dict) -> dict:
    """Full pipeline for one job. Returns the result dict for Render."""
    job_id = job["job_id"]
    video_id = job["video_id"]
    url = job["url"]
    log(f"job {job_id}: {video_id} {url}")
    workdir = os.path.join(WORK_DIR, job_id)
    os.makedirs(workdir, exist_ok=True)
    try:
        duration = video_duration(url)
        if duration is not None and duration > MAX_DURATION_SECONDS:
            raise ValueError(
                f"Video is {duration // 60} minutes — over the "
                "60-minute limit for this service.")
        segments, lang = fetch_captions(url, video_id, workdir)
        source = "captions"
        if not segments:
            log(f"job {job_id}: no captions, falling back to Whisper")
            segments = transcribe_audio(url, video_id, workdir)
            source = "whisper"
        if not segments:
            raise ValueError(
                "No transcript available: this video has no captions "
                "and audio transcription failed.")
        log(f"job {job_id}: transcript via {source}, "
            f"{len(segments)} segments")
        text_lines = [f"[{int(s['start'])}] {s['text']}"
                      for s in segments]
        full_text = " ".join(s["text"] for s in segments)
        stamped = "\n".join(text_lines)
        summary, chapters = summarize(stamped)
        log(f"job {job_id}: summary done, {len(chapters)} chapters")
        claims = extract_claims(stamped)
        log(f"job {job_id}: {len(claims)} claims extracted")
        checked = []
        for claim in claims:
            checked.append(verify_claim(claim))
            log(f"job {job_id}: claim -> "
                f"{checked[-1]['verdict']}: {claim['claim'][:60]}")
        counts = {}
        for c in checked:
            counts[c["verdict"]] = counts.get(c["verdict"], 0) + 1
        log(f"job {job_id}: verdicts {counts}")
        return {
            "transcript": {"text": full_text, "segments": segments,
                           "source": source, "language": lang},
            "summary": summary,
            "chapters": chapters,
            "claims": checked,
            "duration_seconds": duration,
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main():
    log(f"video worker starting; API base {API_BASE}")
    os.makedirs(WORK_DIR, exist_ok=True)
    while True:
        try:
            job = poll_pending()
        except Exception as e:
            log(f"poll error: {e}")
            job = None
        if not job:
            time.sleep(POLL_INTERVAL)
            continue
        job_id = job["job_id"]
        try:
            result = process_job(job)
            post_complete(job_id, result)
        except Exception as e:
            log(f"job {job_id}: FAILED: {e}")
            post_fail(job_id, str(e)[:500])
        time.sleep(2)


if __name__ == "__main__":
    main()
