//! Runs the real backend through the sidecar (needs the repository's `.venv`).
//!
//! Skipped (with a message) when `.venv/bin/ai3d-server` does not exist.

use std::path::{Path, PathBuf};
use std::time::Duration;

use ai3d_desktop_lib::files;
use ai3d_desktop_lib::sidecar::{new_token, Sidecar, SidecarConfig, Status};

fn backend() -> Option<(PathBuf, PathBuf)> {
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..");
    let bin = if cfg!(windows) {
        "Scripts/ai3d-server.exe"
    } else {
        "bin/ai3d-server"
    };
    let program = repo.join(".venv").join(bin);
    program.is_file().then_some((program, repo))
}

fn get(url: &str, token: Option<&str>) -> u16 {
    let mut request = ureq::get(url).timeout(Duration::from_secs(10));
    if let Some(token) = token {
        request = request.set("Authorization", &format!("Bearer {token}"));
    }
    match request.call() {
        Ok(response) => response.status(),
        Err(ureq::Error::Status(code, _)) => code,
        Err(e) => panic!("request failed: {e}"),
    }
}

#[cfg(unix)]
fn alive(pid: u32) -> bool {
    // SAFETY: signal 0 only checks that the process exists.
    unsafe { libc::kill(pid as libc::pid_t, 0) == 0 }
}

#[test]
#[cfg(unix)]
fn sidecar_lifecycle_with_the_real_backend() {
    let Some((program, repo)) = backend() else {
        eprintln!("skipped: run `uv sync --all-packages` to create .venv/bin/ai3d-server");
        return;
    };
    let root = std::env::temp_dir().join(format!("ai3d-sidecar-{}", &new_token()[..12]));
    let sidecar = Sidecar::new(SidecarConfig {
        program,
        args: Vec::new(),
        working_dir: repo,
        data_dir: root.join("data"),
        log_path: root.join("logs/backend.log"),
        startup_timeout: Duration::from_secs(120),
        max_restarts: 2,
    });
    sidecar.start();

    let Status::Running {
        base_url,
        port,
        token,
    } = sidecar.wait_ready(Duration::from_secs(150))
    else {
        panic!("backend did not start: {:?}", sidecar.status());
    };
    assert!(base_url.starts_with("http://127.0.0.1:"));
    assert_eq!(token.len(), 64);

    // Health is open; everything else needs the per-launch token.
    assert_eq!(get(&format!("{base_url}/api/v1/health"), None), 200);
    assert_eq!(get(&format!("{base_url}/api/v1/projects"), None), 401);
    assert_eq!(
        get(&format!("{base_url}/api/v1/projects"), Some(&new_token())),
        401
    );
    assert_eq!(
        get(&format!("{base_url}/api/v1/projects"), Some(&token)),
        200
    );

    // It uses the app's own data folder, and logs there.
    assert!(root.join("data/ai3d.sqlite3").is_file());
    assert!(root.join("logs/backend.log").is_file());

    // Saving only accepts download paths, and reports the backend's answer.
    let dest = root.join("out.glb");
    let refused = files::download(&base_url, &token, "/api/v1/projects", &dest).unwrap_err();
    assert!(refused.contains("Not a downloadable backend path"));
    let missing = "/api/v1/exports/00000000-0000-4000-8000-000000000000/file";
    let not_found = files::download(&base_url, &token, missing, &dest).unwrap_err();
    assert!(not_found.contains("404"), "{not_found}");
    assert!(!dest.exists());

    // A crash is noticed and the backend comes back (new port, new token).
    let pid = sidecar.pid().expect("backend pid");
    // SAFETY: killing the child we started.
    unsafe { libc::kill(pid as libc::pid_t, libc::SIGKILL) };
    std::thread::sleep(Duration::from_secs(1));
    let restarted = loop {
        match sidecar.wait_ready(Duration::from_secs(150)) {
            Status::Running {
                port: new_port,
                token: new_token,
                ..
            } => break (new_port, new_token),
            Status::Starting => continue,
            other => panic!("not restarted: {other:?}"),
        }
    };
    assert_ne!(sidecar.pid(), Some(pid));
    assert!(restarted.1 != token || restarted.0 != port);

    // Stopping ends the process.
    let pid = sidecar.pid().expect("backend pid");
    sidecar.stop();
    assert_eq!(sidecar.status(), Status::Stopped);
    std::thread::sleep(Duration::from_millis(300));
    assert!(!alive(pid), "backend still running after stop");
    let _ = std::fs::remove_dir_all(root);
}
