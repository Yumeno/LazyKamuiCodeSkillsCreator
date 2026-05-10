// Queue Dashboard — Vanilla JS polling client
// Uses same-origin /api/* proxied by queue_dashboard.py

const BASE_INTERVAL = 2000;
const BACKOFF_MAX = 10000;
const LIMIT_DEFAULT = 50;

let currentFilter = "all";
let limit = LIMIT_DEFAULT;
let consecutiveErrors = 0;
let refreshTimer = null;
let workerConnected = false;

async function fetchJSON(path, init) {
  const r = await fetch(path, init);
  if (!r.ok) {
    let msg = `${path}: HTTP ${r.status}`;
    try {
      const b = await r.json();
      if (b && b.error) msg += ` (${b.error})`;
    } catch {}
    throw new Error(msg);
  }
  return r.json();
}

function scheduleRefresh() {
  if (refreshTimer) clearTimeout(refreshTimer);
  const baseInterval = BASE_INTERVAL * (consecutiveErrors + 1);
  const interval = Math.min(baseInterval, BACKOFF_MAX);
  const multiplier = document.visibilityState === "hidden" ? 5 : 1;
  refreshTimer = setTimeout(refresh, interval * multiplier);
}

async function refresh() {
  try {
    const [stats, jobs] = await Promise.all([
      fetchJSON("/api/stats"),
      fetchJSON(`/api/jobs?limit=${limit}`),
    ]);
    workerConnected = true;
    renderSummary(stats);
    renderCategories(stats.category_limits || {});
    // PR5 (#61): /api/stats now ships custom_groups runtime status
    // alongside category_limits. Old workers (pre-PR4) don't include
    // this field; the helper degrades to "(no custom groups configured)".
    renderCustomGroups(stats.custom_groups || {});
    renderEndpoints(stats.endpoints || []);
    renderJobs(jobs.jobs || []);
    renderClock(stats.server_time_utc);
    setStatus(true, null);
    updateWorkerButtons(true);
    consecutiveErrors = 0;
  } catch (e) {
    consecutiveErrors++;
    workerConnected = false;
    setStatus(false, e.message);
    updateWorkerButtons(false);
  } finally {
    scheduleRefresh();
  }
}

function setStatus(ok, errMsg) {
  const dot = document.getElementById("status-indicator");
  dot.style.color = ok ? "limegreen" : "orangered";
  dot.title = ok ? "Connected" : `Error: ${errMsg || "unknown"}`;
  const err = document.getElementById("error-banner");
  if (ok) {
    err.hidden = true;
    err.textContent = "";
  } else {
    err.hidden = false;
    err.textContent = `⚠ ${errMsg || "Worker unreachable"}`;
  }
}

function renderClock(serverUtc) {
  const el = document.getElementById("server-time");
  const local = new Date().toLocaleTimeString();
  el.textContent = `Local: ${local} | UTC: ${serverUtc || "-"}`;
}

function renderSummary(stats) {
  const counts = { pending: 0, running: 0, completed: 0, failed: 0 };
  for (const ep of stats.endpoints || []) {
    counts.pending += ep.pending || 0;
    counts.running += ep.running || 0;
    counts.completed += ep.completed || 0;
    counts.failed += ep.failed || 0;
  }
  for (const [k, v] of Object.entries(counts)) {
    const el = document.getElementById("count-" + k);
    if (el) el.textContent = v;
  }
}

// Tiny el() helper — DOM API (no innerHTML → no XSS)
function el(tag, props = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null) continue;
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") {
      e.addEventListener(k.slice(2), v);
    } else if (k === "dataset") Object.assign(e.dataset, v);
    else e.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null) continue;
    e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return e;
}

