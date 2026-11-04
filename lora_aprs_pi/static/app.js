// LoRa APRS Pi web page. Plain JavaScript, no libraries, works with no internet.
"use strict";

const $ = (sel) => document.querySelector(sel);
let password = "";            // asked for once if the station has a settings password
let options = null;           // boards and symbols, from /api/settings
let lastHeardId = 0;
let unseen = 0;
let lastStatus = null;

// ---------- talking to the station ----------

async function api(path, body) {
  const opts = { headers: {} };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  if (password) opts.headers["X-Station-Password"] = password;
  let res = await fetch(path, opts);
  if (res.status === 401) {
    const typed = prompt("Settings password");
    if (typed === null) throw new Error("A password is needed to change settings.");
    password = typed;
    opts.headers["X-Station-Password"] = password;
    res = await fetch(path, opts);
    if (res.status === 401) { password = ""; throw new Error("That password is not right."); }
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok && !data.errors) throw new Error(data.error || `The station answered ${res.status}.`);
  return data;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;   // never innerHTML: packets come off the air
  return e;
}

function ago(t) {
  const s = Math.max(0, Math.round(Date.now() / 1000 - t));
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min ago`;
}

function inMinutes(s) {
  if (s < 90) return `${s} s`;
  return `${Math.round(s / 60)} min`;
}

// ---------- tabs ----------

const tabs = ["status", "heard", "settings", "wifi"];
function show(name) {
  for (const t of tabs) {
    $(`#tab-${t}`).hidden = t !== name;
    $(`#t-${t}`).setAttribute("aria-selected", String(t === name));
  }
  if (name === "heard") { unseen = 0; badge(); }
  if (name === "settings") loadSettings();
  if (name === "wifi") loadWifi();
  history.replaceState(null, "", `#${name}`);
}
for (const t of tabs) $(`#t-${t}`).addEventListener("click", () => show(t));

function badge() {
  const b = $("#heard-badge");
  b.hidden = unseen === 0;
  b.textContent = unseen > 99 ? "99+" : String(unseen);
}

// ---------- status ----------

