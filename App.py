import streamlit as st
import asyncio
import edge_tts
import requests
from bs4 import BeautifulSoup
import urllib.parse
import re
import os
import time

st.set_page_config(page_title="Novel to Speech - Auto Rename Batch", page_icon="📦")

st.title("📦 ระบบดึงนิยายแบ่งเป็นชุด + ตั้งชื่อไฟล์ + ดาวน์โหลดอัตโนมัติ")

if "novel_text" not in st.session_state:
    st.session_state.novel_text = "วางลิงก์ตอนเริ่มต้นด้านบน แล้วกำหนดช่วงตอนและขนาดชุดที่ต้องการได้เลยครับ"

def fast_translate(text, src_lang='auto', dest_lang='th'):
    if not text.strip():
        return text
    try:
        max_chunk = 3000
        chunks = [text[i:i+max_chunk] for i in range(0, len(text), max_chunk)]
        translated_full = []
        for chunk in chunks:
            url = "https://translate.googleapis.com/translate_a/single"
            params = {"client": "gtx", "sl": src_lang, "tl": dest_lang, "dt": "t", "q": chunk}
            headers = {"User-Agent": "Mozilla/5.0"}
            response = requests.get(url, params=params, headers=headers, timeout=10)
            if response.status_code == 200:
                res_json = response.json()
                translated_chunk = "".join([item[0] for item in res_json[0] if item[0]])
                translated_full.append(translated_chunk)
            else:
                translated_full.append(chunk)
        return "".join(translated_full)
    except Exception:
        return text

# ฟังก์ชันแปลงเสียงย่อยแบบขนาน (Parallel TTS) พร้อมระบบป้องกัน Error
async def text_to_speech_fast(text, voice, output_file):
    clean_text = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', text)
    sub_chunk_size = 2000
    sub_chunks = [clean_text[i:i+sub_chunk_size] for i in range(0, len(clean_text), sub_chunk_size)]
    sub_chunks = [c.strip() for c in sub_chunks if c.strip()]
    
    if not sub_chunks:
        with open(output_file, "wb") as f:
            f.write(b"")
        return

    sub_files = [f"sub_{os.getpid()}_{idx}.mp3" for idx in range(len(sub_chunks))]
    
    async def fetch_sub(s_idx, s_text):
        for _ in range(3):
            try:
                communicate = edge_tts.Communicate(s_text, voice)
                await communicate.save(sub_files[s_idx])
                return
            except Exception:
                await asyncio.sleep(0.5)
        with open(sub_files[s_idx], "wb") as f:
            f.write(b"")

    tasks = [fetch_sub(idx, chunk) for idx, chunk in enumerate(sub_chunks)]
    await asyncio.gather(*tasks)

    with open(output_file, "wb") as outfile:
        for sf in sub_files:
            if os.path.exists(sf) and os.path.getsize(sf) > 0:
                with open(sf, "rb") as sf_in:
                    outfile.write(sf_in.read())
                os.remove(sf)

# ตั้งค่าหน้าจอการใช้งาน
st.subheader("🔗 ตั้งค่าช่วงตอนและขนาดชุด (Batch Settings)")
start_url = st.text_input("วางลิงก์หน้าเว็บนิยาย 'ตอนเริ่มต้น':", "")

col_1, col_2, col_3 = st.columns(3)
with col_1:
    start_ep = st.number_input("เริ่มต้นที่ตอนที่:", min_value=1, value=1)
with col_2:
    end_ep = st.number_input("สิ้นสุดที่ตอนที่ (เช่น 500):", min_value=1, value=100)
with col_3:
    batch_size = st.number_input("จำนวนตอนต่อ 1 ชุด (Batch):", min_value=1, value=50)

col_4, col_5 = st.columns(2)
with col_4:
    src_lang = st.selectbox("ภาษาต้นทาง:", ["auto", "zh-CN", "en", "ja"], index=0)
with col_5:
    voice_choice = st.selectbox("เลือกเสียงพากย์:", ["th-TH-PremwadeeNeural", "th-TH-NiwatNeural"], index=0)

# ช่องแสดงข้อความ
st.subheader("✍️ ข้อความนิยายภาพรวม")
st.session_state.novel_text = st.text_area("ข้อความภาษาไทยสำหรับสร้างเสียง:", value=st.session_state.novel_text, height=180)

char_count = len(st.session_state.novel_text)
st.caption(f"📊 สถิติข้อความปัจจุบัน: **{char_count:,}** ตัวอักษร")

col_btn1, col_btn2 = st.columns(2)
with col_btn1:
    if st.button("✨ ขัดเกลาข้อความให้อ่านง่ายขึ้น"):
        cleaned = re.sub(r'\n\s*\n', '\n\n', st.session_state.novel_text)
        st.session_state.novel_text = re.sub(r'[ \t]+', ' ', cleaned)
        st.success("✨ ขัดเกลาข้อความเรียบร้อยแล้ว!")
        st.rerun()
with col_btn2:
    if st.button("🗑️ ล้างข้อความทั้งหมด"):
        st.session_state.novel_text = ""
        st.rerun()

