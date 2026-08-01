// Nebula Navigator -- front-end logic.
//
// No mock data: every session, item, and sidecar comes from the Python
// backend via the single `bridge` Tauri command. `call(op, args)` is the
// one entry point; everything else is rendering.

const invoke = window.__TAURI__.core.invoke;
const dialogOpen = window.__TAURI__.dialog.open;
const tauriEvent = window.__TAURI__.event;

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
// `archives` is the list of archives the user has open (one at a time is
// active); `registry` is what ~/.nebula/archives.yaml knows about, offered
// in the switcher so known archives don't have to be hunted for on disk.
let archives = [];          // [{ id, label }] -- id is a path or registered name
let registry = [];          // [{ name, root, exists }]
let archive = null;         // active archive id
let sessions = [];
let curSession = null;
let items = [];
let listView = true, showMeta = true, verify = false;
let selected = null, selectedIsSidecar = false;

// search: the rail filters the already-loaded session list locally, while
// artefact search asks the backend to walk the whole archive.
let shownSessions = [];
let sessQuery = "";
let sessCfg = { titles: true, ids: true, tags: true, userTags: true,
                open: true, closed: true, crashed: true, clean: true, dirty: true };
// from/to are stored as ISO (YYYY-MM-DD) for the backend but shown as
// YYYY/MM/DD; `dates` is the master switch for the whole date filter.
let itemCfg = { name: true, tags: true, origin: true, session: true,
                userTags: true, comments: true,
                dates: false, from: "", to: "" };
let searchMode = false, searchMeta = null, searchTimer = null;

// panels
let showSess = false, showSc = false;
let sessInfo = null, sessRaw = false;
let scInfo = null, scRaw = false;
// In-progress annotation edits, kept out of the panel data so an
// async lineage/code refresh can't wipe what is being typed.
let scNotes = null, sessNotes = null;

const $ = (id) => document.getElementById(id);

// ---- persistence --------------------------------------------------------
const LS = {
  get(key, fallback) {
    try {
      const v = localStorage.getItem(key);
      return v === null ? fallback : JSON.parse(v);
    } catch (e) { return fallback; }
  },
  set(key, value) { localStorage.setItem(key, JSON.stringify(value)); },
};

function saveArchives() {
  LS.set("nebula.archives", archives);
  localStorage.setItem("nebula.archive", archive || "");
}

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
// Dates read YYYY/MM/DD throughout the UI; ISO stays on the wire.
function fmtCreated(ts) {
  return (ts || "").slice(0, 19).replace("T", " ").replace(/^(\d{4})-(\d{2})-(\d{2})/, "$1/$2/$3");
}
function fmtDay(iso) { return (iso || "").replace(/-/g, "/"); }

