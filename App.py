# ============================================================
# NOVEL AUDIO FACTORY
# One-click Thai Novel -> MP3
# Microsoft Edge TTS - Premwadee
# ============================================================

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests
import streamlit as st
from bs4 import BeautifulSoup
import edge_tts


# ============================================================
# CONFIG
# ============================================================

APP_TITLE = "🎙️ โรงงานผลิตนิยายเสียง"

VOICE = "th-TH-PremwadeeNeural"

OUTPUT_DIR = "novel_output"

# ระบบเลือกค่าเหล่านี้เอง
EPISODE_WORKERS = 3
TRANSLATE_WORKERS = 3
TTS_WORKERS = 6

TRANSLATE_CHARS = 3500
TTS_CHARS = 3000

REQUEST_TIMEOUT = 35

MAX_RETRY = 6

RETRY_WAIT = [2, 5, 10, 20, 40, 60]

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 10) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/130.0 Mobile Safari/537.36"
    ),
    "Accept-Language": "th,en;q=0.9",
}


# ============================================================
# BASIC
# ============================================================

def timestamp():
    return datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def wait_retry(n):
    time.sleep(
        RETRY_WAIT[
            min(n - 1, len(RETRY_WAIT) - 1)
        ]
    )


def safe_name(text):
    text = re.sub(
        r'[\\/:*?"<>|]+',
        "_",
        text,
    )

    text = re.sub(
        r"\s+",
        "_",
        text,
    )

    return text[:150]


def atomic_json_save(path, data):
    tmp = path + ".tmp"

    with open(
        tmp,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2,
        )

    os.replace(tmp, path)


def load_json(path):
    if not os.path.exists(path):
        return None

    try:
        with open(
            path,
            "r",
            encoding="utf-8",
        ) as f:
            return json.load(f)
    except Exception:
        return None


# ============================================================
# TEXT
# ============================================================

def clean_text(text):
    if not text:
        return ""

    text = text.replace("\xa0", " ")
    text = text.replace("\r\n", "\n")
    text = text.replace("\r", "\n")
    text = text.replace("\u200b", "")
    text = text.replace("\ufeff", "")

    text = re.sub(
        r"[ \t]+",
        " ",
        text,
    )

    text = re.sub(
        r"\n[ \t]+",
        "\n",
        text,
    )

    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text,
    )

    return text.strip()


def split_text(text, limit):
    text = clean_text(text)

    if not text:
        return []

    if len(text) <= limit:
        return [text]

    paragraphs = text.split("\n")

    result = []
    current = ""

    for paragraph in paragraphs:
        paragraph = paragraph.strip()

        if not paragraph:
            continue

        candidate = (
            paragraph
            if not current
            else current + "\n" + paragraph
        )

        if len(candidate) <= limit:
            current = candidate
            continue

        if current:
            result.append(current)
            current = ""

        while len(paragraph) > limit:

            cut_positions = [
                paragraph.rfind("。", 0, limit),
                paragraph.rfind("！", 0, limit),
                paragraph.rfind("？", 0, limit),
                paragraph.rfind(".", 0, limit),
                paragraph.rfind("!", 0, limit),
                paragraph.rfind("?", 0, limit),
                paragraph.rfind(" ", 0, limit),
            ]

            cut = max(cut_positions)

            if cut < limit // 2:
                cut = limit

            result.append(
                paragraph[:cut].strip()
            )

            paragraph = paragraph[cut:].strip()

        current = paragraph

    if current:
        result.append(current)

    return [
        x for x in result
        if x.strip()
    ]


# ============================================================
# HTTP
# ============================================================

def download_page(url):
    last_error = None

    for attempt in range(
        1,
        MAX_RETRY + 1,
    ):
        try:
            response = requests.get(
                url,
                headers=HTTP_HEADERS,
                timeout=REQUEST_TIMEOUT,
            )

            response.raise_for_status()

            if not response.text.strip():
                raise RuntimeError(
                    "หน้าเว็บว่าง"
                )

            return response.text

        except Exception as e:
            last_error = str(e)

            if attempt < MAX_RETRY:
                wait_retry(attempt)

    raise RuntimeError(
        "ดาวน์โหลดหน้าเว็บไม่ได้: "
        + str(last_error)
    )


# ============================================================
# HTML PARSER
# ============================================================

