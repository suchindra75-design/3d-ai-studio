//! Saving files from the private backend to a place the user chooses.
//!
//! The web view cannot write files and cannot fetch arbitrary URLs through this
//! module: it may only name a backend download path from a short allow-list,
//! and the destination always comes from the operating system's Save dialog.

use std::fs::{self, File};
use std::io;
use std::path::{Path, PathBuf};
use std::time::Duration;

/// Backend paths the window may save (models, exports, photos).
const ALLOWED_PREFIXES: [&str; 3] = [
    "/api/v1/model-versions/",
    "/api/v1/exports/",
    "/api/v1/images/",
];

/// True for backend download paths the window may save. No queries, no `..`.
pub fn allowed_download_path(path: &str) -> bool {
    ALLOWED_PREFIXES
        .iter()
        .any(|prefix| path.starts_with(prefix))
        && !path.contains("..")
        && path
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '/' | '-' | '_' | '.'))
}

/// A file name for the Save dialog: no directories, no odd characters.
pub fn safe_file_name(name: &str) -> String {
    let base = name.rsplit(['/', '\\']).next().unwrap_or_default();
    let cleaned: String = base
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.') {
                c
            } else {
                '-'
            }
        })
        .collect();
    let trimmed = cleaned.trim_matches(|c| c == '.' || c == '-');
    if trimmed.is_empty() {
        "model".into()
    } else {
        trimmed.chars().take(120).collect()
    }
}

/// Downloads `base_url + path` with the token and writes it atomically to `dest`.
pub fn download(base_url: &str, token: &str, path: &str, dest: &Path) -> Result<u64, String> {
    if !allowed_download_path(path) {
        return Err(format!("Not a downloadable backend path: {path}"));
    }
    let response = ureq::get(&format!("{base_url}{path}"))
        .set("Authorization", &format!("Bearer {token}"))
        .timeout(Duration::from_secs(600))
        .call()
        .map_err(|e| match e {
            ureq::Error::Status(code, _) => format!("The local engine answered {code}."),
            other => format!("Could not reach the local engine: {other}"),
        })?;
    let partial: PathBuf = dest.with_extension(format!(
        "{}.part",
        dest.extension()
            .and_then(|e| e.to_str())
            .unwrap_or("download")
    ));
    let written = (|| -> io::Result<u64> {
        let mut file = File::create(&partial)?;
        let n = io::copy(&mut response.into_reader(), &mut file)?;
        file.sync_all()?;
        fs::rename(&partial, dest)?;
        Ok(n)
    })();
    written.map_err(|e| {
        let _ = fs::remove_file(&partial);
        format!("Could not save {}: {e}", dest.display())
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_backend_download_paths_are_allowed() {
        assert!(allowed_download_path("/api/v1/model-versions/0b7e/file"));
        assert!(allowed_download_path("/api/v1/exports/12ab-cd/file"));
        assert!(allowed_download_path("/api/v1/images/9f/file"));
        for bad in [
            "/api/v1/projects",
            "/api/v1/exports/../../etc/passwd",
            "/api/v1/exports/x/file?access_token=t",
            "http://evil.example/api/v1/exports/x/file",
            "/api/v1/exports/x/file#frag",
            "api/v1/exports/x/file",
            "/api/v1/exports/x y",
        ] {
            assert!(!allowed_download_path(bad), "{bad} should be refused");
        }
    }

    #[test]
    fn file_names_cannot_choose_a_directory() {
        assert_eq!(safe_file_name("mug-v2.glb"), "mug-v2.glb");
        assert_eq!(
            safe_file_name("../../.ssh/authorized_keys"),
            "authorized_keys"
        );
        assert_eq!(safe_file_name("C:\\Windows\\x.dll"), "x.dll");
        assert_eq!(safe_file_name("Tasse · v1.glb"), "Tasse---v1.glb");
        assert_eq!(safe_file_name(""), "model");
        assert_eq!(safe_file_name(".."), "model");
    }

    #[test]
    fn refused_paths_never_reach_the_network() {
        let dest = std::env::temp_dir().join("ai3d-never-written.bin");
        let err = download("http://127.0.0.1:9", "t", "/etc/passwd", &dest).unwrap_err();
        assert!(err.contains("Not a downloadable backend path"));
        assert!(!dest.exists());
    }
}
