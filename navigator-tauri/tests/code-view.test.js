// jsdom harness for the Navigator's captured-source viewer ("View files…").
//
//     cd navigator-tauri && npm install && npm test
//
// Same shape as uri-ui.test.js: stub the Tauri globals, load index.html for
// the DOM, run main.js as a script in that window, then call what it
// defined. What is worth testing here is that the tree says which file was
// the *entry point* and which files the store can no longer produce -- both
// are claims about provenance, and a viewer that gets them wrong is worse
// than no viewer at all.

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

const CODE = "a".repeat(64);
const INFO = {
  ok: true, id: CODE, short: CODE.slice(0, 12), entry: "repo/run.py",
  n_files: 4, n_blobs: 4, blobs_present: 3, repos: { repo: 3, lib: 1 },
  shared: 1, unique: 3, error: null,
  missing: ["repo/src/gone.py"],
  files: {
    "repo/run.py": "b1", "repo/src/helper.py": "b2",
    "repo/src/gone.py": "b3", "lib/util.py": "b4",
  },
};
const FILES = {
  "repo/run.py": { ok: true, path: "repo/run.py", blob: "b1", size: 21,
                   binary: false, truncated: false, text: "import helper\nhelper.go()\n",
                   error: null },
  "repo/src/helper.py": { ok: true, path: "repo/src/helper.py", blob: "b2", size: 9,
                          binary: false, truncated: false, text: "VALUE = 1", error: null },
  "lib/util.py": { ok: true, path: "lib/util.py", blob: "b4", size: 4000,
                   binary: true, truncated: false, text: null, error: null },
};

async function main() {
  const html = fs.readFileSync(path.join(SRC, "index.html"), "utf8");
  const dom = new JSDOM(html, { runScripts: "outside-only", pretendToBeVisual: true,
                               url: "http://localhost/" });
  const win = dom.window;

  const calls = [];
  const copied = [];
  // main.js grabs __TAURI__.core.invoke once at load, so a test that wants a
  // different answer changes what the stub returns, not the stub itself.
  let infoResponse = INFO;
  win.__TAURI__ = {
    core: { invoke: async (cmd, payload) => {
      calls.push(payload);
      if (payload.op === "code_info") return infoResponse;
      if (payload.op === "code_file") {
        const f = FILES[payload.args.path];
        if (!f) throw new Error(`no such file ${payload.args.path}`);
        return f;
      }
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
  const rows = () => [...$("codeTree").querySelectorAll(".cv-row")];
  const rowFor = (name) => rows().find((r) => r.querySelector(".n").textContent === name);

  console.log("dialog markup");
  ok($("codeScrim"), "#codeScrim exists");
  ok($("codeTree") && $("codeFileBody"), "tree and file panes exist");

  console.log("the sidecar offers it next to Restore");
  const group = win.codeHTML({ produced_by: { code: CODE }, codeInfo: INFO });
  ok(group.includes(`data-view-code="${CODE}"`), "codeHTML renders a View files button");
  ok(group.includes(`data-restore="${CODE}"`), "and still renders Restore files");

  console.log("viewCode opens on the entry point");
  run('archive = "postdoc"; scInfo = null;');
  await win.viewCode(CODE);
  ok($("codeScrim").classList.contains("show"), "dialog is shown");
  ok($("codeTitle").textContent.includes(CODE.slice(0, 12)), "titled by the snapshot");
  const info = calls.find((c) => c.op === "code_info");
  eq(info.args.code, CODE, "asked the backend for this snapshot");
  await new Promise((r) => setTimeout(r, 10));
  const read = calls.filter((c) => c.op === "code_file");
  eq(read.length, 1, "read exactly one file on open");
  eq(read[0].args.path, "repo/run.py", "and it is the entry point, not the first alphabetically");
  ok($("codeFileHead").textContent.includes("entry point"), "header says it is the entry point");

  console.log("the tree has shape, badges and line numbers");
  ok(rowFor("repo") && rowFor("lib"), "each repo is a top-level row");
  ok(rowFor("src"), "directories inside a repo are their own rows");
  ok(rowFor("run.py").querySelector(".cv-badge").textContent === "entry",
     "the entry point is badged in the tree");
  const goneRow = rowFor("gone.py");
  ok(goneRow.classList.contains("gone"), "a file whose bytes are missing is greyed");
  ok(!goneRow.getAttribute("data-cv-file"), "and is not selectable");
  eq($("codeFileBody").querySelector(".cv-nums").textContent, "1\n2",
     "two lines numbered for a two-line file with a trailing newline");

  console.log("selecting another file reads it");
  rowFor("helper.py").onclick();
  await new Promise((r) => setTimeout(r, 10));
  ok($("codeFileBody").textContent.includes("VALUE = 1"), "shows the file's text");
  ok(rowFor("helper.py").classList.contains("sel"), "the row is marked selected");
  ok(!rowFor("run.py").classList.contains("sel"), "and the previous one is not");

  console.log("no blob path is ever shown");
  const shown = $("codeTree").innerHTML + $("codeFileHead").innerHTML;
  ok(!shown.includes("b1") && !shown.includes("blobs"),
     "neither the tree nor the header leaks a location in the store");

  console.log("Copy copies the file that is open");
  await $("codeCopy").onclick();
  eq(copied[copied.length - 1], "VALUE = 1", "copied the selected file's text");

  console.log("a binary file says so instead of showing mojibake");
  rowFor("util.py").onclick();
  await new Promise((r) => setTimeout(r, 10));
  ok($("codeFileBody").textContent.includes("not text"), "explains why there is nothing to show");
  copied.length = 0;
  await $("codeCopy").onclick();
  eq(copied.length, 0, "and there is nothing to copy");

  console.log("directories collapse");
  rowFor("src").onclick();
  ok(!rowFor("helper.py"), "a collapsed directory hides its files");
  rowFor("src").onclick();
  ok(!!rowFor("helper.py"), "and re-expands");

  console.log("Escape closes it");
  win.document.dispatchEvent(new win.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  ok(!$("codeScrim").classList.contains("show"), "Escape closed it");

  console.log("a snapshot the archive no longer has does not open an empty dialog");
  const misses = [];
  run("toast = (m) => { window.__toasts.push(m); };");
  win.__toasts = misses;
  infoResponse = { ok: false, id: "b".repeat(64), error: "not in the archive's code store" };
  await win.viewCode("b".repeat(64));
  ok(!$("codeScrim").classList.contains("show"), "dialog stays shut");
  ok(misses.join(" ").includes("code store"), "and the reason is reported");

  console.log(`\n${checks - failures}/${checks} assertions passed`);
  process.exit(failures ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