function renderCategories(categories) {
  const grid = document.getElementById("category-grid");
  grid.innerHTML = "";
  const entries = Object.entries(categories);
  if (entries.length === 0) {
    grid.appendChild(el("div", { text: "No category data" }));
    return;
  }
  for (const [cat, info] of entries) {
    const card = el("div", {
      class: "category-card " + (info.paused ? "paused" : ""),
    });
    card.appendChild(el("h3", { text: cat }));
    card.appendChild(el("div", {
      text: `Inflight: ${info.inflight ?? 0}/${info.max_inflight ?? 1}`,
    }));
    card.appendChild(el("div", {
      text: `Cooldown: ${info.cooldown_remaining_s ?? 0}s`,
    }));
    card.appendChild(el("div", {
      text: `Consec 429: ${info.consecutive_429 ?? 0}`,
    }));
    card.appendChild(el("div", {
      text: info.paused ? "⏸ PAUSED" : "▶ Active",
    }));
    if (info.pause_reason) {
      card.appendChild(el("pre", {
        class: "reason",
        text: JSON.stringify(info.pause_reason, null, 2),
      }));
    }
    card.appendChild(el("button", {
      text: info.paused ? "Resume" : "Pause",
      onclick: () => toggleCategory(cat, info.paused),
    }));
    grid.appendChild(card);
  }
}

function renderCustomGroups(groups) {
  // PR5 (#61): the right-hand sibling of the Categories section.
  // ``groups`` is the ``custom_groups`` block from /api/stats — keys
  // are user-defined group names, values are the same status shape
  // get_all_status() emits in CustomGroupLimiter (paused, inflight,
  // max_inflight, exhaust_cooldown, cooldown_remaining_s, endpoints,
  // pause_reason).
  const grid = document.getElementById("group-grid");
  if (!grid) return; // older index.html without the section
  grid.innerHTML = "";
  const entries = Object.entries(groups);
  if (entries.length === 0) {
    grid.appendChild(el("div", {
      class: "empty-hint",
      text: "(no custom groups configured)",
    }));
    return;
  }
  for (const [name, info] of entries) {
    const card = el("div", {
      class: "group-card " + (info.paused ? "paused" : ""),
    });
    card.appendChild(el("h3", { text: name }));

    // Endpoints — collapsed text block; users mostly want the runtime
    // values, but seeing the patterns confirms which group is gating
    // a given URL.
    if (Array.isArray(info.endpoints) && info.endpoints.length > 0) {
      card.appendChild(el("div", {
        class: "endpoints",
        title: info.endpoints.join("\n"),
        text: info.endpoints.length === 1
          ? info.endpoints[0]
          : `${info.endpoints[0]} (+${info.endpoints.length - 1} more)`,
      }));
    }

    card.appendChild(el("div", {
      text: `Inflight: ${info.inflight ?? 0}/${info.max_inflight ?? 1}`,
    }));
    card.appendChild(el("div", {
      text: `Cooldown: ${info.cooldown_remaining_s ?? 0}s`,
    }));
    card.appendChild(el("div", {
      text: `Consec 429: ${info.consecutive_429 ?? 0}`,
    }));
    card.appendChild(el("div", {
      text: info.paused ? "⏸ PAUSED" : "▶ Active",
    }));
    if (info.pause_reason) {
      card.appendChild(el("pre", {
        class: "reason",
        text: JSON.stringify(info.pause_reason, null, 2),
      }));
    }
    card.appendChild(el("button", {
      text: info.paused ? "Resume" : "Pause",
      onclick: () => toggleGroup(name, info.paused),
    }));
    grid.appendChild(card);
  }
}

async function toggleGroup(name, currentlyPaused) {
  const action = currentlyPaused ? "resume" : "pause";
  try {
    await fetchJSON(`/api/groups/${encodeURIComponent(name)}/${action}`, { method: "POST" });
    refresh();
  } catch (e) {
    // Worker returns 404 with available_groups for unknown name — surface
    // the message as-is so the user can see the typo / missing group.
    setStatus(false, `${action} group ${name}: ${e.message}`);
  }
}

function renderEndpoints(endpoints) {
  const tbody = document.querySelector("#endpoint-table tbody");
  tbody.innerHTML = "";
  if (endpoints.length === 0) {
    tbody.appendChild(el("tr", {},
      el("td", { colspan: "5", text: "No endpoints yet" })));
    return;
  }
  for (const ep of endpoints) {
    tbody.appendChild(el("tr", {},
      el("td", { title: ep.endpoint || "", text: ep.endpoint || "" }),
      el("td", { text: String(ep.pending ?? 0) }),
      el("td", { text: String(ep.running ?? 0) }),
      el("td", { text: String(ep.completed ?? 0) }),
      el("td", { text: String(ep.failed ?? 0) }),
    ));
  }
}

