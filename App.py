import streamlit as st
import asyncio
import edge_tts
import requests
from bs4 import BeautifulSoup
import urllib.parse
import re
import os

st.set_page_config(page_title="Novel to Speech - เปรมวดี Pro (Real & Fast)", page_icon="🌐")

st.title("🌐 ระบบดึงนิยาย + แปลไทยความเร็วสูง + สร้างเสียงเปรมวดี (ใช้งานจริง)")

# กำหนดค่าเริ่มต้นใน session_state ป้องกันข้อมูลหาย
if "novel_text" not in st.session_state:
    st.session_state.novel_text = "วางลิงก์ตอนแรกด้านบนแล้วกดปุ่มสั่งดึงและแปล หรือพิมพ์ข้อความภาษาไทยที่นี่ได้เลยครับ"

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

# 1. ส่วนดึงและแปลภาษาความเร็วสูง
st.subheader("🔗 ดึงนิยายต่อเนื่อง + แปลไทยแบบรวดเร็ว")
start_url = st.text_input("วางลิงก์หน้าเว็บนิยาย 'ตอนเริ่มต้น' (เช่น ตอนที่ 1):", "")

col_a, col_b = st.columns(2)
with col_a:
    num_chapters = st.number_input("จำนวนตอนที่ต้องการดึง:", min_value=1, max_value=20, value=2)
with col_b:
    src_lang = st.selectbox("ภาษาต้นทางของเว็บ:", ["auto", "zh-CN", "en", "ja"], index=0)

if st.button("🚀 สั่งดึงและแปล (ความเร็วสูง)"):
    if start_url.strip() == "":
        st.warning("⚠️ กรุณากรอกลิงก์เริ่มต้นก่อนครับ")
    else:
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        combined_chapters = []
        current_url = start_url
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        
        success_count = 0
        total_steps = int(num_chapters)
        
        for i in range(total_steps):
            percent_complete = int(((i) / total_steps) * 100)
            progress_bar.progress(percent_complete + 1)
            status_text.text(f"⏳ กำลังดึงและแปลตอนที่ {i+1} จาก {total_steps}...")
            
            try:
                res = requests.get(current_url, headers=headers, timeout=10)
                if res.status_code != 200:
                    st.error(f"❌ ล้มเหลวที่ตอนที่ {i+1}: ไม่สามารถเข้าถึงเว็บไซต์ได้ (HTTP Code: {res.status_code})")
                    break
                    
                soup = BeautifulSoup(res.text, 'html.parser')
                paragraphs = soup.find_all('p')
                raw_chapter_content = "\n".join([p.get_text() for p in paragraphs])
                
                if len(raw_chapter_content.strip()) < 10:
                    raw_chapter_content = soup.get_text()

                translated_content = fast_translate(raw_chapter_content, src_lang=src_lang, dest_lang='th')
                
                combined_chapters.append(f"\n\n=== ตอนที่ {i+1} ===\n\n" + translated_content)
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
                st.error(f"❌ เกิดข้อผิดพลาดที่ตอนที่ {i+1}: {str(e)}")
                break

        progress_bar.progress(100)
        if combined_chapters:
            st.session_state.novel_text = "".join(combined_chapters)
            status_text.success(f"🎉 สำเร็จ! ดึงและแปลเรียบร้อย {success_count} จาก {total_steps} ตอน")
        else:
            status_text.error("❌ การดึงและแปลข้อมูลล้มเหลวทั้งหมด")

# 2. ช่องข้อความตรวจสอบและแก้ไข + ตัวนับจำนวนตัวอักษร
st.subheader("✍️ ตรวจสอบ แก้ไข และขัดเกลาข้อความภาษาไทย")
st.session_state.novel_text = st.text_area("ข้อความภาษาไทยสำหรับสร้างเสียงเปรมวดี:", value=st.session_state.novel_text, height=300)

# แสดงจำนวนตัวอักษรแบบเรียลไทม์
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

# ฟังก์ชันสร้างเสียงจริงแบบไม่หลอกตา
async def generate_audio_real(text_content, voice, output_file, status_callback):
    status_callback("กำลังเชื่อมต่อระบบสังเคราะห์เสียงเปรมวดี...")
    communicate = edge_tts.Communicate(text_content, voice)
    status_callback("กำลังประมวลผลเสียงพากย์ภาษาไทย (th-TH-PremwadeeNeural)...")
    await communicate.save(output_file)
    status_callback("สร้างไฟล์เสียงสำเร็จเรียบร้อย!")

# 4. ปุ่มสร้างเสียงเปรมวดี
st.subheader("🎙️ สร้างเสียงเปรมวดี (ความเร็วสูงและใช้งานจริง)")
if st.button("🎙️ เริ่มสร้างไฟล์เสียงเปรมวดี (MP3)"):
    if st.session_state.novel_text.strip() == "":
        st.warning("⚠️ กรุณามีข้อความสำหรับสร้างเสียงก่อนครับ")
    else:
        status_box = st.empty()
        
        def update_status(msg):
            status_box.info(f"🎧 {msg}")

        try:
            output_file = "premwadee_final_translated.mp3"
            
            asyncio.run(generate_audio_real(st.session_state.novel_text, "th-TH-PremwadeeNeural", output_file, update_status))
            
            if os.path.exists(output_file):
                with open(output_file, "rb") as f:
                    audio_bytes = f.read()
                
                status_box.success("🎉 สร้างเสียงเปรมวดีจากข้อความแปลไทยสำเร็จเรียบร้อย!")
                st.audio(audio_bytes, format="audio/mp3")
                
                st.download_button(
                    label="📥 ดาวน์โหลดไฟล์ MP3 เสียงเปรมวดี",
                    data=audio_bytes,
                    file_name="premwadee_novel_translated.mp3",
                    mime="audio/mp3"
                )
            else:
                status_box.error("❌ ล้มเหลว: ไม่พบไฟล์เสียงที่ถูกสร้างขึ้นในระบบ")
        except Exception as audio_err:
            status_box.error(f"❌ เกิดข้อผิดพลาดในการสร้างเสียงเปรมวดี: {str(audio_err)}")
