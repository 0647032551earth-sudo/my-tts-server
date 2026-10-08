# NOVEL AUDIO FACTORY V7 UNIVERSAL
# Streamlit app for crawling novel chapters -> Thai translation -> Edge TTS -> batch MP3 + ZIP
#
# V6 improvements:
# - Live progress / current set progress
# - Elapsed time and dynamic ETA in seconds + readable format
# - Overall progress
# - Background worker so Streamlit UI can keep polling progress
# - Separate bounded concurrency for fetch/translation/TTS stages
# - Thread-safe checkpoint/progress writes
# - Retry with backoff
# - MP3 validation + self-healing
# - Batch-by-batch automatic continuation
# - Resume after refresh/restart
# - No artificial fixed delay between successful jobs
#
# Requirements:
#   pip install streamlit requests beautifulsoup4 edge-tts
#   ffmpeg must be installed and available in PATH
#
# Run:
#   streamlit run novel_audio_factory_v7.py

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
import streamlit as st
from bs4 import BeautifulSoup
import edge_tts


# ============================================================
# CONFIG
# ============================================================

APP_VERSION = "V7.0"
VOICE = "th-TH-PremwadeeNeural"

BASE_DIR = Path(__file__).resolve().parent / "novel_audio_jobs"
BASE_DIR.mkdir(parents=True, exist_ok=True)

FETCH_WORKERS = 6
TRANSLATE_WORKERS = 4
TTS_WORKERS = 6

MAX_RETRIES = 4
REQUEST_TIMEOUT = 25
TRANSLATE_TIMEOUT = 45

MIN_TEXT_CHARS = 80
MIN_MP3_BYTES = 1500

# Google Translate unofficial endpoint. It may change/rate-limit.
TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36"
)


# ============================================================
# GLOBAL STATE
# ============================================================

JOB_THREADS = {}
JOB_THREADS_LOCK = threading.Lock()

CHECKPOINT_LOCKS = {}
CHECKPOINT_LOCKS_LOCK = threading.Lock()


def get_job_lock(job_id: str) -> threading.Lock:
    with CHECKPOINT_LOCKS_LOCK:
        if job_id not in CHECKPOINT_LOCKS:
            CHECKPOINT_LOCKS[job_id] = threading.Lock()
        return CHECKPOINT_LOCKS[job_id]


# ============================================================
# BASIC HELPERS
# ============================================================

def now_ts() -> float:
    return time.time()


def format_duration(seconds) -> str:
    if seconds is None:
        return "--:--"

    try:
        seconds = max(0, int(seconds))
    except Exception:
        return "--:--"

    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)

    if days:
        return f"{days}d {hours:02d}:{minutes:02d}:{secs:02d}"
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_seconds_exact(seconds) -> str:
    if seconds is None:
        return "กำลังคำนวณ..."
    return f"{max(0, int(seconds)):,} วินาที"


def safe_filename(name: str, max_len: int = 90) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "_", name or "")
    name = re.sub(r"\s+", " ", name).strip()
    return name[:max_len] or "novel"


def atomic_write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def update_state(job_id: str, updater):
    path = job_dir(job_id) / "state.json"
    lock = get_job_lock(job_id)

    with lock:
        state = read_json(path, {}) or {}
        updater(state)
        atomic_write_json(path, state)
        return state


def read_state(job_id: str) -> dict:
    return read_json(job_dir(job_id) / "state.json", {}) or {}


def job_id_from_inputs(url: str, start: int, end: int, per_set: int) -> str:
    raw = f"{url.strip()}|{start}|{end}|{per_set}|{VOICE}|{APP_VERSION}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def job_dir(job_id: str) -> Path:
    p = BASE_DIR / job_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_dir(job_id: str, name: str) -> Path:
    p = job_dir(job_id) / name
    p.mkdir(parents=True, exist_ok=True)
    return p


# ============================================================
# JOB STATE
# ============================================================

def initial_state(url: str, start: int, end: int, per_set: int) -> dict:
    total = end - start + 1
    sets = (total + per_set - 1) // per_set

    return {
        "version": APP_VERSION,
        "url": url,
        "start_episode": start,
        "end_episode": end,
        "episodes_per_set": per_set,
        "total_episodes": total,
        "total_sets": sets,

        "status": "idle",
        "stage": "waiting",
        "message": "พร้อมเริ่มงาน",

        "current_set": 0,
        "current_set_start": None,
        "current_set_end": None,
        "current_set_total": 0,
        "current_set_completed": 0,
        "current_set_started_at": None,
        "current_set_finished_at": None,
        "current_set_active_seconds": 0,
        "current_set_eta_seconds": None,

        "overall_completed": 0,
        "overall_failed": 0,

        "episode_status": {},
        "episode_titles": {},
        "episode_urls": {},

        "set_outputs": {},

        "job_started_at": None,
        "job_finished_at": None,
        "last_activity_at": None,

        "error": None,
    }


def ensure_state(job_id: str, url: str, start: int, end: int, per_set: int):
    path = job_dir(job_id) / "state.json"
    if not path.exists():
        atomic_write_json(path, initial_state(url, start, end, per_set))


# ============================================================
# HTTP / FETCH
# ============================================================

_thread_local = threading.local()


def get_session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,th;q=0.8",
            "Connection": "keep-alive",
        })
        _thread_local.session = session
    return session


