"""Speak through a Tapo camera's own speaker.

Tapo has no ONVIF audio backchannel; two-way audio uses TP-Link's own protocol on port 8800:

    POST /stream (multipart/mixed, HTTP Digest) -> open a "talk" session -> session_id
    -> G.711 A-law, 8 kHz mono, wrapped in MPEG-TS with TP-Link's private stream type 0x90,
       pushed as further multipart parts carrying X-Session-Id.

Credentials are the **TP-Link cloud account** password (not the RTSP camera account): the camera
checks it locally as MD5/SHA-256 of that password, uppercase hex, with username "admin".
Protocol mapped from go2rtc's pkg/tapo and pkg/mpegts implementations.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import socket
import wave

import numpy as np

from .base import Speaker

log = logging.getLogger(__name__)

PORT = 8800
CLIENT_BOUNDARY = b"----client-stream-boundary--"
DEVICE_BOUNDARY = b"--device-stream-boundary--"
TALK_REQUEST = b'{"params":{"talk":{"mode":"aec"},"method":"get"},"seq":3,"type":"request"}'
SESSION_RE = re.compile(r'"session_id"\s*:\s*"([^"]+)"')

RATE = 8000               # G.711 is always 8 kHz
SAMPLES_PER_CHUNK = 480   # 60 ms per PES packet
CHUNK_SECONDS = SAMPLES_PER_CHUNK / RATE
PCMA_STREAM_TYPE = 0x90   # TP-Link private stream type for G.711 A-law
TS_PACKET = 188
PAT_PID, PMT_PID, PES_PID = 0x0000, 0x1000, 0x0100
PES_STREAM_ID = 0xC0      # audio stream


# ---------------------------------------------------------------- G.711 A-law
_ALAW_SEG_ENDS = np.array([0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF], dtype=np.int32)


def linear_to_alaw(pcm: np.ndarray) -> bytes:
    """Vectorised ITU-T G.711 A-law encoder (int16 PCM -> one byte per sample)."""
    x = (pcm.astype(np.int32)) >> 3  # 13-bit magnitude domain
    negative = x < 0
    mask = np.where(negative, 0x55, 0xD5).astype(np.int32)
    x = np.where(negative, -x - 1, x)
    x = np.clip(x, 0, 0xFFF)
    seg = np.searchsorted(_ALAW_SEG_ENDS, x, side="left").astype(np.int32)
    shift = np.where(seg < 2, 1, np.maximum(seg, 1))
    aval = (seg << 4) | ((x >> shift) & 0x0F)
    return bytes((aval ^ mask).astype(np.uint8))


def wav_to_pcma(wav: bytes, volume: float = 1.0) -> bytes:
    """WAV (any rate, mono/stereo) -> G.711 A-law at 8 kHz mono."""
    import av

    with wave.open(io.BytesIO(wav), "rb") as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError("expected 16-bit PCM wav")
    pcm = np.frombuffer(raw, dtype=np.int16).reshape(-1, channels)
    if channels > 1:
        pcm = pcm.mean(axis=1).astype(np.int16).reshape(-1, 1)
    if volume != 1.0:
        pcm = np.clip(pcm.astype(np.float32) * volume, -32768, 32767).astype(np.int16)

    frame = av.AudioFrame.from_ndarray(pcm.T.copy(), format="s16", layout="mono")
    frame.sample_rate = rate
    resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
    chunks = [f.to_ndarray().reshape(-1) for f in resampler.resample(frame) + resampler.resample(None)]
    return linear_to_alaw(np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int16))


# ---------------------------------------------------------------- minimal MPEG-TS muxer
def _crc32_mpeg(data: bytes) -> int:
    """CRC-32/MPEG-2, as used by MPEG-TS PSI sections."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


def _psi_packet(pid: int, table_id: int, body: bytes) -> bytes:
    """One 188-byte TS packet carrying a complete PSI section."""
    section_len = len(body) + 5 + 4  # 5 bytes of header below + body + CRC
    section = bytes([table_id, 0xB0 | (section_len >> 8) & 0x0F, section_len & 0xFF,
                     0x00, 0x01,  # table id extension
                     0xC1,        # reserved + version 0 + current
                     0x00, 0x00]) + body
    section += _crc32_mpeg(section).to_bytes(4, "big")
    payload = b"\x00" + section  # pointer field
    header = bytes([0x47, 0x40 | (pid >> 8), pid & 0xFF, 0x10])
    return (header + payload).ljust(TS_PACKET, b"\xff")


