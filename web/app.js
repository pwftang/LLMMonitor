import { Chart } from "./charts.js";

const view = document.getElementById("view");
const hubStatus = document.getElementById("hub-status");

/* ---------- formatting ---------- */
const fmtGB = (v) => (v == null ? "—" : `${v.toFixed(1)} GB`);
const fmtPct = (v) => (v == null ? "—" : `${(v * 100).toFixed(0)}%`);
const fmtPct100 = (v) => (v == null ? "—" : `${v.toFixed(0)}%`);
const fmtTemp = (v) => (v == null ? "—" : `${v.toFixed(1)}°C`);
const fmtTps = (v) => (v == null ? "—" : `${v.toFixed(1)} t/s`);
const tokParts = (v) =>
  v == null ? null
  : v >= 1e6 ? [(v / 1e6).toFixed(2), "M"]
  : v >= 1e3 ? [(v / 1e3).toFixed(1), "k"]
  : [`${Math.round(v)}`, ""];
const fmtTokens = (v) => { const p = tokParts(v); return p == null ? "—" : p[0] + p[1]; };
const fmtAxisTokens = (v) => (v >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `${(v / 1e3).toFixed(0)}k` : `${Math.round(v)}`);
const slug = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
const ago = (ts) => {
  if (!ts) return "never";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return `${Math.round(s)}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
};
const val = (obj, key) => (obj && obj[key] != null ? obj[key] : null);
// macmon can die while omlx keeps the device "online"; flag it so the
// (frozen) sys.* numbers aren't mistaken for live data.
function macmonWarn(el, d) {
  const down = d.online && d.has_macmon && d.macmon_ok === false;
  el.hidden = !down;
  if (down) {
    el.title = d.macmon_last_ok
      ? `macmon last responded ${ago(d.macmon_last_ok)} — system & power stats are stale`
      : "macmon has not responded since the hub started — system & power stats unavailable";
  }
  return down;
}

/* ---------- api ---------- */
async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
}

let timers = [];
let routeGen = 0;
function clearTimers() {
  for (const t of timers) clearInterval(t);
  timers = [];
}
function every(ms, fn, gen) {
  // gen guards against async renders registering timers after the user
  // navigated away: a stale render must never create new intervals.
  if (gen !== routeGen) return;
  fn();
  timers.push(setInterval(fn, ms));
}
const stale = (gen) => gen !== routeGen;

function setStatus(text, isErr = false) {
  hubStatus.textContent = text;
  hubStatus.classList.toggle("err", isErr);
}

/* ---------- routing ---------- */
const RANGES = ["1h", "3h", "6h", "24h", "7d", "30d"];
function route() {
  clearTimers();
  disposeCharts();
  routeGen += 1;
  // #/device/<id> or #/device/<id>/<range> (range defaults to 1h)
  const m = location.hash.match(/^#\/device\/([^/]+?)(?:\/(\w+))?\/?$/);
  if (m) {
    const range = m[2] && RANGES.includes(m[2]) ? m[2] : "1h";
    renderDetail(decodeURIComponent(m[1]), range);
  } else renderOverview();
}
window.addEventListener("hashchange", route);

/* ---------- overview ---------- */
let liveCharts = [];
function disposeCharts() {
  for (const c of liveCharts) c.dispose();
  liveCharts = [];
}

const LIVE_GROUPS = [
  ["inference", "activity", [
    ["generation", (s, llm) => fmtTps(val(llm, "llm.gen_tps"))],
    ["prefill", (s, llm) => fmtTps(val(llm, "llm.prefill_tps"))],
    ["cached tokens", (s, llm) => fmtTokens(val(llm, "llm.cached_tokens"))],
    ["cache eff", (s, llm) => fmtPct100(val(llm, "llm.cache_eff"))],
    ["requests", (s, llm) => `${num0(val(llm, "llm.active_reqs"))} active · ${num0(val(llm, "llm.waiting_reqs"))} waiting`],
  ]],
  ["memory", "memory", [
    ["model memory", (s, llm) => fmtGB(val(llm, "llm.model_mem_gb"))],
    ["ram", (s, llm, d) => (d.system ? `${fmtGB(val(s, "sys.ram_used_gb"))} / ${fmtGB(val(s, "sys.ram_total_gb"))}` : "—")],
  ]],
  ["system", "cpu", [
    ["cpu / gpu", (s) => `${fmtPct(val(s, "sys.cpu_util"))} / ${fmtPct(val(s, "sys.gpu_util"))}`],
    ["temps", (s) => `${fmtTemp(val(s, "sys.cpu_temp_c"))} / ${fmtTemp(val(s, "sys.gpu_temp_c"))}`],
    ["last seen", (s, llm, d) => (d.online ? "now" : ago(d.last_seen))],
  ]],
];

// Inserted between "inference" and "memory" only for comfyui-enabled devices.
const COMFY_GROUP = ["comfyui", "layers", [
  ["queue", (s, llm, d) => {
    const c = comfyOf(d);
    if (c) return `${Math.round(c.queue_running ?? 0)} running · ${Math.round(c.queue_pending ?? 0)} queued`;
    return d.comfy_ok === false ? "unreachable" : "—";
  }],
  ["model memory", (s, llm, d) => {
    const c = comfyOf(d);
    return c ? fmtGB(c.mem_gb) : "—";
  }],
  ["version", (s, llm, d) => comfyOf(d)?.version || "—"],
]];

/* ---------- oMLX-style bits ---------- */
const gbHTML = (v, dp) => (v == null ? "—" : `${v.toFixed(dp)}<span class="unit">GB</span>`);
const BIG_STATS = [
  ["gen", "generation", (s, llm) => bigHTML(val(llm, "llm.gen_tps"), "t/s", 1)],
  ["mem", "system memory", (s) => bigHTML(val(s, "sys.ram_used_gb"), "GB", 0)],
  ["mdl", "model memory", (s, llm) => gbHTML(val(llm, "llm.model_mem_gb"), 1)],
];
const BIG_STATS_BY_KEY = Object.fromEntries(BIG_STATS.map(([k, label, fn]) => [k, { label, val: fn }]));
const bigHTML = (v, unit, dp = 1) => (v == null ? "—" : `${v.toFixed(dp)}<span class="unit">${unit}</span>`);
const bigStat = (key, label) => `<div class="big-card"><span class="kicker">${label}</span><span class="big"></span></div>`;

function liveModels(d) {
  const base = (Array.isArray(d.raw?.omlx_models) ? d.raw.omlx_models : []).filter(isLiveModel);
  const act = d.raw?.omlx_stats?.active_models?.models;
  if (!Array.isArray(act)) return base;
  const byId = new Map(act.map((a) => [modelName(a), a]));
  const merged = base.map((m) => ({ ...m, ...byId.get(modelName(m)) }));
  for (const a of act) {
    const id = modelName(a);
    if (!base.some((m) => modelName(m) === id)) merged.push({ loaded: true, ...a });
  }
  return merged;
}

function modelActivity(m) {
  if (!m.loaded && m.is_loading) return { dot: "loading", cls: "st-pp", text: "loading" };
  const pp = Array.isArray(m.prefilling) ? m.prefilling.length : 0;
  const reqs = m.active_requests ?? null;
  if (pp) return { dot: "pp", cls: "st-pp", text: reqs ? `${pp} PP · ${reqs} req` : `${pp} PP` };
  if (reqs) {
    const gen = Array.isArray(m.generating) && m.generating.length > 0;
    return { dot: gen ? "gen" : "ok", cls: "st-ok", text: `${reqs} req` };
  }
  if (Array.isArray(m.prefilling) || reqs === 0 || m.idle_seconds != null) {
    let text = "idle";
    if (m.idle_seconds != null) text += ` ${fmtDur(m.idle_seconds)}`;
    if (m.ttl_remaining_seconds != null) text += ` · ~${fmtDur(m.ttl_remaining_seconds)} left`;
    return { dot: "", cls: "st-idle", text };
  }
  return { dot: "ok", cls: "", text: "active" };
}

function modelSubsHTML(m) {
  const tail = (parts) => {
    const s = parts.filter(Boolean).join(" · ");
    return s ? ` · ${s}` : "";
  };
  const subs = [];
  for (const p of m.prefilling || []) {
    const pct = p.total > 0 ? `${Math.min(100, Math.round((p.processed / p.total) * 100))}%` : null;
    const spd = p.speed != null ? `${num0(Math.round(p.speed))} t/s` : null;
    const eta = p.eta != null ? `eta ${fmtDur(p.eta)}` : null;
    subs.push(`<div class="m-sub"><span class="status-dot pp"></span>prompt processing${tail([pct, spd, eta])}</div>`);
  }
  for (const g of m.generating || []) {
    const tok = g.generated_tokens != null ? `${num0(g.generated_tokens)} tok` : null;
    const tps = g.tokens_per_second != null ? `${g.tokens_per_second.toFixed(1)} t/s` : null;
    const el = g.elapsed_seconds != null ? fmtDur(g.elapsed_seconds) : null;
    subs.push(`<div class="m-sub"><span class="status-dot gen"></span>generating${tail([tok, tps, el])}</div>`);
  }
  for (const w of m.waiting || []) {
    const pos = w.queue_position != null ? `#${w.queue_position}` : null;
    const el = w.elapsed_seconds != null ? fmtDur(w.elapsed_seconds) : null;
    subs.push(`<div class="m-sub"><span class="status-dot warn"></span>waiting${tail([pos, el])}</div>`);
  }
  return subs.join("");
}

