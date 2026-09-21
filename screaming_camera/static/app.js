/* Screaming Camera control panel - vanilla JS, no build step. */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmtTime = (ts) => new Date(ts * 1000).toLocaleString([], { hour12: false });
const DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];

let state = null;
let config = null;      // live copy being edited in Settings
let savedConfig = null; // last saved JSON string, for dirty tracking
let oldestEventId = null;

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}
function toast(msg, bad = false) {
  const t = $("#toast"); t.textContent = msg; t.className = bad ? "bad" : ""; t.hidden = false;
  clearTimeout(t._h); t._h = setTimeout(() => (t.hidden = true), 3500);
}

/* ---------------- tabs ---------------- */
$$("nav .tab").forEach((b) => b.addEventListener("click", () => {
  $$("nav .tab").forEach((x) => x.classList.toggle("active", x === b));
  $$(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + b.dataset.tab));
  if (b.dataset.tab === "events") loadEvents(true);
  if (b.dataset.tab === "settings" && !config) loadConfig();
}));

/* ---------------- live state ---------------- */
function chip(id, ok, text, warn = false) {
  const c = $(id); c.textContent = text; c.className = "chip " + (ok ? "ok" : warn ? "warn" : "bad");
}
function renderState(s) {
  state = s;
  const arm = $("#arm");
  arm.textContent = s.armed ? "ARMED" : "DISARMED";
  arm.classList.toggle("on", s.armed);
  chip("#chip-model", s.model.ok, s.model.ok ? "model ok" : "model offline");
  if (s.eufy.enabled) chip("#chip-eufy", s.eufy.driver_connected, s.eufy.driver_connected ? "eufy ok" : s.eufy.connected ? "eufy: no login" : "eufy offline", s.eufy.connected);
  else chip("#chip-eufy", false, "eufy off", true);
  chip("#chip-tts", s.tts.status !== "error", "tts " + (s.tts.engine === "none" ? "off" : s.tts.status), s.tts.engine === "none");
  $("#uptime").textContent = `up ${Math.floor(s.uptime / 60)} min · ${s.cameras.length} cameras · ${s.speakers.length} speakers`;
  renderCameras(s.cameras);
  if (config) renderEufyLogin(s.eufy);
  if (s.eufy.enabled && (s.eufy.needs_verify_code || s.eufy.captcha_id)) showBanner("Eufy needs a 2FA code / captcha - go to Settings > Eufy bridge");
  const sel = $("#events-camera");
  if (sel.options.length <= 1) s.cameras.forEach((c) => sel.add(new Option(c.name, c.id)));
}
function renderCameras(cams) {
  const grid = $("#cameras");
  const ids = new Set(cams.map((c) => c.id));
  $$(".cam", grid).forEach((el) => { if (!ids.has(el.dataset.id)) el.remove(); });
  if (!cams.length) { grid.innerHTML = '<div class="muted">No cameras configured yet. Add one in Settings.</div>'; return; }
  if (grid.firstElementChild && !grid.firstElementChild.classList.contains("cam")) grid.innerHTML = "";
  for (const c of cams) {
    let el = $(`.cam[data-id="${c.id}"]`, grid);
    if (!el) {
      el = document.createElement("div"); el.className = "cam"; el.dataset.id = c.id;
      el.innerHTML = `<div class="view"><img alt="" src="/api/cameras/${c.id}/stream.mjpg" onerror="this.style.display='none'">
        <span class="badge"></span><span class="busy" hidden>ANALYSING</span></div>
        <div class="meta"><span class="name"></span><span class="type"></span><span class="spacer"></span><span class="fps muted small"></span></div>
        <div class="motion"><i></i></div>
        <div class="actions"><button data-act="analyze">Analyse now</button><button data-act="trigger">Wake / trigger</button><button data-act="say">Test voice</button><button data-act="beep">Beep</button></div>`;
      el.addEventListener("click", (e) => camAction(c.id, e.target.dataset.act));
      grid.appendChild(el);
    }
    $(".badge", el).textContent = c.status + (c.error ? " · " + c.error.slice(0, 60) : "");
    $(".badge", el).className = "badge " + c.status;
    $(".busy", el).hidden = !c.busy;
    $(".name", el).textContent = c.name;
    $(".type", el).textContent = c.type + (c.type === "eufy_p2p" ? " · event-driven" : " · continuous");
    $(".fps", el).textContent = c.last_frame_age == null ? "no frames" : `${c.frames} frames · ${c.last_frame_age}s ago`;
    $(".motion i", el).style.width = Math.min(100, c.motion * 1000) + "%";
    const img = $("img", el);
    if (c.status === "streaming" && img.style.display === "none") { img.style.display = ""; img.src = `/api/cameras/${c.id}/stream.mjpg?${Date.now()}`; }
  }
}
async function camAction(id, act) {
  try {
    if (act === "analyze") { toast("Analysing…"); const ev = await api(`/api/test/analyze?camera_id=${id}`, { method: "POST" }); renderLatest(ev); toast(`Threat ${ev.threat_level}: ${ev.scene}`); }
    if (act === "trigger") { await api(`/api/cameras/${id}/trigger`, { method: "POST" }); toast("Triggered"); }
    if (act === "say" || act === "beep") {
      const cam = state.cameras.find((c) => c.id === id);
      if (!cam.speakers.length) return toast("No speakers assigned to this camera", true);
      toast(act === "beep" ? "Sending a 2 s beep (Eufy: wakes the camera first, ~5-10 s)" : "Synthesizing and sending (Eufy: ~10 s)");
      const r = await api("/api/test/speak", { method: "POST", body: JSON.stringify({ text: "This is a test of the security system. You are on camera.", speakers: cam.speakers, tone: act === "beep" }) });
      const failed = Object.entries(r.speakers).filter(([, st]) => st !== "ok").map(([n]) => n);
      toast(r.ok ? "Sent OK on " + Object.keys(r.speakers).join(", ") + (failed.length ? " (failed: " + failed.join(", ") + ")" : "") : "Playback failed: " + JSON.stringify(r.speakers), !r.ok);
    }
  } catch (e) { toast(e.message, true); }
}
$("#arm").addEventListener("click", async () => {
  try { await api("/api/arm", { method: "POST", body: JSON.stringify({ armed: !state.armed_manual }) }); } catch (e) { toast(e.message, true); }
});