function renderJobs(jobs) {
  const list = document.getElementById("job-list");
  list.innerHTML = "";
  const filtered = currentFilter === "all"
    ? jobs
    : jobs.filter(j => {
      const s = j.status;
      if (currentFilter === "running") return s === "running" || s === "polling";
      return s === currentFilter;
    });
  if (filtered.length === 0) {
    list.appendChild(el("li", {
      class: "job-item",
      text: "No jobs matching filter",
    }));
    return;
  }
  for (const j of filtered) {
    const age = j.updated_age_seconds != null
      ? `${Math.round(j.updated_age_seconds)}s ago` : "";
    const shortId = (j.job_id || "").slice(0, 8);
    const status = j.status || "unknown";
    const li = el("li", {
      class: "job-item status-" + status,
      onclick: () => showJobDetail(j.job_id),
    },
      el("span", { class: "badge", text: status }),
      el("span", { class: "endpoint", title: j.endpoint || "", text: j.endpoint || "" }),
      el("span", { class: "age", text: age }),
      el("span", { class: "id", title: j.job_id || "", text: shortId }),
    );
    list.appendChild(li);
  }
}

async function showJobDetail(jobId) {
  if (!jobId) return;
  try {
    const data = await fetchJSON(`/api/jobs/${jobId}?include_args=true`);
    const pretty = JSON.stringify(data, null, 2);
    const content = document.getElementById("job-detail-content");
    content.textContent = pretty.length > 50000
      ? pretty.slice(0, 50000) + "\n\n... (truncated)"
      : pretty;
    openModal();
  } catch (e) {
    alert("Failed to load job: " + e.message);
  }
}

async function toggleCategory(cat, currentlyPaused) {
  const action = currentlyPaused ? "resume" : "pause";
  if (!confirm(`${action} category "${cat}"?`)) return;
  try {
    await fetchJSON(`/api/categories/${cat}/${action}`, { method: "POST" });
    refresh();
  } catch (e) {
    alert(e.message);
  }
}

// ---- Modal ----
function modalEl() { return document.getElementById("job-detail-modal"); }
function openModal() { modalEl().hidden = false; document.body.style.overflow = "hidden"; }
function closeModal() { modalEl().hidden = true; document.body.style.overflow = ""; }

// ---- Settings Panel ----
function openConfigPanel() {
  document.getElementById("config-panel").hidden = false;
  document.getElementById("config-overlay").hidden = false;
  loadConfig();
}
function closeConfigPanel() {
  document.getElementById("config-panel").hidden = true;
  document.getElementById("config-overlay").hidden = true;
}

/* PR5 (#61): graceful-degrade settings panel.

   The pre-PR1 layout was a single set of {max_inflight, min_interval,
   exhaust_cooldown} inputs that mapped to flat `cfg.category.*` fields.
   PR1 introduced per-category values under `cfg.category.limits.{cat}`
   while keeping the flat fields as a legacy mirror, and PR4 added
   `cfg.custom_groups.{name}`. The dashboard now:

   * Reads the new shapes (renders one row per category, one card per
     custom group) and drops the legacy flat input.
   * Detects a pre-v2.11.0 worker by the absence of `cfg.category.limits`
     and shows a `compat-banner` telling the user how to recover. The
     rest of the dashboard (jobs / stats) keeps working — the banner
     only suppresses the per-category form.
   * Hits `/api/version` (worker shipped that endpoint in PR2) before
     `/api/config` so the banner can quote the actual worker version
     when it differs from this dashboard.
*/