function renderStatus(s) {
  lastStatus = s;
  $("#callsign").textContent = s.callsign === "N0CALL" ? "LoRa APRS Pi" : s.callsign;
  $("#sim").hidden = !s.simulate;
  const r = s.radio;
  const lamp = $("#lamp");
  const radioText = `${r.freq_mhz.toFixed(3)} MHz, SF${r.sf}, ${r.bw_khz} kHz, CR 4/${r.cr}`;
  const justSent = s.beacon.last && Date.now() / 1000 - s.beacon.last.time < 6;
  if (r.state === "error") {
    lamp.dataset.state = "error";
    $("#lamp-text").textContent = `Radio problem: ${r.error}`;
  } else if (r.state !== "ok") {
    lamp.dataset.state = "starting";
    $("#lamp-text").textContent = "Starting the radio…";
  } else if (justSent) {
    lamp.dataset.state = "tx";
    $("#lamp-text").textContent = `Sent a beacon (${s.beacon.last.airtime_ms} ms on air)`;
  } else {
    lamp.dataset.state = "ok";
    $("#lamp-text").textContent = `Listening on ${r.freq_mhz.toFixed(3)} MHz`;
  }

  $("#s-radio").textContent = r.state === "error" ? r.error : radioText;
  $("#s-radio").className = r.state === "error" ? "bad" : "";
  $("#s-board").textContent = r.board;
  $("#s-power").textContent = `About ${r.antenna_dbm} dBm at the antenna (chip set to ${r.chip_dbm} dBm)`;
  $("#s-lacf").textContent = s.lacf.available ? "Ready: sends and receives LACF frames" : s.lacf.error;
  $("#s-lacf").className = s.lacf.available ? "" : "bad";

  const g = s.gps;
  if (!g) $("#s-gps").textContent = "No GPS set up";
  else if (g.error) $("#s-gps").textContent = g.error;
  else if (g.fresh) $("#s-gps").textContent =
    `${g.lat.toFixed(5)}, ${g.lon.toFixed(5)}` + (g.alt_m !== null ? `, ${Math.round(g.alt_m * 3.28084)} ft` : "") + `, ${g.sats} satellites`;
  else $("#s-gps").textContent = "Waiting for a fix";

  const c = s.counts;
  $("#s-counts").textContent =
    `${c.rx_ok} received, ${c.rx_dropped} dropped, ${c.tx} sent, ${c.repeated} repeated, ${c.gated} gated`;
  const dg = s.digi;
  $("#s-digi").textContent = { off: "Off", "fill-in": "Fill-in (WIDE1-1)", wide: "Wide area (WIDE1-1 and WIDE2-N)" }[dg.mode]
    + (dg.waiting ? `, ${dg.waiting} waiting to go out` : "");

  const ig = s.igate;
  const igText = { off: "iGate is off", "needs a callsign": "Set your callsign to start the iGate",
                   connecting: "Connecting…", error: `Can't reach the server: ${ig.error}` };
  if (ig.state === "connected") {
    $("#s-igate").textContent = ig.verified
      ? `Logged in to ${ig.server}, ${ig.sent} lines sent`
      : `Logged in to ${ig.server} but NOT verified: packets won't be gated. Check the callsign or passcode.`;
    $("#s-igate").className = ig.verified ? "good" : "bad";
  } else {
    $("#s-igate").textContent = igText[ig.state] || ig.state;
    $("#s-igate").className = ig.state === "error" ? "bad" : "";
  }
  if (!s.network) {
    $("#s-net").textContent = s.simulate ? "Not managed in simulate mode" : "Not managed by this program";
  } else {
    const n = s.network;
    $("#s-net").textContent = n.mode === "hotspot" ? `Hotspot "${n.ssid}" at ${n.ip}`
      : n.mode === "client" ? `${n.ssid}, ${n.ip || "no address yet"}` : (n.note || "Not connected");
  }

  const ap = s.beacon.auto_power;
  $("#s-autopower-row").hidden = !ap.on;
  if (ap.on) $("#s-autopower").textContent =
    `Auto reduced: about ${ap.level_dbm} dBm at the antenna (set maximum ${ap.max_dbm} dBm). ${ap.note}.`;

  const b = s.beacon;
  const fmt = { standard: "standard", lacf: "LACF", alternate: "alternating standard and LACF" }[b.format];
  $("#s-beacon").textContent = b.enabled
    ? `Next beacon in ${inMinutes(b.next_in_s)}, ${fmt} format.` : "Automatic beacons are off.";
  $("#s-beacon-note").hidden = !b.note;
  $("#s-beacon-note").textContent = b.note || "";
  $("#s-beacon-last").hidden = !b.last;
  if (b.last) $("#s-beacon-last").textContent =
    `Last sent ${ago(b.last.time)}, ${b.last.format === "lacf" ? "LACF" : "standard"}, ${b.last.bytes} bytes\n${b.last.text}`;
  $("#s-version").textContent = `LoRa APRS Pi ${s.version}, running ${inMinutes(s.uptime_s)}`;
}

$("#beacon-now").addEventListener("click", async () => {
  const btn = $("#beacon-now");
  btn.disabled = true;
  try { await api("/api/beacon", {}); } catch (e) { alert(e.message); }
  setTimeout(() => { btn.disabled = false; poll(); }, 1500);
});

// ---------- heard list ----------

function signalBars(rssi, snr) {
  // SNR says more than RSSI about whether the next packet will get through
  const level = snr >= 5 ? 4 : snr >= -2 ? 3 : snr >= -8 ? 2 : snr >= -14 ? 1 : 0;
  const s = el("span", "signal");
  s.setAttribute("aria-label", `Signal ${level} of 4`);
  for (let i = 1; i <= 4; i++) s.append(el("i", i <= level ? "on" : ""));
  return s;
}