function modelRowsHTML(models, detail = false) {
  if (!models.length) return `<div class="rows"><div class="m-none">no models loaded</div></div>`;
  return `<div class="rows">${models
    .map((m) => {
      const act = modelActivity(m);
      const flags = modelFlags(m);
      const row = `<div class="row">
        <div class="m-main">
          <span class="m-name">${esc(modelName(m))}</span>
          ${flags !== "—" ? `<span class="m-meta">${esc(flags)}</span>` : ""}
        </div>
        <div class="m-right">
          <span class="m-state ${act.cls}"><span class="status-dot ${act.dot}"></span>${esc(act.text)}</span>
          <span class="m-size">${modelMem(m)}</span>
        </div>
      </div>`;
      if (!detail) return row;
      const subs = modelSubsHTML(m);
      return `<div class="row-wrap">${row}${subs ? `<div class="m-subs">${subs}</div>` : ""}</div>`;
    })
    .join("")}</div>`;
}

function availableModels(d) {
  const all = Array.isArray(d.raw?.omlx_models) ? d.raw.omlx_models : [];
  return all.filter((m) => !isLiveModel(m));
}

function availRowsHTML(models) {
  if (!models.length) return `<div class="rows"><div class="m-none">no models available</div></div>`;
  return `<div class="rows">${models
    .map((m) => `<div class="row">
      <div class="m-main">
        <span class="m-name">${esc(modelName(m))}</span>
        ${m.engine_type ? `<span class="m-meta">${esc(m.engine_type)}</span>` : ""}
      </div>
      <div class="m-right">
        <span class="m-size">${modelMem(m)}</span>
      </div>
    </div>`)
    .join("")}</div>`;
}

