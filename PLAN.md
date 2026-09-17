# Screaming Camera — plan projektu

Autonomiczny system monitoringu: kamery → lokalny model wizyjno‑językowy (VLM) → spersonalizowany
komunikat głosowy do intruza. Działa na Qualcomm Dragonwing IQ‑9075 EVK (Ubuntu 24.04, NPU)
oraz na Windows (RTX 4090) z **identycznym kodem aplikacji**. Czas: ~1 tydzień do działającego demo.

> **Status (14.09.2026):** kod napisany i przetestowany na Windows: rdzeń pipeline'u, wszystkie typy kamer
> (rtsp / eufy_p2p / webcam / file), głośniki (local_audio / eufy_talkback / remote_agent), Piper TTS,
> panel WWW, 18 testów. **Do potwierdzenia na sprzęcie:** GenieX + Gemma 4 na IQ‑9075, eufy-security-ws
> z Twoimi kamerami (livestream P2P i talkback), RTSP z S350/Tapo, głośnik BT na Ubuntu. Szczegóły: README.md.

---

## 1. Decyzje architektoniczne (i dlaczego)

| # | Decyzja | Powód |
|---|---------|-------|
| 1 | Aplikacja rozmawia z modelem **tylko przez OpenAI‑compatible HTTP** (`/v1/chat/completions` z obrazem jako `data:image/jpeg;base64,...`). | Na IQ‑9075 serwerem jest **GenieX** (Qualcomm, NPU, obsługuje VLM z `image_url`), na Windows **llama‑server** (CUDA). Ten sam model (Gemma 4 E4B GGUF + mmproj), ten sam protokół, zero kodu zależnego od platformy. Fallback na płytce: llama‑server na CPU/OpenCL. |
| 2 | Dwa tryby źródła kamery: **ciągły** (RTSP/ONVIF) i **zdarzeniowy** (Eufy P2P — kamera budzi się na ruch). | Kamery bateryjne Eufy nie streamują 24/7. Kamery przewodowe (Tapo, Eufy wired, S350) — tak. Oba tryby za jednym interfejsem `CameraSource`. |
| 3 | Integracja Eufy przez **`eufy-security-ws`** (Node.js, Docker) po WebSocket. | Jedyna droga do: eventów ruchu z HomeBase, livestreamu P2P kamer bateryjnych i domofonu, **talkbacku (głośnik w kamerze)**. Brak dojrzałej alternatywy w Pythonie. |
| 4 | Głośniki jako wymienne **`Speaker`** backendy, przypisywane per kamera w konfiguracji. | Użytkownik ma wybrać w panelu: głośnik kamery Eufy / Bluetooth / zdalny agent / lokalny. |
| 5 | **Zero hardkodu** w promptach i regułach — wszystko w `config.yaml`, edytowalne z panelu. | Wymaganie: persona, styl wypowiedzi, co wykrywać, kiedy reagować — konfigurowalne. |
| 6 | Jeden proces Python (FastAPI + asyncio) + 2 sidecary (serwer modelu, eufy‑ws). SQLite na zdarzenia. Frontend: jeden plik HTML/JS bez build‑stepu. | Prostota; musi się postawić na ARM64 w kilka minut. |
| 7 | W pełni autonomiczny, bez zatwierdzania. Bezpiecznik = tryb **uzbrojony/rozbrojony** + cooldown + harmonogram. | Wymaganie użytkownika (film). |

---

## 2. Inwentarz sprzętu i jak go podpinamy

| Urządzenie | Zasilanie | Wideo | Zdarzenia | Głośnik | Backend |
|---|---|---|---|---|---|
| eufyCam 3 (+ HomeBase 3) | bateria, zewn. | P2P livestream na żądanie (H.264) | ruch/osoba z HomeBase | talkback ✔ | `eufy_p2p` |
| Domofon Eufy | bateria?, zewn. | P2P livestream | ruch / dzwonek | talkback ✔ | `eufy_p2p` |
| Eufy Indoor Cam S350 (obrotowa, 2 obiektywy) | sieć, wewn. | RTSP ciągły (włączyć w apce: Storage → NAS/RTSP) **lub** P2P | ruch | talkback ✔ (przez P2P) | `rtsp` lub `eufy_p2p` |
| Eufy wired (model do ustalenia) | sieć, wewn. | RTSP ciągły | ruch | talkback ✔ (P2P) | `rtsp` / `eufy_p2p` |
| Tapo | sieć, wewn. | RTSP ciągły: `rtsp://user:pass@IP/stream1` (konto kamery w apce Tapo), ONVIF Profile S port 2020 | ONVIF motion events (opcjonalnie) | ✘ (ONVIF S bez audio) | `rtsp` |
| Dowolna kamera IP | — | RTSP URL | (ONVIF opcjonalnie) | ✘ | `rtsp` |

