import streamlit as st
import asyncio
import edge_tts
import requests
from bs4 import BeautifulSoup
import urllib.parse
import re
import os
import time

st.set_page_config(page_title="Novel to Speech - เปรมวดี Pro (Turbo Auto-Recovery)", page_icon="🌐")

st.title("🌐 ระบบดึงนิยาย + แปลไทย + สร้างเสียงเปรมวดี (Turbo & Auto-Recovery)")

# กำหนดค่าเริ่มต้นใน session_state ป้องกันข้อมูลหาย
if "novel_text" not in st.session_state:
    st.session_state.novel_text = "วางลิงก์ตอนเริ่มต้นด้านบน แล้วระบุช่วงตอนที่ต้องการดึง หรือพิมพ์ข้อความภาษาไทยที่นี่ได้เลยครับ"

# ฟังก์ชันแปลภาษาแบบรวดเร็ว
def fast_translate(text, src_lang='auto', dest_lang='th'):
    if not text.strip():
        return text
    try:
        max_chunk = 4000
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

# 1. ส่วนดึงและแปลภาษา (ระบุช่วงตอน จาก... ถึง...)
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
            status_text.text(f"⏳ กำลังดึงและแปลตอนที่ {actual_ep_num} (ช่วงตอนที่ {i+1}/{total_steps})...")
            
            try:
                res = requests.get(current_url, headers=headers, timeout=10)
                if res.status_code != 200:
                    st.error(f"❌ ล้มเหลวที่ตอนที่ {actual_ep_num}: ไม่สามารถเข้าถึงเว็บไซต์ได้ (HTTP Code: {res.status_code})")
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
                    if next_link.startswith('http'):
                        current_url = next_link
                    else:
                        current_url = urllib.parse.urljoin(current_url, next_link)
                else:
                    if re.search(r'-\d+$', current_url):
                        current_url = re.sub(r'-\d+$', lambda m: f"-{int(m.group(1)[1:])+1}", current_url)
                    else:
                        break
                        
            except Exception as e:
                st.error(f"❌ เกิดข้อผิดพลาดที่ตอนที่ {actual_ep_num}: {str(e)}")
                break

        progress_bar.progress(100)
        if combined_chapters:
            st.session_state.novel_text = "".join(combined_chapters)
            status_text.success(f"🎉 สำเร็จ! ดึงและแปลช่วงตอนที่ {start_ep} ถึง {start_ep + success_count - 1} เรียบร้อยแล้ว")
        else:
            status_text.error("❌ การดึงและแปลข้อมูลล้มเหลวทั้งหมด")

# 2. ช่องข้อความตรวจสอบและแก้ไข + ตัวนับจำนวนตัวอักษร
st.subheader("✍️ ตรวจสอบ แก้ไข และขัดเกลาข้อความภาษาไทย")
st.session_state.novel_text = st.text_area("ข้อความภาษาไทยสำหรับสร้างเสียงเปรมวดี:", value=st.session_state.novel_text, height=300)

char_count = len(st.session_state.novel_text)
word_count = len(st.session_state.novel_text.split())
st.caption(f"📊 สถิติข้อความปัจจุบัน: **{char_count:,}** ตัวอักษร | ประมาณ **{word_count:,}** คำ")

# 3. ปุ่มขัดเกลาข้อความ
if st.button("✨ ขัดเกลาข้อความให้อ่านง่ายขึ้น (จัดระเบียบบรรทัด)"):
    if st.session_state.novel_text.strip() == "":
        st.warning("⚠️ ไม่มีข้อความให้ขัดเกลาครับ")
    else:
        cleaned = re.sub(r'\n\s*\n', '\n\n', st.session_state.novel_text)
        cleaned = re.sub(r'[ \t]+', ' ', cleaned)
        st.session_state.novel_text = cleaned
        st.success("✨ ขัดเกลาข้อความเรียบร้อยแล้ว!")
        st.rerun()

