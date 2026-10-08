import streamlit as st
import asyncio
import edge_tts
import requests
from bs4 import BeautifulSoup
import urllib.parse
import re
import os
import time

st.set_page_config(page_title="Novel to Speech - Turbo 50 Chapters", page_icon="🚀")

st.title("🚀 ระบบดึงนิยาย + แปลไทย + สร้างเสียง (Turbo 50 Chapters - พร้อม Progress & Timer)")

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
            params = {"client": "gtx", "sl": src_lang, "tl": dest_lang, "dt": "t", "q": chunk}
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

# ฟังก์ชันแปลงเสียงย่อยแบบขนาน (Parallel TTS)
async def text_to_speech_fast(text, voice, output_file):
    clean_text = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', text)
    sub_chunk_size = 2000
    sub_chunks = [clean_text[i:i+sub_chunk_size] for i in range(0, len(clean_text), sub_chunk_size)]
    sub_chunks = [c.strip() for c in sub_chunks if c.strip()]
    
    if not sub_chunks:
        with open(output_file, "wb") as f:
            f.write(b"")
        return

    sub_files = [f"sub_{os.getpid()}_{idx}.mp3" for idx in range(len(sub_chunks))]
    
    async def fetch_sub(s_idx, s_text):
        for _ in range(2):
            try:
                communicate = edge_tts.Communicate(s_text, voice)
                await communicate.save(sub_files[s_idx])
                return
            except Exception:
                await asyncio.sleep(0.2)
        with open(sub_files[s_idx], "wb") as f:
            f.write(b"")

    tasks = [fetch_sub(idx, chunk) for idx, chunk in enumerate(sub_chunks)]
    await asyncio.gather(*tasks)

    with open(output_file, "wb") as outfile:
        for sf in sub_files:
            if os.path.exists(sf) and os.path.getsize(sf) > 0:
                with open(sf, "rb") as sf_in:
                    outfile.write(sf_in.read())
                os.remove(sf)

# ตั้งค่าหน้าจอการใช้งาน
st.subheader("🔗 ตั้งค่าช่วงตอน (รองรับสูงสุด 50+ ตอนรวดเดียว)")
start_url = st.text_input("วางลิงก์หน้าเว็บนิยาย 'ตอนเริ่มต้น':", "")

col_a, col_b, col_c = st.columns(3)
with col_a:
    start_ep = st.number_input("เริ่มต้นที่ตอนที่:", min_value=1, value=1)
with col_b:
    end_ep = st.number_input("สิ้นสุดที่ตอนที่ (เช่น 50):", min_value=1, value=50)
with col_c:
    src_lang = st.selectbox("ภาษาต้นทาง:", ["auto", "zh-CN", "en", "ja"], index=0)

voice_choice = st.selectbox("เลือกเสียงพากย์:", ["th-TH-PremwadeeNeural", "th-TH-NiwatNeural"], index=0)

# ช่องแสดงข้อความ
st.subheader("✍️ ข้อความนิยายรวมทุกตอน")
st.session_state.novel_text = st.text_area("ข้อความภาษาไทยสำหรับสร้างเสียง:", value=st.session_state.novel_text, height=200)

char_count = len(st.session_state.novel_text)
st.caption(f"📊 สถิติข้อความปัจจุบัน: **{char_count:,}** ตัวอักษร")

col_btn1, col_btn2 = st.columns(2)
with col_btn1:
    if st.button("✨ ขัดเกลาข้อความให้อ่านง่ายขึ้น"):
        cleaned = re.sub(r'\n\s*\n', '\n\n', st.session_state.novel_text)
        st.session_state.novel_text = re.sub(r'[ \t]+', ' ', cleaned)
        st.success("✨ ขัดเกลาข้อความเรียบร้อยแล้ว!")
        st.rerun()
with col_btn2:
    if st.button("🗑️ ล้างข้อความทั้งหมด"):
        st.session_state.novel_text = ""
        st.rerun()

