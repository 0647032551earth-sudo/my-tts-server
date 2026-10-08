# Novel Audio Factory V8.3 — HTML + Python backend (Android/Termux)

แพ็กเกจนี้แปลงหน้า Streamlit เดิมเป็นหน้า HTML ที่เรียก Python backend ผ่าน Flask API โดยคง logic หลักจากโค้ดต้นฉบับ เช่น ดึงลิงก์ตอน แปลภาษาไทย Edge TTS เสียง Premwadee การแบ่งชุด checkpoint/resume retry ตรวจ MP3 และสร้าง ZIP

## ใช้งานบน Android เครื่องเดียว (แนะนำ Termux)

### 1) ติดตั้ง Termux และเตรียมระบบ
- ติดตั้ง Termux จากแหล่งทางการที่โครงการ Termux แนะนำ (แนะนำ F-Droid หรือ GitHub ของ Termux; อย่าใช้ APK เก่าจากแหล่งสุ่ม)
- เปิด Termux แล้วรัน:

```bash
pkg update && pkg upgrade
pkg install python ffmpeg unzip
termux-setup-storage
```

เมื่อ Android ขอสิทธิ์เข้าถึงไฟล์ ให้กดอนุญาต

### 2) แตก ZIP ไปไว้ในพื้นที่ของ Termux

สมมติว่าไฟล์ ZIP อยู่ในโฟลเดอร์ Downloads ของมือถือ ให้รัน:

```bash
cp ~/storage/downloads/Novel_Audio_Factory_V8_3_Web.zip ~
cd ~
unzip Novel_Audio_Factory_V8_3_Web.zip
cd Novel_Audio_Factory_V8_3_Web
```

ถ้าไฟล์ชื่อหรืออยู่คนละโฟลเดอร์ ให้แก้ path ในคำสั่ง `cp` ให้ตรงกับไฟล์จริง

### 3) เริ่มระบบ

```bash
bash start_termux.sh
```

สคริปต์จะติดตั้ง Python dependencies และเริ่มเว็บเซิร์ฟเวอร์ จากนั้นเปิด Chrome บนมือถือไปที่:

**http://127.0.0.1:5000**

หน้าเว็บและ Python ทำงานบนมือถือเครื่องเดียวกัน ไม่ต้องใช้คอมพิวเตอร์หรืออินเทอร์เน็ตสำหรับการเชื่อมต่อหน้าเว็บกับ backend แต่ต้องมีอินเทอร์เน็ตเพื่อดึงนิยาย Google Translate และ Edge TTS

### 4) หยุดและเปิดใหม่
- หยุดระบบ: กลับไปที่ Termux แล้วกด `Ctrl+C`
- เปิดใหม่: เข้าโฟลเดอร์โปรเจกต์แล้วรัน `bash start_termux.sh`
- ไฟล์งาน/checkpoint อยู่ใน `novel_audio_jobs/` อย่าลบหากต้องการเก็บงานหรือ resume

## หมายเหตุสำหรับมือถือ
- Android อาจหยุดแอปเบื้องหลังเพราะระบบประหยัดแบตเตอรี่ ควรตั้งค่า Termux เป็น Unrestricted/ไม่จำกัดแบตเตอรี่ และอย่าปิด Termux ระหว่างทำงาน
- แนะนำให้เสียบชาร์จและระบายความร้อนเมื่อสร้าง MP3 จำนวนมาก
- ค่าเริ่มต้นบน Android ลด worker ลง (fetch=3, translate=2, TTS=1) เพื่อช่วยลด RAM/ความร้อน โดยยังปรับได้ผ่านตัวแปร `NOVEL_FETCH_WORKERS`, `NOVEL_TRANSLATE_WORKERS`, `NOVEL_TTS_WORKERS`
- อย่าเปิดเซิร์ฟเวอร์นี้รับจากอินเทอร์เน็ตสาธารณะ; ค่าเริ่มต้นรับเฉพาะ `127.0.0.1`
- ต้องใช้งานกับเว็บไซต์ที่มีสิทธิ์เข้าถึงและเคารพเงื่อนไขเว็บไซต์
- Google Translate endpoint ในโค้ดเดิมเป็น unofficial endpoint และอาจถูกจำกัด/เปลี่ยนแปลงได้

## ใช้บนคอมพิวเตอร์
ติดตั้ง Python 3.10+, FFmpeg และรัน `pip install -r requirements.txt` จากนั้น `python app.py` แล้วเปิด `http://127.0.0.1:5000`