def ts_header() -> bytes:
    """PAT + PMT announcing a single A-law audio track."""
    pat = _psi_packet(PAT_PID, 0x00, bytes([0x00, 0x01, 0xE0 | (PMT_PID >> 8), PMT_PID & 0xFF]))
    pmt_body = bytes([0xE0 | (PES_PID >> 8), PES_PID & 0xFF,  # PCR PID
                      0xF0, 0x00,                             # program info length
                      PCMA_STREAM_TYPE, 0xE0 | (PES_PID >> 8), PES_PID & 0xFF, 0xF0, 0x00])
    return pat + _psi_packet(PMT_PID, 0x02, pmt_body)


def _pts_bytes(pts: int) -> bytes:
    return bytes([0x20 | ((pts >> 29) & 0x0E) | 1,
                  (pts >> 22) & 0xFF,
                  ((pts >> 14) & 0xFE) | 1,
                  (pts >> 7) & 0xFF,
                  ((pts << 1) & 0xFE) | 1])


def ts_payload(payload: bytes, pts: int, counter: int) -> tuple[bytes, int]:
    """Wrap one audio chunk in a PES packet split across 188-byte TS packets."""
    pes = (b"\x00\x00\x01" + bytes([PES_STREAM_ID])
           + (3 + 5 + len(payload)).to_bytes(2, "big")
           + b"\x80\x80\x05" + _pts_bytes(pts) + payload)
    out = bytearray()
    first = True
    while pes:
        pid = PES_PID | (0x4000 if first else 0)  # payload unit start indicator
        if len(pes) < TS_PACKET - 4:              # last packet: pad with an adaptation field
            stuffing = TS_PACKET - 4 - 1 - len(pes)
            out += bytes([0x47, pid >> 8, pid & 0xFF, 0x30 | (counter & 0x0F), stuffing])
            out += bytes(stuffing) + pes
            pes = b""
        else:
            out += bytes([0x47, pid >> 8, pid & 0xFF, 0x10 | (counter & 0x0F)]) + pes[:TS_PACKET - 4]
            pes = pes[TS_PACKET - 4:]
        counter += 1
        first = False
    return bytes(out), counter


# ---------------------------------------------------------------- protocol
class TapoTalkError(RuntimeError):
    pass


def _digest_header(auth: str, username: str, password: str, method: str, uri: str) -> str:
    def between(text: str, start: str, end: str) -> str:
        i = text.find(start)
        if i < 0:
            return ""
        i += len(start)
        j = text.find(end, i)
        return text[i:j] if j > 0 else ""

    def h(*parts: str) -> str:
        return hashlib.md5(":".join(parts).encode()).hexdigest()

    realm, nonce, qop = between(auth, 'realm="', '"'), between(auth, 'nonce="', '"'), between(auth, 'qop="', '"')
    nc, cnonce = "00000001", os.urandom(16).hex()
    response = h(h(username, realm, password), nonce, nc, cnonce, qop or "auth", h(method, uri))
    header = (f'Digest username="{username}", realm="{realm}", nonce="{nonce}", uri="{uri}", '
              f'qop={qop or "auth"}, nc={nc}, cnonce="{cnonce}", response="{response}"')
    if opaque := between(auth, 'opaque="', '"'):
        header += f', opaque="{opaque}", algorithm=MD5'
    return header


def _read_headers(sock: socket.socket, buf: bytearray) -> tuple[str, bytearray]:
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise TapoTalkError("camera closed the connection")
        buf += chunk
    head, _, rest = bytes(buf).partition(b"\r\n\r\n")
    return head.decode("utf-8", "replace"), bytearray(rest)