async function loadConfig() {
  let workerVersion = null;
  try {
    const v = await fetchJSON("/api/version");
    workerVersion = v.version;
  } catch {
    // /api/version is missing → almost certainly a pre-v2.11.0 worker.
    // Leave workerVersion as null; the banner copy handles that case.
  }

  let cfg;
  try {
    cfg = await fetchJSON("/api/config");
  } catch (e) {
    showCompatBanner(
      "Failed to load /api/config from worker.\n" +
      "  Reason: " + e.message + "\n" +
      "  Is the worker running on " + (window.WORKER_URL || "127.0.0.1:54321") + "?",
      "error",
    );
    return;
  }

  // Render endpoint defaults + worker settings unconditionally — these
  // live outside the per-category schema and have not changed shape
  // since lazy-v2.10.x.
  document.getElementById("cfg-max-concurrent").value = cfg.endpoint?.default_max_concurrent ?? 2;
  document.getElementById("cfg-ep-interval").value = cfg.endpoint?.default_min_interval ?? 10.0;
  document.getElementById("cfg-idle-timeout").value = cfg.worker?.idle_timeout ?? 60;

  // Worker version label in the header.
  const verEl = document.getElementById("worker-version-info");
  if (verEl) {
    verEl.textContent = workerVersion
      ? `Worker: ${workerVersion}`
      : "Worker: (pre-v2.11.0)";
  }

  // PR5 fix #2: the headline test for "is this worker new enough" is
  // the presence of `cfg.category.limits`. The pre-v2.11.0 shape was
  // `cfg.category.{max_inflight,min_interval,exhaust_cooldown}` flat.
  const limitsObj = cfg?.category?.limits;
  const limitsObjValid = limitsObj && typeof limitsObj === "object" && !Array.isArray(limitsObj);
  if (!limitsObjValid) {
    showCompatBanner(
      "Worker API appears to be pre-v2.11.\n" +
      "This dashboard requires Worker v2.11.0 or later for per-category " +
      "settings.\n" +
      "Detected worker version: " + (workerVersion ?? "(unknown — /api/version missing)") + "\n" +
      "Please upgrade or restart the worker:\n" +
      "  curl -X POST http://127.0.0.1:54321/api/worker/shutdown\n" +
      "then re-run a client command to spawn a fresh worker.",
      "warn",
    );
    // Clear the per-category / per-group forms so we don't show stale
    // values from an earlier load.
    document.getElementById("cat-limits-grid").innerHTML = "";
    document.getElementById("group-limits-grid").innerHTML = "";
    return;
  }

  hideCompatBanner();
  renderCategoryLimitsForm(limitsObj);
  renderGroupLimitsForm(cfg.custom_groups || {});
  showConfigResult("", "");
}

function showCompatBanner(message, level) {
  const banner = document.getElementById("compat-banner");
  if (!banner) return;
  banner.textContent = message;
  banner.className = "compat-banner compat-" + (level || "warn");
  banner.hidden = false;
}

function hideCompatBanner() {
  const banner = document.getElementById("compat-banner");
  if (!banner) return;
  banner.hidden = true;
  banner.textContent = "";
  banner.className = "compat-banner";
}

const _CAT_KEYS = ["max_inflight", "min_interval", "exhaust_cooldown"];

function renderCategoryLimitsForm(limits) {
  // limits = { t2i: { max_inflight, min_interval, exhaust_cooldown }, ... }
  const grid = document.getElementById("cat-limits-grid");
  grid.innerHTML = "";

  // Header row (sorted category names — same order PR1 worker uses for
  // the legacy mirror keys, so the user reads consistent ordering
  // everywhere).
  const cats = Object.keys(limits).sort();
  if (cats.length === 0) {
    grid.appendChild(el("div", { class: "empty-hint", text: "(no categories configured)" }));
    return;
  }

  const header = el("div", { class: "cat-limits-grid-header" },
    el("span", { text: "Category" }),
    el("span", { text: "Max Inflight" }),
    el("span", { text: "Min Interval (s)" }),
    el("span", { text: "429 Cooldown (s)" }),
  );
  grid.appendChild(header);

  for (const cat of cats) {
    const vals = limits[cat] || {};
    const row = el("div", { class: "cat-limits-row" });
    row.appendChild(el("span", { class: "key-label", text: cat }));
    for (const key of _CAT_KEYS) {
      const input = el("input", {
        type: "number",
        min: key === "max_inflight" ? 1 : 0,
        step: key === "min_interval" ? 0.1 : 1,
      });
      input.value = vals[key] ?? "";
      input.dataset.cat = cat;
      input.dataset.key = key;
      row.appendChild(input);
    }
    grid.appendChild(row);
  }
}

