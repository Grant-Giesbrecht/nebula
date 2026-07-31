// Nebula Navigator -- front-end logic.
//
// No mock data: every session, item, and sidecar comes from the Python
// backend via the single `bridge` Tauri command. `call(op, args)` is the
// one entry point; everything else is rendering.

const invoke = window.__TAURI__.core.invoke;
const dialogOpen = window.__TAURI__.dialog.open;

// Must match nebula.sidecar.SIDECAR_SUFFIX.
const SIDECAR_SUFFIX = ".meta.json";

const STATUS = {
  paired:  { cls: "ok",   badge: "ok" },
  drifted: { cls: "warn", badge: "warn" },
  orphan:  { cls: "err",  badge: "err" },
  stray:   { cls: "warn", badge: "warn" },
};

async function call(op, args) {
  return await invoke("bridge", { op, args: args || {} });
}

// ---- state --------------------------------------------------------------
let archive = localStorage.getItem("nebula.archive") || null;
let archiveLabel = null;
let sessions = [];
let curSession = null;
let items = [];
let listView = true, showMeta = true, verify = false;
let selected = null, selectedIsSidecar = false;

const $ = (id) => document.getElementById(id);

// ---- glyphs -------------------------------------------------------------
const EXT_COLOR = {
  csv:"#1a9d5a", tsv:"#1a9d5a", json:"#c67c12", yaml:"#c67c12", yml:"#c67c12",
  png:"#8b5cf6", jpg:"#8b5cf6", jpeg:"#8b5cf6", gif:"#8b5cf6", svg:"#8b5cf6",
  h5:"#2f6bff", hdf5:"#2f6bff", npy:"#2f6bff", npz:"#2f6bff", mat:"#2f6bff", dat:"#2f6bff",
  graf:"#e0559b", py:"#2f6bff", ipynb:"#2f6bff", txt:"#6b7686", log:"#6b7686",
  md:"#6b7686", pdf:"#d33f3f", zip:"#b08500", tar:"#b08500", gz:"#b08500",
};
function ext(name) { const i = name.lastIndexOf("."); return i < 0 ? "" : name.slice(i + 1).toLowerCase(); }

function badgeSVG(kind, size) {
  const c = kind === "ok" ? "var(--ok)" : kind === "warn" ? "var(--warn)" : "var(--err)";
  const glyph = kind === "ok"
    ? '<path d="M4 8.5l2.5 2.5L12 5" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>'
    : kind === "warn"
    ? '<path d="M8 4v5" stroke="#fff" stroke-width="2" stroke-linecap="round"/><circle cx="8" cy="12" r="1.1" fill="#fff"/>'
    : '<path d="M5 5l6 6M11 5l-6 6" stroke="#fff" stroke-width="2" stroke-linecap="round"/>';
  return `<span class="badge" style="width:${size}px;height:${size}px;background:${c}">
    <svg width="${size - 5}" height="${size - 5}" viewBox="0 0 16 16">${glyph}</svg></span>`;
}
function fileGlyph(item, px) {
  const missing = item.status === "stray";
  const col = missing ? "var(--miss)" : (EXT_COLOR[ext(item.name)] || "#6b7686");
  const label = ext(item.name).slice(0, 4).toUpperCase();
  const w = px, h = Math.round(px * 1.24);
  const dash = missing ? 'stroke-dasharray="4 3"' : "";
  const doc = `
    <svg width="${w}" height="${h}" viewBox="0 0 40 50">
      <path d="M6 2 h20 l12 12 v32 a2 2 0 0 1-2 2 H8 a2 2 0 0 1-2-2 V4 a2 2 0 0 1 2-2 z"
            fill="${missing ? 'transparent' : col}" opacity="${missing ? 1 : 0.16}"
            stroke="${col}" stroke-width="1.6" ${dash}/>
      <path d="M26 2 v10 a2 2 0 0 0 2 2 h10" fill="none" stroke="${col}" stroke-width="1.6" ${dash}/>
      <text x="21" y="34" text-anchor="middle" font-size="9.5" font-weight="800" fill="${col}">${missing ? '?' : label}</text>
    </svg>`;
  const bsize = Math.round(px * 0.5);
  const badge = STATUS[item.status] ? badgeSVG(STATUS[item.status].badge, bsize) : "";
  return `<span class="glyph" style="width:${w}px;height:${h}px">${doc}${badge}</span>`;
}
function pill(item) {
  const s = STATUS[item.status];
  return `<span class="pill ${s.cls}"><span class="dot"></span>${escapeHtml(item.status_label)}</span>`;
}
function folderSVG() {
  return '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M10 4H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-8l-2-2z"/></svg>';
}
function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtCreated(ts) { return (ts || "").slice(0, 19).replace("T", " "); }

