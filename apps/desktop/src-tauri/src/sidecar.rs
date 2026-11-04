//! The desktop app's private backend ("sidecar").
//!
//! The app starts the AI 3D Studio backend itself: on 127.0.0.1, on a random
//! free port, with a fresh random token that only this app's window receives.
//! Other programs and web pages on the machine can reach the port but get 401
//! without the token. The sidecar is restarted if it crashes (a few times) and
//! stopped gracefully when the app quits, so running jobs are handed back for
//! recovery instead of being cut off mid-write.
//!
//! This module has no Tauri dependency so it can be tested on its own.

use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde::Serialize;

/// Log files larger than this are truncated when the sidecar starts.
const MAX_LOG_BYTES: u64 = 5 * 1024 * 1024;
const LOG_TAIL_LINES: usize = 30;

#[derive(Clone, Debug)]
pub struct SidecarConfig {
    /// The `ai3d-server` executable, or the bundled Python interpreter.
    pub program: PathBuf,
    /// Arguments before the server's own (`-I -m ai3d.cli` for the bundled Python).
    pub args: Vec<String>,
    /// Working directory (development builds use the repository, for its `.env`).
    pub working_dir: PathBuf,
    /// Where the backend keeps projects, models and its database.
    pub data_dir: PathBuf,
    pub log_path: PathBuf,
    pub startup_timeout: Duration,
    pub max_restarts: u32,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(tag = "state", rename_all = "snake_case")]
pub enum Status {
    Starting,
    Running {
        base_url: String,
        port: u16,
        token: String,
    },
    Failed {
        message: String,
        log_tail: Vec<String>,
    },
    Stopped,
}

struct Inner {
    config: SidecarConfig,
    status: Mutex<Status>,
    child: Mutex<Option<Child>>,
    stopping: AtomicBool,
}

#[derive(Clone)]
pub struct Sidecar {
    inner: Arc<Inner>,
}

/// 256 random bits as 64 hex characters.
pub fn new_token() -> String {
    let mut bytes = [0u8; 32];
    getrandom::getrandom(&mut bytes).expect("the operating system has no random source");
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// A port that was free a moment ago (the backend binds it right after).
pub fn free_port() -> std::io::Result<u16> {
    Ok(TcpListener::bind(("127.0.0.1", 0))?.local_addr()?.port())
}

/// The last lines of the backend log, for error messages.
pub fn log_tail(path: &Path, lines: usize) -> Vec<String> {
    let Ok(file) = File::open(path) else {
        return Vec::new();
    };
    let all: Vec<String> = BufReader::new(file).lines().map_while(Result::ok).collect();
    all[all.len().saturating_sub(lines)..].to_vec()
}

fn open_log(path: &Path) -> std::io::Result<File> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let too_big = fs::metadata(path)
        .map(|m| m.len() > MAX_LOG_BYTES)
        .unwrap_or(false);
    OpenOptions::new()
        .create(true)
        .append(!too_big)
        .write(true)
        .truncate(too_big)
        .open(path)
}

/// True once `GET /api/v1/health` answers 200.
pub fn healthy(port: u16) -> bool {
    ureq::get(&format!("http://127.0.0.1:{port}/api/v1/health"))
        .timeout(Duration::from_secs(2))
        .call()
        .map(|r| r.status() == 200)
        .unwrap_or(false)
}

fn terminate(child: &mut Child) {
    #[cfg(unix)]
    {
        // SIGTERM lets uvicorn shut down: the job runner hands jobs back for recovery.
        // SAFETY: plain kill(2) on our own child's pid.
        unsafe {
            libc::kill(child.id() as libc::pid_t, libc::SIGTERM);
        }
        let deadline = Instant::now() + Duration::from_secs(10);
        while Instant::now() < deadline {
            if let Ok(Some(_)) = child.try_wait() {
                return;
            }
            thread::sleep(Duration::from_millis(100));
        }
    }
    let _ = child.kill();
    let _ = child.wait();
}

