use std::fs::OpenOptions;
use std::io::Write;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use tauri::RunEvent;

static BACKEND: Mutex<Option<Child>> = Mutex::new(None);

fn log_dir() -> std::path::PathBuf {
    #[cfg(target_os = "windows")]
    {
        dirs_next::data_local_dir()
            .map(|h| h.join("StockAutomation"))
            .unwrap_or_else(|| std::path::PathBuf::from("C:\\Temp\\StockAutomation"))
    }
    #[cfg(not(target_os = "windows"))]
    {
        dirs_next::home_dir()
            .map(|h| h.join("Library/Logs/StockAutomation"))
            .unwrap_or_else(|| std::path::PathBuf::from("/tmp"))
    }
}

// Persistent user-data directory — survives app updates. DB, recipes, image
// caches, chrome profiles all live here. Mac: ~/Library/Application Support/
// StockAutomation/. Windows: %APPDATA%\StockAutomation\.
fn data_dir() -> std::path::PathBuf {
    #[cfg(target_os = "windows")]
    {
        dirs_next::data_dir()
            .map(|h| h.join("StockAutomation"))
            .unwrap_or_else(|| std::path::PathBuf::from("C:\\Temp\\StockAutomation"))
    }
    #[cfg(not(target_os = "windows"))]
    {
        dirs_next::home_dir()
            .map(|h| h.join("Library/Application Support/StockAutomation"))
            .unwrap_or_else(|| std::path::PathBuf::from("/tmp/StockAutomation"))
    }
}

