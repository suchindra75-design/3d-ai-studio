//! AI 3D Studio desktop shell.
//!
//! The Rust side deliberately exposes a small, fixed set of commands. There is
//! no generic shell, process-spawning, file or HTTP command reachable from the
//! web view: the window can ask for the backend's status, restart it, save one
//! of a few kinds of backend download (or a PNG screenshot it made) to a place
//! the user picks in the native Save dialog, and open the app's own data folder. Local processing goes
//! through the backend's restricted engine.

pub mod files;
pub mod sidecar;

use std::path::{Path, PathBuf};
use std::time::Duration;

use serde::Serialize;
use tauri::{Manager, RunEvent};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_opener::OpenerExt;

use sidecar::{Sidecar, SidecarConfig, Status};

#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct AppInfo {
    pub version: String,
    pub tauri_version: String,
    pub os: String,
    pub arch: String,
}

fn build_app_info(version: &str) -> AppInfo {
    AppInfo {
        version: version.to_string(),
        tauri_version: tauri::VERSION.to_string(),
        os: std::env::consts::OS.to_string(),
        arch: std::env::consts::ARCH.to_string(),
    }
}

/// Version and platform facts shown on the About screen.
#[tauri::command]
fn app_info(app: tauri::AppHandle) -> AppInfo {
    build_app_info(&app.package_info().version.to_string())
}

#[derive(Debug, Serialize)]
struct BackendView {
    #[serde(flatten)]
    status: Status,
    data_dir: String,
    log_path: String,
    program: String,
}

/// The private backend's state; the window needs the URL and token to talk to it.
#[tauri::command]
fn backend_status(sidecar: tauri::State<'_, Sidecar>) -> BackendView {
    let config = sidecar.config();
    BackendView {
        status: sidecar.status(),
        data_dir: config.data_dir.display().to_string(),
        log_path: config.log_path.display().to_string(),
        program: config.program.display().to_string(),
    }
}

#[tauri::command]
fn restart_backend(sidecar: tauri::State<'_, Sidecar>) {
    sidecar.restart();
}

/// Asks where to save a backend download (Save dialog) and writes it there.
/// Returns the saved path, or `None` if the user cancelled.
#[tauri::command]
async fn save_backend_file(
    app: tauri::AppHandle,
    path: String,
    suggested_name: String,
) -> Result<Option<String>, String> {
    if !files::allowed_download_path(&path) {
        return Err("This file cannot be saved from here.".into());
    }
    let Status::Running {
        base_url, token, ..
    } = app.state::<Sidecar>().status()
    else {
        return Err("The local engine is not running.".into());
    };
    let Some(choice) = app
        .dialog()
        .file()
        .set_file_name(files::safe_file_name(&suggested_name))
        .blocking_save_file()
    else {
        return Ok(None);
    };
    let dest = choice.into_path().map_err(|e| e.to_string())?;
    let shown = dest.display().to_string();
    tauri::async_runtime::spawn_blocking(move || files::download(&base_url, &token, &path, &dest))
        .await
        .map_err(|e| e.to_string())??;
    Ok(Some(shown))
}

/// Asks where to save an image made in the window (a screenshot, PNG only) and
/// writes it there. The bytes arrive as the raw request body; the suggested file
/// name in the `x-file-name` header. Returns the saved path, or `None` if cancelled.
#[tauri::command]
async fn save_image(
    app: tauri::AppHandle,
    request: tauri::ipc::Request<'_>,
) -> Result<Option<String>, String> {
    let tauri::ipc::InvokeBody::Raw(bytes) = request.body() else {
        return Err("Expected the image bytes.".into());
    };
    files::acceptable_png(bytes)?;
    let name = request
        .headers()
        .get("x-file-name")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("screenshot.png");
    let mut name = files::safe_file_name(name);
    if !name.to_ascii_lowercase().ends_with(".png") {
        name.push_str(".png");
    }
    let Some(choice) = app.dialog().file().set_file_name(name).blocking_save_file() else {
        return Ok(None);
    };
    let dest = choice.into_path().map_err(|e| e.to_string())?;
    files::write_atomically(bytes, &dest)?;
    Ok(Some(dest.display().to_string()))
}

/// Opens the app's own data folder (projects, models, logs) in Finder/Explorer.
#[tauri::command]
fn open_data_folder(app: tauri::AppHandle) -> Result<(), String> {
    let dir = app.state::<Sidecar>().config().data_dir.clone();
    app.opener()
        .open_path(dir.display().to_string(), None::<&str>)
        .map_err(|e| e.to_string())
}

/// How to start the local engine.
#[derive(Debug, PartialEq, Eq)]
struct Backend {
    program: PathBuf,
    args: Vec<String>,
    /// `None`: the app's data folder (where a user's `.env` for the engine lives).
    working_dir: Option<PathBuf>,
}

