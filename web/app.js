/* ---------------------------------------------------------------------------
   BMS Drawing Verifier - front end. Plain ES modules, no build step.

   Four views (login / upload / running / results) toggled by a data-view
   attribute on <html>. The interesting part is the deck: every finding gets a
   card element ONCE, and navigation only rewrites transforms, so the browser
   animates between states instead of us re-rendering and losing the transition.

   The one rule carried over from the backend: a finding with certified=false is
   an ADVISORY string inference and must never look like a hard failure. It is
   badged, dashed, sorted last, and excluded from the failure count.
--------------------------------------------------------------------------- */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const view = (name) => {
  document.documentElement.setAttribute("data-view", name);
  // each view is a fresh page to the reader; carrying the old scroll offset
  // over lands them halfway down the new one
  window.scrollTo({ top: 0, behavior: "instant" });
};

const SEV_COLOR = {
  CRITICAL: "var(--sev-critical)",
  HIGH: "var(--sev-high)",
  MEDIUM: "var(--sev-medium)",
  ADVISORY: "var(--sev-advisory)",
};
const CATEGORY_LABEL = {
  MISSING_POINT: "Missing point",
  EXTRA_INSTANCE: "Extra instance",
  WRONG_TR: "Wrong terminal",
  TR_PRESENCE: "Terminal presence",
  INVALID_EXCEL_TYPE: "Schedule type",
  TOTALS_MISMATCH: "Schedule totals",
  PAGE_INTEGRITY: "Page integrity",
  AMBIGUOUS_DDC: "Ambiguous DDC",
  TYPE_HINT_MISMATCH: "Type hint",
};

const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, options = {}) {
  const res = await fetch(path, { credentials: "same-origin", ...options });
  if (res.status === 401) { view("login"); throw new Error("Not signed in"); }
  const body = res.headers.get("content-type")?.includes("json")
    ? await res.json() : await res.text();
  if (!res.ok) throw new Error(body?.detail || body || `HTTP ${res.status}`);
  return body;
}

/* ── sign in ─────────────────────────────────────────────────────────────── */

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = $("#login-error");
  error.hidden = true;
  const body = new FormData();
  body.append("username", $("#username").value);
  body.append("password", $("#password").value);
  try {
    const user = await api("/api/login", { method: "POST", body });
    $("#who").textContent = user.display_name;
    $("#password").value = "";
    view("upload");
    loadRecent();
  } catch (exc) {
    error.textContent = exc.message;
    error.hidden = false;
    $(".login-card").classList.remove("shake");
    void $(".login-card").offsetWidth;   // restart the animation
    $(".login-card").classList.add("shake");
  }
});

for (const id of ["#logout", "#logout2"]) {
  $(id).addEventListener("click", async () => {
    await api("/api/logout", { method: "POST" }).catch(() => {});
    view("login");
  });
}

/* ── upload ──────────────────────────────────────────────────────────────── */

// The two EPLAN members the pipeline actually reads. A .edb folder holds
// hundreds of files; we filter to these in the browser so the upload is 2 files.
const EOD_MEMBERS = ["function.eod", "page.eod"];

const picked = { excel: null, edb: null, ref: null, pdf: null };

function pickEodMembers(fileList) {
  const found = {};
  for (const file of fileList) {
    const base = file.name.toLowerCase();
    if (EOD_MEMBERS.includes(base)) found[base] = file;
  }
  return (found["function.eod"] && found["page.eod"])
    ? { fn: found["function.eod"], pg: found["page.eod"], scanned: fileList.length }
    : null;
}

function setSlot(slot, label, ok = true) {
  const drop = $(`[data-drop="${slot}"]`);
  const out = $(`[data-file="${slot}"]`);
  out.textContent = label;
  out.style.color = ok ? "" : "var(--sev-critical)";
  drop.classList.toggle("filled", ok && !!label);
  drop.closest(".step")?.classList.toggle("filled", ok && !!label);
  refreshRunButton();
}

function refreshRunButton() {
  $("#run").disabled = !(picked.excel && picked.edb);
}