// ---- archive / data loading --------------------------------------------
async function openArchive() {
  const dir = await dialogOpen({ directory: true, title: "Choose a nebula archive" });
  if (!dir) return;
  await loadArchive(dir);
}

async function loadArchive(arc) {
  try {
    const { label } = await call("resolve", { archive: arc });
    archive = arc;
    archiveLabel = label;
    localStorage.setItem("nebula.archive", arc);
    $("wtitle").textContent = `Nebula Navigator — ${label}`;
    await reload();
  } catch (e) {
    toast(`Could not open archive: ${e}`);
  }
}

async function reload() {
  if (!archive) return;
  sessions = await call("list_sessions", { archive });
  renderSessions();
  if (sessions.length) {
    await selectSession(sessions[0]);
  } else {
    curSession = null; items = []; selected = null;
    $("itemArea").innerHTML = `<div class="empty">No sessions in this archive.</div>`;
    updateDetails();
  }
  $("statusbar").textContent = `${sessions.length} session(s)`;
}

function renderSessions() {
  $("sessionList").innerHTML = sessions.map((s, i) => {
    const held = s.held ? '<span class="tag-held">HELD</span>' : "";
    const prob = s.n_problems ? `<span class="tag-prob">${s.n_problems} ⚠</span>` : "";
    const sel = curSession && s.run_id === curSession.run_id ? "sel" : "";
    const line1 = `${escapeHtml(s.run_id)} <span class="desc">${escapeHtml(s.description)}</span>`;
    return `<div class="session ${sel}" data-i="${i}">
      <span class="folder">${folderSVG()}</span>
      <div class="meta">
        <div class="line1"><span class="rid">${line1}</span></div>
        <div class="line2"><span>${escapeHtml(s.status)}</span>${held}${prob}</div>
      </div>
      <span class="more" data-open="${i}" title="Open in file manager">⋯</span>
    </div>`;
  }).join("");
  $("sessionList").querySelectorAll(".session").forEach((el) => {
    el.onclick = (ev) => {
      const openIdx = ev.target.getAttribute("data-open");
      if (openIdx !== null) { call("open_path", { path: sessions[+openIdx].path }); return; }
      selectSession(sessions[+el.dataset.i]);
    };
  });
}

async function selectSession(s) {
  curSession = s;
  selected = null; selectedIsSidecar = false;
  renderSessions();
  await reloadItems();
  updateDetails();
}

async function reloadItems() {
  if (!curSession) return;
  items = await call("list_items", { session_path: curSession.path, verify });
  $("itemArea").innerHTML = listView ? listHTML() : gridHTML();
  wireItems();
  const problems = items.filter((i) => i.status !== "paired").length;
  $("statusbar").textContent = `${curSession.run_id}: ${items.length} item(s), ${problems} problem(s)`;
}