Uniwersalność: każda kamera z RTSP URL działa od razu. ONVIF discovery + eventy ruchu — nice‑to‑have (`onvif-zeep`), nie w pierwszym tygodniu.

**Głośniki (do ustalenia w praktyce, w tej kolejności):**
1. `eufy_talkback` — głośnik w kamerze przez eufy‑ws. Zero dodatkowego sprzętu, najlepsze do filmu. Ryzyko: jakość/opóźnienie, niektóre modele kapryśne → sprawdzić w Fazie 0 na każdej kamerze.
2. `local_audio` — dowolne urządzenie audio systemu (USB, 3.5 mm, **Bluetooth** — po sparowaniu w Ubuntu głośnik BT jest zwykłym sinkiem PipeWire). Zasięg BT przez ścianę ~5–10 m; głośnik BT usypia się → opcja *keep‑alive* (cicha pętla) w konfiguracji.
3. `remote_agent` — mikroskrypt `speaker_agent.py` (HTTP `POST /play` z WAV) uruchamiany na czymkolwiek z głośnikiem w zasięgu Wi‑Fi (Raspberry Pi Zero/laptop/stary telefon z Termuxem) — de facto „głośnik Wi‑Fi” za darmo. Prosty i niezawodny; polecany jako główny zewnętrzny, jeśli talkback zawiedzie.
4. `chromecast` (`pychromecast`) — jeśli kiedyś pojawi się urządzenie Cast.

Jedno zdarzenie może grać na wielu głośnikach naraz (np. kamera + BT).

---

## 3. Architektura

```
┌────────────────────────── Panel WWW (LAN, przeglądarka) ───────────────────────────┐
│ kamery na żywo (MJPEG) · uzbrój/rozbrój · oś zdarzeń: klatka + co model zobaczył   │
│ + co powiedział · ustawienia: kamery, głośniki, model, prompty/persona, reguły     │
└────────────────────────────────────▲──────────────────────────────────────────────┘
                                     │ HTTP + WebSocket (FastAPI)
┌────────────────────────────────────┴──────────────────────────────────────────────┐
│ screaming_camera (Python 3.11+, asyncio)                                           │
│                                                                                    │
│  CameraSource ─► FrameGate ─► Analyzer ─► Policy ─► TTS ─► Speaker(s)              │
│  rtsp / eufy_p2p  ruch? osoba?  VLM → JSON   armed?      Piper   eufy_talkback     │
│  webcam / file    (tylko dla    {threat,     cooldown             local_audio      │
│                   ciągłych)     people[],    harmonogram          remote_agent     │
│                                 message}     reguły                                │
│  Config (YAML, hot‑reload) · EventStore (SQLite + JPEG) · Logger                   │
└──────────────┬───────────────────────────────┬────────────────────────────────────┘
               │ WebSocket                     │ HTTP OpenAI API
     ┌─────────┴─────────┐          ┌──────────┴──────────────────────────────┐
     │ eufy-security-ws  │          │ IQ‑9075: GenieX (NPU) · Win: llama‑server│
     │ (Node, Docker)    │          │ model: Gemma 4 E4B‑it GGUF + mmproj      │
     └───────────────────┘          └─────────────────────────────────────────┘
```

**Przepływ — kamera ciągła:** klatki z RTSP (PyAV/OpenCV, ~2–5 fps) → `FrameGate` (różnica klatek; opcjonalnie lekki detektor osób) → gdy ruch: najlepsza klatka (+ ewentualnie 2–3 klatki z ostatnich sekund) → VLM → JSON → jeśli `armed` i `threat >= próg` i cooldown minął → TTS → głośniki przypisane do kamery → zapis zdarzenia.

