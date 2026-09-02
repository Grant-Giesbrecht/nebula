// jsdom harness for the Navigator's code store browser ("Browse code store…").
//
//     cd navigator-tauri && npm install && npm test
//
// The claims worth testing are the ones a person would act on: that a
// file's versions are ordered newest-first, that the artefacts listed
// against a version really are the ones that ran it, that a version
// nothing references is called out (it is what gc deletes), and that a
// derived date is never presented as a recorded one.

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

const B = (c) => c.repeat(64);
const TREE = {
  n_repos: 2, n_paths: 3, n_versions: 4, n_snapshots: 3, n_blobs: 4, n_missing: 1,
  store_dir: "/archives/postdoc/code",
  repos: [
    { name: "lib", n_paths: 1, n_versions: 1, paths: [
      { path: "lib/util.py", name: "util.py", n_versions: 1, n_artefacts: 0, versions: [
        { blob: B("d"), short: B("d").slice(0, 12), size: 40, present: true, entry: false,
          snapshots: [B("3")], n_snapshots: 1, first_seen: null, artefacts: [], n_artefacts: 0 },
      ] },
    ] },
    { name: "repo", n_paths: 2, n_versions: 3, paths: [
      { path: "repo/helper.py", name: "helper.py", n_versions: 2, n_artefacts: 3, versions: [
        { blob: B("a"), short: B("a").slice(0, 12), size: 61, present: true, entry: false,
          snapshots: [B("2")], n_snapshots: 1, first_seen: "2026-08-19T09:00:00",
          n_artefacts: 1, artefacts: [
            { run_id: "S-26-0007", session_path: "/p/S-26-0007", filename: "fit.csv",
              created: "2026-08-19T09:00:00", trashed: false }] },
        { blob: B("b"), short: B("b").slice(0, 12), size: 60, present: true, entry: false,
          snapshots: [B("1")], n_snapshots: 1, first_seen: "2026-08-14T11:00:00",
          n_artefacts: 2, artefacts: [
            { run_id: "S-26-0003", session_path: "/p/S-26-0003", filename: "plot.png",
              created: "2026-08-14T11:00:00", trashed: false },
            { run_id: "S-26-0004", session_path: "/p/S-26-0004", filename: "old.csv",
              created: "2026-08-15T11:00:00", trashed: true }] },
      ] },
      { path: "repo/run.py", name: "run.py", n_versions: 1, n_artefacts: 1, versions: [
        { blob: B("c"), short: B("c").slice(0, 12), size: null, present: false, entry: true,
          snapshots: [B("1")], n_snapshots: 1, first_seen: "2026-08-14T11:00:00",
          n_artefacts: 1, artefacts: [
            { run_id: "S-26-0003", session_path: "/p/S-26-0003", filename: "plot.png",
              created: "2026-08-14T11:00:00", trashed: false }] },
      ] },
    ] },
  ],
};
const BLOBS = {
  [B("a")]: { ok: true, blob: B("a"), size: 61, binary: false, truncated: false,
              text: "VALUE = 99\n", error: null },
  [B("b")]: { ok: true, blob: B("b"), size: 60, binary: false, truncated: false,
              text: "VALUE = 1\n", error: null },
  [B("d")]: { ok: true, blob: B("d"), size: 40, binary: false, truncated: false,
              text: "def util():\n    pass\n", error: null },
};

