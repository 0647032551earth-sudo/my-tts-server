"""
เซิร์ฟเวอร์ TTS ส่วนตัว (เสียงเปรมวดี) — ใช้ edge-tts, ไม่ต้องมี API key
รับคำขอรูปแบบเดียวกับที่หน้าเว็บ V67 ใช้อยู่:
  GET  /api/voices              -> รายการเสียง
  POST /api/tts  {voiceIndex,text,pitch,rate}  -> ไฟล์เสียง audio/mpeg
"""
import asyncio, json, os, threading, time
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

MAX_TEXT = 5000          # ตัวอักษรต่อคำขอ (หน้าเว็บส่งไม่เกิน ~1950)
MAX_PARALLEL = 6         # จำกัดจำนวนงานพร้อมกันฝั่งเซิร์ฟเวอร์
TRIES = 3                # ลองซ้ำเองเมื่อ Microsoft ตอบผิดพลาดชั่วคราว
gate = threading.BoundedSemaphore(MAX_PARALLEL)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def to_rate(x):   # สไลเดอร์ -100..100 -> เปอร์เซ็นต์ความเร็ว
    return "%+d%%" % clamp(int(float(x or 0)), -90, 100)


def to_pitch(x):  # สไลเดอร์ -100..100 -> เฮิรตซ์ (-50..+50)
    return "%+dHz" % clamp(int(float(x or 0) / 2), -50, 50)


async def synth(text, voice, rate, pitch):
    comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
    out = bytearray()
    async for chunk in comm.stream():
        if chunk.get("type") == "audio":
            out += chunk["data"]
    if not out:
        raise RuntimeError("no audio received")
    return bytes(out)


def synth_with_retry(text, voice, rate, pitch):
    last = None
    for k in range(1, TRIES + 1):
        try:
            return asyncio.run(asyncio.wait_for(synth(text, voice, rate, pitch), timeout=45))
        except Exception as e:  # noqa
            last = e
            time.sleep(0.6 * k)
    raise last


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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
        with gate:
            try:
                audio = synth_with_retry(text, voice, rate, pitch)
            except Exception as e:  # noqa
                return self._send(502, {"error": "Failed to generate speech.", "detail": str(e)[:300]})
        self._send(200, audio, "audio/mpeg")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.daemon_threads = True
    print("TTS server listening on port", port, flush=True)
    srv.serve_forever()
