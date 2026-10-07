#!/usr/bin/env python3
# Fast TTS backend for the existing TTS web app.
# API-compatible: GET /api/voices, POST /api/tts
# Uses Microsoft Edge TTS via edge-tts; no API key is required.

import asyncio
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import edge_tts

VOICES = [
    {
        "index": 0,
        "name": "Premwadee",
        "shortName": "th-TH-PremwadeeNeural",
        "raw": {
            "ShortName": "th-TH-PremwadeeNeural",
            "FriendlyName": "Microsoft Premwadee Online (Natural) - Thai (Thailand)",
            "Locale": "th-TH",
        },
    },
    {
        "index": 1,
        "name": "Niwat",
        "shortName": "th-TH-NiwatNeural",
        "raw": {
            "ShortName": "th-TH-NiwatNeural",
            "FriendlyName": "Microsoft Niwat Online (Natural) - Thai (Thailand)",
            "Locale": "th-TH",
        },
    },
]

# Keep the fast path close to the original server.py: one upstream synthesis
# request for each text chunk sent by the browser.
MAX_TEXT = 5000
MAX_PARALLEL_REQUESTS = 6
REQUEST_TIMEOUT = 16.0
ATTEMPT_TIMEOUT = 13.5
RETRIES = 2
RETRY_DELAYS = (0.35, 0.8)

# Only used as a recovery path if Microsoft rejects a normal-sized request.
FALLBACK_PIECE_SIZE = 700
gate = threading.BoundedSemaphore(MAX_PARALLEL_REQUESTS)

_CONTROL_CHARS = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200d\u2060\ufeff\ufffd]"
)
_SILENT_FRAME = bytes([0xFF, 0xF3, 0x64, 0xC0]) + bytes(140)
SILENT_MP3 = _SILENT_FRAME * 21


def clamp(value, low, high):
    return max(low, min(high, value))


def to_rate(value):
    return "%+d%%" % clamp(int(float(value or 0)), -90, 100)


def to_pitch(value):
    return "%+dHz" % clamp(int(float(value or 0) / 2), -50, 50)


def clean_text(text):
    text = _CONTROL_CHARS.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def is_speakable(text):
    return re.search(r"\w", text, flags=re.UNICODE) is not None


def split_fallback(text, limit=FALLBACK_PIECE_SIZE):
    """Split only on the recovery path; prefer whitespace boundaries."""
    atoms = re.findall(r"\S+\s*", text)
    pieces, current = [], ""
    for atom in atoms:
        while len(atom) > limit:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(atom[:limit])
            atom = atom[limit:]
        if current and len(current) + len(atom) > limit:
            pieces.append(current)
            current = ""
        current += atom
    if current:
        pieces.append(current)
    return [piece.strip() for piece in pieces if piece.strip()]


async def synth_once(text, voice, rate, pitch):
    communicator = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
    audio = bytearray()
    async for chunk in communicator.stream():
        if chunk.get("type") == "audio":
            audio.extend(chunk["data"])
    if not audio:
        raise RuntimeError("No audio was received from Microsoft Edge TTS")
    return bytes(audio)


async def synth_with_retry(text, voice, rate, pitch, deadline):
    last_error = None
    for attempt in range(RETRIES):
        remaining = deadline - time.monotonic()
        if remaining <= 0.5:
            break
        try:
            return await asyncio.wait_for(
                synth_once(text, voice, rate, pitch),
                timeout=min(ATTEMPT_TIMEOUT, remaining),
            )
        except Exception as exc:
            last_error = exc
            remaining = deadline - time.monotonic()
            if attempt + 1 < RETRIES and remaining > RETRY_DELAYS[attempt] + 1.0:
                await asyncio.sleep(RETRY_DELAYS[attempt])
    raise last_error or TimeoutError("TTS request timed out")


async def build_audio_async(text, voice, rate, pitch, deadline):
    # Fast path: one Microsoft request for the whole browser chunk.
    try:
        return await synth_with_retry(text, voice, rate, pitch, deadline)
    except Exception as first_error:
        # Recovery path: smaller pieces can work around intermittent upstream
        # failures. This is deliberately not used on successful requests.
        pieces = split_fallback(text)
        if len(pieces) <= 1:
            raise first_error

        output = bytearray()
        for piece in pieces:
            if time.monotonic() >= deadline - 0.5:
                raise TimeoutError("TTS request budget exceeded")
            if is_speakable(piece):
                output.extend(await synth_with_retry(piece, voice, rate, pitch, deadline))
        if not output:
            return SILENT_MP3
        return bytes(output)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")

    def _send(self, status, body, content_type="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # The browser may have timed out or cancelled the request.
            pass

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/healthz"):
            return self._send(200, {"ok": True, "service": "server-fast"})
        if path == "/api/voices":
            return self._send(200, VOICES)
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/api/tts":
            return self._send(404, {"error": "not found"})

        started = time.monotonic()
        deadline = started + REQUEST_TIMEOUT

        try:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0 or length > 2_000_000:
                return self._send(400, {"error": "invalid request body"})
            data = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._send(400, {"error": "invalid JSON"})

        text = clean_text(str(data.get("text") or ""))
        if not text:
            return self._send(400, {"error": "text is empty"})
        if len(text) > MAX_TEXT:
            return self._send(413, {"error": "text too long (max %d)" % MAX_TEXT})
        if not is_speakable(text):
            return self._send(200, SILENT_MP3, "audio/mpeg")

        try:
            voice_index = int(data.get("voiceIndex", 0))
        except (TypeError, ValueError):
            voice_index = 0
        voice = (
            VOICES[voice_index]["shortName"]
            if 0 <= voice_index < len(VOICES)
            else VOICES[0]["shortName"]
        )

        try:
            rate = to_rate(data.get("rate"))
            pitch = to_pitch(data.get("pitch"))
        except (TypeError, ValueError, OverflowError):
            return self._send(400, {"error": "invalid pitch/rate"})

        wait_for_slot = max(0.0, deadline - time.monotonic() - 0.5)
        if not gate.acquire(timeout=wait_for_slot):
            return self._send(
                503,
                {"error": "server busy, retry"},
                extra={"Retry-After": "2"},
            )

        try:
            audio = asyncio.run(build_audio_async(text, voice, rate, pitch, deadline))
        except Exception as exc:
            detail = (str(exc) or type(exc).__name__)[:300]
            print("TTS failed (%d chars): %s" % (len(text), detail), flush=True)
            return self._send(502, {"error": "Failed to generate speech.", "detail": detail})
        finally:
            gate.release()

        elapsed = time.monotonic() - started
        print(
            "TTS ok: chars=%d bytes=%d seconds=%.2f voice=%s"
            % (len(text), len(audio), elapsed, voice),
            flush=True,
        )
        self._send(200, audio, "audio/mpeg")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    print("server-fast listening on port %d (max parallel requests: %d)" %
          (port, MAX_PARALLEL_REQUESTS), flush=True)
    server.serve_forever()
