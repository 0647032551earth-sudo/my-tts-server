import streamlit as st
from gtts import gTTS
import os

# ตั้งค่าหน้าเว็บ
st.set_page_config(page_title="สปีช สเตอร์ (T2S Pro)", page_icon="🗣️")

st.markdown("""
<style>
.main-title { text-align: center; font-weight: 800; margin-bottom: 20px; }
.char-box { font-size: 0.9rem; font-weight: 700; color: #4c4636; margin-bottom: 10px; }
</style>
""", unsafe_allow_html=True)

st.markdown("<h2 class='main-title'>🗣️ สปีช สเตอร์ (สร้างและดาวน์โหลด MP3)</h2>", unsafe_allow_html=True)

# ข้อความเริ่มต้น
default_text = "คืนนั้น มะลินอนดาวผ่านช่องหน้าต่าง เธอคิดถึงเด็กชายและแม่ของเขา แล้วคิดว่าความสุขที่แท้จริงอาจไม่ใช่การได้รับสิ่งของมากมาย แต่คือการได้ให้ และได้เห็นรอยยิ้มของคนที่เรารัก"

# ช่องกรอกข้อความ
text = st.text_area("📝 ข้อความของคุณ (ไม่จำกัดตัวอักษร)", value=default_text, height=180)

# 1. ระบบนับจำนวนตัวอักษรแบบเรียลไทม์
char_count = len(text)
st.markdown(f"<div class='char-box'>📊 จำนวนตัวอักษรทั้งหมด: <b>{char_count:,}</b> ตัวอักษร</div>", unsafe_allow_html=True)

# ตัวเลือกภาษา / เสียง
lang_map = {
    "ภาษาไทย (TH)": "th",
    "English (US)": "en",
    "日本語 (JP)": "ja",
    "中文 (CN)": "zh-CN"
}
selected_lang_name = st.selectbox("🌐 เลือกภาษา / เสียงในระบบ", list(lang_map.keys()))
lang_code = lang_map[selected_lang_name]

st.markdown("---")

# ส่วนประมวลผลและสร้างไฟล์ MP3
if st.button("📥 สร้าง
