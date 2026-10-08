"""
Novel to Speech Automation V2
- ดึงนิยาย
- แปลเป็นไทย
- Edge TTS
- Premwadee
- Batch
- Checkpoint / Resume
- Auto Retry
- Skip Error
- Partial Batch
- ZIP Download
"""

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import zipfile
import io

import requests
from bs4 import BeautifulSoup

try:
    import edge_tts
except ImportError:
    edge_tts = None

try:
    import streamlit as st
except ImportError:
    st = None


# =========================================================
# CONFIG
# =========================================================

OUTPUT_ROOT = "output"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/130 Safari/537.36"
    )
}

TTS_PART_CHARS = 1500
TTS_PARALLEL = 2

NEXT_MARKERS = (
    "next",
    "ถัดไป",
    ">>",
    "下一页",
    "下一章",
    "下一頁",
    "次へ",
)


# =========================================================
# TEXT
# =========================================================

def clean_text(text):
    text = text or ""

    # ลบ control characters แต่คง \n / \t
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", text)

    # normalize ช่องว่าง
    text = re.sub(r"[ \t]+", " ", text)

    # normalize บรรทัดว่าง
    text = re.sub(r"\n\s*\n+", "\n\n", text)

    return text.strip()


def split_text(text, limit):
    """
    แบ่งข้อความโดยพยายามตัดตามประโยคก่อน
    """

    text = clean_text(text)

    if not text:
        return []

    parts = []
    buf = ""

    separators = (
        "。",
        "！",
        "？",
        ". ",
        "! ",
        "? ",
        "；",
        "，",
        ", ",
        " ",
    )

    def flush():
        nonlocal buf

        if buf.strip():
            parts.append(buf.strip())

        buf = ""

    for line in text.splitlines():

        line = line.strip()

        if not line:
            continue

        while len(line) > limit:

            candidates = [
                line.rfind(ch, 0, limit)
                for ch in separators
            ]

            cut = max(candidates)

            if cut < limit // 3:
                cut = limit - 1

            piece = line[:cut + 1].strip()

            if piece:
                parts.append(piece)

            line = line[cut + 1:].strip()

        if not line:
            continue

        if len(buf) + len(line) + 1 > limit:
            flush()

        buf += line + "\n"

    flush()

    return [p for p in parts if p.strip()]


# =========================================================
# HTTP
# =========================================================

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def fetch_page(url, tries=4):
    """
    โหลดหน้าเว็บพร้อม retry
    """

    last = None

    for attempt in range(1, tries + 1):

        try:
            r = SESSION.get(
                url,
                timeout=(10, 30),
                allow_redirects=True,
            )

            r.raise_for_status()

            if (r.encoding or "").lower() == "iso-8859-1":
                r.encoding = r.apparent_encoding

            return BeautifulSoup(r.text, "html.parser")

        except Exception as e:

            last = e

            if attempt < tries:
                time.sleep(min(15, attempt * 2))

    raise RuntimeError(f"โหลดหน้าเว็บไม่ได้: {last}")


# =========================================================
# TRANSLATION
# =========================================================

def translate_text(
    text,
    src_lang="auto",
    dest_lang="th",
    tries=4,
):
    """
    Google Translate unofficial endpoint
    """

    text = clean_text(text)

    if not text:
        return ""

    result = []

    chunks = split_text(text, 3000)

    for chunk_index, chunk in enumerate(chunks):

        last = None

        for attempt in range(1, tries + 1):

            try:

                r = SESSION.get(
                    "https://translate.googleapis.com/translate_a/single",
                    params={
                        "client": "gtx",
                        "sl": src_lang,
                        "tl": dest_lang,
                        "dt": "t",
                        "q": chunk,
                    },
                    timeout=(10, 30),
                )

                r.raise_for_status()

                data = r.json()

                translated = "".join(
                    item[0]
                    for item in data[0]
                    if item and item[0]
                )

                if not translated.strip():
                    raise RuntimeError("ผลแปลว่าง")

                result.append(translated)

                # ลดโอกาสโดน rate limit
                time.sleep(0.15)

                break

            except Exception as e:

                last = e

                if attempt < tries:
                    time.sleep(min(15, attempt * 2))

        else:
            raise RuntimeError(
                f"แปล chunk {chunk_index + 1}/{len(chunks)} ไม่สำเร็จ: {last}"
            )

    return "\n".join(result)


# =========================================================
# HTML EXTRACTION
# =========================================================

