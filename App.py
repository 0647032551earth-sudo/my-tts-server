"""Novel to Speech - Batch (เวอร์ชันปรับปรุงสมบูรณ์ พร้อมดาวน์โหลด ZIP)

ติดตั้ง:  pip install streamlit edge-tts requests beautifulsoup4
รัน:      streamlit run app.py
"""
import asyncio
import hashlib
import json
import os
import re
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

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
OUTPUT_ROOT = "output"
TTS_PART_CHARS = 1500
TTS_PARALLEL = 2
NEXT_MARKERS = ("next", "ถัดไป", ">>", "下一页", "下一章", "下一頁", "次へ")


def clean_text(text):
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", "", text or "")


def split_text(text, limit):
    parts, buf = [], ""

    def flush():
        nonlocal buf
        if buf.strip():
            parts.append(buf.strip())
        buf = ""

    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        while len(line) > limit:
            cut = max(
                line.rfind(ch, 0, limit)
                for ch in ("。", "！", "？", ". ", "! ", "? ", "；", "，", ", ", " ")
            )
            if cut < limit // 3:
                cut = limit - 1
            flush()
            parts.append(line[: cut + 1].strip())
            line = line[cut + 1:].strip()
        if len(buf) + len(line) + 1 > limit:
            flush()
        buf += line + "\n"
    flush()
    return [p for p in parts if p]


def translate_text(text, src_lang="auto", dest_lang="th", tries=3):
    if not text.strip():
        return text
    out = []
    for chunk in split_text(text, 3000):
        last = None
        for a in range(tries):
            try:
                r = requests.get(
                    "https://translate.googleapis.com/translate_a/single",
                    params={"client": "gtx", "sl": src_lang, "tl": dest_lang, "dt": "t", "q": chunk},
                    headers=HEADERS,
                    timeout=15,
                )
                r.raise_for_status()
                out.append("".join(item[0] for item in r.json()[0] if item[0]))
                break
            except Exception as e:
                last = e
                time.sleep(1.5 * (a + 1))
        else:
            raise RuntimeError(f"แปลไม่สำเร็จ: {last}")
    return "\n".join(out)


def fetch_page(url, tries=3):
    last = None
    for a in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            r.raise_for_status()
            if (r.encoding or "").lower() == "iso-8859-1":
                r.encoding = r.apparent_encoding
            return BeautifulSoup(r.text, "html.parser")
        except Exception as e:
            last = e
            time.sleep(1.5 * (a + 1))
    raise RuntimeError(f"โหลดหน้าเว็บไม่ได้: {last}")


def extract_text(soup):
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer"]):
        tag.decompose()
    text = "\n".join(p.get_text() for p in soup.find_all("p"))
    if len(text.strip()) < 10:
        text = soup.get_text("\n")
    return text


def increment_url(url):
    p = urllib.parse.urlparse(url)

    def bump(s):
        m = re.search(r"(\d+)(\D*)$", s)
        if not m:
            return None
        n = str(int(m.group(1)) + 1).zfill(len(m.group(1)))
        return s[: m.start(1)] + n + m.group(2)

    path = bump(p.path)
    if path is not None:
        return urllib.parse.urlunparse(p._replace(path=path))
    q = bump(p.query)
    if q is not None:
        return urllib.parse.urlunparse(p._replace(query=q))
    return None


def find_next_url(soup, current_url):
    tag = soup.find(["a", "link"], rel=lambda v: v and "next" in v, href=True)
    if tag:
        return urllib.parse.urljoin(current_url, tag["href"])
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        txt = a.get_text().strip().lower()
        if not href or href.startswith(("#", "javascript:")) or len(txt) > 20:
            continue
        if any(m in txt for m in NEXT_MARKERS):
            return urllib.parse.urljoin(current_url, href)
    return None


async def tts_to_file(text, voice, out_path, parallel=TTS_PARALLEL, tries=5, backoff=1.0):
    parts = split_text(clean_text(text), TTS_PART_CHARS)
    if not parts:
        raise ValueError("ไม่มีข้อความให้แปลงเสียง")
    sem = asyncio.Semaphore(parallel)
    tmp = [f"{out_path}.part{i}" for i in range(len(parts))]

    async def one(i, p):
        async with sem:
            last = None
            for a in range(tries):
                try:
                    await edge_tts.Communicate(p, voice).save(tmp[i])
                    if os.path.getsize(tmp[i]) < 100:
                        raise RuntimeError("ได้ไฟล์เสียงว่าง")
                    return
                except Exception as e:
                    last = e
                    await asyncio.sleep(min(30, backoff * (2 ** a)))
            raise RuntimeError(f"ช่วงเสียง {i + 1}/{len(parts)} ล้มเหลว: {last}")

    try:
        results = await asyncio.gather(*[one(i, p) for i, p in enumerate(parts)], return_exceptions=True)
        errs = [r for r in results if isinstance(r, Exception)]
        if errs:
            raise errs[0]
        with open(out_path, "wb") as out:
            for t in tmp:
                with open(t, "rb") as f:
                    out.write(f.read())
    finally:
        for t in tmp:
            if os.path.exists(t):
                os.remove(t)