// Accepts YYYY/MM/DD (and tolerates YYYY-MM-DD, since that is what a
// pasted ISO timestamp looks like). Returns ISO, or null if unusable.
function parseDay(text) {
  const t = (text || "").trim();
  if (!t) return "";
  const m = t.match(/^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$/);
  if (!m) return null;
  const [y, mo, d] = [m[1], +m[2], +m[3]];
  if (mo < 1 || mo > 12 || d < 1 || d > 31) return null;
  return `${y}-${String(mo).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
}
function baseName(p) { return String(p).split(/[\\/]/).filter(Boolean).pop() || String(p); }

// ---- archives -----------------------------------------------------------
function activeLabel() {
  const a = archives.find((x) => x.id === archive);
  return a ? a.label : (archive || "");
}

function renderArchiveSelect() {
  const sel = $("archiveSel");
  const open = archives
    .map((a) => `<option value="open:${escapeHtml(a.id)}"${a.id === archive ? " selected" : ""}>${escapeHtml(a.label)}</option>`)
    .join("");
  const known = registry
    .filter((r) => !archives.some((a) => a.id === r.name || a.id === r.root))
    .map((r) => `<option value="reg:${escapeHtml(r.name)}">${escapeHtml(r.name)}${r.exists ? "" : "  (not mounted)"}</option>`)
    .join("");
  sel.innerHTML =
    (open ? `<optgroup label="Open">${open}</optgroup>` : "") +
    (known ? `<optgroup label="Registered">${known}</optgroup>` : "") +
    (open || known ? "" : `<option value="">(no archive open)</option>`);
  $("closeArchive").disabled = !archive;
}

async function onArchivePicked(value) {
  if (!value) return;
  const [kind, id] = [value.slice(0, value.indexOf(":")), value.slice(value.indexOf(":") + 1)];
  if (kind === "reg") await loadArchive(id);       // adds it to the open list
  else if (id !== archive) await loadArchive(id);
}

async function openArchive() {
  const dir = await dialogOpen({ directory: true, title: "Choose a nebula archive" });
  if (!dir) return;
  await loadArchive(dir);
}

async function loadArchive(arc) {
  try {
    const { label } = await call("resolve", { archive: arc });
    if (arc !== archive) {
      // Run ids are per-archive, so carrying a selection across would land
      // on an unrelated session that merely shares an id.
      curSession = null; selected = null; sessInfo = null; scInfo = null;
      showSc = false; updateDock();
      // Results from the previous archive would be misleading here.
      searchMode = false; searchMeta = null;
      $("itemSearch").value = "";
      $("itemSearchClear").classList.add("hidden");
    }
    archive = arc;
    const existing = archives.find((a) => a.id === arc);
    if (existing) existing.label = label;
    else archives.push({ id: arc, label });
    saveArchives();
    renderArchiveSelect();
    $("wtitle").textContent = `Nebula Navigator — ${label}`;
    await reload();
  } catch (e) {
    renderArchiveSelect();   // undo an optimistic <select> change
    toast(`Could not open archive: ${e}`);
  }
}

function closeArchive() {
  if (!archive) return;
  archives = archives.filter((a) => a.id !== archive);
  archive = archives.length ? archives[0].id : null;
  saveArchives();
  if (archive) { loadArchive(archive); return; }
  sessions = []; curSession = null; items = []; selected = null;
  sessInfo = null; scInfo = null; searchMode = false; searchMeta = null;
  renderArchiveSelect();
  applySessionFilter();
  $("wtitle").textContent = "Nebula Navigator";
  $("itemArea").innerHTML = `<div class="empty">Open an archive to begin.</div>`;
  $("statusbar").textContent = "No archive open";
  renderSessionPanel();
  updateDetails();
}

// ---- data loading -------------------------------------------------------
async function reload() {
  if (!archive) return;
  sessions = await call("list_sessions", { archive });
  applySessionFilter();
  if (sessions.length) {
    const keep = curSession && sessions.find((s) => s.run_id === curSession.run_id);
    await selectSession(keep || shownSessions[0] || sessions[0]);
  } else {
    curSession = null; items = []; selected = null; sessInfo = null;
    $("itemArea").innerHTML = `<div class="empty">No sessions in this archive.</div>`;
    renderSessionPanel();
    updateDetails();
  }
  $("statusbar").textContent = `${activeLabel()} — ${sessions.length} session(s)`;
}

// ---- session search (local: the whole list is already in memory) --------
function sessionMatches(s) {
  // Two independent gates: the session's own state, then the text query.
  const status = (s.status || "").toLowerCase();
  const statusOk = status === "open" ? sessCfg.open
    : status === "crashed" ? sessCfg.crashed
    : status === "closed" ? sessCfg.closed
    : (sessCfg.open || sessCfg.closed || sessCfg.crashed);   // unknown status
  if (!statusOk) return false;
  if (!(s.n_problems ? sessCfg.dirty : sessCfg.clean)) return false;

  const terms = sessQuery.toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return true;
  const hay = [];
  if (sessCfg.titles) hay.push(s.description || "");
  if (sessCfg.ids) hay.push(s.run_id || "");
  if (sessCfg.tags) hay.push((s.tags || []).join(" "));
  if (sessCfg.userTags) hay.push((s.user_tags || []).join(" "));
  const blob = hay.join(" ").toLowerCase();
  return terms.every((t) => blob.includes(t));
}

function applySessionFilter() {
  shownSessions = sessions.filter(sessionMatches);
  const filtered = shownSessions.length !== sessions.length;
  $("sessCount").textContent = filtered
    ? `${shownSessions.length} of ${sessions.length}` : "";
  $("sessSearchClear").classList.toggle("hidden", !sessQuery);
  $("sessCfgBtn").classList.toggle("on", isSessCfgNarrowed());
  renderSessions();
}

// True when the options exclude something, so the ⚙ can show it is doing
// work even with an empty query.
function isSessCfgNarrowed() {
  return !(sessCfg.titles && sessCfg.ids && sessCfg.tags && sessCfg.userTags &&
           sessCfg.open && sessCfg.closed && sessCfg.crashed &&
           sessCfg.clean && sessCfg.dirty);
}

function renderSessions() {
  if (!shownSessions.length && (sessQuery || isSessCfgNarrowed()) && sessions.length) {
    $("sessionList").innerHTML = `<div class="none">No sessions match.</div>`;
    return;
  }
  $("sessionList").innerHTML = shownSessions.map((s, i) => {
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
      if (openIdx !== null) { call("open_path", { path: shownSessions[+openIdx].path }); return; }
      selectSession(shownSessions[+el.dataset.i]);
    };
  });
}

async function selectSession(s) {
  curSession = s;
  selected = null; selectedIsSidecar = false;
  renderSessions();
  await reloadItems();
  updateDetails();
  await refreshSessionInfo();
}

// ---- artefact search (backend: walks every session in the archive) ------
function datesOn() { return !!(itemCfg.dates && (itemCfg.from || itemCfg.to)); }

function itemSearchActive() {
  return !!($("itemSearch").value.trim() || datesOn());
}

function scheduleItemSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => runItemSearch(), 220);
}

async function runItemSearch() {
  $("itemSearchClear").classList.toggle("hidden", !itemSearchActive());
  $("itemCfgBtn").classList.toggle("on", datesOn());
  if (!archive) return;
  if (!itemSearchActive()) { await exitSearch(); return; }

  const fields = ["name", "tags", "origin", "session", "userTags", "comments"]
    .filter((f) => itemCfg[f])
    .map((f) => (f === "name" ? "filename" : f === "userTags" ? "user_tags" : f));
  try {
    const res = await call("search_items", {
      archive, query: $("itemSearch").value, fields,
      date_from: (itemCfg.dates && itemCfg.from) || null,
      date_to: (itemCfg.dates && itemCfg.to) || null,
    });
    searchMode = true;
    searchMeta = res;
    items = res.items;
    selected = null; selectedIsSidecar = false;
    renderItemArea();
    updateDetails();
    const bits = [`${res.items.length}${res.truncated ? "+" : ""} match(es)`,
                  `${res.n_scanned} artefact(s) in ${res.n_sessions} session(s)`];
    $("statusbar").textContent = `${activeLabel()} — search: ${bits.join(", scanned ")}`;
  } catch (e) {
    toast(`Search failed: ${e}`);
  }
}

async function exitSearch() {
  if (!searchMode) return;
  searchMode = false; searchMeta = null;
  selected = null; selectedIsSidecar = false;
  if (curSession) await reloadItems();
  else { items = []; renderItemArea(); updateDetails(); }
}

function clearItemSearch() {
  $("itemSearch").value = "";
  resetDates();
  runItemSearch();
}

function resetDates() {
  itemCfg.from = ""; itemCfg.to = "";
  $("ifFrom").value = ""; $("ifTo").value = "";
  $("ifFrom").classList.remove("bad"); $("ifTo").classList.remove("bad");
  saveItemCfg();
}

// Turning the master switch off must not lose what was typed, so the
// values stay in itemCfg -- they just stop being sent.
function syncDateFields() {
  $("ifDates").checked = !!itemCfg.dates;
  $("ifDateFields").classList.toggle("off", !itemCfg.dates);
  $("ifFrom").disabled = $("ifTo").disabled = !itemCfg.dates;
}

// Jump from a search hit to the session that holds it.
async function jumpToSession(runId) {
  const s = sessions.find((x) => x.run_id === runId);
  if (!s) { toast(`${runId} is not in the session list.`); return; }
  $("itemSearch").value = "";
  resetDates();
  $("itemSearchClear").classList.add("hidden");
  searchMode = false; searchMeta = null;
  await selectSession(s);
}

function renderItemArea() {
  $("itemArea").innerHTML = listView ? listHTML() : gridHTML();
  wireItems();
}

async function reloadItems() {
  if (!curSession) return;
  if (searchMode) return;   // a search owns the item area until it's cleared
  items = await call("list_items", { session_path: curSession.path, verify });
  $("itemArea").innerHTML = listView ? listHTML() : gridHTML();
  wireItems();
  const problems = items.filter((i) => i.status !== "paired").length;
  $("statusbar").textContent = `${activeLabel()} — ${curSession.run_id}: ${items.length} item(s), ${problems} problem(s)`;
}

// ---- views --------------------------------------------------------------
// In search mode the same table gains a Session column, since results come
// from all over the archive rather than one open session.
function sessionCell(it) {
  return `<td class="c-sess"><span class="sesslink" data-jump="${escapeHtml(it.run_id)}"
    title="${escapeHtml(it.session_description || "")} — go to this session">${escapeHtml(it.run_id)}</span></td>`;
}

function listHTML() {
  if (searchMode && !items.length) {
    return `<div class="empty">Nothing matched. Try fewer words, or widen the search under ⚙ Advanced.</div>`;
  }
  const rows = items.map((it, idx) => {
    const created = fmtCreated(it.timestamp);
    let r = `<tr data-i="${idx}" data-sc="0" class="${sameSel(idx, false) ? 'sel' : ''}">
      <td><div class="namecell">${fileGlyph(it, 20)}<span class="fname">${escapeHtml(it.display_name || it.name)}</span>${dupBadge(it)}</div></td>
      ${searchMode ? sessionCell(it) : ""}
      <td class="created">${created}</td>
      <td>${pill(it)}</td></tr>`;
    if (it.has_sidecar && showMeta) {
      r += `<tr data-i="${idx}" data-sc="1" class="sidecar ${sameSel(idx, true) ? 'sel' : ''}">
        <td><div class="namecell"><span style="width:20px;text-align:center;color:var(--text-faint)">↳</span><span class="fname">${escapeHtml(it.name + SIDECAR_SUFFIX)}</span></div></td>
        ${searchMode ? "<td></td>" : ""}
        <td class="created">${created}</td>
        <td><span class="pill meta">metadata</span></td></tr>`;
    }
    return r;
  }).join("");
  const sessHead = searchMode ? `<th class="c-sess">Session</th>` : "";
  return `<table><thead><tr><th>Name</th>${sessHead}<th class="c-created">Created</th><th class="c-status">Status</th></tr></thead><tbody>${rows}</tbody></table>`;
}
function gridHTML() {
  if (searchMode && !items.length) {
    return `<div class="empty">Nothing matched. Try fewer words, or widen the search under ⚙ Advanced.</div>`;
  }
  return `<div class="grid">${items.map((it, idx) => `
    <div class="cell ${sameSel(idx, false) ? 'sel' : ''}" data-i="${idx}" data-sc="0" title="${escapeHtml(it.detail)}">
      ${fileGlyph(it, 54)}<span class="cname">${escapeHtml(it.display_name || it.name)}${dupBadge(it)}</span>
      ${searchMode ? `<span class="cell-sess">${escapeHtml(it.run_id)}</span>` : ""}</div>`).join("")}</div>`;
}
// Duplicates keep the name that was asked for, with their write order
// alongside -- the real filename is one hover away.
function dupBadge(it) {
  if (!it.is_duplicate) return "";
  return `<span class="dup" title="written as ${escapeHtml(it.name)} — ` +
    `nebula renamed it so it would not overwrite ${escapeHtml(it.display_name)}">` +
    `${it.position} of ${it.total}</span>`;
}

