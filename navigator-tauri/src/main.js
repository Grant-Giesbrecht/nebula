// Nebula Navigator -- front-end logic.
//
// No mock data: every session, item, and sidecar comes from the Python
// backend via the single `bridge` Tauri command. `call(op, args)` is the
// one entry point; everything else is rendering.

const invoke = window.__TAURI__.core.invoke;
// Each window keeps its own tabs, so two windows do not overwrite one
// another's saved state through the shared localStorage origin.
const WINDOW_LABEL = (() => {
  try {
    const w = window.__TAURI__.window;
    const cur = w && (w.getCurrentWindow ? w.getCurrentWindow() : w.getCurrent());
    return (cur && cur.label) || "main";
  } catch (e) { return "main"; }
})();
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
// calFrom/calTo are an inclusive day range (equal for a single click, both
// null for no filter), so drag-select and click share one code path.
let activity = null, calFrom = null, calTo = null, showCal = false, calWeeksShown = 0;
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
    await loadArchiveKind();
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
  loadActivity();
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

// ---- window tabs --------------------------------------------------------
// Finder-style tabs over one window. A tab owns a *location* -- which rail
// tab is showing, which session or collection is open, the search, the
// selection -- while the archive itself and the loaded session list stay
// shared, because they describe the machine's state rather than a place
// you navigated to.
//
// Switching tabs is therefore: freeze the current location into the tab we
// are leaving, thaw the one we are entering.
let tabs = [];              // [{ id, kind: "browse" | "tree", state }]
let activeTab = null;       // id
let tabSeq = 1;

function newTabId() { return `t${tabSeq++}`; }

function browseState() {
  return {
    railTab, searchMode,
    sessionRun: curSession ? curSession.run_id : null,
    sessionPath: curSession ? curSession.path : null,
    collection: openCollection, collPath: collPath.slice(),
    itemQuery: $("itemSearch").value, sessQuery,
    selectedName: selected ? selected.name : null,
    calFrom, calTo, showCal,
  };
}

function blankTab(kind, state) {
  return { id: newTabId(), kind, state: state || (kind === "browse" ? browseState() : {}) };
}

function activeTabObj() { return tabs.find((t) => t.id === activeTab) || null; }

function tabTitle(tab) {
  if (tab.kind === "index") return "Index";
  if (tab.kind === "tree") {
    const st = tab.state || {};
    return st.filename ? st.filename : (st.run_id ? `${st.run_id} relations` : "Relations");
  }
  const st = tab.state || {};
  if (st.searchMode) return st.itemQuery ? `“${st.itemQuery}”` : "Search";
  if (st.collection) return st.collection;
  if (st.sessionRun) return st.sessionRun;
  return "Archive";
}

const TAB_ICONS = {
  browse: `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>`,
  tree: `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="12" r="2.5"/><circle cx="6" cy="18" r="2.5"/><path d="M8.5 6H13a2 2 0 0 1 2 2v1.5"/><path d="M8.5 18H13a2 2 0 0 0 2-2v-1.5"/></svg>`,
  index: `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="9" y1="9" x2="9" y2="20"/></svg>`,
};

function renderTabs() {
  // A single tab is just "the window", so the strip stays out of the way
  // until there is actually a choice to make.
  $("tabbar").classList.toggle("hidden", tabs.length < 2);
  $("tabstrip").innerHTML = tabs.map((t) => `
    <div class="wtab ${t.id === activeTab ? "on" : ""}" data-tab="${t.id}" title="${escapeHtml(tabTitle(t))}">
      <span class="wt-ico">${TAB_ICONS[t.kind] || ""}</span>
      <span class="wt-label">${escapeHtml(tabTitle(t))}</span>
      <span class="wt-x" data-close="${t.id}" title="Close tab">✕</span>
    </div>`).join("");
  $("tabstrip").querySelectorAll(".wtab").forEach((el) => {
    el.onclick = (ev) => {
      const close = ev.target.closest("[data-close]");
      if (close) { closeTab(close.getAttribute("data-close")); return; }
      selectTab(el.getAttribute("data-tab"));
    };
    el.onpointerdown = (ev) => {
      if (ev.target.closest("[data-close]")) return;
      const tab = tabs.find((t) => t.id === el.getAttribute("data-tab"));
      if (tab) startTabDrag(ev, tab);
    };
    el.oncontextmenu = (ev) => {
      ev.preventDefault();
      const tab = tabs.find((t) => t.id === el.getAttribute("data-tab"));
      if (!tab) return;
      showMenu(ev.clientX, ev.clientY, [
        { head: tabTitle(tab) },
        { label: "Move to a new window", action: () => moveTabToNewWindow(tab) },
        { label: "Duplicate tab", action: () => addTab(tab.kind,
            JSON.parse(JSON.stringify(tab.state || {}))) },
        { separator: true },
        { label: "Close tab", danger: true, disabled: tabs.length < 2,
          action: () => closeTab(tab.id) },
      ]);
    };
  });
}

function applyTabChrome() {
  // A tree or index tab replaces the file list, so the file-list chrome goes
  // with it rather than sitting there inert.
  const kind = (activeTabObj() || {}).kind || "browse";
  const tree = kind === "tree", idx = kind === "index", browse = kind === "browse";
  $("treeArea").classList.toggle("hidden", !tree);
  $("treeTools").classList.toggle("hidden", !tree);
  $("idxArea").classList.toggle("hidden", !idx);
  $("idxTools").classList.toggle("hidden", !idx);
  $("itemArea").classList.toggle("hidden", !browse);
  document.querySelector(".main .search-row.wide").classList.toggle("hidden", !browse);
  document.querySelector(".main .toolbar:not(.tree-tools):not(.idx-tools)")
    .classList.toggle("hidden", !browse);
  $("rail").classList.toggle("hidden", !browse);
  document.getElementById("splitRail").classList.toggle("hidden", !browse);
}

async function selectTab(id) {
  if (id === activeTab) return;
  const cur = activeTabObj();
  if (cur && cur.kind === "browse") cur.state = browseState();
  activeTab = id;
  const tab = activeTabObj();
  renderTabs();
  applyTabChrome();
  if (!tab) return;
  if (tab.kind === "tree") { await renderTreeTab(tab); return; }
  if (tab.kind === "index") { await renderIndexTab(tab); return; }
  await restoreBrowse(tab.state || {});
}

async function restoreBrowse(st) {
  sessQuery = st.sessQuery || "";
  $("sessSearch").value = sessQuery;
  calFrom = st.calFrom || null; calTo = st.calTo || null;
  showCal = !!st.showCal;
  $("itemSearch").value = st.itemQuery || "";
  setRailTab(st.railTab || "sessions", { keepLocation: true });
  renderCalendar();
  applySessionFilter();

  if (st.searchMode && st.itemQuery) { await runItemSearch(); return; }
  searchMode = false; searchMeta = null;
  if (st.collection) {
    collPath = (st.collPath || []).slice();
    await loadCollections();
    await showCollection(st.collection, { push: false });
    return;
  }
  openCollection = null;
  const sess = sessions.find((x) => x.run_id === st.sessionRun);
  if (sess) {
    curSession = sess;
    selected = null; picked = []; pickAnchor = null;
    renderSessions();
    await reloadItems();
    const want = (items || []).find((i) => i.name === st.selectedName);
    if (want) { selected = want; applyItemView(); }
    updateDetails();
    await refreshSessionInfo();
  } else {
    curSession = null; items = []; selected = null;
    renderSessions(); applyItemView(); updateDetails();
    // Nothing open in this tab: the status bar has to say so, or it keeps
    // describing wherever the tab we just left was.
    $("statusbar").textContent = `${activeLabel()} — ${sessions.length} session(s)`;
  }
}

function addTab(kind, state, { activate = true } = {}) {
  const cur = activeTabObj();
  if (cur && cur.kind === "browse") cur.state = browseState();
  const tab = blankTab(kind, state);
  const at = tabs.findIndex((t) => t.id === activeTab);
  tabs.splice(at < 0 ? tabs.length : at + 1, 0, tab);   // beside its opener, like Finder
  saveTabs();
  if (!activate) { renderTabs(); return tab; }
  activeTab = null;                      // force selectTab to do the work
  selectTab(tab.id);
  return tab;
}

function closeTab(id) {
  if (tabs.length < 2) { toast("The last tab stays open."); return; }
  const idx = tabs.findIndex((t) => t.id === id);
  if (idx < 0) return;
  tabs.splice(idx, 1);
  saveTabs();
  if (id === activeTab) {
    const next = tabs[Math.min(idx, tabs.length - 1)];
    activeTab = null;
    selectTab(next.id);
  } else {
    renderTabs();
  }
}

async function openNewWindow() {
  try {
    await invoke("new_window");
  } catch (e) {
    toast(`Could not open a window: ${e}`);
  }
}

// A tab dragged out of the strip goes wherever it was dropped: onto another
// window if the pointer is over one, otherwise into a new window. The
// webview cannot see a pointer that has left it, so the OS side answers
// "which window is under the cursor" at drop time.
async function dropOnOtherWindow(ev, payload) {
  let label = null;
  try {
    label = await invoke("window_at_cursor");
  } catch (e) { label = null; }
  if (label && label === WINDOW_LABEL) return false;
  if (!label) {
    if (payload.kind !== "tab") return false;      // only tabs detach
    try {
      label = await invoke("new_window");
      // The new window has to finish booting before it can be handed a tab.
      await new Promise((r) => setTimeout(r, 700));
    } catch (e) { return false; }
  }
  try {
    await invoke("send_to_window", { label, payload });
  } catch (e) {
    toast(`${e}`);
    return false;
  }
  return true;
}

function startTabDrag(ev, tab) {
  if (ev.button !== 0) return;
  const startX = ev.clientX, startY = ev.clientY;
  let active = false, ghost = null;

  const move = (e) => {
    if (!active) {
      if (Math.hypot(e.clientX - startX, e.clientY - startY) < 8) return;
      active = true;
      ghost = document.createElement("div");
      ghost.className = "drag-ghost";
      ghost.textContent = tabTitle(tab);
      document.body.appendChild(ghost);
      document.body.classList.add("dragging-entry");
    }
    ghost.style.left = `${e.clientX + 12}px`;
    ghost.style.top = `${e.clientY + 12}px`;
  };
  const up = async (e) => {
    document.removeEventListener("pointermove", move);
    document.removeEventListener("pointerup", up);
    document.body.classList.remove("dragging-entry");
    if (ghost) ghost.remove();
    if (!active) return;
    const at = typeof document.elementFromPoint === "function"
      ? document.elementFromPoint(e.clientX, e.clientY) : null;
    const inStrip = !!at && $("tabbar").contains(at);
    if (inStrip) { reorderTab(tab, e.clientX); return; }
    const moved = await dropOnOtherWindow(e, {
      kind: "tab", tab: { kind: tab.kind, state: tab.state },
      archive,
    });
    if (moved) removeTab(tab.id);
  };
  document.addEventListener("pointermove", move);
  document.addEventListener("pointerup", up);
}