function hwLineHTML(s, llm) {
  if (!s || Object.keys(s).length === 0) return "";
  const wait = val(llm, "llm.waiting_reqs");
  return wait != null && wait > 0 ? `${num0(wait)} queued` : "";
}

// ComfyUI is opt-in per device; when unreachable the poller drops raw.comfyui
// entirely (like stale macmon), so "no payload + comfy_ok false" = down.
const comfyOf = (d) => (d && d.raw && d.raw.comfyui ? d.raw.comfyui : null);

/* ---------- icons + theme ---------- */
const ICONS = {
  zap: '<path d="M13 2 3 14h7l-1 8 10-12h-7l1-8z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/>',
  activity: '<path d="M3 12h4l3-8 4 16 3-8h4" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>',
  thermometer: '<path d="M10 4a2 2 0 0 1 4 0v8.5a4.5 4.5 0 1 1-4 0z" fill="none" stroke="currentColor" stroke-width="1.7"/><circle cx="12" cy="16.5" r="1.6" fill="currentColor"/>',
  memory: '<rect x="3" y="8" width="18" height="8" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M7 8V5.5M11 8V5.5M15 8V5.5M7 16v2.5M15 16v2.5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>',
  cpu: '<rect x="5" y="5" width="14" height="14" rx="2" fill="none" stroke="currentColor" stroke-width="1.7"/><rect x="9.5" y="9.5" width="5" height="5" rx="1" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M9 5V2.5M15 5V2.5M9 21.5V19M15 21.5V19M5 9H2.5M5 15H2.5M21.5 9H19M21.5 15H19" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>',
  database: '<ellipse cx="12" cy="5.5" rx="7.5" ry="2.8" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M4.5 5.5v13c0 1.55 3.36 2.8 7.5 2.8s7.5-1.25 7.5-2.8v-13M4.5 12c0 1.55 3.36 2.8 7.5 2.8s7.5-1.25 7.5-2.8" fill="none" stroke="currentColor" stroke-width="1.7"/>',
  box: '<path d="M12 2.5 21 7.5v9l-9 5-9-5v-9l9-5z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M12 12 21 7.5M12 12 3 7.5m9 4.5V21.5" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/>',
  layers: '<rect x="3" y="7" width="13" height="13" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M7 7V5a1.5 1.5 0 0 1 1.5-1.5H18A2.5 2.5 0 0 1 20.5 6v9.5a1.5 1.5 0 0 1-1.5 1.5h-2" fill="none" stroke="currentColor" stroke-width="1.7"/>',
  fan: '<circle cx="12" cy="12" r="1.8" fill="currentColor"/><path d="M12 10c-1-2 .5-4.5.5-4.5C16 5.5 17 9 13 10m2 1.5c2.2-.5 4.5 1.5 4.5 1.5-1 3.5-4.7 3.1-6 .5m-3.5-.5c-1.2 2-4.6 2-4.6 2 0-3.6 3.3-4.9 5.3-3.6" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>',
  sun: '<circle cx="12" cy="12" r="4.2" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M18.4 5.6 17 7M7 17l-1.4 1.4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>',
  moon: '<path d="M20 14.5A8.5 8.5 0 0 1 9.5 4 8.5 8.5 0 1 0 20 14.5z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/>',
  ext: '<path d="M13.5 4H20v6.5M20 4 10.8 13.2M9 5H6.5A2.5 2.5 0 0 0 4 7.5v10A2.5 2.5 0 0 0 6.5 20h10a2.5 2.5 0 0 0 2.5-2.5V15" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>',
};
const icon = (name) =>
  `<svg viewBox="0 0 24 24"${name === "sun" || name === "moon" ? ' fill="none"' : ""} aria-hidden="true">${ICONS[name] || ICONS.box}</svg>`;
function panelIcon(title) {
  const t = title.toLowerCase();
  if (t.includes("power")) return icon("zap");
  if (t.includes("temperature")) return icon("thermometer");
  if (t.includes("ram")) return icon("memory");
  if (t.includes("utilisation") || t.includes("usage")) return icon("cpu");
  if (t.includes("memory") || t.includes("kv cache")) return icon("memory");
  if (t.includes("throughput")) return icon("activity");
  if (t.includes("requests")) return icon("layers");
  if (t.includes("cache")) return icon("database");
  if (t.includes("fan")) return icon("fan");
  return icon("activity");
}

function applyThemeIcon() {
  const btn = document.getElementById("theme-toggle");
  if (btn)
    btn.innerHTML = document.documentElement.dataset.theme === "dark" ? icon("sun") : icon("moon");
}
function toggleTheme() {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("llmmonitor-theme", next);
  applyThemeIcon();
  for (const c of liveCharts) c.draw();
}

