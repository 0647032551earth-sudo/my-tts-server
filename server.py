"""
เซิร์ฟเวอร์ TTS ส่วนตัว (เสียงเปรมวดี) — ใช้ edge-tts, ไม่ต้องมี API key
รับคำขอรูปแบบเดียวกับที่หน้าเว็บ V67 ใช้อยู่:
  GET  /api/voices              -> รายการเสียง
  POST /api/tts  {voiceIndex,text,pitch,rate}  -> ไฟล์เสียง audio/mpeg

เวอร์ชันนี้แก้เพื่อลด HTTP 502 ("No audio was received"):
  - ล้างอักขระแปลกๆ ออกจากข้อความก่อนส่งให้ Microsoft
  - ท่อนที่ไม่มีคำให้อ่าน (มีแต่สัญลักษณ์) -> ตอบเสียงเงียบสั้นๆ แทน error
  - แบ่งข้อความยาวเป็นท่อนย่อยฝั่งเซิร์ฟเวอร์ และลองซ้ำเฉพาะท่อนที่พลาด
  - จำกัดงานพร้อมกันที่ 3 ลดการถูก Microsoft ตัดการเชื่อมต่อ
  - มีเวลาจำกัดต่อคำขอ ถ้าเกินจะตอบ 5xx เร็วๆ ให้หน้าเว็บลองใหม่เอง
"""
import asyncio, json, os, random, re, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import edge_tts

VOICES = [
    {"index": 0, "name": "Premwadee", "shortName": "th-TH-PremwadeeNeural",
     "raw": {"ShortName": "th-TH-PremwadeeNeural",
             "FriendlyName": "Microsoft Premwadee Online (Natural) - Thai (Thailand)", "Locale": "th-TH"}},
    {"index": 1, "name": "Niwat", "shortName": "th-TH-NiwatNeural",
     "raw": {"ShortName": "th-TH-NiwatNeural",
             "FriendlyName": "Microsoft Niwat Online (Natural) - Thai (Thailand)", "Locale": "th-TH"}},
]

MAX_TEXT = 5000        # ตัวอักษรต่อคำขอ (หน้าเว็บส่งไม่เกิน ~1950)
MAX_PARALLEL = 3       # งานพร้อมกันฝั่งเซิร์ฟเวอร์ (เดิม 6) ลดการโดน Microsoft ตัด
PIECE_MAX = 500        # ขนาดท่อนย่อยที่ส่งให้ Microsoft ทีละครั้ง
TRIES = 4              # ลองซ้ำต่อท่อน
ATTEMPT_TIMEOUT = 25   # วินาทีต่อการลอง 1 ครั้ง
REQUEST_BUDGET = 36    # วินาทีรวมต่อคำขอ (หน้าเว็บตัดที่ 40 วินาที)
gate = threading.BoundedSemaphore(MAX_PARALLEL)

# เสียงเงียบ ~0.5 วินาที (MP3 MPEG-2 Layer III 24kHz mono 48kbps, 21 เฟรม)
_SILENT_FRAME = bytes([0xFF, 0xF3, 0x64, 0xC0]) + bytes(140)
SILENT_MP3 = _SILENT_FRAME * 21


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def to_rate(x):   # สไลเดอร์ -100..100 -> เปอร์เซ็นต์ความเร็ว
    return "%+d%%" % clamp(int(float(x or 0)), -90, 100)


def to_pitch(x):  # สไลเดอร์ -100..100 -> เฮิรตซ์ (-50..+50)
    return "%+dHz" % clamp(int(float(x or 0) / 2), -50, 50)


_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200d\u2060\ufeff\ufffd]")


def clean_text(text):
    text = _CTRL.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def speakable(text):
    """มีตัวอักษรหรือตัวเลขที่อ่านออกเสียงได้อย่างน้อย 1 ตัวหรือไม่"""
    return re.search(r"\w", text) is not None


def split_pieces(text, limit=PIECE_MAX):
    """แบ่งข้อความเป็นท่อนไม่เกิน limit โดยตัดที่ช่องว่าง/ขึ้นบรรทัดใหม่ก่อน"""
    atoms = re.findall(r"\S+\s*", text)
    pieces, cur = [], ""
    for a in atoms:
        while len(a) > limit:            # คำยาวไม่มีช่องว่าง (ภาษาไทยติดกัน) ตัดเป็นก้อน
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.append(a[:limit])
            a = a[limit:]
        if len(cur) + len(a) > limit and cur:
            pieces.append(cur)
            cur = ""
        cur += a
    if cur:
        pieces.append(cur)
    return [p.strip() for p in pieces if p.strip()]


async def synth(text, voice, rate, pitch):
    comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
    out = bytearray()
    async for chunk in comm.stream():
        if chunk.get("type") == "audio":
            out += chunk["data"]
    if not out:
        raise RuntimeError("no audio received")
    return bytes(out)


def synth_piece(text, voice, rate, pitch, deadline):
    last = None
    for k in range(1, TRIES + 1):
        remaining = deadline - time.time()
        if remaining < 3:
            break
        try:
            return asyncio.run(asyncio.wait_for(
                synth(text, voice, rate, pitch), timeout=min(ATTEMPT_TIMEOUT, remaining)))
        except Exception as e:  # noqa
            last = e
            print("piece fail try %d/%d (%d chars): %s" % (k, TRIES, len(text), str(e)[:120]), flush=True)
            time.sleep(min(6.0, 0.8 * (2 ** (k - 1))) + random.random() * 0.5)
    raise last or RuntimeError("time budget exceeded")


def build_audio(text, voice, rate, pitch, deadline):
    text = clean_text(text)
    if not speakable(text):
        return SILENT_MP3
    out = bytearray()
    for piece in split_pieces(text):
        if not speakable(piece):
            continue
        out += synth_piece(piece, voice, rate, pitch, deadline)
    return bytes(out) if out else SILENT_MP3


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *a):
        print("%s - %s" % (self.address_string(), fmt % a), flush=True)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/healthz"):
            return self._send(200, {"ok": True, "service": "my-tts-server"})
        if path == "/api/voices":
            return self._send(200, VOICES)
        self._send(404, {"error": "not found"})

    def do_POST(self):
        started = time.time()
        deadline = started + REQUEST_BUDGET
        if self.path.split("?")[0] != "/api/tts":
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "invalid JSON"})
        text = str(data.get("text") or "").strip()
        if not text:
            return self._send(400, {"error": "text is empty"})
        if len(text) > MAX_TEXT:
            return self._send(413, {"error": "text too long (max %d)" % MAX_TEXT})
        try:
            vi = int(data.get("voiceIndex", 0))
        except Exception:
            vi = 0
        voice = VOICES[vi]["shortName"] if 0 <= vi < len(VOICES) else VOICES[0]["shortName"]
        try:
            rate, pitch = to_rate(data.get("rate")), to_pitch(data.get("pitch"))
        except Exception:
            return self._send(400, {"error": "invalid pitch/rate"})

        if not gate.acquire(timeout=max(0.1, deadline - time.time() - 5)):
            return self._send(503, {"error": "server busy, retry"}, extra={"Retry-After": "3"})
        try:
            audio = build_audio(text, voice, rate, pitch, deadline)
        except Exception as e:  # noqa
            return self._send(502, {"error": "Failed to generate speech.", "detail": str(e)[:300]})
        finally:
            gate.release()
        self._send(200, audio, "audio/mpeg")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.daemon_threads = True
    print("TTS server listening on port", port, flush=True)
    srv.serve_forever()