function reorderTab(tab, x) {
  const els = [...$("tabstrip").querySelectorAll(".wtab")];
  let at = els.length;
  els.forEach((el, i) => {
    const r = el.getBoundingClientRect();
    if (x < r.left + r.width / 2 && at === els.length) at = i;
  });
  const from = tabs.findIndex((t) => t.id === tab.id);
  if (from < 0) return;
  tabs.splice(from, 1);
  tabs.splice(at > from ? at - 1 : at, 0, tab);
  saveTabs();
  renderTabs();
}

function removeTab(id) {
  const idx = tabs.findIndex((t) => t.id === id);
  if (idx < 0) return;
  if (tabs.length < 2) {
    // The window would be left empty; give it a plain browse tab instead.
    tabs[idx] = blankTab("browse", {});
    activeTab = tabs[idx].id;
    saveTabs(); renderTabs(); applyTabChrome();
    restoreBrowse({});
    return;
  }
  tabs.splice(idx, 1);
  const next = tabs[Math.min(idx, tabs.length - 1)];
  saveTabs();
  if (id === activeTab) { activeTab = null; selectTab(next.id); } else { renderTabs(); }
}

// Something handed to this window by another one.
async function acceptFromWindow(payload) {
  if (!payload) return;
  if (payload.kind === "tab") {
    if (payload.archive && payload.archive !== archive) {
      try { await loadArchive(payload.archive); } catch (e) { /* keep ours */ }
    }
    addTab(payload.tab.kind, payload.tab.state);
    return;
  }
  if (payload.kind === "refs") {
    // Files dragged from another window: ask where they should go, rather
    // than guessing a collection in a window that may be showing something
    // else entirely.
    openCollectionPicker(payload.refs, payload.label || `${payload.refs.length} files`);
  }
}

async function moveTabToNewWindow(tab) {
  try {
    const label = await invoke("new_window");
    await new Promise((r) => setTimeout(r, 700));
    await invoke("send_to_window", {
      label, payload: { kind: "tab", tab: { kind: tab.kind, state: tab.state }, archive },
    });
    removeTab(tab.id);
  } catch (e) {
    toast(`Could not move the tab: ${e}`);
  }
}

function cycleTab(delta) {
  if (tabs.length < 2) return;
  const idx = tabs.findIndex((t) => t.id === activeTab);
  const next = tabs[(idx + delta + tabs.length) % tabs.length];
  selectTab(next.id);
}

function restoreTabs() {
  // Tabs come back as *locations*, not as loaded data: the archive is
  // loaded once afterwards, and whichever tab is active then restores
  // itself against it. A saved tab pointing at a session that has since
  // gone simply lands on the archive with nothing open.
  // Per window: a second window starts fresh rather than inheriting, and
  // closing one does not disturb the other's tabs.
  const saved = LS.get(tabsKey(), null) || (WINDOW_LABEL === "main"
    ? LS.get("nebula.tabs", null) : null);
  const list = saved && Array.isArray(saved.tabs) ? saved.tabs.filter((t) => t && t.id) : [];
  if (list.length) {
    tabs = list;
    activeTab = saved.activeTab && list.some((t) => t.id === saved.activeTab)
      ? saved.activeTab : list[0].id;
    // Keep generated ids from colliding with restored ones.
    tabSeq = 1 + list.reduce((m, t) => Math.max(m, parseInt(String(t.id).slice(1), 10) || 0), 0);
  } else {
    tabs = [blankTab("browse")];
    activeTab = tabs[0].id;
  }
  renderTabs();
  applyTabChrome();
}

// Tab titles follow the location, so re-render the strip whenever the
// location moves -- but coalesce, since navigation fires several of these.
let tabSaveTimer = null;
function scheduleTabSave() {
  clearTimeout(tabSaveTimer);
  tabSaveTimer = setTimeout(() => { saveTabs(); renderTabs(); }, 120);
}

function tabsKey() { return `nebula.tabs.${WINDOW_LABEL}`; }

function saveTabs() {
  const cur = activeTabObj();
  if (cur && cur.kind === "browse") cur.state = browseState();
  LS.set(tabsKey(), { tabs, activeTab });
}

function duplicateTab() {
  const cur = activeTabObj();
  if (!cur) return;
  addTab(cur.kind, JSON.parse(JSON.stringify(cur.kind === "browse" ? browseState() : cur.state)));
}

// Opening "in a new tab" is the same gesture everywhere: build the location
// the click would have navigated to, and hand it to a fresh tab instead.
function openSessionInNewTab(s) {
  addTab("browse", Object.assign(browseState(), {
    railTab: "sessions", searchMode: false, collection: null, collPath: [],
    sessionRun: s.run_id, sessionPath: s.path, selectedName: null, itemQuery: "",
  }));
}

function openCollectionInNewTab(name) {
  addTab("browse", Object.assign(browseState(), {
    railTab: "collections", searchMode: false, collection: name, collPath: [name],
    selectedName: null, itemQuery: "",
  }));
}

function openSearchInNewTab(query) {
  addTab("browse", Object.assign(browseState(), {
    railTab: "sessions", searchMode: true, itemQuery: query,
    collection: null, collPath: [], selectedName: null,
  }));
}

function openRelationsTab(runId, filename) {
  if (!runId) { toast("Select a session or file first."); return; }
  addTab("tree", { run_id: runId, filename: filename || null,
                   direction: "both", depth: 3 });
}

function openIndexTab(runId) {
  if (!archive) { toast("No archive open."); return; }
  addTab("index", { table: "sessions", query: "", run_id: runId || "", offset: 0 });
}

function openRelationsForSelection() {
  if (!archive) { toast("No archive open."); return; }
  if (selected && curSession) return openRelationsTab(curSession.run_id, selected.name);
  if (selected && selected.run_id) return openRelationsTab(selected.run_id, selected.name);
  if (curSession) return openRelationsTab(curSession.run_id, null);
  toast("Open a session or select a file first.");
}

// The two things about a session that are *claims* rather than data: the
// signature the index recorded for it, and any lock (a hold, or a seal over
// its year). Both are shown next to what is true on disk right now, so the
// index can be checked rather than believed.
function indexStateHTML(ix, runId) {
  if (!ix) return "";
  const short = (v) => (v ? String(v).slice(0, 12) : "—");
  const sig = (label, value, cls) =>
    `<div class="sig-row"><span class="dp-k">${escapeHtml(label)}</span>`
    + `<span class="sig ${cls || ""}">${escapeHtml(short(value))}</span></div>`;

  let verdict;
  if (!ix.index_exists) {
    verdict = noteBox("info", "No index yet — the Navigator reads the filesystem "
      + "directly, so nothing here depends on one.");
  } else if (!ix.index_usable) {
    verdict = noteBox("info", "The index was written by a different version of nebula; "
      + "it will be rebuilt the next time anything reads it.");
  } else if (!ix.indexed) {
    verdict = noteBox("info", "This session is not in the index yet. The next read "
      + "sweeps it in automatically.");
  } else if (ix.in_sync) {
    verdict = noteBox("ok", "The index matches what is on disk.");
  } else {
    verdict = noteBox("info", "This session has changed since it was indexed. "
      + "The next read picks it up — or sweep now from the index window.");
  }

  const seal = ix.sealed
    ? row("Year seal", `${escapeHtml(ix.year)} sealed ${escapeHtml(fmtCreated(ix.seal && ix.seal.sealed))}`
        + (ix.skipped_by_seal
           ? ` — <span class="warn-text">freshness sweeps skip this year</span>`
           : ` — not yet verified by the index, so sweeps still check it`),
        { html: true })
    : row("Year seal", `${ix.year} is not sealed`);

  return group("Index & locks",   // group() escapes the title itself
    sig("On disk", ix.live_sig) +
    sig("Indexed", ix.indexed_sig,
        ix.indexed ? (ix.in_sync ? "sig-ok" : "sig-bad") : "") +
    verdict +
    row("Hold", holdText(runId)) +
    seal +
    `<div class="mg-actions"><button class="dbtn ghost" id="sessIndexOpen">`
    + `Show this session in the index…</button></div>`);
}

function holdText(runId) {
  const info = sessInfo || {};
  if (!info.held) return info.appendable ? "not held (still appendable today)" : "not held";
  return info.hold_until === "forever"
    ? "held indefinitely — writes stay allowed until released"
    : `held until ${fmtCreated(info.hold_until)}`;
}

// ---- archive kinds and transfers ---------------------------------------
// Three kinds share one format and differ only in policy: a standard
// archive owns its ids, an intake archive's are provisional (I-...) until a
// merge renames them, and a fragment carries someone else's ids unchanged
// so a citation stays valid. The badge exists because those differences are
// invisible in the file list but change what the buttons may do.
let archiveKind = null;

async function loadArchiveKind() {
  archiveKind = null;
  if (archive) {
    try { archiveKind = await call("archive_kind", { archive }); } catch (e) { archiveKind = null; }
  }
  renderKindBadge();
}

function renderKindBadge() {
  const el = $("kindBadge");
  const k = archiveKind;
  if (!k || (k.kind === "standard" && !k.locked)) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  el.className = `kind-badge ${k.locked ? "locked" : k.kind}`;
  el.textContent = k.locked ? "merged — locked" : k.kind;
  el.title = k.locked
    ? `Merged into ${k.merged_to} on ${k.merged_at}. Writing is refused so nothing `
      + `written now could be mistaken for data that has already been merged.`
    : k.kind === "fragment"
      ? `An excerpt of ${k.user || "someone"}'s ${k.name}. Ids are theirs and are kept `
        + `exactly, so a reference to one stays valid. Read-only.`
      : `Sessions here are numbered I-<yy>-<nnnn> and are provisional: merging into a `
        + `standard archive renames them and records what they became.`;
}

let xferState = null;

