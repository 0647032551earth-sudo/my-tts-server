import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import zipfile
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup
import edge_tts
import streamlit as st


# ============================================================
# NOVEL TTS HYPER PIPELINE
# Microsoft Edge TTS - Thai Premwadee
# ============================================================

APP_NAME = "Novel TTS Hyper Pipeline"

OUTPUT_ROOT = "output"

DEFAULT_VOICE = "th-TH-PremwadeeNeural"

TTS_PART_CHARS = 3000
TRANSLATE_PART_CHARS = 3500

TTS_PARALLEL = 6
TRANSLATE_PARALLEL = 3
EPISODE_CONCURRENCY = 3

REQUEST_TIMEOUT = 30

HTTP_RETRIES = 4
TRANSLATE_RETRIES = 4
TTS_RETRIES = 4
EPISODE_RETRIES = 3

QUEUE_SIZE = 6

RETRY_BACKOFF = (3, 10, 30, 60)

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,th;q=0.8",
}

NEXT_MARKERS = (
    "next",
    "next chapter",
    "ต่อไป",
    "ถัดไป",
    ">>",
    "下一章",
    "下一页",
    "下一頁",
    "次へ",
)


# ============================================================
# GENERAL
# ============================================================

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def sleep_retry(attempt):
    time.sleep(
        RETRY_BACKOFF[
            min(attempt - 1, len(RETRY_BACKOFF) - 1)
        ]
    )


def safe_filename(name):
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", "_", name)
    return name[:150]


def clean_text(text):
    if not text:
        return ""

    text = text.replace("\xa0", " ")
    text = text.replace("\r\n", "\n")
    text = text.replace("\r", "\n")
    text = text.replace("\u200b", "")
    text = text.replace("\ufeff", "")

    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def prepare_tts_text(text, remove_spaces=False):
    text = clean_text(text)

    if remove_spaces:
        # ตัดเฉพาะ space/tab
        # ไม่เปลี่ยนคำหรือเครื่องหมายอื่น
        text = re.sub(r"[ \t]+", "", text)

    return text.strip()


# ============================================================
# TEXT SPLITTER
# ============================================================

def split_text(text, max_chars):
    text = clean_text(text)

    if not text:
        return []

    if len(text) <= max_chars:
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

        if len(candidate) <= max_chars:
            current = candidate
            continue

        if current:
            result.append(current)
            current = ""

        while len(paragraph) > max_chars:
            positions = [
                paragraph.rfind("。", 0, max_chars),
                paragraph.rfind("！", 0, max_chars),
                paragraph.rfind("？", 0, max_chars),
                paragraph.rfind(".", 0, max_chars),
                paragraph.rfind("!", 0, max_chars),
                paragraph.rfind("?", 0, max_chars),
                paragraph.rfind(" ", 0, max_chars),
            ]

            cut = max(positions)

            if cut < max_chars // 2:
                cut = max_chars

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

def fetch_page(url):
    last_error = None

    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            r = requests.get(
                url,
                headers=HTTP_HEADERS,
                timeout=REQUEST_TIMEOUT,
            )

            r.raise_for_status()

            return r.text

        except Exception as e:
            last_error = str(e)

            if attempt < HTTP_RETRIES:
                sleep_retry(attempt)

    raise RuntimeError(
        f"โหลดหน้าเว็บไม่ได้: {last_error}"
    )


# ============================================================
# HTML
# ============================================================

def extract_text(html):
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

        if len(text) >= 20:
            paragraphs.append(text)

    if paragraphs:
        return "\n\n".join(paragraphs)

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

    # 1. rel=next
    for a in soup.find_all("a"):
        rel = a.get("rel")

        if not rel:
            continue

        rel_text = " ".join(rel).lower()

        if "next" in rel_text:
            href = a.get("href")

            if href:
                return urllib.parse.urljoin(
                    current_url,
                    href,
                )

    # 2. ปุ่ม Next / ต่อไป
    candidates = []

    for a in soup.find_all("a"):
        href = a.get("href")

        if not href:
            continue

        text = clean_text(
            a.get_text(
                " ",
                strip=True,
            )
        ).lower()

        score = 0

        for marker in NEXT_MARKERS:
            if marker in text:
                score += 10

        if text in (
            "next",
            "next chapter",
            "ต่อไป",
            "ถัดไป",
        ):
            score += 30

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

    # 3. URL /1 /2 /3
    m = re.search(
        r"(\d+)(?!.*\d)",
        current_url,
    )

    if m:
        number = int(m.group(1)) + 1

        return (
            current_url[:m.start(1)]
            + str(number)
            + current_url[m.end(1):]
        )

    return None