/* ---------------- verdicts & events ---------------- */
function threatClass(t) { return t >= 7 ? "t-high" : t >= 4 ? "t-mid" : "t-low"; }
function renderLatest(ev) {
  const el = $("#latest"); el.classList.remove("empty");
  el.innerHTML = `<div><img src="/api/snapshots/${ev.snapshot}" alt=""></div><div>
    <div class="line1 muted small">${fmtTime(ev.ts)} · ${esc(ev.camera_name)} · trigger: ${esc(ev.trigger)} · ${ev.latency_ms} ms model</div>
    <div class="msg ${ev.spoken ? "" : "quiet"}">${ev.spoken ? "🔊 " : ""}${esc(ev.message || (ev.error ? "⚠ " + ev.error : "(nothing to say)"))}</div>
    <div class="threat ${threatClass(ev.threat_level)}"><b>${ev.threat_level}</b> threat level · <span class="muted">${esc(ev.decision)}</span></div>
    <div class="people">${(ev.people || []).map((p) => `<span class="person">👤 ${esc(p.clothing)} — ${esc(p.action)}${p.carrying ? " · " + esc(p.carrying) : ""}</span>`).join("")}</div>
    <div class="kv"><b>Scene:</b> ${esc(ev.scene)}<br><b>Why:</b> ${esc(ev.reasoning)}</div></div>`;
}
function eventRow(ev) {
  const d = document.createElement("div");
  d.className = "ev" + (ev.spoken ? " spoken" : ""); d.dataset.id = ev.id;
  d.innerHTML = `<img src="/api/snapshots/${ev.snapshot}" loading="lazy" alt="">
    <div><div class="line1"><span class="threat ${threatClass(ev.threat_level)}"><b>${ev.threat_level}</b></span>
      <span>${fmtTime(ev.ts)}</span><span>${esc(ev.camera_name)}</span><span>${esc(ev.trigger)}</span>${ev.armed ? "" : "<span>disarmed</span>"}</div>
      <div class="scene">${esc(ev.scene || ev.error)}</div>
      ${ev.message ? `<div class="${ev.spoken ? "said" : "muted"}">${esc(ev.message)}</div>` : ""}</div>
    <div class="muted small">${ev.latency_ms} ms</div>`;
  d.addEventListener("click", () => {
    const pre = $("pre", d);
    if (pre) return pre.remove();
    const p = document.createElement("pre"); p.textContent = JSON.stringify(ev, null, 2); d.appendChild(p);
  });
  return d;
}
async function loadEvents(reset) {
  const list = $("#events");
  if (reset) { list.innerHTML = ""; oldestEventId = null; }
  const q = new URLSearchParams({ limit: 50 });
  if (oldestEventId) q.set("before_id", oldestEventId);
  if ($("#spoken-only").checked) q.set("spoken_only", "true");
  if ($("#events-camera").value) q.set("camera_id", $("#events-camera").value);
  const evs = await api("/api/events?" + q);
  evs.forEach((ev) => list.appendChild(eventRow(ev)));
  if (evs.length) oldestEventId = evs[evs.length - 1].id;
  $("#more-events").hidden = evs.length < 50;
  const st = await api("/api/stats");
  $("#stats").textContent = `24 h: ${st.events_24h} events · ${st.spoken_24h} spoken · max threat ${st.max_threat_24h}`;
}
$("#refresh-events").addEventListener("click", () => loadEvents(true));
$("#more-events").addEventListener("click", () => loadEvents(false));
$("#spoken-only").addEventListener("change", () => loadEvents(true));
$("#events-camera").addEventListener("change", () => loadEvents(true));