def extract_text(soup):
    """
    ดึงข้อความจากหน้าเว็บแบบ generic
    """

    # ลบส่วนที่ไม่ใช่เนื้อหา
    for tag in soup([
        "script",
        "style",
        "noscript",
        "nav",
        "header",
        "footer",
        "aside",
        "form",
    ]):
        tag.decompose()

    paragraphs = []

    for p in soup.find_all("p"):

        text = p.get_text(" ", strip=True)

        if len(text) >= 2:
            paragraphs.append(text)

    text = "\n".join(paragraphs)

    # fallback
    if len(text.strip()) < 30:
        text = soup.get_text("\n")

    text = clean_text(text)

    return text


# =========================================================
# NEXT URL
# =========================================================

def increment_url(url):
    """
    fallback สำหรับเว็บที่ URL เป็นเลขตอน
    """

    p = urllib.parse.urlparse(url)

    def bump(value):

        m = re.search(r"(\d+)(\D*)$", value)

        if not m:
            return None

        n = int(m.group(1)) + 1

        new_number = str(n).zfill(len(m.group(1)))

        return (
            value[:m.start(1)]
            + new_number
            + m.group(2)
        )

    path = bump(p.path)

    if path is not None:
        return urllib.parse.urlunparse(
            p._replace(path=path)
        )

    return None


def find_next_url(soup, current_url):
    """
    หาลิงก์ตอนถัดไป
    """

    # rel=next
    tag = soup.find(
        ["a", "link"],
        rel=lambda value:
            value and "next" in value,
        href=True,
    )

    if tag:
        return urllib.parse.urljoin(
            current_url,
            tag["href"],
        )

    # text
    for a in soup.find_all("a", href=True):

        href = a["href"].strip()

        txt = a.get_text(
            " ",
            strip=True,
        ).lower()

        if not href:
            continue

        if href.startswith(
            ("#", "javascript:")
        ):
            continue

        if len(txt) > 30:
            continue

        if any(
            marker in txt
            for marker in NEXT_MARKERS
        ):
            return urllib.parse.urljoin(
                current_url,
                href,
            )

    # fallback
    return increment_url(current_url)


# =========================================================
# TTS
# =========================================================

async def tts_to_file(
    text,
    voice,
    out_path,
    parallel=TTS_PARALLEL,
    tries=5,
):
    if edge_tts is None:
        raise RuntimeError(
            "ยังไม่ได้ติดตั้ง edge-tts"
        )

    text = clean_text(text)

    if not text:
        raise ValueError(
            "ไม่มีข้อความให้แปลงเสียง"
        )

    parts = split_text(
        text,
        TTS_PART_CHARS,
    )

    if not parts:
        raise ValueError(
            "ไม่สามารถแบ่งข้อความได้"
        )

    os.makedirs(
        os.path.dirname(out_path),
        exist_ok=True,
    )

    sem = asyncio.Semaphore(parallel)

    tmp_dir = tempfile.mkdtemp(
        prefix="tts_",
        dir=os.path.dirname(out_path),
    )

    tmp_files = [
        os.path.join(
            tmp_dir,
            f"part_{i:05d}.mp3",
        )
        for i in range(len(parts))
    ]

    async def one(index, part):

        async with sem:

            last = None

            for attempt in range(1, tries + 1):

                try:

                    await edge_tts.Communicate(
                        part,
                        voice,
                    ).save(tmp_files[index])

                    if not os.path.exists(
                        tmp_files[index]
                    ):
                        raise RuntimeError(
                            "ไม่พบไฟล์เสียง"
                        )

                    if os.path.getsize(
                        tmp_files[index]
                    ) < 100:
                        raise RuntimeError(
                            "ไฟล์เสียงว่าง"
                        )

                    return

                except Exception as e:

                    last = e

                    if attempt < tries:
                        await asyncio.sleep(
                            min(
                                30,
                                2 ** (attempt - 1),
                            )
                        )

            raise RuntimeError(
                f"TTS part {index + 1}/{len(parts)} "
                f"ล้มเหลว: {last}"
            )

    try:

        results = await asyncio.gather(
            *[
                one(i, part)
                for i, part in enumerate(parts)
            ],
            return_exceptions=True,
        )

        errors = [
            x
            for x in results
            if isinstance(x, Exception)
        ]

        if errors:
            raise errors[0]

        merge_mp3(
            tmp_files,
            out_path,
        )

    finally:

        shutil.rmtree(
            tmp_dir,
            ignore_errors=True,
        )


