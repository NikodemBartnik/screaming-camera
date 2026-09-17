"""Text-to-speech -> WAV bytes (16-bit PCM mono). Piper (offline, fast, runs on ARM64) by default,
pyttsx3 (system voices: SAPI on Windows, espeak on Linux) as a zero-download fallback."""
from __future__ import annotations

import asyncio
import io
import logging
import wave
from pathlib import Path

from .config import TTSConfig

log = logging.getLogger(__name__)


class TTS:
    def __init__(self, cfg: TTSConfig):
        self.cfg = cfg
        self._piper = None
        self._piper_key = None
        self.status = "idle"
        self.last_error = ""

    def update(self, cfg: TTSConfig) -> None:
        self.cfg = cfg

    # ---- piper -----------------------------------------------------------------------------
    def _load_piper(self):
        key = (self.cfg.piper_model, self.cfg.piper_data_dir)
        if self._piper is not None and self._piper_key == key:
            return self._piper
        from piper import PiperVoice

        model = self.cfg.piper_model
        path = Path(model)
        if not path.suffix == ".onnx" or not path.exists():
            data_dir = Path(self.cfg.piper_data_dir)
            data_dir.mkdir(parents=True, exist_ok=True)
            path = data_dir / f"{model}.onnx"
            if not path.exists():
                from piper.download_voices import download_voice

                log.info("downloading piper voice %s to %s", model, data_dir)
                download_voice(model, data_dir)
        self._piper = PiperVoice.load(path)
        self._piper_key = key
        return self._piper

    def _piper_wav(self, text: str) -> bytes:
        from piper import SynthesisConfig

        voice = self._load_piper()
        syn = SynthesisConfig(length_scale=1.0 / max(self.cfg.speed, 0.1))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            voice.synthesize_wav(text, w, syn_config=syn)
        return buf.getvalue()

    # ---- pyttsx3 ---------------------------------------------------------------------------
    def _pyttsx3_wav(self, text: str) -> bytes:
        import tempfile

        import pyttsx3

        engine = pyttsx3.init()
        if self.cfg.pyttsx3_voice:
            for v in engine.getProperty("voices"):
                if self.cfg.pyttsx3_voice.lower() in (v.name or "").lower():
                    engine.setProperty("voice", v.id)
                    break
        engine.setProperty("rate", int(engine.getProperty("rate") * self.cfg.speed))
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "tts.wav"
            engine.save_to_file(text, str(out))
            engine.runAndWait()
            return out.read_bytes()

    # ---- public ----------------------------------------------------------------------------
    def synthesize_sync(self, text: str) -> bytes:
        if self.cfg.engine == "none":
            raise RuntimeError("TTS disabled")
        try:
            wav = self._piper_wav(text) if self.cfg.engine == "piper" else self._pyttsx3_wav(text)
            self.status = "ok"
            self.last_error = ""
            return wav
        except Exception as e:  # noqa: BLE001
            self.status = "error"
            self.last_error = str(e)
            raise

    async def synthesize(self, text: str) -> bytes:
        return await asyncio.to_thread(self.synthesize_sync, text)


def wav_info(wav: bytes) -> tuple[int, int, int]:
    """(sample_rate, channels, sample_width_bytes)"""
    with wave.open(io.BytesIO(wav), "rb") as w:
        return w.getframerate(), w.getnchannels(), w.getsampwidth()