def work_dir_for(start_url, voice):
    key = hashlib.md5(f"{start_url}|{voice}".encode()).hexdigest()[:8]
    d = os.path.join(OUTPUT_ROOT, key)
    os.makedirs(d, exist_ok=True)
    return d


def load_cp(d):
    p = os.path.join(d, "checkpoint.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"eps": {}, "failed": {}}


def save_cp(d, cp):
    with open(os.path.join(d, "checkpoint.json"), "w", encoding="utf-8") as f:
        json.dump(cp, f, ensure_ascii=False)


def merge_files(files, out_path):
    with open(out_path, "wb") as out:
        for f in files:
            with open(f, "rb") as src:
                out.write(src.read())


def fmt_secs(s):
    s = int(s)
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def run_batches(start_url, start_ep, end_ep, batch_size, src_lang, voice, ui):
    d = work_dir_for(start_url, voice)
    cp = load_cp(d)
    total = end_ep - start_ep + 1
    num_batches = (total + batch_size - 1) // batch_size
    ui["info"](f"ทั้งหมด {total} ตอน แบ่งเป็น {num_batches} ชุด (ชุดละประมาณ {batch_size} ตอน)")
    t0, done = time.time(), 0
    current_url = start_url
    batches = []

    for b in range(num_batches):
        b_start = start_ep + b * batch_size
        b_end = min(b_start + batch_size - 1, end_ep)
        ok_files = []
        for ep in range(b_start, b_end + 1):
            ui["status"](f"ชุดที่ {b + 1}/{num_batches}  ตอนที่ {ep}  (เสร็จแล้ว {done}/{total}, ใช้เวลา {fmt_secs(time.time() - t0)})")
            rec = cp["eps"].get(str(ep))
            if rec and os.path.exists(rec["file"]):
                ok_files.append(rec["file"])
                current_url = rec.get("next_url") or current_url
                done += 1
                ui["progress"](done / total)
                continue

            ep_file = os.path.join(d, f"ep_{ep:04d}.mp3")
            nxt = None
            try:
                soup = fetch_page(current_url)
                nxt = find_next_url(soup, current_url) or increment_url(current_url)
                text = extract_text(soup)
                thai = translate_text(text, src_lang=src_lang)
                asyncio.run(tts_to_file(thai, voice, ep_file))
                cp["eps"][str(ep)] = {"file": ep_file, "url": current_url, "next_url": nxt}
                cp["failed"].pop(str(ep), None)
                ok_files.append(ep_file)
                ui["preview"](thai[:1500])
            except Exception as e:
                cp["failed"][str(ep)] = f"{type(e).__name__}: {e}"
                ui["warn"](f"ตอนที่ {ep} ล้มเหลว: {e}")
                if nxt is None:
                    nxt = increment_url(current_url)
            save_cp(d, cp)
            done += 1
            ui["progress"](done / total)
            if not nxt or nxt == current_url:
                ui["warn"]("หาลิงก์ตอนถัดไปไม่ได้ หยุดการทำงาน (กดเริ่มใหม่เพื่อทำต่อจากที่ค้าง)")
                return batches, cp
            current_url = nxt

        if ok_files:
            name = os.path.join(d, f"novel_ep_{b_start}_to_{b_end}.mp3")
            merge_files(ok_files, name)
            batches.append((f"ตอนที่ {b_start} ถึง {b_end}", name))
            ui["batch"](batches)
    return batches, cp


def main():
    st.set_page_config(page_title="Novel to Speech", page_icon="📦")
    st.title("📦 ดึงนิยาย แปลไทย แปลงเสียง แบ่งชุด (อัตโนมัติข้ามคืน)")
    st.caption("ระบบทำงานต่อเนื่อง มี Checkpoint ป้องกันหลุด และรวมไฟล์ดาวน์โหลดเป็น ZIP ได้")

    st.session_state.setdefault("novel_text", "")
    st.session_state.setdefault("batches", [])

    start_url = st.text_input("ลิงก์ตอนเริ่มต้น:", "")
    c1, c2, c3 = st.columns(3)
    start_ep = c1.number_input("เริ่มที่ตอน:", min_value=1, value=1)
    end_ep = c2.number_input("สิ้นสุดที่ตอน:", min_value=1, value=100)
    batch_size = c3.number_input("ตอนต่อชุด:", min_value=1, value=50)
    c4, c5 = st.columns(2)
    src_lang = c4.selectbox("ภาษาต้นทาง:", ["auto", "zh-CN", "en", "ja"])
    voice = c5.selectbox("เสียง:", ["th-TH-PremwadeeNeural", "th-TH-NiwatNeural"])

    st.subheader("✍️ ข้อความตัวอย่าง")
    st.session_state.novel_text = st.text_area("ข้อความภาษาไทย:", value=st.session_state.novel_text, height=160)

    b1, b2, b3 = st.columns(3)
    if b1.button("✨ จัดระเบียบข้อความ"):
        t = re.sub(r"\n\s*\n", "\n\n", st.session_state.novel_text)
        st.session_state.novel_text = re.sub(r"[ \t]+", " ", t)
        st.rerun()
    if b2.button("🗑️ ล้างข้อความ"):
        st.session_state.novel_text = ""
        st.rerun()
    if b3.button("🔊 แปลงข้อความนี้เดี่ยวๆ"):
        txt = st.session_state.novel_text.strip()
        if not txt:
            st.warning("ยังไม่มีข้อความ")
        else:
            os.makedirs(OUTPUT_ROOT, exist_ok=True)
            path = os.path.join(OUTPUT_ROOT, "manual.mp3")
            try:
                with st.spinner("กำลังแปลงเสียง..."):
                    asyncio.run(tts_to_file(txt, voice, path))
                with open(path, "rb") as f:
                    data = f.read()
                st.audio(data, format="audio/mp3")
                st.download_button("📥 ดาวน์โหลด MP3", data, "manual.mp3", "audio/mp3")
            except Exception as e:
                st.error(str(e))

    st.subheader("🎙️ แปลงนิยายแบบแบ่งชุดข้ามคืน")
    if st.button("🚀 เริ่ม / ทำต่อ"):
        if not start_url.strip():
            st.warning("กรอกลิงก์เริ่มต้นก่อน")
        elif end_ep < start_ep:
            st.warning("ตอนสิ้นสุดต้องไม่น้อยกว่าตอนเริ่มต้น")
        else:
            bar, status = st.progress(0), st.empty()
            ui = {
                "info": st.info,
                "status": status.markdown,
                "progress": lambda x: bar.progress(min(max(x, 0.0), 1.0)),
                "warn": st.warning,
                "preview": lambda t: st.session_state.__setitem__("novel_text", t),
                "batch": lambda b: st.session_state.__setitem__("batches", list(b)),
            }
            batches, cp = run_batches(start_url.strip(), int(start_ep), int(end_ep), int(batch_size), src_lang, voice, ui)
            st.session_state.batches = batches
            if cp["failed"]:
                st.error("ตอนที่ล้มเหลว: " + ", ".join(sorted(cp["failed"], key=int)))
            else:
                status.success("เสร็จทุกชุดเรียบร้อย!")

    if st.session_state.batches:
        st.markdown("---")
        st.subheader("📥 ดาวน์โหลดไฟล์แยกตามชุด หรือดาวน์โหลดรวมทั้งหมดเป็น ZIP")
        
        # เพิ่มปุ่มดาวน์โหลดรวบยอดเป็น ZIP
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for title, path in st.session_state.batches:
                if os.path.exists(path):
                    zip_file.write(path, os.path.basename(path))
        zip_buffer.seek(0)
        
        st.download_button(
            label="📦 ดาวน์โหลดทุกชุดรวมกันเป็นไฟล์ ZIP เดียว",
            data=zip_buffer,
            file_name="all_novel_batches.zip",
            mime="application/zip",
            type="primary"
        )
        
        st.markdown("")
        for title, path in st.session_state.batches:
            if os.path.exists(path):
                with open(path, "rb") as f:
                    st.download_button(f"📥 {title} ({os.path.basename(path)})", f, os.path.basename(path), "audio/mp3", key=path)


if __name__ == "__main__" and st is not None:
    main()