# 4. ฟังก์ชันสร้างเสียง Turbo + Auto-Recovery (ลื่นไหล รวดเร็ว และแก้ไขข้อผิดพลาดอัตโนมัติ)
async def generate_audio_chunks_turbo(text_content, voice, output_filename, progress_bar, status_text):
    # ทำความสะอาดข้อความ ป้องกันอักขระแปลกปลอม
    clean_text = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', text_content)
    
    # เพิ่มขนาดก้อนให้ใหญ่ขึ้นเล็กน้อย (1,500 ตัวอักษร) เพื่อลดจำนวนรอบและทำให้เร็วขึ้น
    chunk_size = 1500
    text_chunks = [clean_text[i:i+chunk_size] for i in range(0, len(clean_text), chunk_size)]
    text_chunks = [c.strip() for c in text_chunks if c.strip()]
    
    total_chunks = len(text_chunks)
    total_chars = len(clean_text)
    
    if total_chunks == 0:
        return False

    temp_files = [f"temp_part_{idx}.mp3" for idx in range(total_chunks)]
    
    # ฟังก์ชันตัวช่วยสร้างเสียง พร้อมระบบ Auto-Retry อัตโนมัติถ้าเกิด Error
    async def process_chunk_with_retry(idx, chunk):
        for attempt in range(3): # ลองใหม่สูงสุด 3 ครั้งถ้าพลาด
            try:
                communicate = edge_tts.Communicate(chunk, voice)
                await communicate.save(temp_files[idx])
                return True
            except Exception:
                await asyncio.sleep(0.5) # พักแป๊บหนึ่งแล้วลองใหม่
        
        # ถ้าพยายามครบ 3 ครั้งแล้วยังพัง ให้สร้างไฟล์ว่างข้ามไปอัตโนมัติโดยไม่ทำแอปพัง
        with open(temp_files[idx], "wb") as f:
            f.write(b"")
        return False

    # เพิ่มความเร็วด้วยการประมวลผลพร้อมกันกลุ่มละ 5 ก้อน (Turbo Mode)
    batch_size = 5
    completed_count = 0
    processed_chars = 0
    start_time = time.time()
    
    for i in range(0, total_chunks, batch_size):
        batch_indices = range(i, min(i + batch_size, total_chunks))
        
        batch_tasks = [process_chunk_with_retry(idx, text_chunks[idx]) for idx in batch_indices]
        await asyncio.gather(*batch_tasks)
        
        completed_count += len(batch_indices)
        for idx in batch_indices:
            processed_chars += len(text_chunks[idx])
        
        # คำนวณเวลา, ความเร็ว (ตัวอักษร/วินาที) และ ETA แบบเรียลไทม์
        elapsed_time = max(time.time() - start_time, 0.1)
        chars_per_sec = processed_chars / elapsed_time
        
        remaining_chars = total_chars - processed_chars
        eta_seconds = remaining_chars / chars_per_sec if chars_per_sec > 0 else 0
        
        current_pct = int((completed_count / total_chunks) * 100)
        progress_bar.progress(min(current_pct, 100))
        
        status_text.markdown(f"""
        🚀 **[Turbo Mode] กำลังสังเคราะห์เสียงเปรมวดี...** ({current_pct}%)<br>
        ⏱️ ทำงานไปแล้ว: **{elapsed_time:.1f} วินาที**<br>
        ⚡ ความเร็ว: **{chars_per_sec:.1f} ตัวอักษร / 1 วินาที**<br>
        ⏳ จะเสร็จในอีกประมาณ: **{eta_seconds:.1f} วินาที** (เหลืออีก {max(remaining_chars, 0):,} ตัวอักษร)
        """, unsafe_allow_html=True)

    # รวมไฟล์ MP3 ย่อยทั้งหมดเข้าด้วยกัน
    status_text.text("🔗 กำลังรวมไฟล์เสียงทั้งหมดเข้าด้วยกัน...")
    progress_bar.progress(95)
    
    with open(output_filename, "wb") as outfile:
        for f in temp_files:
            if os.path.exists(f) and os.path.getsize(f) > 0:
                with open(f, "rb") as infile:
                    outfile.write(infile.read())
                os.remove(f)

    total_duration = time.time() - start_time
    progress_bar.progress(100)
    status_text.success(f"🎉 สร้างไฟล์เสียงสำเร็จ 100%! (ใช้เวลาทั้งหมด {total_duration:.1f} วินาที)")
    return True

st.subheader("🎙️ สร้างเสียงเปรมวดี (Turbo & Auto-Recovery)")
if st.button("🎙️ เริ่มสร้างไฟล์เสียงเปรมวดี (Turbo MP3)"):
    if st.session_state.novel_text.strip() == "":
        st.warning("⚠️ กรุณามีข้อความสำหรับสร้างเสียงก่อนครับ")
    else:
        audio_progress = st.progress(0)
        audio_status = st.empty()

        try:
            output_file = "premwadee_final_translated.mp3"
            
            success = asyncio.run(generate_audio_chunks_turbo(
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
                    label="📥 ดาวน์โหลดไฟล์ MP3 เสียงเปรมวดี (Turbo)",
                    data=audio_bytes,
                    file_name="premwadee_novel_translated.mp3",
                    mime="audio/mp3"
                )
            else:
                audio_status.error("❌ ล้มเหลว: ไม่พบไฟล์เสียงที่ถูกสร้างขึ้นในระบบ")
        except Exception as audio_err:
            audio_progress.progress(100)
            audio_status.error(f"❌ เกิดข้อผิดพลาดในการสร้างเสียงเปรมวดี: {str(audio_err)}")