function sameSel(idx, isSc) { return selected === items[idx] && selectedIsSidecar === isSc; }

function wireItems() {
  $("itemArea").querySelectorAll("[data-i]").forEach((el) => {
    const it = items[+el.dataset.i];
    const isSc = el.dataset.sc === "1";
    el.onclick = (ev) => {
      const jump = ev.target.getAttribute && ev.target.getAttribute("data-jump");
      if (jump) { ev.stopPropagation(); jumpToSession(jump); return; }
      selectItem(it, isSc);
    };
    el.ondblclick = () => activate(it, isSc);
  });
}

function selectItem(it, isSc) {
  selected = it; selectedIsSidecar = isSc;
  $("itemArea").innerHTML = listView ? listHTML() : gridHTML();
  wireItems();
  updateDetails();
  if (showSc && it.has_sidecar) openSidecarPanel(it);
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
  if (!it) {
    $("detText").textContent = "Select an item to see its provenance.";
    $("detProv").innerHTML = "";
    return;
  }
  const lines = it.detail.split("\n");
  $("detText").innerHTML = lines
    .map((ln, i) => (i === 0 ? `<span class="hl">${escapeHtml(ln)}</span>` : escapeHtml(ln)))
    .join("\n");
  $("detProv").innerHTML = provenanceLine(it);
  wireEntryPoint($("detProv"), it);
}

// ---- entry point ---------------------------------------------------------
// Two ways to reach the script, resolved by the backend: the local checkout
// (editable, but possibly at a different commit) and a hosted URL pinned to
// the recorded commit. Prefer local, fall back to the host, and say so when
// neither is available -- resolved on click so selecting a file stays cheap.
function wireEntryPoint(el, it) {
  el.querySelectorAll("[data-entry]").forEach((n) => {
    n.onclick = (ev) => { ev.stopPropagation(); openEntryPoint(it); };
  });
}

async function openEntryPoint(it, prefer) {
  if (!it || !archive) return;
  let link;
  try {
    link = await call("entry_point_link", { archive, item: it });
  } catch (e) {
    toast(`Could not resolve the entry point: ${e}`);
    return;
  }
  const local = link.local, remote = link.remote;
  const wantRemote = prefer === "remote";

  if (!wantRemote && local && local.exists) {
    call("open_path", { path: local.path });
    if (local.matches_commit === false) {
      toast("Opened your working copy — it is at a different commit than the one recorded.");
    } else if (link.dirty) {
      toast("Opened your working copy — the tree was dirty when this ran, so it may differ.");
    }
    return;
  }
  if (remote && remote.url) {
    call("open_url", { url: remote.url });
    // Say *why* the host copy was used, so a silent fallback can't be
    // mistaken for "this is your file".
    const why = wantRemote ? "" : (link.note ? `${link.note} — ` : "no local copy — ");
    toast(`${why}opened the hosted copy at ${String(remote.url).includes("github") ? "GitHub" : "the host"}.`
          + (remote.warning ? ` ${remote.warning}` : ""));
    return;
  }
  if (wantRemote && local && local.exists) {
    toast("No hosted link for this commit — open the local copy with the filename instead.");
    return;
  }
  toast(link.note || "No way to open this entry point on this machine.");
}

// The "produced by" facts worth seeing without opening the panel: what
// built the file, and from where. Same phrasing as the sidecar panel.
function buildHTML(pb) {
  if (!pb.repo && !pb.commit) return "";
  const dirty = pb.dirty === true ? ' <span class="warn-text">(uncommitted changes)</span>'
    : pb.dirty === false ? ' <span class="ok-text">(clean)</span>' : "";
  return `${escapeHtml(pb.repo || "?")} @ <span class="mono">${escapeHtml((pb.commit || "?").slice(0, 8))}</span>${dirty}`;
}

function provenanceLine(it) {
  const build = buildHTML(it);
  const bits = [];
  if (build) bits.push(`<span class="dp"><span class="dp-k">Built from</span>${build}</span>`);
  if (it.entry_point) {
    bits.push(`<span class="dp"><span class="dp-k">Entry point</span>` +
      `<span class="mono link" data-entry="1" title="Open the script that produced this">` +
      `${escapeHtml(it.entry_point)}</span></span>`);
  }
  if (!bits.length) {
    // An imported file has no git provenance -- say what it does have.
    if (it.source === "external") {
      bits.push(`<span class="dp"><span class="dp-k">Imported</span>${escapeHtml(it.origin || "no origin recorded")}</span>`);
    } else if (it.has_sidecar) {
      bits.push(`<span class="dp dim">no build provenance recorded in this sidecar</span>`);
    }
  }
  if (it.is_duplicate) {
    bits.push(`<span class="dp"><span class="dp-k">Duplicate</span>` +
      `write ${it.position} of ${it.total}` +
      (it.original_name ? ` — asked for <span class="mono">${escapeHtml(it.original_name)}</span>,`
        + ` stored as <span class="mono">${escapeHtml(it.name)}</span>` : "") +
      `</span>`);
  }
  if (it.user_tags && it.user_tags.length) {
    bits.push(`<span class="dp"><span class="dp-k">Your tags</span>` +
      it.user_tags.map((t) => `<span class="chip user">${escapeHtml(t)}</span>`).join(" ") +
      `</span>`);
  }
  if (it.n_derived_from) {
    bits.push(`<span class="dp"><span class="dp-k">Derived from</span>${it.n_derived_from} source(s)</span>`);
  }
  return bits.join("");
}

