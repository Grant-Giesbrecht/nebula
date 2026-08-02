// Nebula Navigator -- Tauri core.
//
// All of Nebula's real logic lives in Python, so this core does not
// reimplement any of it. Instead it spawns `python -m nebula.navigator.api`
// once as a long-lived child ("sidecar") and talks to it over stdin/stdout
// with line-delimited JSON. A single `bridge` command forwards {op, args}
// from the webview to Python and returns the parsed response, so adding a
// new backend capability never requires touching Rust -- only a new `op` in
// api.py and a call site in the front-end.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;

use serde_json::Value;
use tauri::State;

/// How many trailing stderr lines from Python we keep for error reports.
const STDERR_KEEP: usize = 40;

/// The frozen bridge, if this build has one.
///
/// `bundle.externalBin` ships `binaries/nebula-bridge-<triple>` and Tauri
/// copies it next to the main executable with the triple stripped -- inside
/// `Foo.app/Contents/MacOS/` for a bundle, or `target/<profile>/` under
/// `tauri dev`. When it is there we use it and never touch the system
/// Python at all; when it isn't (a dev checkout where build-sidecar.sh
/// hasn't been run) we fall back to a real interpreter.
fn sidecar_path() -> Option<std::path::PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let candidate = exe.parent()?.join("nebula-bridge");
    candidate.is_file().then_some(candidate)
}

/// Interpreters to try, in order, when NEBULA_PYTHON isn't set.
///
/// A bundled .app launched from Finder does NOT inherit the shell's PATH --
/// it gets /usr/bin:/bin:/usr/sbin:/sbin, where `python3` is Apple's stub
/// (3.9, no site-packages of ours). So resolving via PATH alone works under
/// `tauri dev` and then fails in the shipped app. We probe explicit install
/// locations too, and pick the first interpreter that can actually import
/// `nebula` rather than the first one that merely exists.
fn candidate_pythons() -> Vec<String> {
    if let Ok(explicit) = std::env::var("NEBULA_PYTHON") {
        if !explicit.trim().is_empty() {
            return vec![explicit];
        }
    }

    let mut out: Vec<String> = Vec::new();
    let mut push = |c: String| {
        if !c.is_empty() && !out.contains(&c) {
            out.push(c);
        }
    };

    // PATH first: honours an activated venv when run from a terminal.
    push("python3".to_string());

    // python.org framework builds, newest-looking first.
    let framework = "/Library/Frameworks/Python.framework/Versions";
    if let Ok(entries) = std::fs::read_dir(framework) {
        let mut versions: Vec<String> = entries
            .filter_map(|e| e.ok())
            .filter_map(|e| e.file_name().into_string().ok())
            .collect();
        versions.sort();
        versions.reverse();
        for v in versions {
            push(format!("{framework}/{v}/bin/python3"));
        }
    }

    // Homebrew (Apple silicon, then Intel), then the system stub as a
    // last resort -- it will normally fail the import probe, which is fine.
    for p in [
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        "/usr/bin/python3",
    ] {
        push(p.to_string());
    }

    out
}

