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


SCHEMA_HINT = """Respond with ONLY a JSON object, no markdown, in exactly this shape:
{
  "threat_level": <integer 0-10, 0 = nothing/harmless, 10 = crime in progress>,
  "scene": "<one sentence: what is happening>",
  "people": [{"clothing": "<colours and garments>", "action": "<what they are doing>", "carrying": "<objects or empty>"}],
  "reasoning": "<one short sentence why you chose this threat level>",
  "message": "<what to say through the loudspeaker, or empty string if nothing should be said>"
}"""


def build_system_prompt(p: PromptConfig) -> str:
    parts = [
        p.persona.strip(),
        f"\nWATCH FOR (suspicious, raise threat_level): {p.watch_for.strip()}",
        f"\nIGNORE (harmless, keep threat_level low, empty message): {p.ignore.strip()}",
        f"\nMESSAGE STYLE: {p.message_style.strip()} Maximum {p.max_message_words} words. "
        f"Write the message in {p.language}. Only write a message when threat_level is 5 or higher; "
        "otherwise leave it empty.",
    ]
    if p.extra_instructions.strip():
        parts.append(f"\nADDITIONAL INSTRUCTIONS: {p.extra_instructions.strip()}")
    parts.append("\n" + SCHEMA_HINT)
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

    def _request(self, images: list[np.ndarray], user_text: str) -> tuple[str, dict[str, Any]]:
        """(url, json payload) for the configured API flavour."""
        system = build_system_prompt(self.prompt)
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
                "options": {"temperature": self.model.temperature, "num_predict": self.model.max_tokens},
            }
            return f"{self._base}/api/chat", payload
        content: list[dict[str, Any]] = [{"type": "image_url", "image_url": {"url": e}} for e in encoded]
        content.append({"type": "text", "text": user_text})
        payload = {
            "model": self.model.name,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "temperature": self.model.temperature,
            "max_tokens": self.model.max_tokens,
            "stream": False,
            **(self.model.extra_body or {}),
        }
        return f"{self._base}/chat/completions", payload

    async def analyze(self, images: list[np.ndarray], camera_name: str, trigger: str,
                      context: str = "") -> Analysis:
        when = time.strftime("%A %H:%M")
        user_text = (
            f"Camera: {camera_name}. Trigger: {trigger}. Local time: {when}. "
            + (f"{context} " if context else "")
            + (f"You get {len(images)} consecutive frames, oldest first. " if len(images) > 1 else "")
            + "Analyse the frame(s) and answer with the JSON object."
        )
        url, payload = self._request(images, user_text)
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
