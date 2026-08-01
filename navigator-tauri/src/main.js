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
// Multi-select: `selected` stays the primary (what the panels describe),
// `picked` is everything highlighted. Cmd/Ctrl toggles, Shift extends.
let picked = [], pickAnchor = null;

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

// rail: sessions | collections | views
let railTab = "sessions";
let collections = [], savedViews = [];
let collTree = { roots: [], byName: {} };
let collExpanded = {};          // name -> open?
let collPath = [];              // breadcrumb of the collection being viewed
let entryOpen = {};             // "parent/child" -> expanded? (in the item area)
let openCollection = null;     // name of the collection shown in the item area

// How the file list is ordered and which statuses are shown. The backend
// hands items over newest-first; this re-sorts client-side so changing it
// is instant and works on search results too.
let shownItems = [];
let itemSort = { by: "date", desc: true,
                 show: { paired: true, drifted: true, orphan: true, stray: true } };

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
    if (railTab === "collections") await loadCollections();
    if (railTab === "views") await loadViews();
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
  sessions = []; curSession = null; items = []; shownItems = []; selected = null;
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
  selected = null; selectedIsSidecar = false; picked = []; pickAnchor = null;
  renderSessions();
  await reloadItems();
  updateDetails();
  await refreshSessionInfo();
}

// ---- collections and saved searches -------------------------------------
// A collection points at things instead of holding them, so opening one
// never leaves the archive's real layout -- it is a view, like search.
function setRailTab(tab) {
  railTab = tab;
  for (const [id, name] of [["tabSessions", "sessions"], ["tabCollections", "collections"],
                            ["tabViews", "views"]]) {
    $(id).classList.toggle("on", name === tab);
  }
  $("sessHead").classList.toggle("hidden", tab !== "sessions");
  $("sessionList").classList.toggle("hidden", tab !== "sessions");
  document.querySelector(".rail .search-row").classList.toggle("hidden", tab !== "sessions");
  $("collPane").classList.toggle("hidden", tab !== "collections");
  $("viewPane").classList.toggle("hidden", tab !== "views");
  LS.set("nebula.railTab", tab);
  // Leaving the collections tab means leaving its contents: the item area
  // is shared, so hand it back to the open session rather than stranding a
  // collection listing under the Sessions tab.
  if (tab !== "collections" && openCollection) {
    openCollection = null;
    collPath = [];
    if (curSession && !searchMode) reloadItems();
    else renderItemArea();
  }
  if (tab === "collections") loadCollections();
  if (tab === "views") loadViews();
}

async function loadCollections() {
  if (!archive) return;
  try {
    const ov = await call("collections_overview", { archive });
    collections = ov.collections;
    collTree = { roots: ov.roots, byName: {} };
    for (const c of ov.collections) collTree.byName[c.name] = c;
  } catch (e) {
    collections = [];
    collTree = { roots: [], byName: {} };
  }
  collExpanded = Object.assign(collExpanded, LS.get("nebula.collOpen", {}));
  renderCollections();
}

// A nested collection belongs under its parent, not beside it: the rail is
// a tree, and only collections nothing else contains are roots.
function collectionRowsHTML(name, depth, ancestors) {
  const c = collTree.byName[name];
  if (!c) return "";
  if (ancestors.includes(name)) {          // hand-edited cycle
    return `<div class="citem" style="padding-left:${10 + depth * 14}px">
        <span class="cname">${escapeHtml(name)}<span class="ctitle">(cycle)</span></span>
      </div>`;
  }
  const kids = c.children || [];
  const open = !!collExpanded[name];
  const twisty = kids.length
    ? `<span class="tw" data-toggle="${escapeHtml(name)}" title="${open ? "Collapse" : "Expand"}">`
      + `<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">`
      + `<path d="${open ? "M3.5 6 L8 10.5 L12.5 6" : "M6 3.5 L10.5 8 L6 12.5"}"`
      + ` fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"`
      + ` stroke-linejoin="round"/></svg></span>`
    : `<span class="tw empty"></span>`;
  let html = `
    <div class="citem ${name === openCollection ? "sel" : ""}" data-name="${escapeHtml(name)}"
         data-drop-collection="${escapeHtml(name)}"
         style="padding-left:${6 + depth * 14}px">
      ${twisty}
      <span class="cname">${escapeHtml(c.title || c.name)}
        ${c.title ? `<span class="ctitle">${escapeHtml(c.name)}</span>` : ""}</span>
      <span class="ccount">${c.n_entries}</span>
    </div>`;
  if (open) {
    for (const kid of kids) html += collectionRowsHTML(kid, depth + 1, ancestors.concat([name]));
  }
  return html;
}

// The readable name: a free-form title when set, else the storable one.
function collLabel(name) {
  const c = collTree.byName[name] || collections.find((x) => x.name === name);
  return (c && c.title) || name;
}

function parentOf(name) {
  const found = collections.find((c) => (c.children || []).includes(name));
  return found ? found.name : null;
}

function renderCollections() {
  if (!collections.length) {
    $("collList").innerHTML = `<div class="none">No collections yet.<br>` +
      `Use <b>+ new</b>, or add a file from its detail bar.</div>`;
    return;
  }
  $("collList").innerHTML = collTree.roots
    .map((name) => collectionRowsHTML(name, 0, [])).join("");

  $("collList").querySelectorAll(".citem[data-name]").forEach((el) => {
    const name = el.getAttribute("data-name");
    el.onclick = (ev) => {
      // closest(), not ev.target: the twisty holds an <svg>, so a click
      // lands on the path and never on the span carrying the attribute.
      const tw = ev.target.closest && ev.target.closest("[data-toggle]");
      if (tw) {
        const toggle = tw.getAttribute("data-toggle");
        collExpanded[toggle] = !collExpanded[toggle];
        LS.set("nebula.collOpen", collExpanded);
        renderCollections();
        return;
      }
      showCollection(name);
    };
    el.oncontextmenu = (ev) => { ev.preventDefault(); showCollectionMenu(ev.clientX, ev.clientY, name); };
    el.onpointerdown = (ev) => {
      if (ev.target.closest(".tw")) return;            // the twisty is a button
      // Only a nested collection can be moved: a root has no parent to
      // move it out of.
      const parent = parentOf(name);
      if (!parent) return;
      startEntryDrag(ev, { ref: `collections/${name}`, label: name, from: parent });
    };
  });
}

async function showCollection(name, { push = true } = {}) {
  if (push) {
    const at = collPath.indexOf(name);
    collPath = at >= 0 ? collPath.slice(0, at + 1) : collPath.concat([name]);
  }
  openCollection = name;
  searchMode = false;
  // Opening a child from the rail should reveal it there too.
  for (const parent of collPath.slice(0, -1)) collExpanded[parent] = true;
  LS.set("nebula.collOpen", collExpanded);
  try {
    const tree = await call("collection_tree", { archive, name });
    $("itemArea").innerHTML = collectionHTML(tree);
    wireCollection();
    const n = (tree.entries || []).length;
    $("statusbar").textContent = `${activeLabel()} — collection ${name}: ${n} entrie(s)`;
  } catch (e) {
    toast(`Could not open ${name}: ${e}`);
  }
  renderCollections();
}

