# Security policy

## Supported versions

AI 3D Studio is pre-1.0. Security fixes are made on the `main` branch and
included in the next release.

## Reporting a vulnerability

Please **do not** open a public issue. Use GitHub's private vulnerability
reporting ("Security" tab → "Report a vulnerability") on the project
repository. Include affected versions, reproduction steps and impact. You can
expect an acknowledgement within a week; we will coordinate a fix and
disclosure date with you.

## Security model (summary)

Details: [docs/ARCHITECTURE.md §7](docs/ARCHITECTURE.md#7-security-model).

- The backend binds to `127.0.0.1` by default and allows CORS only from the
  web and desktop app origins. It has **no authentication** in this phase:
  do not expose it to untrusted networks.
- Provider API keys are server-side only; they are never returned by the API,
  logged, or included in exports.
- Uploads are validated by content (JPEG/PNG/WebP), size- and pixel-limited,
  and stored under server-generated keys; user filenames are display-only.
- Storage keys are canonicalised and confined to the storage root.
- Images are sent to cloud providers only when a project allows it and the
  job is consented; EXIF/GPS metadata is stripped from those copies.
- The desktop shell exposes a fixed set of Tauri commands, no shell/fs/http
  plugins, and a strict Content Security Policy. External tools are run only
  from an allow-list with argument lists built in code (never `shell=True`).