async function main() {
  const html = fs.readFileSync(path.join(SRC, "index.html"), "utf8");
  const dom = new JSDOM(html, { runScripts: "outside-only", pretendToBeVisual: true,
                               url: "http://localhost/" });
  const win = dom.window;

  const calls = [];
  const copied = [];
  let treeResponse = TREE;
  win.__TAURI__ = {
    core: { invoke: async (cmd, payload) => {
      calls.push(payload);
      if (payload.op === "code_store_tree") {
        if (treeResponse instanceof Error) throw treeResponse;
        return treeResponse;
      }
      if (payload.op === "code_blob") return BLOBS[payload.args.blob];
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
  const rows = () => [...$("storeTree").querySelectorAll(".cv-row")];
  const labels = () => rows().map((r) => r.querySelector(".n").textContent);
  const rowFor = (name) => rows().find((r) => r.querySelector(".n").textContent === name);
  const tick = () => new Promise((r) => setTimeout(r, 10));

  console.log("dialog markup");
  ok($("codeStoreScrim"), "#codeStoreScrim exists");
  ok($("storeFilter") && $("storeUsers"), "filter box and artefact list exist");

  console.log("an archive must be open");
  const toasts = [];
  win.__toasts = toasts;
  run("toast = (m) => { window.__toasts.push(m); };");
  run("archive = null;");
  await win.openCodeStore();
  ok(!$("codeStoreScrim").classList.contains("show"), "no dialog without an archive");
  ok(toasts.join(" ").includes("Open an archive"), "and it says why");

  console.log("openCodeStore renders repos, collapsed to paths");
  run('archive = "postdoc";');
  await win.openCodeStore();
  ok($("codeStoreScrim").classList.contains("show"), "dialog is shown");
  eq(calls[calls.length - 1].op, "code_store_tree", "asked the backend for the tree");
  ok(labels().includes("lib") && labels().includes("repo"), "both repos are listed");
  ok(labels().includes("helper.py"), "paths are listed under their repo");
  ok(!labels().includes(B("a").slice(0, 12)), "versions stay folded until a path is opened");
  ok($("storeNote").textContent.includes("3 file(s)"), "the footer counts the store");
  ok($("storeNote").textContent.includes("missing"), "and mentions the missing bytes");

  console.log("a path opens into its versions, newest first");
  rowFor("helper.py").onclick();
  const vers = labels().filter((l) => l.length === 12);
  eq(vers[0], B("a").slice(0, 12), "the 2026/08/19 version is listed first");
  eq(vers[1], B("b").slice(0, 12), "the 2026/08/14 version second");
  ok(rowFor(B("a").slice(0, 12)).textContent.includes("2026/08/19"), "each version shows its date");

  console.log("selecting a version reads exactly that blob");
  rowFor(B("b").slice(0, 12)).onclick();
  await tick();
  const read = calls[calls.length - 1];
  eq(read.op, "code_blob", "reads by digest, not by snapshot");
  eq(read.args.blob, B("b"), "and it is the version that was clicked");
  ok($("storeFileBody").textContent.includes("VALUE = 1"), "shows that version's bytes");
  ok($("storeFileHead").textContent.includes("repo/helper.py"), "header names the path");

  console.log("the artefacts that ran it are listed, and a date is called derived");
  const users = [...$("storeUsers").querySelectorAll(".u")];
  eq(users.length, 2, "both artefacts are listed");
  ok(users[0].textContent.includes("S-26-0003/plot.png"), "named run/file");
  ok(users[1].textContent.includes("trashed"), "a trashed session is flagged, not hidden");
  ok($("storeUsers").textContent.includes("not recorded by the store"),
     "the earliest-use date is labelled as derived");

  console.log("following an artefact leaves the browser");
  const went = [];
  win.__went = went;
  run("gotoArtifact = async (s, f) => { window.__went.push([s, f]); };");
  users[0].onclick();
  await tick();
  ok(!$("codeStoreScrim").classList.contains("show"), "the browser closes");
  eq(JSON.stringify(went[0]), JSON.stringify(["/p/S-26-0003", "plot.png"]),
     "and it navigates to that artefact");

  console.log("Copy copies the open version");
  await win.openCodeStore();
  rowFor("helper.py").onclick();
  rowFor(B("a").slice(0, 12)).onclick();
  await tick();
  await $("storeCopy").onclick();
  eq(copied[copied.length - 1], "VALUE = 99\n", "copied the selected version");

  console.log("a version nothing references says so");
  rowFor("util.py").onclick();
  rowFor(B("d").slice(0, 12)).onclick();
  await tick();
  ok($("storeUsers").textContent.includes("gc"), "names what would collect it");
  ok(!$("storeUsers").querySelector(".u"), "and lists no artefacts");

  console.log("a version whose bytes are gone is not readable");
  rowFor("run.py").onclick();
  const goneRow = rowFor(B("c").slice(0, 12));
  ok(goneRow.classList.contains("gone"), "the row is greyed");
  ok(!goneRow.getAttribute("data-store-blob"), "and is not selectable");
  ok(goneRow.textContent.includes("missing"), "and says why");

  console.log("the entry point is badged wherever it appears");
  ok(goneRow.querySelector(".cv-badge").textContent === "entry",
     "run.py's version is marked as an entry point");

  console.log("the filter narrows to matching paths and opens them");
  $("storeFilter").value = "helper";
  $("storeFilter").oninput();
  ok(!labels().includes("util.py"), "non-matching paths are dropped");
  ok(labels().includes("helper.py"), "the match is kept");
  ok(labels().includes(B("a").slice(0, 12)), "and a searched path is already open");
  ok($("storeNote").textContent.includes("1 of 3"), "the footer says how much matched");
  $("storeFilter").value = "nothing-matches-this";
  $("storeFilter").oninput();
  ok($("storeTree").textContent.includes("No file matches"), "an empty result explains itself");

  console.log("Escape closes it");
  win.document.dispatchEvent(new win.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  ok(!$("codeStoreScrim").classList.contains("show"), "Escape closed it");

  console.log("an unreadable store reports rather than showing an empty tree");
  treeResponse = new Error("permission denied");
  await win.openCodeStore();
  ok($("storeTree").textContent.includes("permission denied"), "the error is shown in the tree pane");
  $("storeClose").onclick();

  console.log("an archive with no captured source says so");
  treeResponse = { repos: [], n_repos: 0, n_paths: 0, n_versions: 0, n_snapshots: 0,
                   n_blobs: 0, n_missing: 0, store_dir: "/p/code" };
  await win.openCodeStore();
  ok($("storeTree").textContent.includes("captured no source"), "empty store is explained");

  console.log(`\n${checks - failures}/${checks} assertions passed`);
  process.exit(failures ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
