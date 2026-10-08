import streamlit as st
import asyncio
import edge_tts
import requests
from bs4 import BeautifulSoup
import urllib.parse
import re
import os
import time

st.set_page_config(page_title="Novel to Speech - แยกตอนรวมไฟล์เดียว", page_icon="🌐")

st.title("🌐 ระบบดึงนิยาย + แปลไทย + สร้างเสียง (แปลงทีละตอนแล้วรวมเป็นไฟล์เดียว)")

if "novel_text" not in st.session_state:
    st.session_state.novel_text = "วางลิงก์ตอนเริ่มต้นด้านบน แล้วระบุช่วงตอนที่ต้องการดึง หรือพิมพ์ข้อความภาษาไทยที่นี่ได้เลยครับ"

def fast_translate(text, src_lang='auto', dest_lang='th'):
    if not text.strip():
        return text
    try:
        max_chunk = 3000
        chunks = [text[i:i+max_chunk] for i in range(0, len(text), max_chunk)]
        translated_full = []
        
        for chunk in chunks:
            url = "https://translate.googleapis.com/translate_a/single"
            params = {
                "client": "gtx",
                "sl": src_lang,
                "tl": dest_lang,
                "dt": "t",
                "q": chunk
            }
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

# ส่วนดึงนิยาย
st.subheader("🔗 ดึงนิยายต่อเนื่องแบบระบุช่วงตอน")
start_url = st.text_input("วางลิงก์หน้าเว็บนิยาย 'ตอนเริ่มต้น':", "")

col_a, col_b, col_c = st.columns(3)
with col_a:
    start_ep = st.number_input("เริ่มต้นที่ตอนที่:", min_value=1, value=1)
with col_b:
    end_ep = st.number_input("สิ้นสุดที่ตอนที่:", min_value=1, value=5)
with col_c:
    src_lang = st.selectbox("ภาษาต้นทาง:", ["auto", "zh-CN", "en", "ja"], index=0)

if st.button("🚀 สั่งดึงและแปลตามช่วงตอน"):
    if start_url.strip() == "":
        st.warning("⚠️ กรุณากรอกลิงก์เริ่มต้นก่อนครับ")
    elif end_ep < start_ep:
        st.warning("⚠️ ตอนสิ้นสุดต้องมากกว่าหรือเท่ากับตอนเริ่มต้นครับ")
    else:
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        combined_chapters = []
        current_url = start_url
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        total_steps = (end_ep - start_ep) + 1
        success_count = 0
        
        for i in range(total_steps):
            actual_ep_num = start_ep + i
            percent_complete = int((i / total_steps) * 100)
            progress_bar.progress(percent_complete + 1)
            status_text.text(f"⏳ กำลังดึงและแปลตอนที่ {actual_ep_num}...")
            
            try:
                res = requests.get(current_url, headers=headers, timeout=10)
                if res.status_code != 200:
                    break
                    
                soup = BeautifulSoup(res.text, 'html.parser')
                paragraphs = soup.find_all('p')
                raw_chapter_content = "\n".join([p.get_text() for p in paragraphs])
                if len(raw_chapter_content.strip()) < 10:
                    raw_chapter_content = soup.get_text()

                translated_content = fast_translate(raw_chapter_content, src_lang=src_lang, dest_lang='th')
                combined_chapters.append(f"\n\n=== ตอนที่ {actual_ep_num} ===\n\n" + translated_content)
                success_count += 1
                
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
                    else:
                        break
            except Exception:
                break

        progress_bar.progress(100)
        if combined_chapters:
            st.session_state.novel_text = "".join(combined_chapters)
            status_text.success(f"🎉 สำเร็จ! ดึงและแปลช่วงตอนที่ {start_ep} ถึง {start_ep + success_count - 1} เรียบร้อยแล้ว")

# ช่องข้อความ
st.subheader("✍️ ตรวจสอบ แก้ไข และขัดเกลาข้อความภาษาไทย")
st.session_state.novel_text = st.text_area("ข้อความภาษาไทยสำหรับสร้างเสียงเปรมวดี:", value=st.session_state.novel_text, height=250)

char_count = len(st.session_state.novel_text)
st.caption(f"📊 สถิติข้อความปัจจุบัน: **{char_count:,}** ตัวอักษร")

if st.button("✨ ขัดเกลาข้อความให้อ่านง่ายขึ้น"):
    cleaned = re.sub(r'\n\s*\n', '\n\n', st.session_state.novel_text)
    st.session_state.novel_text = re.sub(r'[ \t]+', ' ', cleaned)
    st.success("✨ ขัดเกลาข้อความเรียบร้อยแล้ว!")
    st.rerun()