function handleEdbSelection(slot, files) {
  const members = pickEodMembers(files);
  if (!members) {
    picked[slot] = null;
    setSlot(slot, `No Function.eod + Page.eod in those ${files.length} file(s)`, false);
    return;
  }
  picked[slot] = members;
  const scanned = members.scanned > 2 ? ` (of ${members.scanned} files)` : "";
  setSlot(slot, `Function.eod + Page.eod${scanned}`);
}

$("#excel").addEventListener("change", (e) => {
  const file = e.target.files[0];
  picked.excel = file || null;
  setSlot("excel", file ? file.name : "");
  $("#excel-options").hidden = !file || file.name.toLowerCase().endsWith(".csv");
});
$("#edb").addEventListener("change", (e) => handleEdbSelection("edb", e.target.files));
$("#edb-files").addEventListener("change", (e) => handleEdbSelection("edb", e.target.files));
$("#ref").addEventListener("change", (e) => handleEdbSelection("ref", e.target.files));
$("#pdf").addEventListener("change", (e) => {
  picked.pdf = e.target.files[0] || null;
  setSlot("pdf", picked.pdf ? picked.pdf.name : "");
});

$$("[data-pick]").forEach((button) => {
  button.addEventListener("click", () => $(`#${button.dataset.pick}`).click());
});

// Drag and drop. A dropped directory arrives as entries, so walk them for the
// two members rather than asking the user to dig them out.
$$(".drop").forEach((drop) => {
  const slot = drop.dataset.drop;
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", async (e) => {
    e.preventDefault();
    drop.classList.remove("over");
    const files = await filesFromDataTransfer(e.dataTransfer);
    if (!files.length) return;
    if (slot === "edb" || slot === "ref") return handleEdbSelection(slot, files);
    picked[slot] = files[0];
    setSlot(slot, files[0].name);
    if (slot === "excel") {
      $("#excel-options").hidden = files[0].name.toLowerCase().endsWith(".csv");
    }
  });
});

async function filesFromDataTransfer(dt) {
  const entries = [...dt.items].map((i) => i.webkitGetAsEntry?.()).filter(Boolean);
  if (!entries.length) return [...dt.files];
  const out = [];
  const walk = async (entry, depth = 0) => {
    if (entry.isFile) {
      out.push(await new Promise((res) => entry.file(res)));
    } else if (entry.isDirectory && depth < 3) {
      const reader = entry.createReader();
      let batch;
      do {
        batch = await new Promise((res) => reader.readEntries(res));
        for (const child of batch) await walk(child, depth + 1);
      } while (batch.length);
    }
  };
  for (const entry of entries) await walk(entry);
  return out;
}

$("#load-sheets").addEventListener("click", async () => {
  if (!picked.excel) return;
  const button = $("#load-sheets");
  button.disabled = true;
  button.textContent = "Loading…";
  const body = new FormData();
  body.append("excel", picked.excel);
  body.append("password", $("#excel_password").value);
  try {
    const { sheets } = await api("/api/excel/sheets", { method: "POST", body });
    $("#sheet").innerHTML = sheets
      .map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("");
  } catch (exc) {
    showJobError(exc.message);
  } finally {
    button.disabled = false;
    button.textContent = "Load sheets";
  }
});

function showJobError(message) {
  const box = $("#job-error");
  box.textContent = message;
  box.hidden = false;
}

$("#job-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#job-error").hidden = true;
  const body = new FormData();
  const isCsv = picked.excel.name.toLowerCase().endsWith(".csv");
  body.append(isCsv ? "schedule_csv" : "excel", picked.excel);
  body.append("excel_password", $("#excel_password").value);
  body.append("sheet", $("#sheet").value);
  body.append("function_eod", picked.edb.fn, "Function.eod");
  body.append("page_eod", picked.edb.pg, "Page.eod");
  if (picked.ref) {
    body.append("ref_function_eod", picked.ref.fn, "Function.eod");
    body.append("ref_page_eod", picked.ref.pg, "Page.eod");
  }
  if (picked.pdf) body.append("pdf", picked.pdf);
  body.append("label", picked.excel.name);

  view("running");
  renderRunSteps(0, "Uploading…");
  try {
    const { job_id } = await api("/api/jobs", { method: "POST", body });
    pollJob(job_id);
  } catch (exc) {
    view("upload");
    showJobError(exc.message);
  }
});