function heardItem(e) {
  const li = el("li", e.dir === "tx" ? "sent" : e.text ? "" : "dropped");
  const l1 = el("div", "line1");
  l1.append(el("span", "who", e.source || (e.text ? "?" : "Unreadable packet")));
  if (e.dir === "tx") l1.append(el("span", "tag sent", e.kind === "repeat" ? "Repeated" : "Sent"));
  if (e.repeating) l1.append(el("span", "tag sent", "Repeated"));
  if (e.own_repeat) l1.append(el("span", "tag gated", e.repeated_by ? `Your beacon, via ${e.repeated_by}` : "Your beacon"));
  if (e.gated) l1.append(el("span", "tag gated", "Gated"));
  if (e.format === "lacf") l1.append(el("span", "tag lacf", "LACF"));
  else if (e.format === "standard") l1.append(el("span", "tag", "Standard"));
  l1.append(el("span", "when", ago(e.time)));
  li.append(l1);

  const what = [];
  if (e.dir === "rx") what.push(`${Math.round(e.rssi)} dBm, SNR ${e.snr.toFixed(1)} dB`);
  if (e.dir === "tx" && e.airtime_ms) what.push(`${e.airtime_ms} ms on air`);
  if (e.dir === "tx" && e.power_dbm !== undefined && e.power_dbm !== null) what.push(`about ${e.power_dbm} dBm`);
  if (e.type && e.text) what.push(e.type);
  if (e.distance_km !== undefined) what.push(`${e.distance_km} km at ${e.bearing}°`);
  if (e.altitude_ft !== undefined) what.push(`${e.altitude_ft} ft`);
  if (e.path) what.push(`via ${e.path}`);
  if (e.not_gated) what.push(`not gated: ${e.not_gated}`);
  if (e.not_repeated) what.push(`not repeated: ${e.not_repeated}`);
  const w = el("div", "what");
  if (e.dir === "rx") { w.append(signalBars(e.rssi, e.snr), " "); }
  w.append(what.join(", "));
  li.append(w);

  if (e.digi_report) li.append(el("div", "comment",
    `${e.digi_report.digi} heard you at ${e.digi_report.rssi} dBm, SNR ${e.digi_report.snr.toFixed(1)} dB. ` +
    `You heard its repeat at ${Math.round(e.rssi)} dBm, SNR ${e.snr.toFixed(1)} dB.`));
  if (e.error) li.append(el("div", "comment bad", e.error));
  else if (e.comment) li.append(el("div", "comment", e.comment));
  if (e.text) {
    const d = el("details");
    d.append(el("summary", "", "Packet"), el("pre", "raw", e.text));
    li.append(d);
  }
  li.dataset.time = e.time;
  return li;
}

function renderHeard(items) {
  const list = $("#heard-list");
  for (const e of items.slice().reverse()) {      // oldest first, so the newest ends up on top
    list.prepend(heardItem(e));
    lastHeardId = Math.max(lastHeardId, e.id);
    if ($("#tab-heard").hidden && e.dir === "rx") unseen++;
  }
  while (list.children.length > 200) list.lastChild.remove();
  $("#heard-empty").hidden = list.children.length > 0;
  badge();
  for (const li of list.children) li.querySelector(".when").textContent = ago(+li.dataset.time);
}

// ---------- settings ----------

function getPath(obj, path) { return path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj); }
function setPath(obj, path, val) {
  const keys = path.split(".");
  let o = obj;
  for (const k of keys.slice(0, -1)) o = o[k] = o[k] || {};
  o[keys.at(-1)] = val;
}

async function loadSettings() {
  const data = await api("/api/settings");
  options = data.options;
  const prof = $("#profiles");
  prof.replaceChildren(...Object.entries(options.profiles).map(([k, p]) => { const o = el("option", "", p.label); o.value = k; return o; }));
  const sym = $("#symbols");
  sym.replaceChildren(...Object.entries(options.symbols).map(([k, label]) => { const o = el("option", "", `${label} (${k})`); o.value = k; return o; }));
  fillForm($("#settings-form"), data.settings);
  fillForm($("#w-hotspot"), data.settings);
  $("#pw-state").textContent = data.settings.web.password_set
    ? "A password is set. Enter a new one to change it." : "No password: anyone on this network can change settings.";
  $("#clear-pw").hidden = !data.settings.web.password_set;
  savedCallsign = data.settings.station.callsign;
  showPasscode(savedCallsign);
  boardChanged();
}