def extract_novel_text(html):
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    for tag in soup([
        "script",
        "style",
        "noscript",
        "nav",
        "header",
        "footer",
        "aside",
        "form",
        "iframe",
        "svg",
    ]):
        tag.decompose()

    paragraphs = []

    for p in soup.find_all("p"):
        text = clean_text(
            p.get_text(
                " ",
                strip=True,
            )
        )

        if len(text) >= 10:
            paragraphs.append(text)

    if paragraphs:
        return "\n\n".join(
            paragraphs
        )

    body = soup.body

    if body:
        return clean_text(
            body.get_text(
                "\n",
                strip=True,
            )
        )

    return ""


def find_next_url(current_url, html):
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    # ----------------------------------------
    # rel=next
    # ----------------------------------------

    for link in soup.find_all("a"):
        rel = link.get("rel")

        if not rel:
            continue

        rel_text = " ".join(
            rel
        ).lower()

        if "next" in rel_text:
            href = link.get("href")

            if href:
                return urllib.parse.urljoin(
                    current_url,
                    href,
                )

    # ----------------------------------------
    # Next button
    # ----------------------------------------

    candidates = []

    markers = [
        "next",
        "next chapter",
        "ต่อไป",
        "ตอนต่อไป",
        "ถัดไป",
        "下一章",
        "下一页",
        "下一頁",
    ]

    for link in soup.find_all("a"):
        href = link.get("href")

        if not href:
            continue

        text = clean_text(
            link.get_text(
                " ",
                strip=True,
            )
        ).lower()

        score = 0

        for marker in markers:
            if marker in text:
                score += 10

        if text in (
            "next",
            "next chapter",
            "ต่อไป",
            "ตอนต่อไป",
            "ถัดไป",
        ):
            score += 50

        if score:
            candidates.append(
                (
                    score,
                    urllib.parse.urljoin(
                        current_url,
                        href,
                    ),
                )
            )

    if candidates:
        candidates.sort(
            key=lambda x: x[0],
            reverse=True,
        )

        return candidates[0][1]

    # ----------------------------------------
    # Numeric URL fallback
    # ----------------------------------------

    match = re.search(
        r"(\d+)(?!.*\d)",
        current_url,
    )

    if match:
        number = int(
            match.group(1)
        ) + 1

        return (
            current_url[:match.start(1)]
            + str(number)
            + current_url[match.end(1):]
        )

    return None


# ============================================================
# TRANSLATION
# ============================================================

def translate_part(text):
    endpoint = (
        "https://translate.googleapis.com/"
        "translate_a/single"
    )

    params = {
        "client": "gtx",
        "sl": "auto",
        "tl": "th",
        "dt": "t",
        "q": text,
    }

    last_error = None

    for attempt in range(
        1,
        MAX_RETRY + 1,
    ):
        try:
            response = requests.get(
                endpoint,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )

            response.raise_for_status()

            data = response.json()

            result = ""

            for item in data[0]:
                if item and item[0]:
                    result += item[0]

            result = clean_text(result)

            if not result:
                raise RuntimeError(
                    "ไม่ได้ข้อความแปล"
                )

            return result

        except Exception as e:
            last_error = str(e)

            if attempt < MAX_RETRY:
                wait_retry(attempt)

    raise RuntimeError(
        "Translation error: "
        + str(last_error)
    )


async def translate_episode(text):
    parts = split_text(
        text,
        TRANSLATE_CHARS,
    )

    if not parts:
        return ""

    semaphore = asyncio.Semaphore(
        TRANSLATE_WORKERS
    )

    loop = asyncio.get_running_loop()

    async def translate_worker(
        index,
        part,
    ):
        async with semaphore:

            result = await loop.run_in_executor(
                None,
                translate_part,
                part,
            )

            return index, result

    jobs = [
        translate_worker(
            i,
            part,
        )
        for i, part in enumerate(parts)
    ]

    results = await asyncio.gather(
        *jobs
    )

    results.sort(
        key=lambda x: x[0]
    )

    return "\n\n".join(
        x[1]
        for x in results
    )


# ============================================================
# TTS
# ============================================================

