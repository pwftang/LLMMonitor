/* LLMMonitor rack dashboard — vanilla JS, no charts, no frameworks.
 *
 * Polls /api/rack (the hub's own aggregated snapshot; the hub makes zero
 * upstream calls for it) and patches text nodes in place. Panels are built
 * once per server-id set; refreshes only touch text/classes, so a Pi 3B
 * kiosk does negligible work per tick.
 */

const POLL_MS = 5000;

const serversEl = document.getElementById("rack-servers");
const overallEl = document.getElementById("rack-overall");
const routerEl = document.getElementById("rack-router");
const clockEl = document.getElementById("rack-clock");

/* ---------- formatting (values are never invented: null → "—") ---------- */
const dash = "—";
const f1 = (v) => (v == null ? dash : v.toFixed(1));
const f0 = (v) => (v == null ? dash : String(Math.round(v)));
const ago = (age) => {
  if (age == null) return "never";
  if (age < 90) return `${Math.round(age)}s`;
  if (age < 5400) return `${Math.round(age / 60)}m`;
  return `${Math.round(age / 3600)}h`;
};
const isStale = (age, staleAfter) => age == null || age > staleAfter;

/* ---------- panel construction (only when the server set changes) ------- */
let panels = new Map(); // id -> {root, refs}

function panelHTML() {
  const stat = (key) =>
    `<div class="stat"><span class="v" data-v="${key}">${dash}</span>` +
    `<span class="k" data-k="${key}"></span><span class="a" data-a="${key}"></span></div>`;
  return `
    <div class="srv-head">
      <span class="srv-name"></span>
      <span class="srv-badge"></span>
      <span class="srv-age"></span>
    </div>
    <div class="srv-model"><span class="k">model</span> <span data-f="model"></span></div>
    <div class="srv-stats">
      ${stat("reqs")}${stat("gen")}${stat("prefill")}${stat("mem")}
    </div>
    <div class="srv-foot">
      <span class="membar"><i></i></span>
      <span class="srv-mem-p"></span>
    </div>`;
}

function buildPanels(servers) {
  const ids = servers.map((s) => s.id);
  const same =
    ids.length === panels.size && ids.every((id, i) => panels.has(id));
  if (same) return;
  serversEl.textContent = "";
  panels = new Map();
  for (const s of servers) {
    const root = document.createElement("section");
    root.className = "srv";
    root.innerHTML = panelHTML();
    serversEl.appendChild(root);
    panels.set(s.id, {
      root,
      name: root.querySelector(".srv-name"),
      badge: root.querySelector(".srv-badge"),
      age: root.querySelector(".srv-age"),
      model: root.querySelector('[data-f="model"]'),
      membar: root.querySelector(".membar > i"),
      memP: root.querySelector(".srv-mem-p"),
      v: Object.fromEntries(
        ["reqs", "gen", "prefill", "mem"].map((k) => [
          k,
          root.querySelector(`[data-v="${k}"]`),
        ]),
      ),
      k: Object.fromEntries(
        ["reqs", "gen", "prefill", "mem"].map((k) => [
          k,
          root.querySelector(`[data-k="${k}"]`),
        ]),
      ),
      a: Object.fromEntries(
        ["reqs", "gen", "prefill", "mem"].map((k) => [
          k,
          root.querySelector(`[data-a="${k}"]`),
        ]),
      ),
    });
  }
}

function setStat(p, key, value, label, age, staleAfter, cls) {
  p.v[key].textContent = value;
  if (cls) p.v[key].className = `v ${cls}`;
  else p.v[key].className = "v";
  p.k[key].textContent = label;
  p.a[key].textContent = value === dash ? "" : ago(age);
  p.a[key].className = isStale(age, staleAfter) && value !== dash ? "a stale" : "a";
}

