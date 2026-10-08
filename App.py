import streamlit as st
import asyncio
import edge_tts
import requests
from bs4 import BeautifulSoup
import re
import os

st.set_page_config(page_title="Novel to Speech - เปรมวดี Pro", page_icon="📖")

st.title("📖 ระบบแปลงนิยายเป็นเสียงเปรมวดี (พร้อมดึงช่วงตอน & ขัดเกลาข้อความ)")

# ส่วนที่ 1: ตั้งค่าการดึงนิยายตามช่วงตอน
st.subheader("🔗 ดึงเนื้อหานิยายตามช่วงตอน")
base_url = st.text_input("วางลิงก์หน้าเว็บนิยาย (เช่น เว็บที่มีการรันเลขตอน เช่น .../chapter/1):", "")

col1, col2 = st.columns(2)
with col1:
    start_ep = st.number_input("จากตอนที่:", min_value=1, value=1)
with col2:
    end_ep = st.number_input("ถึงตอนที่:", min_value=1, value=1)

fetched_text = ""
if st.button("📥 ดึงเนื้อหาตามช่วงตอน"):
    if base_url.strip() == "":
        st.warning("กรุณากรอกลิงก์เว็บไซต์ก่อนครับ")
    else:
        with st.spinner(f"กำลังดึงเนื้อหาตั้งแต่ตอนที่ {start_ep} ถึง {end_ep}..."):
            combined_chapters = []
            try:
                headers = {"User-Agent": "Mozilla/5.0"}
                
                # วนลูปดึงข้อมูลตามช่วงตอนที่ระบุ (ตัวอย่างการแทนที่เลขตอนใน URL)
                for ep in range(int(start_ep), int(end_ep) + 1):
                    # สมมติฐานโครงสร้างลิงก์ทั่วไป สามารถปรับเปลี่ยนวิธีต่อ URL ได้ตามเว็บที่ใช้
                    target_url = base_url
                    if "{ep}" in base_url:
                        target_url = base_url.format(ep=ep)
                    else:
                        # หากไม่มี {ep} ให้ลองพยายามต่อท้าย หรือดึงจากลิงก์เดียว
                        target_url = f"{base_url.rstrip('/')}/{ep}"
                    
                    try:
                        res = requests.get(target_url, headers=headers, timeout=5)
                        if res.status_code == 200:
                            soup = BeautifulSoup(res.text, 'html.parser')
                            paragraphs = soup.find_all('p')
                            chapter_text = f"\n\n--- ตอนที่ {ep} ---\n\n" + "\n".join([p.get_text() for p in paragraphs])
                            combined_chapters.append(chapter_text)
                    except:
                        continue
                
                if combined_chapters:
                    fetched_text = "".join(combined_chapters)
                    st.success(f"ดึงเนื้อหาสำเร็จตั้งแต่ตอนที่ {start_ep} ถึง {end_ep}!")
                else:
                    st.warning("ไม่สามารถดึงข้อมูลตามช่วงตอนได้อัตโนมัติ แนะนำให้วางลิงก์ทีละตอน หรือคัดลอกข้อความมาวางด้านล่างครับ")
            except Exception as e:
                st.error(f"เกิดข้อผิดพลาด: {e}")

# ส่วนที่ 2: ช่องข้อความหลัก พร้อมฟังก์ชันขัดเกลา
st.subheader("✍️ ตรวจสอบ แก้ไข และขัดเกลาข้อความ")
default_text = fetched_text if fetched_text else "วางลิงก์ด้านบนแล้วกดดึงตอน หรือพิมพ์เนื้อหานิยายที่นี่ได้เลยครับ"
text = st.text_area("ข้อความที่จะนำไปสร้างเสียงเปรมวดี:", default_text, height=300)

# ปุ่มฟังก์ชันขัดเกลาข้อความ (Clean/Refine Text)
if st.button("✨ ขัดเกลาข้อความให้อ่านง่ายขึ้น (ลบอักขระพิเศษส่วนเกิน)"):
    if text.strip() == "":
        st.warning("ไม่มีข้อความให้ขัดเกลาครับ")
    else:
        # ทำความสะอาดข้อความ เช่น ลบช่องว่างเกิน ลบแท็กแปลกๆ หรือสัญลักษณ์ซ้ำซ้อน
        cleaned = re.sub(r'\n\s*\n', '\n\n', text)  // จัดระเบียบบรรทัดว่าง
        cleaned = re.sub(r'[ \t]+', ' ', cleaned)   // ลดช่องว่างติดกัน
        text = cleaned
        st.success("ขัดเกลาข้อความเรียบร้อยแล้ว!")
        st.rerun()

# ฟังก์ชัน async สำหรับสร้างเสียงด้วย edge-tts
async def generate_audio(text_content, voice, output_file):
    communicate = edge_tts.Communicate(text_content, voice)
    await communicate.save(output_file)

# ส่วนที่ 3: ปุ่มสร้างเสียงเปรมวดี
if st.button("🎙️ สร้างไฟล์เสียงเปรมวดี (MP3)"):
    if text.strip() == "":
        st.warning("กรุณามีข้อความสำหรับสร้างเสียงก่อนครับ")
    else:
        with st.spinner("กำลังสังเคราะห์เสียงเปรมวดี (ความยาวตามเนื้อหา)..."):
            output_file = "premwadee_range_novel.mp3"
            
            # รันฟังก์ชัน async
            asyncio.run(generate_audio(text, "th-TH-PremwadeeNeural", output_file))
            
            with open(output_file, "rb") as f:
                audio_bytes = f.read()
            
            st.success("สร้างเสียงเปรมวดีสำเร็จเรียบร้อย!")
            st.audio(audio_bytes, format="audio/mp3")
            
            st.download_button(
                label="📥 ดาวน์โหลดไฟล์ MP3 เสียงเปรมวดี",
                data=audio_bytes,
                file_name="premwadee_chapters.mp3",
                mime="audio/mp3"
            )