$("#new-run").addEventListener("click", () => { view("upload"); loadRecent(); });
$("#run-cancel").addEventListener("click", () => view("upload"));

async function loadRecent() {
  try {
    const { jobs } = await api("/api/jobs");
    const done = jobs.filter((j) => j.state === "done").slice(0, 5);
    $("#recent").hidden = !done.length;
    $("#recent-list").innerHTML = done.map((j) => `
      <li>
        <span class="muted">${esc(j.label || j.id)}</span>
        <button class="btn btn-ghost" data-job="${esc(j.id)}">
          ${j.summary?.total ?? 0} finding(s) →
        </button>
      </li>`).join("");
    $$("#recent-list [data-job]").forEach((b) =>
      b.addEventListener("click", () => showResults(b.dataset.job)));
  } catch { /* not signed in yet */ }
}

/* ── running ─────────────────────────────────────────────────────────────── */

const STAGE_NAMES = [
  "Classify the schedule",
  "Read Function.eod + Page.eod",
  "Extract point + terminal records",
  "Scope to active pages",
  "Verify by multiplicity",
  "Page-family type hints (advisory)",
  "Terminal (TR) diff",
];

function renderRunSteps(stage, message) {
  $("#run-stage").textContent = stage
    ? `Step ${stage} of ${STAGE_NAMES.length}` : "Starting…";
  $("#run-message").textContent = message || "";
  $("#run-bar").style.width = `${(stage / STAGE_NAMES.length) * 100}%`;
  $("#run-steps").innerHTML = STAGE_NAMES.map((name, i) => {
    const state = i + 1 < stage ? "done" : i + 1 === stage ? "active" : "";
    const mark = i + 1 < stage ? "✓" : i + 1 === stage ? "▸" : "·";
    return `<li class="${state}"><span>${mark}</span><span>${esc(name)}</span></li>`;
  }).join("");
}

async function pollJob(jobId) {
  for (;;) {
    const job = await api(`/api/jobs/${jobId}`);
    renderRunSteps(job.stage, job.message);
    if (job.state === "done") return showResults(jobId);
    if (job.state === "error") {
      view("upload");
      return showJobError(job.message);
    }
    await new Promise((r) => setTimeout(r, 700));
  }
}

/* ── results: the card deck ──────────────────────────────────────────────── */

let all = [];          // every finding for this job
let shown = [];        // after severity filtering
let cards = [];        // one element per entry in `shown`
let index = 0;
let hasPdf = false;
let jobId = null;

async function showResults(id) {
  jobId = id;
  const data = await api(`/api/jobs/${id}/findings`);
  all = data.findings;
  hasPdf = data.has_pdf;
  renderStats(data.summary);
  applyFilter();
  view("results");
  // preventScroll, or focusing the deck scrolls the verdict and stats off the
  // top of the page the moment the results open
  $("#deck").focus({ preventScroll: true });
}

function renderStats(summary) {
  const stats = [
    ["Schedule expects", summary.schedule_expects],
    ["In the drawing", summary.eplan_points],
    ["Matched", summary.matched],
    ["Findings", summary.failure_count],
  ];
  $("#stats").innerHTML = stats.map(([label, value]) =>
    `<div class="stat"><b>${value ?? "—"}</b><span>${esc(label)}</span></div>`).join("");
}

function applyFilter() {
  shown = all;
  index = 0;
  buildDeck();
  renderGrid();
}

function buildDeck() {
  const stack = $("#deck-stack");
  stack.innerHTML = "";
  cards = shown.map((finding, i) => {
    const el = document.createElement("article");
    el.className = "card" + (finding.certified ? "" : " advisory");
    el.dataset.sev = finding.severity;
    el.innerHTML = cardHtml(finding, i);
    stack.appendChild(el);
    return el;
  });
  $("#empty").hidden = shown.length > 0;
  $("#deck").hidden = shown.length === 0;
  $("#deck-nav").hidden = shown.length === 0;
  $$(".hint").forEach((h) => (h.hidden = shown.length === 0));
  renderDots();
  position();
  wirePdfButtons();
}

