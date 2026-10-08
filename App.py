import streamlit as st
import asyncio
import edge_tts
import requests
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator
import re
import os

st.set_page_config(page_title="Novel to Speech - เปรมวดี AI Pro (แปล+ดึง+เสียง)", page_icon="🎙️")

st.title("🎙️ ระบบดึงนิยาย + แปลภาษาไทย + เสียงเปรมวดีอัตโนมัติ")

# 1. ฟังก์ชันเก่า + ใหม่: ระบบดึงข้อมูลและแปลภาษาต่อเนื่องอัตโนมัติ
st.subheader("🔗 ดึงนิยายต่อเนื่อง + แปลเป็นไทยอัตโนมัติ")
start_url = st.text_input("วางลิงก์หน้าเว็บนิยาย 'ตอนเริ่มต้น' (เช่น ตอนที่ 1):", "")

col_a, col_b = st.columns(2)
with col_a:
    num_chapters = st.number_input("จำนวนตอนที่ต้องการดึง:", min_value=1, max_value=10, value=2)
with col_b:
    target_lang = st.selectbox("แปลภาษาต้นทางเป็น:", ["th", "en"], index=0, help="เลือก th หากต้องการแปลเป็นไทย")

fetched_text = ""
if st.button("🚀 สั่งดึงและแปลเนื้อหาอัตโนมัติ"):
    if start_url.strip() == "":
        st.warning("กรุณากรอกลิงก์เริ่มต้นก่อนครับ")
    else:
        with st.spinner(f"กำลังดึงเนื้อหาและแปลภาษา {num_chapters} ตอน..."):
            combined_chapters = []
            current_url = start_url
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            translator = GoogleTranslator(source='auto', target='th')
            
            for i in range(int(num_chapters)):
                try:
                    res = requests.get(current_url, headers=headers, timeout=10)
                    if res.status_code != 200:
                        break
                        
                    soup = BeautifulSoup(res.text, 'html.parser')
                    
                    # ดึงเนื้อหาจากแท็ก <p>
                    paragraphs = soup.find_all('p')
                    raw_chapter_content = "\n".join([p.get_text() for p in paragraphs])
                    
                    if len(raw_chapter_content.strip()) > 50:
                        # แปลข้อความทีละย่อหน้าเพื่อความแม่นยำและไม่ให้เกินลิมิต
                        paragraphs_list = raw_chapter_content.split('\n')
                        translated_paragraphs = []
                        for para in paragraphs_list:
                            if para.strip():
                                try:
                                    # แปลเป็นภาษาไทย
                                    trans_text = translator.translate(para)
                                    translated_paragraphs.append(trans_text)
                                except:
                                    translated_paragraphs.append(para)
                            else:
                                translated_paragraphs.append("")
                        
                        translated_content = "\n".join(translated_paragraphs)
                        combined_chapters.append(f"\n\n=== ตอนที่ {i+1} ===\n\n" + translated_content)
                    
                    # ค้นหาลิงก์ตอนถัดไปอัตโนมัติ
                    next_link = None
                    for a in soup.find_all('a', href=True):
                        text_a = a.get_text().lower()
                        if 'next' in text_a or 'ถัดไป' in text_a or '>>' in text_a:
                            next_link = a['href']
                            break
                    
                    if next_link:
                        if next_link.startswith('http'):
                            current_url = next_link
                        else:
                            from urllib.parse import urljoin
                            current_url = urljoin(current_url, next_link)
                    else:
                        if re.search(r'-\d+$', current_url):
                            current_url = re.sub(r'-\d+$', lambda m: f"-{int(m.group(1)[1:])+1}", current_url)
                        else:
                            break
                except Exception as e:
                    break
            
            if combined_chapters:
                fetched_text = "".join(combined_chapters)
                st.success(f"ดึงและแปลภาษาไทยสำเร็จ {len(combined_chapters)} ตอน!")
            else:
                st.warning("ไม่สามารถดึงข้อมูลอัตโนมัติได้ แนะนำให้คัดลอกข้อความมาวางด้านล่างครับ")

# 2. ฟังก์ชันเก่า: ช่องข้อความตรวจสอบและแก้ไข
st.subheader("✍️ ตรวจสอบ แก้ไข และขัดเกลาข้อความ")
default_text = fetched_text if fetched_text else "วางลิงก์เริ่มต้นด้านบนแล้วกดปุ่มดึงและแปลอัตโนมัติ หรือพิมพ์ข้อความที่นี่ได้เลยครับ"
text = st.text_area("ข้อความภาษาไทยที่จะนำไปสร้างเสียงเปรมวดี:", default_text, height=300)

# 3. ฟังก์ชันเก่า: ปุ่มขัดเกลาข้อความ
if st.button("✨ ขัดเกลาข้อความให้อ่านง่ายขึ้น (จัดระเบียบบรรทัด)"):
    if text.strip() == "":
        st.warning("ไม่มีข้อความให้ขัดเกลาครับ")
    else:
        cleaned = re.sub(r'\n\s*\n', '\n\n', text)
        cleaned = re.sub(r'[ \t]+', ' ', cleaned)
        text = cleaned
        st.success("ขัดเกลาข้อความเรียบร้อยแล้ว!")
        st.rerun()

# ฟังก์ชัน async สำหรับสร้างเสียงด้วย edge-tts (เสียงเปรมวดี)
async def generate_audio(text_content, voice, output_file):
    communicate = edge_tts.Communicate(text_content, voice)
    await communicate.save(output_file)

# 4. ฟังก์ชันเก่า: ปุ่มสร้างเสียงเปรมวดีและดาวน์โหลด MP3
if st.button("🎙️ สร้างไฟล์เสียงเปรมวดี (MP3)"):
    if text.strip() == "":
        st.warning("กรุณามีข้อความสำหรับสร้างเสียงก่อนครับ")
    else:
        with st.spinner("กำลังสังเคราะห์เสียงเปรมวดีจากเนื้อหาภาษาไทย..."):
            output_file = "premwadee_translated_novel.mp3"
            
            # เรียกใช้เสียงเปรมวดี th-TH-PremwadeeNeural
            asyncio.run(generate_audio(text, "th-TH-PremwadeeNeural", output_file))
            
            with open(output_file, "rb") as f:
                audio_bytes = f.read()
            
            st.success("สร้างเสียงเปรมวดีจากเนื้อหาแปลไทยสำเร็จเรียบร้อย!")
            st.audio(audio_bytes, format="audio/mp3")
            
            st.download_button(
                label="📥 ดาวน์โหลดไฟล์ MP3 เสียงเปรมวดี",
                data=audio_bytes,
                file_name="premwadee_novel_translated.mp3",
                mime="audio/mp3"
            )