def fetch_html(url: str) -> str:
    session = get_session()
    last_error = None

    for attempt in range(MAX_RETRIES):
        try:
            r = session.get(url, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()

            # requests often detects charset, but some novel sites don't declare it.
            r.encoding = r.apparent_encoding or r.encoding
            text = r.text

            if len(text) < 200:
                raise RuntimeError("หน้าเว็บสั้นผิดปกติ")

            return text

        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                time.sleep(min(2 ** attempt, 5))

    raise RuntimeError(f"โหลดหน้าเว็บไม่สำเร็จ: {last_error}")


def clean_text(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"\r\n?", "\n", text)
    lines = []

    for line in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line:
            lines.append(line)

    return "\n".join(lines)


def extract_novel_text(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "noscript", "svg", "form"]):
        tag.decompose()

    title = ""
    if soup.title:
        title = soup.title.get_text(" ", strip=True)

    # Prefer common article/content containers.
    selectors = [
        "article",
        "[class*='chapter-content']",
        "[class*='chapter_content']",
        "[class*='chapter-body']",
        "[class*='chapter_body']",
        "[class*='entry-content']",
        "[class*='post-content']",
        "[class*='novel-content']",
        "[id*='chapter-content']",
        "[id*='chapter_content']",
        "[id*='chapter-body']",
        "[id*='content']",
        "main",
    ]

    candidates = []

    for selector in selectors:
        for node in soup.select(selector):
            ps = node.find_all("p")
            if ps:
                txt = "\n".join(p.get_text(" ", strip=True) for p in ps)
            else:
                txt = node.get_text("\n", strip=True)

            txt = clean_text(txt)

            if len(txt) >= MIN_TEXT_CHARS:
                candidates.append(txt)

    if candidates:
        text = max(candidates, key=len)
    else:
        ps = soup.find_all("p")
        text = clean_text("\n".join(p.get_text(" ", strip=True) for p in ps))

    # Remove obvious navigation-only pages.
    if len(text) < MIN_TEXT_CHARS:
        raise RuntimeError("ไม่พบเนื้อหานิยายที่เพียงพอ")

    return title, text


def find_next_url(current_url: str, html: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")

    # Standard rel=next first.
    for link in soup.find_all("a", href=True):
        rel = link.get("rel") or []
        rel = [str(x).lower() for x in rel]
        if "next" in rel:
            return urljoin(current_url, link["href"])

    # Text-based next buttons.
    next_words = [
        "next chapter",
        "next",
        "ตอนต่อไป",
        "บทต่อไป",
        "ตอนถัดไป",
        "ถัดไป",
    ]

    for link in soup.find_all("a", href=True):
        label = link.get_text(" ", strip=True).lower()
        if any(word in label for word in next_words):
            return urljoin(current_url, link["href"])

    # IMPORTANT: do not fabricate a chapter URL here. Some sites use slugs
    # that cannot be derived by incrementing a number, and doing so caused
    # Old V6.1 used URL-number guessing; V7 never fabricates chapter URLs.
    return None


# ============================================================
# TRANSLATION
# ============================================================

def translate_chunk(text: str) -> str:
    params = {
        "client": "gtx",
        "sl": "auto",
        "tl": "th",
        "dt": "t",
        "q": text,
    }

    session = get_session()
    r = session.get(
        TRANSLATE_URL,
        params=params,
        timeout=TRANSLATE_TIMEOUT,
    )
    r.raise_for_status()

    data = r.json()
    pieces = []

    for item in data[0]:
        if item and item[0]:
            pieces.append(item[0])

    result = "".join(pieces).strip()

    if len(result) < 10:
        raise RuntimeError("ผลแปลสั้นผิดปกติ")

    return result


def split_for_translation(text: str, max_chars: int = 4500):
    paragraphs = [x.strip() for x in text.split("\n") if x.strip()]
    chunks = []
    current = ""

    for p in paragraphs:
        if len(current) + len(p) + 1 <= max_chars:
            current = f"{current}\n{p}".strip()
        else:
            if current:
                chunks.append(current)

            # Long paragraph fallback.
            if len(p) > max_chars:
                for i in range(0, len(p), max_chars):
                    chunks.append(p[i:i + max_chars])
                current = ""
            else:
                current = p

    if current:
        chunks.append(current)

    return chunks


def translate_text(text: str) -> str:
    chunks = split_for_translation(text)
    out = []

    for chunk in chunks:
        last_error = None

        for attempt in range(MAX_RETRIES):
            try:
                out.append(translate_chunk(chunk))
                break
            except Exception as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    # Backoff only after a failure. No artificial delay on success.
                    time.sleep(min(1.5 * (2 ** attempt), 8))
        else:
            raise RuntimeError(f"แปลไม่สำเร็จ: {last_error}")

    return "\n".join(out)


# ============================================================
# TTS
# ============================================================

def run_tts_sync(text: str, output_path: Path):
    output_path.parent.mkdir(parents=True, exist_ok=True)

    async def _run():
        communicate = edge_tts.Communicate(text, VOICE)
        await communicate.save(str(output_path))

    asyncio.run(_run())


def valid_mp3(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < MIN_MP3_BYTES:
        return False

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return True

    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )

        if result.returncode != 0:
            return False

        duration = float(result.stdout.strip() or "0")
        return duration > 0.05

    except Exception:
        return False


# ============================================================
# CACHE
# ============================================================

def episode_paths(job_id: str, episode: int):
    root = job_dir(job_id)
    return {
        "html": root / "html_cache" / f"{episode}.html",
        "raw": root / "text_cache" / f"{episode}.txt",
        "translated": root / "translated_cache" / f"{episode}.txt",
        "mp3": root / "episode_mp3" / f"episode_{episode:06d}.mp3",
    }


def load_cached_text(path: Path) -> Optional[str]:
    try:
        if path.exists() and path.stat().st_size > 10:
            return path.read_text(encoding="utf-8")
    except Exception:
        pass
    return None


# ============================================================
# EPISODE URL DISCOVERY
# ============================================================


def infer_episode_number_from_url(url: str) -> Optional[int]:
    """Best-effort chapter number detection; never used to fabricate URLs."""
    patterns = [
        r"(?:chapter|chap|episode|ep)[-_ /]*(\d+)",
        r"/(\d+)(?:[/?#]|$)",
    ]
    for pattern in patterns:
        m = re.search(pattern, url or "", flags=re.IGNORECASE)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                pass
    return None


def infer_episode_number_from_html(html: str) -> Optional[int]:
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    if soup.title:
        candidates.append(soup.title.get_text(" ", strip=True))
    for tag in soup.find_all(["h1", "h2", "h3"]):
        candidates.append(tag.get_text(" ", strip=True))
    for text in candidates:
        m = re.search(r"(?:chapter|chap|episode|ep|ตอน|บท)\s*[-#:： ]*\s*(\d+)", text, re.I)
        if m:
            return int(m.group(1))
    return None


def _link_episode_number(anchor, current_url: str) -> Optional[int]:
    href = urljoin(current_url, anchor.get("href", ""))
    label = anchor.get_text(" ", strip=True)
    blob = f"{label} {href}"
    return infer_episode_number_from_url(blob) or infer_episode_number_from_html(label)


def extract_real_chapter_links(current_url: str, html: str) -> dict[int, str]:
    """Extract real chapter links already present in the page/TOC.

    This is deliberately heuristic and cross-site. It only returns URLs that
    actually occur in the HTML; it never invents a URL by changing a number.
    """
    soup = BeautifulSoup(html, "html.parser")
    found = {}
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "#")):
            continue
        url = urljoin(current_url, href)
        if urlparse(url).scheme not in ("http", "https"):
            continue
        n = _link_episode_number(a, current_url)
        if n is None:
            continue
        # Avoid treating the same page as a chapter link when the label is generic.
        label = a.get_text(" ", strip=True).lower()
        if n > 0 and (re.search(r"(?:chapter|chap|episode|ep|ตอน|บท)", label + " " + href, re.I)):
            found.setdefault(n, url)
    return found