// ---- pretty-print helpers (shared by both panels) -----------------------
function row(label, value, opts) {
  const o = opts || {};
  if (value === null || value === undefined || value === "") return "";
  const cls = ["v", o.mono ? "mono" : "", o.wrap ? "wrap" : ""].filter(Boolean).join(" ");
  const body = o.html ? value : escapeHtml(value);
  return `<div class="kv"><div class="k">${escapeHtml(label)}</div><div class="${cls}">${body}</div></div>`;
}
function group(title, inner) {
  if (!inner) return "";
  return `<div class="grp"><div class="grp-h">${escapeHtml(title)}</div>${inner}</div>`;
}
function chips(values, cls) {
  if (!values || !values.length) return "";
  return `<div class="chips">${values
    .map((v) => `<span class="chip ${cls || ""}">${escapeHtml(v)}</span>`).join("")}</div>`;
}
function valueText(v) {
  if (v === null || v === undefined) return "null";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}
function dictRows(obj) {
  if (!obj) return "";
  const keys = Object.keys(obj);
  if (!keys.length) return "";
  return keys.map((k) => row(k, valueText(obj[k]), { mono: true, wrap: true })).join("");
}
function colorJSON(s) {
  return escapeHtml(s)
    .replace(/(&quot;(?:\\.|[^&]|&(?!quot;))*?&quot;)(\s*:)?/g,
      (m, str, colon) => colon ? `<span class="k">${str}</span>${colon}` : `<span class="s">${str}</span>`)
    .replace(/: (-?\d+(?:\.\d+)?)/g, ': <span class="n">$1</span>');
}
function noteBox(kind, text) {
  return `<div class="note ${kind}">${escapeHtml(text)}</div>`;
}
function pathRow(label, path) {
  return row(label,
    `<span class="link" data-open-path="${escapeHtml(path)}" title="Open in file manager">${escapeHtml(path)}</span>`,
    { mono: true, wrap: true, html: true });
}
function wirePaths(el) {
  el.querySelectorAll("[data-open-path]").forEach((n) => {
    n.onclick = () => call("open_path", { path: n.getAttribute("data-open-path") });
  });
}

// ---- sidecar panel ------------------------------------------------------
async function openSidecarPanel(it) {
  if (!it || !it.sidecar_path) return;
  scInfo = await call("sidecar_info", { sidecar_path: it.sidecar_path });
  scInfo.itemName = it.name;
  showSc = true;
  savePanels();
  renderSidecarPanel();
  updateDock();
  // Lineage and code stats each need a scan, so fetch them after the
  // panel is already on screen rather than making the sidecar wait.
  await Promise.all([loadLineage(it), loadCodeInfo(it)]);
}

async function loadCodeInfo(it) {
  const code = scInfo && scInfo.produced_by && scInfo.produced_by.code;
  if (!archive || !code) return;
  try {
    const info = await call("code_info", { archive, code });
    if (scInfo && scInfo.itemName === it.name) {
      scInfo.codeInfo = info;
      renderSidecarPanel();
    }
  } catch (e) {
    console.error("code_info failed", e);
  }
}

async function loadLineage(it) {
  const sessionPath = it.session_path || (curSession && curSession.path);
  if (!archive || !sessionPath) return;
  try {
    const lin = await call("lineage", { archive, session_path: sessionPath, filename: it.name });
    if (scInfo && scInfo.itemName === it.name) {
      scInfo.lineage = lin;
      renderSidecarPanel();
    }
  } catch (e) {
    console.error("lineage failed", e);   // the panel is still useful without it
  }
}

function renderSidecarPanel() {
  const info = scInfo;
  const body = $("scBody");
  if (!info) { $("scTitle").textContent = "Sidecar"; body.innerHTML = ""; return; }
  $("scTitle").textContent = `Sidecar — ${info.itemName || baseName(info.name)}`;
  $("scRaw").classList.toggle("on", scRaw);

  if (scRaw || !info.ok) {
    const note = info.error ? noteBox("err", info.error) : "";
    body.innerHTML = note + `<pre>${colorJSON(info.raw || "")}</pre>`;
    return;
  }

  const pb = info.produced_by || {};
  // A sidecar written before `source` existed defaults to "script" -- say
  // so, instead of dressing an assumption up as a recorded fact.
  const assumed = info.source_recorded === false;
  const srcCls = assumed ? "miss" : pb.source === "external" ? "warn" : pb.source === "script" ? "ok" : "miss";
  const srcText = assumed ? `${pb.source || "script"}?` : (pb.source || "unknown source");
  const srcTitle = assumed
    ? "not recorded in this sidecar — assumed from the default"
    : "recorded in the sidecar";
  const head = `<div class="p-title">
      <div class="p-name">${escapeHtml(info.itemName || baseName(info.name))}</div>
      <span class="chip ${srcCls}" title="${escapeHtml(srcTitle)}">${escapeHtml(srcText)}</span>
    </div>`;

  const dup = selected && selected.is_duplicate
    ? row("Duplicate", `write ${selected.position} of ${selected.total}`
        + (selected.original_name
           ? ` — asked for <span class="mono">${escapeHtml(selected.original_name)}</span>`
           : ""), { html: true })
    : "";
  const overview = group("Overview",
    dup +
    row("Created", fmtCreated(info.created)) +
    row("SHA-256", info.sha256, { mono: true, wrap: true }) +
    pathRow("Sidecar", info.path));

  // The git fields say more together than apart: "nebula @ 48c4a28c (dirty)".
  const build = buildHTML(pb) || null;

  const provenance = group(
    pb.source === "external" ? "Imported" : "Produced by",
    row("Origin", pb.origin, { wrap: true }) +
    row("Built from", build, { html: true, wrap: true }) +
    row("Entry point", pb.entry_point
        ? `<span class="link" data-entry="1" title="Open the script">${escapeHtml(pb.entry_point)}</span>`
          + (pb.commit ? ` <span class="ext-link" data-entry-remote="1" title="View at this commit on the host">↗</span>` : "")
        : null, { mono: true, wrap: true, html: true }) +
    row("Imported by", pb.imported_by) +
    row("Imported at", fmtCreated(pb.imported_at)) +
    (assumed ? noteBox("info", "This sidecar predates the source/origin fields, "
                     + "so how the file arrived was never recorded.") : ""));

  const lineage = lineageHTML(info);
  const code = codeHTML(info);
  const inputs = group("Inputs", dictRows(info.inputs));
  const extra = group("Other fields", dictRows(info.extra));

  const notesDraft = scNotes || {
    tags: (selected && selected.user_tags) || [],
    comment: (selected && selected.user_comment) || "",
  };
  const notes = selected ? notesHTML("sc", notesDraft) : "";
  const all = overview + provenance + notes + lineage + code + inputs + extra;
  body.innerHTML = head + (all || noteBox("info", "This sidecar records no further detail."));
  wirePaths(body);
  wireLineage(body);
  wireEntryPoint(body, selected);
  body.querySelectorAll("[data-entry-remote]").forEach((n) => {
    n.onclick = (ev) => { ev.stopPropagation(); openEntryPoint(selected, "remote"); };
  });
  body.querySelectorAll("[data-restore]").forEach((n) => {
    n.onclick = (ev) => { ev.stopPropagation(); restoreCode(n.getAttribute("data-restore")); };
  });
  if (selected) {
    const sessionPath = selected.session_path || (curSession && curSession.path);
    wireNotes("sc", sessionPath, selected.name, (saved) => {
      selected.user_tags = saved.tags;
      selected.user_comment = saved.comment;
      updateDetails();
    });
  }
}

