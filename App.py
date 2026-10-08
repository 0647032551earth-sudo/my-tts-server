import streamlit as st
import asyncio
import edge_tts
import os

st.set_page_config(page_title="Text to Speech - เปรมวดี", page_icon="🗣️")

st.title("🗣️ แปลงข้อความ เป็นเสียงเปรมวดี (MP3)")

text = st.text_area("กรอกข้อความที่ต้องการแปลงเสียง:", "สวัสดีครับ ยินดีต้อนรับสู่ระบบแปลงเสียงเปรมวดี")

# ฟังก์ชันสำหรับสร้างเสียงด้วย edge-tts แบบ async
async def generate_audio(text, voice, output_file):
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(output_file)

if st.button("สร้างไฟล์เสียงเปรมวดี"):
    if text.strip() == "":
        st.warning("กรุณากรอกข้อความก่อนครับ")
    else:
        with st.spinner("กำลังสังเคราะห์เสียงเปรมวดี..."):
            output_file = "premwadee_output.mp3"
            
            # รันฟังก์ชัน async บน Streamlit
            asyncio.run(generate_audio(text, "th-TH-PremwadeeNeural", output_file))
            
            with open(output_file, "rb") as f:
                audio_bytes = f.read()
            
            st.success("สร้างเสียงเปรมวดีสำเร็จ!")
            st.audio(audio_bytes, format="audio/mp3")
            
            st.download_button(
                label="ดาวน์โหลดไฟล์ MP3 เสียงเปรมวดี",
                data=audio_bytes,
                file_name="premwadee.mp3",
                mime="audio/mp3"
            )