async function renderOverview() {
  clearTimers();
  disposeCharts();
  const gen = routeGen;
  let devices = [];
  try {
    devices = await api("/api/devices");
    const online = devices.filter((d) => d.online).length;
    setStatus(`${online}/${devices.length} online · ${new Date().toLocaleTimeString()}`);
  } catch (e) {
    setStatus(`hub unreachable: ${e.message}`, true);
  }
  if (stale(gen)) return;

  view.innerHTML = `<div class="grid"></div>`;
  const grid = view.firstElementChild;
  const sparks = [];
  const updaters = new Map();

  for (const d of devices) {
    const card = document.createElement("div");
    card.className = "card" + (d.online ? "" : " offline");
    card.onclick = () => (location.hash = `#/device/${encodeURIComponent(d.id)}`);

    card.innerHTML = `
      <div class="card-head">
        <span class="status-dot"></span>
        <div class="card-title">
          <h2>${esc(d.name)} <span class="omlx-ver"></span></h2>
          <div class="card-ips"></div>
        </div>
        ${d.omlx_admin_url
          ? `<a class="icon-btn card-admin" href="${esc(d.omlx_admin_url)}" target="_blank" rel="noopener noreferrer" title="open omlx admin dashboard" aria-label="open omlx admin dashboard">${icon("ext")}</a>`
          : ""}
        <span class="macmon-warn" hidden>macmon unreachable</span>
        <span class="card-state"></span>
      </div>
      <div class="card-body">
        <div class="ov-sec">
          <div class="ov-sec-head">${icon("box")}<span class="kicker">models</span></div>
          <div class="models-zone"></div>
        </div>
        <div class="ov-sec">
          <div class="ov-sec-head">${icon("activity")}<span class="kicker">inference</span></div>
          <div class="i-stats">
            <div class="stat"><span class="label">prompt processing</span><span class="value i-val" data-i="pp">—</span></div>
            <div class="stat"><span class="label">token generation</span><span class="value i-val" data-i="gen">—</span></div>
            <div class="stat"><span class="label">kv cache</span><span class="value i-val" data-i="kv">—</span></div>
            <div class="stat"><span class="label">cache efficiency</span><span class="value i-val" data-i="eff">—</span></div>
            <div class="stat"><span class="label">total prefill tokens</span><span class="value i-val" data-i="ptok">—</span></div>
          </div>
        </div>
        <div class="ov-sec sec-sys">
          <div class="ov-sec-head">${icon("cpu")}<span class="kicker">system</span></div>
          <div class="mbar-line">
            <span class="kicker">ram used</span>
            <span class="mbar-val"></span>
          </div>
          <div class="mbar mbar-use"><div class="mbar-fill" style="width:0%"></div></div>
          <canvas class="spark spark-use" id="spark-ram-${d.id}"></canvas>
          <div class="mbar-line"><span class="kicker">cpu</span><span class="mbar-val cpu-val"></span></div>
          <div class="mbar mbar-use"><div class="mbar-fill cpu-fill" style="width:0%"></div></div>
          <canvas class="spark spark-use" id="spark-cpu-${d.id}"></canvas>
          <div class="mbar-line"><span class="kicker">gpu</span><span class="mbar-val gpu-val"></span></div>
          <div class="mbar mbar-use"><div class="mbar-fill gpu-fill" style="width:0%"></div></div>
          <canvas class="spark spark-use" id="spark-gpu-${d.id}"></canvas>
          <div class="hw-line"></div>
          <span class="ago"></span>
        </div>
        <div class="ov-sec sec-pwr">
          <div class="ov-sec-head">${icon("zap")}<span class="kicker">power</span></div>
          <div class="i-stats">
            <div class="stat"><span class="label">power draw</span><span class="value i-val" data-p="pwr">—</span></div>
            <div class="stat"><span class="label">cpu power</span><span class="value i-val" data-p="cpu">—</span></div>
            <div class="stat"><span class="label">gpu power</span><span class="value i-val" data-p="gpu">—</span></div>
            <div class="stat"><span class="label">fan speed</span><span class="value i-val" data-p="fan">—</span></div>
          </div>
        </div>
        ${d.has_comfyui ? `
        <div class="ov-sec sec-comfy">
          <div class="ov-sec-head">${icon("layers")}<span class="kicker">comfyui</span></div>
          <div class="i-stats">
            <div class="stat"><span class="label">running</span><span class="value i-val" data-c="run">—</span></div>
            <div class="stat"><span class="label">queued</span><span class="value i-val" data-c="pend">—</span></div>
            <div class="stat"><span class="label">model memory</span><span class="value i-val" data-c="mem">—</span></div>
          </div>
        </div>` : ""}
      </div>
    `;
    grid.appendChild(card);

    const adminLink = card.querySelector(".card-admin");
    if (adminLink) adminLink.addEventListener("click", (e) => e.stopPropagation());

    const dotEl = card.querySelector(".status-dot");
    const stateEl = card.querySelector(".card-state");
    const verEl = card.querySelector(".omlx-ver");
    const ipEl = card.querySelector(".card-ips");
    const macmonWarnEl = card.querySelector(".macmon-warn");
    const modelsEl = card.querySelector(".models-zone");
    const iEls = {};
    for (const el of card.querySelectorAll(".i-val[data-i]")) iEls[el.dataset.i] = el;
    const pEls = {};
    for (const el of card.querySelectorAll(".i-val[data-p]")) pEls[el.dataset.p] = el;
    const cEls = {};
    for (const el of card.querySelectorAll(".i-val[data-c]")) cEls[el.dataset.c] = el;
    const ramValEl = card.querySelector(".mbar-val");
    const ramFillEl = card.querySelector(".mbar-fill");
    const cpuValEl = card.querySelector(".cpu-val");
    const cpuFillEl = card.querySelector(".cpu-fill");
    const gpuValEl = card.querySelector(".gpu-val");
    const gpuFillEl = card.querySelector(".gpu-fill");
    const hwEl = card.querySelector(".hw-line");
    const agoEl = card.querySelector(".ago");
    let lastRows = "";
    let lastIPs = "";
    const updateCard = (fresh) => {
      const s = fresh.system || {}, llm = fresh.llm || {};
      const pressure = fresh.pressure_level;
      let state;
      if (!fresh.online) state = ["", "offline"];
      else if (pressure === "critical") state = ["crit", "critical"];
      else if (pressure === "warning") state = ["warn", "warning"];
      else state = ["ok", pressure || "online"];
      dotEl.className = `status-dot ${state[0]}`;
      stateEl.textContent = state[1];
      stateEl.className = `card-state st-${state[0] || "idle"}`;
      card.classList.toggle("sys-stale", macmonWarn(macmonWarnEl, fresh));
      verEl.textContent = fresh.omlx_version ? `omlx ${fresh.omlx_version}` : "";
      const ipBits = [];
      if (fresh.tailscale_ip) ipBits.push(`<span class="cip"><b>ts</b>&hairsp;${esc(fresh.tailscale_ip)}</span>`);
      if (fresh.local_ip) ipBits.push(`<span class="cip"><b>lan</b>&hairsp;${esc(fresh.local_ip)}</span>`);
      const ipHTML = ipBits.join("");
      if (ipHTML !== lastIPs) {
        lastIPs = ipHTML;
        ipEl.innerHTML = ipHTML;
      }

      const rows = modelRowsHTML(liveModels(fresh));
      if (rows !== lastRows) {
        lastRows = rows;
        modelsEl.innerHTML = rows;
      }

      const big = (el, num, unit) => {
        el.innerHTML = num == null ? "—" : unit ? `${num}<span class="unit">${unit}</span>` : num;
      };
      const gt = val(llm, "llm.gen_tps");
      const pp = val(llm, "llm.prefill_tps"), eff = val(llm, "llm.cache_eff");
      big(iEls.pp, pp == null ? null : pp.toFixed(1), "t/s");
      big(iEls.gen, gt == null ? null : gt.toFixed(1), "t/s");
      big(iEls.kv, ...(tokParts(val(llm, "llm.cached_tokens")) || [null, ""]));
      big(iEls.eff, eff == null ? null : eff.toFixed(0), "%");
      big(iEls.ptok, ...(tokParts(val(llm, "llm.prompt_tokens")) || [null, ""]));

      const watts = (v) => (v == null ? null : v.toFixed(1));
      big(pEls.pwr, watts(val(s, "sys.sys_power_w")), "W");
      big(pEls.cpu, watts(val(s, "sys.cpu_power_w")), "W");
      big(pEls.gpu, watts(val(s, "sys.gpu_power_w")), "W");
      const fan = val(s, "sys.fan_rpm_max");
      big(pEls.fan, fan == null ? null : Math.round(fan).toLocaleString(), "rpm");

      const setBar = (valEl, fillEl, pct, text) => {
        valEl.textContent = pct == null ? "—" : text;
        fillEl.style.width = pct == null ? "0%" : `${Math.min(100, pct).toFixed(1)}%`;
        fillEl.className = `mbar-fill${pct == null ? "" : pct > 92 ? " crit" : pct > 80 ? " warn" : ""}`;
      };
      const used = val(s, "sys.ram_used_gb"), total = val(s, "sys.ram_total_gb");
      const pct = used != null && total ? (used / total) * 100 : null;
      setBar(ramValEl, ramFillEl, pct, `${(used || 0).toFixed(0)} / ${(total || 0).toFixed(0)} GB`);
      const cpu = val(s, "sys.cpu_util"), gpu = val(s, "sys.gpu_util");
      const cTemp = val(s, "sys.cpu_temp_c"), gTemp = val(s, "sys.gpu_temp_c");
      const withTemp = (pctText, t) => (pctText === "—" || t == null ? pctText : `${pctText} · ${fmtTemp(t)}`);
      setBar(cpuValEl, cpuFillEl, cpu != null ? cpu * 100 : null, withTemp(fmtPct(cpu), cTemp));
      setBar(gpuValEl, gpuFillEl, gpu != null ? gpu * 100 : null, withTemp(fmtPct(gpu), gTemp));

      const c = comfyOf(fresh);
      if (cEls.run) {
        if (c) {
          cEls.run.textContent = Math.round(c.queue_running ?? 0);
          cEls.pend.textContent = Math.round(c.queue_pending ?? 0);
          cEls.mem.innerHTML = c.mem_gb == null ? "—" : gbHTML(c.mem_gb, 1);
        } else {
          const down = fresh.comfy_ok === false;
          cEls.run.textContent = down ? "unreachable" : "—";
          cEls.pend.textContent = "—";
          cEls.mem.textContent = "—";
        }
      }

      hwEl.innerHTML = hwLineHTML(s, llm);
      agoEl.textContent = fresh.online ? "" : `last seen ${ago(fresh.last_seen)}`;
    };
    updaters.set(d.id, updateCard);
    updateCard(d);

    const sparkDefs = [
      { cid: `spark-ram-${d.id}`, metric: "sys.ram_used_pct", mul: 100, colorVar: "--chart-2", ymax: 100 },
      { cid: `spark-cpu-${d.id}`, metric: "sys.cpu_util", mul: 100, colorVar: "--chart-4", ymax: 100 },
      { cid: `spark-gpu-${d.id}`, metric: "sys.gpu_util", mul: 100, colorVar: "--chart-5", ymax: 100 },
    ];
    const cardSparks = [];
    for (const def of sparkDefs) {
      const chart = new Chart(card.querySelector(`#${CSS.escape(def.cid)}`), {
        axes: false,
        crosshair: false,
        legend: false,
        fill: def.fill || "gradient",
        fmt: def.fmt || fmtPct100,
        frame: true,
        ymax: def.ymax,
      });
      liveCharts.push(chart);
      cardSparks.push([def.metric, def.mul, def.colorVar || null, chart]);
    }
    sparks.push([d.id, cardSparks]);
  }

  // sparkline data: 1h of cpu/gpu/ram usage, refreshed every 30s
  const SPARK_METRICS = ["sys.cpu_util", "sys.gpu_util", "sys.ram_used_pct"];
  const refreshSparks = async () => {
    for (const [id, cardSparks] of sparks) {
      try {
        const h = await api(`/api/devices/${id}/history?metrics=${SPARK_METRICS.join(",")}&range=1h`);
        for (const [metric, mul, colorVar, chart] of cardSparks) {
          const data = (h.series[metric] || []).map(([t, v]) => [t, v * mul]);
          chart.setSeries([{ label: metric, data, colorVar }]);
        }
      } catch { /* device may be offline */ }
    }
  };
  every(30000, refreshSparks, gen);
  every(3000, async () => {
    if (location.hash.startsWith("#/device")) return;
    try {
      const fresh = await api("/api/devices");
      // only a device joining/leaving or flipping online warrants a full rebuild;
      // pressure-level changes are painted in place by updateCard
      const structural = (list) =>
        JSON.stringify(list.map((d) => [d.id, d.online]));
      if (structural(fresh) !== structural(devices)) {
        renderOverview();
        return;
      }
      devices = fresh;
      const online = fresh.filter((d) => d.online).length;
      setStatus(`${online}/${fresh.length} online · ${new Date().toLocaleTimeString()}`);
      for (const d of fresh) updaters.get(d.id)?.(d);
    } catch (e) {
      setStatus(`hub unreachable: ${e.message}`, true);
    }
  }, gen);
}