// ---- views --------------------------------------------------------------
function listHTML() {
  const rows = items.map((it, idx) => {
    const created = fmtCreated(it.timestamp);
    let r = `<tr data-i="${idx}" data-sc="0" class="${sameSel(idx, false) ? 'sel' : ''}">
      <td><div class="namecell">${fileGlyph(it, 20)}<span class="fname">${escapeHtml(it.name)}</span></div></td>
      <td class="created">${created}</td>
      <td>${pill(it)}</td></tr>`;
    if (it.has_sidecar && showMeta) {
      r += `<tr data-i="${idx}" data-sc="1" class="sidecar ${sameSel(idx, true) ? 'sel' : ''}">
        <td><div class="namecell"><span style="width:20px;text-align:center;color:var(--text-faint)">↳</span><span class="fname">${escapeHtml(it.name + SIDECAR_SUFFIX)}</span></div></td>
        <td class="created">${created}</td>
        <td><span class="pill meta">metadata</span></td></tr>`;
    }
    return r;
  }).join("");
  return `<table><thead><tr><th>Name</th><th class="c-created">Created</th><th class="c-status">Status</th></tr></thead><tbody>${rows}</tbody></table>`;
}
function gridHTML() {
  return `<div class="grid">${items.map((it, idx) => `
    <div class="cell ${sameSel(idx, false) ? 'sel' : ''}" data-i="${idx}" data-sc="0" title="${escapeHtml(it.detail)}">
      ${fileGlyph(it, 54)}<span class="cname">${escapeHtml(it.name)}</span></div>`).join("")}</div>`;
}
function sameSel(idx, isSc) { return selected === items[idx] && selectedIsSidecar === isSc; }

function wireItems() {
  $("itemArea").querySelectorAll("[data-i]").forEach((el) => {
    const it = items[+el.dataset.i];
    const isSc = el.dataset.sc === "1";
    el.onclick = () => selectItem(it, isSc);
    el.ondblclick = () => activate(it, isSc);
  });
}

function selectItem(it, isSc) {
  selected = it; selectedIsSidecar = isSc;
  $("itemArea").innerHTML = listView ? listHTML() : gridHTML();
  wireItems();
  updateDetails();
}
function activate(it, isSc) {
  selectItem(it, isSc);
  if (isSc) openSidecarPanel(it);
  else if (it.has_artifact) call("open_path", { path: it.artifact_path });
  else if (it.has_sidecar) openSidecarPanel(it);
}

function updateDetails() {
  const it = selected;
  const hasArt = !!(it && it.has_artifact);
  const hasSc = !!(it && it.has_sidecar);
  $("openArt").disabled = !hasArt;
  $("openSc").disabled = !hasSc;
  $("editSc").disabled = !hasSc;
  if (!it) { $("detText").textContent = "Select an item to see its provenance."; return; }
  const lines = it.detail.split("\n");
  $("detText").innerHTML = lines
    .map((ln, i) => (i === 0 ? `<span class="hl">${escapeHtml(ln)}</span>` : escapeHtml(ln)))
    .join("\n");
}

// ---- sidecar panel ------------------------------------------------------
async function openSidecarPanel(it) {
  if (!it || !it.sidecar_path) return;
  const { text } = await call("sidecar_display", { sidecar_path: it.sidecar_path });
  $("scTitle").textContent = `Sidecar — ${it.name}`;
  $("scBody").innerHTML = colorJSON(text);
  $("scPanel").classList.remove("hidden");
}
function colorJSON(s) {
  return escapeHtml(s)
    .replace(/(&quot;(?:\\.|[^&]|&(?!quot;))*?&quot;)(\s*:)?/g,
      (m, str, colon) => colon ? `<span class="k">${str}</span>${colon}` : `<span class="s">${str}</span>`)
    .replace(/: (-?\d+(?:\.\d+)?)/g, ': <span class="n">$1</span>');
}