def merge_mp3(files, out_path):
    """
    ใช้ ffmpeg ถ้ามี
    ถ้าไม่มีให้ fallback เป็น byte concat
    """

    files = [
        f for f in files
        if os.path.exists(f)
    ]

    if not files:
        raise RuntimeError(
            "ไม่มีไฟล์ MP3 ให้รวม"
        )

    ffmpeg = shutil.which("ffmpeg")

    if ffmpeg:

        list_file = out_path + ".txt"

        try:

            with open(
                list_file,
                "w",
                encoding="utf-8",
            ) as f:

                for path in files:

                    safe = path.replace(
                        "'",
                        "'\\''",
                    )

                    f.write(
                        f"file '{safe}'\n"
                    )

            result = subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    list_file,
                    "-c",
                    "copy",
                    out_path,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            if result.returncode == 0:
                return

        finally:

            if os.path.exists(list_file):
                os.remove(list_file)

    # fallback
    with open(out_path, "wb") as out:

        for path in files:

            with open(path, "rb") as src:
                shutil.copyfileobj(
                    src,
                    out,
                )


# =========================================================
# CHECKPOINT
# =========================================================

def work_dir_for(
    start_url,
    voice,
    src_lang,
):
    key = hashlib.md5(
        f"{start_url}|{voice}|{src_lang}".encode()
    ).hexdigest()[:12]

    path = os.path.join(
        OUTPUT_ROOT,
        key,
    )

    os.makedirs(
        path,
        exist_ok=True,
    )

    return path


def default_checkpoint():
    return {
        "version": 2,
        "eps": {},
        "failed": {},
        "batches": {},
    }


def load_cp(work_dir):

    path = os.path.join(
        work_dir,
        "checkpoint.json",
    )

    if not os.path.exists(path):
        return default_checkpoint()

    try:

        with open(
            path,
            "r",
            encoding="utf-8",
        ) as f:

            cp = json.load(f)

        cp.setdefault("version", 2)
        cp.setdefault("eps", {})
        cp.setdefault("failed", {})
        cp.setdefault("batches", {})

        return cp

    except Exception:

        return default_checkpoint()


def save_cp(work_dir, cp):

    path = os.path.join(
        work_dir,
        "checkpoint.json",
    )

    temp = path + ".tmp"

    with open(
        temp,
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            cp,
            f,
            ensure_ascii=False,
            indent=2,
        )

    os.replace(
        temp,
        path,
    )


# =========================================================
# TIME
# =========================================================

def fmt_secs(seconds):

    seconds = int(seconds)

    return (
        f"{seconds // 3600}:"
        f"{(seconds % 3600) // 60:02d}:"
        f"{seconds % 60:02d}"
    )


# =========================================================
# BATCH
# =========================================================

def make_batch(
    work_dir,
    cp,
    b_start,
    b_end,
):
    files = []
    missing = []

    for ep in range(
        b_start,
        b_end + 1,
    ):

        rec = cp["eps"].get(
            str(ep)
        )

        if (
            rec
            and rec.get("status") == "success"
            and os.path.exists(
                rec.get("file", "")
            )
        ):
            files.append(
                rec["file"]
            )
        else:
            missing.append(ep)

    if not files:
        return None, missing

    filename = (
        f"novel_ep_{b_start}"
        f"_to_{b_end}.mp3"
    )

    if missing:
        filename = (
            f"novel_ep_{b_start}"
            f"_to_{b_end}_PARTIAL.mp3"
        )

    out_path = os.path.join(
        work_dir,
        filename,
    )

    merge_mp3(
        files,
        out_path,
    )

    cp["batches"][
        f"{b_start}-{b_end}"
    ] = {
        "file": out_path,
        "start": b_start,
        "end": b_end,
        "missing": missing,
    }

    return out_path, missing


# =========================================================
# MAIN PROCESS
# =========================================================