// ---- user annotations ---------------------------------------------------
// Mutable tags and a comment, stored in the session's annotations.yaml.
// Kept visually and conceptually apart from creation-time tags: those are
// a claim about the run, these are a note to yourself.
function notesHTML(kind, draft) {
  const tags = escapeHtml((draft.tags || []).join(", "));
  const comment = escapeHtml(draft.comment || "");
  return group("Your notes",
    `<div class="notes">
       <label class="notes-l">Tags <span class="hint">comma-separated; no commas or newlines inside a tag</span></label>
       <input class="notes-tags" id="${kind}NotesTags" value="${tags}" spellcheck="false"
              placeholder="e.g. shows-drift, paper:2026" />
       <label class="notes-l">Comment</label>
       <textarea class="notes-comment" id="${kind}NotesComment" rows="4"
                 placeholder="anything worth remembering about this">${comment}</textarea>
       <div class="notes-actions">
         <span class="notes-state" id="${kind}NotesState"></span>
         <button class="dbtn fill" id="${kind}NotesSave">Save notes</button>
       </div>
     </div>`);
}

function wireNotes(kind, sessionPath, filename, onSaved) {
  const tagsEl = $(`${kind}NotesTags`), commentEl = $(`${kind}NotesComment`);
  const stateEl = $(`${kind}NotesState`);
  if (!tagsEl || !commentEl) return;

  const draft = () => ({
    tags: tagsEl.value.split(",").map((t) => t.trim()).filter(Boolean),
    comment: commentEl.value,
  });
  const stash = () => {
    if (kind === "sc") scNotes = draft(); else sessNotes = draft();
    stateEl.textContent = "unsaved";
  };
  tagsEl.oninput = stash;
  commentEl.oninput = stash;

  $(`${kind}NotesSave`).onclick = async () => {
    const d = draft();
    try {
      const saved = await call("set_annotation", {
        session_path: sessionPath, filename: filename || null,
        tags: d.tags, comment: d.comment,
      });
      if (kind === "sc") scNotes = null; else sessNotes = null;
      stateEl.textContent = "saved";
      toast(filename ? `Saved notes for ${filename}` : "Saved session notes");
      if (onSaved) onSaved(saved);
    } catch (e) {
      stateEl.textContent = "";
      toast(`Could not save notes: ${e}`);   // e.g. a tag with a comma in it
    }
  };
}

// ---- captured source ----------------------------------------------------
// What was snapshotted for this artifact: which snapshot, how big, which
// repos it drew from, and how much of it the store already had.
function codeHTML(info) {
  const pb = info.produced_by || {};
  if (!pb.code) return "";
  const ci = info.codeInfo;
  const short = `<span class="mono" title="${escapeHtml(pb.code)}">${escapeHtml(pb.code.slice(0, 12))}</span>`;
  if (!ci) return group("Captured source", row("Snapshot", short, { html: true }));
  if (!ci.ok) {
    return group("Captured source",
      row("Snapshot", short, { html: true }) + noteBox("err", ci.error));
  }

  const repoChips = Object.entries(ci.repos || {})
    .map(([name, n]) => `<span class="chip">${escapeHtml(name)} <b>${n}</b></span>`).join("");
  const missing = ci.n_blobs - ci.blobs_present;
  return group("Captured source",
    row("Snapshot", short, { html: true }) +
    row("Files", `${ci.n_files} file(s) from ${Object.keys(ci.repos || {}).length} repo(s)`) +
    (repoChips ? `<div class="chips">${repoChips}</div>` : "") +
    row("Storage", `${ci.shared} kept (already stored) · ${ci.unique} only in this snapshot`) +
    (missing > 0 ? noteBox("err", `${missing} file(s) listed by this snapshot are missing from the store`) : "") +
    `<div class="grp-actions"><button class="dbtn ghost" data-restore="${escapeHtml(pb.code)}">
       Restore files…</button></div>`);
}

// Write the snapshot back out as real files, at their original paths, so
// the code that produced an artifact can be read (or run) as a tree.
async function restoreCode(code) {
  const parent = await dialogOpen({ directory: true, title: "Where should the source go?" });
  if (!parent) return;
  try {
    const res = await call("restore_code", { archive, code, dest_parent: parent });
    call("open_path", { path: res.dest });
    const bits = [`Restored ${res.n_written} file(s) to ${baseName(res.dest)}`];
    if (res.missing && res.missing.length) bits.push(`${res.missing.length} missing from the store`);
    if (res.rejected && res.rejected.length) bits.push(`${res.rejected.length} skipped as unsafe paths`);
    toast(bits.join(" — "));
  } catch (e) {
    toast(`Restore failed: ${e}`);
  }
}

// ---- lineage ------------------------------------------------------------
// Both directions of the provenance graph, as clickable rows: what this
// file came from, and what came from it. Downstream is the half the CLI's
// `show` never displays -- looking at a source file, nothing tells you
// anything derives from it.
function lineageRow(r, dir) {
  const arrow = dir === "up" ? "←" : "→";
  const label = r.whole_session ? `${r.run_id} (whole session)`
    : `${r.run_id}/${r.filename}`;
  const cls = r.resolved === false ? "unresolved" : r.exists ? "" : "missing";
  const note = r.note ? `<span class="ln-note">${escapeHtml(r.note)}</span>` : "";
  const clickable = r.exists && !r.whole_session;
  return `<div class="ln ${cls}${clickable ? " go" : ""}"
      ${clickable ? `data-goto-session="${escapeHtml(r.session_path)}" data-goto-file="${escapeHtml(r.filename)}"` : ""}
      title="${clickable ? "Go to this artefact" : escapeHtml(r.note || "")}">
      <span class="ln-arrow">${arrow}</span>
      <span class="ln-name">${escapeHtml(label)}</span>${note}</div>`;
}

function lineageHTML(info) {
  const lin = info.lineage;
  if (!lin) {
    // Fall back to the raw refs until the archive scan comes back.
    const refs = info.derived_from || [];
    return group("Lineage", refs.length
      ? `<div class="ln-h">Derived from</div>` +
        refs.map((r) => `<div class="ln"><span class="ln-arrow">←</span>
          <span class="ln-name">${escapeHtml(r.ref)}</span></div>`).join("")
      : "");
  }
  const up = lin.upstream || [], down = lin.downstream || [];
  if (!up.length && !down.length) {
    return group("Lineage", noteBox("info",
      "No recorded relationships: nothing lists this file as a source, and "
      + "it declares no sources of its own."));
  }
  const upHTML = up.length
    ? `<div class="ln-h">Derived from</div>` + up.map((r) => lineageRow(r, "up")).join("")
    : "";
  const downHTML = down.length
    ? `<div class="ln-h">Used by</div>` + down.map((r) => lineageRow(r, "down")).join("")
    : "";
  return group("Lineage", upHTML + downHTML);
}

