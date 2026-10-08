import streamlit as st
import asyncio
import edge_tts
import requests
from bs4 import BeautifulSoup
import urllib.parse
import re
import os

st.set_page_config(page_title="Novel to Speech - เปรมวดี Pro (Fast Real Progress)", page_icon="🌐")

st.title("🌐 ระบบดึงนิยาย (ระบุช่วงตอน) + แปลไทย + สร้างเสียงเปรมวดี (เร่งความเร็ว + เปอร์เซ็นต์จริง)")

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

# 4. ฟังก์ชันสร้างเสียงแบบคู่ขนาน (รวดเร็วขึ้น + ได้เปอร์เซ็นต์จริง)
async def generate_audio_chunks_fast(text_content, voice, output_filename, progress_bar, status_text):
    # ขยายขนาดท่อนเป็น 3,000 ตัวอักษรต่อท่อน เพื่อลดจำนวนครั้งในการวนลูป
    chunk_size = 3000
    text_chunks = [text_content[i:i+chunk_size] for i in range(0, len(text_content), chunk_size)]
    total_chunks = len(text_chunks)
    
    if total_chunks == 0:
        return False

    temp_files = [f"temp_part_{idx}.mp3" for idx in range(total_chunks)]
    
    # ฟังก์ชันช่วยแปลงทีละท่อน
    async def process_chunk(idx, chunk):
        communicate = edge_tts.Communicate(chunk, voice)
        await communicate.save(temp_files[idx])

    # แบ่งงานทำเป็นกลุ่ม (ครั้งละ 4 ท่อนพร้อมกัน) เพื่อความเร็วสูงสุด
    batch_size = 4
    completed_count = 0
    
    for i in range(0, total_chunks, batch_size):
        batch_indices = range(i, min(i + batch_size, total_chunks))
        
        # อัปเดตสถานะและเปอร์เซ็นต์ตามความคืบหน้าจริง
        current_pct = int((completed_count / total_chunks) * 100)
        progress_bar.progress(max(current_pct, 1))
        status_text.text(f"🎧 กำลังสังเคราะห์เสียงกลุ่มท่อนที่ {i+1} ถึง {min(i+batch_size, total_chunks)} จาก {total_chunks} ({current_pct}%)...")
        
        # สั่งรันพร้อมกันในกลุ่ม
        tasks = [process_chunk(idx, text_chunks[idx]) for idx in batch_indices]
        await asyncio.gather(*tasks)
        
        completed_count += len(batch_indices)

    # รวมไฟล์ MP3 ย่อยทั้งหมดเข้าด้วยกัน
    status_text.text("🔗 กำลังรวมไฟล์เสียงทั้งหมดเข้าด้วยกัน...")
    progress_bar.progress(95)
    
    with open(output_filename, "wb") as outfile:
        for f in temp_files:
            if os.path.exists(f):
                with open(f, "rb") as infile:
                    outfile.write(infile.read())
                os.remove(f) # ลบไฟล์ย่อยทิ้งหลังรวมเสร็จ

    progress_bar.progress(100)
    status_text.text("🎉 สร้างไฟล์เสียงสำเร็จ 100%!")
    return True

st.subheader("🎙️ สร้างเสียงเปรมวดี (เร่งความเร็ว + เปอร์เซ็นต์จริง)")
if st.button("🎙️ เริ่มสร้างไฟล์เสียงเปรมวดี (MP3)"):
    if st.session_state.novel_text.strip() == "":
        st.warning("⚠️ กรุณามีข้อความสำหรับสร้างเสียงก่อนครับ")
    else:
        audio_progress = st.progress(0)
        audio_status = st.empty()

        try:
            output_file = "premwadee_final_translated.mp3"
            
            # รันฟังก์ชันสร้างเสียงแบบกลุ่มคู่ขนานผ่าน asyncio
            success = asyncio.run(generate_audio_chunks_fast(
                st.session_state.novel_text, 
                "th-TH-PremwadeeNeural", 
                output_file, 
                audio_progress, 
                audio_status
            ))
            
            if success and os.path.exists(output_file):
                with open(output_file, "rb") as f:
                    audio_bytes = f.read()
                
                audio_status.success("🎉 สร้างเสียงเปรมวดีจากข้อความแปลไทยสำเร็จเรียบร้อย!")
                st.audio(audio_bytes, format="audio/mp3")
                
                st.download_button(
                    label="📥 ดาวน์โหลดไฟล์ MP3 เสียงเปรมวดี",
                    data=audio_bytes,
                    file_name="premwadee_novel_translated.mp3",
                    mime="audio/mp3"
                )
            else:
                audio_status.error("❌ ล้มเหลว: ไม่พบไฟล์เสียงที่ถูกสร้างขึ้นในระบบ")
        except Exception as audio_err:
            audio_progress.progress(100)
            audio_status.error(f"❌ เกิดข้อผิดพลาดในการสร้างเสียงเปรมวดี: {str(audio_err)}")