def find_toc_urls(current_url: str, html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    keys = ("table of contents", "contents", "chapters", "chapter list", "สารบัญ", "รายการตอน")
    out = []
    for a in soup.find_all("a", href=True):
        label = a.get_text(" ", strip=True).lower()
        href = a.get("href", "")
        blob = f"{label} {href}".lower()
        if any(k in blob for k in keys):
            u = urljoin(current_url, href)
            if u not in out and urlparse(u).scheme in ("http", "https"):
                out.append(u)
    return out[:5]


def find_next_url(current_url: str, html: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")

    # 1) Semantic rel=next.
    for link in soup.find_all("a", href=True):
        rel = [str(x).lower() for x in (link.get("rel") or [])]
        if "next" in rel:
            return urljoin(current_url, link["href"])

    # 2) Common next-button labels/classes.
    patterns = [
        r"^next(?:\s+chapter)?$",
        r"^next\s*chapter",
        r"next\s*chapter",
        r"^ตอนต่อไป$",
        r"ตอนต่อไป",
        r"บทต่อไป",
        r"ตอนถัดไป",
        r"ถัดไป",
        r"下一章",
        r"下一页",
    ]
    candidates = []
    for link in soup.find_all("a", href=True):
        label = link.get_text(" ", strip=True)
        blob = f"{label} {link.get('class', '')} {link.get('id', '')}".lower()
        if any(re.search(p, blob, re.I) for p in patterns):
            candidates.append(urljoin(current_url, link["href"]))
    if candidates:
        return candidates[0]
    return None


def discover_episode_urls(job_id: str, start_url: str, start_episode: int, end_episode: int):
    """Universal multi-site chapter discovery.

    Priority:
      A. Cached real URLs from a previous run.
      B. Real chapter links embedded in the supplied page.
      C. A real Table-of-Contents link embedded in the supplied page.
      D. Real Next navigation, walking only actual links.

    Never fabricates a chapter URL. A 404 is retried and then treated as a
    discovery failure for that URL, not as failure of every requested episode.
    """
    total = end_episode - start_episode + 1
    state = read_state(job_id)
    known = {str(k): v for k, v in (state.get("episode_urls") or {}).items()
             if start_episode <= int(k) <= end_episode}

    def save(msg, stage="discovery"):
        update_state(job_id, lambda s: s.update({
            "episode_urls": known,
            "stage": stage,
            "message": msg,
            "last_activity_at": now_ts(),
        }))

    if len(known) == total:
        save(f"มีลิงก์ตอนจริงใน checkpoint ครบ {start_episode}-{end_episode}", "discovery_complete")
        return known

    visited = set()
    queue = [start_url]
    page_limit = max(250, (end_episode - start_episode + 1) * 8)

    while queue and len(visited) < page_limit:
        current_url = queue.pop(0)
        if not current_url or current_url in visited:
            continue
        visited.add(current_url)

        save(f"กำลังค้นหาลิงก์ตอนจริง ({len(known)}/{total})\n{current_url}")
        try:
            html = fetch_html(current_url)
        except Exception as e:
            # If this was a queued TOC candidate, ignore it and continue with
            # other real navigation links. If it is the only path, fail clearly.
            save(f"เปิดลิงก์ไม่ได้ กำลังลองเส้นทางอื่น: {current_url}", "discovery_retry")
            continue

        actual = infer_episode_number_from_url(current_url) or infer_episode_number_from_html(html)
        if actual is not None and start_episode <= actual <= end_episode:
            known.setdefault(str(actual), current_url)

        # A page may expose a whole chapter list. Use those real hrefs first.
        page_links = extract_real_chapter_links(current_url, html)
        for n, u in sorted(page_links.items()):
            if start_episode <= n <= end_episode:
                known.setdefault(str(n), u)

        if len(known) == total:
            break

        # TOC links are real links, never generated URLs.
        for toc in find_toc_urls(current_url, html):
            if toc not in visited:
                queue.insert(0, toc)

        # If requested start is later than supplied page, real Next navigation
        # remains the universal fallback. Add it to the queue.
        nxt = find_next_url(current_url, html)
        if nxt and nxt not in visited:
            queue.append(nxt)

        # Prioritize a real link to the first missing episode if one exists.
        missing = [ep for ep in range(start_episode, end_episode + 1) if str(ep) not in known]
        if missing:
            target = missing[0]
            direct_real = page_links.get(target)
            if direct_real and direct_real not in visited:
                queue.insert(0, direct_real)

    missing = [ep for ep in range(start_episode, end_episode + 1) if str(ep) not in known]
    if missing:
        first = missing[0]
        raise RuntimeError(
            f"ค้นหาลิงก์ตอนจริงไม่ครบ: ขาดตอน {first} เป็นต้นไป "
            f"(ตรวจแล้ว {len(visited)} หน้า, พบ {len(known)}/{total} ตอน)"
        )

    save(f"พบลิงก์จริงครบตอน {start_episode}-{end_episode}", "discovery_complete")
    return known


# ============================================================
# EPISODE PROCESS
# ============================================================

def set_episode_status(job_id: str, episode: int, status: str, **extra):
    def upd(s):
        es = s.setdefault("episode_status", {})
        item = es.setdefault(str(episode), {})
        item["status"] = status
        item["updated_at"] = now_ts()
        item.update(extra)
        s["last_activity_at"] = now_ts()

    update_state(job_id, upd)


def _fetch_episode_stage(job_id: str, episode: int, url: str):
    paths = episode_paths(job_id, episode)
    if paths["raw"].exists():
        raw = load_cached_text(paths["raw"])
        title = read_state(job_id).get("episode_titles", {}).get(str(episode), "")
        if raw:
            return title, raw
    html = fetch_html(url)
    paths["html"].parent.mkdir(parents=True, exist_ok=True)
    paths["html"].write_text(html, encoding="utf-8")
    title, raw = extract_novel_text(html)
    paths["raw"].parent.mkdir(parents=True, exist_ok=True)
    paths["raw"].write_text(raw, encoding="utf-8")
    update_state(job_id, lambda s: s.setdefault("episode_titles", {}).update({str(episode): title}))
    return title, raw


def _translate_episode_stage(job_id: str, episode: int, raw_text: str):
    paths = episode_paths(job_id, episode)
    cached = load_cached_text(paths["translated"])
    if cached:
        return cached
    translated = translate_text(raw_text)
    paths["translated"].parent.mkdir(parents=True, exist_ok=True)
    paths["translated"].write_text(translated, encoding="utf-8")
    return translated


def _tts_episode_stage(job_id: str, episode: int, translated: str):
    paths = episode_paths(job_id, episode)
    if not valid_mp3(paths["mp3"]):
        if paths["mp3"].exists():
            try: paths["mp3"].unlink()
            except Exception: pass
        run_tts_sync(translated, paths["mp3"])
    if not valid_mp3(paths["mp3"]):
        raise RuntimeError("MP3 ตรวจสอบแล้วไม่ผ่าน")
    return paths["mp3"]


def process_episode(job_id: str, episode: int, url: str, fetch_pool, translate_pool, tts_pool):
    """Run one episode through real fetch -> translate -> TTS stage pools.

    Different episodes can occupy different stages simultaneously. Successful
    stages are cached, so a retry resumes at the first missing/invalid stage.
    """
    try:
        set_episode_status(job_id, episode, "fetching")
        fetch_future = fetch_pool.submit(_fetch_episode_stage, job_id, episode, url)
        title, raw_text = fetch_future.result()
        if len(raw_text) < MIN_TEXT_CHARS:
            raise RuntimeError("เนื้อหาตอนสั้นผิดปกติ")

        set_episode_status(job_id, episode, "translating", title=title)
        translate_future = translate_pool.submit(_translate_episode_stage, job_id, episode, raw_text)
        translated = translate_future.result()
        if len(translated) < 10:
            raise RuntimeError("ข้อความแปลว่าง/สั้นเกินไป")

        set_episode_status(job_id, episode, "tts")
        tts_future = tts_pool.submit(_tts_episode_stage, job_id, episode, translated)
        mp3 = tts_future.result()

        set_episode_status(job_id, episode, "done", mp3=str(mp3), finished_at=now_ts())
        return True
    except Exception as e:
        current = read_state(job_id).get("episode_status", {}).get(str(episode), {}).get("status")
        if current != "retrying":
            set_episode_status(job_id, episode, "failed", error=str(e))
        raise


# ============================================================
# SELF HEAL
# ============================================================

def process_episode_with_retry(job_id: str, episode: int, url: str, fetch_pool, translate_pool, tts_pool):
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return process_episode(
                job_id,
                episode,
                url,
                fetch_pool,
                translate_pool,
                tts_pool,
            )
        except Exception as e:
            last_error = e

            set_episode_status(
                job_id,
                episode,
                "retrying",
                attempt=attempt,
                error=str(e),
            )

            if attempt < MAX_RETRIES:
                # Backoff only after an actual failure.
                time.sleep(min(2 ** (attempt - 1), 8))

    set_episode_status(
        job_id,
        episode,
        "failed",
        attempts=MAX_RETRIES,
        error=str(last_error),
    )
    return False


# ============================================================
# FFMPEG BATCH BUILD
# ============================================================

def merge_mp3s(mp3_paths: list[Path], output: Path):
    if not mp3_paths:
        raise RuntimeError("ไม่มี MP3 ให้รวม")

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ไม่พบ ffmpeg ใน PATH")

    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".txt",
        delete=False,
        encoding="utf-8",
    ) as f:
        concat_file = Path(f.name)

        for p in mp3_paths:
            safe = str(p.resolve()).replace("'", "'\\''")
            f.write(f"file '{safe}'\n")

    try:
        cmd_copy = [
            ffmpeg,
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy",
            str(output),
        ]

        r = subprocess.run(
            cmd_copy,
            capture_output=True,
            text=True,
            timeout=3600,
        )

        if r.returncode == 0 and valid_mp3(output):
            return

        # Fallback: re-encode if source MP3s have incompatible streams.
        cmd_reencode = [
            ffmpeg,
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            "-vn",
            "-c:a", "libmp3lame",
            "-b:a", "128k",
            str(output),
        ]

        r2 = subprocess.run(
            cmd_reencode,
            capture_output=True,
            text=True,
            timeout=3600,
        )

        if r2.returncode != 0 or not valid_mp3(output):
            raise RuntimeError(
                "รวม MP3 ไม่สำเร็จ\n"
                + (r2.stderr[-1500:] if r2.stderr else "")
            )

    finally:
        try:
            concat_file.unlink()
        except Exception:
            pass


def zip_file(input_file: Path, output_zip: Path):
    output_zip.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(
        output_zip,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as z:
        z.write(input_file, arcname=input_file.name)


# ============================================================
# PROGRESS / ETA
# ============================================================

def calculate_batch_metrics(state: dict):
    total = int(state.get("current_set_total") or 0)
    done = int(state.get("current_set_completed") or 0)
    started = state.get("current_set_started_at")

    if not started:
        return {
            "elapsed": 0,
            "eta": None,
            "rate": 0,
            "percent": 0,
        }

    finished_time = state.get("current_set_finished_at") or now_ts()
    elapsed = max(0, finished_time - started)

    if total:
        percent = min(100, (done / total) * 100)
    else:
        percent = 0

    if done > 0 and elapsed > 0 and done < total:
        rate = done / elapsed
        eta = (total - done) / rate
    elif done >= total and total > 0:
        rate = done / max(elapsed, 0.001)
        eta = 0
    else:
        rate = 0
        eta = None

    return {
        "elapsed": elapsed,
        "eta": eta,
        "rate": rate,
        "percent": percent,
    }


def refresh_progress_for_batch(job_id: str, set_start: int, set_end: int):
    state = read_state(job_id)
    statuses = state.get("episode_status", {})

    done = 0
    failed = 0

    for ep in range(set_start, set_end + 1):
        status = statuses.get(str(ep), {}).get("status")
        if status == "done":
            done += 1
        elif status == "failed":
            failed += 1

    started = state.get("current_set_started_at")
    active = 0

    if started:
        active = now_ts() - started
        if state.get("current_set_finished_at"):
            active = state["current_set_finished_at"] - started

    total = set_end - set_start + 1

    if done > 0 and active > 0 and done < total:
        eta = ((total - done) / done) * active
    elif done >= total:
        eta = 0
    else:
        eta = None

    def upd(s):
        s["current_set_completed"] = done
        s["current_set_total"] = total
        s["current_set_active_seconds"] = max(0, active)
        s["current_set_eta_seconds"] = eta
        s["overall_completed"] = sum(
            1 for x in s.get("episode_status", {}).values()
            if x.get("status") == "done"
        )
        s["overall_failed"] = sum(
            1 for x in s.get("episode_status", {}).values()
            if x.get("status") == "failed"
        )
        s["last_activity_at"] = now_ts()

    return update_state(job_id, upd)


# ============================================================
# MAIN JOB
# ============================================================

def run_job(job_id: str):
    state = read_state(job_id)

    url = state["url"]
    start = int(state["start_episode"])
    end = int(state["end_episode"])
    per_set = int(state["episodes_per_set"])

    def start_job(s):
        s["status"] = "running"
        s["stage"] = "crawling"
        s["job_started_at"] = s.get("job_started_at") or now_ts()
        s["last_activity_at"] = now_ts()
        s["error"] = None

    update_state(job_id, start_job)

    try:
        # Discover all URLs first. This is separate from audio processing,
        # and cached URLs allow later resumes to skip crawling work.
        known_urls = discover_episode_urls(
            job_id,
            url,
            start,
            end,
        )

        missing = [
            ep for ep in range(start, end + 1)
            if str(ep) not in known_urls
        ]

        if missing:
            raise RuntimeError(
                f"หา URL ของตอน {missing[0]} ไม่พบ "
                f"(พบถึงตอน {missing[0]-1})"
            )

        # True stage pools: different episodes can be fetching, translating,
        # and synthesizing at the same time. No artificial success delay.
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS, thread_name_prefix="fetch") as fetch_pool, \
             ThreadPoolExecutor(max_workers=TRANSLATE_WORKERS, thread_name_prefix="translate") as translate_pool, \
             ThreadPoolExecutor(max_workers=TTS_WORKERS, thread_name_prefix="tts") as tts_pool, \
             ThreadPoolExecutor(max_workers=max(FETCH_WORKERS, TRANSLATE_WORKERS, TTS_WORKERS), thread_name_prefix="episode") as episode_pool:

            for set_index, set_start in enumerate(
                range(start, end + 1, per_set),
                start=1,
            ):
                set_end = min(set_start + per_set - 1, end)
                batch_total = set_end - set_start + 1

                # Don't restart a fully completed batch after resume.
                state_now = read_state(job_id)
                all_done = all(
                    state_now.get("episode_status", {})
                    .get(str(ep), {})
                    .get("status") == "done"
                    for ep in range(set_start, set_end + 1)
                )

                if all_done:
                    update_state(job_id, lambda s: (
                        s.update({
                            "current_set": set_index,
                            "current_set_start": set_start,
                            "current_set_end": set_end,
                            "current_set_total": batch_total,
                            "current_set_completed": batch_total,
                            "current_set_finished_at": s.get("current_set_finished_at") or now_ts(),
                            "stage": "set_completed",
                            "message": f"ข้ามชุดที่ {set_index}: ทำเสร็จแล้ว",
                        })
                    ))
                    continue

                batch_start_time = now_ts()

                def begin_batch(s):
                    s["current_set"] = set_index
                    s["current_set_start"] = set_start
                    s["current_set_end"] = set_end
                    s["current_set_total"] = batch_total
                    s["current_set_completed"] = 0
                    s["current_set_started_at"] = batch_start_time
                    s["current_set_finished_at"] = None
                    s["current_set_active_seconds"] = 0
                    s["current_set_eta_seconds"] = None
                    s["stage"] = "processing"
                    s["message"] = f"กำลังสร้างชุดที่ {set_index}"
                    s["last_activity_at"] = now_ts()

                update_state(job_id, begin_batch)

                # Submit episodes concurrently.
                futures = {}

                for ep in range(set_start, set_end + 1):
                    status = read_state(job_id).get(
                        "episode_status", {}
                    ).get(str(ep), {}).get("status")

                    if status == "done":
                        continue

                    ep_url = known_urls[str(ep)]

                    future = episode_pool.submit(
                        process_episode_with_retry,
                        job_id,
                        ep,
                        ep_url,
                        fetch_pool,
                        translate_pool,
                        tts_pool,
                    )
                    futures[future] = ep

                # Wait for episode futures while continuously writing progress.
                for future in futures:
                    ep = futures[future]

                    try:
                        future.result()
                    except Exception as e:
                        set_episode_status(
                            job_id,
                            ep,
                            "failed",
                            error=str(e),
                        )

                    refresh_progress_for_batch(
                        job_id,
                        set_start,
                        set_end,
                    )

                # Final progress refresh.
                refresh_progress_for_batch(
                    job_id,
                    set_start,
                    set_end,
                )

                state_after = read_state(job_id)
                failed_eps = [
                    ep for ep in range(set_start, set_end + 1)
                    if state_after.get("episode_status", {})
                    .get(str(ep), {})
                    .get("status") != "done"
                ]

                if failed_eps:
                    # One extra self-heal pass for failed episodes.
                    update_state(job_id, lambda s: (
                        s.update({
                            "stage": "self_heal",
                            "message": f"กำลังซ่อมตอนที่ล้มเหลว {len(failed_eps)} ตอน",
                        })
                    ))

                    for ep in failed_eps:
                        process_episode_with_retry(
                            job_id,
                            ep,
                            known_urls[str(ep)],
                            fetch_pool,
                            translate_pool,
                            tts_pool,
                        )
                        refresh_progress_for_batch(
                            job_id,
                            set_start,
                            set_end,
                        )

                state_after = read_state(job_id)

                final_failed = [
                    ep for ep in range(set_start, set_end + 1)
                    if state_after.get("episode_status", {})
                    .get(str(ep), {})
                    .get("status") != "done"
                ]

                if final_failed:
                    raise RuntimeError(
                        f"ชุดที่ {set_index} ยังมีตอนที่สร้างไม่สำเร็จ: "
                        f"{final_failed[:20]}"
                    )

                # ---------------- MERGE ----------------
                def merging(s):
                    s["stage"] = "merging"
                    s["message"] = f"กำลังรวม MP3 ชุดที่ {set_index}"
                    s["current_set_completed"] = batch_total

                update_state(job_id, merging)

                mp3_paths = [
                    episode_paths(job_id, ep)["mp3"]
                    for ep in range(set_start, set_end + 1)
                ]

                for p in mp3_paths:
                    if not valid_mp3(p):
                        raise RuntimeError(f"MP3 เสียหรือหาย: {p.name}")

                novel_name = safe_filename(
                    Path(urlparse(url).path).name or "novel"
                )

                set_name = (
                    f"{novel_name}_EP_{set_start}-{set_end}"
                )

                output_mp3 = job_dir(job_id) / "batches" / f"{set_name}.mp3"
                output_zip = job_dir(job_id) / "batches" / f"{set_name}.zip"

                if not valid_mp3(output_mp3):
                    merge_mp3s(mp3_paths, output_mp3)

                # ---------------- ZIP ----------------
                def zipping(s):
                    s["stage"] = "zipping"
                    s["message"] = f"กำลังสร้าง ZIP ชุดที่ {set_index}"

                update_state(job_id, zipping)

                if not output_zip.exists() or output_zip.stat().st_size < 100:
                    zip_file(output_mp3, output_zip)

                finish_time = now_ts()

                def finish_batch(s):
                    s["current_set_finished_at"] = finish_time
                    s["current_set_active_seconds"] = finish_time - batch_start_time
                    s["current_set_eta_seconds"] = 0
                    s["current_set_completed"] = batch_total
                    s["stage"] = "set_completed"
                    s["message"] = f"ชุดที่ {set_index} เสร็จแล้ว"
                    s.setdefault("set_outputs", {})[str(set_index)] = {
                        "start": set_start,
                        "end": set_end,
                        "mp3": str(output_mp3),
                        "zip": str(output_zip),
                        "elapsed_seconds": finish_time - batch_start_time,
                    }
                    s["last_activity_at"] = finish_time

                update_state(job_id, finish_batch)

            # All sets complete.
            def complete_job(s):
                s["status"] = "completed"
                s["stage"] = "completed"
                s["message"] = "สร้างทั้งหมดเสร็จเรียบร้อย"
                s["job_finished_at"] = now_ts()
                s["last_activity_at"] = now_ts()
                s["overall_completed"] = s["total_episodes"]

            update_state(job_id, complete_job)

    except Exception as e:
        def fail_job(s):
            s["status"] = "error"
            s["stage"] = "error"
            s["error"] = str(e)
            s["message"] = "งานหยุดเพราะเกิดข้อผิดพลาด"
            s["last_activity_at"] = now_ts()

        update_state(job_id, fail_job)


def start_job_thread(job_id: str):
    with JOB_THREADS_LOCK:
        t = JOB_THREADS.get(job_id)

        if t and t.is_alive():
            return False

        t = threading.Thread(
            target=run_job,
            args=(job_id,),
            daemon=True,
            name=f"novel-job-{job_id}",
        )
        JOB_THREADS[job_id] = t
        t.start()
        return True


# ============================================================
# STREAMLIT UI
# ============================================================

st.set_page_config(
    page_title="NOVEL AUDIO FACTORY V7 UNIVERSAL",
    page_icon="🎧",
    layout="wide",
)

st.title("🎧 NOVEL AUDIO FACTORY V7 UNIVERSAL")
st.caption(
    "Novel → Thai → Premwadee TTS → MP3 → Batch ZIP | "
    "Live Progress + ETA + Resume + Self-Healing"
)

with st.sidebar:
    st.subheader("⚙️ ระบบ")

    st.write(f"Version: **{APP_VERSION}**")
    st.write(f"Voice: **{VOICE}**")

    st.write(
        f"Workers: Fetch {FETCH_WORKERS} / "
        f"Translate {TRANSLATE_WORKERS} / TTS {TTS_WORKERS}"
    )

    st.info(
        "หมายเหตุ: browser/Streamlit ไม่สามารถบังคับ Android "
        "ให้ดาวน์โหลดหลายไฟล์แบบเงียบ ๆ ได้อย่างรับประกัน "
        "จึงยังต้องกดดาวน์โหลดไฟล์จากหน้าเว็บ"
    )


# ---------------- INPUT ----------------

st.subheader("📚 ตั้งค่างาน")

url = st.text_input(
    "Novel URL",
    placeholder="https://example.com/chapter-1",
)

col1, col2, col3 = st.columns(3)

with col1:
    start_episode = st.number_input(
        "ตอนเริ่ม",
        min_value=1,
        value=1,
        step=1,
    )

with col2:
    end_episode = st.number_input(
        "ตอนสุดท้าย",
        min_value=1,
        value=500,
        step=1,
    )

with col3:
    episodes_per_set = st.number_input(
        "จำนวนตอนต่อชุด",
        min_value=1,
        value=50,
        step=1,
    )

if end_episode < start_episode:
    st.error("ตอนสุดท้ายต้องมากกว่าหรือเท่ากับตอนเริ่ม")

total_episodes = max(0, int(end_episode) - int(start_episode) + 1)
total_sets = (
    (total_episodes + int(episodes_per_set) - 1)
    // int(episodes_per_set)
    if total_episodes
    else 0
)

st.write(
    f"📦 ทั้งหมด **{total_episodes} ตอน** → "
    f"**{total_sets} ชุด**"
)

detected_input_episode = infer_episode_number_from_url(url) if url else None
if detected_input_episode is not None and int(start_episode) != detected_input_episode:
    st.info(
        f"🔎 URL นี้มีเลขตอนประมาณ **{detected_input_episode}** "
        f"แต่คุณเลือกเริ่มที่ **{int(start_episode)}** — "
        "V7 จะค้นหาลิงก์ตอนจริงจากหน้าเว็บ/สารบัญ/ปุ่ม Next "
        "โดยไม่สร้าง URL เดาจากเลขตอน"
    )

start_button = st.button(
    "🚀 เริ่มสร้าง",
    type="primary",
    use_container_width=True,
)

if start_button:
    if not url.strip():
        st.error("กรุณาใส่ Novel URL")
        st.stop()

    if end_episode < start_episode:
        st.error("ช่วงตอนไม่ถูกต้อง")
        st.stop()

    job_id = job_id_from_inputs(
        url,
        int(start_episode),
        int(end_episode),
        int(episodes_per_set),
    )

    ensure_state(
        job_id,
        url.strip(),
        int(start_episode),
        int(end_episode),
        int(episodes_per_set),
    )

    state = read_state(job_id)

    # If previous job errored/completed, keep checkpoint and resume.
    if state.get("status") == "completed":
        st.success(
            "งานนี้สร้างเสร็จแล้ว ระบบจะเปิดผลลัพธ์เดิมให้"
        )
        st.session_state["job_id"] = job_id
    else:
        # IMPORTANT: set RUNNING before launching the thread.
        # This removes the race where the page stayed at waiting / 0 / 0.
        def mark_running(s):
            s["status"] = "running"
            s["stage"] = "starting"
            s["message"] = "กำลังเริ่มระบบ..."
            s["job_started_at"] = s.get("job_started_at") or now_ts()
            s["last_activity_at"] = now_ts()
            s["error"] = None

        update_state(job_id, mark_running)
        # Verify the checkpoint exists before the UI reruns. This prevents
        # a transient "job not found" screen on slow filesystems.
        if not (job_dir(job_id) / "state.json").exists():
            ensure_state(
                job_id,
                url.strip(),
                int(start_episode),
                int(end_episode),
                int(episodes_per_set),
            )
            update_state(job_id, mark_running)
        st.session_state["job_id"] = job_id
        start_job_thread(job_id)
        st.rerun()


# ---------------- SELECT / RESUME EXISTING ----------------

job_id = st.session_state.get("job_id")

if not job_id:
    # If there is a matching state, don't automatically start it.
    candidate = job_id_from_inputs(
        url.strip() if url else "",
        int(start_episode),
        int(end_episode),
        int(episodes_per_set),
    )

    if url.strip() and (job_dir(candidate) / "state.json").exists():
        job_id = candidate
        st.session_state["job_id"] = job_id


if job_id:
    state = read_state(job_id)

    if not state:
        # The Streamlit session can outlive the local state file after a
        # process restart/redeploy. Recreate the state instead of showing
        # a dead "job not found" screen.
        if url.strip() and end_episode >= start_episode:
            ensure_state(
                job_id,
                url.strip(),
                int(start_episode),
                int(end_episode),
                int(episodes_per_set),
            )
            state = read_state(job_id)

        if not state:
            st.error("ไม่พบข้อมูล job และไม่สามารถกู้สถานะงานได้")
            st.session_state.pop("job_id", None)
            st.stop()

    # Keep worker alive across Streamlit reruns.
    if state.get("status") == "running":
        start_job_thread(job_id)

    st.divider()
    st.subheader("📊 ความคืบหน้า")

    current_set = int(state.get("current_set") or 0)
    total_sets_state = int(state.get("total_sets") or total_sets or 0)

    overall_done = int(state.get("overall_completed") or 0)
    overall_total = int(state.get("total_episodes") or total_episodes or 0)

    overall_percent = (
        overall_done / overall_total * 100
        if overall_total
        else 0
    )

    # ---------------- CURRENT SET ----------------
    st.markdown(
        f"### 📦 ชุดที่ {current_set} / {total_sets_state}"
    )

    set_start = state.get("current_set_start")
    set_end = state.get("current_set_end")

    if set_start and set_end:
        st.write(f"ตอน **{set_start} – {set_end}**")

    batch_total = int(state.get("current_set_total") or 0)
    batch_done = int(state.get("current_set_completed") or 0)

    metrics = calculate_batch_metrics(state)

    batch_percent = metrics["percent"]
    elapsed = metrics["elapsed"]
    eta = metrics["eta"]
    rate = metrics["rate"]

    st.progress(
        min(1.0, max(0.0, batch_percent / 100))
    )

    m1, m2, m3, m4 = st.columns(4)

    with m1:
        st.metric(
            "ชุดนี้เสร็จแล้ว",
            f"{batch_done} / {batch_total}",
        )

    with m2:
        st.metric(
            "ความคืบหน้าชุดนี้",
            f"{batch_percent:.1f}%",
        )

    with m3:
        st.metric(
            "เวลาที่ใช้",
            format_duration(elapsed),
            help=format_seconds_exact(elapsed),
        )

    with m4:
        st.metric(
            "เหลือประมาณ",
            format_duration(eta),
            help=(
                format_seconds_exact(eta)
                if eta is not None
                else "รอข้อมูลเพิ่มเพื่อคำนวณ ETA"
            ),
        )

    if rate > 0:
        st.caption(
            f"⚡ ความเร็วเฉลี่ยชุดนี้: {rate * 60:.2f} ตอน/นาที"
        )

    stage = state.get("stage", "waiting")
    message = state.get("message", "")

    st.info(
        f"สถานะ: **{stage}** — {message}"
    )

    # ---------------- OVERALL ----------------
    st.markdown("### 🌍 ภาพรวมทั้งหมด")

    st.progress(
        min(1.0, max(0.0, overall_percent / 100))
    )

    o1, o2, o3 = st.columns(3)

    with o1:
        st.metric(
            "ตอนเสร็จแล้ว",
            f"{overall_done} / {overall_total}",
        )

    with o2:
        st.metric(
            "ความคืบหน้ารวม",
            f"{overall_percent:.1f}%",
        )

    with o3:
        st.metric(
            "ตอนที่ล้มเหลว",
            str(state.get("overall_failed") or 0),
        )

    # ---------------- EPISODE TABLE ----------------
    if set_start and set_end:
        statuses = state.get("episode_status", {})

        rows = []

        for ep in range(int(set_start), int(set_end) + 1):
            item = statuses.get(str(ep), {})
            rows.append({
                "ตอน": ep,
                "สถานะ": item.get("status", "รอ"),
                "ข้อความผิดพลาด": item.get("error", ""),
            })

        st.dataframe(
            rows,
            use_container_width=True,
            hide_index=True,
        )

    # ---------------- OUTPUTS ----------------
    st.markdown("### 📥 ไฟล์ที่สร้างแล้ว")

    outputs = state.get("set_outputs", {})

    if outputs:
        for set_no in sorted(
            outputs.keys(),
            key=lambda x: int(x),
            reverse=True,
        ):
            out = outputs[set_no]

            st.write(
                f"**ชุดที่ {set_no}: ตอน "
                f"{out['start']}–{out['end']}**"
            )

            c1, c2 = st.columns(2)

            zip_path = Path(out["zip"])
            mp3_path = Path(out["mp3"])

            with c1:
                if zip_path.exists():
                    with zip_path.open("rb") as f:
                        st.download_button(
                            "⬇️ ดาวน์โหลด ZIP",
                            data=f.read(),
                            file_name=zip_path.name,
                            mime="application/zip",
                            key=f"zip_{job_id}_{set_no}",
                            use_container_width=True,
                        )

            with c2:
                if mp3_path.exists():
                    with mp3_path.open("rb") as f:
                        st.download_button(
                            "🎧 ดาวน์โหลด MP3",
                            data=f.read(),
                            file_name=mp3_path.name,
                            mime="audio/mpeg",
                            key=f"mp3_{job_id}_{set_no}",
                            use_container_width=True,
                        )

    # ---------------- STATUS ----------------
    if state.get("status") == "completed":
        st.success("🎉 งานทั้งหมดเสร็จเรียบร้อยแล้ว")

    elif state.get("status") == "error":
        st.error(
            f"❌ งานหยุด: {state.get('error', 'ไม่ทราบสาเหตุ')}"
        )

    elif state.get("status") == "running":
        # Rerun automatically so the UI follows the background job.
        time.sleep(1.0)
        st.rerun()

    elif state.get("status") == "idle":
        # A thread may have just been launched before Streamlit reran.
        # If it is alive, keep polling instead of showing a false waiting state.
        with JOB_THREADS_LOCK:
            thread_alive = bool(
                JOB_THREADS.get(job_id)
                and JOB_THREADS[job_id].is_alive()
            )

        if thread_alive:
            time.sleep(1.0)
            st.rerun()
        else:
            st.info("กด เริ่มสร้าง เพื่อเริ่มงาน")