async def create_tts_file(
    text,
    output,
):
    last_error = None

    for attempt in range(
        1,
        MAX_RETRY + 1,
    ):
        try:

            communicate = edge_tts.Communicate(
                text,
                VOICE,
            )

            await communicate.save(
                output
            )

            if (
                os.path.exists(output)
                and os.path.getsize(output) > 1000
            ):
                return True

            raise RuntimeError(
                "ไฟล์เสียงผิดปกติ"
            )

        except Exception as e:
            last_error = str(e)

            try:
                if os.path.exists(output):
                    os.remove(output)
            except Exception:
                pass

            if attempt < MAX_RETRY:
                await asyncio.sleep(
                    RETRY_WAIT[
                        min(
                            attempt - 1,
                            len(RETRY_WAIT) - 1,
                        )
                    ]
                )

    raise RuntimeError(
        "TTS error: "
        + str(last_error)
    )


async def create_episode_tts(
    text,
    output,
):
    parts = split_text(
        text,
        TTS_CHARS,
    )

    if not parts:
        raise RuntimeError(
            "ไม่มีข้อความสำหรับ TTS"
        )

    # ----------------------------------------
    # ตอนสั้น
    # ----------------------------------------

    if len(parts) == 1:
        await create_tts_file(
            parts[0],
            output,
        )
        return

    # ----------------------------------------
    # ตอนยาว
    # ----------------------------------------

    temp_dir = tempfile.mkdtemp(
        prefix="tts_"
    )

    try:

        semaphore = asyncio.Semaphore(
            TTS_WORKERS
        )

        async def worker(
            index,
            part,
        ):
            filename = os.path.join(
                temp_dir,
                f"{index:05d}.mp3",
            )

            async with semaphore:

                await create_tts_file(
                    part,
                    filename,
                )

            return index, filename

        jobs = [
            worker(
                i,
                part,
            )
            for i, part in enumerate(parts)
        ]

        results = await asyncio.gather(
            *jobs
        )

        results.sort(
            key=lambda x: x[0]
        )

        merge_mp3(
            [
                x[1]
                for x in results
            ],
            output,
        )

    finally:

        shutil.rmtree(
            temp_dir,
            ignore_errors=True,
        )


# ============================================================
# FFMPEG
# ============================================================

def check_ffmpeg():
    return shutil.which(
        "ffmpeg"
    ) is not None


def merge_mp3(
    files,
    output,
):
    if not files:
        raise RuntimeError(
            "ไม่มี MP3 ให้รวม"
        )

    if len(files) == 1:
        shutil.copyfile(
            files[0],
            output,
        )
        return

    if not check_ffmpeg():
        raise RuntimeError(
            "ไม่พบ ffmpeg"
        )

    list_file = output + ".txt"
    temp_output = output + ".tmp.mp3"

    with open(
        list_file,
        "w",
        encoding="utf-8",
    ) as f:

        for path in files:

            absolute = os.path.abspath(
                path
            )

            absolute = absolute.replace(
                "'",
                "'\\''",
            )

            f.write(
                "file '"
                + absolute
                + "'\n"
            )

    try:

        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                list_file,
                "-map",
                "0:a:0",
                "-c",
                "copy",
                temp_output,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
        )

        os.replace(
            temp_output,
            output,
        )

    except Exception as e:

        if os.path.exists(
            temp_output
        ):
            os.remove(
                temp_output
            )

        raise RuntimeError(
            "รวม MP3 ไม่สำเร็จ: "
            + str(e)
        )

    finally:

        if os.path.exists(
            list_file
        ):
            os.remove(
                list_file
            )


# ============================================================
# FILE VALIDATION
# ============================================================

def valid_mp3(path):
    if not os.path.exists(path):
        return False

    try:

        size = os.path.getsize(
            path
        )

        if size < 1000:
            return False

        return True

    except Exception:
        return False


# ============================================================
# EPISODE CACHE
# ============================================================

def episode_paths(
    job_dir,
    episode,
):
    directory = os.path.join(
        job_dir,
        "episodes",
        f"{episode:05d}",
    )

    os.makedirs(
        directory,
        exist_ok=True,
    )

    return {
        "dir": directory,
        "html": os.path.join(
            directory,
            "page.html",
        ),
        "original": os.path.join(
            directory,
            "original.txt",
        ),
        "thai": os.path.join(
            directory,
            "thai.txt",
        ),
        "mp3": os.path.join(
            directory,
            f"episode_{episode:05d}.mp3",
        ),
    }