impl Sidecar {
    pub fn new(config: SidecarConfig) -> Self {
        Self {
            inner: Arc::new(Inner {
                config,
                status: Mutex::new(Status::Starting),
                child: Mutex::new(None),
                stopping: AtomicBool::new(false),
            }),
        }
    }

    /// A sidecar that cannot start, with the reason (e.g. no backend in this build).
    pub fn unavailable(config: SidecarConfig, message: String) -> Self {
        let sidecar = Self::new(config);
        sidecar.set(Status::Failed {
            message,
            log_tail: Vec::new(),
        });
        sidecar
    }

    pub fn config(&self) -> &SidecarConfig {
        &self.inner.config
    }

    pub fn status(&self) -> Status {
        self.inner.status.lock().unwrap().clone()
    }

    fn set(&self, status: Status) {
        *self.inner.status.lock().unwrap() = status;
    }

    /// Starts the backend and keeps it running in a background thread.
    pub fn start(&self) {
        self.inner.stopping.store(false, Ordering::SeqCst);
        self.set(Status::Starting);
        let this = self.clone();
        thread::Builder::new()
            .name("ai3d-sidecar".into())
            .spawn(move || this.supervise())
            .expect("could not start the sidecar thread");
    }

    /// Blocks until the backend runs or fails (for tests and the CLI).
    pub fn wait_ready(&self, timeout: Duration) -> Status {
        let deadline = Instant::now() + timeout;
        loop {
            let status = self.status();
            if !matches!(status, Status::Starting) || Instant::now() > deadline {
                return status;
            }
            thread::sleep(Duration::from_millis(100));
        }
    }

    fn supervise(&self) {
        let config = &self.inner.config;
        let mut restarts = 0;
        loop {
            match self.launch() {
                Ok((port, token)) => {
                    self.set(Status::Running {
                        base_url: format!("http://127.0.0.1:{port}"),
                        port,
                        token,
                    });
                    restarts = 0;
                    self.watch();
                }
                Err(message) => {
                    if self.inner.stopping.load(Ordering::SeqCst) {
                        self.set(Status::Stopped);
                        return;
                    }
                    restarts += 1;
                    if restarts > config.max_restarts {
                        self.set(Status::Failed {
                            message,
                            log_tail: log_tail(&config.log_path, LOG_TAIL_LINES),
                        });
                        return;
                    }
                }
            }
            if self.inner.stopping.load(Ordering::SeqCst) {
                self.set(Status::Stopped);
                return;
            }
            // The backend exited by itself: start it again after a short pause.
            self.set(Status::Starting);
            thread::sleep(Duration::from_millis(500 * u64::from(restarts.max(1))));
        }
    }

    /// Spawns the backend and waits until it answers.
    fn launch(&self) -> Result<(u16, String), String> {
        let config = &self.inner.config;
        let port = free_port().map_err(|e| format!("No free local port: {e}"))?;
        let token = new_token();
        fs::create_dir_all(&config.data_dir)
            .map_err(|e| format!("Cannot create {}: {e}", config.data_dir.display()))?;
        let log = open_log(&config.log_path)
            .map_err(|e| format!("Cannot write {}: {e}", config.log_path.display()))?;
        let log_err = log.try_clone().map_err(|e| e.to_string())?;
        let child = Command::new(&config.program)
            .args(&config.args)
            .args(["--host", "127.0.0.1", "--port", &port.to_string()])
            .current_dir(&config.working_dir)
            .env("AI3D_API_TOKEN", &token)
            .env("AI3D_DATA_DIR", &config.data_dir)
            .env("AI3D_HOST", "127.0.0.1")
            .env("AI3D_LOG_FORMAT", "json")
            // The backend exits by itself if this app disappears without a clean quit.
            .env("AI3D_PARENT_PID", std::process::id().to_string())
            .stdin(Stdio::null())
            .stdout(log)
            .stderr(log_err)
            .spawn()
            .map_err(|e| format!("Could not start {}: {e}", config.program.display()))?;
        *self.inner.child.lock().unwrap() = Some(child);

        let deadline = Instant::now() + config.startup_timeout;
        while Instant::now() < deadline {
            if self.inner.stopping.load(Ordering::SeqCst) {
                return Err("Stopped while starting.".into());
            }
            if let Some(child) = self.inner.child.lock().unwrap().as_mut() {
                if let Ok(Some(code)) = child.try_wait() {
                    return Err(format!("The local engine exited during start-up ({code})."));
                }
            }
            if healthy(port) {
                return Ok((port, token));
            }
            thread::sleep(Duration::from_millis(250));
        }
        if let Some(mut child) = self.inner.child.lock().unwrap().take() {
            terminate(&mut child);
        }
        Err(format!(
            "The local engine did not answer within {} s.",
            config.startup_timeout.as_secs()
        ))
    }

