# Screaming Camera

Autonomous home monitoring that talks back. Cameras → local vision-language model → a spoken,
personalised warning ("Hey, you in the orange jacket pulling on my gate — you're on camera").

Runs on the **Qualcomm Dragonwing IQ‑9075 EVK** (Ubuntu 24.04, model on the NPU via GenieX) and on
**Windows** for development (model via Ollama / llama‑server) with the same code.

```
cameras (RTSP · Eufy P2P · webcam · files)
   └─► motion gate ─► VLM (OpenAI‑compatible HTTP) ─► policy (armed? threshold? cooldown?)
                                                        └─► Piper TTS ─► speakers (camera talkback · Bluetooth/USB · Wi‑Fi agent)
web panel: live view · verdicts · events log · every setting (prompts, cameras, speakers, model)
```

## Quick start (Windows)

```powershell
.\scripts\setup_windows.ps1        # venv, deps, Ollama model, config.yaml
.\.venv\Scripts\python -m screaming_camera
```
Open http://localhost:8080. Add cameras and speakers in **Settings**, click **ARM**.

## Quick start (IQ‑9075 / Ubuntu)

```bash
bash scripts/setup_iq9075.sh       # apt deps, venv, docker, systemd unit
# install GenieX, pull Gemma 4 E4B, `geniex serve` (port 18181) - see script output
sudo systemctl start screaming-camera
```

## Cameras

| Type | Use for | Notes |
|---|---|---|
| `rtsp` | Tapo, wired Eufy (S350, Indoor Cam), Reolink, anything with an RTSP URL | continuous; motion gate decides when to call the model |
| `eufy_p2p` | eufyCam 3, doorbells, any battery Eufy device | event‑driven via [eufy-security-ws](https://github.com/bropat/eufy-security-ws) (`docker compose up -d`), livestream only while an event is active |
| `webcam` | laptop camera for development | |
| `file` | video file or folder of images | reproducible prompt testing |

Eufy bridge: copy `.env.example` → `.env` with a **guest** Eufy account, `docker compose up -d`, watch
`docker compose logs -f` for 2FA/captcha. Then Settings → Eufy bridge → *List devices* to get serials.

## Speakers

| Type | Use for |
|---|---|
| `eufy_talkback` | the camera's own speaker (AAC over P2P talkback) |
| `local_audio` | any OS output device: USB, jack, **Bluetooth** (`keep_alive` stops BT speakers from dozing off) |
| `remote_agent` | `scripts/speaker_agent.py` on a Raspberry Pi / old phone with a speaker = DIY Wi‑Fi speaker |

Each camera lists which speakers it uses; one event can play on several at once.

## Model

Any endpoint that accepts `image_url` parts in `/v1/chat/completions`:

| Where | endpoint | model |
|---|---|---|
| IQ‑9075, GenieX (NPU) | `http://127.0.0.1:18181/v1` | `gemma-4-E4B-it` |
| Windows, Ollama | `http://127.0.0.1:11434/v1` | `ministral-3:8b`, `qwen3.8:27b` … (`gemma4:e4b` in Ollama 0.34 does **not** see images - known issue) |
| llama‑server | `http://127.0.0.1:8081/v1` | any GGUF + `--mmproj` |

`api: ollama` switches to Ollama's native API. `extra_body` passes provider extras (default
`{"reasoning_effort": "none"}` so thinking models don't burn the token budget).

Spike / benchmark without cameras: `python scripts/test_model.py samples/person.jpg -n 3`.

## Configuration

Everything lives in `config.yaml` (see `config.example.yaml`) and is editable from the panel: persona,
what to watch for, what to ignore, message style and language, threat threshold, cooldowns, arming
schedule, cameras, speakers, model, TTS. Saving from the panel hot‑applies; camera/speaker changes
restart just those components.

## Development

```
.venv\Scripts\python -m pytest         # unit + integration tests, no hardware needed
.venv\Scripts\python -m screaming_camera -v
```

Layout: `screaming_camera/` (`config` · `cameras/` · `gate` · `analyzer` · `policy` · `tts` ·
`speakers/` · `eufy/` · `store` · `pipeline` · `app` · `static/`), `scripts/`, `tests/`.
See `PLAN.md` for the design and the week plan.
