"""
HookFinder worker.

Polls Supabase `analysis_jobs` for queued rows. For each one:
  1. Runs `yt-dlp` to download English auto-subs as SRT (no video download).
  2. Parses SRT into timestamped segments (preserving start/end times).
  3. Chunks long transcripts and POSTs each chunk to the `analyze-transcript`
     Supabase edge function, which runs the JohnBot prompt against the Lovable
     AI Gateway using the managed LOVABLE_API_KEY (kept inside Lovable Cloud).
  4. Writes the result JSON back to the job row.

Required env vars:
  SUPABASE_URL                  e.g. https://xxxx.supabase.co
  SUPABASE_SERVICE_ROLE_KEY     service role key (server-side only)
  WORKER_ID                     optional, defaults to "worker-1"
  POLL_INTERVAL_SECONDS         optional, defaults to 3
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import requests
from supabase import Client, create_client

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
WORKER_ID = os.environ.get("WORKER_ID", f"worker-{socket.gethostname()}-{uuid.uuid4().hex[:6]}")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL_SECONDS", "3"))

ANALYZE_FN_URL = f"{SUPABASE_URL}/functions/v1/analyze-transcript"


# ---------- yt-dlp transcript fetch ---------------------------------------

def run_ytdlp(url: str, workdir: Path) -> tuple[Path | None, str]:
    """Run yt-dlp and return (srt_path, video_title)."""
    out_template = str(workdir / "%(title)s [%(id)s].%(ext)s")
    cmd = [
        "yt-dlp",
        "--no-overwrites",
        "--skip-download",
        "--write-auto-subs",
        "--write-subs",
        "--sub-langs", "en.*,en",
        "--convert-subs", "srt",
        "--no-playlist",
        "-o", out_template,
        "--print", "%(title)s",
        url,
    ]
    print(f"[yt-dlp] {' '.join(cmd)}", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    print(f"[yt-dlp] rc={proc.returncode}", flush=True)
    if proc.stdout:
        print(f"[yt-dlp stdout] {proc.stdout[:2000]}", flush=True)
    if proc.stderr:
        print(f"[yt-dlp stderr] {proc.stderr[:2000]}", flush=True)

    title = (proc.stdout.strip().splitlines() or ["Untitled video"])[0]
    srts = sorted(workdir.glob("*.srt"))
    if not srts:
        return None, title
    eng = [p for p in srts if ".en" in p.name.lower()]
    return (eng[0] if eng else srts[0]), title


SRT_BLOCK_RE = re.compile(
    r"(?P<idx>\d+)\s*\n"
    r"(?P<start>\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(?P<end>\d{2}:\d{2}:\d{2}[,.]\d{3})\s*\n"
    r"(?P<text>(?:.+\n?)+?)(?:\n\s*\n|\Z)",
    re.MULTILINE,
)


def srt_time_to_seconds(t: str) -> float:
    t = t.replace(",", ".")
    h, m, rest = t.split(":")
    s = float(rest)
    return int(h) * 3600 + int(m) * 60 + s


def parse_srt(srt_text: str) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    seen_text: set[str] = set()
    for m in SRT_BLOCK_RE.finditer(srt_text):
        text_lines = [
            re.sub(r"<[^>]+>", "", ln).strip()
            for ln in m.group("text").splitlines()
        ]
        text = " ".join([ln for ln in text_lines if ln]).strip()
        if not text:
            continue
        if text in seen_text:
            continue
        seen_text.add(text)
        start = srt_time_to_seconds(m.group("start"))
        end = srt_time_to_seconds(m.group("end"))
        segments.append({
            "text": text,
            "start": start,
            "duration": max(0.0, end - start),
        })
    return segments


# ---------- manual transcript parsing (paste-in fallback) -----------------

MANUAL_TS_RE = re.compile(r"^\[?((?:\d{1,2}:)?\d{1,2}:\d{2})\]?\s*[-:]?\s*(.*)$")


def parse_manual_transcript(raw: str) -> tuple[list[dict[str, Any]], bool]:
    segments: list[dict[str, Any]] = []
    had_ts = False
    pending_ts: float | None = None
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = MANUAL_TS_RE.match(line)
        if m and (m.group(2) or re.match(r"^\d", line)):
            had_ts = True
            parts = [int(p) for p in m.group(1).split(":")]
            if len(parts) == 3:
                secs = parts[0] * 3600 + parts[1] * 60 + parts[2]
            else:
                secs = parts[0] * 60 + parts[1]
            text = (m.group(2) or "").strip()
            if text:
                segments.append({"text": text, "start": float(secs), "duration": 0.0})
            else:
                pending_ts = float(secs)
            continue
        if pending_ts is not None:
            segments.append({"text": line, "start": pending_ts, "duration": 0.0})
            pending_ts = None
        else:
            last = segments[-1] if segments else None
            start = (last["start"] + 5.0) if last else 0.0
            segments.append({"text": line, "start": start, "duration": 5.0})
    return segments, had_ts


# ---------- transcript shaping --------------------------------------------

def fmt_ts(s: float) -> str:
    s = int(s)
    h, rem = divmod(s, 3600)
    m, ss = divmod(rem, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{ss:02d}"
    return f"{m:02d}:{ss:02d}"


def chunk_transcript_to_text(segments: list[dict[str, Any]], window_seconds: int = 30) -> str:
    if not segments:
        return ""
    lines: list[str] = []
    buf: list[str] = []
    window_start = segments[0]["start"]
    for seg in segments:
        if seg["start"] - window_start >= window_seconds and buf:
            lines.append(f"[{fmt_ts(window_start)}] {' '.join(buf)}")
            buf = []
            window_start = seg["start"]
        buf.append(seg["text"])
    if buf:
        lines.append(f"[{fmt_ts(window_start)}] {' '.join(buf)}")
    return "\n".join(lines)


def split_into_chunks(segments: list[dict[str, Any]], chunk_seconds: int) -> list[list[dict[str, Any]]]:
    if not segments:
        return []
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    chunk_start = segments[0]["start"]
    for seg in segments:
        if seg["start"] - chunk_start >= chunk_seconds and current:
            chunks.append(current)
            current = []
            chunk_start = seg["start"]
        current.append(seg)
    if current:
        chunks.append(current)
    return chunks


# ---------- analyze-transcript edge function call -------------------------

def call_analyze_fn(mode: str, user_content: str) -> list[dict[str, Any]]:
    """POST a transcript chunk to the analyze-transcript edge function.

    The edge function holds the JohnBot prompt and calls the Lovable AI
    Gateway using the managed LOVABLE_API_KEY (auto-injected inside Lovable
    Cloud). The worker only needs the Supabase service role key.
    """
    res = requests.post(
        ANALYZE_FN_URL,
        headers={
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "Content-Type": "application/json",
        },
        json={"mode": mode, "userContent": user_content},
        timeout=300,
    )
    if res.status_code == 429:
        raise RuntimeError("rate_limit")
    if res.status_code == 402:
        raise RuntimeError("payment_required")
    if not res.ok:
        print(f"[analyze-fn] error {res.status_code}: {res.text[:1000]}", flush=True)
        raise RuntimeError("analyze_fn_error")
    data = res.json()
    return data.get("clips", []) or []


def analyze(segments: list[dict[str, Any]], title: str, set_progress) -> list[dict[str, Any]]:
    transcript_text = chunk_transcript_to_text(segments, 30)
    total_chars = len(transcript_text)
    last = segments[-1]
    total_seconds = last["start"] + last.get("duration", 0)
    print(f"[analyze] segments={len(segments)} chars={total_chars} duration={fmt_ts(total_seconds)}", flush=True)

    needs_chunking = total_chars > 80_000 or total_seconds > 35 * 60
    if not needs_chunking:
        set_progress("Analyzing transcript with JohnBot…")
        return call_analyze_fn(
            "single",
            f"Video title: {title}\n\nTranscript (timestamps in [MM:SS] or [HH:MM:SS]):\n\n{transcript_text}",
        )

    CHUNK_SECONDS = 12 * 60
    chunks = split_into_chunks(segments, CHUNK_SECONDS)
    print(f"[analyze] chunked two-pass over {len(chunks)} chunks", flush=True)

    candidates: list[dict[str, Any]] = []
    for idx, chunk_segs in enumerate(chunks, 1):
        set_progress(f"Analyzing chunk {idx}/{len(chunks)}…")
        text = chunk_transcript_to_text(chunk_segs, 30)
        start_ts = fmt_ts(chunk_segs[0]["start"])
        end_ts = fmt_ts(chunk_segs[-1]["start"] + chunk_segs[-1].get("duration", 0))
        user_content = (
            f"Video title: {title}\n"
            f"Chunk {idx} of {len(chunks)} (covers {start_ts}–{end_ts} of the full video).\n"
            "Use the absolute [HH:MM:SS] or [MM:SS] timestamps exactly as shown.\n\n"
            f"Transcript:\n\n{text}"
        )
        try:
            clips = call_analyze_fn("chunk", user_content)
            print(f"[analyze] chunk {idx}: {len(clips)} candidates", flush=True)
            candidates.extend(clips)
        except Exception as e:
            print(f"[analyze] chunk {idx} failed: {e}", flush=True)

    if not candidates:
        return []

    set_progress(f"Ranking {len(candidates)} candidates into the final cut…")
    try:
        return call_analyze_fn(
            "final",
            f"Video title: {title}\nCandidate pool (already extracted from chunks):\n\n{json.dumps(candidates, indent=2)}",
        )
    except Exception as e:
        print(f"[analyze] final rank failed, returning top {min(7, len(candidates))} raw: {e}", flush=True)
        return candidates[:7]


# ---------- video id helper -----------------------------------------------

VIDEO_ID_RES = [
    re.compile(r"(?:youtube\.com/watch\?(?:[^#]*&)?v=|youtu\.be/|youtube\.com/live/|youtube\.com/embed/|youtube\.com/shorts/|youtube\.com/v/)([a-zA-Z0-9_-]{11})"),
]


def extract_video_id(url: str) -> str | None:
    for r in VIDEO_ID_RES:
        m = r.search(url)
        if m:
            return m.group(1)
    return None


# ---------- job loop ------------------------------------------------------

def claim_one_job(supabase: Client) -> dict[str, Any] | None:
    res = (
        supabase.table("analysis_jobs")
        .select("id")
        .eq("status", "queued")
        .order("created_at", desc=False)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    if not rows:
        return None
    job_id = rows[0]["id"]
    upd = (
        supabase.table("analysis_jobs")
        .update({
            "status": "running",
            "worker_id": WORKER_ID,
            "claimed_at": "now()",
            "progress": "Worker picked up your job…",
        })
        .eq("id", job_id)
        .eq("status", "queued")
        .select("*")
        .execute()
    )
    if not upd.data:
        return None
    return upd.data[0]


def update_job(supabase: Client, job_id: str, fields: dict[str, Any]) -> None:
    try:
        supabase.table("analysis_jobs").update(fields).eq("id", job_id).execute()
    except Exception as e:
        print(f"[job {job_id}] update failed: {e}", flush=True)


def process_job(supabase: Client, job: dict[str, Any]) -> None:
    job_id = job["id"]
    url: str | None = job.get("url")
    manual: str | None = job.get("manual_transcript")

    def set_progress(msg: str) -> None:
        print(f"[job {job_id}] {msg}", flush=True)
        update_job(supabase, job_id, {"progress": msg})

    try:
        if manual and manual.strip():
            set_progress("Parsing pasted transcript…")
            segments, _ = parse_manual_transcript(manual)
            title = "Pasted transcript"
            video_id = extract_video_id(url) if url else None
        else:
            if not url:
                raise RuntimeError("No URL and no manual transcript on this job.")
            video_id = extract_video_id(url)
            set_progress("Fetching transcript via yt-dlp…")
            with tempfile.TemporaryDirectory() as td:
                workdir = Path(td)
                srt_path, title = run_ytdlp(url, workdir)
                if not srt_path:
                    raise RuntimeError(
                        "yt-dlp couldn't pull a transcript for this video. "
                        "If YouTube shows a transcript, paste it manually below and HookFinder will analyze it."
                    )
                set_progress("Parsing SRT into timestamped segments…")
                segments = parse_srt(srt_path.read_text(encoding="utf-8", errors="ignore"))

        if not segments:
            raise RuntimeError(
                "Transcript came through empty. Try pasting it manually from YouTube's transcript panel."
            )

        last = segments[-1]
        total_seconds = last["start"] + last.get("duration", 0)

        clips = analyze(segments, title, set_progress)

        result = {
            "videoId": video_id,
            "title": title,
            "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else None,
            "durationSeconds": total_seconds,
            "clips": clips,
        }
        update_job(supabase, job_id, {
            "status": "done",
            "progress": "Done.",
            "result": result,
        })
        print(f"[job {job_id}] done with {len(clips)} clips", flush=True)
    except Exception as e:
        msg = str(e) or "Worker failed unexpectedly."
        print(f"[job {job_id}] ERROR: {msg}", flush=True)
        update_job(supabase, job_id, {
            "status": "error",
            "progress": None,
            "error": msg,
        })


def main() -> None:
    print(f"[worker] starting as {WORKER_ID}, polling every {POLL_INTERVAL}s", flush=True)
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    while True:
        try:
            job = claim_one_job(supabase)
            if job:
                process_job(supabase, job)
            else:
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            print("[worker] stopping", flush=True)
            sys.exit(0)
        except Exception as e:
            print(f"[worker] loop error: {e}", flush=True)
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