# ปุ่มเริ่มกระบวนการแบ่งชุด พร้อมระบบกู้คืนตัวเองและแสดงปุ่มดาวน์โหลด
st.subheader("🎙️ ระบบแปลงและดาวน์โหลดทีละชุด (Auto-Recovery & Download)")
if st.button("🚀 เริ่มต้นแปลงนิยายแบบแบ่งชุดอัตโนมัติ (ยาวข้ามคืนได้)"):
    if start_url.strip() == "":
        st.warning("⚠️ กรุณากรอกลิงก์เริ่มต้นก่อนครับ")
    elif end_ep < start_ep:
        st.warning("⚠️ ตอนสิ้นสุดต้องมากกว่าหรือเท่ากับตอนเริ่มต้นครับ")
    else:
        total_episodes = (end_ep - start_ep) + 1
        num_batches = (total_episodes + batch_size - 1) // batch_size
        
        st.info(f"ℹ️ ระบบจะทำการแบ่งตอนทั้งหมด {total_episodes} ตอน ออกเป็น **{num_batches} ชุด** (ชุดละประมาณ {batch_size} ตอน)")
        
        overall_progress = st.progress(0)
        status_display = st.empty()
        
        current_url = start_url
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        
        global_start_time = time.time()
        completed_episodes_count = 0
        
        downloadable_batches = []
        
        for batch_idx in range(num_batches):
            batch_start_ep = start_ep + (batch_idx * batch_size)
            batch_end_ep = min(start_ep + ((batch_idx + 1) * batch_size) - 1, end_ep)
            
            status_display.markdown(f"📦 **กำลังประมวลผลชุดที่ {batch_idx + 1} / {num_batches}** (ตอนที่ {batch_start_ep} ถึง {batch_end_ep})")
            
            batch_chapter_texts = []
            batch_audio_files = []
            
            current_batch_total = (batch_end_ep - batch_start_ep) + 1
            for b_i in range(current_batch_total):
                actual_ep_num = batch_start_ep + b_i
                completed_episodes_count += 1
                
                pct = int((completed_episodes_count / total_episodes) * 100)
                overall_progress.progress(max(min(pct, 100), 1))
                
                # ระบบป้องกัน Error ข้ามตอนอัตโนมัติหากเว็บมีปัญหา
                for attempt in range(2):
                    try:
                        res = requests.get(current_url, headers=headers, timeout=10)
                        if res.status_code == 200:
                            soup = BeautifulSoup(res.text, 'html.parser')
                            paragraphs = soup.find_all('p')
                            raw_content = "\n".join([p.get_text() for p in paragraphs])
                            if len(raw_content.strip()) < 10:
                                raw_content = soup.get_text()

                            translated_content = fast_translate(raw_content, src_lang=src_lang, dest_lang='th')
                            
                            formatted_ch = f"\n\n=== ตอนที่ {actual_ep_num} ===\n\n" + translated_content
                            batch_chapter_texts.append(formatted_ch)
                            
                            ch_audio_file = f"batch_{batch_idx}_ch_{actual_ep_num}.mp3"
                            batch_audio_files.append(ch_audio_file)
                            
                            asyncio.run(text_to_speech_fast(translated_content, voice_choice, ch_audio_file))
                            
                            # ค้นหาลิงก์ตอนถัดไป
                            next_link = None
                            for a in soup.find_all('a', href=True):
                                text_a = a.get_text().lower()
                                if 'next' in text_a or 'ถัดไป' in text_a or '>>' in text_a or '下一页' in text_a:
                                    next_link = a['href']
                                    break
                            
                            if next_link:
                                current_url = next_link if next_link.startswith('http') else urllib.parse.urljoin(current_url, next_link)
                            else:
                                if re.search(r'-\d+$', current_url):
                                    current_url = re.sub(r'-\d+$', lambda m: f"-{int(m.group(1)[1:])+1}", current_url)
                            break
                    except Exception:
                        time.sleep(1)

            # รวมไฟล์เสียงประจำชุด และตั้งชื่อไฟล์ตามช่วงตอนจริงอัตโนมัติ
            if batch_audio_files:
                batch_filename = f"novel_ep_{batch_start_ep}_to_{batch_end_ep}.mp3"
                with open(batch_filename, "wb") as batch_out:
                    for caf in batch_audio_files:
                        if os.path.exists(caf) and os.path.getsize(caf) > 0:
                            with open(caf, "rb") as caf_in:
                                batch_out.write(caf_in.read())
                            os.remove(caf)
                
                if os.path.exists(batch_filename):
                    with open(batch_filename, "rb") as bf:
                        b_bytes = bf.read()
                    downloadable_batches.append((f"ตอนที่ {batch_start_ep} ถึง {batch_end_ep}", b_bytes, batch_filename))

            if batch_chapter_texts:
                st.session_state.novel_text += "".join(batch_chapter_texts)

        overall_progress.progress(100)
        total_elapsed = time.time() - global_start_time
        status_display.success(f"🎉 ประมวลผลเสร็จสิ้นทุกชุด! ใช้เวลาไปทั้งหมด {int(total_elapsed)} วินาที")

        # 📥 บันทึกรายการไฟล์ลงใน Session State เพื่อให้ปุ่มดาวน์โหลดแสดงผลคงอยู่ตลอดไป
        st.session_state.downloadable_batches = downloadable_batches

    # 📥 แสดงส่วนปุ่มดาวน์โหลด (ดึงมาจาก Session State ป้องกันปุ่มหาย)
if "downloadable_batches" in st.session_state and st.session_state.downloadable_batches:
    st.markdown("---")
    st.subheader("📥 ดาวน์โหลดไฟล์เสียงแยกตามชุด (พร้อมชื่อตอนอัตโนมัติ)")
    for title, data_bytes, filename in st.session_state.downloadable_batches:
        st.markdown(f"🎵 **ไฟล์: `{filename}`**")
        st.audio(data_bytes, format="audio/mp3")
        st.download_button(
            label=f"📥 ดาวน์โหลด {title} ({filename})",
            data=data_bytes,
            file_name=filename,
            mime="audio/mp3",
            key=filename
        )