function cardHtml(f, i) {
  const showCounts = f.expected !== null || f.found !== null;
  const evidence = (f.evidence || []).filter((e) => Object.values(e).some((v) => v != null));

  const counts = showCounts ? `
    <div class="counts">
      <div class="expected"><div class="n">${f.expected ?? "—"}</div><div class="lab">Expected</div></div>
      <div class="arrow">→</div>
      <div class="found"><div class="n">${f.found ?? "—"}</div><div class="lab">Found</div></div>
    </div>` : "";

  const table = evidence.length ? `
    <div class="evidence">
      <h4>Where in the drawing</h4>
      <div class="card-scroll"><table>
        <tr><th>Object id</th><th>Page</th><th>Channel / slot</th></tr>
        ${evidence.map((e) => `<tr>
          <td>${esc(e.function_object_id ?? "—")}</td>
          <td>${esc(e.page_name ?? "—")}</td>
          <td>${esc(e.channel_raw ?? (e.terminal_slot != null ? `pos ${e.terminal_slot + 1}` : "—"))}</td>
        </tr>`).join("")}
      </table></div>
    </div>` : "";

  const pair = f.likely_pair ? `
    <div class="pair">
      Looks like the same point as <b>${esc(f.likely_pair.title)}</b>
      (${Math.round(f.likely_pair.similarity * 100)}% alike).
      <div class="why">${esc(f.likely_pair.note)}</div>
    </div>` : "";

  const detail = f.category === "TOTALS_MISMATCH" && f.detail?.expected ? `
    <div class="evidence"><h4>QTY × flag vs Total_*</h4>
      <div class="card-scroll"><table>
        <tr><th>Type</th><th>Expected</th><th>Actual</th></tr>
        ${["DI", "DO", "AI", "AO"].map((t) => `<tr><td>${t}</td>
          <td>${esc(f.detail.expected?.[t] ?? "—")}</td>
          <td>${esc(f.detail.actual?.[t] ?? "—")}</td></tr>`).join("")}
      </table></div>
    </div>` : "";

  const why = !f.certified && f.detail?.why_advisory
    ? `<div class="pair"><b>Advisory only.</b> <div class="why">${esc(f.detail.why_advisory)}</div></div>`
    : "";

  const pdfButton = hasPdf && f.page_name
    ? `<button class="btn btn-ghost" data-pdf="1">Open the drawing PDF</button>` : "";

  return `
    <div class="card-top">
      <span class="pill">${esc(f.severity)}</span>
      <span class="pill pill-cat">${esc(CATEGORY_LABEL[f.category] || f.category)}</span>
      <span class="badge ${f.certified ? "certified" : "advisory"}">
        ${f.certified ? "Certified" : "Advisory"}</span>
      <span class="counter">${i + 1} / ${shown.length}</span>
    </div>
    <h3>${esc(f.title)}</h3>
    <p class="summary">${esc(f.summary)}</p>
    ${counts}${table}${detail}${pair}${why}
    <div class="card-foot">${pdfButton}</div>`;
}

function wirePdfButtons() {
  $$("[data-pdf]").forEach((b) =>
    b.addEventListener("click", () => window.open(`/api/jobs/${jobId}/pdf`, "_blank")));
}

/* Position every card from its CIRCULAR distance to the current one, so the
   deck loops: after the last finding the first is the next card, and the stack
   behind the active card wraps too. Only transforms change, so the browser
   tweens between states instead of us re-rendering.

   offset is the forward distance 0..count-1: 0 is active, 1 and 2 stack behind,
   everything else is hidden. To keep the wrap (10->1 / 1->10) looking like any
   other step, the immediately-previous card (offset === count-1) is parked in
   the same "just above" spot the old linear code used for a card that has just
   left, so Next fades it up and out while Prev slides it back down into place.
   Every other hidden card waits just below the stack and rises in as you go. */