/* ---------------- websocket ---------------- */
function connectWs() {
  const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  ws.onmessage = (m) => {
    const { type, data } = JSON.parse(m.data);
    if (type === "state") renderState(data);
    if (type === "event") {
      renderLatest(data);
      if ($("#tab-events").classList.contains("active")) $("#events").prepend(eventRow(data));
      showBanner(data.spoken ? `🔊 ${data.camera_name}: “${data.message}”` : `${data.camera_name}: threat ${data.threat_level} — ${data.scene}`, data.spoken);
    }
    if (type === "analyzing") showBanner(`Analysing ${data.camera_id} (${data.trigger})…`);
    if (type === "speaking") showBanner(`🔊 Speaking on ${data.speakers.join(", ")}: “${data.text}”`, true);
    if (type === "config" && data.io_restarted) toast("Config applied, cameras restarted");
  };
  ws.onclose = () => setTimeout(connectWs, 2000);
}
function showBanner(text, speaking = false) {
  const b = $("#banner"); b.textContent = text; b.className = "banner" + (speaking ? " speaking" : ""); b.hidden = false;
  clearTimeout(b._h); b._h = setTimeout(() => (b.hidden = true), 12000);
}

/* ---------------- settings ---------------- */
const getPath = (o, p) => p.split(".").reduce((a, k) => (a == null ? undefined : a[k]), o);
const setPath = (o, p, v) => { const ks = p.split("."); const last = ks.pop(); ks.reduce((a, k) => (a[k] ??= {}), o)[last] = v; };