def run_batches(
    start_url,
    start_ep,
    end_ep,
    batch_size,
    src_lang,
    voice,
    ui,
):
    work_dir = work_dir_for(
        start_url,
        voice,
        src_lang,
    )

    cp = load_cp(work_dir)

    total = end_ep - start_ep + 1

    start_time = time.time()

    current_url = start_url

    batches = []

    success_count = 0
    failed_count = 0

    for b in range(
        (total + batch_size - 1)
        // batch_size
    ):

        b_start = (
            start_ep
            + b * batch_size
        )

        b_end = min(
            b_start
            + batch_size
            - 1,
            end_ep,
        )

        ui["info"](
            f"ชุด {b + 1}  "
            f"ตอน {b_start}-{b_end}"
        )

        for ep in range(
            b_start,
            b_end + 1,
        ):

            done = (
                ep - start_ep
            )

            ui["progress"](
                done / total
            )

            # -----------------------------------------
            # ตรวจไฟล์สำเร็จเดิม
            # -----------------------------------------

            rec = cp["eps"].get(
                str(ep)
            )

            if (
                rec
                and rec.get("status")
                == "success"
                and os.path.exists(
                    rec.get("file", "")
                )
            ):

                success_count += 1

                next_url = rec.get(
                    "next_url"
                )

                if next_url:
                    current_url = next_url

                ui["status"](
                    f"ตอน {ep} ✓ "
                    f"สำเร็จแล้ว | "
                    f"{success_count}/{total}"
                )

                continue

            # -----------------------------------------
            # URL ของตอนนี้
            # -----------------------------------------

            if rec and rec.get("url"):
                ep_url = rec["url"]
            else:
                ep_url = current_url

            ep_file = os.path.join(
                work_dir,
                f"ep_{ep:05d}.mp3",
            )

            try:

                ui["status"](
                    f"กำลังทำตอน {ep} | "
                    f"เวลาที่ใช้ "
                    f"{fmt_secs(time.time() - start_time)}"
                )

                # -------------------------------------
                # FETCH
                # -------------------------------------

                soup = fetch_page(
                    ep_url
                )

                next_url = (
                    find_next_url(
                        soup,
                        ep_url,
                    )
                    or increment_url(
                        ep_url
                    )
                )

                # -------------------------------------
                # EXTRACT
                # -------------------------------------

                original = extract_text(
                    soup
                )

                if len(original.strip()) < 30:
                    raise RuntimeError(
                        "ไม่พบเนื้อหานิยาย"
                    )

                # -------------------------------------
                # TRANSLATE
                # -------------------------------------

                thai = translate_text(
                    original,
                    src_lang=src_lang,
                )

                if len(thai.strip()) < 10:
                    raise RuntimeError(
                        "ข้อความแปลสั้นผิดปกติ"
                    )

                # -------------------------------------
                # TTS
                # -------------------------------------

                asyncio.run(
                    tts_to_file(
                        thai,
                        voice,
                        ep_file,
                    )
                )

                # -------------------------------------
                # SUCCESS
                # -------------------------------------

                cp["eps"][
                    str(ep)
                ] = {
                    "status": "success",
                    "url": ep_url,
                    "next_url": next_url,
                    "file": ep_file,
                    "updated": time.time(),
                }

                cp["failed"].pop(
                    str(ep),
                    None,
                )

                save_cp(
                    work_dir,
                    cp,
                )

                success_count += 1

                ui["preview"](
                    thai[:1500]
                )

                if next_url:
                    current_url = next_url

            except Exception as e:

                failed_count += 1

                error_text = (
                    f"{type(e).__name__}: {e}"
                )

                # สำคัญมาก:
                # จำ URL ของตอนที่เสียเอาไว้
                # เพื่อให้ retry ภายหลังได้ตรงตอน

                next_url = (
                    locals().get(
                        "next_url"
                    )
                    or increment_url(
                        ep_url
                    )
                )

                old = cp["failed"].get(
                    str(ep),
                    {},
                )

                attempts = (
                    old.get("attempts", 0)
                    + 1
                )

                cp["failed"][
                    str(ep)
                ] = {
                    "url": ep_url,
                    "next_url": next_url,
                    "attempts": attempts,
                    "error": error_text,
                    "updated": time.time(),
                }

                cp["eps"][
                    str(ep)
                ] = {
                    "status": "failed",
                    "url": ep_url,
                    "next_url": next_url,
                    "file": ep_file,
                    "error": error_text,
                    "attempts": attempts,
                    "updated": time.time(),
                }

                save_cp(
                    work_dir,
                    cp,
                )

                ui["warn"](
                    f"ตอน {ep} ล้มเหลว "
                    f"(attempt {attempts})\n\n"
                    f"{error_text}"
                )

                # สำคัญ:
                # แม้ตอนนี้เสีย ก็ยังไปตอนถัดไป
                # แต่ URL ของตอนที่เสียถูกเก็บไว้แล้ว

                if next_url:
                    current_url = next_url

            # -----------------------------------------
            # progress
            # -----------------------------------------

            ui["progress"](
                (ep - start_ep + 1)
                / total
            )

        # =================================================
        # MERGE BATCH
        # =================================================

        batch_path, missing = make_batch(
            work_dir,
            cp,
            b_start,
            b_end,
        )

        save_cp(
            work_dir,
            cp,
        )

        if batch_path:

            title = (
                f"ตอนที่ {b_start} ถึง {b_end}"
            )

            batches.append(
                (
                    title,
                    batch_path,
                    missing,
                )
            )

            ui["batch"](
                list(batches)
            )

            if missing:

                ui["warn"](
                    f"ชุด {b_start}-{b_end} "
                    f"มีตอนที่ไม่มีเสียง: "
                    f"{', '.join(map(str, missing))}"
                )

    # =====================================================
    # FINAL
    # =====================================================

    ui["progress"](1.0)

    return (
        batches,
        cp,
        success_count,
        failed_count,
        work_dir,
    )