function position() {
  const count = cards.length;
  cards.forEach((el, i) => {
    const offset = count ? (((i - index) % count) + count) % count : 0;
    if (offset === 0) {                       // active
      el.style.opacity = "1";
      el.style.transform = "translate(-50%, 0) scale(1)";
      el.style.zIndex = "50";
      el.toggleAttribute("aria-hidden", false);
    } else if (offset === 1 || offset === 2) { // stacked to the RIGHT (horizontal)
      // kept fairly opaque so the cards behind read clearly, not as faint ghosts;
      // wider offset + gentle scale so a thick edge of each peeks out on the right
      el.style.opacity = offset === 1 ? ".8" : ".6";
      el.style.transform =
        `translate(calc(-50% + ${offset * 48}px), 0) scale(${1 - offset * 0.045})`;
      el.style.zIndex = String(50 - offset);
      el.toggleAttribute("aria-hidden", true);
    } else {                                   // hidden
      // the previous card exits/enters to the LEFT; everything else to the RIGHT
      const x = offset === count - 1 ? -72 : 120;
      el.style.opacity = "0";
      el.style.transform = `translate(calc(-50% + ${x}px), 0) scale(.86)`;
      el.style.zIndex = "0";
      el.toggleAttribute("aria-hidden", true);
    }
    el.style.pointerEvents = offset === 0 ? "auto" : "none";
  });
  // Loop mode has no ends, so Prev/Next never disable - except a lone finding,
  // which cannot be navigated at all.
  const navigable = count > 1;
  $("#prev").disabled = !navigable;
  $("#next").disabled = !navigable;
  updateCounter();
  $$("#dots .dot").forEach((d, i) =>
    d.setAttribute("aria-current", String(i === index)));
}

function updateCounter() {
  const el = $("#counter");
  if (el) el.textContent = cards.length ? `Finding ${index + 1} of ${cards.length}` : "";
}

function go(delta) {
  const count = cards.length;
  if (count < 2) return;                 // a single finding: navigation is a no-op
  index = ((index + delta) % count + count) % count;   // wraps at both ends
  position();
}

function renderDots() {
  $("#dots").innerHTML = shown.map((f, i) => `
    <button class="dot" data-i="${i}" style="--dc:${SEV_COLOR[f.severity]}"
            aria-current="${i === index}" title="${esc(f.title)}"></button>`).join("");
  $$("#dots .dot").forEach((d) => d.addEventListener("click", () => {
    index = Number(d.dataset.i);
    position();
  }));
}

$("#prev").addEventListener("click", () => go(-1));
$("#next").addEventListener("click", () => go(1));

document.addEventListener("keydown", (event) => {
  if (document.documentElement.dataset.view !== "results") return;
  if (event.target.matches("input, select, textarea")) return;
  if (event.key === "ArrowRight") go(1);
  else if (event.key === "ArrowLeft") go(-1);
  else if (event.key.toLowerCase() === "g") toggleGrid();
  else if (event.key === "Escape") toggleGrid(false);
});

// swipe
let swipeFrom = null;
$("#deck").addEventListener("pointerdown", (e) => { swipeFrom = e.clientX; });
$("#deck").addEventListener("pointerup", (e) => {
  if (swipeFrom === null) return;
  const dx = e.clientX - swipeFrom;
  swipeFrom = null;
  if (Math.abs(dx) > 60) go(dx < 0 ? 1 : -1);
});

/* ── grid overview ───────────────────────────────────────────────────────── */

function renderGrid() {
  $("#grid").innerHTML = shown.map((f, i) => `
    <button class="grid-card ${f.certified ? "" : "advisory"}" data-i="${i}"
            style="--sev:${SEV_COLOR[f.severity]}">
      <span class="pill" style="--sev:${SEV_COLOR[f.severity]}">${esc(f.severity)}</span>
      <strong>${esc(f.title)}</strong>
    </button>`).join("");
  $$("#grid .grid-card").forEach((c) => c.addEventListener("click", () => {
    index = Number(c.dataset.i);
    toggleGrid(false);
    position();
  }));
}

function toggleGrid(force) {
  const grid = $("#grid");
  const show = force === undefined ? grid.hidden : force;
  if (show && !shown.length) return;
  grid.hidden = !show;
  $("#deck").hidden = show || !shown.length;
  $("#deck-nav").hidden = show || !shown.length;
  $$(".hint").forEach((h) => (h.hidden = show || !shown.length));
}

/* ── boot ────────────────────────────────────────────────────────────────── */

api("/api/me")
  .then((me) => { $("#who").textContent = me.username; view("upload"); loadRecent(); })
  .catch(() => view("login"));
