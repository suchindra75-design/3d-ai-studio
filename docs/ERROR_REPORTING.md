# Error reporting (optional)

AI 3D Studio sends **nothing** anywhere unless you set it up. For a
self-hosted server you can have the backend report unexpected errors to a
Sentry-compatible service that **you** choose: sentry.io, a self-hosted
Sentry, or GlitchTip.

```bash
uv sync --all-packages --extra errors
AI3D_SENTRY_DSN=https://<key>@<your-sentry-host>/<project> ai3d-server
```

The desktop app never turns this on.

## What is sent

**Sent:**
- Unexpected errors (log records at ERROR level) with their stack traces:
  file names, line numbers and function names.
- Up to a few warning messages logged just before an error.
- The app version, the environment (`production`, …) and the Python version.

**Never sent:**
- Request data: URLs, query strings (which can hold the desktop app's token),
  headers, cookies and bodies. The SDK's web-framework integrations are off,
  so this data is not collected at all.
- Local variables, source code, your host name and user identity.
- Photos, models or any file content.
- Performance traces and session tracking.

Every string is also scrubbed before it leaves:
- token, key and `Bearer` values become `[removed]`;
- your home-folder path becomes `~`.

`backend/tests/test_error_reporting.py` checks all of this against a captured
event.

Error messages can still contain project-related text, such as a file name
inside a path. If that is unacceptable for your installation, leave error
reporting off.
