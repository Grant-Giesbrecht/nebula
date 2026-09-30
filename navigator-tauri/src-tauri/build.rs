use std::path::PathBuf;
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

fn main() {
    stamp_build();
    tauri_build::build()
}

fn git(args: &[&str]) -> Option<String> {
    let out = Command::new("git").args(args).output().ok()?;
    if !out.status.success() {
        return None;
    }
    Some(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

/// Bake which commit this app was built from into the binary, in the same
/// shape nebula/buildinfo.py gives the bridge: commit count (orders builds
/// from one branch) + short hash (names the commit) + whether the tree had
/// uncommitted changes. main.rs reads these back in `app_build_info`.
fn stamp_build() {
    let count = git(&["rev-list", "--count", "HEAD"]).unwrap_or_default();
    let sha = git(&["rev-parse", "--short=7", "HEAD"]).unwrap_or_default();
    // Untracked files don't count, and neither does the bridge binary: it
    // is tracked but rewritten by every sidecar build, so counting it would
    // mark every app build that follows one as dirty. Kept in step with
    // _DIRTY_EXCLUDES in nebula/buildinfo.py.
    let dirty = match git(&["rev-parse", "--show-toplevel"]) {
        Some(top) => git(&[
            "-C", &top, "status", "--porcelain", "--untracked-files=no", "--", ".",
            ":(exclude)navigator-tauri/src-tauri/binaries",
        ])
        .map(|s| !s.is_empty())
        .unwrap_or(false),
        None => false,
    };
    let built = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);

    println!("cargo:rustc-env=NEBULA_BUILD_COUNT={count}");
    println!("cargo:rustc-env=NEBULA_BUILD_SHA={sha}");
    println!("cargo:rustc-env=NEBULA_BUILD_DIRTY={dirty}");
    println!("cargo:rustc-env=NEBULA_BUILD_TIME={built}");

    // Re-stamp when the commit moves. A commit rewrites the index, and a
    // checkout or branch switch rewrites HEAD. An edit that is not yet staged
    // doesn't touch either, so the dirty flag can lag until the next build
    // cargo decides to run this for another reason.
    if let Some(git_dir) = git(&["rev-parse", "--absolute-git-dir"]) {
        let git_dir = PathBuf::from(git_dir);
        for f in ["HEAD", "index", "packed-refs", "refs/heads"] {
            println!("cargo:rerun-if-changed={}", git_dir.join(f).display());
        }
    }
}