    /// Waits while the backend runs; returns when it exits or is stopped.
    fn watch(&self) {
        loop {
            {
                let mut guard = self.inner.child.lock().unwrap();
                match guard.as_mut().map(Child::try_wait) {
                    Some(Ok(None)) => {}
                    _ => {
                        guard.take();
                        return;
                    }
                }
            }
            thread::sleep(Duration::from_millis(500));
        }
    }

    /// Stops the backend gracefully (SIGTERM, then kill after 10 s).
    pub fn stop(&self) {
        self.inner.stopping.store(true, Ordering::SeqCst);
        let child = self.inner.child.lock().unwrap().take();
        if let Some(mut child) = child {
            terminate(&mut child);
        }
        self.set(Status::Stopped);
    }

    pub fn restart(&self) {
        self.stop();
        self.start();
    }

    /// Process id of the running backend (tests and diagnostics).
    pub fn pid(&self) -> Option<u32> {
        self.inner.child.lock().unwrap().as_ref().map(Child::id)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tokens_are_long_random_hex() {
        let a = new_token();
        let b = new_token();
        assert_eq!(a.len(), 64);
        assert!(a.chars().all(|c| c.is_ascii_hexdigit()));
        assert_ne!(a, b);
    }

    #[test]
    fn free_ports_are_bindable() {
        let port = free_port().unwrap();
        assert!(port > 0);
        TcpListener::bind(("127.0.0.1", port)).unwrap();
    }

    #[test]
    fn log_tail_returns_the_last_lines() {
        let dir = std::env::temp_dir().join(format!("ai3d-tail-{}", new_token()));
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("backend.log");
        fs::write(
            &path,
            (1..=50).map(|n| format!("line {n}\n")).collect::<String>(),
        )
        .unwrap();
        let tail = log_tail(&path, 3);
        assert_eq!(tail, vec!["line 48", "line 49", "line 50"]);
        assert!(log_tail(&dir.join("missing.log"), 3).is_empty());
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn a_missing_program_fails_with_a_reason() {
        let dir = std::env::temp_dir().join(format!("ai3d-missing-{}", new_token()));
        let sidecar = Sidecar::new(SidecarConfig {
            program: dir.join("no-such-server"),
            args: Vec::new(),
            working_dir: std::env::temp_dir(),
            data_dir: dir.join("data"),
            log_path: dir.join("backend.log"),
            startup_timeout: Duration::from_secs(5),
            max_restarts: 1,
        });
        sidecar.start();
        match sidecar.wait_ready(Duration::from_secs(10)) {
            Status::Failed { message, .. } => assert!(message.contains("Could not start")),
            other => panic!("expected a failure, got {other:?}"),
        }
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn status_serializes_with_a_state_tag() {
        let json = serde_json::to_value(Status::Running {
            base_url: "http://127.0.0.1:1".into(),
            port: 1,
            token: "t".into(),
        })
        .unwrap();
        assert_eq!(json["state"], "running");
        assert_eq!(json["base_url"], "http://127.0.0.1:1");
    }
}