function fillForm(form, settings) {
  for (const input of form.querySelectorAll("[name]")) {
    const v = getPath(settings, input.name);
    if (input.type === "checkbox") input.checked = !!v;
    else input.value = v === null || v === undefined ? "" : v;
    clearError(input);
  }
}

function readForm(form) {
  const out = {};
  for (const input of form.querySelectorAll("[name]")) {
    if (input.closest("[hidden]") && input.name.startsWith("radio.custom")) continue;
    setPath(out, input.name, input.type === "checkbox" ? input.checked : input.value.trim());
  }
  return out;
}

function clearError(input) {
  input.removeAttribute("aria-invalid");
  const next = input.parentElement.querySelector(".err");
  if (next) next.remove();
}

function showErrors(form, errors) {
  let first = null;
  for (const [name, msg] of Object.entries(errors)) {
    const input = form.querySelector(`[name="${name}"]`);
    if (!input) continue;
    input.setAttribute("aria-invalid", "true");
    input.parentElement.append(el("span", "err", msg));
    first = first || input;
  }
  if (first) first.focus();
  return first;
}

// The passcode box shows the passcode worked out from the saved callsign, so it's
// clear one is already in place. Typing a number overrides it.
function showPasscode(savedCall) {
  const box = $("[name='igate.passcode']");
  const typedCall = $("[name='station.callsign']").value.trim().toUpperCase();
  const auto = options && options.auto_passcode;
  if (auto !== null && auto !== undefined && typedCall === savedCall) {
    box.placeholder = `${auto} (from ${savedCall})`;
    $("#passcode-hint").textContent =
      `Filled in automatically from ${savedCall}. Type a number only to use a different passcode, or -1 to listen only.`;
  } else {
    box.placeholder = "Worked out from your callsign";
    $("#passcode-hint").textContent = "Filled in automatically from your callsign once you save it.";
  }
}
let savedCallsign = "";
$("[name='station.callsign']").addEventListener("input", () => showPasscode(savedCallsign));

function boardChanged() {
  if (!options) return;
  const p = options.profiles[$("#profiles").value];
  $("#custom-pins").hidden = $("#profiles").value !== "custom";
  const want = Number($("[name='radio.power_dbm']").value);
  const gain = $("#profiles").value === "custom" ? Number($("[name='radio.custom.pa_gain_db']").value) : p.pa_gain_db;
  const maxChip = $("#profiles").value === "custom" ? Number($("[name='radio.custom.max_chip_dbm']").value) : p.max_chip_dbm;
  const chip = Math.max(-9, Math.min(maxChip, Math.round(want - gain)));
  $("#power-hint").textContent =
    `The chip will be set to ${chip} dBm, about ${chip + gain} dBm at the antenna. This board goes up to about ${maxChip + gain} dBm.`;
}
$("#profiles").addEventListener("change", boardChanged);
$("[name='radio.power_dbm']").addEventListener("input", boardChanged);
$("#custom-pins").addEventListener("input", boardChanged);

async function save(form, changes, msgEl) {
  for (const i of form.querySelectorAll("[aria-invalid]")) clearError(i);
  msgEl.className = "save-msg";
  msgEl.textContent = "Saving…";
  try {
    const res = await api("/api/settings", changes);
    if (res.errors && Object.keys(res.errors).length) {
      showErrors(form, res.errors);
      msgEl.className = "save-msg bad";
      msgEl.textContent = "Some settings need fixing; see the marked fields.";
      return false;
    }
    msgEl.textContent = "Saved. The radio picks up changes right away.";
    return true;
  } catch (e) {
    msgEl.className = "save-msg bad";
    msgEl.textContent = e.message;
    return false;
  }
}

$("#settings-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const changes = readForm($("#settings-form"));
  const newPw = changes.web.password;
  if (await save($("#settings-form"), changes, $("#save-msg"))) {
    if (newPw) password = newPw;
    $("[name='web.password']").value = "";
    loadSettings();
  }
});