# =========================================================
# STREAMLIT
# =========================================================

def main():

    st.set_page_config(
        page_title="Novel to Speech",
        page_icon="📚",
        layout="wide",
    )

    st.title(
        "📚 Novel → Thai → Speech"
    )

    st.caption(
        "Batch + Checkpoint + Auto Recovery + ZIP"
    )

    # ---------------------------------------------
    # SESSION STATE
    # ---------------------------------------------

    st.session_state.setdefault(
        "novel_text",
        "",
    )

    st.session_state.setdefault(
        "batches",
        [],
    )

    st.session_state.setdefault(
        "last_work_dir",
        "",
    )

    # ---------------------------------------------
    # SETTINGS
    # ---------------------------------------------

    st.subheader(
        "⚙️ ตั้งค่าการทำงาน"
    )

    start_url = st.text_input(
        "ลิงก์ตอนเริ่มต้น",
        placeholder="https://example.com/chapter-1",
    )

    c1, c2, c3 = st.columns(3)

    start_ep = c1.number_input(
        "ตอนเริ่ม",
        min_value=1,
        value=1,
        step=1,
    )

    end_ep = c2.number_input(
        "ตอนสิ้นสุด",
        min_value=1,
        value=100,
        step=1,
    )

    batch_size = c3.number_input(
        "ตอนต่อชุด",
        min_value=1,
        value=50,
        step=1,
    )

    c4, c5 = st.columns(2)

    src_lang = c4.selectbox(
        "ภาษาต้นทาง",
        [
            "auto",
            "zh-CN",
            "en",
            "ja",
        ],
    )

    voice = c5.selectbox(
        "เสียง TTS",
        [
            "th-TH-PremwadeeNeural",
            "th-TH-NiwatNeural",
        ],
        index=0,
    )

    # ---------------------------------------------
    # TEXT AREA
    # ---------------------------------------------

    st.subheader(
        "✍️ ข้อความตัวอย่าง"
    )

    st.text_area(
        "ข้อความภาษาไทย",
        key="novel_text",
        height=180,
    )

    c1, c2, c3 = st.columns(3)

    if c1.button(
        "✨ จัดระเบียบข้อความ",
        use_container_width=True,
    ):

        st.session_state.novel_text = clean_text(
            st.session_state.novel_text
        )

        st.rerun()

    if c2.button(
        "🗑️ ล้างข้อความ",
        use_container_width=True,
    ):

        st.session_state.novel_text = ""

        st.rerun()

    if c3.button(
        "🔊 แปลงข้อความนี้",
        use_container_width=True,
    ):

        text = (
            st.session_state.novel_text
            .strip()
        )

        if not text:

            st.warning(
                "ยังไม่มีข้อความ"
            )

        else:

            os.makedirs(
                OUTPUT_ROOT,
                exist_ok=True,
            )

            path = os.path.join(
                OUTPUT_ROOT,
                "manual.mp3",
            )

            try:

                with st.spinner(
                    "กำลังแปลงเสียง..."
                ):

                    asyncio.run(
                        tts_to_file(
                            text,
                            voice,
                            path,
                        )
                    )

                with open(
                    path,
                    "rb",
                ) as f:

                    data = f.read()

                st.audio(
                    data,
                    format="audio/mp3",
                )

                st.download_button(
                    "📥 ดาวน์โหลด MP3",
                    data=data,
                    file_name="manual.mp3",
                    mime="audio/mpeg",
                    use_container_width=True,
                )

            except Exception as e:

                st.error(
                    f"TTS Error: {e}"
                )

    # ---------------------------------------------
    # MAIN JOB
    # ---------------------------------------------

    st.divider()

    st.subheader(
        "🎙️ ดึงนิยาย + แปล + แปลงเสียง"
    )

    if st.button(
        "🚀 เริ่ม / ทำต่อจาก Checkpoint",
        type="primary",
        use_container_width=True,
    ):

        if not start_url.strip():

            st.warning(
                "กรอกลิงก์ตอนเริ่มต้นก่อน"
            )

        elif end_ep < start_ep:

            st.warning(
                "ตอนสิ้นสุดต้องมากกว่าหรือเท่ากับตอนเริ่ม"
            )

        else:

            bar = st.progress(0)

            status = st.empty()

            info = st.empty()

            preview = st.empty()

            batches_box = st.empty()

            def show_batches(items):

                batches_box.empty()

                for title, path, missing in items:

                    if missing:

                        st.warning(
                            f"⚠️ {title} "
                            f"ขาดตอน: "
                            f"{', '.join(map(str, missing))}"
                        )

                    else:

                        st.success(
                            f"✅ {title}"
                        )

            ui = {

                "info": info.info,

                "status": status.markdown,

                "progress": lambda value:
                    bar.progress(
                        min(
                            max(
                                float(value),
                                0.0,
                            ),
                            1.0,
                        )
                    ),

                "warn": st.warning,

                "preview": lambda text:
                    st.session_state.__setitem__(
                        "novel_text",
                        text,
                    ),

                "batch": lambda items:
                    show_batches(items),
            }

            try:

                (
                    batches,
                    cp,
                    success_count,
                    failed_count,
                    work_dir,
                ) = run_batches(
                    start_url.strip(),
                    int(start_ep),
                    int(end_ep),
                    int(batch_size),
                    src_lang,
                    voice,
                    ui,
                )

                st.session_state.batches = (
                    batches
                )

                st.session_state.last_work_dir = (
                    work_dir
                )

                if failed_count:

                    st.error(
                        f"เสร็จการประมวลผล "
                        f"แต่มีตอนที่ล้มเหลว "
                        f"{failed_count} ครั้ง"
                    )

                    failed_eps = sorted(
                        [
                            int(x)
                            for x in cp["failed"]
                        ]
                    )

                    if failed_eps:

                        st.warning(
                            "ตอนที่ต้อง retry: "
                            + ", ".join(
                                map(
                                    str,
                                    failed_eps,
                                )
                            )
                        )

                else:

                    st.success(
                        "🎉 ทุกตอนสำเร็จ!"
                    )

            except Exception as e:

                st.error(
                    f"ระบบหยุดแบบไม่คาดคิด: {e}"
                )

                st.info(
                    "Checkpoint ถูกบันทึกไว้ "
                    "ให้กด เริ่ม / ทำต่อ "
                    "อีกครั้งได้"
                )

    # ---------------------------------------------
    # DOWNLOAD
    # ---------------------------------------------

    if st.session_state.batches:

        st.divider()

        st.subheader(
            "📥 ดาวน์โหลด"
        )

        # ZIP
        zip_buffer = io.BytesIO()

        with zipfile.ZipFile(
            zip_buffer,
            "w",
            zipfile.ZIP_DEFLATED,
        ) as z:

            for (
                title,
                path,
                missing,
            ) in st.session_state.batches:

                if os.path.exists(path):

                    z.write(
                        path,
                        os.path.basename(
                            path
                        ),
                    )

        zip_buffer.seek(0)

        st.download_button(
            "📦 ดาวน์โหลดทุกชุดเป็น ZIP",
            data=zip_buffer.getvalue(),
            file_name="all_novel_batches.zip",
            mime="application/zip",
            type="primary",
            use_container_width=True,
        )

        st.markdown(
            "### ไฟล์แต่ละชุด"
        )

        for (
            title,
            path,
            missing,
        ) in st.session_state.batches:

            if not os.path.exists(path):
                continue

            with open(
                path,
                "rb",
            ) as f:

                data = f.read()

            label = (
                f"📥 {title}"
            )

            if missing:
                label += (
                    " ⚠️ PARTIAL"
                )

            st.download_button(
                label,
                data=data,
                file_name=os.path.basename(
                    path
                ),
                mime="audio/mpeg",
                key=f"download_{path}",
                use_container_width=True,
            )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    if st is not None:
        main()