/// True if this interpreter can import the bridge module. Cheap: one
/// short-lived process, all stdio discarded.
fn can_import_nebula(python: &str) -> bool {
    Command::new(python)
        .args(["-c", "import nebula.navigator.api"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

/// A live handle to the Python bridge process. Requests are serialized by
/// the enclosing `Mutex`, so a write is always immediately followed by the
/// read of *its* response -- ids can't interleave.
struct Bridge {
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
    next_id: u64,
    /// Python's stderr, drained by a background thread. Without this, a
    /// crash in the child surfaces only as "Broken pipe" -- and a bundled
    /// app's inherited stderr isn't visible anywhere.
    stderr_tail: Arc<Mutex<Vec<String>>>,
    python: String,
    _child: Child,
}

impl Bridge {
    fn spawn() -> Result<Self, String> {
        let mut tried: Vec<String> = Vec::new();

        // An explicit NEBULA_PYTHON wins over everything, including the
        // frozen sidecar -- that is the point of setting it.
        let explicit = std::env::var("NEBULA_PYTHON")
            .ok()
            .filter(|v| !v.trim().is_empty());
        if let Some(python) = explicit {
            return Bridge::spawn_with(&python, &["-m", "nebula.navigator.api"]);
        }

        // Preferred path in a shipped app: self-contained, no system Python.
        if let Some(sidecar) = sidecar_path() {
            let path = sidecar.to_string_lossy().to_string();
            match Bridge::spawn_with(&path, &[]) {
                Ok(bridge) => {
                    // Worth saying out loud: under `tauri dev` this frozen
                    // binary shadows the working tree, so edits to
                    // src/nebula/*.py do nothing until build-sidecar.sh is
                    // re-run. Set NEBULA_PYTHON to develop against live code.
                    eprintln!(
                        "[bridge] using bundled sidecar: {path}\n\
                         [bridge] (frozen -- Python source edits require ./build-sidecar.sh, \
                         or set NEBULA_PYTHON to use live code)"
                    );
                    return Ok(bridge);
                }
                Err(e) => tried.push(format!("{path} (bundled sidecar) -- {e}")),
            }
        }

        for python in candidate_pythons() {
            if can_import_nebula(&python) {
                return Bridge::spawn_with(&python, &["-m", "nebula.navigator.api"]);
            }
            tried.push(format!("{python} (no `nebula` package)"));
        }

        Err(format!(
            "could not start the Nebula bridge.\n\nTried:\n  {}\n\n\
             This build has no bundled sidecar, and no Python on this machine \
             has the `nebula` package. Either run ./build-sidecar.sh and rebuild, \
             or `pip install -e .` in the repo root, or set NEBULA_PYTHON.",
            tried.join("\n  ")
        ))
    }

    fn spawn_with(program: &str, args: &[&str]) -> Result<Self, String> {
        let python = program;
        let mut child = Command::new(program)
            .args(args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|e| format!("failed to start Python bridge ({python}): {e}"))?;

        let stdin = child.stdin.take().ok_or("bridge: no stdin handle")?;
        let stdout = BufReader::new(child.stdout.take().ok_or("bridge: no stdout handle")?);

        let stderr_tail = Arc::new(Mutex::new(Vec::<String>::new()));
        if let Some(err) = child.stderr.take() {
            let sink = Arc::clone(&stderr_tail);
            thread::spawn(move || {
                for line in BufReader::new(err).lines().map_while(Result::ok) {
                    eprintln!("[bridge] {line}");
                    if let Ok(mut buf) = sink.lock() {
                        buf.push(line);
                        if buf.len() > STDERR_KEEP {
                            let drop_n = buf.len() - STDERR_KEEP;
                            buf.drain(..drop_n);
                        }
                    }
                }
            });
        }

        let mut bridge = Bridge {
            stdin,
            stdout,
            next_id: 1,
            stderr_tail,
            python: python.to_string(),
            _child: child,
        };

        // Handshake now, so a broken interpreter fails here with a real
        // message instead of at the first user action with EPIPE.
        bridge
            .call("ping", Value::Null)
            .map_err(|e| format!("Python bridge ({python}) did not answer ping: {e}"))?;

        Ok(bridge)
    }

    /// Trailing Python stderr, if any -- appended to I/O errors so the
    /// underlying traceback reaches the user.
    fn stderr_context(&self) -> String {
        match self.stderr_tail.lock() {
            Ok(buf) if !buf.is_empty() => format!("\n\nPython said:\n{}", buf.join("\n")),
            _ => String::new(),
        }
    }

    fn call(&mut self, op: &str, args: Value) -> Result<Value, String> {
        let id = self.next_id;
        self.next_id += 1;

        let req = serde_json::json!({ "id": id, "op": op, "args": args });
        let mut line = serde_json::to_string(&req).map_err(|e| e.to_string())?;
        line.push('\n');
        if let Err(e) = self.stdin.write_all(line.as_bytes()) {
            return Err(format!(
                "bridge write failed ({}): {e}{}",
                self.python,
                self.stderr_context()
            ));
        }
        self.stdin.flush().map_err(|e| e.to_string())?;

        let mut resp_line = String::new();
        let n = self
            .stdout
            .read_line(&mut resp_line)
            .map_err(|e| format!("bridge read failed: {e}{}", self.stderr_context()))?;
        if n == 0 {
            return Err(format!(
                "bridge closed unexpectedly (Python process exited){}",
                self.stderr_context()
            ));
        }

        let resp: Value = serde_json::from_str(resp_line.trim())
            .map_err(|e| format!("bridge returned invalid JSON: {e}: {resp_line}"))?;
        if resp.get("ok").and_then(Value::as_bool).unwrap_or(false) {
            Ok(resp.get("data").cloned().unwrap_or(Value::Null))
        } else {
            Err(resp
                .get("error")
                .and_then(Value::as_str)
                .unwrap_or("unknown bridge error")
                .to_string())
        }
    }
}

/// Either a running bridge, or the reason we haven't got one. Startup
/// failure must not panic: a panicking bundled app just vanishes, with the
/// explanation going to a stderr nobody reads. Holding the error lets the
/// window open and report it through the normal error path instead.
enum BridgeSlot {
    Live(Box<Bridge>),
    Failed(String),
}

struct BridgeState(Mutex<BridgeSlot>);

/// The one command the front-end calls: `invoke('bridge', { op, args })`.
#[tauri::command]
fn bridge(op: String, args: Option<Value>, state: State<BridgeState>) -> Result<Value, String> {
    let mut slot = state
        .0
        .lock()
        .map_err(|_| "bridge lock poisoned".to_string())?;
    match &mut *slot {
        BridgeSlot::Live(b) => b.call(&op, args.unwrap_or(Value::Null)),
        BridgeSlot::Failed(why) => Err(why.clone()),
    }
}

/// macOS only: replace the default menu bar with the three submenus that
/// actually matter here -- the stock menu is mostly irrelevant to this app.
///
/// Edit is not optional: on macOS, WKWebView's copy/paste and select-all
/// come from these menu items. Drop them and Cmd-C stops working in the
/// search boxes and the selectable panel text.
///
/// Window brings back Cmd-M / Cmd-W. Mind the interaction with the
/// front-end's shortcuts: AppKit consumes menu accelerators *before* the
/// webview sees a key event, so anything bound here is unavailable to
/// main.js. That is why "show metadata" is Cmd-Shift-M rather than Cmd-M.
///
/// The View submenu exists to make the app's own shortcuts *findable*.
/// Because of that same interception, those items cannot simply be labels:
/// once an accelerator is in the menu, the webview stops receiving it. So
/// each one emits `menu://action` with its id, and the front-end runs the
/// same handler the keyboard path would have. Keep the ids in sync with
/// MENU_ACTIONS in main.js.
#[cfg(target_os = "macos")]
fn install_menu(app: &tauri::App) -> tauri::Result<()> {
    use tauri::menu::{AboutMetadata, MenuBuilder, MenuItemBuilder, SubmenuBuilder};

    let identity_item = MenuItemBuilder::with_id("menu:identity", "Set Your Name…")
        .build(app)?;
    let app_menu = SubmenuBuilder::new(app, "Nebula Navigator")
        .about(Some(AboutMetadata::default()))
        .separator()
        .item(&identity_item)
        .separator()
        .hide()
        .hide_others()
        .show_all()
        .separator()
        .quit()
        .build()?;

    let edit_menu_extras = MenuItemBuilder::with_id("menu:collect", "Add to Collection…")
        .accelerator("CmdOrCtrl+Shift+C")
        .build(app)?;

    let edit_menu = SubmenuBuilder::new(app, "Edit")
        .undo()
        .redo()
        .separator()
        .cut()
        .copy()
        .paste()
        .select_all()
        .separator()
        .item(&edit_menu_extras)
        .build()?;

    // Ids match the MENU_ACTIONS table in main.js.
    let metadata = MenuItemBuilder::with_id("menu:metadata", "Show File Metadata")
        .accelerator("CmdOrCtrl+Shift+M")
        .build(app)?;
    let session = MenuItemBuilder::with_id("menu:session", "Show Session Info")
        .accelerator("CmdOrCtrl+Shift+S")
        .build(app)?;
    let archive = MenuItemBuilder::with_id("menu:archive", "Archive Management…")
        .build(app)?;
    let reload = MenuItemBuilder::with_id("menu:reload", "Reload Archive")
        .accelerator("CmdOrCtrl+R")
        .build(app)?;
    let open_sel = MenuItemBuilder::with_id("menu:open", "Open Selected File")
        .accelerator("CmdOrCtrl+O")
        .build(app)?;
    let import = MenuItemBuilder::with_id("menu:import", "Import Files…")
        .accelerator("CmdOrCtrl+Shift+I")
        .build(app)?;

    let new_tab = MenuItemBuilder::with_id("menu:new-tab", "New Tab")
        .accelerator("CmdOrCtrl+T")
        .build(app)?;
    let dup_tab = MenuItemBuilder::with_id("menu:duplicate-tab", "Duplicate Tab")
        .accelerator("CmdOrCtrl+D")
        .build(app)?;
    let close_tab = MenuItemBuilder::with_id("menu:close-tab", "Close Tab")
        .accelerator("CmdOrCtrl+W")
        .build(app)?;
    let next_tab = MenuItemBuilder::with_id("menu:next-tab", "Next Tab")
        .accelerator("CmdOrCtrl+Shift+]")
        .build(app)?;
    let prev_tab = MenuItemBuilder::with_id("menu:prev-tab", "Previous Tab")
        .accelerator("CmdOrCtrl+Shift+[")
        .build(app)?;
    let relations = MenuItemBuilder::with_id("menu:relations", "Show Relations")
        .accelerator("CmdOrCtrl+Shift+L")
        .build(app)?;

    let tab_sessions = MenuItemBuilder::with_id("menu:tab-sessions", "Sessions")
        .accelerator("CmdOrCtrl+1")
        .build(app)?;
    let tab_collections = MenuItemBuilder::with_id("menu:tab-collections", "Collections")
        .accelerator("CmdOrCtrl+2")
        .build(app)?;
    let tab_searches = MenuItemBuilder::with_id("menu:tab-searches", "Saved Searches")
        .accelerator("CmdOrCtrl+3")
        .build(app)?;

    // Anything that brings data in or sends it out belongs in File; View is
    // for what is on screen. Splitting them this way means Import/Export are
    // where a Mac user reaches for them rather than buried under View.
    let new_archive = MenuItemBuilder::with_id("menu:new-archive", "New Archive…")
        .accelerator("CmdOrCtrl+Shift+N")
        .build(app)?;
    let new_window_item = MenuItemBuilder::with_id("menu:new-window", "New Window")
        .accelerator("CmdOrCtrl+N")
        .build(app)?;
    let import_intake = MenuItemBuilder::with_id("menu:import-intake",
                                                 "Import Intake Archive…")
        .build(app)?;
    let import_fragment = MenuItemBuilder::with_id("menu:import-fragment",
                                                   "Import Fragment…")
        .build(app)?;
    let adopt_fragment = MenuItemBuilder::with_id("menu:adopt-fragment",
                                                  "Adopt from Fragment…")
        .build(app)?;
    let export_item = MenuItemBuilder::with_id("menu:export", "Export Fragment…")
        .accelerator("CmdOrCtrl+Shift+E")
        .build(app)?;
    let open_archive = MenuItemBuilder::with_id("menu:open-archive", "Open Archive…")
        .accelerator("CmdOrCtrl+Shift+O")
        .build(app)?;
    let index_item = MenuItemBuilder::with_id("menu:index-view", "Inspect Index")
        .build(app)?;
    let file_menu = SubmenuBuilder::new(app, "File")
        .item(&new_archive)
        .item(&new_window_item)
        .item(&new_tab)
        .item(&dup_tab)
        .separator()
        .item(&open_archive)
        .separator()
        .item(&import)
        .item(&import_intake)
        .item(&import_fragment)
        .item(&adopt_fragment)
        .separator()
        .item(&export_item)
        .separator()
        .item(&open_sel)
        .separator()
        .item(&close_tab)
        .build()?;

    let view_menu = SubmenuBuilder::new(app, "View")
        .item(&tab_sessions)
        .item(&tab_collections)
        .item(&tab_searches)
        .separator()
        .item(&metadata)
        .item(&session)
        .item(&relations)
        .separator()
        .item(&archive)
        .item(&index_item)
        .separator()
        .item(&reload)
        .build()?;

    let window_menu = SubmenuBuilder::new(app, "Window")
        .item(&next_tab)
        .item(&prev_tab)
        .separator()
        .minimize()
        .maximize()
        .fullscreen()
        .separator()
        .close_window()
        .build()?;

    let menu = MenuBuilder::new(app)
        .items(&[&app_menu, &file_menu, &edit_menu, &view_menu, &window_menu])
        .build()?;
    app.set_menu(menu)?;
    Ok(())
}

/// Open another window onto the same archive.
///
/// One process, one Python bridge: `bridge` is an app-wide command, so a
/// second webview shares the sidecar rather than spawning another. Each
/// window keeps its own tabs (keyed by window label in localStorage).
#[tauri::command]
fn new_window(app: tauri::AppHandle) -> Result<String, String> {
    use tauri::{WebviewUrl, WebviewWindowBuilder};

    let label = format!("nav-{}", std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0));
    WebviewWindowBuilder::new(&app, &label, WebviewUrl::App("index.html".into()))
        .title("Nebula Navigator")
        .inner_size(1180.0, 780.0)
        .build()
        .map_err(|e| e.to_string())?;
    Ok(label)
}

/// Open a panel (sidecar or session info) as its own window.
///
/// The subject travels in the URL rather than in a follow-up message, so
/// the window is self-sufficient the moment it boots -- no waiting for the
/// webview to be ready, and no race between "window exists" and "window
/// knows what to show".
#[tauri::command]
fn open_panel_window(app: tauri::AppHandle, kind: String, title: String,
                     query: String) -> Result<String, String> {
    use tauri::{WebviewUrl, WebviewWindowBuilder};

    let label = format!("panel-{}-{}", kind, std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0));
    let url = format!("index.html?{query}");
    WebviewWindowBuilder::new(&app, &label, WebviewUrl::App(url.into()))
        .title(title)
        .inner_size(460.0, 700.0)
        .min_inner_size(320.0, 320.0)
        .build()
        .map_err(|e| e.to_string())?;
    Ok(label)
}

/// Tell every window something changed, so a panel showing the same subject
/// refreshes instead of quietly going stale.
#[tauri::command]
fn broadcast(app: tauri::AppHandle, event: String,
             payload: serde_json::Value) -> Result<(), String> {
    use tauri::Emitter;

    app.emit(&event, payload).map_err(|e| e.to_string())
}

/// The main window, for a panel handing itself back.
#[tauri::command]
fn main_window_label(app: tauri::AppHandle) -> Option<String> {
    use tauri::Manager;

    if app.get_webview_window("main").is_some() {
        return Some("main".into());
    }
    // The original window may have been closed; any non-panel window will do.
    app.webview_windows().keys()
        .find(|l| !l.starts_with("panel-"))
        .cloned()
}

/// Which window is under the pointer right now, in screen coordinates.
///
/// This is what makes a tab (or a selection of files) draggable *between*
/// windows: a webview cannot see a pointer that left it, so at drop time
/// the front-end asks the OS side which window the cursor is over, and the
/// payload is handed to that window as an event.
#[tauri::command]
fn window_at_cursor(app: tauri::AppHandle) -> Result<Option<String>, String> {
    use tauri::Manager;

    let cursor = app.cursor_position().map_err(|e| e.to_string())?;
    let mut best: Option<(String, i32)> = None;
    for (label, window) in app.webview_windows() {
        let (Ok(pos), Ok(size)) = (window.outer_position(), window.outer_size()) else {
            continue;
        };
        let (x, y) = (cursor.x as i32, cursor.y as i32);
        let inside = x >= pos.x && y >= pos.y
            && x < pos.x + size.width as i32
            && y < pos.y + size.height as i32;
        if !inside {
            continue;
        }
        // Prefer the focused window when they overlap: that is the one the
        // user is looking at, and the one a drop visually landed on.
        let score = if window.is_focused().unwrap_or(false) { 1 } else { 0 };
        if best.as_ref().map_or(true, |(_, s)| score >= *s) {
            best = Some((label, score));
        }
    }
    Ok(best.map(|(label, _)| label))
}

/// Hand a payload (a tab, or a set of refs) to another window.
#[tauri::command]
fn send_to_window(app: tauri::AppHandle, label: String,
                  payload: serde_json::Value) -> Result<(), String> {
    use tauri::{Emitter, Manager};

    let window = app.get_webview_window(&label)
        .ok_or_else(|| format!("no window {label}"))?;
    window.emit("nebula://accept", payload).map_err(|e| e.to_string())?;
    let _ = window.set_focus();
    Ok(())
}

fn main() {
    // Not named `bridge`: that would shadow the #[tauri::command] fn of the
    // same name, which `generate_handler!` needs to resolve in this scope.
    let slot = match Bridge::spawn() {
        Ok(b) => BridgeSlot::Live(Box::new(b)),
        Err(why) => {
            eprintln!("Nebula Navigator: {why}");
            BridgeSlot::Failed(why)
        }
    };

    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(BridgeState(Mutex::new(slot)))
        .invoke_handler(tauri::generate_handler![
            bridge, new_window, window_at_cursor, send_to_window,
            open_panel_window, main_window_label, broadcast
        ])
        .setup(|_app| {
            #[cfg(target_os = "macos")]
            install_menu(_app)?;
            Ok(())
        })
        // A menu accelerator never reaches the webview, so the item hands
        // the action to the front-end itself. Ids are "menu:<action>".
        .on_menu_event(|app, event| {
            use tauri::Emitter;

            if let Some(action) = event.id().0.strip_prefix("menu:") {
                let _ = app.emit("menu://action", action);
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running Nebula Navigator");
}