fn log_line(msg: &str) {
    let dir = log_dir();
    let _ = std::fs::create_dir_all(&dir);
    if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(dir.join("backend.log")) {
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

fn python_has_deps(python: &str) -> bool {
    // ⚠️ KEEP THIS LIST IN SYNC WITH main.py's top-level imports.
    // If a module main.py needs isn't listed here, the launcher may pick a Python
    // missing it → Flask crashes at startup with ModuleNotFoundError → empty UI
    // with no clear error.
    //
    // `browser_cookie3` is REQUIRED on Windows (Chrome cookie decryption) but
    // optional on macOS (Safari binarycookies parsed in-tree). The import check
    // verifies all platforms uniformly so launches with the wrong Python pick
    // up the absence quickly.
    Command::new(python)
        .args(["-c", "import flask, flask_cors, playwright, PIL, bs4, requests, browser_cookie3"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

fn find_python3(project_root: Option<&std::path::Path>) -> Option<String> {
    // 1) Project venv — most reliable; paths differ by OS
    if let Some(root) = project_root {
        #[cfg(target_os = "windows")]
        let venv_names = [".venv/Scripts/python.exe", "venv/Scripts/python.exe", "Scripts/python.exe"];
        #[cfg(not(target_os = "windows"))]
        let venv_names = [".venv/bin/python3", ".venv/bin/python", "venv/bin/python3", "venv/bin/python", "bin/python3", "bin/python"];

        for rel in &venv_names {
            let p = root.join(rel);
            if p.exists() {
                let ps = p.to_string_lossy().to_string();
                if python_has_deps(&ps) {
                    log_line(&format!("python found in project venv: {ps}"));
                    return Some(ps);
                }
            }
        }
    }

    // 2) Common fixed locations
    #[cfg(target_os = "windows")]
    let candidates: &[&str] = &[
        "C:\\Python313\\python.exe",
        "C:\\Python312\\python.exe",
        "C:\\Python311\\python.exe",
        "C:\\Python310\\python.exe",
        "C:\\Program Files\\Python313\\python.exe",
        "C:\\Program Files\\Python312\\python.exe",
        "C:\\Program Files\\Python311\\python.exe",
        "C:\\Program Files\\Python310\\python.exe",
    ];
    #[cfg(not(target_os = "windows"))]
    let candidates: &[&str] = &[
        "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3",
        "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3",
        "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
        "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3",
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        "/usr/bin/python3",
    ];

    for c in candidates {
        if std::path::Path::new(c).exists() && python_has_deps(c) {
            log_line(&format!("python found at {c}"));
            return Some((*c).to_string());
        }
    }

    // 3) PATH / launcher fallback
    #[cfg(target_os = "windows")]
    {
        // Windows Python Launcher (py.exe) — resolves the latest installed Python
        if let Ok(out) = Command::new("py").args(["-3", "-c", "import sys; print(sys.executable)"]).output() {
            let path = String::from_utf8_lossy(&out.stdout).trim().to_string();
            if !path.is_empty() && std::path::Path::new(&path).exists() && python_has_deps(&path) {
                log_line(&format!("python found via py launcher: {path}"));
                return Some(path);
            }
        }
        for cmd in ["python", "python3"] {
            if python_has_deps(cmd) {
                log_line(&format!("python found on PATH: {cmd}"));
                return Some(cmd.to_string());
            }
        }
    }
    #[cfg(not(target_os = "windows"))]
    {
        let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/zsh".into());
        if let Ok(out) = Command::new(&shell).args(["-l", "-c", "command -v python3"]).output() {
            let path = String::from_utf8_lossy(&out.stdout).trim().to_string();
            if !path.is_empty() && std::path::Path::new(&path).exists() && python_has_deps(&path) {
                log_line(&format!("python3 resolved via {shell} -l: {path}"));
                return Some(path);
            }
        }
    }

    None
}

fn find_main_py() -> Option<std::path::PathBuf> {
    if let Ok(exe) = std::env::current_exe() {
        if let Some(exe_dir) = exe.parent() {
            // ⚠️ DO NOT REMOVE THE _up_/_up_/ PATHS — they are NOT dev artifacts.
            // tauri.conf.json declares resources as ["../../main.py", ...] (two parent
            // traversals). Tauri 2 mirrors that depth under Resources as _up_/_up_/.
            // Without these paths, packaged app can't find main.py → empty UI.
            //
            // ⚠️ DO NOT add a writability check here. Signed .app installs ARE
            // read-only; user-writable state lives in STOCK_DATA_DIR (see data_dir()).
            #[cfg(target_os = "windows")]
            for rel in ["_up_/_up_/main.py", "_up_/main.py", "main.py"] {
                let p = exe_dir.join(rel);
                if p.exists() { return Some(p); }
            }
            #[cfg(target_os = "macos")]
            for rel in [
                "../Resources/_up_/_up_/main.py",
                "../Resources/_up_/main.py",
                "../Resources/main.py",
            ] {
                let p = exe_dir.join(rel);
                if p.exists() {
                    return Some(p.canonicalize().unwrap_or(p));
                }
            }

            // Dev/cargo builds: target/debug/ or target/release/ — walk up to project root
            for rel in ["../../../../main.py", "../../../main.py"] {
                let p = exe_dir.join(rel);
                if p.exists() {
                    return Some(p.canonicalize().unwrap_or(p));
                }
            }
        }
    }

    // Env override (power users / CI)
    if let Ok(p) = std::env::var("STOCK_MAIN_PY") {
        let pb = std::path::PathBuf::from(p);
        if pb.exists() { return Some(pb); }
    }

    None
}

fn start_backend() {
    // Kill any stale instance from a previous crash
    #[cfg(not(target_os = "windows"))]
    let _ = Command::new("pkill").args(["-f", "main.py"]).status();
    #[cfg(target_os = "windows")]
    let _ = Command::new("wmic")
        .args(["process", "where", "CommandLine like '%main.py%'", "delete"])
        .status();

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
                     Install with: pip install flask playwright pillow beautifulsoup4 requests");
            return;
        }
    };

    let dir = log_dir();
    let _ = std::fs::create_dir_all(&dir);
    let stdout_log = OpenOptions::new().create(true).append(true).open(dir.join("python.log")).ok();
    let stderr_log = OpenOptions::new().create(true).append(true).open(dir.join("python.log")).ok();

    // Stable data dir survives auto-updates (DB lived inside .app before — wiped on update).
    let data = data_dir();
    let _ = std::fs::create_dir_all(&data);
    log_line(&format!("STOCK_DATA_DIR = {}", data.display()));

    let mut cmd = Command::new(&python);
    cmd.arg(&path)
        .current_dir(&data)
        .env("STOCK_DATA_DIR", &data);
    if let Some(f) = stdout_log { cmd.stdout(Stdio::from(f)); }
    if let Some(f) = stderr_log { cmd.stderr(Stdio::from(f)); }

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
            #[cfg(target_os = "windows")]
            // Kill entire process tree (Python may have spawned Chromium subprocesses)
            let _ = Command::new("taskkill")
                .args(["/F", "/T", "/PID", &child.id().to_string()])
                .status();
            let _ = child.kill();
        }
    }
    // Clean up any remaining main.py processes (e.g., from a previous crash)
    #[cfg(not(target_os = "windows"))]
    let _ = Command::new("pkill").args(["-f", "main.py"]).status();
    #[cfg(target_os = "windows")]
    let _ = Command::new("wmic")
        .args(["process", "where", "CommandLine like '%main.py%'", "delete"])
        .status();
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