function collectionHTML(node, nested) {
  if (node.missing) return `<div class="empty">No collection called ${escapeHtml(node.name)}.</div>`;
  const crumbs = collPath.length > 1
    ? `<div class="crumbs">` + collPath.map((n, i) =>
        (i ? `<span class="sep">›</span>` : "") +
        `<span class="crumb ${i === collPath.length - 1 ? "here" : ""}" data-crumb="${escapeHtml(n)}"`
        + ` data-drop-collection="${escapeHtml(n)}">`
        + `${escapeHtml(collLabel(n))}</span>`).join("") + `</div>`
    : "";
  const anyFolders = (node.entries || []).some((e) => e.kind === "collection" && e.child);
  const head = nested ? "" : `${crumbs}<div class="ctree-head">
      <span class="t">${escapeHtml(node.title || node.name)}</span>
      ${node.description ? `<span class="d">${escapeHtml(node.description)}</span>` : ""}
      ${anyFolders ? `<button class="dbtn ghost tiny" id="collExpandAll">Expand all</button>
        <button class="dbtn ghost tiny" id="collCollapseAll">Collapse all</button>` : ""}
    </div>`;

  if (node.cycle) return `${head}${noteBox("err", "This collection contains itself — stopping here.")}`;
  if (!(node.entries || []).length && !nested) {
    return `${head}<div class="empty">Nothing in this collection yet.</div>`;
  }

  const rows = (node.entries || []).map((e) => {
    const cls = e.resolved === false ? "unresolved" : (e.exists ? "" : "missing");
    const go = e.exists && (e.kind === "file" || e.kind === "session" || e.kind === "collection");
    const attrs = go
      ? `data-goto="${escapeHtml(e.kind)}" data-ref="${escapeHtml(e.ref)}" ` +
        `data-target="${escapeHtml(e.target || "")}" data-path="${escapeHtml(e.path || "")}" ` +
        `data-openkey="${escapeHtml(`${node.name}/${e.target || e.ref}`)}"`
      : "";
    // A nested collection reads as a folder name, not as the raw
    // "collections/<name>" ref -- the ref spelling is an implementation
    // detail here, and seeing it beside a parent named something else is
    // just confusing.
    const shown = e.kind === "collection" ? collLabel(e.target || e.ref) : e.ref;
    const kindLabel = e.kind === "collection" ? "folder" : e.kind;
    const key = `${node.name}/${e.target || e.ref}`;
    const openable = e.kind === "collection" && e.child;
    const isOpen = openable && entryOpen[key] !== false;   // expanded by default
    const tw = openable
      ? `<span class="etw" data-open="${escapeHtml(key)}" title="${isOpen ? "Collapse" : "Expand"}">`
        + `<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">`
        + `<path d="${isOpen ? "M3.5 6 L8 10.5 L12.5 6" : "M6 3.5 L10.5 8 L6 12.5"}"`
        + ` fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"`
        + ` stroke-linejoin="round"/></svg></span>`
      : `<span class="etw empty"></span>`;
    const dropAttr = e.kind === "collection" && e.exists
      ? ` data-drop-collection="${escapeHtml(e.target || "")}"` : "";
    return `<div class="crow ${cls} ${go ? "go" : ""}" ${attrs}${dropAttr} data-dragref="${escapeHtml(e.ref)}">
        ${tw}
        <span class="ckind">${escapeHtml(kindLabel)}</span>
        <span class="cref">${escapeHtml(shown)}</span>
        ${e.note ? `<span class="cnote">${escapeHtml(e.note)}</span>` : ""}
        ${e.note_error ? `<span class="cbad">${escapeHtml(e.note_error)}</span>` : ""}
        <span class="cx" data-remove="${escapeHtml(e.ref)}" title="Remove from this collection">✕</span>
      </div>` + (e.child && isOpen
        ? `<div class="cnest">${collectionHTML(e.child, true)}</div>` : "");
  }).join("");
  return `${head}<div class="ctree">${rows}</div>`;
}

function wireCollection() {
  const area = $("itemArea");
  if ($("collExpandAll")) {
    $("collExpandAll").onclick = () => setAllFolders(true);
    $("collCollapseAll").onclick = () => setAllFolders(false);
  }
  area.querySelectorAll("[data-open]").forEach((el) => {
    el.onclick = (ev) => {
      ev.stopPropagation();
      const key = el.getAttribute("data-open");
      entryOpen[key] = entryOpen[key] === false;
      showCollection(openCollection, { push: false });
    };
  });
  area.querySelectorAll(".crumb").forEach((el) => {
    el.onclick = () => showCollection(el.getAttribute("data-crumb"));
  });
  area.querySelectorAll("[data-goto]").forEach((el) => {
    const kind = el.getAttribute("data-goto");
    const target = el.getAttribute("data-target") || "";
    el.onclick = (ev) => {
      if (ev.target.getAttribute("data-remove") !== null) return;
      if (kind === "collection") {
        // Expand in place: descending into a folder loses the parent
        // context, which is rarely what a click means.
        const key = el.getAttribute("data-openkey");
        entryOpen[key] = entryOpen[key] === false;
        showCollection(openCollection, { push: false });
        return;
      }
      // Stay in the collection: selecting shows the file's properties in
      // the detail bar and panels, without swapping the whole view.
      const [runId, filename] = target.split("/");
      if (kind === "file") selectFromCollection(runId, filename, el);
      else if (kind === "session") gotoRunId(runId);
    };
    el.ondblclick = () => {
      if (kind === "file") {
        const path = el.getAttribute("data-path");
        if (path) call("open_path", { path });
      } else if (kind === "session") {
        gotoRunId(target);
      }
      // Deliberately no double-click descent for a folder: it drops the
      // parent context. Open a folder on its own from its right-click menu
      // or the rail tree.
    };
  });
  area.oncontextmenu = (ev) => {
    if (ev.target.closest(".crow")) return;      // the row's own menu wins
    ev.preventDefault();
    showMenu(ev.clientX, ev.clientY, [
      { head: openCollection },
      { label: "New folder here…", action: () => newNestedCollection(openCollection) },
      { label: "Add selected file(s)…", disabled: !selectedRefs().length,
        action: () => openCollectionPicker(selectedRefs(),
          picked.length > 1 ? `${picked.length} files` : (selected && selected.name)) },
    ]);
  };
  area.querySelectorAll("[data-dragref]").forEach((el) => {
    el.onpointerdown = (ev) => {
      if (ev.target.closest(".cx, .etw")) return;      // buttons keep working
      const ref = el.getAttribute("data-dragref");
      const label = el.querySelector(".cref").textContent;
      startEntryDrag(ev, { ref, label, from: openCollection });
    };
  });
  area.querySelectorAll(".crow").forEach((el) => {
    el.oncontextmenu = (ev) => {
      ev.preventDefault();
      showEntryMenu(ev.clientX, ev.clientY, {
        ref: el.getAttribute("data-dragref") || el.querySelector(".cref").textContent,
        kind: el.querySelector(".ckind").textContent,
        target: el.getAttribute("data-target") || "",
        path: el.getAttribute("data-path") || "",
        exists: !el.classList.contains("missing") && !el.classList.contains("unresolved"),
      });
    };
  });
  area.querySelectorAll("[data-remove]").forEach((el) => {
    el.onclick = async (ev) => {
      ev.stopPropagation();
      const ref = el.getAttribute("data-remove");
      try {
        await call("collection_remove", { archive, name: openCollection, refs: [ref] });
        toast(`Removed ${ref} from ${openCollection}`);
        await loadCollections();
        await showCollection(openCollection);
      } catch (e) {
        toast(`Could not remove: ${e}`);
      }
    };
  });
}