// ---- import -------------------------------------------------------------
let pendingPaths = [];
async function startImport() {
  if (!archive) { toast("Open an archive first."); return; }
  const picked = await dialogOpen({ multiple: true, title: "Choose files to import" });
  if (!picked) return;
  pendingPaths = Array.isArray(picked) ? picked : [picked];
  await showImportDialog();
}
async function showImportDialog() {
  $("dlgHead").textContent = `Import ${pendingPaths.length} file(s)`;
  $("dlgFiles").innerHTML = pendingPaths
    .map((p) => `<span class="f">${escapeHtml(p.split(/[\\/]/).pop())}</span>`).join("");

  const importable = await call("importable_sessions", { archive });
  const sel = $("dlgSession");
  sel.innerHTML = importable
    .map((s) => `<option value="${escapeHtml(s.run_id)}">${escapeHtml(s.run_id)}   ${escapeHtml(s.description)}</option>`)
    .join("");
  if (curSession && importable.some((s) => s.run_id === curSession.run_id)) {
    sel.value = curSession.run_id;
  }
  const canExisting = importable.length > 0;
  $("modeExisting").disabled = !canExisting;
  document.querySelector(`input[name="mode"][value="${canExisting ? 'existing' : 'new'}"]`).checked = true;
  syncMode();
  $("dlgTags").value = ""; $("dlgDesc").value = ""; $("dlgOrigin").value = "";
  $("scrim").classList.add("show");
}
function syncMode() {
  const existing = $("modeExisting").checked;
  $("dlgSession").disabled = !existing;
  $("newFields").style.display = existing ? "none" : "flex";
}
async function doImport() {
  const origin = $("dlgOrigin").value.trim() || null;
  try {
    let targetRunId;
    if ($("modeExisting").checked) {
      targetRunId = $("dlgSession").value;
      await call("import_file", { archive, run_id: targetRunId, paths: pendingPaths, origin, allow_frozen: true });
    } else {
      const tags = $("dlgTags").value.split(",").map((t) => t.trim()).filter(Boolean);
      const res = await call("import_new", { archive, paths: pendingPaths, tags, description: $("dlgDesc").value.trim(), origin });
      targetRunId = res.run_id;
    }
    $("scrim").classList.remove("show");
    await reload();
    const target = sessions.find((s) => s.run_id === targetRunId);
    if (target) await selectSession(target);
    toast(`Imported ${pendingPaths.length} file(s) into ${targetRunId}`);
  } catch (e) {
    toast(`Import failed: ${e}`);
  }
}

// ---- misc UI ------------------------------------------------------------
let toastTimer = null;
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 3200);
}

function setView(list) {
  listView = list;
  $("viewList").classList.toggle("on", list);
  $("viewGrid").classList.toggle("on", !list);
  if (curSession) { $("itemArea").innerHTML = list ? listHTML() : gridHTML(); wireItems(); }
}

// ---- wiring -------------------------------------------------------------
$("openArchive").onclick = openArchive;
$("refresh").onclick = () => reload();
$("viewList").onclick = () => setView(true);
$("viewGrid").onclick = () => setView(false);
$("meta").onchange = (e) => { showMeta = e.target.checked; if (curSession) { $("itemArea").innerHTML = listHTML(); wireItems(); } };
$("verify").onchange = (e) => { verify = e.target.checked; reloadItems(); };
$("openArt").onclick = () => selected && selected.artifact_path && call("open_path", { path: selected.artifact_path });
$("editSc").onclick = () => selected && selected.sidecar_path && call("open_path", { path: selected.sidecar_path });
$("openSc").onclick = () => selected && openSidecarPanel(selected);
$("scClose").onclick = () => $("scPanel").classList.add("hidden");
$("importBtn").onclick = startImport;
$("modeExisting").onchange = syncMode;
$("modeNew").onchange = syncMode;
$("dlgCancel").onclick = () => $("scrim").classList.remove("show");
$("dlgImport").onclick = doImport;
$("scrim").onclick = (e) => { if (e.target === $("scrim")) $("scrim").classList.remove("show"); };
$("themeBtn").onclick = () => {
  const r = document.documentElement;
  const cur = r.getAttribute("data-theme") ||
    (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  r.setAttribute("data-theme", cur === "dark" ? "light" : "dark");
};

// ---- boot ---------------------------------------------------------------
if (archive) {
  loadArchive(archive).catch((e) => toast(`Startup error: ${e}`));
}