/* ---------- detail ---------- */
async function renderDetail(id, initialRange = "1h") {
  let range = initialRange;
  let lastModelsHTML = "";
  let lastAvailHTML = "";
  const gen = routeGen;
  disposeCharts();
  view.innerHTML = `<div class="empty">loading…</div>`;

  const panels = [
    { title: "Power", metrics: ["sys.sys_power_w", "sys.cpu_power_w", "sys.gpu_power_w"], labels: ["system", "cpu", "gpu"], macmon: true, fmt: (v) => `${v.toFixed(1)} W` },
    { title: "CPU usage", metrics: ["sys.cpu_util"], labels: ["cpu"], pct: true, macmon: true, fmt: (v) => `${v.toFixed(0)}%` },
    { title: "GPU usage", metrics: ["sys.gpu_util"], labels: ["gpu"], pct: true, macmon: true, fmt: (v) => `${v.toFixed(0)}%` },
    { title: "CPU temperature", metrics: ["sys.cpu_temp_c"], labels: ["cpu"], macmon: true, fmt: (v) => `${v.toFixed(1)}°C` },
    { title: "GPU temperature", metrics: ["sys.gpu_temp_c"], labels: ["gpu"], macmon: true, fmt: (v) => `${v.toFixed(1)}°C` },
    { title: "RAM utilisation", metrics: ["sys.ram_used_pct"], labels: ["ram"], pct: true, macmon: true, fmt: (v) => `${v.toFixed(0)}%` },
    { title: "Memory", metrics: ["sys.ram_used_gb", "llm.model_mem_gb"], labels: ["ram used", "models"], dynamic: "models", fmt: (v) => `${v.toFixed(1)} GB` },
    { title: "Token throughput", metrics: ["llm.prefill_tps", "llm.gen_tps"], labels: ["prefill", "generation"], omlx: true, fmt: (v) => `${v.toFixed(1)} t/s` },
    { title: "KV cache", metrics: ["llm.cached_tokens"], labels: ["cached tokens"], omlx: true, fmt: fmtAxisTokens },
    { title: "Cache efficiency", metrics: ["llm.cache_eff"], labels: ["hit rate"], omlx: true, fmt: (v) => `${v.toFixed(0)}%` },
    { title: "Requests", metrics: ["llm.active_reqs", "llm.waiting_reqs"], labels: ["active", "waiting"], omlx: true, fmt: (v) => v.toFixed(0) },
    { title: "ComfyUI queue", metrics: ["comfyui.queue_running", "comfyui.queue_pending"], labels: ["running", "queued"], comfy: true, fmt: (v) => v.toFixed(0) },
    { title: "ComfyUI model memory", metrics: ["comfyui.model_mem_gb"], labels: ["models"], comfy: true, fmt: (v) => `${v.toFixed(1)} GB` },
    { title: "Fan", metrics: ["sys.fan_rpm_max"], labels: ["rpm"], macmon: true, fmt: (v) => `${v.toLocaleString()} rpm` },
  ];

  async function build() {
    const d = await api(`/api/devices/${id}/latest`);
    const head = `
      <div class="detail-head">
        <a class="back" href="#">&larr; all devices</a>
        <h2>${esc(d.name)}</h2>
        <span class="d-state"><span class="status-dot"></span><span class="d-state-text"></span></span>
        <span class="macmon-warn" hidden>macmon unreachable</span>
        <div class="segments">${RANGES
          .map((r) => `<button data-r="${r}" class="${r === range ? "active" : ""}">${r}</button>`)
          .join("")}</div>
      </div>
      <div class="stat-cards" id="big"></div>
      <div class="panel speed-panel">
        <div class="panel-head">${icon("zap")}<span class="kicker">Throughput</span></div>
        <div class="speed-grid">
          <div><span class="kicker">prefill</span><span class="speed-val" id="sp-pre"></span></div>
          <div><span class="kicker">generation</span><span class="speed-val" id="sp-gen"></span></div>
        </div>
      </div>
      <div class="live-groups" id="live"></div>
      <div class="panels" id="panels"></div>
      <div class="panel wide" style="margin-top:14px">
        <div class="panel-head">${icon("box")}<span class="kicker">Loaded models</span></div>
        <div class="panel-body" id="models-table"></div>
      </div>
      <div class="panel wide" style="margin-top:14px">
        <div class="panel-head">${icon("database")}<span class="kicker">Available models</span></div>
        <div class="panel-body" id="avail-models"></div>
      </div>`;
    view.innerHTML = head;
    view.querySelector("#big").innerHTML = BIG_STATS.map(([key, label]) => bigStat(key, label)).join("");
    const liveGroups = [...LIVE_GROUPS];
    if (d.has_comfyui) liveGroups.splice(1, 0, COMFY_GROUP);
    view.querySelector("#live").innerHTML = liveGroups.map(([title, iconName, stats]) =>
      `<div class="panel live-group"><div class="panel-head">${icon(iconName)}<span class="kicker">${title}</span></div><div class="live-group-body">${stats.map(([label]) => stat(label, "")).join("")}</div></div>`
    ).join("");
    view.querySelectorAll(".segments button").forEach((b) => {
      b.onclick = () => {
        range = b.dataset.r;
        view.querySelectorAll(".segments button").forEach((x) => x.classList.toggle("active", x === b));
        refreshCharts();
        // replaceState, not location.hash — assigning the hash would fire
        // hashchange and rebuild the whole view
        history.replaceState(null, "", `#/device/${encodeURIComponent(id)}/${range}`);
      };
    });

    const charts = {};
    const statsEls = {};
    const panelsDiv = view.querySelector("#panels");
    for (const p of panels) {
      const div = document.createElement("div");
      div.className = "panel";
      let right = "";
      if (d.has_omlx === false && p.omlx) right = `<span class="head-right">no oMLX</span>`;
      else if (d.has_comfyui === false && p.comfy) right = `<span class="head-right">no ComfyUI</span>`;
      div.innerHTML = `<div class="panel-head">${panelIcon(p.title)}<span class="kicker">${p.title}</span>${right}</div><div class="panel-body"><canvas></canvas><div class="chart-stats"></div></div>`;
      panelsDiv.appendChild(div);
      charts[p.title] = new Chart(div.querySelector("canvas"), { fmt: p.fmt });
      liveCharts.push(charts[p.title]);
      statsEls[p.title] = div.querySelector(".chart-stats");
    }

    return { d, charts, statsEls, groups: liveGroups };
  }

  let ctx;
  try {
    ctx = await build();
  } catch (e) {
    if (!stale(gen))
      view.innerHTML = `<div class="flash-err">failed to load device: ${esc(e.message)}</div>`;
    return;
  }
  if (stale(gen)) return;

  // DOM writes are in-place (textContent) except the model rows, which are
  // rewritten only when their HTML actually changes — wholesale innerHTML
  // rebuilds every poll tick drove unbounded layout/GC churn in Firefox.
  async function refreshLive() {
    try {
      const d = await api(`/api/devices/${id}/latest`);
      const s = d.system || {}, llm = d.llm || {};

      const pressure = d.pressure_level;
      let state;
      if (!d.online) state = ["", "offline"];
      else if (pressure === "critical") state = ["crit", "critical"];
      else if (pressure === "warning") state = ["warn", "warning"];
      else state = ["ok", pressure || "online"];
      const dot = view.querySelector(".d-state .status-dot");
      const txt = view.querySelector(".d-state-text");
      if (dot) dot.className = `status-dot ${state[0]}`;
      if (txt) {
        txt.textContent = state[1];
        txt.className = `d-state-text st-${state[0] || "idle"}`;
      }
      const macmonWarnEl = view.querySelector(".macmon-warn");
      if (macmonWarnEl) macmonWarn(macmonWarnEl, d);

      const bigCells = view.querySelectorAll("#big .big");
      if (bigCells.length) {
        BIG_STATS.forEach(([key], i) => {
          if (bigCells[i]) bigCells[i].innerHTML = BIG_STATS_BY_KEY[key].val(s, llm);
        });
      }

      const pre = val(llm, "llm.prefill_tps"), genV = val(llm, "llm.gen_tps");
      const setSpeed = (elId, v) => {
        const el = view.querySelector(elId);
        if (el) el.innerHTML = v == null ? "—" : `${v.toFixed(1)}<span class="unit">t/s</span>`;
      };
      setSpeed("#sp-pre", pre);
      setSpeed("#sp-gen", genV);

      const cells = view.querySelectorAll("#live .stat .value");
      if (cells.length) {
        let i = 0;
        for (const [, , stats] of ctx.groups) {
          for (const [, fn] of stats) {
            if (cells[i]) cells[i].textContent = fn(s, llm, d);
            i++;
          }
        }
      }

      const tbl = view.querySelector("#models-table");
      if (tbl) {
        const html = modelRowsHTML(liveModels(d), true);
        if (html !== lastModelsHTML) {
          lastModelsHTML = html;
          tbl.innerHTML = html;
        }
      }

      const avail = view.querySelector("#avail-models");
      if (avail) {
        const html = availRowsHTML(availableModels(d));
        if (html !== lastAvailHTML) {
          lastAvailHTML = html;
          avail.innerHTML = html;
        }
      }
    } catch {
      // keep previous values on transient error
    }
  }

  async function refreshCharts() {
    // per-model memory series are derived from the current model list
    const d = await api(`/api/devices/${id}/latest`).catch(() => null);
    const modelMetrics = [];
    if (d && Array.isArray(d.raw?.omlx_models)) {
      for (const m of d.raw.omlx_models.filter(isLiveModel)) {
        const name = modelName(m);
        if (name) modelMetrics.push([`llm.model.${slug(name)}.mem_gb`, shortName(name)]);
      }
    }
    const wanted = new Set();
    for (const p of panels) {
      p.metrics.forEach((m) => wanted.add(m));
      if (p.dynamic === "models") modelMetrics.forEach(([m]) => wanted.add(m));
    }
    const all = [...wanted];
    const h = await api(`/api/devices/${id}/history?metrics=${all.join(",")}&range=${range}`);
    const series = h.series;
    const bands = h.bands || {};
    const scaleBand = (b, mul) => (b == null ? null : {
      min: b.min.map(([t, v]) => [t, v * mul]),
      max: b.max.map(([t, v]) => [t, v * mul]),
    });
    for (const p of panels) {
      const mul = p.pct ? 100 : 1;
      const list = p.metrics.map((m, i) => {
        const s = {
          label: p.labels[i],
          data: mul !== 1 ? (series[m] || []).map(([t, v]) => [t, v * mul]) : series[m] || [],
        };
        const band = scaleBand(bands[m], mul);
        if (band) s.band = band;
        return s;
      });
      if (p.dynamic === "models") {
        for (const [m, label] of modelMetrics) {
          list.push({ label, data: series[m] || [] });
        }
      }
      ctx.charts[p.title].setSeries(list);
      const statsEl = ctx.statsEls[p.title];
      if (statsEl) statsEl.innerHTML = chartStatsHTML(list, p.fmt);
    }
  }

  try {
    await refreshLive();
    await refreshCharts();
  } catch (e) {
    if (!stale(gen))
      view.insertAdjacentHTML("afterbegin", `<div class="flash-err">${esc(e.message)}</div>`);
  }
  every(3000, refreshLive, gen);
  every(30000, () => { refreshCharts().catch(() => {}); }, gen);
}