/* ---------- render one snapshot ---------- */
function render(data) {
  // Ages were computed at snapshot time; shift them by the time elapsed
  // since the fetch so re-renders between polls stay honest.
  const drift = Math.max(0, Date.now() / 1000 - data.generated_at);
  const adj = (a) => (a == null ? null : a + drift);
  const staleAfter = data.stale_after_s ?? 30;
  buildPanels(data.servers);

  for (const s of data.servers) {
    const p = panels.get(s.id);
    if (!p) continue;
    const seenAge = adj(s.last_seen_age_s);
    const llmAge = adj(s.active_requests_age_s);
    p.root.className =
      "srv" +
      (s.active ? " active" : "") +
      (s.status === "offline" ? " offline" : s.status === "stale" ? " stale" : "");
    p.name.textContent = s.name;

    const badgeText =
      s.status === "offline" ? "OFFLINE"
      : s.status === "stale" ? "STALE"
      : s.active ? "SERVING"
      : s.inference ? "IDLE"
      : "NO LLM";
    p.badge.textContent = badgeText;
    p.badge.className =
      "srv-badge" +
      (s.active ? "" : s.status === "offline" ? " offline"
       : s.status === "stale" ? " stale" : " idle");

    p.age.textContent = s.online || seenAge != null
      ? `seen ${ago(seenAge)} ago`
      : "never seen";
    p.age.className = "srv-age" + (isStale(seenAge, staleAfter) ? " stale" : "");

    p.model.textContent = s.loaded_model
      ? `${s.loaded_model} · ${ago(adj(s.loaded_model_age_s))} ago`
      : "unknown";

    const reqsCls = (s.active_requests ?? 0) > 0 ? "hot" : null;
    setStat(p, "reqs", f0(s.active_requests), "active reqs", llmAge, staleAfter, reqsCls);
    setStat(p, "gen", f1(s.gen_tps), "tok/s gen", adj(s.gen_tps_age_s), staleAfter);
    setStat(p, "prefill", f0(s.prefill_tps), "tok/s prefill", adj(s.prefill_tps_age_s), staleAfter);
    setStat(p, "mem", f1(s.model_mem_gb), "model GB", adj(s.model_mem_age_s), staleAfter);

    // Memory-pressure bar: hostmon % if known, else omlx pressure level.
    const pct = s.mem_pressure_pct;
    let w = 0, cls = "";
    if (pct != null) {
      w = Math.min(100, pct * 100);
      cls = w > 92 ? "crit" : w > 80 ? "warn" : "";
      p.memP.textContent = `pressure ${Math.round(w)}% · ${ago(adj(s.mem_pressure_age_s))} ago`;
    } else if (s.pressure_level) {
      w = { normal: 30, warning: 70, critical: 95 }[s.pressure_level] ?? 0;
      cls = s.pressure_level === "critical" ? "crit" : s.pressure_level === "warning" ? "warn" : "";
      p.memP.textContent = `pressure ${s.pressure_level}`;
    } else {
      p.memP.textContent = "pressure —";
    }
    p.membar.style.width = `${w}%`;
    p.membar.className = cls;
    // No per-server routing decision: route events belong to the router
    // device and deployment names are not device ids. The panel state above
    // (badge/active class) is driven only by server metrics and s.active;
    // the global last decision lives in the router pill.
  }

  const o = data.overall;
  const oAge = adj(o.age_s);
  overallEl.textContent =
    o.status === "live" ? `live · ${ago(oAge)} ago`
    : o.status === "stale" ? `stale · last data ${ago(oAge)} ago`
    : o.status === "offline" ? "all servers offline"
    : "no data yet";
  overallEl.className = `rack-pill ${o.status === "live" ? "live" : o.status === "stale" ? "stale" : "offline"}`;

  const r = data.router;
  if (!r || !r.configured) {
    routerEl.textContent = "";
    routerEl.className = "rack-pill router";
    routerEl.style.display = "none";
  } else {
    routerEl.style.display = "";
    const bits = [];
    bits.push(r.ok === false ? "router down" : r.ready === "ready" ? "router ready" : "router n/r");
    if (r.wait_avg_s != null) bits.push(`wait ${r.wait_avg_s.toFixed(1)}s · ${ago(adj(r.wait_avg_age_s))} ago`);
    if (r.auth_ok === false) bits.push("key needed");
    // Global last routing decision (router device's newest route event).
    // Deployment names are NOT device ids: shown as a name only, never
    // mapped to or highlighted on a server panel.
    const d = r.last_decision;
    if (d && typeof d.deployment === "string" && d.deployment) {
      bits.push(`last route → ${d.deployment} · ${ago(adj(d.age_s))} ago`);
    }
    routerEl.textContent = bits.join(" · ");
    routerEl.className =
      "rack-pill router " +
      (r.ok === false ? "err" : r.ready === "ready" ? "live" : "stale");
  }
}

/* ---------- loop ---------- */
let lastGood = 0;
let lastData = null;

async function tick() {
  try {
    const r = await fetch("/api/rack", { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    lastGood = Date.now();
    lastData = data;
    render(data);
  } catch {
    // Hub unreachable: keep the last snapshot on screen but mark it stale.
    const ageS = (Date.now() - lastGood) / 1000;
    if (lastGood) {
      overallEl.textContent = `hub unreachable · data ${ago(ageS)} ago`;
      overallEl.className = "rack-pill stale";
    } else {
      overallEl.textContent = "hub unreachable";
      overallEl.className = "rack-pill offline";
    }
  }
}

function clock() {
  const d = new Date();
  clockEl.textContent =
    `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

clock();
setInterval(clock, 1000);
tick();
setInterval(tick, POLL_MS);
// Re-render ages between polls so "Xs ago" stays honest without refetching.
setInterval(() => { if (lastData) render(lastData); }, POLL_MS);