# ============================================================
# GOOGLE TRANSLATE
# ============================================================

def translate_one(text, src_lang="auto"):
    endpoint = (
        "https://translate.googleapis.com/"
        "translate_a/single"
    )

    params = {
        "client": "gtx",
        "sl": src_lang,
        "tl": "th",
        "dt": "t",
        "q": text,
    }

    last_error = None

    for attempt in range(
        1,
        TRANSLATE_RETRIES + 1,
    ):
        try:
            r = requests.get(
                endpoint,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )

            r.raise_for_status()

            data = r.json()

            result = ""

            for item in data[0]:
                if item and item[0]:
                    result += item[0]

            result = clean_text(result)

            if result:
                return result

            raise RuntimeError(
                "Translate ไม่คืนข้อความ"
            )

        except Exception as e:
            last_error = str(e)

            if attempt < TRANSLATE_RETRIES:
                sleep_retry(attempt)

    raise RuntimeError(
        f"แปลภาษาไม่ได้: {last_error}"
    )


async def translate_text(
    text,
    src_lang,
    parallel,
):
    text = clean_text(text)

    if not text:
        return ""

    parts = split_text(
        text,
        TRANSLATE_PART_CHARS,
    )

    semaphore = asyncio.Semaphore(
        max(1, parallel)
    )

    loop = asyncio.get_running_loop()

    async def worker(index, part):
        async with semaphore:
            result = await loop.run_in_executor(
                None,
                translate_one,
                part,
                src_lang,
            )

            return index, result

    results = await asyncio.gather(
        *[
            worker(i, part)
            for i, part in enumerate(parts)
        ]
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

async def tts_one(
    text,
    output_file,
    voice,
):
    last_error = None

    for attempt in range(
        1,
        TTS_RETRIES + 1,
    ):
        try:
            communicate = edge_tts.Communicate(
                text,
                voice,
            )

            await communicate.save(
                output_file
            )

            if (
                os.path.exists(output_file)
                and os.path.getsize(output_file) > 1000
            ):
                return

            raise RuntimeError(
                "ไฟล์เสียงมีขนาดผิดปกติ"
            )

        except Exception as e:
            last_error = str(e)

            if os.path.exists(output_file):
                try:
                    os.remove(output_file)
                except Exception:
                    pass

            if attempt < TTS_RETRIES:
                await asyncio.sleep(
                    RETRY_BACKOFF[
                        min(
                            attempt - 1,
                            len(RETRY_BACKOFF) - 1,
                        )
                    ]
                )

    raise RuntimeError(
        f"TTS ล้มเหลว: {last_error}"
    )


async def tts_episode(
    text,
    output_file,
    voice,
    parallel,
    remove_spaces=False,
):
    text = prepare_tts_text(
        text,
        remove_spaces,
    )

    if not text:
        raise RuntimeError(
            "ไม่มีข้อความสำหรับ TTS"
        )

    parts = split_text(
        text,
        TTS_PART_CHARS,
    )

    if len(parts) == 1:
        await tts_one(
            parts[0],
            output_file,
            voice,
        )
        return

    work_dir = tempfile.mkdtemp(
        prefix="tts_parts_"
    )

    semaphore = asyncio.Semaphore(
        max(1, parallel)
    )

    try:
        async def worker(index, part):
            part_file = os.path.join(
                work_dir,
                f"{index:05d}.mp3",
            )

            async with semaphore:
                await tts_one(
                    part,
                    part_file,
                    voice,
                )

            return index, part_file

        results = await asyncio.gather(
            *[
                worker(i, part)
                for i, part in enumerate(parts)
            ]
        )

        results.sort(
            key=lambda x: x[0]
        )

        merge_mp3(
            [x[1] for x in results],
            output_file,
        )

    finally:
        shutil.rmtree(
            work_dir,
            ignore_errors=True,
        )


# ============================================================
# FFMPEG
# ============================================================

def ffmpeg_available():
    return shutil.which("ffmpeg") is not None


def merge_mp3(files, output):
    if not files:
        raise RuntimeError(
            "ไม่มีไฟล์ MP3 สำหรับรวม"
        )

    if len(files) == 1:
        shutil.copyfile(
            files[0],
            output,
        )
        return

    if not ffmpeg_available():
        raise RuntimeError(
            "ไม่พบ ffmpeg ในเครื่อง"
        )

    list_file = output + ".txt"

    with open(
        list_file,
        "w",
        encoding="utf-8",
    ) as f:
        for path in files:
            path = os.path.abspath(path)
            path = path.replace("'", "'\\''")

            f.write(
                "file '"
                + path
                + "'\n"
            )

    temp_output = output + ".tmp.mp3"

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
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        os.replace(
            temp_output,
            output,
        )

    except subprocess.CalledProcessError as e:
        error = e.stderr.decode(
            "utf-8",
            errors="ignore",
        )

        raise RuntimeError(
            "ffmpeg merge ล้มเหลว:\n"
            + error[-2000:]
        )

    finally:
        for p in (
            list_file,
            temp_output,
        ):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


# ============================================================
# CHECKPOINT
# ============================================================

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


def save_json(path, data):
    data["updated"] = now()

    temp = path + ".tmp"

    with open(
        temp,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2,
        )

    os.replace(
        temp,
        path,
    )


def new_checkpoint(
    start_url,
    start_ep,
    end_ep,
    batch_size,
    src_lang,
    voice,
):
    return {
        "version": 10,
        "created": now(),
        "updated": now(),
        "start_url": start_url,
        "start_ep": start_ep,
        "end_ep": end_ep,
        "batch_size": batch_size,
        "src_lang": src_lang,
        "voice": voice,
        "episodes": {},
        "finished": False,
    }


# ============================================================
# EPISODE PROCESSING
# ============================================================

async def process_episode(
    episode_no,
    url,
    work_dir,
    src_lang,
    voice,
    tts_parallel,
    translate_parallel,
    remove_spaces,
):
    episode_dir = os.path.join(
        work_dir,
        f"episode_{episode_no:05d}",
    )

    os.makedirs(
        episode_dir,
        exist_ok=True,
    )

    html_file = os.path.join(
        episode_dir,
        "page.html",
    )

    original_file = os.path.join(
        episode_dir,
        "original.txt",
    )

    thai_file = os.path.join(
        episode_dir,
        "thai.txt",
    )

    mp3_file = os.path.join(
        episode_dir,
        f"episode_{episode_no:05d}.mp3",
    )

    # --------------------------------------------------------
    # HTML CACHE
    # --------------------------------------------------------

    if os.path.exists(html_file):
        with open(
            html_file,
            "r",
            encoding="utf-8",
        ) as f:
            html = f.read()
    else:
        loop = asyncio.get_running_loop()

        html = await loop.run_in_executor(
            None,
            fetch_page,
            url,
        )

        with open(
            html_file,
            "w",
            encoding="utf-8",
        ) as f:
            f.write(html)

    # --------------------------------------------------------
    # ORIGINAL
    # --------------------------------------------------------

    if os.path.exists(original_file):
        with open(
            original_file,
            "r",
            encoding="utf-8",
        ) as f:
            original = f.read()
    else:
        original = extract_text(html)

        if not original:
            raise RuntimeError(
                "ไม่พบข้อความในหน้าเว็บ"
            )

        with open(
            original_file,
            "w",
            encoding="utf-8",
        ) as f:
            f.write(original)

    # --------------------------------------------------------
    # TRANSLATION
    # --------------------------------------------------------

    if os.path.exists(thai_file):
        with open(
            thai_file,
            "r",
            encoding="utf-8",
        ) as f:
            thai = f.read()
    else:
        thai = await translate_text(
            original,
            src_lang,
            translate_parallel,
        )

        if not thai:
            raise RuntimeError(
                "แปลภาษาแล้วไม่มีข้อความ"
            )

        with open(
            thai_file,
            "w",
            encoding="utf-8",
        ) as f:
            f.write(thai)

    # --------------------------------------------------------
    # TTS
    # --------------------------------------------------------

    if not (
        os.path.exists(mp3_file)
        and os.path.getsize(mp3_file) > 1000
    ):
        await tts_episode(
            thai,
            mp3_file,
            voice,
            tts_parallel,
            remove_spaces,
        )

    # --------------------------------------------------------
    # NEXT URL
    # --------------------------------------------------------

    next_url = find_next_url(
        url,
        html,
    )

    return {
        "episode": episode_no,
        "url": url,
        "next_url": next_url,
        "original_file": original_file,
        "thai_file": thai_file,
        "mp3_file": mp3_file,
        "status": "success",
    }


# ============================================================
# BATCH
# ============================================================

def create_batch(
    episode_results,
    batch_no,
    work_dir,
):
    successful = [
        x
        for x in episode_results
        if x.get("status") == "success"
        and os.path.exists(x["mp3_file"])
    ]

    successful.sort(
        key=lambda x: x["episode"]
    )

    if not successful:
        return None

    batch_dir = os.path.join(
        work_dir,
        "batches",
    )

    os.makedirs(
        batch_dir,
        exist_ok=True,
    )

    first_ep = successful[0]["episode"]
    last_ep = successful[-1]["episode"]

    output = os.path.join(
        batch_dir,
        f"batch_{batch_no:04d}_"
        f"ep_{first_ep:05d}-"
        f"{last_ep:05d}.mp3",
    )

    merge_mp3(
        [
            x["mp3_file"]
            for x in successful
        ],
        output,
    )

    return output


def create_zip(
    batch_files,
    work_dir,
):
    if not batch_files:
        return None

    zip_path = os.path.join(
        work_dir,
        "Novel_TTS_Batches.zip",
    )

    with zipfile.ZipFile(
        zip_path,
        "w",
        zipfile.ZIP_DEFLATED,
    ) as z:
        for path in batch_files:
            if os.path.exists(path):
                z.write(
                    path,
                    arcname=os.path.basename(path),
                )

    return zip_path


# ============================================================
# CONTINUOUS PIPELINE
# ============================================================

async def run_pipeline(
    start_url,
    start_ep,
    end_ep,
    batch_size,
    src_lang,
    voice,
    episode_concurrency,
    tts_parallel,
    translate_parallel,
    remove_spaces,
    max_episode_retries,
):
    os.makedirs(
        OUTPUT_ROOT,
        exist_ok=True,
    )

    job_name = (
        datetime.now()
        .strftime("%Y%m%d_%H%M%S")
    )

    work_dir = os.path.join(
        OUTPUT_ROOT,
        f"job_{job_name}",
    )

    os.makedirs(
        work_dir,
        exist_ok=True,
    )

    checkpoint_file = os.path.join(
        work_dir,
        "checkpoint.json",
    )

    checkpoint = new_checkpoint(
        start_url,
        start_ep,
        end_ep,
        batch_size,
        src_lang,
        voice,
    )

    save_json(
        checkpoint_file,
        checkpoint,
    )

    # --------------------------------------------------------
    # URL PRODUCER
    #
    # สำคัญ:
    # URL ของตอนถัดไปต้องได้จากตอนก่อนหน้า
    # ดังนั้น producer เดิน URL ตามลำดับ
    # แต่ worker สามารถประมวลผลหลายตอนได้พร้อมกัน
    # --------------------------------------------------------

    queue = asyncio.Queue(
        maxsize=max(
            1,
            episode_concurrency,
            QUEUE_SIZE,
        )
    )

    results = {}
    result_lock = asyncio.Lock()

    producer_error = None

    async def producer():
        nonlocal producer_error

        current_url = start_url

        for episode_no in range(
            start_ep,
            end_ep + 1,
        ):
            try:
                await queue.put(
                    (
                        episode_no,
                        current_url,
                    )
                )

                # ต้อง fetch เพื่อหา URL ตอนถัดไป
                loop = asyncio.get_running_loop()

                html = await loop.run_in_executor(
                    None,
                    fetch_page,
                    current_url,
                )

                next_url = find_next_url(
                    current_url,
                    html,
                )

                if not next_url:
                    if episode_no < end_ep:
                        raise RuntimeError(
                            f"หา URL ตอน {episode_no + 1} ไม่ได้"
                        )

                    break

                current_url = next_url

            except Exception as e:
                producer_error = str(e)
                break

        for _ in range(
            episode_concurrency
        ):
            await queue.put(None)

    async def worker(worker_id):
        while True:
            item = await queue.get()

            if item is None:
                queue.task_done()
                return

            episode_no, url = item

            try:
                success = False
                last_error = None

                for attempt in range(
                    1,
                    max_episode_retries + 1,
                ):
                    try:
                        result = await process_episode(
                            episode_no,
                            url,
                            work_dir,
                            src_lang,
                            voice,
                            tts_parallel,
                            translate_parallel,
                            remove_spaces,
                        )

                        success = True
                        break

                    except Exception as e:
                        last_error = str(e)

                        if attempt < max_episode_retries:
                            await asyncio.sleep(
                                RETRY_BACKOFF[
                                    min(
                                        attempt - 1,
                                        len(RETRY_BACKOFF) - 1,
                                    )
                                ]
                            )

                async with result_lock:
                    if success:
                        results[episode_no] = result

                        checkpoint["episodes"][
                            str(episode_no)
                        ] = result

                    else:
                        results[episode_no] = {
                            "episode": episode_no,
                            "url": url,
                            "status": "failed",
                            "error": last_error,
                        }

                        checkpoint["episodes"][
                            str(episode_no)
                        ] = results[episode_no]

                    save_json(
                        checkpoint_file,
                        checkpoint,
                    )

            finally:
                queue.task_done()

    producer_task = asyncio.create_task(
        producer()
    )

    workers = [
        asyncio.create_task(
            worker(i)
        )
        for i in range(
            max(1, episode_concurrency)
        )
    ]

    await producer_task
    await queue.join()

    await asyncio.gather(
        *workers,
        return_exceptions=True,
    )

    # --------------------------------------------------------
    # BATCH BUILD
    # --------------------------------------------------------

    successful = [
        results[k]
        for k in sorted(results)
        if results[k].get("status")
        == "success"
    ]

    failed = [
        k
        for k in range(
            start_ep,
            end_ep + 1,
        )
        if k not in results
        or results[k].get("status")
        != "success"
    ]

    batch_files = []

    for i in range(
        0,
        len(successful),
        batch_size,
    ):
        batch_items = successful[
            i:i + batch_size
        ]

        batch_no = (
            i // batch_size
        ) + 1

        path = create_batch(
            batch_items,
            batch_no,
            work_dir,
        )

        if path:
            batch_files.append(path)

            checkpoint["batches"][
                str(batch_no)
            ] = {
                "file": path,
                "episodes": [
                    x["episode"]
                    for x in batch_items
                ],
                "status": "success",
            }

            save_json(
                checkpoint_file,
                checkpoint,
            )

    zip_path = create_zip(
        batch_files,
        work_dir,
    )

    checkpoint["finished"] = (
        len(failed) == 0
    )

    checkpoint["missing"] = failed

    save_json(
        checkpoint_file,
        checkpoint,
    )

    return {
        "work_dir": work_dir,
        "checkpoint": checkpoint_file,
        "results": results,
        "batch_files": batch_files,
        "zip": zip_path,
        "missing": failed,
        "producer_error": producer_error,
    }


# ============================================================
# STREAMLIT UI
# ============================================================

st.set_page_config(
    page_title=APP_NAME,
    page_icon="🎙️",
    layout="wide",
)

st.title(
    "🎙️ Novel → Thai Speech Factory"
)

st.caption(
    "Crawl → Translate → Edge TTS → MP3 → Batch → ZIP"
)

st.info(
    "เสียงเริ่มต้น: Microsoft Edge TTS — "
    "th-TH-PremwadeeNeural"
)


# ============================================================
# MAIN SETTINGS
# ============================================================

st.subheader(
    "⚙️ ตั้งค่าหลัก"
)

url = st.text_input(
    "URL ตอนเริ่มต้น",
    placeholder="https://example.com/novel/1",
)

c1, c2, c3 = st.columns(3)

with c1:
    start_ep = st.number_input(
        "ตอนเริ่ม",
        min_value=1,
        value=1,
        step=1,
    )

with c2:
    end_ep = st.number_input(
        "ตอนจบ",
        min_value=1,
        value=10,
        step=1,
    )

with c3:
    batch_size = st.number_input(
        "ตอนต่อ Batch",
        min_value=1,
        value=10,
        step=1,
    )

c4, c5 = st.columns(2)

with c4:
    src_lang = st.text_input(
        "ภาษาต้นฉบับ",
        value="auto",
    )

with c5:
    voice = st.text_input(
        "เสียง TTS",
        value=DEFAULT_VOICE,
    )


# ============================================================
# PERFORMANCE
# ============================================================

st.subheader(
    "🚀 Performance"
)

p1, p2, p3, p4 = st.columns(4)

with p1:
    episode_concurrency = st.number_input(
        "ตอนพร้อมกัน",
        min_value=1,
        max_value=8,
        value=EPISODE_CONCURRENCY,
        step=1,
    )

with p2:
    tts_parallel = st.number_input(
        "TTS พร้อมกัน",
        min_value=1,
        max_value=16,
        value=TTS_PARALLEL,
        step=1,
    )

with p3:
    translate_parallel = st.number_input(
        "แปลพร้อมกัน",
        min_value=1,
        max_value=8,
        value=TRANSLATE_PARALLEL,
        step=1,
    )

with p4:
    max_episode_retries = st.number_input(
        "Retry ต่อตอน",
        min_value=1,
        max_value=10,
        value=EPISODE_RETRIES,
        step=1,
    )

remove_spaces = st.checkbox(
    "ตัดเฉพาะช่องว่างก่อนส่งเข้า TTS",
    value=False,
)


# ============================================================
# START
# ============================================================

st.divider()

if st.button(
    "🚀 START PIPELINE",
    type="primary",
    use_container_width=True,
):
    if not url.strip():
        st.error(
            "กรุณาใส่ URL ตอนเริ่มต้น"
        )

    elif end_ep < start_ep:
        st.error(
            "ตอนจบต้องมากกว่าหรือเท่ากับตอนเริ่ม"
        )

    else:
        progress = st.empty()
        status = st.empty()

        status.info(
            "กำลังเริ่ม Pipeline..."
        )

        started = time.time()

        try:
            result = asyncio.run(
                run_pipeline(
                    start_url=url.strip(),
                    start_ep=int(start_ep),
                    end_ep=int(end_ep),
                    batch_size=int(batch_size),
                    src_lang=(
                        src_lang.strip()
                        or "auto"
                    ),
                    voice=(
                        voice.strip()
                        or DEFAULT_VOICE
                    ),
                    episode_concurrency=int(
                        episode_concurrency
                    ),
                    tts_parallel=int(
                        tts_parallel
                    ),
                    translate_parallel=int(
                        translate_parallel
                    ),
                    remove_spaces=remove_spaces,
                    max_episode_retries=int(
                        max_episode_retries
                    ),
                )
            )

            elapsed = int(
                time.time() - started
            )

            progress.success(
                "Pipeline เสร็จแล้ว"
            )

            status.success(
                f"ใช้เวลา {elapsed // 60} นาที "
                f"{elapsed % 60} วินาที"
            )

            missing = result.get(
                "missing",
                [],
            )

            producer_error = result.get(
                "producer_error"
            )

            if producer_error:
                st.warning(
                    "Producer หยุด: "
                    + producer_error
                )

            if missing:
                st.warning(
                    "ตอนที่ยังไม่สำเร็จ: "
                    + ", ".join(
                        map(
                            str,
                            missing,
                        )
                    )
                )
            else:
                st.success(
                    "🎉 สำเร็จครบทุกตอน"
                )

            # ------------------------------------------------
            # BATCH DOWNLOAD
            # ------------------------------------------------

            st.subheader(
                "🎧 Batch MP3"
            )

            for path in result.get(
                "batch_files",
                [],
            ):
                if not os.path.exists(path):
                    continue

                filename = os.path.basename(
                    path
                )

                with open(
                    path,
                    "rb",
                ) as f:
                    st.download_button(
                        "⬇️ "
                        + filename,
                        data=f.read(),
                        file_name=filename,
                        mime="audio/mpeg",
                        key=(
                            "batch_"
                            + filename
                        ),
                    )

            # ------------------------------------------------
            # ZIP
            # ------------------------------------------------

            zip_path = result.get(
                "zip"
            )

            if (
                zip_path
                and os.path.exists(zip_path)
            ):
                st.subheader(
                    "📦 ZIP"
                )

                with open(
                    zip_path,
                    "rb",
                ) as f:
                    st.download_button(
                        "⬇️ ดาวน์โหลด ZIP ทั้งหมด",
                        data=f.read(),
                        file_name=os.path.basename(
                            zip_path
                        ),
                        mime="application/zip",
                    )

            # ------------------------------------------------
            # LOCATION
            # ------------------------------------------------

            st.success(
                "ไฟล์ทั้งหมดอยู่ที่โฟลเดอร์:\n\n"
                + result["work_dir"]
            )

        except Exception as e:
            st.error(
                "Pipeline เกิดข้อผิดพลาด:\n"
                + str(e)
            )


# ============================================================
# MANUAL TTS
# ============================================================

st.divider()

st.subheader(
    "📝 Manual TTS"
)

manual_text = st.text_area(
    "ข้อความสำหรับสร้าง MP3",
    height=220,
)

manual_remove_spaces = st.checkbox(
    "Manual TTS: ตัดเฉพาะช่องว่าง",
    value=False,
)

if st.button(
    "🔊 สร้าง MP3",
    use_container_width=True,
):
    if not manual_text.strip():
        st.error(
            "กรุณาใส่ข้อความ"
        )

    else:
        manual_dir = os.path.join(
            OUTPUT_ROOT,
            "manual",
        )

        os.makedirs(
            manual_dir,
            exist_ok=True,
        )

        output = os.path.join(
            manual_dir,
            "manual_tts.mp3",
        )

        try:
            asyncio.run(
                tts_episode(
                    manual_text,
                    output,
                    voice.strip()
                    or DEFAULT_VOICE,
                    int(tts_parallel),
                    manual_remove_spaces,
                )
            )

            st.success(
                "สร้าง MP3 สำเร็จ"
            )

            with open(
                output,
                "rb",
            ) as f:
                st.download_button(
                    "⬇️ ดาวน์โหลด MP3",
                    data=f.read(),
                    file_name="manual_tts.mp3",
                    mime="audio/mpeg",
                )

            st.audio(
                output,
                format="audio/mp3",
            )

        except Exception as e:
            st.error(
                "สร้าง MP3 ไม่สำเร็จ: "
                + str(e)
            )


# ============================================================
# CHECKPOINT SEARCH
# ============================================================

st.divider()

st.subheader(
    "🔄 Checkpoint"
)

if st.button(
    "🔍 ตรวจหา Checkpoint",
    use_container_width=True,
):
    if not os.path.exists(
        OUTPUT_ROOT
    ):
        st.info(
            "ยังไม่มีงานเก่า"
        )

    else:
        checkpoints = []

        for root, dirs, files in os.walk(
            OUTPUT_ROOT
        ):
            if (
                "checkpoint.json"
                in files
            ):
                checkpoints.append(
                    os.path.join(
                        root,
                        "checkpoint.json",
                    )
                )

        if not checkpoints:
            st.info(
                "ไม่พบ Checkpoint"
            )

        else:
            st.success(
                f"พบ {len(checkpoints)} งาน"
            )

            for path in checkpoints:
                cp = load_json(path)

                if not cp:
                    continue

                episodes = cp.get(
                    "episodes",
                    {},
                )

                success_count = sum(
                    1
                    for x in episodes.values()
                    if x.get("status")
                    == "success"
                )

                st.write(
                    f"📁 {path} — "
                    f"สำเร็จ "
                    f"{success_count} ตอน"
            )