async function loadConfig() {
  config = await api("/api/config"); savedConfig = JSON.stringify(config);
  bindForm(); renderSchedule(); renderCameraCards(); renderSpeakerCards(); renderSaySpeakers(); markDirty();
}
function bindForm() {
  $$("[data-path]").forEach((el) => {
    const v = getPath(config, el.dataset.path);
    if (el.type === "checkbox") el.checked = !!v; else if (el.dataset.json) el.value = JSON.stringify(v ?? {}); else el.value = v ?? "";
    el.oninput = () => {
      let val = el.type === "checkbox" ? el.checked : el.value;
      if (el.type === "number") val = Number(val);
      if (el.dataset.json) { try { val = JSON.parse(val || "{}"); el.style.borderColor = ""; } catch { el.style.borderColor = "var(--bad)"; return; } }
      setPath(config, el.dataset.path, val); markDirty();
    };
  });
}
function markDirty() {
  const dirty = JSON.stringify(config) !== savedConfig;
  $("#dirty").textContent = dirty ? "Unsaved changes" : "No unsaved changes";
  $("#dirty").style.color = dirty ? "var(--warn)" : "";
}
$("#save-config").addEventListener("click", async () => {
  try { const r = await api("/api/config", { method: "PUT", body: JSON.stringify(config) }); savedConfig = JSON.stringify(config); markDirty(); toast("Saved & applied (v" + r.version + ")"); }
  catch (e) { toast("Save failed: " + e.message, true); }
});
$("#reload-config").addEventListener("click", loadConfig);

/* schedule */
function renderSchedule() {
  const box = $("#schedule"); box.innerHTML = "";
  config.policy.schedule.forEach((w, i) => {
    const row = document.createElement("div"); row.className = "sched";
    row.innerHTML = `<div class="days">${DAYS.map((d, di) => `<span class="${w.days.includes(di) ? "on" : ""}" data-d="${di}">${d}</span>`).join("")}</div>
      <label>from<input type="time" value="${w.start}"></label><label>to<input type="time" value="${w.end}"></label><button class="ghost danger">×</button>`;
    $$(".days span", row).forEach((s) => s.onclick = () => { const d = +s.dataset.d; w.days = w.days.includes(d) ? w.days.filter((x) => x !== d) : [...w.days, d].sort(); s.classList.toggle("on"); markDirty(); });
    const [a, b] = $$("input", row); a.oninput = () => { w.start = a.value; markDirty(); }; b.oninput = () => { w.end = b.value; markDirty(); };
    $("button", row).onclick = () => { config.policy.schedule.splice(i, 1); renderSchedule(); markDirty(); };
    box.appendChild(row);
  });
}
$("#add-window").addEventListener("click", () => { config.policy.schedule.push({ days: [0, 1, 2, 3, 4, 5, 6], start: "22:00", end: "06:00" }); renderSchedule(); markDirty(); });

