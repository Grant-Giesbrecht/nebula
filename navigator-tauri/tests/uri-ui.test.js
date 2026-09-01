// jsdom harness for the Navigator's "Get URI" feature.
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
  win.__TAURI__ = {
    core: { invoke: async (cmd, payload) => {
      calls.push(payload);
      if (payload.op === "uri") return uriResponse;
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

  console.log("showUri populates and opens the dialog");
  run('archive = "postdoc"');
  await win.showUri({ session: "S-26-0001", file: "raw.csv" }, "S-26-0001/raw.csv");
  const last = calls[calls.length - 1];
  eq(last.op, "uri", "calls the uri op");
  eq(last.args.archive, "postdoc", "passes the active archive");
  eq(last.args.file, "raw.csv", "passes the filename");
  ok($("uriScrim").classList.contains("show"), "dialog is shown");
  eq($("uriText").value, uriResponse.uri, "URI is in the box");
  ok($("uriMeta").textContent.includes("/archives/postdoc"), "path is shown");
  ok($("uriWarn").classList.contains("hidden"), "no warning block for a clean URI");
  ok(!$("uriCopy").disabled, "Copy is enabled");

  console.log("Copy button");
  await $("uriCopy").onclick();
  eq(copied[copied.length - 1], uriResponse.uri, "copies the URI");
  ok(!$("uriScrim").classList.contains("show"), "closes after copying");

  console.log("Escape closes it");
  await win.showUri({ session: "S-26-0001" }, "S-26-0001");
  ok($("uriScrim").classList.contains("show"), "reopened");
  win.document.dispatchEvent(new win.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  ok(!$("uriScrim").classList.contains("show"), "Escape closed it");

  console.log("a URI that cannot be minted still explains itself");
  uriResponse = {
    ok: false, uri: null, error: "no owner", archive: "scratch",
    warnings: ["this archive declares no owner, so the URI names nobody"],
  };
  await win.showUri({ session: "S-26-0001" }, "S-26-0001");
  eq($("uriText").value, "", "no URI in the box");
  ok(!$("uriWarn").classList.contains("hidden"), "the warning block is shown");
  ok($("uriWarn").textContent.includes("declares no owner"), "and says why");
  ok($("uriCopy").disabled, "Copy is disabled when there is nothing to copy");
  $("uriClose").onclick();

  console.log("a warning on a URI that IS minted is shown alongside it");
  uriResponse = {
    ok: true, uri: "nebula://grant@local/postdoc/S-26-0001", kind: "session",
    user: "grant@local", archive: "postdoc", path: "/p", exists: true,
    unique: false, warnings: ["the owner 'grant@local' is a local name"],
    error: null, label: "S-26-0001",
  };
  await win.showUri({ session: "S-26-0001" }, "S-26-0001");
  eq($("uriText").value, "nebula://grant@local/postdoc/S-26-0001", "URI still offered");
  ok(!$("uriWarn").classList.contains("hidden"), "caveat shown with it");
  $("uriClose").onclick();

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
    const hit = entries.find((e) => e.label && e.label.startsWith("Get URI"));
    ok(!!hit, `${names[i]} menu has a Get URI entry`);
    ok(hit && !hit.disabled, `${names[i]} menu's entry is enabled`);
  });

  console.log("multi-select copies a list instead of opening the dialog");
  run('picked = [{ name: "a.csv" }, { name: "b.csv" }]; selected = picked[0];');
  menus.length = 0;
  win.showItemMenu(0, 0);
  const multi = menus[0].find((e) => e.label && e.label.startsWith("Get URIs"));
  ok(!!multi, "label switches to Get URIs (2)");
  uriResponse = { ok: true, uri: "nebula://g@ncsu.edu/postdoc/S-26-0001/x.csv", warnings: [] };
  copied.length = 0;
  await multi.action();
  eq(copied.length, 1, "one clipboard write");
  eq(copied[0].split("\n").length, 2, "two URIs, one per line");
  ok(!$("uriScrim").classList.contains("show"), "no dialog for a multi-selection");

  console.log(`\n${checks - failures}/${checks} assertions passed`);
  process.exit(failures ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
