// Queue Dashboard — Vanilla JS polling client
// Uses same-origin /api/* proxied by queue_dashboard.py

const BASE_INTERVAL = 2000;
const BACKOFF_MAX = 10000;
const LIMIT_DEFAULT = 50;

let currentFilter = "all";
let limit = LIMIT_DEFAULT;
let consecutiveErrors = 0;
let refreshTimer = null;

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
    renderSummary(stats);
    renderCategories(stats.category_limits || {});
    renderEndpoints(stats.endpoints || []);
    renderJobs(jobs.jobs || []);
    renderClock(stats.server_time_utc);
    setStatus(true, null);
    consecutiveErrors = 0;
  } catch (e) {
    consecutiveErrors++;
    setStatus(false, e.message);
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
  el.textContent = `Local: ${local} | Server UTC: ${serverUtc || "-"}`;
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
      ? `${Math.round(j.updated_age_seconds)}s ago`
      : "";
    const shortId = (j.job_id || "").slice(0, 8);
    const status = j.status || "unknown";
    const li = el("li", {
      class: "job-item status-" + status,
      onclick: () => showJobDetail(j.job_id),
    },
      el("span", { class: "badge", text: status }),
      el("span", {
        class: "endpoint",
        title: j.endpoint || "",
        text: j.endpoint || "",
      }),
      el("span", { class: "age", text: age }),
      el("span", {
        class: "id",
        title: j.job_id || "",
        text: shortId,
      }),
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
      ? pretty.slice(0, 50000) + "\n\n... (truncated, see full JSON via CLI)"
      : pretty;
    openModal();
  } catch (e) {
    alert("Failed to load job: " + e.message);
  }
}

async function toggleCategory(cat, currentlyPaused) {
  const action = currentlyPaused ? "resume" : "pause";
  const confirmMsg = currentlyPaused
    ? `Resume dispatching for category "${cat}"?`
    : `Pause dispatching for category "${cat}"?`;
  if (!confirm(confirmMsg)) return;
  try {
    await fetchJSON(`/api/categories/${cat}/${action}`, { method: "POST" });
    refresh();
  } catch (e) {
    alert(e.message);
  }
}

// Modal
function modalEl() { return document.getElementById("job-detail-modal"); }
function openModal() {
  modalEl().hidden = false;
  document.body.style.overflow = "hidden";
}
function closeModal() {
  modalEl().hidden = true;
  document.body.style.overflow = "";
}

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
document.querySelector("#job-detail-modal .close")
  .addEventListener("click", closeModal);
modalEl().addEventListener("click", e => {
  if (e.target === modalEl()) closeModal(); // background click
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && !modalEl().hidden) closeModal();
});

// Visibility change — refresh immediately when tab becomes visible
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") refresh();
});

refresh();