/* generic card editor for cameras / speakers */
const CAMERA_FIELDS = {
  common: [["id", "ID (unique, no spaces)"], ["name", "Name"], ["type", "Type", "select", ["rtsp", "eufy_p2p", "webcam", "file"]], ["enabled", "Enabled", "checkbox"]],
  rtsp: [["url", "RTSP URL (rtsp://user:pass@ip/stream1)"]],
  eufy_p2p: [["serial", "Eufy device serial"], ["event_hold_seconds", "Keep stream alive after event (s)", "number"]],
  webcam: [["device_index", "Device index", "number"]],
  file: [["path", "Video file or image folder"]],
  analysis: [["fps", "Frames/s to gate", "number"], ["motion_sensitivity", "Motion sensitivity (0.005 sensitive – 0.1 lazy)", "number"]],
};
const SPEAKER_FIELDS = {
  common: [["id", "ID"], ["name", "Name"], ["type", "Type", "select", ["local_audio", "eufy_talkback", "remote_agent"]], ["enabled", "Enabled", "checkbox"], ["volume", "Volume (0–1.5)", "number"]],
  local_audio: [["device", "Output device name contains (empty = default)"], ["keep_alive", "Keep-alive (Bluetooth)", "checkbox"]],
  eufy_talkback: [["serial", "Eufy device serial"], ["channels", "AAC channels (1 mono / 2 stereo)", "number"]],
  remote_agent: [["url", "Agent URL (http://host:8181)"]],
};
function fieldEl(obj, [key, label, kind, options], rerender) {
  const wrap = document.createElement("label");
  if (kind === "checkbox") { wrap.className = "check"; wrap.innerHTML = `<input type="checkbox"> ${label}`; const i = $("input", wrap); i.checked = !!obj[key]; i.onchange = () => { obj[key] = i.checked; markDirty(); }; return wrap; }
  wrap.innerHTML = label;
  let input;
  if (kind === "select") { input = document.createElement("select"); options.forEach((o) => input.add(new Option(o, o))); input.value = obj[key]; input.onchange = () => { obj[key] = input.value; markDirty(); rerender(); }; }
  else { input = document.createElement("input"); if (kind === "number") { input.type = "number"; input.step = "any"; } input.value = obj[key] ?? ""; input.oninput = () => { obj[key] = kind === "number" ? Number(input.value) : input.value; markDirty(); }; }
  wrap.appendChild(input); return wrap;
}
function renderCameraCards() {
  const box = $("#cameras-form"); box.innerHTML = "";
  config.cameras.forEach((cam, i) => {
    const card = document.createElement("div"); card.className = "card";
    const head = document.createElement("div"); head.className = "head";
    head.innerHTML = `<b>${esc(cam.name || cam.id)}</b><button class="ghost danger">remove</button>`;
    $("button", head).onclick = () => { config.cameras.splice(i, 1); renderCameraCards(); markDirty(); };
    card.appendChild(head);
    const rows = [CAMERA_FIELDS.common, CAMERA_FIELDS[cam.type] || [], CAMERA_FIELDS.analysis];
    rows.forEach((fs) => { const r = document.createElement("div"); r.className = "row"; fs.forEach((f) => r.appendChild(fieldEl(cam, f, renderCameraCards))); card.appendChild(r); });
    const sp = document.createElement("div"); sp.className = "checks"; sp.innerHTML = "<span class='muted'>Speakers:</span>";
    config.speakers.forEach((s) => { const l = document.createElement("label"); l.innerHTML = `<input type="checkbox" ${cam.speakers.includes(s.id) ? "checked" : ""}> ${esc(s.name || s.id)}`; $("input", l).onchange = (e) => { cam.speakers = e.target.checked ? [...cam.speakers, s.id] : cam.speakers.filter((x) => x !== s.id); markDirty(); }; sp.appendChild(l); });
    if (!config.speakers.length) sp.innerHTML += "<span class='muted'>none configured</span>";
    card.appendChild(sp); box.appendChild(card);
  });
}
function renderSpeakerCards() {
  const box = $("#speakers-form"); box.innerHTML = "";
  config.speakers.forEach((sp, i) => {
    const card = document.createElement("div"); card.className = "card";
    const head = document.createElement("div"); head.className = "head";
    head.innerHTML = `<b>${esc(sp.name || sp.id)}</b><button class="ghost danger">remove</button>`;
    $("button", head).onclick = () => { config.speakers.splice(i, 1); renderSpeakerCards(); renderCameraCards(); renderSaySpeakers(); markDirty(); };
    card.appendChild(head);
    [SPEAKER_FIELDS.common, SPEAKER_FIELDS[sp.type] || []].forEach((fs) => { const r = document.createElement("div"); r.className = "row"; fs.forEach((f) => r.appendChild(fieldEl(sp, f, renderSpeakerCards))); card.appendChild(r); });
    box.appendChild(card);
  });
}
function renderSaySpeakers() {
  const box = $("#say-speakers"); box.innerHTML = "";
  config.speakers.forEach((s) => { const l = document.createElement("label"); l.innerHTML = `<input type="checkbox" value="${esc(s.id)}" checked> ${esc(s.name || s.id)}`; box.appendChild(l); });
}
$("#add-camera").addEventListener("click", () => { config.cameras.push({ id: "cam" + (config.cameras.length + 1), name: "", type: "rtsp", enabled: true, url: "", device_index: 0, path: "", serial: "", fps: 2, motion_sensitivity: 0.02, event_hold_seconds: 20, speakers: [] }); renderCameraCards(); markDirty(); });
$("#add-speaker").addEventListener("click", () => { config.speakers.push({ id: "spk" + (config.speakers.length + 1), name: "", type: "local_audio", enabled: true, device: "", keep_alive: false, serial: "", url: "", volume: 1 }); renderSpeakerCards(); renderCameraCards(); renderSaySpeakers(); markDirty(); });