# ============================================================
# PROCESS ONE EPISODE
# ============================================================

async def process_episode(
    job_dir,
    episode,
    url,
):
    paths = episode_paths(
        job_dir,
        episode,
    )

    # ----------------------------------------
    # HTML
    # ----------------------------------------

    if os.path.exists(
        paths["html"]
    ):

        with open(
            paths["html"],
            "r",
            encoding="utf-8",
        ) as f:
            html = f.read()

    else:

        loop = asyncio.get_running_loop()

        html = await loop.run_in_executor(
            None,
            download_page,
            url,
        )

        with open(
            paths["html"],
            "w",
            encoding="utf-8",
        ) as f:
            f.write(html)

    # ----------------------------------------
    # ORIGINAL
    # ----------------------------------------

    if os.path.exists(
        paths["original"]
    ):

        with open(
            paths["original"],
            "r",
            encoding="utf-8",
        ) as f:
            original = f.read()

    else:

        original = extract_novel_text(
            html
        )

        if not original:
            raise RuntimeError(
                f"ตอน {episode} "
                "ไม่พบข้อความนิยาย"
            )

        with open(
            paths["original"],
            "w",
            encoding="utf-8",
        ) as f:
            f.write(original)

    # ----------------------------------------
    # TRANSLATION
    # ----------------------------------------

    if os.path.exists(
        paths["thai"]
    ):

        with open(
            paths["thai"],
            "r",
            encoding="utf-8",
        ) as f:
            thai = f.read()

    else:

        thai = await translate_episode(
            original
        )

        if not thai:
            raise RuntimeError(
                "แปลไม่ได้"
            )

        with open(
            paths["thai"],
            "w",
            encoding="utf-8",
        ) as f:
            f.write(thai)

    # ----------------------------------------
    # TTS
    # ----------------------------------------

    if not valid_mp3(
        paths["mp3"]
    ):

        await create_episode_tts(
            thai,
            paths["mp3"],
        )

    if not valid_mp3(
        paths["mp3"]
    ):
        raise RuntimeError(
            "สร้าง MP3 แล้วแต่ไฟล์เสีย"
        )

    return {
        "episode": episode,
        "url": url,
        "mp3": paths["mp3"],
        "status": "done",
    }


# ============================================================
# DISCOVER ALL EPISODE URLS
# ============================================================

def discover_urls(
    start_url,
    start_episode,
    end_episode,
    job_dir,
    checkpoint,
):
    urls = {}

    current_url = start_url

    for episode in range(
        start_episode,
        end_episode + 1,
    ):

        # checkpoint มี URL อยู่แล้ว
        saved = checkpoint.get(
            "urls",
            {},
        ).get(
            str(episode)
        )

        if saved:
            current_url = saved

        urls[episode] = current_url

        # ไม่ต้องหา next หลังตอนสุดท้าย
        if episode == end_episode:
            break

        cache = episode_paths(
            job_dir,
            episode,
        )

        # ใช้ HTML cache ถ้ามี
        if os.path.exists(
            cache["html"]
        ):

            with open(
                cache["html"],
                "r",
                encoding="utf-8",
            ) as f:
                html = f.read()

        else:

            html = download_page(
                current_url
            )

            with open(
                cache["html"],
                "w",
                encoding="utf-8",
            ) as f:
                f.write(html)

        next_url = find_next_url(
            current_url,
            html,
        )

        if not next_url:
            raise RuntimeError(
                f"หา URL ตอน {episode + 1} ไม่ได้"
            )

        current_url = next_url

        checkpoint.setdefault(
            "urls",
            {},
        )[str(episode + 1)] = current_url

        atomic_json_save(
            checkpoint["_file"],
            checkpoint,
        )

    return urls


# ============================================================
# SELF HEAL
# ============================================================

