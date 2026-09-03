// jsdom harness for the asset browser's two selections.
//
//     cd navigator-tauri && npm install && npm test
//
// Assets live in two lists -- the rail in the sidebar and the grid in the
// main area -- and both are the same asset. What is tested here is that
// they behave like it: the same context menu from either, and Cmd-O
// opening whichever thing the active tab is actually about.

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

  const calls = [];
  win.__TAURI__ = {
    core: { invoke: async (cmd, payload) => { calls.push(payload); return {}; } },
    event: { listen: async () => () => {} },
    window: { getCurrentWindow: () => ({ label: "main", listen: async () => {} }) },
    webviewWindow: { WebviewWindow: function () {} },
    dialog: {}, opener: {}, path: {},
  };
  Object.defineProperty(win.navigator, "clipboard", {
    value: { writeText: async () => {} }, configurable: true,
  });
  win.matchMedia = win.matchMedia || (() => ({ matches: false, addListener() {}, addEventListener() {} }));

  const ctx = dom.getInternalVMContext();
  const run = (code) => new vm.Script(code).runInContext(ctx);
  try {
    run(fs.readFileSync(path.join(SRC, "main.js"), "utf8"));
  } catch (e) {
    console.log("main.js threw while loading:", e.message);
    process.exit(1);
  }
  await new Promise((r) => setTimeout(r, 30));

  const $ = (id) => win.document.getElementById(id);
  const tick = () => new Promise((r) => setTimeout(r, 10));
  const toasts = [];
  win.__toasts = toasts;
  const menus = [];
  win.__menus = menus;
  run("toast = (m) => { window.__toasts.push(m); };");
  run("showMenu = (x, y, entries) => { window.__menus.push(entries); };");
  run(`archive = "postdoc";
       assetList = [{ id: "AF-26-0017", name: "cal.json", size_human: "2.1 KB",
                      policy_resolved: "manual", n_snapshots: 0 },
                    { id: "AF-26-0018", name: "fig.svg", size_human: "8.0 KB",
                      policy_resolved: "manual", n_snapshots: 1 }];
       railTab = "assets";`);

  console.log("the rail lists assets");
  await win.renderAssetRail();
  const rows = () => [...$("assetRailList").querySelectorAll(".arow")];
  eq(rows().length, 2, "both assets are in the sidebar");

  console.log("right-clicking one in the rail offers the grid's menu");
  const ev = { preventDefault() {}, clientX: 10, clientY: 20 };
  await rows()[1].oncontextmenu(ev);
  ok(menus.length === 1, "a menu was opened");
  const railLabels = menus[0].filter((e) => e.label).map((e) => e.label);
  ok(railLabels.includes("Open"), "it offers Open");
  ok(railLabels.includes("Save version…"), "it offers Save version");
  ok(railLabels.includes("Copy URI"), "it offers Copy URI");
  // main.js's top-level `let`s are global-lexical, not properties of the
  // window, so state is read by evaluating the name rather than off `win`.
  eq(run("assetSel"), "AF-26-0018", "and right-clicking selected that asset");

  // The grid's menu is the benchmark: same asset, same entries, same order.
  menus.length = 0;
  win.assetContextMenu(0, 0, "AF-26-0018");
  eq(JSON.stringify(menus[0].filter((e) => e.label).map((e) => e.label)),
     JSON.stringify(railLabels), "the rail menu matches the grid's exactly");

  console.log("double-clicking a rail row opens the asset");
  calls.length = 0;
  rows()[0].ondblclick();
  eq(calls[0].op, "asset_open", "opens it");
  eq(calls[0].args.asset_id, "AF-26-0017", "the one that was double-clicked");

  console.log("Cmd-O on the asset tab opens the selected asset");
  run(`tabs = [{ id: 1, kind: "assets", state: {} }]; activeTab = 1;
       assetSel = "AF-26-0018"; selected = null;`);
  calls.length = 0; toasts.length = 0;
  win.openSelectedExternally();
  eq(calls.length, 1, "it opened something");
  eq(calls[0].op, "asset_open", "and it is the asset op");
  eq(calls[0].args.asset_id, "AF-26-0018", "for the selected asset");
  eq(toasts.length, 0, "no 'select a file first' complaint");

  console.log("Cmd-O on a browse tab still opens the selected artefact");
  run(`tabs = [{ id: 2, kind: "browse", state: {} }]; activeTab = 2;
       selected = { name: "raw.csv", artifact_path: "/p/S-26-0001/raw.csv" };
       selectedIsSidecar = false;`);
  calls.length = 0;
  win.openSelectedExternally();
  eq(calls[0].op, "open_path", "the artefact wins on a browse tab");
  eq(calls[0].args.path, "/p/S-26-0001/raw.csv", "and it is the selected file");

  console.log("with nothing selected, the rail's asset is the fallback");
  run("selected = null;");
  calls.length = 0;
  win.openSelectedExternally();
  eq(calls[0].op, "asset_open", "falls back to the asset the sidebar has selected");

  console.log("and with no selection anywhere it says which one it wants");
  run('assetSel = null; selected = null; railTab = "assets";');
  calls.length = 0; toasts.length = 0;
  win.openSelectedExternally();
  eq(calls.length, 0, "nothing is opened");
  ok(toasts[0].includes("asset"), "the message names an asset, not a file");

  console.log(`\n${checks - failures}/${checks} assertions passed`);
  process.exit(failures ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