$("#check-model").addEventListener("click", async () => {
  $("#model-check").textContent = "checking…";
  try { const r = await api("/api/model/health"); $("#model-check").textContent = r.ok ? "OK — models: " + (r.models || []).join(", ") : "Unreachable: " + r.error; } catch (e) { $("#model-check").textContent = e.message; }
});
$("#say").addEventListener("click", async () => {
  const speakers = $$("#say-speakers input:checked").map((i) => i.value);
  if (!speakers.length) return toast("Pick at least one speaker", true);
  try { const r = await api("/api/test/speak", { method: "POST", body: JSON.stringify({ text: $("#say-text").value || $("#say-text").placeholder, speakers }) }); toast(r.ok ? "Played" : "Failed: " + JSON.stringify(r.speakers), !r.ok); } catch (e) { toast(e.message, true); }
});
$("#list-audio").addEventListener("click", async () => { const d = await api("/api/audio/devices"); $("#audio-devices").textContent = Array.isArray(d) ? d.map((x) => `${x.index}: ${x.name}  [${x.hostapi}]`).join("\n") : d.error; });
function renderEufyLogin(e) {
  const box = $("#eufy-login");
  if (!e || !e.enabled) { box.hidden = true; $("#eufy-status").textContent = e ? "bridge disabled" : ""; return; }
  $("#eufy-status").textContent = !e.connected ? "bridge unreachable (is docker compose up?)" : e.driver_connected ? "logged in to Eufy cloud" : "bridge up, not logged in" + (e.connection_error ? ": " + e.connection_error : "");
  const captcha = !!e.captcha_id, verify = !!e.needs_verify_code;
  box.hidden = !(captcha || verify);
  $("#eufy-captcha").hidden = !captcha;
  if (captcha) { const img = e.captcha_image || ""; $("#eufy-captcha").src = img.startsWith("data:") ? img : "data:image/png;base64," + img; }
  $("#eufy-login-msg").textContent = captcha ? "Eufy asks for a captcha - type the characters from the image:" : "Eufy sent a verification code to the account's e-mail - type it here:";
  box.dataset.kind = captcha ? "captcha" : "verify_code";
}
$("#eufy-send-code").addEventListener("click", async () => {
  const kind = $("#eufy-login").dataset.kind, code = $("#eufy-code").value.trim();
  if (!code) return;
  try { await api(`/api/eufy/${kind}`, { method: "POST", body: JSON.stringify({ code }) }); $("#eufy-code").value = ""; toast("Sent - wait a few seconds, then List devices"); } catch (e) { toast(e.message, true); }
});
$("#eufy-reconnect").addEventListener("click", async () => { try { await api("/api/eufy/connect", { method: "POST" }); toast("Connecting - watch for a 2FA / captcha prompt"); } catch (e) { toast(e.message, true); } });
$("#list-eufy").addEventListener("click", async () => {
  const d = await api("/api/eufy/devices"); renderEufyLogin(d);
  $("#eufy-devices").textContent = !d.enabled ? "Eufy bridge disabled (enable, save, then list)." : !d.connected ? "Cannot reach eufy-security-ws - run `docker compose up -d` and check the URL." : !d.driver_connected ? "Bridge reachable but not logged in to Eufy - complete 2FA / captcha above or check `docker compose logs -f`." : d.devices.map((x) => `${x.serial}  ${x.name} (${x.model}) station ${x.station} battery ${x.battery ?? "-"}`).join("\n") || "no devices (try Reconnect / check ACCEPT_INVITATIONS)";
});

/* ---------------- boot ---------------- */
(async () => {
  try { renderState(await api("/api/state")); } catch (e) { toast("Server unreachable", true); }
  try { const evs = await api("/api/events?limit=1"); if (evs.length) renderLatest(evs[0]); } catch {}
  connectWs();
})();