async def self_heal_episode(
    job_dir,
    episode,
    url,
    status_callback=None,
):
    last_error = None

    for attempt in range(
        1,
        MAX_RETRY + 1,
    ):

        try:

            if status_callback:
                status_callback(
                    episode,
                    f"กำลังซ่อม/ลองใหม่ "
                    f"{attempt}/{MAX_RETRY}",
                )

            result = await process_episode(
                job_dir,
                episode,
                url,
            )

            return result

        except Exception as e:

            last_error = str(e)

            # ------------------------------------
            # ล้างเฉพาะไฟล์ที่อาจเสีย
            # ------------------------------------

            paths = episode_paths(
                job_dir,
                episode,
            )

            if "TTS" in str(e):
                try:
                    if os.path.exists(
                        paths["mp3"]
                    ):
                        os.remove(
                            paths["mp3"]
                        )
                except Exception:
                    pass

            if "แปล" in str(e):
                try:
                    if os.path.exists(
                        paths["thai"]
                    ):
                        os.remove(
                            paths["thai"]
                        )
                except Exception:
                    pass

            if attempt < MAX_RETRY:

                await asyncio.sleep(
                    RETRY_WAIT[
                        min(
                            attempt - 1,
                            len(RETRY_WAIT) - 1,
                        )
                    ]
                )

    raise RuntimeError(
        f"ตอน {episode} ล้มเหลวหลัง "
        f"{MAX_RETRY} ครั้ง: "
        f"{last_error}"
    )


# ============================================================
# CREATE ONE BATCH
# ============================================================

async def process_batch(
    job_dir,
    batch_number,
    episodes,
    urls,
    checkpoint,
    status_callback=None,
):
    results = {}

    # ----------------------------------------
    # ทำหลายตอนพร้อมกันแบบจำกัด
    # ----------------------------------------

    semaphore = asyncio.Semaphore(
        EPISODE_WORKERS
    )

    async def worker(
        episode
    ):
        async with semaphore:

            result = await self_heal_episode(
                job_dir,
                episode,
                urls[episode],
                status_callback,
            )

            results[
                episode
            ] = result

    await asyncio.gather(
        *[
            worker(ep)
            for ep in episodes
        ]
    )

    # ----------------------------------------
    # เรียงตอน
    # ----------------------------------------

    ordered = [
        results[ep]
        for ep in sorted(results)
    ]

    # ----------------------------------------
    # รวม MP3
    # ----------------------------------------

    batch_dir = os.path.join(
        job_dir,
        "batches",
    )

    os.makedirs(
        batch_dir,
        exist_ok=True,
    )

    first_ep = min(episodes)
    last_ep = max(episodes)

    output = os.path.join(
        batch_dir,
        f"ชุด_{batch_number:03d}_"
        f"ตอน_{first_ep}-{last_ep}.mp3",
    )

    if not valid_mp3(output):

        merge_mp3(
            [
                x["mp3"]
                for x in ordered
            ],
            output,
        )

    if not valid_mp3(output):
        raise RuntimeError(
            f"รวมชุด {batch_number} "
            "ไม่สำเร็จ"
        )

    # ----------------------------------------
    # Checkpoint
    # ----------------------------------------

    checkpoint.setdefault(
        "batches",
        {},
    )[str(batch_number)] = {
        "first_episode": first_ep,
        "last_episode": last_ep,
        "file": output,
        "status": "done",
        "updated": timestamp(),
    }

    for ep in episodes:

        checkpoint.setdefault(
            "episodes",
            {},
        )[str(ep)] = {
            "status": "done",
            "mp3": results[ep]["mp3"],
        }

    atomic_json_save(
        checkpoint["_file"],
        checkpoint,
    )

    return output


# ============================================================
# COMPLETE JOB
# ============================================================