// Expand or collapse every folder in the open collection at once.
function setAllFolders(open) {
  document.querySelectorAll("#itemArea [data-open]").forEach((el) => {
    entryOpen[el.getAttribute("data-open")] = open ? undefined : false;
  });
  // Nested keys not currently rendered (because their parent is collapsed)
  // are handled when they next render: default is expanded.
  if (open) {
    for (const key of Object.keys(entryOpen)) {
      if (entryOpen[key] === false) delete entryOpen[key];
    }
  }
  showCollection(openCollection, { push: false });
}

// Select a file that lives elsewhere, without leaving this view.
async function selectFromCollection(runId, filename, row) {
  const s = sessions.find((x) => x.run_id === runId);
  if (!s) { toast(`${runId} is not in this archive view.`); return; }
  try {
    const item = await call("get_item", { session_path: s.path, filename, run_id: runId });
    selected = item;
    selectedIsSidecar = false;
    picked = [item];
    updateDetails();
    if (showSc && item.has_sidecar) openSidecarPanel(item);
    refreshSessionInfo();          // the panel follows the file's own session
    $("itemArea").querySelectorAll(".crow.on").forEach((n) => n.classList.remove("on"));
    if (row) row.classList.add("on");
  } catch (e) {
    toast(`Could not read ${filename}: ${e}`);
  }
}

// Follow a collection entry back into the archive proper.
async function gotoRunId(runId, filename) {
  const s = sessions.find((x) => x.run_id === runId);
  if (!s) { toast(`${runId} is not in this archive view.`); return; }
  setRailTab("sessions");
  openCollection = null;
  await selectSession(s);
  if (!filename) return;
  const target = items.find((i) => i.name === filename);
  if (target) selectItem(target, false);
  else toast(`${filename} is not in ${runId}.`);
}

// ---- saved searches -----------------------------------------------------
async function loadViews() {
  if (!archive) return;
  try {
    savedViews = await call("list_views", { archive });
  } catch (e) {
    savedViews = [];
  }
  renderViews();
}

function renderViews() {
  if (!savedViews.length) {
    $("viewList").innerHTML = `<div class="none">No saved searches.<br>` +
      `Search for something, then use <b>+ save</b>.</div>`;
    return;
  }
  $("viewList").innerHTML = savedViews.map((v, i) => `
    <div class="citem" data-i="${i}">
      <span class="cname">${escapeHtml(v.name)}
        <span class="ctitle">${escapeHtml(v.title || v.query || "(empty query)")}</span></span>
      <span class="cx" data-del="${escapeHtml(v.name)}" title="Delete this view">✕</span>
    </div>`).join("");
  $("viewList").querySelectorAll(".citem").forEach((el) => {
    el.onclick = (ev) => {
      const del = ev.target.getAttribute("data-del");
      if (del !== null) { deleteView(del); return; }
      runView(savedViews[+el.dataset.i].name);
    };
  });
}

async function runView(name) {
  try {
    const res = await call("run_view", { archive, name });
    openCollection = null;
    searchMode = true;
    searchMeta = res;
    items = res.items;
    selected = null; selectedIsSidecar = false;
    // Show what is being run, so the results aren't unexplained.
    $("itemSearch").value = (res.view && res.view.query) || "";
    $("itemSearchClear").classList.remove("hidden");
    applyItemView();
    updateDetails();
    $("statusbar").textContent =
      `${activeLabel()} — view ${name}: ${res.items.length} match(es)`;
  } catch (e) {
    toast(`Could not run ${name}: ${e}`);
  }
}

async function deleteView(name) {
  const ok = await confirmAction({
    body: `Delete the saved search <b>${escapeHtml(name)}</b>?<br><br>`
      + `It is only a stored query — nothing it matched is affected.`,
    confirmLabel: "Delete",
  });
  if (!ok) return;
  try {
    await call("delete_view", { archive, name });
    await loadViews();
    toast(`Deleted view ${name}`);
  } catch (e) {
    toast(`Could not delete: ${e}`);
  }
}

async function saveCurrentSearch() {
  const query = $("itemSearch").value.trim();
  if (!query && !datesOn()) {
    toast("Search for something first, then save it.");
    return;
  }
  const name = await promptName("Save this search as", suggestName(query));
  if (!name) return;
  const fields = ["name", "tags", "origin", "session", "userTags", "comments"]
    .filter((f) => itemCfg[f])
    .map((f) => (f === "name" ? "filename" : f === "userTags" ? "user_tags" : f));
  try {
    await call("save_view", {
      archive, name, query, fields,
      date_from: (itemCfg.dates && itemCfg.from) || null,
      date_to: (itemCfg.dates && itemCfg.to) || null,
    });
    toast(`Saved view ${name}`);
    setRailTab("views");
  } catch (e) {
    toast(`Could not save: ${e}`);
  }
}

