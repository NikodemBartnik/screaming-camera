"""VLM analyzer: frame(s) + user-configured prompt -> structured verdict.

Talks to any OpenAI-compatible chat endpoint that accepts image_url content parts:
GenieX on the Qualcomm board, llama-server / Ollama / LM Studio on a PC. The model is asked for
JSON; parsing is deliberately forgiving because small local models like to add prose.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from typing import Any

import cv2
import httpx
import numpy as np
from pydantic import BaseModel, Field

from .config import ModelConfig, PromptConfig

log = logging.getLogger(__name__)


class Person(BaseModel):
    clothing: str = ""
    action: str = ""
    carrying: str = ""


class Analysis(BaseModel):
    threat_level: int = 0
    scene: str = ""
    people: list[Person] = Field(default_factory=list)
    reasoning: str = ""
    message: str = ""
    raw: str = ""
    latency_ms: int = 0
    model: str = ""
    error: str = ""
    stage: int = 0  # 0 = single call, 1 = classified only, 2 = classified + described


# Every output token costs ~75 ms on the board's NPU, so both schemas are as terse as possible and the
# model is told to minify: no pretty-printing, no prose, no reasoning field.
# "message" comes right after the verdict: it is the field that matters and the model commits to it
# before spending tokens on descriptions.
SCHEMA_HINT = (
    'Respond with ONLY a minified JSON object on one line, no markdown, no spaces or newlines, exactly: '
    '{"threat_level":<integer 0-10, 0 = harmless, 10 = crime in progress>,'
    '"message":"<if threat_level>=5: what to say through the loudspeaker to the person, else empty string>",'
    '"people":[{"clothing":"<colours and garments>","action":"<what they do>","carrying":"<object or empty>"}],'
    '"scene":"<max 8 words>"}'
)

CLASSIFY_HINT = (
    'Respond with ONLY a minified JSON object on one line, no markdown, no spaces or newlines, exactly: '
    '{"threat_level":<integer 0-10, 0 = harmless, 10 = crime in progress>,"scene":"<max 8 words>"}'
)


def _context(p: PromptConfig) -> list[str]:
    return [
        f"WATCH FOR (suspicious, raise threat_level): {p.watch_for.strip()}",
        f"IGNORE (harmless, keep threat_level low): {p.ignore.strip()}",
    ]


def build_classify_prompt(p: PromptConfig) -> str:
    """Stage 1: cheap verdict (~15 output tokens)."""
    parts = ["You are the AI security guard of a private house watching a camera frame."]
    parts += _context(p)
    if p.extra_instructions.strip():
        parts.append(f"ADDITIONAL INSTRUCTIONS: {p.extra_instructions.strip()}")
    parts.append(CLASSIFY_HINT)
    return "\n".join(parts)


def build_system_prompt(p: PromptConfig) -> str:
    """Stage 2 (or single-stage): description + loudspeaker message."""
    parts = [p.persona.strip()]
    parts += _context(p)
    parts.append(
        f"MESSAGE STYLE: {p.message_style.strip()} Maximum {p.max_message_words} words. "
        f"Write the message in {p.language}."
    )
    if p.extra_instructions.strip():
        parts.append(f"ADDITIONAL INSTRUCTIONS: {p.extra_instructions.strip()}")
    parts.append(SCHEMA_HINT)
    return "\n".join(parts)


def encode_image(img: np.ndarray, max_side: int, quality: int) -> str:
    h, w = img.shape[:2]
    scale = max_side / max(h, w)
    if scale < 1.0:
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RuntimeError("jpeg encode failed")
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii")


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_analysis(text: str) -> Analysis:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE | re.MULTILINE).strip()
    candidate = cleaned
    m = _JSON_RE.search(cleaned)
    if m:
        candidate = m.group(0)
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        # last resort: trailing commas / single quotes
        fixed = re.sub(r",\s*([}\]])", r"\1", candidate).replace("'", '"')
        try:
            data = json.loads(fixed)
        except json.JSONDecodeError:
            return Analysis(raw=text, error="model did not return JSON", scene=cleaned[:200])
    if not isinstance(data, dict):
        return Analysis(raw=text, error="JSON is not an object")
    people = []
    for item in data.get("people") or []:
        if isinstance(item, dict):
            people.append(Person(**{k: str(v) for k, v in item.items() if k in Person.model_fields}))
        elif isinstance(item, str):
            people.append(Person(action=item))
    try:
        threat = int(round(float(data.get("threat_level", 0))))
    except (TypeError, ValueError):
        threat = 0
    msg = data.get("message") or ""
    if not isinstance(msg, str):
        msg = str(msg)
    return Analysis(
        threat_level=max(0, min(10, threat)),
        scene=str(data.get("scene", "")),
        people=people,
        reasoning=str(data.get("reasoning", "")),
        message=msg.strip(),
        raw=text,
    )


class Analyzer:
    def __init__(self, model: ModelConfig, prompt: PromptConfig):
        self.model = model
        self.prompt = prompt
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(model.timeout_seconds, connect=10))
        self.status = "unknown"
        self.last_error = ""

    def update(self, model: ModelConfig, prompt: PromptConfig) -> None:
        if model.timeout_seconds != self.model.timeout_seconds:
            self.client = httpx.AsyncClient(timeout=httpx.Timeout(model.timeout_seconds, connect=10))
        self.model, self.prompt = model, prompt

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.model.api_key:
            h["Authorization"] = f"Bearer {self.model.api_key}"
        return h

    @property
    def _base(self) -> str:
        return self.model.endpoint.rstrip("/")

    async def health(self) -> dict[str, Any]:
        try:
            if self.model.api == "ollama":
                r = await self.client.get(f"{self._base}/api/tags", headers=self._headers(), timeout=5)
                r.raise_for_status()
                ids = [m.get("name") for m in r.json().get("models", [])]
            else:
                r = await self.client.get(f"{self._base}/models", headers=self._headers(), timeout=5)
                r.raise_for_status()
                ids = [m.get("id") for m in r.json().get("data", [])]
            self.status = "ok"
            return {"ok": True, "models": ids}
        except Exception as e:  # noqa: BLE001
            self.status = "unreachable"
            self.last_error = str(e)
            return {"ok": False, "error": str(e)}

    def _request(self, images: list[np.ndarray], user_text: str, system: str,
                 max_tokens: int | None = None) -> tuple[str, dict[str, Any]]:
        """(url, json payload) for the configured API flavour."""
        max_tokens = max_tokens or self.model.max_tokens
        encoded = [encode_image(img, self.model.max_image_side, self.model.jpeg_quality) for img in images]
        if self.model.api == "ollama":
            payload = {
                "model": self.model.name,
                "stream": False,
                "think": False,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_text, "images": [e.split(",", 1)[1] for e in encoded]},
                ],
                "options": {"temperature": self.model.temperature, "num_predict": max_tokens},
            }
            return f"{self._base}/api/chat", payload
        content: list[dict[str, Any]] = [{"type": "image_url", "image_url": {"url": e}} for e in encoded]
        content.append({"type": "text", "text": user_text})
        payload = {
            "model": self.model.name,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "temperature": self.model.temperature,
            "max_tokens": max_tokens,
            "stream": False,
            **(self.model.extra_body or {}),
        }
        return f"{self._base}/chat/completions", payload

    async def _call(self, images: list[np.ndarray], user_text: str, system: str, max_tokens: int | None) -> Analysis:
        url, payload = self._request(images, user_text, system, max_tokens)
        t0 = time.perf_counter()
        try:
            r = await self.client.post(url, headers=self._headers(), json=payload)
            r.raise_for_status()
            data = r.json()
            if self.model.api == "ollama":
                text = data["message"]["content"] or ""
            else:
                text = data["choices"][0]["message"]["content"] or ""
            self.status = "ok"
            self.last_error = ""
        except Exception as e:  # noqa: BLE001
            self.status = "error"
            self.last_error = str(e)
            log.warning("VLM request failed: %s", e)
            return Analysis(error=str(e), latency_ms=int((time.perf_counter() - t0) * 1000), model=self.model.name)
        analysis = parse_analysis(text)
        analysis.latency_ms = int((time.perf_counter() - t0) * 1000)
        analysis.model = self.model.name
        return analysis

    async def analyze(self, images: list[np.ndarray], camera_name: str, trigger: str,
                      context: str = "") -> Analysis:
        when = time.strftime("%A %H:%M")
        user_text = (
            f"Camera: {camera_name}. Trigger: {trigger}. Local time: {when}. "
            + (f"{context} " if context else "")
            + (f"You get {len(images)} consecutive frames, oldest first. " if len(images) > 1 else "")
            + "Analyse the frame(s) and answer with the JSON object."
        )
        if not self.model.two_stage:
            return await self._call(images, user_text, build_system_prompt(self.prompt), None)

        # Stage 1: verdict only (~15 tokens). Stage 2 only when it is worth 4-5 s of generation.
        first = await self._call(images, user_text, build_classify_prompt(self.prompt), 40)
        if first.error or first.threat_level < self.model.describe_min_threat:
            first.stage = 1
            return first
        second = await self._call(images, user_text, build_system_prompt(self.prompt), None)
        if second.error:
            first.stage = 1
            first.latency_ms += second.latency_ms
            return first
        second.stage = 2
        second.scene = second.scene or first.scene
        second.latency_ms += first.latency_ms
        second.raw = first.raw + "\n---\n" + second.raw
        return second