class TapoTalkSession:
    """One talk session: connect, authenticate, open the session, push A-law audio."""

    def __init__(self, host: str, cloud_password: str, timeout: float = 10.0):
        self.host, self.cloud_password, self.timeout = host, cloud_password, timeout
        self.sock: socket.socket | None = None
        self.session_id = ""
        self._counter = 0
        self._pts = 0

    def _request_bytes(self, auth_header: str = "") -> bytes:
        lines = ["POST /stream HTTP/1.1", f"Host: {self.host}:{PORT}",
                 "Content-Type: multipart/mixed; boundary=--client-stream-boundary--"]
        if auth_header:
            lines.append(f"Authorization: {auth_header}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode()

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, PORT), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        self.sock.sendall(self._request_bytes())
        head, buf = _read_headers(self.sock, bytearray())
        if "401" not in head.split("\r\n")[0]:
            raise TapoTalkError(f"expected 401 challenge, got: {head.splitlines()[0]}")
        auth = next((l.split(":", 1)[1].strip() for l in head.split("\r\n")
                     if l.lower().startswith("www-authenticate")), "")
        if "digest" not in auth.lower():
            raise TapoTalkError("camera did not offer Digest auth")
        # The camera stores a hash of the cloud password; SHA-256 on newer firmware, MD5 otherwise.
        digest = (hashlib.sha256 if 'encrypt_type="3"' in auth else hashlib.md5)
        password = digest(self.cloud_password.encode()).hexdigest().upper()
        self.sock.sendall(self._request_bytes(_digest_header(auth, "admin", password, "POST", "/stream")))
        head, buf = _read_headers(self.sock, buf)
        status = head.splitlines()[0]
        if " 200" not in status:
            raise TapoTalkError(f"authentication failed ({status.strip()}) - is this the TP-Link *cloud* password?")

        # Open the talk session and read the session id from the camera's multipart reply.
        part = (CLIENT_BOUNDARY + b"\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(TALK_REQUEST)).encode() + b"\r\n\r\n" + TALK_REQUEST + b"\r\n")
        self.sock.sendall(part)
        self.session_id = self._read_session_id(buf)

    def _read_session_id(self, buf: bytearray) -> str:
        """Read multipart parts until the camera answers with the talk session id."""
        for _ in range(64):
            if match := SESSION_RE.search(bytes(buf).decode("utf-8", "replace")):
                return match.group(1)
            chunk = self.sock.recv(4096) if self.sock else b""
            if not chunk:
                break
            buf += chunk
        raise TapoTalkError(f"no session_id in the camera reply: {bytes(buf)[:200]!r}")

    def write(self, body: bytes) -> None:
        assert self.sock is not None
        head = (CLIENT_BOUNDARY + b"\r\nContent-Type: audio/mp2t\r\nX-If-Encrypt: 0\r\nX-Session-Id: "
                + self.session_id.encode() + b"\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n")
        self.sock.sendall(head + body)

    def send_header(self) -> None:
        self.write(ts_header())

    def send_audio(self, alaw: bytes) -> None:
        """Push one chunk; PTS advances on the 90 kHz clock."""
        body, self._counter = ts_payload(alaw, self._pts, self._counter)
        self._pts += int(len(alaw) * 90000 / RATE)
        self.write(body)

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None


class TapoTalkbackSpeaker(Speaker):
    def __init__(self, cfg):
        super().__init__(cfg)
        self._lock = asyncio.Lock()

    def _play_sync(self, alaw: bytes) -> None:
        import time
        session = TapoTalkSession(self.cfg.host or self.cfg.url, self.cfg.password)
        try:
            session.connect()
            session.send_header()
            step = SAMPLES_PER_CHUNK
            t0 = time.monotonic()
            for i in range(0, len(alaw), step):
                session.send_audio(alaw[i:i + step])
                delay = t0 + (i / step + 1) * CHUNK_SECONDS - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
            time.sleep(0.4)  # let the camera drain its buffer before we close the socket
        finally:
            session.close()

    async def play(self, wav: bytes) -> None:
        if not (self.cfg.host or self.cfg.url):
            raise ValueError(f"speaker {self.cfg.id}: set the camera's IP address")
        if not self.cfg.password:
            raise ValueError(f"speaker {self.cfg.id}: set the TP-Link cloud password")
        alaw = await asyncio.to_thread(wav_to_pcma, wav, self.cfg.volume)
        async with self._lock:
            try:
                await asyncio.to_thread(self._play_sync, alaw)
                self.status = "ok"
                self.last_error = ""
                log.info("tapo talkback %s: sent %.1fs of audio", self.cfg.id, len(alaw) / RATE)
            except Exception as e:  # noqa: BLE001
                self.status = "error"
                self.last_error = str(e)
                raise