function wireLineage(el) {
  el.querySelectorAll("[data-goto-session]").forEach((n) => {
    n.onclick = () => gotoArtifact(n.getAttribute("data-goto-session"),
                                   n.getAttribute("data-goto-file"));
  });
}

// Follow a lineage link: select the target's session, then the file in it.
async function gotoArtifact(sessionPath, filename) {
  const s = sessions.find((x) => x.path === sessionPath);
  if (!s) { toast("That session is not in the current archive view."); return; }
  if (!curSession || curSession.path !== sessionPath || searchMode) {
    searchMode = false; searchMeta = null;
    $("itemSearch").value = "";
    $("itemSearchClear").classList.add("hidden");
    await selectSession(s);
  }
  const target = items.find((i) => i.name === filename);
  if (!target) { toast(`${filename} is not in ${s.run_id}.`); return; }
  selectItem(target, false);
  openSidecarPanel(target);
}

// ---- session info panel -------------------------------------------------
async function refreshSessionInfo() {
  if (!showSess) return;
  if (!curSession) { sessInfo = null; renderSessionPanel(); return; }
  sessInfo = await call("session_info", { session_path: curSession.path });
  renderSessionPanel();
}

function renderSessionPanel() {
  const info = sessInfo;
  const body = $("sessBody");
  if (!info) {
    $("sessTitle").textContent = "Session";
    body.innerHTML = noteBox("info", "Select a session to see its details.");
    return;
  }
  $("sessTitle").textContent = `Session — ${info.run_id || baseName(info.session_path)}`;
  $("sessRaw").classList.toggle("on", sessRaw);

  if (sessRaw || !info.ok) {
    const note = info.error ? noteBox("err", info.error) : "";
    body.innerHTML = note + `<pre>${escapeHtml(info.raw || "")}</pre>`;
    return;
  }

  const stCls = info.status === "open" ? "ok" : info.status === "crashed" ? "err" : "miss";
  const head = `<div class="p-title">
      <div class="p-name">${escapeHtml(info.run_id)}</div>
      <span class="chip ${stCls}">${escapeHtml(info.status)}</span>
      ${info.held ? '<span class="chip warn">held</span>' : ""}
      ${info.appendable ? "" : '<span class="chip miss">frozen</span>'}
    </div>` +
    (info.description ? `<div class="p-desc">${escapeHtml(info.description)}</div>` : "");

  const about = group("About",
    chips(info.tags, "tag") +
    (info.user_tags && info.user_tags.length
      ? `<div class="chips">${info.user_tags
          .map((t) => `<span class="chip user">${escapeHtml(t)}</span>`).join("")}</div>`
      : "") +
    row("Created", fmtCreated(info.created)) +
    row("Hold until", info.hold_until === "forever" ? "indefinite" : fmtCreated(info.hold_until)) +
    pathRow("Folder", info.session_path));

  const counts = group("Contents",
    row("Items", String(info.n_items)) +
    row("Problems", info.n_problems
      ? `<span class="warn-text">${info.n_problems}</span>` : "none",
      { html: info.n_problems > 0 }) +
    row("Total size", info.size_human));

  const related = group("Related runs",
    (info.related_runs || []).length
      ? `<div class="chips">${info.related_runs
          .map((r) => `<span class="chip mono">${escapeHtml(r.ref)}</span>`).join("")}</div>`
      : "");

  const hist = (info.history || []).slice().reverse();
  const history = group(`History (${hist.length})`, hist.length
    ? `<div class="hist">${hist.map((h) => `
        <div class="hrow">
          <div class="hline">
            <span class="haction">${escapeHtml(h.action || "?")}</span>
            ${h.file ? `<span class="hfile mono">${escapeHtml(h.file)}</span>` : ""}
          </div>
          <div class="hmeta">${escapeHtml(fmtCreated(h.at))}${h.by ? ` · ${escapeHtml(h.by)}` : ""}</div>
          ${h.note ? `<div class="hnote">${escapeHtml(h.note)}</div>` : ""}
        </div>`).join("")}</div>`
    : "");

  const notesDraft = sessNotes || {
    tags: info.user_tags || [], comment: info.user_comment || "",
  };
  body.innerHTML = head + about + counts + notesHTML("sess", notesDraft) + related +
    (history || noteBox("info", "No manual operations recorded."));
  wirePaths(body);
  wireNotes("sess", info.session_path, null, (saved) => {
    sessInfo.user_tags = saved.tags;
    sessInfo.user_comment = saved.comment;
    if (curSession) reload();       // the rail search matches session user tags
  });
}

// ---- dock / panel visibility -------------------------------------------
function savePanels() { LS.set("nebula.panels", { sess: showSess, sc: showSc }); }

function updateDock() {
  $("sessPanel").classList.toggle("hidden", !showSess);
  $("scPanel").classList.toggle("hidden", !showSc);
  const both = showSess && showSc;
  $("splitDockRows").classList.toggle("hidden", !both);
  // Only meaningful to hold the session panel at a fixed height when it has
  // a neighbour; alone it should fill the dock.
  if (both) setSessHeight(LS.get("nebula.sessH", 320));
  else $("sessPanel").style.flex = "1 1 0";
  const any = showSess || showSc;
  $("dock").classList.toggle("hidden", !any);
  $("splitDock").classList.toggle("hidden", !any);
  $("sessInfoBtn").classList.toggle("on", showSess);
}

async function toggleSessionPanel() {
  showSess = !showSess;
  savePanels();
  updateDock();
  if (showSess) await refreshSessionInfo();
}

