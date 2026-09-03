// jsdom harness for the Navigator's "Copy URI" feature.
//
//     cd navigator-tauri && npm install && npm test
//
// main.js is written for a Tauri webview: it imports nothing, expects the
// __TAURI__ globals, and wires everything at load. So we stub those, load
// index.html for the DOM, run main.js as a script in that window, and then
// call the functions it defined.
//
// The front-end is otherwise untested, and this is the part of it worth
// testing: what these menus copy is a *permanent identifier*, and getting
// it wrong is not a visual bug that someone notices -- it is a wrong string
// in a paper.

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { JSDOM } = require("jsdom");

process.on("unhandledRejection", (e) => {
  console.log("unhandled rejection:", e && (e.stack || e.message || String(e)));
});

const SRC = path.resolve(__dirname, "..", "src");

let failures = 0, checks = 0;
function ok(cond, what) {
  checks++;
  if (cond) { console.log(`  ok  ${what}`); }
  else { failures++; console.log(`  FAIL ${what}`); }
}
function eq(got, want, what) { ok(got === want, `${what} (got ${JSON.stringify(got)})`); }

async function main() {
  const html = fs.readFileSync(path.join(SRC, "index.html"), "utf8");
  const dom = new JSDOM(html, { runScripts: "outside-only", pretendToBeVisual: true,
                               url: "http://localhost/" });
  const win = dom.window;

  // ---- stubs -----------------------------------------------------------
  const calls = [];
  let uriResponse = {
    ok: true, uri: "nebula://g@ncsu.edu/postdoc/S-26-0001/raw.csv",
    kind: "file", user: "g@ncsu.edu", archive: "postdoc",
    path: "/archives/postdoc/data/2026/S-26-0001/raw.csv", exists: true,
    unique: true, warnings: [], error: null, label: "S-26-0001/raw.csv",
  };
  const copied = [];
  let uriQueue = [];
  let resolveResponse = null;
  win.__TAURI__ = {
    core: { invoke: async (cmd, payload) => {
      // take_uris is a plain Rust command, not a bridge op: no payload.
      if (cmd === "take_uris") { const q = uriQueue; uriQueue = []; return q; }
      calls.push(payload);
      if (payload.op === "uri") return uriResponse;
      if (payload.op === "resolve_uri") return resolveResponse;
      if (payload.op === "file_manager_name") return { name: "Finder" };
      return {};
    } },
    event: { listen: async () => () => {} },
    window: { getCurrentWindow: () => ({ label: "main", listen: async () => {} }) },
    webviewWindow: { WebviewWindow: function () {} },
    dialog: {}, opener: {}, path: {},
  };
  Object.defineProperty(win.navigator, "clipboard", {
    value: { writeText: async (t) => { copied.push(t); } }, configurable: true,
  });
  win.matchMedia = win.matchMedia || (() => ({ matches: false, addListener() {}, addEventListener() {} }));

  // ---- load ------------------------------------------------------------
  // Run main.js as a *script*, not as eval code: its top-level `let`
  // bindings (archive, curSession, picked, ...) would otherwise be trapped
  // in the eval's own scope and unreachable from the test.
  const ctx = dom.getInternalVMContext();
  const run = (code) => new vm.Script(code).runInContext(ctx);
  const js = fs.readFileSync(path.join(SRC, "main.js"), "utf8");
  try {
    run(js);
  } catch (e) {
    console.log("main.js threw while loading:", e.message);
    process.exit(1);
  }
  await new Promise((r) => setTimeout(r, 30));

  const $ = (id) => win.document.getElementById(id);

  console.log("dialog markup");
  ok($("uriScrim"), "#uriScrim exists");
  ok($("uriText"), "#uriText exists");
  ok($("uriCopy"), "#uriCopy exists");

  console.log("copyUri copies straight to the clipboard, with no dialog");
  const toasts = [];
  win.__toasts = toasts;
  run("toast = (m) => { window.__toasts.push(m); };");
  run('archive = "postdoc"');
  await win.copyUri({ session: "S-26-0001", file: "raw.csv" }, "S-26-0001/raw.csv");
  const last = calls[calls.length - 1];
  eq(last.op, "uri", "calls the uri op");
  eq(last.args.archive, "postdoc", "passes the active archive");
  eq(last.args.file, "raw.csv", "passes the filename");
  eq(copied[copied.length - 1], uriResponse.uri, "the URI is on the clipboard");
  ok(!$("uriScrim").classList.contains("show"), "no dialog is opened");
  eq(toasts[toasts.length - 1], "URI copied", "and it says so");

  console.log("an unreachable clipboard falls back to the dialog");
  const realClipboard = win.navigator.clipboard.writeText;
  win.navigator.clipboard.writeText = async () => { throw new Error("denied"); };
  await win.copyUri({ session: "S-26-0001", file: "raw.csv" }, "S-26-0001/raw.csv");
  ok($("uriScrim").classList.contains("show"), "the dialog opens instead");
  eq($("uriText").value, uriResponse.uri, "with the URI in the box to select by hand");
  ok(toasts[toasts.length - 1].includes("Cmd/Ctrl-C"), "and says how to copy it");
  win.navigator.clipboard.writeText = realClipboard;

  console.log("Escape closes the dialog");
  win.document.dispatchEvent(new win.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  ok(!$("uriScrim").classList.contains("show"), "Escape closed it");

  console.log("a URI that cannot be minted still explains itself");
  uriResponse = {
    ok: false, uri: null, error: "no owner", archive: "scratch",
    warnings: ["this archive declares no owner, so the URI names nobody"],
  };
  await win.copyUri({ session: "S-26-0001" }, "S-26-0001");
  ok($("uriScrim").classList.contains("show"), "the dialog opens when nothing can be copied");
  eq($("uriText").value, "", "no URI in the box");
  ok(!$("uriWarn").classList.contains("hidden"), "the warning block is shown");
  ok($("uriWarn").textContent.includes("declares no owner"), "and says why");
  ok($("uriCopy").disabled, "Copy is disabled when there is nothing to copy");
  $("uriClose").onclick();

  console.log("a caveat on a URI that IS minted rides along with the copy");
  uriResponse = {
    ok: true, uri: "nebula://grant@local/postdoc/S-26-0001", kind: "session",
    user: "grant@local", archive: "postdoc", path: "/p", exists: true,
    unique: false, warnings: ["the owner 'grant@local' is a local name"],
    error: null, label: "S-26-0001",
  };
  copied.length = 0;
  await win.copyUri({ session: "S-26-0001" }, "S-26-0001");
  eq(copied[0], "nebula://grant@local/postdoc/S-26-0001", "it is copied, caveat and all");
  ok(!$("uriScrim").classList.contains("show"), "still no dialog");
  ok(toasts[toasts.length - 1].includes("local name"), "the caveat is in the toast");

  console.log("menus offer it");
  const menus = [];
  win.__menus = menus;
  run("showMenu = (x, y, entries) => { window.__menus.push(entries); };");
  run('curSession = { run_id: "S-26-0001", path: "/p" };'
         + 'selected = { name: "raw.csv" }; picked = [selected];');
  win.showItemMenu(0, 0);
  win.showSessionMenu(0, 0, { run_id: "S-26-0001", description: "d", path: "/p" });
  win.showCollectionMenu(0, 0, "paper-2026");
  win.showEntryMenu(0, 0, { ref: "S-26-0001/raw.csv", kind: "file", exists: true, path: "/p" });
  run('assetList = [{ id: "AF-26-0017", name: "cal.json" }];');
  win.assetContextMenu(0, 0, "AF-26-0017");
  const names = ["item", "session", "collection", "entry", "asset"];
  menus.forEach((entries, i) => {
    const hit = entries.find((e) => e.label && e.label.startsWith("Copy URI"));
    ok(!!hit, `${names[i]} menu has a Copy URI entry`);
    ok(hit && !hit.disabled, `${names[i]} menu's entry is enabled`);
  });

  console.log("multi-select copies a list");
  run('picked = [{ name: "a.csv" }, { name: "b.csv" }]; selected = picked[0];');
  menus.length = 0;
  win.showItemMenu(0, 0);
  const multi = menus[0].find((e) => e.label && e.label.startsWith("Copy URIs"));
  ok(!!multi, "label switches to Copy URIs (2)");
  uriResponse = { ok: true, uri: "nebula://g@ncsu.edu/postdoc/S-26-0001/x.csv", warnings: [] };
  copied.length = 0;
  await multi.action();
  eq(copied.length, 1, "one clipboard write");
  eq(copied[0].split("\n").length, 2, "two URIs, one per line");
  ok(!$("uriScrim").classList.contains("show"), "no dialog for a multi-selection");

  // ---- incoming links --------------------------------------------------
  console.log("a nebula:// link handed over by the OS is resolved and opened");
  const resolved = {
    ok: true, error: null, kind: "file", archive: "postdoc",
    archive_root: "/archives/postdoc", user: "g@ncsu.edu",
    run_id: "S-26-0001", filename: "raw.csv", collection: null, asset: null,
    path: "/archives/postdoc/data/2026/S-26-0001/raw.csv", exists: true,
  };
  uriQueue = ["nebula://g@ncsu.edu/postdoc~ab12/S-26-0001/raw.csv"];
  resolveResponse = resolved;
  const went = [];
  win.__went = went;
  run("gotoRunId = async (r, f) => { window.__went.push([r, f]); };");
  run('archive = "postdoc";');
  await win.drainUris();
  eq(calls[calls.length - 1].args.uri,
     "nebula://g@ncsu.edu/postdoc~ab12/S-26-0001/raw.csv", "the whole URI is handed to the backend");
  eq(JSON.stringify(went[0]), JSON.stringify(["S-26-0001", "raw.csv"]),
     "and it navigates to the artefact it names");
  ok(!$("uriScrim").classList.contains("show"), "no dialog for a link that works");

  console.log("a link to an archive this machine does not have explains itself");
  resolveResponse = { ok: false, error: "no archive 'theirs' owned by 'someone@else.edu' "
                      + "is registered on this machine.", archive_root: null };
  await win.openNebulaUri("nebula://someone@else.edu/theirs/S-26-0001/raw.csv");
  ok($("uriScrim").classList.contains("show"), "the dialog opens");
  ok($("uriWarn").textContent.includes("registered on this machine"),
     "carrying the backend's own explanation");
  eq($("uriText").value, "nebula://someone@else.edu/theirs/S-26-0001/raw.csv",
     "and the link itself, to copy or forward");
  $("uriClose").onclick();

  console.log("an asset link selects the asset and shows the browser");
  resolveResponse = Object.assign({}, resolved, {
    kind: "asset", run_id: null, filename: null, asset: "AF-26-0017", exists: true });
  run("setRailTab = (t) => { window.__rail = t; };");
  await win.openNebulaUri("nebula://g@ncsu.edu/postdoc/assets/AF-26-0017");
  eq(run("assetSel"), "AF-26-0017", "the asset is selected");
  eq(win.__rail, "assets", "and the rail moves to Assets");

  console.log(`\n${checks - failures}/${checks} assertions passed`);
  process.exit(failures ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