**Przepływ — kamera zdarzeniowa (Eufy):** event `motion/person/doorbell` z eufy‑ws → `start_livestream` → pierwsze dobre klatki (2–4 s) → dalej jak wyżej → `stop_livestream` po zakończeniu (lub po N s bez ruchu).

**Wyjście VLM (wymuszony JSON):**
```json
{
  "threat_level": 0-10,
  "scene": "krótki opis",
  "people": [{"clothing": "...", "action": "...", "carrying": "..."}],
  "reasoning": "dlaczego",
  "message": "co powiedzieć (albo pusty string)"
}
```
Prompt buduje się z konfiguracji: `persona` + `what_to_watch_for` + `ignore` (np. „osoba w kurtce listonosza”) + `message_style` + `language` + `max_words`. Panel ma przycisk „Testuj prompt na tej klatce”.

**Konfiguracja (`config.yaml`, edytowana z panelu):**
```yaml
model:
  endpoint: http://127.0.0.1:18181/v1      # GenieX na płytce; llama-server na Windows
  name: gemma-4-E4B-it
  max_image_side: 768
cameras:
  - id: front
    name: Front yard
    type: eufy_p2p
    serial: T8160XXXXXXXX
    speakers: [front_cam, bt_outdoor]
  - id: tapo_hall
    type: rtsp
    url: rtsp://user:pass@192.168.1.50/stream2
    fps: 3
    speakers: [bt_outdoor]
speakers:
  - id: front_cam
    type: eufy_talkback
    serial: T8160XXXXXXXX
  - id: bt_outdoor
    type: local_audio
    device: "JBL Flip"
    keep_alive: true
eufy:
  ws_url: ws://127.0.0.1:3000
policy:
  armed: false
  schedule: []                 # opcjonalnie: godziny auto‑uzbrojenia
  threat_threshold: 6
  cooldown_seconds: 45
prompt:
  persona: "You are a sharp, slightly sarcastic home security guard..."
  watch_for: "people approaching the house, doors, gates, packages..."
  ignore: "the owner (tall guy with a beard), mail carriers, cats"
  message_style: "address the person directly, mention their clothing and what they are doing, 1-2 sentences"
  language: en
tts:
  engine: piper
  voice: en_US-ryan-high
```

---

## 4. Stos technologiczny

- **Python 3.11+**, `fastapi`, `uvicorn`, `websockets`, `httpx`, `pydantic` + `pyyaml`, `aiosqlite`
- Wideo: `av` (PyAV) do RTSP (stabilniejsze niż OpenCV na ARM), `opencv-python-headless` do obróbki/różnicy klatek, `numpy`
- TTS: `piper-tts` (offline, ARM64 + Windows), audio out: `sounddevice`
- Sidecary: **GenieX** (`geniex serve`, port 18181) na IQ‑9075; **llama.cpp `llama-server`** (CUDA) na Windows; **`bropat/eufy-security-ws`** w Dockerze (arm64 i amd64)
- Model: **Gemma 4 E4B‑it** (multimodal, GGUF Q4_0 + mmproj) — potwierdzony przez Qualcomm na IQ9 w GenieX; ten sam GGUF w llama‑server. Zapasowy: Gemma 4 E2B (szybszy), Qwen3‑VL 4B.
- Frontend: jeden `index.html` + vanilla JS (lub Preact z CDN), ciemny motyw, bez bundlera. Podgląd: MJPEG z serwera.
- Uruchamianie: `pip install -e .`, `python -m screaming_camera`; na płytce `systemd` unit + `docker compose` dla eufy‑ws.

---

## 5. Struktura repo

```
screaming-camera/
  PLAN.md
  README.md
  pyproject.toml
  config.example.yaml
  docker-compose.yml            # eufy-security-ws
  scripts/
    setup_windows.ps1           # llama-server + model download
    setup_iq9075.sh             # geniex, docker, piper, systemd
    speaker_agent.py            # zdalny głośnik Wi‑Fi (Pi/laptop)
  screaming_camera/
    __main__.py                 # uvicorn
    config.py                   # pydantic models + load/save/hot-reload
    app.py                      # FastAPI routes + WS
    pipeline.py                 # orkiestracja per kamera
    cameras/  base.py rtsp.py eufy_p2p.py webcam.py file.py
    gate.py                     # FrameGate
    analyzer.py                 # VLM client + prompt builder + JSON parse
    policy.py                   # armed/cooldown/schedule
    tts.py                      # Piper
    speakers/ base.py local_audio.py eufy_talkback.py remote_agent.py
    eufy/ ws_client.py          # klient eufy-security-ws
    store.py                    # SQLite + snapshots
    static/ index.html app.js style.css
  tests/                        # pipeline na plikach wideo, bez sprzętu
  samples/                      # klipy testowe (git-ignored)
```

