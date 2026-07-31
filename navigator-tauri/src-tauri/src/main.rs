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
        let candidates = candidate_pythons();
        let mut tried: Vec<String> = Vec::new();

        for python in &candidates {
            if can_import_nebula(python) {
                return Bridge::spawn_with(python);
            }
            tried.push(python.clone());
        }

        Err(format!(
            "no Python interpreter with the `nebula` package was found.\n\n\
             Tried: {}\n\n\
             Install it for one of those interpreters (`pip install -e .` in \
             the repo root), or set NEBULA_PYTHON to the interpreter that has it.",
            tried.join(", ")
        ))
    }

    fn spawn_with(python: &str) -> Result<Self, String> {
        let mut child = Command::new(python)
            .args(["-m", "nebula.navigator.api"])
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
        .invoke_handler(tauri::generate_handler![bridge])
        .run(tauri::generate_context!())
        .expect("error while running Nebula Navigator");
}