async def run_job(
    start_url,
    start_episode,
    end_episode,
    episodes_per_batch,
    progress_callback=None,
    status_callback=None,
):
    os.makedirs(
        OUTPUT_DIR,
        exist_ok=True,
    )

    # ----------------------------------------
    # Job ID
    # ----------------------------------------

    job_id = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    job_dir = os.path.join(
        OUTPUT_DIR,
        "job_" + job_id,
    )

    os.makedirs(
        job_dir,
        exist_ok=True,
    )

    checkpoint_file = os.path.join(
        job_dir,
        "checkpoint.json",
    )

    checkpoint = {
        "created": timestamp(),
        "updated": timestamp(),
        "start_url": start_url,
        "start_episode": start_episode,
        "end_episode": end_episode,
        "episodes_per_batch":
            episodes_per_batch,
        "urls": {},
        "episodes": {},
        "batches": {},
        "status": "running",
        "_file": checkpoint_file,
    }

    atomic_json_save(
        checkpoint_file,
        checkpoint,
    )

    # ----------------------------------------
    # Discover URL
    # ----------------------------------------

    if status_callback:
        status_callback(
            "กำลังค้นหา URL ของแต่ละตอน..."
        )

    urls = discover_urls(
        start_url,
        start_episode,
        end_episode,
        job_dir,
        checkpoint,
    )

    # ----------------------------------------
    # จำนวนชุด
    # ----------------------------------------

    all_episodes = list(
        range(
            start_episode,
            end_episode + 1,
        )
    )

    batches = []

    for i in range(
        0,
        len(all_episodes),
        episodes_per_batch,
    ):
        batches.append(
            all_episodes[
                i:i + episodes_per_batch
            ]
        )

    total_batches = len(
        batches
    )

    batch_files = []

    # ----------------------------------------
    # ทำทีละชุด
    # ----------------------------------------

    for batch_index, episodes in enumerate(
        batches,
        start=1,
    ):

        # ------------------------------------
        # ถ้าชุดนี้เสร็จแล้ว
        # ข้ามทันที
        # ------------------------------------

        saved_batch = checkpoint.get(
            "batches",
            {},
        ).get(
            str(batch_index)
        )

        if (
            saved_batch
            and saved_batch.get(
                "status"
            ) == "done"
            and os.path.exists(
                saved_batch.get(
                    "file",
                    "",
                )
            )
        ):

            batch_files.append(
                saved_batch["file"]
            )

            if status_callback:
                status_callback(
                    f"ชุด {batch_index}/"
                    f"{total_batches} "
                    "มีอยู่แล้ว — ข้าม"
                )

            continue

        if status_callback:
            status_callback(
                f"กำลังสร้างชุด "
                f"{batch_index}/{total_batches} "
                f"ตอน {episodes[0]}-"
                f"{episodes[-1]}"
            )

        # ------------------------------------
        # สร้างชุด
        # ------------------------------------

        batch_file = await process_batch(
            job_dir,
            batch_index,
            episodes,
            urls,
            checkpoint,
            status_callback,
        )

        batch_files.append(
            batch_file
        )

        if progress_callback:
            progress_callback(
                batch_index,
                total_batches,
            )

    # ----------------------------------------
    # สร้าง ZIP รวมทุกชุด
    # ----------------------------------------

    zip_path = os.path.join(
        job_dir,
        "นิยายเสียง_ทั้งหมด.zip",
    )

    with __import__(
        "zipfile"
    ).ZipFile(
        zip_path,
        "w",
        __import__(
            "zipfile"
        ).ZIP_DEFLATED,
    ) as z:

        for file in batch_files:

            if os.path.exists(file):

                z.write(
                    file,
                    arcname=os.path.basename(
                        file
                    ),
                )

    checkpoint["status"] = "complete"
    checkpoint["completed"] = timestamp()
    checkpoint["zip"] = zip_path

    # ลบตัวช่วยภายในก่อนบันทึก
    checkpoint.pop(
        "_file",
        None,
    )

    atomic_json_save(
        checkpoint_file,
        checkpoint,
    )

    return {
        "job_dir": job_dir,
        "zip": zip_path,
        "batches": batch_files,
        "total_episodes":
            len(all_episodes),
        "total_batches":
            total_batches,
    }


# ============================================================
# STREAMLIT
# ============================================================

st.set_page_config(
    page_title=APP_TITLE,
    page_icon="🎙️",
    layout="centered",
)

st.title(
    APP_TITLE
)

st.caption(
    "ใส่ข้อมูล 4 อย่าง แล้วกดเริ่มครั้งเดียว"
)

st.info(
    "เสียงที่ใช้: Microsoft Edge TTS — "
    "Premwadee (th-TH-PremwadeeNeural)"
)


# ============================================================
# INPUT
# ============================================================

novel_url = st.text_input(
    "1️⃣ ลิงก์นิยาย",
    placeholder="วางลิงก์ตอนแรกที่นี่",
)

start_episode = st.number_input(
    "2️⃣ ตอนเริ่มต้น",
    min_value=1,
    value=1,
    step=1,
)

end_episode = st.number_input(
    "3️⃣ ตอนสุดท้าย",
    min_value=1,
    value=500,
    step=1,
)

episodes_per_batch = st.number_input(
    "4️⃣ จำนวนตอนต่อชุด",
    min_value=1,
    value=50,
    step=1,
)