function renderGroupLimitsForm(groups) {
  // groups = { name: { endpoints, max_inflight, min_interval, exhaust_cooldown } }
  const grid = document.getElementById("group-limits-grid");
  grid.innerHTML = "";

  const names = Object.keys(groups);
  if (names.length === 0) {
    grid.appendChild(el("div", { class: "empty-hint", text: "(no custom groups configured)" }));
    return;
  }

  const header = el("div", { class: "group-limits-grid-header" },
    el("span", { text: "Group" }),
    el("span", { text: "Max Inflight" }),
    el("span", { text: "Min Interval (s)" }),
    el("span", { text: "429 Cooldown (s)" }),
  );
  grid.appendChild(header);

  for (const name of names) {
    const vals = groups[name] || {};
    const row = el("div", { class: "group-limits-row" });
    row.appendChild(el("span", {
      class: "key-label",
      text: name,
      title: Array.isArray(vals.endpoints) ? vals.endpoints.join("\n") : "",
    }));
    for (const key of _CAT_KEYS) {
      const input = el("input", {
        type: "number",
        min: key === "max_inflight" ? 1 : 0,
        step: key === "min_interval" ? 0.1 : 1,
      });
      input.value = vals[key] ?? "";
      input.dataset.group = name;
      input.dataset.key = key;
      row.appendChild(input);
    }
    grid.appendChild(row);
  }
}

async function applyConfig() {
  // Build per-category PATCH body from the rendered form. We only
  // include fields the user actually changed (skip blank / invalid)
  // so an unrelated category isn't accidentally clamped to 0.
  const catLimits = {};
  for (const input of document.querySelectorAll("#cat-limits-grid input")) {
    const cat = input.dataset.cat;
    const key = input.dataset.key;
    if (!cat || !key) continue;
    if (input.value === "") continue;
    const num = Number(input.value);
    if (!Number.isFinite(num)) continue;
    if (!catLimits[cat]) catLimits[cat] = {};
    catLimits[cat][key] = num;
  }

  // Same shape for custom_groups.
  const grpLimits = {};
  for (const input of document.querySelectorAll("#group-limits-grid input")) {
    const grp = input.dataset.group;
    const key = input.dataset.key;
    if (!grp || !key) continue;
    if (input.value === "") continue;
    const num = Number(input.value);
    if (!Number.isFinite(num)) continue;
    if (!grpLimits[grp]) grpLimits[grp] = {};
    grpLimits[grp][key] = num;
  }

  const body = {
    endpoint: {
      default_max_concurrent: Number(document.getElementById("cfg-max-concurrent").value),
      default_min_interval: Number(document.getElementById("cfg-ep-interval").value),
    },
    worker: {
      idle_timeout: Number(document.getElementById("cfg-idle-timeout").value),
    },
  };
  // Only include category.limits / groups if the form actually had values
  // for them — keeps the worker logs clean and avoids an empty
  // `category.limits: {}` triggering a no-op apply.
  if (Object.keys(catLimits).length > 0) {
    body.category = { limits: catLimits };
  }
  if (Object.keys(grpLimits).length > 0) {
    body.groups = grpLimits;
  }

  try {
    const result = await fetchJSON("/api/config", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    let msg = "Applied: " + Object.keys(result.applied || {}).join(", ");
    if (result.requires_restart?.length > 0)
      msg += "\nRequires restart: " + result.requires_restart.join(", ");
    if (Object.keys(result.rejected || {}).length > 0)
      msg += "\nRejected: " + JSON.stringify(result.rejected);
    showConfigResult(msg, Object.keys(result.rejected || {}).length > 0 ? "error" : "success");
    refresh();
  } catch (e) {
    showConfigResult("Apply failed: " + e.message, "error");
  }
}

function showConfigResult(msg, cls) {
  const el = document.getElementById("cfg-result");
  el.textContent = msg;
  el.className = cls || "";
}

// ---- Skills ----
let skillsLoaded = false;

async function loadSkills(forceRefresh = false) {
  const url = forceRefresh ? "/api/skills?refresh=true" : "/api/skills";
  try {
    const data = await fetchJSON(url);
    renderSkills(data);
    skillsLoaded = true;
  } catch (e) {
    document.getElementById("skills-grid").innerHTML = "";
    document.getElementById("skills-grid").appendChild(
      el("div", { text: "Failed to load skills: " + e.message })
    );
  }
}

function renderSkills(data) {
  const grid = document.getElementById("skills-grid");
  grid.innerHTML = "";
  document.getElementById("skills-count").textContent = `(${data.total || 0})`;

  if (!data.items || data.items.length === 0) {
    grid.appendChild(el("div", { text: "No skills found" }));
    return;
  }
  for (const s of data.items) {
    const catClass = `skill-cat skill-cat-${s.category || "other"}`;
    const card = el("div", { class: "skill-card" },
      el("div", {},
        el("span", { class: "skill-name", text: s.id }),
        el("span", { class: catClass, text: s.category || "other" }),
      ),
    );
    if (s.endpoint_url) {
      card.appendChild(el("div", {
        class: "skill-ep",
        title: s.endpoint_url,
        text: s.endpoint_url,
      }));
    }
    grid.appendChild(card);
  }
}

// ---- Worker Management ----
function updateWorkerButtons(connected) {
  document.getElementById("btn-start").hidden = connected;
  document.getElementById("btn-stop").hidden = !connected;
  document.getElementById("btn-restart").hidden = !connected;
}

async function startWorker() {
  document.getElementById("btn-start").disabled = true;
  document.getElementById("btn-start").textContent = "Starting...";
  try {
    const result = await fetchJSON("/api/worker/start", { method: "POST" });
    if (result.status === "started" || result.status === "already_running") {
      refresh();
    } else {
      alert("Start failed: " + (result.error || result.status));
    }
  } catch (e) {
    alert("Start failed: " + e.message);
  } finally {
    document.getElementById("btn-start").disabled = false;
    document.getElementById("btn-start").textContent = "▶ Start";
  }
}

async function stopWorker() {
  if (!confirm("Stop the worker daemon?")) return;
  try {
    await fetchJSON("/api/worker/shutdown", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ timeout: 30 }),
    });
    setTimeout(refresh, 2000);
  } catch (e) {
    // 502 is expected if worker shuts down before responding
    setTimeout(refresh, 2000);
  }
}