$("#clear-pw").addEventListener("click", async () => {
  if (await save($("#settings-form"), { web: { clear_password: true } }, $("#save-msg"))) loadSettings();
});

// ---------- WiFi ----------

async function loadWifi() {
  if (!options) await loadSettings().catch(() => {});
  try {
    const w = await api("/api/wifi");
    renderWifi(w);
  } catch (e) { $("#w-state").textContent = e.message; }
}

function renderWifi(w) {
  const help = $("#w-help");
  help.hidden = true;
  if (!w.available) {
    $("#w-state").textContent = "WiFi control needs NetworkManager (Raspberry Pi OS Bookworm or newer).";
    return;
  }
  if (w.mode === "hotspot") {
    $("#w-state").textContent = `The Pi is running its hotspot "${w.ssid}". Join a network below to put it on your WiFi.`;
  } else if (w.mode === "client") {
    $("#w-state").textContent = `Connected to ${w.ssid}` + (w.ip ? ` at ${w.ip}` : "") + ".";
    if (w.hostname) { help.hidden = false; help.textContent = `On this network the page is at http://${w.hostname}.local or http://${w.ip}`; }
  } else {
    $("#w-state").textContent = "Not connected to WiFi.";
  }
}

$("#w-scan").addEventListener("click", async () => {
  const list = $("#w-list");
  list.replaceChildren(el("li", "hint", "Looking…"));
  try {
    const nets = (await api("/api/wifi/scan")).networks;
    list.replaceChildren();
    if (!nets.length) list.append(el("li", "hint", "No networks found. While the Pi is its own hotspot it can't always scan; use Type a network name."));
    for (const n of nets) {
      const b = el("button");
      b.type = "button";
      b.append(el("span", "ssid", n.ssid), signalBars(0, (n.signal - 50) / 4), el("span", "lock", n.secure ? "Password" : "Open"));
      b.addEventListener("click", () => pickNetwork(n));
      const li = el("li"); li.append(b); list.append(li);
    }
  } catch (e) { list.replaceChildren(el("li", "bad", e.message)); }
});

$("#w-other").addEventListener("click", () => {
  const ssid = prompt("Network name (SSID)");
  if (ssid) pickNetwork({ ssid: ssid.trim(), secure: true });
});

let picked = null;
function pickNetwork(n) {
  picked = n;
  $("#w-join").hidden = false;
  $("#w-join-name").textContent = `Join ${n.ssid}`;
  const pw = $("#w-join [name=password]");
  pw.closest("label").hidden = !n.secure;
  pw.value = "";
  if (n.secure) pw.focus();
}

$("#w-join").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    const r = await api("/api/wifi/connect", { ssid: picked.ssid, password: $("#w-join [name=password]").value });
    $("#w-join").hidden = true;
    const help = $("#w-help");
    help.hidden = false;
    help.textContent = r.message;
  } catch (e) { alert(e.message); }
});

$("#w-hotspot").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const changes = readForm($("#w-hotspot"));
  if (await save($("#w-hotspot"), changes, $("#w-msg"))) $("[name='wifi.hotspot_password']").value = "";
});

$("#w-hotspot-now").addEventListener("click", async () => {
  if (!confirm("Switch the Pi to its own hotspot? It will leave the current WiFi network.")) return;
  try { $("#w-msg").textContent = (await api("/api/wifi/hotspot", {})).message; }
  catch (e) { $("#w-msg").textContent = e.message; }
});

// ---------- polling ----------

async function poll() {
  try {
    const [status, heard] = await Promise.all([api("/api/status"), api(`/api/heard?since=${lastHeardId}`)]);
    renderStatus(status);
    renderHeard(heard.heard);
  } catch (e) {
    $("#lamp").dataset.state = "error";
    $("#lamp-text").textContent = "Can't reach the station. Is the Pi on, and is this phone on the same network?";
  }
}

const start = location.hash.slice(1);
show(tabs.includes(start) ? start : "status");
poll();
setInterval(poll, 2000);