# ฟังก์ชันแปลงเสียงทีละตอน แล้วนำมารวมเป็นไฟล์เดียว
async def generate_audio_by_chapter(text_content, voice, output_filename, progress_bar, status_text):
    # แยกข้อความตามรูปแบบตอน เช่น "=== ตอนที่ X ==="
    chapters = re.split(r'(===\s*ตอนที่\s*\d+\s*===)', text_content)
    
    chapter_blocks = []
    current_title = "ตอนที่ 1"
    
    for part in chapters:
        if "=== ตอนที่" in part:
            current_title = part.strip()
        elif part.strip():
            chapter_blocks.append((current_title, part.strip()))
            
    # ถ้าไม่มีหัวข้อตอน ให้แบ่งด้วยขนาดตัวอักษรปกติ (ประมาณ 2,000 ตัวอักษรต่อส่วน) แทน
    if not chapter_blocks:
        chunk_size = 2000
        chunks = [text_content[i:i+chunk_size] for i in range(0, len(text_content), chunk_size)]
        chapter_blocks = [(f"ส่วนที่ {idx+1}", c.strip()) for idx, c in enumerate(chunks) if c.strip()]

    total_chapters = len(chapter_blocks)
    if total_chapters == 0:
        return False

    temp_chapter_files = []
    
    for idx, (ch_title, ch_text) in enumerate(chapter_blocks):
        current_pct = int(((idx) / total_chapters) * 100)
        progress_bar.progress(current_pct)
        status_text.text(f"🎙️ กำลังแปลงเสียง: {ch_title} ({idx + 1}/{total_chapters})")
        
        # ซอยย่อยภายในตอนนั้นๆ เป็นก้อนละ 2,000 ตัวอักษร เพื่อแปลงให้รวดเร็วและปลอดภัย
        sub_chunk_size = 2000
        sub_chunks = [ch_text[i:i+sub_chunk_size] for i in range(0, len(ch_text), sub_chunk_size)]
        
        sub_files = []
        for s_idx, s_text in enumerate(sub_chunks):
            sub_file = f"temp_ch_{idx}_sub_{s_idx}.mp3"
            sub_files.append(sub_file)
            
            # ระบบลองใหม่ (Retry) อัตโนมัติถ้าเกิดข้อผิดพลาด
            success_sub = False
            for attempt in range(2):
                try:
                    clean_s_text = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', s_text)
                    communicate = edge_tts.Communicate(clean_s_text, voice)
                    await communicate.save(sub_file)
                    success_sub = True
                    break
                except Exception:
                    await asyncio.sleep(0.3)
            
            if not success_sub:
                with open(sub_file, "wb") as f:
                    f.write(b"")

        # รวมย่อยแต่ละส่วนของตอนนี้ให้เป็นไฟล์ของตอนนี้
        chapter_file = f"temp_chapter_{idx}.mp3"
        temp_chapter_files.append(chapter_file)
        
        with open(chapter_file, "wb") as ch_out:
            for sf in sub_files:
                if os.path.exists(sf) and os.path.getsize(sf) > 0:
                    with open(sf, "rb") as sf_in:
                        ch_out.write(sf_in.read())
                    os.remove(sf)

    # รวมไฟล์ทุกตอนเข้าเป็นไฟล์เสียงหลักไฟล์เดียว
    status_text.text("🔗 กำลังรวมไฟล์เสียงทุกตอนเข้าเป็นไฟล์หลัก...")
    progress_bar.progress(95)
    
    with open(output_filename, "wb") as final_out:
        for cf in temp_chapter_files:
            if os.path.exists(cf) and os.path.getsize(cf) > 0:
                with open(cf, "rb") as cf_in:
                    final_out.write(cf_in.read())
                os.remove(cf)

    progress_bar.progress(100)
    status_text.success("🎉 สร้างและรวมไฟล์เสียงทุกตอนสำเร็จเรียบร้อยแล้ว!")
    return True

st.subheader("🎙️ สร้างเสียงเปรมวดี (แปลงทีละตอนรวมเป็นไฟล์เดียว)")
if st.button("🎙️ เริ่มแปลงทีละตอนและรวมไฟล์ (MP3)"):
    if st.session_state.novel_text.strip() == "":
        st.warning("⚠️ กรุณามีข้อความสำหรับสร้างเสียงก่อนครับ")
    else:
        audio_progress = st.progress(0)
        audio_status = st.empty()

        try:
            output_file = "premwadee_all_chapters_combined.mp3"
            success = asyncio.run(generate_audio_by_chapter(
                st.session_state.novel_text, 
                "th-TH-PremwadeeNeural", 
                output_file, 
                audio_progress, 
                audio_status
            ))
            
            if success and os.path.exists(output_file):
                with open(output_file, "rb") as f:
                    audio_bytes = f.read()
                
                st.audio(audio_bytes, format="audio/mp3")
                st.download_button(
                    label="📥 ดาวน์โหลดไฟล์ MP3 รวมทุกตอน",
                    data=audio_bytes,
                    file_name="novel_combined_audio.mp3",
                    mime="audio/mp3"
                )
        except Exception as e:
            audio_status.error(f"❌ เกิดข้อผิดพลาด: {str(e)}")