async function restartWorker() {
  if (!confirm("Restart the worker daemon?")) return;
  document.getElementById("btn-restart").disabled = true;
  document.getElementById("btn-restart").textContent = "Restarting...";
  try {
    const result = await fetchJSON("/api/worker/restart", { method: "POST" });
    if (result.status === "started" || result.status === "already_running") {
      refresh();
    } else {
      alert("Restart failed: " + (result.error || result.status));
    }
  } catch (e) {
    alert("Restart failed: " + e.message);
  } finally {
    document.getElementById("btn-restart").disabled = false;
    document.getElementById("btn-restart").textContent = "↻ Restart";
  }
}

// ---- Event handlers ----

// Filter buttons
document.querySelectorAll("#job-filter button").forEach(btn => {
  btn.addEventListener("click", () => {
    currentFilter = btn.dataset.filter;
    document.querySelectorAll("#job-filter button").forEach(b =>
      b.classList.toggle("active", b === btn));
    refresh();
  });
});

// Limit input
const limitInput = document.getElementById("limit-input");
if (limitInput) {
  limitInput.addEventListener("change", () => {
    const v = parseInt(limitInput.value, 10);
    if (Number.isFinite(v) && v > 0 && v <= 500) {
      limit = v;
      refresh();
    }
  });
}

// Modal close handlers
document.querySelector("#job-detail-modal .close").addEventListener("click", closeModal);
modalEl().addEventListener("click", e => { if (e.target === modalEl()) closeModal(); });
document.addEventListener("keydown", e => {
  if (e.key === "Escape") {
    if (!modalEl().hidden) closeModal();
    else if (!document.getElementById("config-panel").hidden) closeConfigPanel();
  }
});

// Settings panel
document.getElementById("menu-toggle").addEventListener("click", openConfigPanel);
document.getElementById("panel-close").addEventListener("click", closeConfigPanel);
document.getElementById("config-overlay").addEventListener("click", closeConfigPanel);
document.getElementById("cfg-apply").addEventListener("click", applyConfig);
document.getElementById("cfg-reload").addEventListener("click", loadConfig);

// Skills
document.getElementById("skills-refresh").addEventListener("click", () => loadSkills(true));

// Worker controls
document.getElementById("btn-start").addEventListener("click", startWorker);
document.getElementById("btn-stop").addEventListener("click", stopWorker);
document.getElementById("btn-restart").addEventListener("click", restartWorker);

// Visibility change
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") refresh();
});

// Initial load
refresh();
loadSkills();