/// The interpreter inside an engine bundle (`pnpm bundle:engine`).
fn bundled_python(bundle: &Path) -> PathBuf {
    if cfg!(windows) {
        bundle.join("python").join("python.exe")
    } else {
        bundle.join("python").join("bin").join("python3")
    }
}

/// Where the engine is, in order of preference: `AI3D_DESKTOP_BACKEND`, the
/// engine bundled with the app, and (development builds only) the repository's
/// virtual environment.
fn find_backend(resource_dir: Option<PathBuf>) -> Option<Backend> {
    let exe = if cfg!(windows) {
        "ai3d-server.exe"
    } else {
        "ai3d-server"
    };
    if let Ok(path) = std::env::var("AI3D_DESKTOP_BACKEND") {
        let path = PathBuf::from(path);
        if path.is_file() {
            return Some(Backend {
                working_dir: path.parent().map(Path::to_path_buf),
                program: path,
                args: Vec::new(),
            });
        }
    }
    if let Some(resources) = resource_dir {
        let python = bundled_python(&resources.join("backend"));
        if python.is_file() {
            // -I: ignore PYTHON* variables and the user's site-packages.
            return Some(Backend {
                program: python,
                args: ["-I", "-m", "ai3d.cli"].map(String::from).to_vec(),
                working_dir: None,
            });
        }
    }
    if cfg!(debug_assertions) {
        let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..");
        let bin = if cfg!(windows) { "Scripts" } else { "bin" };
        let venv = repo.join(".venv").join(bin).join(exe);
        if venv.is_file() {
            return Some(Backend {
                program: venv,
                args: Vec::new(),
                working_dir: Some(repo),
            });
        }
    }
    None
}

fn create_sidecar(app: &tauri::App) -> Sidecar {
    let data_dir = app
        .path()
        .app_data_dir()
        .unwrap_or_else(|_| std::env::temp_dir().join("ai3d-studio"));
    let log_path = data_dir.join("logs").join("backend.log");
    let found = find_backend(app.path().resource_dir().ok());
    let config = SidecarConfig {
        program: found
            .as_ref()
            .map(|b| b.program.clone())
            .unwrap_or_default(),
        args: found.as_ref().map(|b| b.args.clone()).unwrap_or_default(),
        working_dir: found
            .as_ref()
            .and_then(|b| b.working_dir.clone())
            .unwrap_or_else(|| data_dir.clone()),
        data_dir,
        log_path,
        startup_timeout: Duration::from_secs(90),
        max_restarts: 3,
    };
    if found.is_none() {
        return Sidecar::unavailable(
            config,
            "The local engine is not included in this build. Build the app with \
             `pnpm build:desktop-app` (which bundles it), set AI3D_DESKTOP_BACKEND to an \
             ai3d-server executable, or run `uv sync --all-packages` in the repository for a \
             development build."
                .into(),
        );
    }
    let sidecar = Sidecar::new(config);
    sidecar.start();
    sidecar
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        // Used from Rust only; the window has no dialog/opener permissions of its own.
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            let sidecar = create_sidecar(app);
            app.manage(sidecar);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            app_info,
            backend_status,
            restart_backend,
            save_backend_file,
            save_image,
            open_data_folder
        ])
        .build(tauri::generate_context!())
        .expect("error while building AI 3D Studio")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                app.state::<Sidecar>().stop();
            }
        });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn app_info_reports_platform() {
        let info = build_app_info("0.1.0");
        assert_eq!(info.version, "0.1.0");
        assert_eq!(info.os, std::env::consts::OS);
        assert_eq!(info.arch, std::env::consts::ARCH);
        assert!(info.tauri_version.starts_with('2'));
    }

    #[test]
    fn app_info_serializes_with_snake_case_keys() {
        let json = serde_json::to_value(build_app_info("1.2.3")).unwrap();
        for key in ["version", "tauri_version", "os", "arch"] {
            assert!(json.get(key).is_some(), "missing {key}");
        }
    }

    #[test]
    fn development_builds_find_the_repository_backend() {
        // Present wherever the repository's .venv was created with `uv sync`.
        if let Some(backend) = find_backend(None) {
            assert!(backend.program.is_file());
            assert!(backend
                .working_dir
                .unwrap()
                .join("pyproject.toml")
                .is_file());
        }
    }

    #[test]
    fn a_bundled_engine_runs_isolated_from_the_data_folder() {
        let resources = std::env::temp_dir().join(format!("ai3d-res-{}", sidecar::new_token()));
        let python = bundled_python(&resources.join("backend"));
        std::fs::create_dir_all(python.parent().unwrap()).unwrap();
        std::fs::write(&python, b"").unwrap();
        let backend = find_backend(Some(resources.clone())).unwrap();
        assert_eq!(backend.program, python);
        assert_eq!(backend.args, ["-I", "-m", "ai3d.cli"]);
        assert_eq!(backend.working_dir, None);
        std::fs::remove_dir_all(resources).unwrap();
    }
}