# ============================================================
# SUMMARY
# ============================================================

if end_episode >= start_episode:

    total = (
        int(end_episode)
        - int(start_episode)
        + 1
    )

    batches = (
        total
        + int(episodes_per_batch)
        - 1
    ) // int(episodes_per_batch)

    st.success(
        f"ระบบจะสร้างทั้งหมด "
        f"{total} ตอน → "
        f"{batches} ชุด"
    )


# ============================================================
# START
# ============================================================

start = st.button(
    "🚀 เริ่มสร้างทั้งหมด",
    type="primary",
    use_container_width=True,
)


if start:

    if not novel_url.strip():

        st.error(
            "กรุณาใส่ลิงก์นิยาย"
        )

        st.stop()

    if end_episode < start_episode:

        st.error(
            "ตอนสุดท้ายต้องมากกว่าหรือเท่ากับตอนเริ่ม"
        )

        st.stop()

    if episodes_per_batch < 1:

        st.error(
            "จำนวนตอนต่อชุดต้องมากกว่า 0"
        )

        st.stop()

    progress_bar = st.progress(
        0
    )

    status_box = st.empty()

    episode_box = st.empty()

    started_at = time.time()

    def update_progress(
        current,
        total,
    ):
        progress_bar.progress(
            int(
                current
                / total
                * 100
            )
        )

    def update_status(
        *args,
    ):
        text = " ".join(
            str(x)
            for x in args
        )

        status_box.info(
            text
        )

    try:

        result = asyncio.run(
            run_job(
                start_url=novel_url.strip(),
                start_episode=int(
                    start_episode
                ),
                end_episode=int(
                    end_episode
                ),
                episodes_per_batch=int(
                    episodes_per_batch
                ),
                progress_callback=
                    update_progress,
                status_callback=
                    update_status,
            )
        )

        elapsed = int(
            time.time()
            - started_at
        )

        progress_bar.progress(
            100
        )

        status_box.success(
            "🎉 สร้างเสร็จทั้งหมดแล้ว"
        )

        st.success(
            f"สร้าง {result['total_episodes']} "
            f"ตอนสำเร็จใน "
            f"{elapsed // 60} นาที "
            f"{elapsed % 60} วินาที"
        )

        # ------------------------------------
        # BATCH DOWNLOAD
        # ------------------------------------

        st.subheader(
            "📦 ไฟล์ชุด"
        )

        for index, path in enumerate(
            result["batches"],
            start=1,
        ):

            if not os.path.exists(
                path
            ):
                continue

            filename = os.path.basename(
                path
            )

            with open(
                path,
                "rb",
            ) as f:

                data = f.read()

            st.download_button(
                f"⬇️ {filename}",
                data=data,
                file_name=filename,
                mime="audio/mpeg",
                key=(
                    "download_batch_"
                    + str(index)
                    + "_"
                    + filename
                ),
                use_container_width=True,
            )

        # ------------------------------------
        # ALL ZIP
        # ------------------------------------

        if os.path.exists(
            result["zip"]
        ):

            st.subheader(
                "📚 ดาวน์โหลดทั้งหมด"
            )

            with open(
                result["zip"],
                "rb",
            ) as f:

                zip_data = f.read()

            st.download_button(
                "⬇️ ดาวน์โหลดทุกชุดเป็น ZIP",
                data=zip_data,
                file_name=(
                    "นิยายเสียง_ทั้งหมด.zip"
                ),
                mime="application/zip",
                key="download_all_zip",
                use_container_width=True,
            )

        st.success(
            "งานทั้งหมดเสร็จแล้ว"
        )

    except Exception as e:

        st.error(
            "ระบบหยุดจากข้อผิดพลาด: "
            + str(e)
        )

        st.warning(
            "Checkpoint ถูกบันทึกไว้ "
            "ไฟล์ที่สร้างสำเร็จแล้วจะไม่ถูกทำซ้ำ "
            "เมื่อเริ่มงานใหม่"
        )


# ============================================================
# FOOTER
# ============================================================

st.divider()

st.caption(
    "ระบบจะตรวจสอบไฟล์ที่มีอยู่แล้ว "
    "และพยายามกู้คืนงานที่ค้างโดยอัตโนมัติ"
                    )
