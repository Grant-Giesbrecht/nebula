// jsdom harness for window tabs vs. rail tabs.
//
//     cd navigator-tauri && npm install && npm test
//
// These are two different things that used to be tangled: the rail's four
// buttons choose what the *current* tab shows, and window tabs are made by
// the + button and Cmd-T. Assets was the exception -- clicking it conjured
// a window tab, and leaving it again landed in some other tab whose saved
// rail tab then won, so "Searches" arrived at "Sessions".

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

  win.__TAURI__ = {
    core: { invoke: async (cmd, payload) => {
      if (payload.op === "list_assets") return [];
      if (payload.op === "list_sessions") return [];
      if (payload.op === "list_items") return [];
      if (payload.op === "collections_overview") return { collections: [], roots: [] };
      if (payload.op === "list_views") return [];
      return {};
    } },
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
  const tick = () => new Promise((r) => setTimeout(r, 15));
  const nTabs = () => run("tabs.length");
  const kind = () => run("(activeTabObj() || {}).kind");
  const rail = () => run("railTab");
  run('archive = "postdoc"; tabs = [blankTab("browse")]; activeTab = tabs[0].id;');
  run("renderTabs(); applyTabChrome();");

  console.log("the new-tab button is always on screen");
  ok(!$("tabbar").classList.contains("hidden"), "the tab bar is visible with one tab");
  ok($("tabAdd"), "the + button exists");
  eq($("tabstrip").children.length, 0, "but a lone tab shows no chip");

  console.log("the rail's Assets button does not make a tab");
  eq(nTabs(), 1, "one tab to start");
  win.setRailTab("assets");
  await tick();
  eq(nTabs(), 1, "still one tab");
  eq(kind(), "assets", "the current tab is showing assets");
  eq(rail(), "assets", "and the rail agrees");

  console.log("leaving Assets goes where the click said, not to Sessions");
  win.setRailTab("views");
  await tick();
  eq(nTabs(), 1, "no tab was spawned on the way out");
  eq(kind(), "browse", "the tab is a browse tab again");
  eq(rail(), "views", "the rail is on Searches, which is what was clicked");
  ok(!$("viewPane").classList.contains("hidden"), "and the saved-searches pane is shown");
  ok($("assetPane").classList.contains("hidden"), "the asset pane is hidden");

  console.log("the same holds for every other rail button");
  for (const [from, to] of [["assets", "collections"], ["assets", "sessions"]]) {
    win.setRailTab(from);
    await tick();
    win.setRailTab(to);
    await tick();
    eq(rail(), to, `${from} → ${to} lands on ${to}`);
    eq(nTabs(), 1, `${from} → ${to} spawned no tab`);
  }

  console.log("a browse tab keeps its place across a trip through Assets");
  run(`curSession = { run_id: "S-26-0001", path: "/p/S-26-0001" };
       sessions = [curSession];`);
  win.setRailTab("assets");
  await tick();
  eq(run("(activeTabObj().back || {}).sessionRun"), "S-26-0001",
     "the browse location is remembered while assets is showing");
  win.setRailTab("sessions");
  await tick();
  eq(run("(activeTabObj().state || {}).sessionRun"), "S-26-0001",
     "and restored on the way back");

  console.log("+ and Cmd-T are what make tabs");
  $("tabAdd").onclick();
  await tick();
  eq(nTabs(), 2, "the + button makes one");
  ok($("tabstrip").children.length === 2, "and now the chips are shown");
  win.runAction("new-tab");
  await tick();
  eq(nTabs(), 3, "Cmd-T makes one");

  console.log("Cmd-3 reaches Assets without making a tab");
  win.runAction("tab-assets");
  await tick();
  eq(nTabs(), 3, "no fourth tab");
  eq(kind(), "assets", "the active tab shows assets");

  console.log(`\n${checks - failures}/${checks} assertions passed`);
  process.exit(failures ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