// ---- resizable panes ----------------------------------------------------
// Each splitter drags one neighbouring pane's flex-basis and remembers it.
function makeSplitter(el, opts) {
  el.addEventListener("mousedown", (ev) => {
    ev.preventDefault();
    const start = opts.axis === "x" ? ev.clientX : ev.clientY;
    const startSize = opts.get();
    const busy = opts.axis === "x" ? "resizing" : "resizing-y";
    document.body.classList.add(busy);

    const move = (e) => {
      const now = opts.axis === "x" ? e.clientX : e.clientY;
      const delta = (now - start) * (opts.invert ? -1 : 1);
      const size = Math.max(opts.min, Math.min(opts.max(), startSize + delta));
      opts.set(size);
    };
    const up = () => {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
      document.body.classList.remove(busy);
      LS.set(opts.key, opts.get());
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  });
}

function setRailWidth(px) { $("rail").style.flex = `0 0 ${px}px`; $("rail").style.width = `${px}px`; }
function setDockWidth(px) { $("dock").style.flex = `0 0 ${px}px`; $("dock").style.width = `${px}px`; }
function setSessHeight(px) { $("sessPanel").style.flex = `0 0 ${px}px`; }

function initPanes() {
  setRailWidth(LS.get("nebula.railW", 280));
  setDockWidth(LS.get("nebula.dockW", 340));
  setSessHeight(LS.get("nebula.sessH", 320));

  makeSplitter($("splitRail"), {
    axis: "x", key: "nebula.railW", min: 180,
    max: () => Math.max(220, window.innerWidth - 420),
    get: () => $("rail").getBoundingClientRect().width,
    set: setRailWidth,
  });
  makeSplitter($("splitDock"), {
    axis: "x", key: "nebula.dockW", min: 240, invert: true,
    max: () => Math.max(280, window.innerWidth - 420),
    get: () => $("dock").getBoundingClientRect().width,
    set: setDockWidth,
  });
  makeSplitter($("splitDockRows"), {
    axis: "y", key: "nebula.sessH", min: 120,
    max: () => Math.max(160, $("dock").getBoundingClientRect().height - 140),
    get: () => $("sessPanel").getBoundingClientRect().height,
    set: setSessHeight,
  });
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
  $("dlgArchive").textContent = activeLabel();
  $("dlgFiles").innerHTML = pendingPaths
    .map((p) => `<span class="f">${escapeHtml(baseName(p))}</span>`).join("");

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
  $("dlgTags").value = ""; $("dlgDesc").value = ""; $("dlgOrigin").value = "";
  $("dlgDerived").value = ""; $("dlgRefs").innerHTML = "";
  syncMode();
  $("scrim").classList.add("show");
}
function syncMode() {
  const existing = $("modeExisting").checked;
  $("dlgSession").disabled = !existing;
  $("newFields").style.display = existing ? "none" : "flex";
  // `nebula import-new` has no --derived-from, and manual.import_new does
  // not accept one, so the GUI doesn't pretend to offer it for a brand-new
  // session -- that would silently drop what you typed.
  $("dlgDerived").disabled = !existing;
  $("dlgRefs").innerHTML = existing ? $("dlgRefs").innerHTML
    : `<span class="ref bad">not available when creating a new session — import first, then add refs</span>`;
  if (existing) refreshDerivedCandidates();
}

function derivedRefs() {
  return $("dlgDerived").value.split(",").map((r) => r.trim()).filter(Boolean);
}

// Offer the target session's own files for completion: same-session refs
// are the common case, and a bare filename is a valid ref.
async function refreshDerivedCandidates() {
  const runId = $("dlgSession").value;
  const s = sessions.find((x) => x.run_id === runId);
  if (!s) { $("dlgDerivedList").innerHTML = ""; return; }
  try {
    const its = await call("list_items", { session_path: s.path, verify: false });
    $("dlgDerivedList").innerHTML = its
      .filter((i) => i.has_artifact)
      .map((i) => `<option value="${escapeHtml(i.name)}"></option>`).join("");
  } catch (e) {
    $("dlgDerivedList").innerHTML = "";
  }
  validateDerived();
}

// Refs are allowed to dangle (the CLI permits it), so a missing target is
// a warning, not a block -- but a ref that doesn't parse is an error.
let derivedTimer = null;
function scheduleValidateDerived() {
  clearTimeout(derivedTimer);
  derivedTimer = setTimeout(validateDerived, 200);
}

async function validateDerived() {
  const refs = derivedRefs();
  const box = $("dlgRefs");
  if (!$("modeExisting").checked) return;
  if (!refs.length) { box.innerHTML = ""; return; }
  try {
    const res = await call("resolve_refs", { archive, run_id: $("dlgSession").value, refs });
    box.innerHTML = res.map((r) => {
      if (!r.valid) return `<span class="ref bad">${escapeHtml(r.text)} — ${escapeHtml(r.error || "invalid ref")}</span>`;
      if (r.exists) return `<span class="ref ok">${escapeHtml(r.ref)} ✓</span>`;
      return `<span class="ref warn">${escapeHtml(r.ref)} — ${escapeHtml(r.note || "not found")}</span>`;
    }).join("");
  } catch (e) {
    box.innerHTML = "";
  }
}
async function doImport() {
  const origin = $("dlgOrigin").value.trim() || null;
  try {
    let targetRunId;
    if ($("modeExisting").checked) {
      targetRunId = $("dlgSession").value;
      await call("import_file", { archive, run_id: targetRunId, paths: pendingPaths,
                                  origin, derived_from: derivedRefs(), allow_frozen: true });
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

// ---- drag and drop ------------------------------------------------------
// Files dropped on the window take the same path as the Import button --
// the picker step is simply already done. Tauri gives us absolute paths,
// so the drop is exactly equivalent to picking those files in the dialog.
//
// The webview's onDragDropEvent wrapper is the documented API; we listen to
// the underlying `tauri://drag-*` events directly instead, since those are
// available through the plain event API under withGlobalTauri.
function initDragDrop() {
  const over = () => {
    $("dzSub").textContent = archive ? `into ${activeLabel()}` : "open an archive first";
    $("dropZone").classList.toggle("blocked", !archive);
    $("dropZone").classList.add("show");
  };
  const hide = () => $("dropZone").classList.remove("show");

  const on = (name, fn) =>
    tauriEvent.listen(name, fn).catch((e) => console.error(`listen ${name} failed`, e));

  on("tauri://drag-enter", over);
  on("tauri://drag-over", over);
  on("tauri://drag-leave", hide);
  on("tauri://drag-drop", (event) => {
    hide();
    const p = event.payload || {};
    onDropPaths(p.paths || []);
  });
}

async function onDropPaths(paths) {
  if (!paths.length) return;
  if (!archive) { toast("Open an archive before dropping files."); return; }
  pendingPaths = paths;
  await showImportDialog();
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
  if (curSession || searchMode) renderItemArea();
}

// ---- search option popovers --------------------------------------------
function saveSessCfg() { LS.set("nebula.sessCfg", sessCfg); }
function saveItemCfg() { LS.set("nebula.itemCfg", itemCfg); }

const SESS_BOXES = { sfTitle: "titles", sfId: "ids", sfTag: "tags",
                     sfUserTag: "userTags", ssOpen: "open",
                     ssClosed: "closed", ssCrashed: "crashed", sqClean: "clean", sqDirty: "dirty" };
const ITEM_BOXES = { ifName: "name", ifTag: "tags", ifOrigin: "origin",
                     ifSession: "session", ifUserTags: "userTags",
                     ifComments: "comments" };

function syncCfgUI() {
  for (const [id, key] of Object.entries(SESS_BOXES)) $(id).checked = !!sessCfg[key];
  for (const [id, key] of Object.entries(ITEM_BOXES)) $(id).checked = !!itemCfg[key];
  $("ifFrom").value = fmtDay(itemCfg.from);
  $("ifTo").value = fmtDay(itemCfg.to);
  syncDateFields();
}

function closePops(except) {
  for (const id of ["sessCfg", "itemCfg"]) {
    if (id !== except) $(id).classList.add("hidden");
  }
}
function togglePop(id) {
  const el = $(id);
  const willShow = el.classList.contains("hidden");
  closePops(willShow ? id : null);
  el.classList.toggle("hidden", !willShow);
}

function wireSearchOptions() {
  for (const [id, key] of Object.entries(SESS_BOXES)) {
    $(id).onchange = (e) => { sessCfg[key] = e.target.checked; saveSessCfg(); applySessionFilter(); };
  }
  for (const [id, key] of Object.entries(ITEM_BOXES)) {
    $(id).onchange = (e) => { itemCfg[key] = e.target.checked; saveItemCfg(); runItemSearch(); };
  }
  // A half-typed date shouldn't fire a search on every keystroke, so the
  // field marks itself invalid and simply isn't sent until it parses.
  const wireDate = (id, key) => {
    const apply = (e) => {
      const iso = parseDay(e.target.value);
      $(id).classList.toggle("bad", iso === null);
      if (iso === null) return;
      itemCfg[key] = iso;
      saveItemCfg();
      scheduleItemSearch();
    };
    $(id).oninput = apply;
    $(id).onblur = (e) => {
      const iso = parseDay(e.target.value);
      if (iso !== null) e.target.value = fmtDay(iso);   // normalise 2026/7/4
    };
  };
  wireDate("ifFrom", "from");
  wireDate("ifTo", "to");

  $("ifDates").onchange = (e) => {
    itemCfg.dates = e.target.checked;
    saveItemCfg(); syncDateFields(); runItemSearch();
  };
  $("ifReset").onclick = () => { resetDates(); runItemSearch(); };

  $("sessCfgBtn").onclick = (e) => { e.stopPropagation(); togglePop("sessCfg"); };
  $("itemCfgBtn").onclick = (e) => { e.stopPropagation(); togglePop("itemCfg"); };
  $("sessCfg").onclick = (e) => e.stopPropagation();
  $("itemCfg").onclick = (e) => e.stopPropagation();
  document.addEventListener("click", () => closePops(null));

  $("sessSearch").oninput = (e) => { sessQuery = e.target.value; applySessionFilter(); };
  $("sessSearchClear").onclick = () => { $("sessSearch").value = ""; sessQuery = ""; applySessionFilter(); };
  $("itemSearch").oninput = scheduleItemSearch;
  $("itemSearchClear").onclick = clearItemSearch;
}

// ---- keyboard shortcuts -------------------------------------------------
// One set of bindings, spelled with each platform's own command modifier:
// Cmd on macOS, Ctrl on Windows/Linux. They work while a search box has
// focus too -- none of them type a character.
//
// Note the Shift on the metadata binding: plain Cmd-M is Minimize in the
// macOS Window menu, and AppKit consumes menu accelerators before the
// webview sees a key event, so that spelling would never reach us.
const IS_MAC = /mac/i.test(
  (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || "");

// The platform's command modifier -- and the *other* one must not be held,
// so Ctrl-O on a Mac doesn't fire the Cmd-O action.
function hasMod(e) { return IS_MAC ? (e.metaKey && !e.ctrlKey) : (e.ctrlKey && !e.metaKey); }

// Tooltips are authored with Ctrl- spellings; rewrite them for macOS.
function localizeShortcutHints() {
  if (!IS_MAC) return;
  document.querySelectorAll("[title]").forEach((el) => {
    el.title = el.title.replace(/Ctrl-Shift-/g, "⇧⌘").replace(/Ctrl-/g, "⌘");
  });
}

function toggleMetadataPanel() {
  if (showSc) { showSc = false; savePanels(); updateDock(); return; }
  if (!selected) { toast("Select a file first."); return; }
  if (!selected.has_sidecar) { toast(`${selected.name} has no metadata sidecar.`); return; }
  openSidecarPanel(selected);
}

function openSelectedExternally() {
  const it = selected;
  if (!it) { toast("Select a file first."); return; }
  // Whichever row is selected is what opens: the sidecar row opens the
  // .meta.json, the artefact row the artefact itself.
  const path = selectedIsSidecar ? it.sidecar_path : (it.artifact_path || it.sidecar_path);
  if (!path) { toast(`${it.name} has no file on disk to open.`); return; }
  call("open_path", { path });
}

async function refreshAll() {
  if (!archive) { toast("No archive open."); return; }
  await reload();
  if (searchMode) await runItemSearch();   // keep results in step with disk
  await refreshSessionInfo();
  toast(`Reloaded ${activeLabel()}`);
}

function initShortcuts() {
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closePops(null); return; }
    if (!hasMod(e) || e.altKey) return;
    const k = (e.key || "").toLowerCase();
    const shift = e.shiftKey;

    let action = null;
    if (k === "m" && shift) action = toggleMetadataPanel;
    else if (k === "s" && shift) action = toggleSessionPanel;
    else if (k === "r" && !shift) action = refreshAll;
    else if (k === "i" && shift) action = startImport;
    else if (k === "o" && !shift) action = openSelectedExternally;
    if (!action) return;

    e.preventDefault();   // Ctrl-R would otherwise reload the webview
    Promise.resolve(action()).catch((err) => toast(`${err}`));
  });
}

// ---- wiring -------------------------------------------------------------
$("openArchive").onclick = openArchive;
$("closeArchive").onclick = closeArchive;
$("archiveSel").onchange = (e) => onArchivePicked(e.target.value);
$("refresh").onclick = () => reload();
$("viewList").onclick = () => setView(true);
$("viewGrid").onclick = () => setView(false);
$("meta").onchange = (e) => { showMeta = e.target.checked; if (curSession) { $("itemArea").innerHTML = listHTML(); wireItems(); } };
$("verify").onchange = (e) => { verify = e.target.checked; reloadItems(); };
$("openArt").onclick = () => selected && selected.artifact_path && call("open_path", { path: selected.artifact_path });
$("editSc").onclick = () => selected && selected.sidecar_path && call("open_path", { path: selected.sidecar_path });
$("openSc").onclick = () => selected && openSidecarPanel(selected);
$("scClose").onclick = () => { showSc = false; savePanels(); updateDock(); };
$("scRaw").onclick = () => { scRaw = !scRaw; renderSidecarPanel(); };
$("sessInfoBtn").onclick = toggleSessionPanel;
$("sessClose").onclick = () => { showSess = false; savePanels(); updateDock(); };
$("sessRaw").onclick = () => { sessRaw = !sessRaw; renderSessionPanel(); };
$("importBtn").onclick = startImport;
$("modeExisting").onchange = syncMode;
$("modeNew").onchange = syncMode;
$("dlgSession").onchange = refreshDerivedCandidates;
$("dlgDerived").oninput = scheduleValidateDerived;
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
async function boot() {
  initPanes();
  initDragDrop();

  sessCfg = Object.assign(sessCfg, LS.get("nebula.sessCfg", {}));
  itemCfg = Object.assign(itemCfg, LS.get("nebula.itemCfg", {}));
  syncCfgUI();
  wireSearchOptions();
  initShortcuts();
  localizeShortcutHints();
  $("sessCfgBtn").classList.toggle("on", isSessCfgNarrowed());
  $("itemCfgBtn").classList.toggle("on", datesOn());

  const panels = LS.get("nebula.panels", { sess: false, sc: false });
  showSess = !!panels.sess;
  showSc = false;              // nothing selected yet, so no sidecar to show
  updateDock();
  renderSessionPanel();

  archives = LS.get("nebula.archives", []);
  const last = localStorage.getItem("nebula.archive") || null;
  if (!archives.length && last) archives = [{ id: last, label: last }];

  try {
    registry = await call("list_archives", {});
  } catch (e) {
    registry = [];             // no registry file, or the bridge is down
  }
  renderArchiveSelect();

  const start = archives.some((a) => a.id === last) ? last : (archives[0] || {}).id;
  if (start) {
    try { await loadArchive(start); }
    catch (e) { toast(`Startup error: ${e}`); }
  }
}

boot();