/* ---------- small helpers ---------- */
function stat(label, value, cls = "") {
  return `<div class="stat"><div class="label">${label}</div><div class="value ${cls}">${value}</div></div>`;
}
function chartStatsHTML(list, fmt) {
  // per-series avg/peak over the currently displayed range; the chip index
  // spans every series (empty included) so its colour matches the chart line
  const chips = [];
  list.forEach((s, i) => {
    if (!s.data.length) return;
    let sum = 0, peak = -Infinity;
    for (const [, v] of s.data) { sum += v; if (v > peak) peak = v; }
    chips.push(
      `<span class="cs"><span class="cs-dot" style="background:var(--chart-${(i % 9) + 1})"></span>${esc(s.label)} <b>avg ${fmt(sum / s.data.length)} · peak ${fmt(peak)}</b></span>`
    );
  });
  return chips.join("");
}
function num0(v) {
  return v == null ? "—" : String(v);
}
function fmtDur(s) {
  if (s == null) return "";
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return m % 60 ? `${h}h ${m % 60}m` : `${h}h`;
}
function hotClass(v) {
  if (v == null) return "";
  return v > 0.92 ? "crit" : v > 0.8 ? "hot" : "";
}
function tempClass(v) {
  if (v == null) return "";
  return v > 90 ? "crit" : v > 75 ? "hot" : "";
}
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}
function modelName(m) {
  return m.id || m.model_id || m.model || m.name || "unknown";
}
function shortName(name) {
  const parts = String(name).split("/");
  return parts[parts.length - 1];
}
function modelField(m, keys) {
  for (const k of keys) if (m[k] != null) return m[k];
  return null;
}
function isLiveModel(m) {
  return m.loaded || m.is_loading;
}
function modelMem(m) {
  const v = modelField(m, ["actual_size", "memory_used", "memory_bytes", "memory", "ram_bytes", "size_bytes"]);
  return v == null ? "—" : `${(v / 1024 ** 3).toFixed(1)} GB`;
}
function modelFlags(m) {
  const flags = [];
  if (m.pinned || m.is_pinned) flags.push("pinned");
  if (m.is_default) flags.push("default");
  if (m.ttl_seconds) flags.push(`ttl ${m.ttl_seconds}s`);
  return flags.join(", ") || "—";
}

const themeToggle = document.getElementById("theme-toggle");
if (themeToggle) themeToggle.addEventListener("click", toggleTheme);
applyThemeIcon();
route();