---

## 6. Harmonogram (7 dni)

**Dzień 1 — spike ryzyk (równolegle, żaden kod „produktu”)**
- [ ] Płytka: pierwsze uruchomienie, sprawdzić wersję Ubuntu, sieć, SSH. Zainstalować GenieX, `geniex pull` Gemma 4 E4B, `geniex serve`, wysłać `curl` z obrazkiem → zmierzyć czas na klatkę (cel: < 5 s). Jeśli GenieX nie ma VLM na tej płytce → llama‑server CPU/OpenCL jako plan B.
- [ ] Windows: `llama-server` (CUDA) z tym samym GGUF + mmproj; ten sam `curl` → potwierdzić identyczne API.
- [ ] Eufy: `eufy-security-ws` w Dockerze (login, 2FA), zobaczyć eventy z HomeBase 3, `start_livestream` eufyCam 3 → klatka w Pythonie, **talkback** z WAV na każdej kamerze (eufyCam 3, domofon, S350, wired). Wynik = tabela „co działa”.
- [ ] RTSP: włączyć w apce Eufy dla S350 i wired; konto kamery w Tapo; `ffprobe` każdego URL.
- [ ] Piper na obu platformach, odsłuch.

**Dzień 2–3 — rdzeń na Windows (kamera = webcam/plik/RTSP Tapo)**
- config + pydantic, `CameraSource` (`webcam`, `file`, `rtsp`), `FrameGate`, `Analyzer` (prompt builder, JSON), `Policy`, `tts`, `local_audio`, `store`. Działa end‑to‑end z konsolą jako UI.

**Dzień 4 — Eufy**
- klient eufy‑ws, `eufy_p2p` source (event → livestream → klatki → stop), `eufy_talkback` speaker, obsługa wielu kamer.

**Dzień 5 — panel WWW**
- podgląd kamer, uzbrój/rozbrój, oś zdarzeń z klatką/JSON/tekstem, edycja całej konfiguracji (kamery, głośniki, prompty), „testuj prompt na klatce”, „powiedz to teraz” do testu głośników.

**Dzień 6 — deploy na IQ‑9075**
- `setup_iq9075.sh`, systemd, docker compose, Bluetooth pairing, strojenie fps/rozdzielczości pod realny czas VLM, test wielu kamer naraz.

**Dzień 7 — bufor i szlif pod film**
- `remote_agent` głośnik jeśli potrzebny, keep‑alive BT, scenariusze testowe (kolorowa kurtka, paczka, furtka), ewentualnie detektor osób w `FrameGate`.

---

## 7. Ryzyka i plany B

| Ryzyko | Prawdopod. | Plan B |
|---|---|---|
| GenieX na IQ‑9075 nie obsługuje obrazu / jest wolny | średnie | llama‑server CPU (8 rdzeni, 36 GB RAM) lub OpenCL; model E2B; mniejszy obraz (512 px). Aplikacja bez zmian. |
| Talkback Eufy nie działa na danym modelu | średnie | `remote_agent` na Pi/laptopie lub głośnik BT. |
| Livestream P2P Eufy wstaje > 5 s | średnie | Równolegle włączyć RTSP‑on‑motion jako drugie źródło; do filmu użyć kamer przewodowych. |
| Głośnik BT usypia / traci połączenie | wysokie | keep‑alive + auto‑reconnect w `local_audio`; alternatywnie `remote_agent`. |
| Fałszywe alarmy (właściciel, listonosz) | wysokie | tryb rozbrojony + `ignore` w prompcie + próg + cooldown. |
| Latencja end‑to‑end 8–15 s | pewne | Zaakceptowane; pokazać w filmie jako cechę. |

---

## 8. Poza zakresem pierwszego tygodnia
ONVIF discovery/eventy, detektor YOLO na NPU, sterowanie PTZ S350 („obróć się w stronę intruza”), wielojęzyczność, powiadomienia push, HTTPS/auth panelu (LAN only), nagrywanie klipów.