function suggestName(text) {
  return (text || "view").toLowerCase().replace(/[^a-z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "").slice(0, 40) || "view";
}

// A tiny prompt built on the confirm dialog, so names are typed in-app
// rather than through a browser prompt() the webview may not offer.
function promptName(label, initial) {
  const body = `${escapeHtml(label)}:<br><br>`
    + `<input id="cfmInput" class="notes-tags" value="${escapeHtml(initial || "")}" spellcheck="false" />`;
  const done = confirmAction({ body, confirmLabel: "Save", danger: false });
  const input = $("cfmInput");
  if (input) { input.focus(); input.select(); }
  return done.then((ok) => (ok && input ? input.value.trim() : null));
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
    selected = null; selectedIsSidecar = false; picked = [];
    applyItemView();
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
  else { items = []; applyItemView(); updateDetails(); }
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
  applyItemView();
  const problems = items.filter((i) => i.status !== "paired").length;
  $("statusbar").textContent = `${activeLabel()} — ${curSession.run_id}: ${items.length} item(s), ${problems} problem(s)`;
}

// ---- ordering and filtering --------------------------------------------
// Worst first when ascending: the files that need attention lead.
const STATUS_RANK = { orphan: 0, stray: 1, drifted: 2, paired: 3 };

function itemTime(it) {
  const t = Date.parse(it.timestamp || "");
  return Number.isNaN(t) ? null : t;
}

function applyItemView() {
  const show = itemSort.show;
  shownItems = items.filter((it) => show[it.status] !== false);

  const dir = itemSort.desc ? -1 : 1;
  shownItems.sort((a, b) => {
    let d = 0;
    if (itemSort.by === "date") {
      const ta = itemTime(a), tb = itemTime(b);
      // Undated items sort last whichever way the list runs.
      if (ta === null || tb === null) return ta === tb ? 0 : (ta === null ? 1 : -1);
      d = ta - tb;
    } else if (itemSort.by === "title") {
      d = (a.display_name || a.name).localeCompare(b.display_name || b.name);
    } else {
      d = (STATUS_RANK[a.status] ?? 9) - (STATUS_RANK[b.status] ?? 9);
    }
    if (d) return d * dir;
    // Stable tail: duplicates always read 1, 2, 3 within their group.
    return (a.display_name || a.name).localeCompare(b.display_name || b.name)
      || (a.position || 1) - (b.position || 1);
  });

  $("sortBtn").classList.toggle("on", isSortNarrowed());
  renderItemArea();
}

function isSortNarrowed() {
  const s = itemSort.show;
  return itemSort.by !== "date" || !itemSort.desc
    || !(s.paired && s.drifted && s.orphan && s.stray);
}

function syncSortUI() {
  $("sortDate").checked = itemSort.by === "date";
  $("sortTitle").checked = itemSort.by === "title";
  $("sortStatus").checked = itemSort.by === "status";
  $("sortDesc").checked = itemSort.desc;
  $("sortAsc").checked = !itemSort.desc;
  // The direction labels only make sense against the chosen field.
  const labels = { date: ["Newest first", "Oldest first"],
                   title: ["Z to A", "A to Z"],
                   status: ["Best first", "Problems first"] };
  const [descLbl, ascLbl] = labels[itemSort.by] || labels.date;
  $("sortDescLbl").textContent = descLbl;
  $("sortAscLbl").textContent = ascLbl;
  $("fltPaired").checked = itemSort.show.paired !== false;
  $("fltDrifted").checked = itemSort.show.drifted !== false;
  $("fltOrphan").checked = itemSort.show.orphan !== false;
  $("fltStray").checked = itemSort.show.stray !== false;
}

function wireSort() {
  const set = (patch) => {
    Object.assign(itemSort, patch);
    LS.set("nebula.itemSort", itemSort);
    syncSortUI();
    applyItemView();
  };
  $("sortDate").onchange = () => set({ by: "date" });
  $("sortTitle").onchange = () => set({ by: "title" });
  $("sortStatus").onchange = () => set({ by: "status" });
  $("sortDesc").onchange = () => set({ desc: true });
  $("sortAsc").onchange = () => set({ desc: false });
  for (const [id, key] of [["fltPaired", "paired"], ["fltDrifted", "drifted"],
                           ["fltOrphan", "orphan"], ["fltStray", "stray"]]) {
    $(id).onchange = (e) => {
      itemSort.show[key] = e.target.checked;
      set({});
    };
  }
  $("sortBtn").onclick = (e) => { e.stopPropagation(); togglePop("sortCfg"); };
  $("sortCfg").onclick = (e) => e.stopPropagation();
}

// ---- views --------------------------------------------------------------
// In search mode the same table gains a Session column, since results come
// from all over the archive rather than one open session.
function sessionCell(it) {
  return `<td class="c-sess"><span class="sesslink" data-jump="${escapeHtml(it.run_id)}"
    title="${escapeHtml(it.session_description || "")} — go to this session">${escapeHtml(it.run_id)}</span></td>`;
}

function listHTML() {
  if (!shownItems.length) return emptyItemsHTML();
  const rows = shownItems.map((it, idx) => {
    const created = fmtCreated(it.timestamp);
    let r = `<tr data-i="${idx}" data-sc="0" class="${sameSel(idx, false) ? 'sel' : ''} ${isPicked(idx) ? 'multi' : ''}">
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
function emptyItemsHTML() {
  if (searchMode && !items.length) {
    return `<div class="empty">Nothing matched. Try fewer words, or widen the search under ⚙ Advanced.</div>`;
  }
  if (items.length) {
    return `<div class="empty">No files match the status filter — check ⇅ Sort.</div>`;
  }
  return `<div class="empty">This session has no files.</div>`;
}

function gridHTML() {
  if (!shownItems.length) return emptyItemsHTML();
  return `<div class="grid">${shownItems.map((it, idx) => `
    <div class="cell ${sameSel(idx, false) || isPicked(idx) ? 'sel' : ''}" data-i="${idx}" data-sc="0" title="${escapeHtml(it.detail)}">
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

function sameSel(idx, isSc) { return selected === shownItems[idx] && selectedIsSidecar === isSc; }
function isPicked(idx) { return picked.length > 1 && picked.includes(shownItems[idx]); }

function wireItems() {
  $("itemArea").querySelectorAll("[data-i]").forEach((el) => {
    const it = shownItems[+el.dataset.i];
    const isSc = el.dataset.sc === "1";
    el.onclick = (ev) => {
      const jump = ev.target.getAttribute && ev.target.getAttribute("data-jump");
      if (jump) { ev.stopPropagation(); jumpToSession(jump); return; }
      selectItem(it, isSc, ev);
    };
    el.ondblclick = () => activate(it, isSc);
    el.oncontextmenu = (ev) => {
      ev.preventDefault();
      // Right-clicking outside the current multi-selection selects that row
      // first, so the menu always acts on what is highlighted.
      if (!picked.includes(it)) selectItem(it, isSc);
      showItemMenu(ev.clientX, ev.clientY);
    };
  });
}

function selectItem(it, isSc, ev) {
  const toggle = ev && (ev.metaKey || ev.ctrlKey);
  const extend = ev && ev.shiftKey;

  if (extend && pickAnchor && shownItems.includes(pickAnchor)) {
    const a = shownItems.indexOf(pickAnchor), b = shownItems.indexOf(it);
    picked = shownItems.slice(Math.min(a, b), Math.max(a, b) + 1);
  } else if (toggle) {
    picked = picked.includes(it) ? picked.filter((x) => x !== it) : picked.concat([it]);
    pickAnchor = it;
  } else {
    picked = [it];
    pickAnchor = it;
  }

  // The primary is the row just clicked, unless a toggle removed it.
  selected = picked.includes(it) ? it : (picked[picked.length - 1] || null);
  selectedIsSidecar = selected === it ? isSc : false;
  renderItemArea();
  updateDetails();
  if (showSc && selected && selected.has_sidecar && picked.length === 1) {
    openSidecarPanel(selected);
  }
  if (showSess && selected && selected.session_path) refreshSessionInfo();
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
  // Reseal needs both halves (it re-records the file's hash in its sidecar);
  // "write sidecar" is for the opposite case, a file with no sidecar at all.
  $("resealBtn").disabled = !(hasArt && hasSc);
  $("adoptBtn").disabled = !(hasArt && !hasSc);
  $("delBtn").disabled = !it;
  if (!it) {
    $("detText").textContent = "Select an item to see its provenance.";
    $("detProv").innerHTML = "";
    $("addCollBtn").disabled = true;
    if ($("detColl")) $("detColl").innerHTML = "";
    return;
  }
  if (picked.length > 1) {
    const n = picked.length;
    $("detText").innerHTML = `<span class="det-count">${n} files selected</span>\n`
      + escapeHtml(picked.map((p) => p.display_name || p.name).slice(0, 6).join(", "))
      + (n > 6 ? `, and ${n - 6} more` : "");
    $("detProv").innerHTML = "";
    if ($("detColl")) $("detColl").innerHTML = "";
    $("addCollBtn").disabled = false;
    return;
  }
  const lines = it.detail.split("\n");
  $("detText").innerHTML = lines
    .map((ln, i) => (i === 0 ? `<span class="hl">${escapeHtml(ln)}</span>` : escapeHtml(ln)))
    .join("\n");
  $("detProv").innerHTML = provenanceLine(it);
  wireEntryPoint($("detProv"), it);
  $("addCollBtn").disabled = !selectedRef();
  refreshMembership();
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

// ---- dragging entries between collections -------------------------------
// Pointer events rather than HTML5 drag-and-drop: the webview's file-drop
// handler (what powers importing) interferes with in-page DnD, and this
// works the same on every platform.
let dragState = null;

function startEntryDrag(ev, { ref, label, from }) {
  if (ev.button !== 0) return;
  const startX = ev.clientX, startY = ev.clientY;
  dragState = { ref, label, from, active: false, target: null };

  const move = (e) => {
    if (!dragState) return;
    if (!dragState.active) {
      if (Math.hypot(e.clientX - startX, e.clientY - startY) < 5) return;
      dragState.active = true;
      const ghost = document.createElement("div");
      ghost.className = "drag-ghost";
      ghost.textContent = dragState.label;
      document.body.appendChild(ghost);
      dragState.ghost = ghost;
      document.body.classList.add("dragging-entry");
    }
    dragState.ghost.style.left = `${e.clientX + 12}px`;
    dragState.ghost.style.top = `${e.clientY + 12}px`;

    document.querySelectorAll(".drop-target").forEach((n) => n.classList.remove("drop-target"));
    const under = document.elementFromPoint(e.clientX, e.clientY);
    const target = under && under.closest("[data-drop-collection]");
    dragState.target = target ? target.getAttribute("data-drop-collection") : null;
    if (target && dragState.target !== dragState.from) target.classList.add("drop-target");
  };

  const up = async () => {
    document.removeEventListener("pointermove", move);
    document.removeEventListener("pointerup", up);
    document.querySelectorAll(".drop-target").forEach((n) => n.classList.remove("drop-target"));
    document.body.classList.remove("dragging-entry");
    const state = dragState;
    dragState = null;
    if (state && state.ghost) state.ghost.remove();
    if (!state || !state.active || !state.target || state.target === state.from) return;

    try {
      await call("collection_move", {
        archive, from: state.from, to: state.target, refs: [state.ref],
      });
      toast(`Moved ${state.label} to ${state.target}`);
      await loadCollections();
      if (openCollection) await showCollection(openCollection, { push: false });
    } catch (e) {
      // A cycle or duplicate: nothing was removed, so the entry is intact.
      toast(`${e}`);
    }
  };

  document.addEventListener("pointermove", move);
  document.addEventListener("pointerup", up);
}

// ---- context menus ------------------------------------------------------
// The webview's own menu is useless here, so right-click gets a real one.
let fileManagerName = "Finder";

function showMenu(x, y, entries) {
  const el = $("ctxMenu");
  el.innerHTML = entries.map((e) => {
    if (e.separator) return `<div class="sep"></div>`;
    if (e.head) return `<div class="head">${escapeHtml(e.head)}</div>`;
    return `<button ${e.disabled ? "disabled" : ""} class="${e.danger ? "danger" : ""}">`
      + `${escapeHtml(e.label)}</button>`;
  }).join("");
  el.classList.remove("hidden");

  // Place it at the cursor, flipped where it would fall off screen.
  const r = el.getBoundingClientRect();
  el.style.left = `${Math.max(4, Math.min(x, window.innerWidth - r.width - 6))}px`;
  el.style.top = `${Math.max(4, Math.min(y, window.innerHeight - r.height - 6))}px`;

  const buttons = [...el.querySelectorAll("button")];
  const actionable = entries.filter((e) => !e.separator && !e.head);
  buttons.forEach((btn, i) => {
    btn.onclick = () => {
      closeMenu();
      const entry = actionable[i];
      if (entry && entry.action) Promise.resolve(entry.action()).catch((err) => toast(`${err}`));
    };
  });
}

function closeMenu() { $("ctxMenu").classList.add("hidden"); }

function showItemMenu(x, y) {
  const many = picked.length > 1;
  const it = selected;
  const label = many ? `${picked.length} files` : (it ? it.display_name || it.name : "");
  const entries = [
    { head: label },
    { label: many ? "Add all to collection…" : "Add to collection…",
      action: () => openCollectionPicker(selectedRefs(), label) },
    { separator: true },
    { label: "Open", disabled: many || !(it && it.has_artifact),
      action: () => call("open_path", { path: it.artifact_path }) },
    { label: `Reveal in ${fileManagerName}`, disabled: !(it && (it.artifact_path || it.sidecar_path)),
      action: () => call("reveal_path", { path: it.artifact_path || it.sidecar_path }) },
    { label: "Show metadata", disabled: many || !(it && it.has_sidecar),
      action: () => openSidecarPanel(it) },
    { separator: true },
    { label: "Reseal checksum", disabled: many || !(it && it.has_artifact && it.has_sidecar),
      action: resealSelected },
    { label: many ? "Delete…" : "Delete…", danger: true, disabled: !it,
      action: deleteSelected },
  ];
  showMenu(x, y, entries);
}

function showCollectionMenu(x, y, name) {
  showMenu(x, y, [
    { head: collLabel(name) },
    { label: "New folder inside…", action: () => newNestedCollection(name) },
    { label: "Open", action: () => showCollection(name) },
    { label: "Rename…", action: () => renameCollection(name) },
    { separator: true },
    { label: "Delete collection…", danger: true, action: () => deleteCollection(name) },
  ]);
}

function showEntryMenu(x, y, entry) {
  const isFile = entry.kind === "file" && entry.exists;
  const isFolder = entry.kind === "folder" && entry.exists;
  showMenu(x, y, [
    { head: entry.ref },
    ...(isFolder ? [
      { label: "Open this folder", action: () => showCollection(entry.target) },
      { label: "New folder inside this one…", action: () => newNestedCollection(entry.target) },
      { label: "Rename…", action: () => renameCollection(entry.target) },
      { separator: true },
    ] : []),
    { label: "Add to collection…", disabled: !entry.exists,
      action: () => openCollectionPicker([entry.ref], entry.ref) },
    { label: `Reveal in ${fileManagerName}`, disabled: !isFile,
      action: () => call("reveal_path", { path: entry.path }) },
    { separator: true },
    { label: "Remove from this collection", danger: true,
      action: async () => {
        await call("collection_remove", { archive, name: openCollection, refs: [entry.ref] });
        toast(`Removed ${entry.ref} from ${openCollection}`);
        await loadCollections();
        await showCollection(openCollection);
      } },
  ]);
}

// A nested collection is a collection; "folder" is just what it is called
// when it sits inside another one.
async function newNestedCollection(parent) {
  return newCollection(parent);
}

// One path for both: a free-form title, stored under a slug so the archive
// stays portable (spaces and ':' are fine to read, not to put in filenames).
async function newCollection(parent) {
  const label = parent ? `New folder inside ${collLabel(parent)}` : "New collection";
  const title = await promptName(label, "");
  if (!title) return;
  try {
    const { slug } = await call("slugify", { text: title });
    await call("create_collection", { archive, name: slug, title });
    if (parent) {
      await call("collection_add", { archive, name: parent, refs: [`collections/${slug}`] });
    }
    await loadCollections();
    await showCollection(parent || slug, { push: !parent });
    toast(parent ? `Created ${title} inside ${collLabel(parent)}` : `Created ${title}`);
  } catch (e) {
    toast(`${e}`);
  }
}

// Renaming edits the readable title by default. The storable name only
// changes when the title has no usable slug yet, since renaming it rewrites
// every parent's ref.
async function renameCollection(name) {
  const current = collLabel(name);
  const next = await promptName("Rename collection", current);
  if (!next || next === current) return;
  try {
    await call("rename_collection", { archive, name, title: next });
    await loadCollections();
    if (openCollection === name) await showCollection(name, { push: false });
    toast(`Renamed to ${next}`);
  } catch (e) {
    toast(`${e}`);
  }
}

async function deleteCollection(name) {
  const ok = await confirmAction({
    body: `Delete the collection <b>${escapeHtml(name)}</b>?<br><br>`
      + `It is only a list of references — every file and session it points at `
      + `stays exactly where it is.`,
    confirmLabel: "Delete",
  });
  if (!ok) return;
  try {
    await call("delete_collection", { archive, name });
    if (openCollection === name) { openCollection = null; renderItemArea(); }
    await loadCollections();
    toast(`Deleted collection ${name}`);
  } catch (e) {
    toast(`${e}`);
  }
}

// ---- add to collection --------------------------------------------------
// The ref for the current selection, in the compact spelling: collections
// hold refs, not paths.
function selectedRef() {
  const ctx = selectedSession();
  if (!selected || !ctx) return null;
  return `${ctx.runId}/${selected.name}`;
}

// Every highlighted artefact as a ref. Each carries its own session, so a
// multi-selection from search results spanning sessions still works.
function selectedRefs() {
  const fallback = curSession && curSession.run_id;
  return picked
    .map((it) => {
      const runId = it.run_id || fallback;
      return runId ? `${runId}/${it.name}` : null;
    })
    .filter(Boolean);
}

async function openCollectionPicker(refs, label) {
  const list = Array.isArray(refs) ? refs.filter(Boolean) : (refs ? [refs] : []);
  if (!archive || !list.length) return;
  pendingCollRef = { refs: list, label: label || (list.length === 1 ? list[0] : `${list.length} items`) };
  $("collTarget").textContent = pendingCollRef.label;
  try {
    collections = await call("list_collections", { archive });
  } catch (e) {
    collections = [];
  }
  $("collPick").innerHTML = collections
    .map((c) => `<option value="${escapeHtml(c.name)}">${escapeHtml(c.name)}` +
                `${c.title ? "   " + escapeHtml(c.title) : ""}</option>`).join("");
  const none = collections.length === 0;
  $("collNew").checked = none;             // nothing to add to yet
  $("collPick").disabled = none;
  $("collNewFields").style.display = none ? "flex" : "none";
  $("collNewName").value = "";
  $("collNote").value = "";
  $("collScrim").classList.add("show");
}

let pendingCollRef = null;

function syncCollMode() {
  const makeNew = $("collNew").checked;
  $("collPick").disabled = makeNew;
  $("collNewFields").style.display = makeNew ? "flex" : "none";
}

async function doAddToCollection() {
  if (!pendingCollRef) return;
  const makeNew = $("collNew").checked;
  const name = makeNew ? $("collNewName").value.trim() : $("collPick").value;
  if (!name) { toast("Name the collection first."); return; }
  try {
    await call("collection_add", {
      archive, name, refs: pendingCollRef.refs, create: makeNew,
      note: $("collNote").value.trim(),
    });
    $("collScrim").classList.remove("show");
    toast(`Added ${pendingCollRef.label} to ${name}`);
    await loadCollections();
    await refreshMembership();
  } catch (e) {
    // A cycle, a duplicate, or an unparseable ref -- all worth showing.
    toast(`${e}`);
  }
}

// Which collections the selected file belongs to, shown beside it.
let membership = [];
async function refreshMembership() {
  const ref = selectedRef();
  membership = [];
  if (archive && ref) {
    try {
      membership = await call("collections_containing", { archive, ref });
    } catch (e) {
      membership = [];
    }
  }
  const el = $("detColl");
  if (!el) return;
  el.innerHTML = membership.length
    ? `<span class="dp"><span class="dp-k">Collections</span>` +
      membership.map((n) => `<span class="chip coll" data-open-coll="${escapeHtml(n)}">${escapeHtml(n)}</span>`).join(" ") +
      `</span>`
    : "";
  el.querySelectorAll("[data-open-coll]").forEach((n) => {
    n.onclick = () => { setRailTab("collections"); showCollection(n.getAttribute("data-open-coll")); };
  });
}

// ---- item management ----------------------------------------------------
function selectedSession() {
  if (!selected) return null;
  const path = selected.session_path || (curSession && curSession.path);
  const runId = selected.run_id || (curSession && curSession.run_id);
  return path && runId ? { path, runId } : null;
}

async function deleteSelected() {
  const ctx = selectedSession();
  if (!selected || !ctx) return;
  const ok = await confirmAction({
    body: `Move <b>${escapeHtml(selected.name)}</b> to <span class="mono">${escapeHtml(ctx.runId)}/.trash/</span>?`
      + `<br><br>Its sidecar goes with it and the deletion is logged in the session history. `
      + `Nothing is erased — you can move it back by hand.`,
    confirmLabel: "Move to trash",
  });
  if (!ok) return;
  try {
    await call("delete_file", { archive, run_id: ctx.runId, filename: selected.name });
    toast(`${selected.name} moved to .trash/`);
    selected = null;
    await reload();
  } catch (e) {
    // The delete guard refuses if another artefact derives from this one.
    const again = String(e).includes("derives from") || String(e).includes("still")
      ? await confirmAction({
          body: `nebula refused: <span class="mono">${escapeHtml(String(e))}</span>`
            + `<br><br>Delete anyway? The dependent file's provenance will point at something missing.`,
          confirmLabel: "Delete anyway",
        })
      : false;
    if (!again) { toast(`Delete failed: ${e}`); return; }
    try {
      await call("delete_file", { archive, run_id: ctx.runId, filename: selected.name, force: true });
      toast(`${selected.name} moved to .trash/ (forced)`);
      selected = null;
      await reload();
    } catch (e2) {
      toast(`Delete failed: ${e2}`);
    }
  }
}

async function resealSelected() {
  const ctx = selectedSession();
  if (!selected || !ctx) return;
  const ok = await confirmAction({
    body: `Re-record the checksum of <b>${escapeHtml(selected.name)}</b> from its current bytes?`
      + `<br><br>Only do this when you meant to change the file: it makes the sidecar agree `
      + `with whatever is on disk now, so genuine corruption would stop being detectable.`,
    confirmLabel: "Reseal",
  });
  if (!ok) return;
  try {
    const res = await call("reseal", { archive, run_id: ctx.runId, filename: selected.name });
    toast(`Resealed — sha256 ${String(res.sha256).slice(0, 12)}…`);
    await reloadItems();
  } catch (e) {
    toast(`Reseal failed: ${e}`);
  }
}

async function adoptSelected() {
  if (!selected || !selected.artifact_path) return;
  try {
    await call("adopt_file", { path: selected.artifact_path, origin: "adopted in Navigator" });
    toast(`Wrote a sidecar for ${selected.name}`);
    await reloadItems();
  } catch (e) {
    toast(`Could not write a sidecar: ${e}`);
  }
}

// ---- session management -------------------------------------------------
async function holdSession(release) {
  if (!curSession) return;
  try {
    if (release) {
      const res = await call("release", { archive, run_id: curSession.run_id });
      toast(res.had_hold ? `Hold released on ${curSession.run_id}` : "That session had no hold");
    } else {
      await call("hold", { archive, run_id: curSession.run_id });
      toast(`${curSession.run_id} held — it stays appendable past today`);
    }
    await reload();
  } catch (e) {
    toast(`Failed: ${e}`);
  }
}

async function deleteSession() {
  if (!curSession) return;
  const ok = await confirmAction({
    body: `Move the whole session <b>${escapeHtml(curSession.run_id)}</b> `
      + `(${curSession.n_items} file(s)) to the archive's <span class="mono">.trash/</span>?`
      + `<br><br>Nothing is erased, but anything deriving from its files will point at a `
      + `session that is no longer in the archive.`,
    confirmLabel: "Move to trash",
  });
  if (!ok) return;
  try {
    await call("delete_session", { archive, run_id: curSession.run_id });
    toast(`${curSession.run_id} moved to .trash/`);
    curSession = null; selected = null;
    await reload();
  } catch (e) {
    toast(`Delete failed: ${e}`);
  }
}

// ---- confirmation -------------------------------------------------------
// Destructive management actions ask first. Resolves true/false.
let cfmResolve = null;
function confirmAction({ body, confirmLabel = "Confirm", danger = true }) {
  $("cfmBody").innerHTML = body;
  $("cfmOk").textContent = confirmLabel;
  $("cfmOk").classList.toggle("danger", danger);
  $("cfmScrim").classList.add("show");
  return new Promise((resolve) => { cfmResolve = resolve; });
}
function closeConfirm(result) {
  $("cfmScrim").classList.remove("show");
  if (cfmResolve) { cfmResolve(result); cfmResolve = null; }
}

// ---- archive management -------------------------------------------------
let arcStats = null, checkResult = null, gcPreview = null;

async function openArchivePanel() {
  if (!archive) { toast("Open an archive first."); return; }
  $("arcScrim").classList.add("show");
  $("arcBody").innerHTML = `<div class="mg-note">Reading the archive…</div>`;
  await refreshArchiveStats();
}

async function refreshArchiveStats() {
  try {
    arcStats = await call("archive_stats", { archive });
  } catch (e) {
    $("arcBody").innerHTML = noteBox("err", `Could not read the archive: ${e}`);
    return;
  }
  renderArchivePanel();
}

function renderArchivePanel() {
  const a = arcStats;
  if (!a) return;
  const idx = a.index || {}, code = a.code || {}, cfg = a.settings || {};

  const overview = `<div class="mg"><div class="mg-h">Overview</div>` +
    row("Archive", `<span class="link" data-open-path="${escapeHtml(a.root)}">${escapeHtml(a.root)}</span>`,
        { mono: true, wrap: true, html: true }) +
    row("Sessions", String(a.n_sessions)) +
    row("Artefacts", `${a.n_items}` + (a.n_problems ? ` — <span class="warn-text">${a.n_problems} with problems</span>` : ""),
        { html: !!a.n_problems }) +
    row("Size", _human(a.size)) + `</div>`;

  const staleWarn = (a.stale_open || []).length
    ? noteBox("err", `${a.stale_open.length} session(s) still marked open from an earlier day `
        + `(${a.stale_open.map((s) => s.run_id).join(", ")}) — likely a script that never closed.`)
    : "";

  const index = `<div class="mg"><div class="mg-h">Index <span class="issue-w">rebuildable cache</span></div>` +
    (idx.exists
      ? row("Last rebuilt", fmtCreated(idx.built)) +
        row("Sessions indexed", idx.sessions === null ? "unreadable" : String(idx.sessions)) +
        (idx.stale ? noteBox("info", `The index lists ${idx.sessions} session(s) but the archive has `
          + `${a.n_sessions}. Rebuild to bring it up to date.`) : "")
      : noteBox("info", "No index yet. `nebula ls` and the CLI's graph queries need one; "
          + "the Navigator reads the filesystem directly and works without it.")) +
    `<div class="mg-actions"><button class="dbtn ghost" id="arcRebuild">Rebuild index</button></div></div>`;

  const issues = checkResult ? checkIssuesHTML(checkResult) : "";
  const integrity = `<div class="mg"><div class="mg-h">Integrity</div>` +
    `<div class="mg-note">Reports orphans, stray sidecars, dangling refs and missing code blobs.</div>` +
    `<div class="mg-actions">
       <button class="dbtn ghost" id="arcCheck">Run check</button>
       <label class="check"><input type="checkbox" id="arcVerify" /> Verify checksums <span class="hint">(re-hashes every file)</span></label>
     </div>${issues}</div>`;

  const gc = `<div class="mg"><div class="mg-h">Captured source</div>` +
    row("Stored", `${code.blobs} blob(s), ${code.manifests} snapshot(s)`) +
    row("Size", code.human || "0 B") +
    pathRow("Folder", code.dir) +
    `<div class="mg-actions">
       <button class="dbtn ghost" id="arcGc">Find unreferenced (dry run)</button>
       ${gcPreview && (gcPreview.manifests.length || gcPreview.blobs.length)
         ? `<button class="dbtn danger" id="arcGcDelete">Delete ${gcPreview.manifests.length + gcPreview.blobs.length} object(s)</button>`
         : ""}
     </div>` +
    (gcPreview ? gcHTML(gcPreview) : "") + `</div>`;

  const settings = `<div class="mg"><div class="mg-h">Settings</div>` +
    row("On overwrite", cfg.on_overwrite) +
    row("Capture source", cfg.capture_code ? "on" : "off") +
    pathRow("archive.yaml", cfg.config_file) +
    (cfg.config_exists ? "" : `<div class="mg-note">Not present — these are the defaults. `
      + `Change them with <span class="mono">nebula config</span>.</div>`) + `</div>`;

  const body = $("arcBody");
  body.innerHTML = staleWarn + overview + index + integrity + gc + settings;
  wirePaths(body);
  $("arcRebuild").onclick = rebuildIndex;
  $("arcCheck").onclick = runCheck;
  $("arcGc").onclick = () => runGc(false);
  if ($("arcGcDelete")) $("arcGcDelete").onclick = () => runGc(true);
}

function _human(n) {
  if (n === null || n === undefined) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = n, i = 0;
  while (size >= 1024 && i < units.length - 1) { size /= 1024; i++; }
  return i === 0 ? `${size} B` : `${size.toFixed(1)} ${units[i]}`;
}

function checkIssuesHTML(res) {
  if (!res.issues.length) {
    return noteBox("info", res.verified
      ? "No problems found, checksums included."
      : "No problems found (checksums not verified).");
  }
  const rows = res.issues.map((i) => `
    <div class="issue ${i.severity === "error" ? "" : "info"}">
      <div><span class="issue-k">${escapeHtml(i.kind)}</span>
        <span class="issue-w">${escapeHtml([i.session, i.file].filter(Boolean).join("/"))}</span></div>
      <div class="issue-d">${escapeHtml(i.detail)}</div>
      ${i.fix ? `<div class="issue-f">fix: ${escapeHtml(i.fix)}</div>` : ""}
    </div>`).join("");
  return `<div class="mg-note">${res.n_errors} error(s), ${res.n_info} info</div>`
    + `<div class="issues">${rows}</div>`;
}

function gcHTML(res) {
  const n = res.manifests.length + res.blobs.length;
  if (!n) {
    return noteBox("info", `Nothing unreferenced: ${res.live_manifests} snapshot(s) and `
      + `${res.live_blobs} blob(s) are all still pointed at by a sidecar.`);
  }
  return noteBox("info", `${res.manifests.length} snapshot(s) and ${res.blobs.length} blob(s) `
    + `(${res.human}) are not referenced by any sidecar${res.dry_run ? " — nothing deleted yet" : ""}.`);
}

async function rebuildIndex() {
  const btn = $("arcRebuild");
  btn.disabled = true; btn.textContent = "Rebuilding…";
  try {
    const res = await call("rebuild_index", { archive });
    arcStats = res.stats;
    toast("Index rebuilt");
  } catch (e) {
    toast(`Rebuild failed: ${e}`);
  }
  renderArchivePanel();
}

async function runCheck() {
  const btn = $("arcCheck");
  const verify = $("arcVerify").checked;
  btn.disabled = true; btn.textContent = verify ? "Checking (hashing)…" : "Checking…";
  try {
    checkResult = await call("check", { archive, verify });
  } catch (e) {
    toast(`Check failed: ${e}`);
  }
  renderArchivePanel();
  if (checkResult) $("arcVerify").checked = verify;
}

async function runGc(really) {
  if (really) {
    const n = gcPreview.manifests.length + gcPreview.blobs.length;
    const ok = await confirmAction({
      body: `Permanently delete <b>${n}</b> unreferenced object(s) (${escapeHtml(gcPreview.human)}) `
        + `from the code store?<br><br>Snapshots referenced by any sidecar — including trashed `
        + `sessions — are kept. This cannot be undone.`,
      confirmLabel: "Delete",
    });
    if (!ok) return;
  }
  try {
    gcPreview = await call("gc", { archive, delete: really });
    if (really) {
      toast(`Deleted ${gcPreview.manifests.length + gcPreview.blobs.length} object(s)`);
      await refreshArchiveStats();
      return;
    }
  } catch (e) {
    toast(`gc failed: ${e}`);
  }
  renderArchivePanel();
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
// Which session the info panel describes: the selected artefact's own,
// when it has one (collection and search results span sessions), else the
// session being browsed.
function panelSessionPath() {
  if (selected && selected.session_path) return selected.session_path;
  return curSession ? curSession.path : null;
}

async function refreshSessionInfo() {
  if (!showSess) return;
  const path = panelSessionPath();
  if (!path) { sessInfo = null; renderSessionPanel(); return; }
  sessInfo = await call("session_info", { session_path: path });
  sessNotes = null;                 // the draft belonged to the previous session
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
  const actions = `<div class="mg-actions">
      <button class="dbtn ghost" id="sessHold">${info.held ? "Release hold" : "Hold"}</button>
      <button class="dbtn ghost" id="sessAddColl">Add to collection</button>
      <button class="dbtn ghost danger-text" id="sessDelete">Move session to trash</button>
    </div>`;
  body.innerHTML = head + about + counts + actions + notesHTML("sess", notesDraft) + related +
    (history || noteBox("info", "No manual operations recorded."));
  wirePaths(body);
  $("sessHold").onclick = () => holdSession(!!info.held);
  $("sessAddColl").onclick = () => openCollectionPicker(info.run_id, info.run_id);
  $("sessDelete").onclick = deleteSession;
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
  for (const id of ["sessCfg", "itemCfg", "sortCfg"]) {
    if (id !== except) $(id).classList.add("hidden");
  }
}
function togglePop(id) {
  const el = $(id);
  const willShow = el.classList.contains("hidden");
  closePops(willShow ? id : null);
  el.classList.toggle("hidden", !willShow);
  if (willShow) placePop(el);
}

// A fixed popover has to be told where to go. Anchored under its button,
// nudged left if it would run off the right edge.
function placePop(el) {
  // Keyed off a class rather than the computed position: the style may not
  // be loaded yet, and this way the placement is testable.
  if (!el.classList.contains("anchored")) return;
  const anchor = el.parentElement && el.parentElement.querySelector("button");
  if (!anchor) return;
  const r = anchor.getBoundingClientRect();
  el.style.top = `${r.bottom + 4}px`;
  el.style.left = "0px";                       // measure at a known origin
  const width = el.getBoundingClientRect().width;
  const left = Math.max(8, Math.min(r.left, window.innerWidth - width - 8));
  el.style.left = `${left}px`;
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

// One table, two entry points: the macOS menu (which owns these
// accelerators and emits menu://action) and the keydown handler used
// everywhere else. Ids match install_menu() in main.rs.
function addSelectionToCollection() {
  const refs = selectedRefs();
  if (!refs.length) { toast("Select a file first."); return; }
  const label = refs.length === 1
    ? (selected.display_name || selected.name) : `${refs.length} files`;
  openCollectionPicker(refs, label);
}

const MENU_ACTIONS = {
  metadata: toggleMetadataPanel,
  collect: addSelectionToCollection,
  "tab-sessions": () => setRailTab("sessions"),
  "tab-collections": () => setRailTab("collections"),
  "tab-searches": () => setRailTab("views"),
  session: toggleSessionPanel,
  archive: openArchivePanel,
  reload: refreshAll,
  import: startImport,
  open: openSelectedExternally,
};

function runAction(name) {
  const action = MENU_ACTIONS[name];
  if (!action) return;
  Promise.resolve(action()).catch((err) => toast(`${err}`));
}

function initShortcuts() {
  // Menu items can't just be labels: an accelerator in the menu is
  // swallowed by the OS before the webview sees it, so the item sends the
  // action here instead.
  tauriEvent.listen("menu://action", (e) => runAction(e.payload))
    .catch((e) => console.error("menu listener failed", e));

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closePops(null); return; }
    if (!hasMod(e) || e.altKey) return;
    const k = (e.key || "").toLowerCase();
    const shift = e.shiftKey;

    let name = null;
    if (k === "m" && shift) name = "metadata";
    else if (k === "s" && shift) name = "session";
    else if (k === "r" && !shift) name = "reload";
    else if (k === "i" && shift) name = "import";
    else if (k === "o" && !shift) name = "open";
    else if (k === "c" && shift) name = "collect";
    else if (k === "1" && !shift) name = "tab-sessions";
    else if (k === "2" && !shift) name = "tab-collections";
    else if (k === "3" && !shift) name = "tab-searches";
    if (!name) return;

    e.preventDefault();   // Ctrl-R would otherwise reload the webview
    runAction(name);
  });
}

// ---- wiring -------------------------------------------------------------
$("openArchive").onclick = openArchive;
$("closeArchive").onclick = closeArchive;
$("archiveSel").onchange = (e) => onArchivePicked(e.target.value);
$("refresh").onclick = () => reload();
$("viewList").onclick = () => setView(true);
$("viewGrid").onclick = () => setView(false);
$("meta").onchange = (e) => { showMeta = e.target.checked; if (curSession || searchMode) renderItemArea(); };
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
$("tabSessions").onclick = () => setRailTab("sessions");
$("tabCollections").onclick = () => setRailTab("collections");
$("tabViews").onclick = () => setRailTab("views");
$("newCollBtn").onclick = () => newCollection(null);
$("saveViewBtn").onclick = saveCurrentSearch;
$("addCollBtn").onclick = () => addSelectionToCollection();
document.addEventListener("click", closeMenu);
document.addEventListener("contextmenu", (e) => {
  // Suppress the webview's own menu everywhere; ours is opt-in per element.
  if (!e.target.closest("input, textarea, .ctxmenu")) e.preventDefault();
});
$("collNew").onchange = syncCollMode;
$("collCancel").onclick = () => $("collScrim").classList.remove("show");
$("collOk").onclick = doAddToCollection;
$("collScrim").onclick = (e) => { if (e.target === $("collScrim")) $("collScrim").classList.remove("show"); };
$("arcBtn").onclick = openArchivePanel;
$("arcClose").onclick = () => $("arcScrim").classList.remove("show");
$("arcScrim").onclick = (e) => { if (e.target === $("arcScrim")) $("arcScrim").classList.remove("show"); };
$("cfmCancel").onclick = () => closeConfirm(false);
$("cfmOk").onclick = () => closeConfirm(true);
$("cfmScrim").onclick = (e) => { if (e.target === $("cfmScrim")) closeConfirm(false); };
$("resealBtn").onclick = resealSelected;
$("adoptBtn").onclick = adoptSelected;
$("delBtn").onclick = deleteSelected;
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
  itemSort = Object.assign(itemSort, LS.get("nebula.itemSort", {}));
  itemSort.show = Object.assign({ paired: true, drifted: true, orphan: true, stray: true },
                                itemSort.show || {});
  syncSortUI();
  wireSort();
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

  setRailTab(LS.get("nebula.railTab", "sessions"));
  archives = LS.get("nebula.archives", []);
  const last = localStorage.getItem("nebula.archive") || null;
  if (!archives.length && last) archives = [{ id: last, label: last }];

  try {
    fileManagerName = (await call("file_manager_name", {})).name || "Finder";
  } catch (e) {
    fileManagerName = "Finder";
  }
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
