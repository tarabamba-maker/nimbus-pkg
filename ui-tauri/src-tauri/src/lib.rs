use std::fs::OpenOptions;
use std::io::Write;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use tauri::RunEvent;

static BACKEND: Mutex<Option<Child>> = Mutex::new(None);

/// Append one line to ~/Library/Logs/StockAutomation/backend.log (macOS) so
/// the user can post-mortem why python failed to start. On a packaged .app GUI
/// stdout/stderr are otherwise invisible.
fn log_line(msg: &str) {
    let log_dir = dirs_next::home_dir()
        .map(|h| h.join("Library/Logs/StockAutomation"))
        .unwrap_or_else(|| std::path::PathBuf::from("/tmp"));
    let _ = std::fs::create_dir_all(&log_dir);
    let log_path = log_dir.join("backend.log");
    if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(&log_path) {
        let _ = writeln!(f, "[{}] {}", chrono_now(), msg);
    }
    eprintln!("[backend] {msg}");
}

fn chrono_now() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    format!("{}", secs)
}

/// Run `python -c "import flask, playwright, PIL, bs4"` and return whether it succeeded.
/// We rely on these packages to start Flask, so a python without them is useless.
fn python_has_deps(python: &str) -> bool {
    Command::new(python)
        .args(["-c", "import flask, playwright, PIL, bs4"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

/// Find a usable python3 interpreter. GUI-launched .app processes on macOS
/// inherit only `/usr/bin:/bin:/usr/sbin:/sbin`, which means homebrew/pyenv
/// /python.org installs and project venvs are invisible. Try project venv
/// first, then common system locations, then login-shell PATH — and verify
/// flask + deps are importable at every step.
fn find_python3(project_root: Option<&std::path::Path>) -> Option<String> {
    // 1) Project virtualenv — the most reliable bet, since the user likely
    //    set up packages there. Check .venv/ and venv/ in the project root.
    if let Some(root) = project_root {
        for venv in ["bin/python3", "bin/python", ".venv/bin/python3", ".venv/bin/python",
                     "venv/bin/python3", "venv/bin/python"] {
            let p = root.join(venv);
            if p.exists() {
                let ps = p.to_string_lossy().to_string();
                if python_has_deps(&ps) {
                    log_line(&format!("python3 found in project venv: {ps}"));
                    return Some(ps);
                }
            }
        }
    }
    // 2) Common system install locations (newest first)
    let candidates = [
        "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3",
        "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3",
        "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
        "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3",
        "/opt/homebrew/bin/python3", // Apple Silicon homebrew
        "/usr/local/bin/python3",    // Intel homebrew
        "/usr/bin/python3",          // Apple-shipped (may lack our deps)
    ];
    for c in &candidates {
        if std::path::Path::new(c).exists() && python_has_deps(c) {
            log_line(&format!("python3 found at {c}"));
            return Some((*c).to_string());
        }
    }
    // 3) Login shell PATH (picks up direnv/conda/pyenv etc.)
    let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/zsh".into());
    if let Ok(out) = Command::new(&shell)
        .args(["-l", "-c", "command -v python3"])
        .output()
    {
        let path = String::from_utf8_lossy(&out.stdout).trim().to_string();
        if !path.is_empty() && std::path::Path::new(&path).exists() && python_has_deps(&path) {
            log_line(&format!("python3 resolved via {shell} -l: {path}"));
            return Some(path);
        }
    }
    None
}

/// Locate main.py: bundled inside the .app Resources for packaged builds,
/// or the project root for dev/cargo runs.
fn find_main_py() -> Option<std::path::PathBuf> {
    // 1) Bundled inside .app: Stock Automation.app/Contents/Resources/main.py
    if let Ok(exe) = std::env::current_exe() {
        if let Some(macos_dir) = exe.parent() {
            let resources = macos_dir.join("../Resources/main.py");
            if resources.exists() {
                return Some(resources);
            }
            // 2) Dev/cargo: target/debug/ui-tauri → up to project root
            let dev = macos_dir.join("../../../../main.py");
            if dev.exists() {
                return Some(dev);
            }
        }
    }
    // 3) Env override (for power users / dev)
    if let Ok(p) = std::env::var("STOCK_MAIN_PY") {
        let pb = std::path::PathBuf::from(p);
        if pb.exists() {
            return Some(pb);
        }
    }
    // 4) Last-resort hardcoded dev path
    let fb = std::path::PathBuf::from("/Users/admin/Desktop/Stock_Automation/main.py");
    if fb.exists() {
        return Some(fb);
    }
    None
}

fn start_backend() {
    // Kill any stale instance first
    let _ = Command::new("pkill").args(["-f", "main.py"]).status();
    std::thread::sleep(std::time::Duration::from_millis(500));

    let path = match find_main_py() {
        Some(p) => p,
        None => {
            log_line("ERROR: main.py not found in any expected location");
            return;
        }
    };
    log_line(&format!("main.py found at {}", path.display()));

    let project_root = path.parent().map(|p| p.to_path_buf());
    let python = match find_python3(project_root.as_deref()) {
        Some(p) => p,
        None => {
            log_line("ERROR: python3 with required packages (flask/playwright/PIL/bs4) not found. \
                     Install with: pip3 install flask playwright pillow beautifulsoup4 requests");
            return;
        }
    };

    // Open a log file for python's stdout/stderr so the user can debug
    let log_dir = dirs_next::home_dir()
        .map(|h| h.join("Library/Logs/StockAutomation"))
        .unwrap_or_else(|| std::path::PathBuf::from("/tmp"));
    let _ = std::fs::create_dir_all(&log_dir);
    let stdout_log = OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_dir.join("python.log"))
        .ok();
    let stderr_log = OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_dir.join("python.log"))
        .ok();

    let mut cmd = Command::new(&python);
    cmd.arg(&path)
        .current_dir(path.parent().unwrap_or(std::path::Path::new(".")));
    if let Some(f) = stdout_log {
        cmd.stdout(Stdio::from(f));
    }
    if let Some(f) = stderr_log {
        cmd.stderr(Stdio::from(f));
    }

    match cmd.spawn() {
        Ok(child) => {
            log_line(&format!("python spawned, PID={}", child.id()));
            *BACKEND.lock().unwrap() = Some(child);
        }
        Err(e) => log_line(&format!("ERROR: failed to spawn python: {e}")),
    }
}

fn stop_backend() {
    if let Ok(mut guard) = BACKEND.lock() {
        if let Some(mut child) = guard.take() {
            let _ = child.kill();
        }
    }
    let _ = Command::new("pkill").args(["-f", "main.py"]).status();
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    start_backend();

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .build(tauri::generate_context!())
        .expect("error while running tauri application")
        .run(|_app, event| {
            if let RunEvent::Exit = event {
                stop_backend();
            }
        });
}