# ปุ่มรันความเร็วสูงพร้อม Progress และ Timer
st.subheader("🎙️ ระบบสร้างเสียงความเร็วสูง (Turbo 50 Episodes)")
if st.button("🚀 เริ่มแปลง 50 ตอนอัตโนมัติแบบความเร็วสูง"):
    if start_url.strip() == "":
        st.warning("⚠️ กรุณากรอกลิงก์เริ่มต้นก่อนครับ")
    elif end_ep < start_ep:
        st.warning("⚠️ ตอนสิ้นสุดต้องมากกว่าหรือเท่ากับตอนเริ่มต้นครับ")
    else:
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        all_chapter_texts = []
        temp_chapter_audio_files = []
        current_url = start_url
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        
        total_steps = (end_ep - start_ep) + 1
        success_count = 0
        start_time = time.time()
        
        for i in range(total_steps):
            actual_ep_num = start_ep + i
            
            # คำนวณเปอร์เซ็นต์ความคืบหน้าอย่างละเอียด
            percent_val = int(((i) / total_steps) * 90)
            progress_bar.progress(max(percent_val, 1))
            
            # คำนวณเวลาที่ใช้ไป (Timer)
            elapsed_sec = time.time() - start_time
            minutes = int(elapsed_sec // 60)
            seconds = int(elapsed_sec % 60)
            time_str = f"{minutes} นาที {seconds} วินาที" if minutes > 0 else f"{seconds} วินาที"
            
            status_text.markdown(f"""
            ⚡ **กำลังประมวลผลตอนที่ {actual_ep_num}** ({i+1}/{total_steps} ตอน) — **คืบหน้า: {percent_val}%**<br>
            ⏱️ เวลาที่ใช้ไป: **{time_str}**
            """, unsafe_allow_html=True)
            
            try:
                # 1. ดึงเว็บ
                res = requests.get(current_url, headers=headers, timeout=8)
                if res.status_code != 200:
                    break
                    
                soup = BeautifulSoup(res.text, 'html.parser')
                paragraphs = soup.find_all('p')
                raw_content = "\n".join([p.get_text() for p in paragraphs])
                if len(raw_content.strip()) < 10:
                    raw_content = soup.get_text()

                # 2. แปลภาษา
                translated_content = fast_translate(raw_content, src_lang=src_lang, dest_lang='th')
                
                formatted_chapter_text = f"\n\n=== ตอนที่ {actual_ep_num} ===\n\n" + translated_content
                all_chapter_texts.append(formatted_chapter_text)
                st.session_state.novel_text = "".join(all_chapter_texts)

                # 3. แปลงเสียงเฉพาะตอนนี้แบบความเร็วสูง
                ch_audio_file = f"turbo_ch_{actual_ep_num}.mp3"
                temp_chapter_audio_files.append(ch_audio_file)
                
                asyncio.run(text_to_speech_fast(translated_content, voice_choice, ch_audio_file))
                success_count += 1
                
                # 4. หาลิงก์ตอนถัดไป
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

        # 5. รวมไฟล์เสียงทั้งหมดรวดเดียว
        if temp_chapter_audio_files:
            status_text.text("🔗 กำลังรวมไฟล์เสียงทุกตอนเข้าเป็นไฟล์หลัก...")
            progress_bar.progress(95)
            
            final_output_file = "premwadee_turbo_50_episodes.mp3"
            with open(final_output_file, "wb") as final_out:
                for caf in temp_chapter_audio_files:
                    if os.path.exists(caf) and os.path.getsize(caf) > 0:
                        with open(caf, "rb") as caf_in:
                            final_out.write(caf_in.read())
                        os.remove(caf)
            
            total_elapsed = time.time() - start_time
            tot_min = int(total_elapsed // 60)
            tot_sec = int(total_elapsed % 60)
            total_time_str = f"{tot_min} นาที {tot_sec} วินาที" if tot_min > 0 else f"{tot_sec} วินาที"
            
            progress_bar.progress(100)
            status_text.success(f"🎉 สำเร็จ! แปลงและรวมเสียง {success_count} ตอน เรียบร้อยในเวลา {total_time_str}!")
            
            if os.path.exists(final_output_file):
                with open(final_output_file, "rb") as f:
                    audio_bytes = f.read()
                
                st.audio(audio_bytes, format="audio/mp3")
                st.download_button(
                    label="📥 ดาวน์โหลดไฟล์ MP3 (Turbo 50 ตอน)",
                    data=audio_bytes,
                    file_name="novel_turbo_50_episodes.mp3",
                    mime="audio/mp3"
                )
        else:
            status_text.error("❌ ไม่สามารถสร้างไฟล์เสียงได้ กรุณาตรวจสอบลิงก์เริ่มต้นอีกครั้ง")