function xferRowsHTML(plan) {
  return (plan.sessions || []).map((s) => {
    const renamed = s.new_run_id !== s.run_id;
    const bits = [];
    if (s.partial) bits.push(`${s.omitted} omitted`);
    if (s.note) bits.push(s.note);
    return `<div class="xfer-row ${s.foreign ? "foreign" : ""}">
        <span class="rid">${escapeHtml(s.run_id)}</span>
        ${renamed ? `<span class="arrow">→</span><span class="new">${escapeHtml(s.new_run_id)}</span>` : ""}
        ${bits.length ? `<span class="tag">${escapeHtml(bits.join(" · "))}</span>` : ""}
        <span class="n">${s.files.length} file(s) · ${_human(s.bytes)}</span>
      </div>`;
  }).join("");
}

function xferPlanHTML(plan, op) {
  const renamed = Object.keys(plan.renames || {}).length;
  const notes = [];
  if (op === "export") {
    notes.push("Session ids are kept exactly as they are here, so anything citing "
      + "them stays valid. The result is read-only and cannot be merged.");
  } else if (op === "merge") {
    notes.push("Each session gets a new permanent id, and both archives record the "
      + "pairing — so a notebook entry naming the intake id still resolves.");
  } else {
    notes.push("Copies are taken; the fragment is not modified. Each session records "
      + "the nebula:// URI it came from.");
  }
  const head = `<div class="xfer-sum">
      <span class="big">${plan.n_sessions} session(s)</span>
      <span>${plan.n_files} file(s)</span>
      <span>${_human(plan.bytes)}</span>
      ${renamed ? `<span>${renamed} renamed</span>` : ""}
      ${plan.foreign_bytes ? `<span>${_human(plan.foreign_bytes)} from other archives</span>` : ""}
    </div>`;
  const list = plan.sessions.length
    ? `<div class="xfer-list">${xferRowsHTML(plan)}</div>`
    : noteBox("info", "Nothing to transfer.");
  const skipped = (plan.skipped || []).length
    ? noteBox("info", `Skipped: ` + plan.skipped
        .map((s) => `${s.run_id} (${s.note})`).join(", ")) : "";
  const dangling = (plan.dangling || []).length
    ? noteBox("info", `${plan.dangling.length} reference(s) will not resolve in the `
        + `result: ` + plan.dangling.slice(0, 4).map((d) => d.ref).join(", ")
        + (plan.dangling.length > 4 ? "…" : "")) : "";
  const colls = (plan.collections || []).length
    ? noteBox("info", `Collections coming too: ` + plan.collections
        .map((c) => c.renamed ? `${c.name} → ${c.new_name}` : c.name).join(", ")
        + (plan.collections.some((c) => c.renamed)
           ? " (renamed where the name was already taken, so nothing curated here "
             + "silently gains entries)" : "")) : "";
  const warn = (plan.warnings || []).map((w) => noteBox("err", w)).join("");
  return head + list + skipped + dangling + colls + warn + noteBox("info", notes[0]);
}

async function openTransfer(op, args, { title }) {
  xferState = { op, args, plan: null };
  $("xferTitle").querySelector("span").textContent = title;
  $("xferBody").innerHTML = `<div class="idx-note">Working out what would move…</div>`;
  $("xferForeignWrap").classList.toggle("hidden", op !== "export");
  $("xferOk").disabled = true;
  $("xferScrim").classList.add("show");

  const res = await call("transfer_plan", Object.assign({ op, archive }, args,
    op === "export" ? { include_foreign: $("xferForeign").checked } : {}));
  if (!res.ok) {
    $("xferBody").innerHTML = noteBox("err", res.error || "could not plan the transfer");
    return;
  }
  xferState.plan = res.plan;
  $("xferBody").innerHTML = xferPlanHTML(res.plan, op);
  $("xferOk").disabled = !res.plan.n_sessions;
  $("xferOk").textContent = op === "export" ? "Export" : op === "merge" ? "Merge" : "Adopt";
}

async function runTransfer() {
  if (!xferState || !xferState.plan) return;
  const { op, args } = xferState;
  $("xferOk").disabled = true;
  $("xferOk").textContent = "Working…";
  const res = await call("transfer_run", Object.assign({ op, archive }, args,
    op === "export" ? { include_foreign: $("xferForeign").checked } : {}));
  $("xferScrim").classList.remove("show");
  if (!res.ok) { toast(res.error || "transfer failed"); return; }
  const p = res.plan;
  toast(op === "export"
    ? `Exported ${p.n_sessions} session(s) to ${args.dest}`
    : `${op === "merge" ? "Merged" : "Adopted"} ${p.n_sessions} session(s)`);
  if (op !== "export") { await reload(); await loadArchiveKind(); }
}

async function exportSelection({ collection = null, sessions = null, refs = null,
                                 label = "" }) {
  if (!archive) { toast("No archive open."); return; }
  const dest = await pickFolder(`Where should the fragment go?`);
  if (!dest) return;
  const name = (label || "selection").replace(/[^A-Za-z0-9._-]+/g, "-");
  openTransfer("export", { dest: `${dest}/${name}-fragment`, collection, sessions, refs },
               { title: `Export ${label || "selection"}` });
}

async function pickFolder(title) {
  try {
    const picked = await dialogOpen({ directory: true, multiple: false, title });
    return typeof picked === "string" ? picked : null;
  } catch (e) { return null; }
}

async function startMerge() {
  if (!archive) { toast("No archive open."); return; }
  const source = await pickFolder("Which intake archive should be merged in?");
  if (!source) return;
  openTransfer("merge", { source }, { title: "Merge intake archive" });
}

async function startAdopt() {
  if (!archive) { toast("No archive open."); return; }
  const source = await pickFolder("Which fragment should be adopted?");
  if (!source) return;
  openTransfer("adopt", { source }, { title: "Adopt from a fragment" });
}

// ---- index inspector ----------------------------------------------------
// A read-only window onto index.db. It shows the index *as it is* and never
// sweeps on its own: a status display that quietly repaired what it was
// describing could never show you a problem. Sweeping is a button.
const IDX_PAGE = 200;
let idxData = null;

async function renderIndexTab(tab) {
  const st = tab.state;
  $("idxSearch").value = st.query || "";
  $("idxArea").innerHTML = `<div class="idx-note">Reading the index…</div>`;
  if (!archive) { $("idxArea").innerHTML = `<div class="idx-note">No archive open.</div>`; return; }
  try {
    idxData = await call("index_view", {
      archive, table: st.table || "sessions", query: st.query || "",
      run_id: st.run_id || "", limit: IDX_PAGE, offset: st.offset || 0,
    });
  } catch (e) {
    idxData = null;
    $("idxArea").innerHTML = `<div class="idx-note">Couldn't read the index: ${escapeHtml(String(e))}</div>`;
    return;
  }
  renderIndexView();
  $("statusbar").textContent = `${activeLabel()} — index: ${idxData.table}`
    + ` (${idxData.total} row(s))`;
}

function idxCell(v) {
  if (v === null || v === undefined) return `<span class="null">null</span>`;
  const text = String(v);
  return `<span title="${escapeHtml(text)}">${escapeHtml(text)}</span>`;
}

function renderIndexView() {
  if (!idxData) return;
  const st = (activeTabObj() || {}).state || {};
  const s = idxData.status || {};

  $("idxTabs").innerHTML = (idxData.tables || []).map((t) => `
    <button class="tbtn ${t.name === idxData.table ? "on" : ""}" data-itab="${t.name}">
      ${escapeHtml(t.name)}<span class="idx-count">${t.rows === null ? "—" : t.rows}</span>
    </button>`).join("");
  $("idxTabs").querySelectorAll("[data-itab]").forEach((el) => {
    el.onclick = () => {
      const tab = activeTabObj();
      if (!tab) return;
      tab.state.table = el.getAttribute("data-itab");
      tab.state.offset = 0;
      saveTabs();
      renderIndexTab(tab);
    };
  });

  const head = `<div class="idx-head">
      <span><span class="k">File</span><span class="v mono">${escapeHtml(s.path || "")}</span></span>
      <span><span class="k">Schema</span><span class="v">v${escapeHtml(String(s.schema_version || "?"))}`
        + `${s.usable === false ? " (out of date)" : ""}</span></span>
      <span><span class="k">Built</span><span class="v">${escapeHtml(fmtCreated(s.built) || "—")}</span></span>
      <span><span class="k">Size</span><span class="v">${_human(s.size || 0)}</span></span>
      ${(s.sealed_years || []).length
        ? `<span><span class="k">Sealed</span><span class="v">`
          + (s.sealed_years || []).map((y) => `${escapeHtml(y.year)} (${y.sessions})`).join(", ")
          + `</span></span>` : ""}
      ${st.run_id ? `<span><span class="k">Filtered</span><span class="v mono">`
        + `${escapeHtml(st.run_id)}</span> <span class="open-link" id="idxClearRun">show all</span></span>` : ""}
    </div>`;

  if (idxData.error) {
    $("idxArea").innerHTML = head + `<div class="idx-note">${escapeHtml(idxData.error)}</div>`;
    wireIndexHead();
    return;
  }

  const cols = idxData.columns || [];
  const rows = idxData.rows || [];
  const body = rows.length
    ? `<table class="idx-table"><thead><tr>`
      + cols.map((c) => `<th>${escapeHtml(c)}</th>`).join("")
      + `</tr></thead><tbody>`
      + rows.map((r) => `<tr>` + cols.map((c) => `<td>${idxCell(r[c])}</td>`).join("") + `</tr>`).join("")
      + `</tbody></table>`
    : `<div class="idx-note">No rows${idxData.query ? " match that filter" : ""}.</div>`;

  $("idxArea").innerHTML = head + body;
  wireIndexHead();

  const from = rows.length ? (idxData.offset + 1) : 0;
  $("idxPage").textContent = `${from}–${idxData.offset + rows.length} of ${idxData.total}`;
  $("idxPrev").disabled = idxData.offset <= 0;
  $("idxNext").disabled = idxData.offset + rows.length >= idxData.total;
}

function wireIndexHead() {
  const clear = $("idxClearRun");
  if (!clear) return;
  clear.onclick = () => {
    const tab = activeTabObj();
    if (!tab) return;
    tab.state.run_id = ""; tab.state.offset = 0;
    saveTabs();
    renderIndexTab(tab);
  };
}

