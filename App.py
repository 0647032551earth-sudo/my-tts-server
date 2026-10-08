import streamlit as st
from gtts import gTTS
import os

st.set_page_config(page_title="Text to Speech", page_icon="🗣️")

st.title("🗣️ แปลงข้อความ เป็นเสียง MP3")

text = st.text_area("กรอกข้อความที่ต้องการแปลงเสียง:", "สวัสดีครับ ยินดีต้อนรับสู่ระบบแปลงข้อความภาษาไทย")

lang = st.selectbox("เลือกภาษา", ["th", "en", "ja", "zh-CN"])

if st.button("สร้างไฟล์เสียง MP3"):
    if text.strip() == "":
        st.warning("กรุณากรอกข้อความก่อนครับ")
    else:
        with st.spinner("กำลังสร้างไฟล์เสียง..."):
            tts = gTTS(text=text, lang=lang, slow=False)
            file_path = "output.mp3"
            tts.save(file_path)
            
            with open(file_path, "rb") as f:
                audio_bytes = f.read()
            
            st.success("สร้างไฟล์สำเร็จ!")
            st.audio(audio_bytes, format="audio/mp3")
            
            st.download_button(
                label="ดาวน์โหลดไฟล์ MP3",
                data=audio_bytes,
                file_name="speech.mp3",
                mime="audio/mp3"
            )
