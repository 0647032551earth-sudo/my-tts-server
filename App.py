import streamlit as st
import asyncio
import edge_tts
import requests
from bs4 import BeautifulSoup
import os

st.set_page_config(page_title="Novel to Speech - เปรมวดี", page_icon="📖")

st.title("📖 แปลงลิงก์นิยาย เป็นเสียงเปรมวดี (MP3)")

# ส่วนที่ 1: ช่องสำหรับวางลิงก์ดึงเนื้อหา
st.subheader("🔗 ดึงเนื้อหาจากลิงก์เว็บนิยาย")
url_input = st.text_input("วางลิงก์หน้าเว็บนิยายที่ต้องการดึงข้อความ:", "")

fetched_text = ""
if st.button("ดึงเนื้อหาจากลิงก์"):
    if url_input.strip() == "":
        st.warning("กรุณากรอกลิงก์เว็บไซต์ก่อนครับ")
    else:
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            response = requests.get(url_input, headers=headers, timeout=10)
            response.raise_for_status()
            
            # ดึงข้อความด้วย BeautifulSoup
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # ดึงเนื้อหาจากแท็ก <p> ทั้งหมดในหน้า (มักเป็นเนื้อหานิยาย)
            paragraphs = soup.find_all('p')
            extracted_text = "\n".join([p.get_text() for p in paragraphs])
            
            if len(extracted_text.strip()) > 50:
                fetched_text = extracted_text
                st.success("ดึงเนื้อหานิยายสำเร็จ!")
            else:
                st.warning("ดึงข้อมูลได้น้อยเกินไป เว็บนี้อาจมีการป้องกันการดึงข้อมูล แนะนำให้คัดลอกเนื้อหามาวางด้านล่างแทนครับ")
        except Exception as e:
            st.error(f"เกิดข้อผิดพลาดในการดึงข้อมูล: {e}")

# ส่วนที่ 2: ช่องข้อความหลักสำหรับสร้างเสียง (สามารถแก้ไขหรือวางข้อความเองได้)
st.subheader("✍️ ตรวจสอบหรือแก้ไขข้อความก่อนแปลงเสียง")
default_text = fetched_text if fetched_text else "สวัสดีครับ วางลิงก์ด้านบนแล้วกดดึงข้อความ หรือพิมพ์เนื้อหานิยายที่นี่ได้เลยครับ"
text = st.text_area("ข้อความที่จะนำไปสร้างเสียงเปรมวดี:", default_text, height=250)

# ฟังก์ชัน async สำหรับสร้างเสียงด้วย edge-tts
async def generate_audio(text_content, voice, output_file):
    communicate = edge_tts.Communicate(text_content, voice)
    await communicate.save(output_file)

# ส่วนที่ 3: ปุ่มสร้างเสียงเปรมวดี
if st.button("🎙️ สร้างไฟล์เสียงเปรมวดี (MP3)"):
    if text.strip() == "":
        st.warning("กรุณามีข้อความสำหรับสร้างเสียงก่อนครับ")
    else:
        with st.spinner("กำลังสังเคราะห์เสียงเปรมวดีจากข้อความ..."):
            output_file = "premwadee_novel.mp3"
            
            # รันฟังก์ชัน async
            asyncio.run(generate_audio(text, "th-TH-PremwadeeNeural", output_file))
            
            with open(output_file, "rb") as f:
                audio_bytes = f.read()
            
            st.success("สร้างเสียงเปรมวดีสำเร็จเรียบร้อย!")
            st.audio(audio_bytes, format="audio/mp3")
            
            st.download_button(
                label="📥 ดาวน์โหลดไฟล์ MP3 เสียงเปรมวดี",
                data=audio_bytes,
                file_name="premwadee_novel.mp3",
                mime="audio/mp3"
            )