function idxPageBy(delta) {
  const tab = activeTabObj();
  if (!tab || tab.kind !== "index") return;
  tab.state.offset = Math.max(0, (tab.state.offset || 0) + delta * IDX_PAGE);
  saveTabs();
  renderIndexTab(tab);
}

// ---- relations (tree) view ---------------------------------------------
let treeData = null, treeExpanded = {};

async function renderTreeTab(tab) {
  const st = tab.state;
  $("treeUp").checked = st.direction !== "down";
  $("treeDown").checked = st.direction !== "up";
  $("treeDepth").value = String(st.depth || 3);
  $("treeRootLabel").textContent = st.filename ? `${st.run_id}/${st.filename}` : st.run_id;
  $("treeArea").innerHTML = `<div class="tree-empty">Working…</div>`;
  if (!archive) { $("treeArea").innerHTML = `<div class="tree-empty">No archive open.</div>`; return; }
  try {
    treeData = await call("provenance_tree", {
      archive, run_id: st.run_id, filename: st.filename || "",
      direction: st.direction || "both", depth: st.depth || 3,
    });
  } catch (e) {
    treeData = null;
    $("treeArea").innerHTML = `<div class="tree-empty">Couldn't build the tree: ${escapeHtml(String(e))}</div>`;
    return;
  }
  renderTree();
  $("statusbar").textContent = `${activeLabel()} — relations for `
    + `${st.filename ? st.run_id + "/" + st.filename : st.run_id}`;
}

// "index" means the answer came from the swept SQLite index; "scan" means
// it came from reading sidecars. Both are correct -- but only one of them
// is fast, and a view shouldn't imply the wrong one.
function treeSourceLabel(source) {
  return source === "scan"
    ? "read from sidecars (no usable index)"
    : "from the index";
}

function treeNodeHTML(node, dir, depthPath) {
  const key = `${depthPath}|${node.ref}`;
  const kids = node.children || [];
  const open = treeExpanded[key] !== false;
  const arrow = dir === "up" ? "←" : "→";
  const cls = [
    "tnode",
    node.resolved === false ? "unresolved" : "",
    node.resolved !== false && !node.exists ? "missing" : "",
    node.exists ? "go" : "",
  ].filter(Boolean).join(" ");
  const where = node.run_id && node.filename ? node.run_id : "";
  // Same twisty as the collections tree, so the two trees behave alike.
  const chev = kids.length
    ? `<span class="tw" data-tex="${escapeHtml(key)}" title="${open ? "Collapse" : "Expand"}">`
      + `<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">`
      + `<path d="${open ? "M3.5 6 L8 10.5 L12.5 6" : "M6 3.5 L10.5 8 L6 12.5"}"`
      + ` fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"`
      + ` stroke-linejoin="round"/></svg></span>`
    : `<span class="tw empty"></span>`;
  return `
    <div class="tnode-wrap">
      <div class="${cls}" ${node.exists ? `data-goto="${escapeHtml(node.run_id)}" data-file="${escapeHtml(node.filename || "")}"` : ""}>
        ${chev}
        <span class="tn-arrow">${arrow}</span>
        <span class="tn-name">${escapeHtml(node.filename || node.run_id)}</span>
        ${where ? `<span class="tn-where">${escapeHtml(where)}</span>` : ""}
        ${node.archive && node.resolved === false ? `<span class="tn-where">${escapeHtml(node.archive)}</span>` : ""}
        ${node.note ? `<span class="tn-note">${escapeHtml(node.note)}</span>` : ""}
        ${node.seen ? `<span class="tn-seen">shown above</span>` : ""}
      </div>
      ${kids.length && open ? `<div class="tkids">${kids.map((k) => treeNodeHTML(k, dir, key)).join("")}</div>` : ""}
      ${node.truncated ? `<div class="tkids"><span class="tmore" data-deeper="1">more beyond depth ${treeData.depth} — go deeper</span></div>` : ""}
    </div>`;
}

function renderTree() {
  if (!treeData) return;
  const dirs = [];
  if (treeData.direction !== "down") dirs.push(["upstream", "up", "Built from"]);
  if (treeData.direction !== "up") dirs.push(["downstream", "down", "Used by"]);

  const body = (treeData.branches || []).map((b) => {
    const sections = dirs.map(([key, dir, label]) => {
      const nodes = b[key] || [];
      const truncFlag = dir === "up" ? b.item.truncated_up : b.item.truncated_down;
      if (!nodes.length) {
        return `<div class="tsec"><div class="tsec-h">${label}</div>`
          + `<div class="tree-empty">nothing recorded</div></div>`;
      }
      return `<div class="tsec"><div class="tsec-h">${label}</div>`
        + nodes.map((n) => treeNodeHTML(n, dir, b.item.ref)).join("")
        + (truncFlag ? `<span class="tmore" data-deeper="1">more beyond depth ${treeData.depth} — go deeper</span>` : "")
        + `</div>`;
    }).join("");
    return `<div class="tbranch">
        <div class="tb-h">
          <span class="tn-name">${escapeHtml(b.item.filename || b.item.run_id)}</span>
          <span class="tn-where">${escapeHtml(b.item.run_id)}</span>
        </div>${sections}</div>`;
  }).join("");

  const sess = treeData.session;
  const head = sess
    ? `<div class="tsec-h">${escapeHtml(sess.run_id)} — ${escapeHtml(sess.description || "")}</div>`
    : "";
  $("treeArea").innerHTML = head + (body || `<div class="tree-empty">This session has no artefacts with metadata.</div>`);
  $("treeSource").textContent = treeSourceLabel(treeData.source);

  $("treeArea").querySelectorAll("[data-tex]").forEach((el) => {
    el.onclick = (ev) => {
      ev.stopPropagation();
      const key = el.getAttribute("data-tex");
      treeExpanded[key] = treeExpanded[key] === false;
      renderTree();
    };
  });
  $("treeArea").querySelectorAll("[data-deeper]").forEach((el) => {
    el.onclick = () => {
      const tab = activeTabObj();
      if (!tab) return;
      tab.state.depth = Math.min(50, (tab.state.depth || 3) * 2);
      saveTabs();
      renderTreeTab(tab);
    };
  });
  $("treeArea").querySelectorAll(".tnode.go").forEach((el) => {
    el.onclick = (ev) => {
      if (ev.target.closest("[data-tex]")) return;
      const run = el.getAttribute("data-goto");
      const file = el.getAttribute("data-file");
      addTab("browse", Object.assign(browseState(), {
        railTab: "sessions", searchMode: false, collection: null, collPath: [],
        sessionRun: run, sessionPath: null, selectedName: file || null, itemQuery: "",
      }));
    };
  });
}

// ---- activity calendar --------------------------------------------------
// A GitHub-style strip over the archive's own timeline: the data is just
// list_sessions' created dates bucketed by local day, so this renders what
// data/<year>/ already contains rather than needing an index.
async function loadActivity() {
  if (!archive) return;
  try {
    activity = await call("activity", { archive });
  } catch (e) {
    activity = null;
  }
  renderCalendar();
}

function dayKey(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`
    + `-${String(d.getDate()).padStart(2, "0")}`;
}

function calLevel(n, busiest) {
  if (!n) return 0;
  if (busiest <= 1) return 3;
  const share = n / busiest;
  return share > 0.66 ? 3 : share > 0.33 ? 2 : 1;
}

// Fit as many trailing weeks as the rail is currently wide enough for. Measure
// the rail, not the strip: the strip's own overflow would otherwise be part of
// what we measure, and it could never shrink again.
function calWeeks() {
  const width = ($("rail").getBoundingClientRect().width || 240) - 28;
  return Math.max(6, Math.min(53, Math.floor(width / 12)));
}

// Re-lay the strip when the splitter moves, or it keeps the week count it was
// built with and spills past the rail.
function watchCalendarWidth() {
  if (typeof ResizeObserver === "undefined") return;
  new ResizeObserver(() => {
    if (showCal && activity && calWeeks() !== calWeeksShown) renderCalendar();
  }).observe($("rail"));
}

function renderCalendar() {
  $("calendar").classList.toggle("hidden", !showCal);
  $("calToggle").classList.toggle("on", showCal);
  if (!showCal || !activity) return;

  const weeks = calWeeks();
  calWeeksShown = weeks;

  const today = new Date();
  const end = new Date(today);
  end.setDate(end.getDate() + (6 - end.getDay()));      // end of this week
  const start = new Date(end);
  start.setDate(start.getDate() - (weeks * 7 - 1));

  const cells = [];
  const months = [];
  let lastMonth = null;
  for (let w = 0; w < weeks; w++) {
    const first = new Date(start);
    first.setDate(first.getDate() + w * 7);
    const label = first.toLocaleString(undefined, { month: "short" });
    months.push(`<span style="width:12px">${first.getMonth() !== lastMonth ? escapeHtml(label) : ""}</span>`);
    if (first.getMonth() !== lastMonth) lastMonth = first.getMonth();

    for (let d = 0; d < 7; d++) {
      const day = new Date(start);
      day.setDate(day.getDate() + w * 7 + d);
      const key = dayKey(day);
      const info = (activity.days || {})[key];
      const future = day > today;
      const lvl = calLevel(info ? info.sessions : 0, activity.busiest || 1);
      const title = future ? ""
        : info ? `${key} — ${info.sessions} session(s), ${info.items} file(s)`
        : `${key} — nothing`;
      cells.push(`<div class="cal-cell lv${lvl} ${future ? "future" : ""} `
        + `${inCalRange(key) ? "sel" : ""}" data-day="${key}" title="${escapeHtml(title)}"
           style="grid-row:${d + 1}"></div>`);
    }
  }
  $("calMonths").innerHTML = months.join("");
  $("calGrid").innerHTML = cells.join("");
  $("calLegendText").textContent = !calFrom ? `${Object.keys(activity.days || {}).length} active day(s)`
    : calFrom === calTo ? `showing ${calFrom}` : `showing ${calFrom} → ${calTo}`;

  bindCalendarDrag();
}

function inCalRange(key) {
  return !!calFrom && key >= calFrom && key <= calTo;   // ISO keys sort as dates
}

function setCalRange(a, b) {
  if (!a) { calFrom = calTo = null; return; }
  calFrom = a <= b ? a : b;
  calTo = a <= b ? b : a;
}

// Click picks one day, drag picks a span. The whole gesture is previewed by
// re-rendering the strip, and only committed to the session filter on mouseup
// -- dragging across 40 days should not re-filter the list 40 times.
function bindCalendarDrag() {
  const grid = $("calGrid");
  // The event target during a drag is whatever is under the pointer (we set
  // no pointer capture), so no hit-testing by coordinates is needed.
  const dayAt = (ev) => {
    const cell = ev.target && ev.target.closest
      && ev.target.closest(".cal-cell:not(.future)");
    return cell ? cell.getAttribute("data-day") : null;
  };

  grid.onmousedown = (ev) => {
    const cell = ev.target.closest(".cal-cell:not(.future)");
    if (!cell) return;
    ev.preventDefault();                       // no text selection while dragging
    const anchor = cell.getAttribute("data-day");
    const before = { from: calFrom, to: calTo };
    let dragged = false, last = anchor;

    const move = (e) => {
      const day = dayAt(e);
      if (!day || day === last) return;        // only re-render when it changes
      last = day;
      dragged = true;
      setCalRange(anchor, day);
      renderCalendar();
    };
    const up = (e) => {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
      const day = dayAt(e) || anchor;
      if (!dragged && before.from === anchor && before.to === anchor) {
        setCalRange(null);                     // click the same single day to clear
      } else {
        setCalRange(anchor, dragged ? day : anchor);
      }
      renderCalendar();
      applySessionFilter();
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  };
}

// ---- session search (local: the whole list is already in memory) --------
function sessionDay(s) {
  const t = Date.parse(s.created || "");
  return Number.isNaN(t) ? null : dayKey(new Date(t));
}

function sessionMatches(s) {
  if (calFrom) {
    const day = sessionDay(s);
    if (!day || !inCalRange(day)) return false;
  }
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

// "Today" / "Yesterday" / a weekday for the last week / else Month Year.
// Sessions are newest-first, so these read as a descending timeline.
function dayBucket(created) {
  const t = Date.parse(created || "");
  if (Number.isNaN(t)) return "Undated";
  const d = new Date(t);
  const today = new Date();
  const startOfDay = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate());
  const days = Math.round((startOfDay(today) - startOfDay(d)) / 86400000);
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return d.toLocaleString(undefined, { weekday: "long" });
  return d.toLocaleString(undefined, { month: "long", year: "numeric" });
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
  // A calendar day is a filter too, so an empty result explains itself
  // rather than looking like an empty archive.
  if (!shownSessions.length && (sessQuery || calFrom || isSessCfgNarrowed()) && sessions.length) {
    $("sessionList").innerHTML = `<div class="none">No sessions match.</div>`;
    return;
  }
  let lastBucket = null;
  $("sessionList").innerHTML = shownSessions.map((s, i) => {
    const bucket = dayBucket(s.created);
    const header = bucket === lastBucket ? ""
      : `<div class="day-head"><span>${escapeHtml(bucket)}</span></div>`;
    lastBucket = bucket;
    const held = s.held ? '<span class="tag-held">HELD</span>' : "";
    const prob = s.n_problems ? `<span class="tag-prob">${s.n_problems} ⚠</span>` : "";
    const sel = curSession && s.run_id === curSession.run_id ? "sel" : "";
    const line1 = `${escapeHtml(s.run_id)} <span class="desc">${escapeHtml(s.description)}</span>`;
    return header + `<div class="session ${sel}" data-i="${i}">
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
      const s = shownSessions[+el.dataset.i];
      // Cmd/Ctrl-click opens elsewhere rather than here, as a browser does.
      if (ev.metaKey || ev.ctrlKey) { openSessionInNewTab(s); return; }
      selectSession(s);
    };
    el.oncontextmenu = (ev) => {
      ev.preventDefault();
      showSessionMenu(ev.clientX, ev.clientY, shownSessions[+el.dataset.i]);
    };
  });
}

async function selectSession(s) {
  curSession = s;
  scheduleTabSave();
  selected = null; selectedIsSidecar = false; picked = []; pickAnchor = null;
  renderSessions();
  await reloadItems();
  updateDetails();
  await refreshSessionInfo();
}

// ---- collections and saved searches -------------------------------------
// A collection points at things instead of holding them, so opening one
// never leaves the archive's real layout -- it is a view, like search.
function setRailTab(tab, { keepLocation = false } = {}) {
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
  if (!keepLocation && tab !== "collections" && openCollection) {
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
      <span class="cname">${escapeHtml(c.name)}</span>
      <span class="ccount">${c.n_entries}</span>
    </div>`;
  if (open) {
    for (const kid of kids) html += collectionRowsHTML(kid, depth + 1, ancestors.concat([name]));
  }
  return html;
}

// The readable name: a free-form title when set, else the storable one.
// Files and names are kept in sync, so a collection is shown by its name;
// `title` survives as an optional one-line description.
function collLabel(name) {
  return name;
}

function setAllRail(open) {
  for (const c of collections) {
    if ((c.children || []).length) collExpanded[c.name] = open;
  }
  LS.set("nebula.collOpen", collExpanded);
  renderCollections();
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
  const hasKids = collections.some((c) => (c.children || []).length);
  $("collTreeTools").innerHTML = hasKids
    ? `<span class="open-link" id="railExpandAll">expand all</span>
       <span class="open-link" id="railCollapseAll">collapse all</span>` : "";
  if (hasKids) {
    $("railExpandAll").onclick = () => setAllRail(true);
    $("railCollapseAll").onclick = () => setAllRail(false);
  }

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
  scheduleTabSave();
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
  const head = nested ? "" : `${crumbs}<div class="ctree-head" data-drop-collection="${escapeHtml(node.name)}">
      <span class="t">${escapeHtml(node.name)}</span>
      ${node.title || node.description
        ? `<span class="d">${escapeHtml(node.title || node.description)}</span>` : ""}
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
    return `<div class="crow ${cls} ${go ? "go" : ""}" ${attrs}${dropAttr}
        data-dragref="${escapeHtml(e.ref)}" data-owner="${escapeHtml(node.name)}">
        ${tw}
        <span class="ckind">${escapeHtml(kindLabel)}</span>
        <span class="cref">${escapeHtml(shown)}</span>
        ${e.note ? `<span class="cnote">${escapeHtml(e.note)}</span>` : ""}
        ${e.note_error ? `<span class="cbad">${escapeHtml(e.note_error)}</span>` : ""}
        <span class="cx" data-remove="${escapeHtml(e.ref)}" title="Remove from this collection">✕</span>
      </div>` + (e.child && isOpen
        ? `<div class="cnest">${collectionHTML(e.child, true)}</div>` : "");
  }).join("");
  return `${head}<div class="ctree"${nested ? "" : ` data-drop-collection="${escapeHtml(node.name)}"`}>${rows}</div>`;
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
      // `from` is the collection the row lives in, which for a nested row
      // is the child -- not whatever is open at the top.
      ev.preventDefault();                       // no text selection while dragging
      const ref = el.getAttribute("data-dragref");
      const label = el.querySelector(".cref").textContent;
      startEntryDrag(ev, { ref, label, from: el.getAttribute("data-owner") || openCollection });
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
      // Sort by what the list actually shows, or an alphabetical sort reads
      // as out of order wherever a duplicate was renamed.
      d = a.name.localeCompare(b.name);
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
      <td><div class="namecell">${fileGlyph(it, 20)}<span class="fname">${escapeHtml(it.name)}</span>${dupBadge(it)}</div></td>
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
      ${fileGlyph(it, 54)}<span class="cname">${escapeHtml(it.name)}${dupBadge(it)}</span>
      ${searchMode ? `<span class="cell-sess">${escapeHtml(it.run_id)}</span>` : ""}</div>`).join("")}</div>`;
}
// The list shows the name that is actually on disk -- that is what you will
// find in Finder, and what a ref has to name -- while the panels lead with
// the name that was asked for. The badge carries the other half either way.
function dupBadge(it) {
  if (!it.is_duplicate) return "";
  const asked = it.original_name || it.display_name;
  return `<span class="dup" title="asked for ${escapeHtml(asked)} — nebula wrote it as ` +
    `${escapeHtml(it.name)} so it would not overwrite the earlier one">` +
    `${it.position} of ${it.total}</span>`;
}

function sameSel(idx, isSc) { return selected === shownItems[idx] && selectedIsSidecar === isSc; }
function isPicked(idx) { return picked.length > 1 && picked.includes(shownItems[idx]); }

function wireItems() {
  const area = $("itemArea");
  // Dragging from empty space sweeps a marquee; dragging from a row moves
  // files. Deciding by where the gesture *starts* keeps them from competing.
  area.onpointerdown = (ev) => {
    if (ev.target.closest("[data-i]")) return;
    if (ev.target.closest("button, input, a, .crow, .ctree")) return;
    startMarquee(ev);
  };
  area.querySelectorAll("[data-i]").forEach((el) => {
    const it = shownItems[+el.dataset.i];
    const isSc = el.dataset.sc === "1";
    el.onpointerdown = (ev) => {
      if (ev.button !== 0 || isSc) return;
      // Drag whatever is highlighted; dragging an unselected row selects it
      // first, which is what every file manager does.
      if (!picked.includes(it)) selectItem(it, isSc, ev);
      startItemDrag(ev);
    };
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
// ---- keyboard navigation in the file list ------------------------------
// The list behaves like a file manager's: arrows move the selection, Shift
// extends it, Home/End/PageUp/PageDown jump. Without this the only way to
// walk a session is the mouse, and Cmd-A selected the window's text.
function moveSelection(delta, { extend = false, to = null } = {}) {
  if (!shownItems.length) return false;
  const cur = selected ? shownItems.indexOf(selected) : -1;
  let next;
  if (to === "start") next = 0;
  else if (to === "end") next = shownItems.length - 1;
  else if (cur < 0) next = delta > 0 ? 0 : shownItems.length - 1;
  else next = cur + delta;
  next = Math.max(0, Math.min(shownItems.length - 1, next));
  const it = shownItems[next];
  if (!it) return false;

  if (extend && pickAnchor && shownItems.includes(pickAnchor)) {
    const a = shownItems.indexOf(pickAnchor);
    picked = shownItems.slice(Math.min(a, next), Math.max(a, next) + 1);
  } else {
    picked = [it];
    pickAnchor = it;
  }
  selected = it;
  selectedIsSidecar = false;
  renderItemArea();
  updateDetails();
  revealSelected();
  if (showSc && selected.has_sidecar && picked.length === 1) openSidecarPanel(selected);
  if (showSess && selected.session_path) refreshSessionInfo();
  return true;
}

function revealSelected() {
  const row = document.querySelector("#itemArea .sel");
  if (row && row.scrollIntoView) row.scrollIntoView({ block: "nearest" });
}

function selectAllItems() {
  if (!shownItems.length) return false;
  picked = shownItems.slice();
  pickAnchor = picked[0];
  selected = selected && picked.includes(selected) ? selected : picked[0];
  renderItemArea();
  updateDetails();
  return true;
}

// How many rows a PageUp/PageDown covers, from the actual list height --
// a fixed guess would be wrong in grid view and at any other window size.
function pageStep() {
  const area = $("itemArea");
  const row = area.querySelector("tbody tr, .cell");
  if (!row) return 10;
  const h = row.getBoundingClientRect().height || 24;
  return Math.max(1, Math.floor((area.clientHeight || 400) / h) - 1);
}

function handleListKey(e) {
  // Only when the file list is what the user is working in.
  const tab = activeTabObj();
  if (tab && tab.kind !== "browse") return false;
  // e.target may be the document itself, which has no closest().
  const el = e.target;
  if (el && el.closest && el.closest("input, textarea, select")) return false;
  if (!shownItems.length) return false;

  if (hasMod(e) && (e.key || "").toLowerCase() === "a" && !e.shiftKey) {
    return selectAllItems();
  }
  const extend = e.shiftKey;
  switch (e.key) {
    case "ArrowDown": return moveSelection(1, { extend });
    case "ArrowUp": return moveSelection(-1, { extend });
    case "ArrowRight": return listView ? false : moveSelection(1, { extend });
    case "ArrowLeft": return listView ? false : moveSelection(-1, { extend });
    case "Home": return moveSelection(0, { extend, to: "start" });
    case "End": return moveSelection(0, { extend, to: "end" });
    case "PageDown": return moveSelection(pageStep(), { extend });
    case "PageUp": return moveSelection(-pageStep(), { extend });
    default: return false;
  }
}

// ---- marquee (rubber-band) selection -----------------------------------
// Drag on empty space in the file list to sweep up rows. Starting on a row
// is a file drag instead (see startItemDrag), so the two never compete.
let marquee = null;

function startMarquee(ev) {
  if (ev.button !== 0 || !shownItems.length) return;
  const area = $("itemArea");
  const additive = ev.shiftKey || ev.metaKey || ev.ctrlKey;
  // Clear and re-render *before* the box exists: renderItemArea rewrites
  // the list's innerHTML, which would otherwise delete the box we just
  // appended and leave the drag with nothing to draw.
  if (!additive) { picked = []; selected = null; renderItemArea(); }
  const box = document.createElement("div");
  box.className = "marquee";
  area.appendChild(box);
  marquee = { x0: ev.clientX, y0: ev.clientY, box, base: additive ? picked.slice() : [] };
  document.body.classList.add("marquee-on");

  const move = (e) => {
    if (!marquee) return;
    const x = Math.min(marquee.x0, e.clientX), y = Math.min(marquee.y0, e.clientY);
    const w = Math.abs(e.clientX - marquee.x0), h = Math.abs(e.clientY - marquee.y0);
    const r = area.getBoundingClientRect();
    Object.assign(marquee.box.style, {
      left: `${x - r.left + area.scrollLeft}px`, top: `${y - r.top + area.scrollTop}px`,
      width: `${w}px`, height: `${h}px`,
    });
    const hit = itemsIntersecting({ left: x, top: y, right: x + w, bottom: y + h });
    picked = marquee.base.concat(hit.filter((it) => !marquee.base.includes(it)));
    selected = picked[picked.length - 1] || null;
    paintSelection();
  };
  const up = () => {
    document.removeEventListener("pointermove", move);
    document.removeEventListener("pointerup", up);
    if (marquee) { marquee.box.remove(); marquee = null; }
    document.body.classList.remove("marquee-on");
    renderItemArea();
    updateDetails();
  };
  document.addEventListener("pointermove", move);
  document.addEventListener("pointerup", up);
}

function itemsIntersecting(rect) {
  const out = [];
  document.querySelectorAll("#itemArea [data-i][data-sc='0'], #itemArea .cell[data-i]")
    .forEach((el) => {
      const r = el.getBoundingClientRect();
      if (r.right >= rect.left && r.left <= rect.right
          && r.bottom >= rect.top && r.top <= rect.bottom) {
        const it = shownItems[+el.getAttribute("data-i")];
        if (it && !out.includes(it)) out.push(it);
      }
    });
  return out;
}

// Repaint highlights without rebuilding the list: a marquee updates on
// every pointermove, and re-rendering there would fight the drag.
//
// Mirrors what listHTML/gridHTML do, rather than inventing a third
// convention: in the table the primary row is `sel` and the rest of a
// multi-selection is `multi`; in the grid every chosen cell is `sel`.
function paintSelection() {
  document.querySelectorAll("#itemArea [data-i]").forEach((el) => {
    const it = shownItems[+el.getAttribute("data-i")];
    const isSc = el.getAttribute("data-sc") === "1";
    const on = !!it && !isSc && picked.includes(it);
    if (el.classList.contains("cell")) {
      el.classList.toggle("sel", on);
      return;
    }
    el.classList.toggle("sel", on && it === selected);
    el.classList.toggle("multi", on && picked.length > 1);
  });
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
  // Lead with the name the script asked for -- that is the name in the code
  // that produced this -- and name the file it actually became beneath it.
  const asked = (selected && selected.original_name) || null;
  const onDisk = info.itemName || baseName(info.name);
  const head = `<div class="p-title">
      <div class="p-name">${escapeHtml(asked || onDisk)}</div>
      <span class="chip ${srcCls}" title="${escapeHtml(srcTitle)}">${escapeHtml(srcText)}</span>
    </div>`
    + (asked ? `<div class="p-sub">stored as <span class="mono">${escapeHtml(onDisk)}</span></div>` : "");

  const dup = selected && selected.is_duplicate
    ? row("Duplicate", `write ${selected.position} of ${selected.total}`
        + (selected.original_name
           ? ` — asked for <span class="mono">${escapeHtml(selected.original_name)}</span>,`
             + ` stored as <span class="mono">${escapeHtml(selected.name)}</span>`
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

// Hit-testing for a drag. Wrapped because elementFromPoint is not
// guaranteed to exist (it does not in jsdom), and a missing hit-test must
// degrade to "no target" rather than throwing mid-gesture and leaving a
// ghost stuck to the cursor.
function dropTargetAt(x, y) {
  if (typeof document.elementFromPoint !== "function") return null;
  const under = document.elementFromPoint(x, y);
  return (under && under.closest) ? under.closest("[data-drop-collection]") : null;
}

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
    const target = dropTargetAt(e.clientX, e.clientY);
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

// Dragging files out of the list and onto a collection -- in the rail tree,
// in the collection view, or in another window. Same pointer-based approach
// as the collection entries use: Tauri's own file-drop handler swallows
// HTML5 drag events, so this never uses them.
function startItemDrag(ev) {
  const refs = selectedRefs();
  if (!refs.length) return;
  const startX = ev.clientX, startY = ev.clientY;
  const label = picked.length === 1
    ? (selected.display_name || selected.name) : `${picked.length} files`;
  let state = { active: false, target: null, refs, label };

  const move = (e) => {
    if (!state.active) {
      if (Math.hypot(e.clientX - startX, e.clientY - startY) < 5) return;
      state.active = true;
      const ghost = document.createElement("div");
      ghost.className = "drag-ghost";
      ghost.textContent = label;
      document.body.appendChild(ghost);
      state.ghost = ghost;
      document.body.classList.add("dragging-entry");
    }
    state.ghost.style.left = `${e.clientX + 12}px`;
    state.ghost.style.top = `${e.clientY + 12}px`;
    document.querySelectorAll(".drop-target").forEach((n) => n.classList.remove("drop-target"));
    const target = dropTargetAt(e.clientX, e.clientY);
    state.target = target ? target.getAttribute("data-drop-collection") : null;
    if (target) target.classList.add("drop-target");
  };

  const up = async (e) => {
    document.removeEventListener("pointermove", move);
    document.removeEventListener("pointerup", up);
    document.querySelectorAll(".drop-target").forEach((n) => n.classList.remove("drop-target"));
    document.body.classList.remove("dragging-entry");
    if (state.ghost) state.ghost.remove();
    if (!state.active) return;
    if (state.target) { await addRefsToCollection(state.target, state.refs, state.label); return; }
    // Dropped outside this window: hand it to whichever window is there.
    await dropOnOtherWindow(e, { kind: "refs", refs: state.refs, label: state.label });
  };

  document.addEventListener("pointermove", move);
  document.addEventListener("pointerup", up);
}

async function addRefsToCollection(name, refs, label) {
  try {
    await call("collection_add", { archive, name, refs, create: false, note: "" });
    noteRecentCollection(name);
    toast(`Added ${label || `${refs.length} item(s)`} to ${name}`);
    await loadCollections();
    if (openCollection) await showCollection(openCollection, { push: false });
  } catch (e) {
    toast(`${e}`);
  }
}

// ---- recently used collections -----------------------------------------
// The whole point of the right-click shortcuts: filing many files into one
// collection should not mean re-picking it from a dialog every time.
const RECENT_MAX = 5;
let recentColls = [];

function noteRecentCollection(name) {
  if (!name) return;
  recentColls = [name].concat(recentColls.filter((n) => n !== name)).slice(0, RECENT_MAX);
  LS.set("nebula.recentColls", recentColls);
}

// The two shortcuts that make filing many files bearable: the last
// collection used, by name, plus a submenu of the recent few.
function collectionShortcuts(getRefs, label) {
  const recent = knownRecentCollections();
  if (!recent.length) return [];
  const out = [{
    label: `Add to ${recent[0]}`,
    action: () => addRefsToCollection(recent[0], getRefs(), label),
  }];
  if (recent.length > 1) {
    out.push({
      label: "Add to",
      submenu: recent.map((name) => ({
        label: name,
        action: () => addRefsToCollection(name, getRefs(), label),
      })),
    });
  }
  return out;
}

function knownRecentCollections() {
  const known = new Set(collections.map((c) => c.name));
  return recentColls.filter((n) => known.has(n));
}

// ---- context menus ------------------------------------------------------
// The webview's own menu is useless here, so right-click gets a real one.
let fileManagerName = "Finder";

function showMenu(x, y, entries) {
  const el = $("ctxMenu");
  el.innerHTML = entries.map((e, i) => {
    if (e.separator) return `<div class="sep"></div>`;
    if (e.head) return `<div class="head">${escapeHtml(e.head)}</div>`;
    if (e.submenu) {
      // Rendered inline as a real nested list rather than a hover-out
      // flyout: it survives a narrow window, and it is reachable by
      // keyboard and by a jsdom test.
      return `<div class="sub ${e.disabled ? "off" : ""}" data-sub="${i}">
          <button class="sub-h" ${e.disabled ? "disabled" : ""}>${escapeHtml(e.label)}
            <span class="chev">›</span></button>
          <div class="sub-items hidden">${e.submenu.map((c, j) =>
            `<button data-sub-item="${i}.${j}">${escapeHtml(c.label)}</button>`).join("")}</div>
        </div>`;
    }
    return `<button ${e.disabled ? "disabled" : ""} class="${e.danger ? "danger" : ""}">`
      + `${escapeHtml(e.label)}</button>`;
  }).join("");
  el.classList.remove("hidden");

  // Place it at the cursor, flipped where it would fall off screen.
  const r = el.getBoundingClientRect();
  el.style.left = `${Math.max(4, Math.min(x, window.innerWidth - r.width - 6))}px`;
  el.style.top = `${Math.max(4, Math.min(y, window.innerHeight - r.height - 6))}px`;

  const run = (fn) => {
    closeMenu();
    if (fn) Promise.resolve(fn()).catch((err) => toast(`${err}`));
  };

  el.querySelectorAll(".sub-h").forEach((btn) => {
    btn.onclick = (ev) => {
      ev.stopPropagation();
      const wrap = btn.closest(".sub");
      const items = wrap.querySelector(".sub-items");
      const opening = items.classList.contains("hidden");
      el.querySelectorAll(".sub-items").forEach((n) => n.classList.add("hidden"));
      items.classList.toggle("hidden", !opening);
    };
  });
  el.querySelectorAll("[data-sub-item]").forEach((btn) => {
    const [i, j] = btn.getAttribute("data-sub-item").split(".").map(Number);
    btn.onclick = (ev) => { ev.stopPropagation(); run(entries[i].submenu[j].action); };
  });

  // Plain rows only: submenu buttons carry their own handlers above.
  const plain = [...el.querySelectorAll(":scope > button")];
  const actionable = entries.filter((e) => !e.separator && !e.head && !e.submenu);
  plain.forEach((btn, i) => {
    btn.onclick = () => run((actionable[i] || {}).action);
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
    ...collectionShortcuts(() => selectedRefs(), label),
    { label: "Export as a fragment…", disabled: !selectedRefs().length,
      action: () => exportSelection({ refs: selectedRefs(), label }) },
    { label: "Show relations", disabled: many || !it,
      action: () => openRelationsTab(it && it.run_id ? it.run_id
                                     : (curSession && curSession.run_id), it && it.name) },
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

function showSessionMenu(x, y, s) {
  showMenu(x, y, [
    { head: `${s.run_id}${s.description ? " — " + s.description : ""}` },
    { label: "Open", action: () => selectSession(s) },
    { label: "Open in new tab", action: () => openSessionInNewTab(s) },
    { label: "Show relations", action: () => openRelationsTab(s.run_id, null) },
    { label: "Show in index", action: () => openIndexTab(s.run_id) },
    { label: "Export as a fragment…",
      action: () => exportSelection({ sessions: [s.run_id], label: s.run_id }) },
    { separator: true },
    { label: `Reveal in ${fileManagerName}`, action: () => call("reveal_path", { path: s.path }) },
  ]);
}


function showCollectionMenu(x, y, name) {
  showMenu(x, y, [
    { head: collLabel(name) },
    { label: "New folder inside…", action: () => newNestedCollection(name) },
    { label: "Open", action: () => showCollection(name) },
    { label: "Open in new tab", action: () => openCollectionInNewTab(name) },
    { label: "Export as a fragment…",
      action: () => exportSelection({ collection: name, label: name }) },
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
      { label: "Open in new tab", action: () => openCollectionInNewTab(entry.target) },
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
  const name = await promptName(label, "");
  if (!name) return;
  try {
    await call("create_collection", { archive, name });
    if (parent) {
      await call("collection_add", { archive, name: parent, refs: [`collections/${name}`] });
    }
    await loadCollections();
    await showCollection(parent || name, { push: !parent });
    toast(parent ? `Created ${name} inside ${parent}` : `Created ${name}`);
  } catch (e) {
    toast(`${e}`);
  }
}

// Renaming changes the real name -- the filename and every
// "collections/<old>" ref that pointed at it. A collection is a scratch
// organisational tool; keeping an old name around would only confuse.
async function renameCollection(name) {
  const next = await promptName("Rename collection", name);
  if (!next || next === name) return;
  try {
    await call("rename_collection", { archive, name, new: next });
    if (openCollection === name) openCollection = next;
    collPath = collPath.map((n) => (n === name ? next : n));
    if (collExpanded[name] !== undefined) {
      collExpanded[next] = collExpanded[name];
      delete collExpanded[name];
      LS.set("nebula.collOpen", collExpanded);
    }
    await loadCollections();
    if (openCollection) await showCollection(openCollection, { push: false });
    toast(`Renamed to ${next}`);
  } catch (e) {
    toast(`${e}`);       // e.g. a ':' in the name, or the name is taken
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
  // A tree, not a dropdown: collections nest, and a flat list of names
  // cannot show that "figures" is inside "paper-2026".
  try {
    const ov = await call("collections_overview", { archive });
    collTree = { roots: ov.roots, byName: {} };
    for (const c of ov.collections) collTree.byName[c.name] = c;
  } catch (e) { /* fall back to whatever the tree already holds */ }
  const none = collections.length === 0;
  pickChoice = none ? null : (knownRecentCollections()[0] || collTree.roots[0] || null);
  pickExpanded = Object.assign({}, collExpanded);
  for (const name of pickPathTo(pickChoice)) pickExpanded[name] = true;
  renderPickTree();
  $("collNew").checked = none;             // nothing to add to yet
  $("collPick").classList.toggle("off", none);
  $("collNewFields").style.display = none ? "flex" : "none";
  $("collNewName").value = "";
  $("collNote").value = "";
  $("collScrim").classList.add("show");
}

let pendingCollRef = null;
let pickChoice = null, pickExpanded = {};

function pickPathTo(name) {
  // Ancestors of a collection, so the picker opens with it revealed.
  const path = [];
  const walk = (node, trail) => {
    if (node === name) { path.push(...trail); return true; }
    for (const kid of (collTree.byName[node] || {}).children || []) {
      if (walk(kid, trail.concat([node]))) return true;
    }
    return false;
  };
  for (const root of collTree.roots || []) if (walk(root, [])) break;
  return path;
}

function pickRowsHTML(name, depth, seen) {
  const c = collTree.byName[name];
  if (!c || seen.includes(name)) return "";
  const kids = c.children || [];
  const open = !!pickExpanded[name];
  const recent = knownRecentCollections().includes(name);
  const twisty = kids.length
    ? `<span class="tw" data-pick-toggle="${escapeHtml(name)}">`
      + `<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path d="${
          open ? "M3.5 6 L8 10.5 L12.5 6" : "M6 3.5 L10.5 8 L6 12.5"}" fill="none"
          stroke="currentColor" stroke-width="2" stroke-linecap="round"
          stroke-linejoin="round"/></svg></span>`
    : `<span class="tw empty"></span>`;
  let html = `<div class="pick-row ${name === pickChoice ? "on" : ""}"
        data-pick="${escapeHtml(name)}" role="option"
        style="padding-left:${8 + depth * 14}px">
      ${twisty}<span class="pname">${escapeHtml(name)}</span>
      ${recent ? `<span class="recent">recent</span>` : ""}
    </div>`;
  if (open) for (const kid of kids) html += pickRowsHTML(kid, depth + 1, seen.concat([name]));
  return html;
}

function renderPickTree() {
  const el = $("collPick");
  if (!(collTree.roots || []).length) {
    el.innerHTML = `<div class="pick-empty">No collections yet — create one below.</div>`;
    return;
  }
  el.innerHTML = (collTree.roots || []).map((n) => pickRowsHTML(n, 0, [])).join("");
  el.querySelectorAll("[data-pick-toggle]").forEach((n) => {
    n.onclick = (ev) => {
      ev.stopPropagation();
      const name = n.getAttribute("data-pick-toggle");
      pickExpanded[name] = !pickExpanded[name];
      renderPickTree();
    };
  });
  el.querySelectorAll("[data-pick]").forEach((n) => {
    n.onclick = () => {
      pickChoice = n.getAttribute("data-pick");
      $("collNew").checked = false;
      syncCollMode();
      renderPickTree();
    };
    n.ondblclick = () => { pickChoice = n.getAttribute("data-pick"); doAddToCollection(); };
  });
}

function syncCollMode() {
  const makeNew = $("collNew").checked;
  $("collPick").classList.toggle("off", makeNew);
  $("collNewFields").style.display = makeNew ? "flex" : "none";
}

async function doAddToCollection() {
  if (!pendingCollRef) return;
  const makeNew = $("collNew").checked;
  const name = makeNew ? $("collNewName").value.trim() : pickChoice;
  if (!name) { toast(makeNew ? "Name the collection first." : "Pick a collection."); return; }
  try {
    await call("collection_add", {
      archive, name, refs: pendingCollRef.refs, create: makeNew,
      note: $("collNote").value.trim(),
    });
    noteRecentCollection(name);
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

  const pend = idx.pending || {};
  // Staleness is measured by session signatures, so say what actually
  // differs rather than quoting a session count that may well match.
  const pendBits = [
    pend.added ? `${pend.added} new` : "",
    pend.updated ? `${pend.updated} changed` : "",
    pend.removed ? `${pend.removed} gone` : "",
  ].filter(Boolean).join(", ");
  const sealed = idx.sealed_years || [];
  const index = `<div class="mg"><div class="mg-h">Index <span class="issue-w">rebuildable cache</span></div>` +
    (idx.exists
      ? row("Last built", fmtCreated(idx.built)) +
        row("Sessions indexed", idx.sessions === null ? "unreadable" : String(idx.sessions)) +
        (sealed.length ? row("Sealed years",
                             sealed.map((y) => `${y.year} (${y.sessions})`).join(", ")) : "") +
        (idx.usable === false
          ? noteBox("info", "This index was written by a different version of nebula; "
              + "it will be rebuilt automatically the next time anything reads it.")
          : idx.stale
            ? noteBox("info", "Sessions on disk have moved on since this was built"
                + (pendBits ? ` — ${pendBits}` : "") + ". Reads bring it up to date "
                + "automatically; rebuild only if you want it done now.")
            : "")
      : noteBox("info", "No index yet. `nebula ls` and the CLI's graph queries build one on "
          + "demand; the Navigator reads the filesystem directly and works without it.")) +
    `<div class="mg-actions"><button class="dbtn ghost" id="arcRebuild">Rebuild index</button>`
    + `<button class="dbtn ghost" id="arcInspect">Inspect index…</button></div></div>`;

  const ident = (a.identity || {});
  const transfers = `<div class="mg"><div class="mg-h">Archives &amp; transfers`
    + `<span class="issue-w">${escapeHtml(ident.kind || "standard")}</span></div>`
    + row("This archive", `${escapeHtml(ident.name || a.label)}`
        + (ident.user ? ` — ${escapeHtml(ident.user)}` : ""))
    + (ident.locked
        ? noteBox("info", `Merged into ${escapeHtml(ident.merged_to || "another archive")} `
            + `on ${escapeHtml(fmtCreated(ident.merged_at))}. Writing is refused until it `
            + `is unlocked, so nothing written now can be mistaken for merged data.`)
        : "")
    + `<div class="mg-note">Merge brings an intake archive's sessions in under new `
    + `permanent ids. Adopt copies sessions out of a fragment someone sent you. `
    + `Export writes a fragment others can read, keeping these ids exactly.</div>`
    + `<div class="mg-actions">
         <button class="dbtn ghost" id="arcMerge">Merge intake…</button>
         <button class="dbtn ghost" id="arcAdopt">Adopt fragment…</button>
         <button class="dbtn ghost" id="arcExport">Export…</button>
       </div></div>`;

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
  body.innerHTML = staleWarn + overview + index + transfers + integrity + gc + settings;
  wirePaths(body);
  $("arcRebuild").onclick = rebuildIndex;
  $("arcInspect").onclick = () => {
    $("arcScrim").classList.remove("show");
    openIndexTab("");
  };
  $("arcMerge").onclick = () => { $("arcScrim").classList.remove("show"); startMerge(); };
  $("arcAdopt").onclick = () => { $("arcScrim").classList.remove("show"); startAdopt(); };
  $("arcExport").onclick = () => {
    $("arcScrim").classList.remove("show");
    exportSelection({ sessions: curSession ? [curSession.run_id] : null,
                      label: curSession ? curSession.run_id : "archive" });
  };
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
  const arrow = dir === "up" ? "←" : dir === "rel" ? "↔" : "→";
  const label = r.whole_session || !r.filename ? `${r.run_id}${r.whole_session ? " (whole session)" : ""}`
    : `${r.run_id}/${r.filename}`;
  const cls = r.resolved === false ? "unresolved" : r.exists ? "" : "missing";
  const note = r.note ? `<span class="ln-note">${escapeHtml(r.note)}</span>` : "";
  const clickable = r.exists && !!r.session_path;
  return `<div class="ln ${cls}${clickable ? " go" : ""}"
      ${clickable ? `data-goto-session="${escapeHtml(r.session_path)}" `
        + `data-goto-file="${escapeHtml(r.filename || "")}"` : ""}
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
  // filename may be empty: a related_run points at a whole session.
  const s = sessions.find((x) => x.path === sessionPath);
  if (!s) { toast("That session is not in the current archive view."); return; }
  if (!curSession || curSession.path !== sessionPath || searchMode) {
    searchMode = false; searchMeta = null;
    $("itemSearch").value = "";
    $("itemSearchClear").classList.add("hidden");
    await selectSession(s);
  }
  if (!filename) return;
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

  const indexed = indexStateHTML(info.index, info.run_id);

  const related = group("Related runs",
    (info.related_runs || []).length
      ? info.related_runs.map((r) => lineageRow(r, "rel")).join("")
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
    indexed + (history || noteBox("info", "No manual operations recorded."));
  wirePaths(body);
  const inspect = $("sessIndexOpen");
  if (inspect) inspect.onclick = () => openIndexTab(info.run_id);
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
  // NB: these ids must not collide with the rail's saved-search list
  // (#viewList) -- getElementById returns the first match in the document,
  // which silently wired this button to the wrong element.
  $("viewModeList").classList.toggle("on", list);
  $("viewModeGrid").classList.toggle("on", !list);
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

function treeDirChanged() {
  const tab = activeTabObj();
  if (!tab || tab.kind !== "tree") return;
  const up = $("treeUp").checked, down = $("treeDown").checked;
  // Turning both off would show an empty page and look broken; keep the
  // one being switched off from taking the other with it.
  if (!up && !down) { $("treeUp").checked = true; }
  tab.state.direction = !$("treeDown").checked ? "up"
    : !$("treeUp").checked ? "down" : "both";
  saveTabs();
  renderTreeTab(tab);
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
  "new-tab": () => addTab("browse"),
  "new-window": openNewWindow,
  "close-tab": () => closeTab(activeTab),
  "duplicate-tab": duplicateTab,
  "next-tab": () => cycleTab(1),
  "prev-tab": () => cycleTab(-1),
  relations: openRelationsForSelection,
  "index-view": () => openIndexTab(curSession ? curSession.run_id : ""),
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
  tauriEvent.listen("nebula://accept", (e) => acceptFromWindow(e.payload))
    .catch((e) => console.error("window hand-off listener failed", e));

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closePops(null); return; }
    // Selection keys first: Cmd-A here means "select every file", not
    // "select the window's text", and the arrows walk the list.
    if (handleListKey(e)) { e.preventDefault(); return; }
    // Ctrl-Tab cycles tabs on every platform (it is not a Cmd shortcut even
    // on macOS), so it is checked before the command-modifier gate.
    if (e.ctrlKey && e.key === "Tab") {
      e.preventDefault();
      cycleTab(e.shiftKey ? -1 : 1);
      return;
    }
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
    else if (k === "t" && !shift) name = "new-tab";
    else if (k === "n" && !shift) name = "new-window";
    else if (k === "w" && !shift) name = "close-tab";
    else if (k === "d" && !shift) name = "duplicate-tab";
    else if (k === "]" && shift) name = "next-tab";
    else if (k === "[" && shift) name = "prev-tab";
    else if (k === "l" && shift) name = "relations";
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
$("viewModeList").onclick = () => setView(true);
$("viewModeGrid").onclick = () => setView(false);
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
$("calToggle").onclick = () => {
  showCal = !showCal;
  LS.set("nebula.showCal", showCal);
  if (!showCal && calFrom) {
    // A filter you can no longer see is a trap: hiding the strip clears it.
    setCalRange(null);
    renderCalendar();
    applySessionFilter();
    return;
  }
  if (showCal && !activity) loadActivity(); else renderCalendar();
};
$("tabAdd").onclick = () => addTab("browse");
$("xferClose").onclick = () => $("xferScrim").classList.remove("show");
$("xferCancel").onclick = () => $("xferScrim").classList.remove("show");
$("xferOk").onclick = runTransfer;
$("xferForeign").onchange = () => {
  // Re-plan: whether a colleague's data is embedded changes the size, which
  // is the number this dialog exists to show before anything moves.
  if (xferState && xferState.op === "export") {
    openTransfer("export", xferState.args, { title: $("xferTitle").querySelector("span").textContent });
  }
};
$("xferScrim").onclick = (e) => { if (e.target === $("xferScrim")) $("xferScrim").classList.remove("show"); };
$("idxPrev").onclick = () => idxPageBy(-1);
$("idxNext").onclick = () => idxPageBy(1);
let idxSearchTimer = null;
$("idxSearch").oninput = () => {
  clearTimeout(idxSearchTimer);
  idxSearchTimer = setTimeout(() => {
    const tab = activeTabObj();
    if (!tab || tab.kind !== "index") return;
    tab.state.query = $("idxSearch").value.trim();
    tab.state.offset = 0;
    saveTabs();
    renderIndexTab(tab);
  }, 250);
};
$("idxSweep").onclick = async () => {
  const tab = activeTabObj();
  if (!tab || tab.kind !== "index") return;
  try {
    const res = await call("index_sweep", { archive });
    const s = res.swept || {};
    toast(s.rebuilt
      ? `Rebuilt: ${s.reason || "index was unusable"}`
      : `Swept ${s.checked_sessions || 0} session(s) — ${s.added || 0} added, `
        + `${s.updated || 0} updated, ${s.removed || 0} removed`
        + ((s.skipped_years || []).length ? `, ${s.skipped_years.join(", ")} skipped (sealed)` : ""));
    await renderIndexTab(tab);
    await refreshSessionInfo();
  } catch (e) { toast(`Sweep failed: ${e}`); }
};
$("idxRebuild").onclick = async () => {
  const tab = activeTabObj();
  if (!tab || tab.kind !== "index") return;
  try {
    await call("rebuild_index", { archive });
    toast("Index rebuilt");
    await renderIndexTab(tab);
    await refreshSessionInfo();
  } catch (e) { toast(`Rebuild failed: ${e}`); }
};
$("treeUp").onchange = treeDirChanged;
$("treeDown").onchange = treeDirChanged;
$("treeDepth").onchange = () => {
  const tab = activeTabObj();
  if (!tab || tab.kind !== "tree") return;
  tab.state.depth = +$("treeDepth").value || 3;
  saveTabs();
  renderTreeTab(tab);
};
$("treeReload").onclick = () => {
  const tab = activeTabObj();
  if (tab && tab.kind === "tree") renderTreeTab(tab);
};
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
  watchCalendarWidth();
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

  recentColls = LS.get("nebula.recentColls", []);
  showCal = LS.get("nebula.showCal", false);
  setRailTab(LS.get("nebula.railTab", "sessions"));
  restoreTabs();
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
    try {
      await loadArchive(start);
      // Only now is there an archive for a saved tab to point into, so the
      // active tab restores itself here rather than at restoreTabs().
      const tab = activeTabObj();
      if (tab && tab.kind === "tree") await renderTreeTab(tab);
      else if (tab && tab.kind === "index") await renderIndexTab(tab);
      else if (tab && tab.state && (tab.state.sessionRun || tab.state.collection
                                    || tab.state.searchMode)) {
        await restoreBrowse(tab.state);
      }
    } catch (e) { toast(`Startup error: ${e}`); }
  }
}

boot();
