# Releasing

## What a release contains

| Artifact | Built by | Notes |
|---|---|---|
| Desktop app with its own local engine (`.dmg` for macOS Apple Silicon and Intel; `.AppImage`/`.deb` for Linux; `.msi`/`.exe` for Windows) | `.github/workflows/release.yml` → `desktop` | Draft GitHub release; a maintainer checks and publishes it |
| `THIRD_PARTY_LICENSES-<os>-<arch>.txt` | same | Licences of everything inside the bundled engine |
| Container images `ghcr.io/<owner>/<repo>-backend` and `-web` | `release.yml` → `images` | Pushed only for version tags |

## Steps

1. Update `CHANGELOG.md` and the version everywhere it appears:
   `grep -rl '"0.1.0"\|= "0.1.0"' --include=package.json --include=pyproject.toml --include=Cargo.toml --include=tauri.conf.json --include=__init__.py . | grep -v node_modules`
   (root, `apps/*`, `packages/*`, `backend`, `local-engine`, the Tauri config
   and `Cargo.toml`), then `uv lock` and `pnpm install`.
2. Run every check ([TESTING.md](TESTING.md)), including the optional suites
   (COLMAP, Blender, PostgreSQL, S3).
3. Tag and push: `git tag v0.2.0 && git push origin v0.2.0`.
4. When the workflow finishes, install each desktop build on a clean machine:
   the app must start its engine (start-up screen → Projects), create a project,
   upload a photo and export a model. Then publish the draft release.

## The bundled engine

`pnpm build:desktop-app` runs `pnpm bundle:engine` and then `tauri build` with
`apps/desktop/src-tauri/tauri.bundle-engine.conf.json`.

`pnpm bundle:engine` (`infrastructure/scripts/bundle_engine.py`) builds
`apps/desktop/src-tauri/resources/backend/` (git-ignored):

- **CPython 3.11** from python-build-standalone (uv's relocatable build).
  Its GUI toolkit, IDLE, test suite, pip and setuptools are removed.
- **The backend and its locked dependencies.** They are installed from
  `uv.lock` with hash checking. The `usdz` extra is included unless you pass
  `--without-usdz`. Open3D is left out unless you pass `--with-open3d` (about
  290 MB); without it, point clouds are not filtered.
- **Byte-compiled code.** The app bundle is read-only, so Python cannot cache
  byte-code itself.
- **`THIRD_PARTY_LICENSES.txt`** (package, version, licence and full licence
  texts) and **`bundle.json`** (contents, size and smoke-test result).
- **A smoke test.** The engine is started with a token and must answer and
  list its export formats.

The app runs `python -I -m ai3d.cli` from its data folder. `-I` ignores
`PYTHON*` environment variables and the user's site-packages.

Measured on an Intel Mac (macOS 26): the engine is 674 MB (45 packages), the
`.app` is 670 MB and the `.dmg` is 184 MB. The first launch took about 44 s
(macOS checks the new libraries); later launches took about 10 s until projects
were listed.

Not bundled: COLMAP and Blender. Users install them separately (see
[LOCAL_GPU_SETUP.md](LOCAL_GPU_SETUP.md)); the app finds them in their usual
locations. Blender is GPL software; it runs as a separate program and is never
distributed with the app.

## Licences to check before each release

The engine's licence list is generated from package metadata. Read it for
changes. As of 0.1.0, everything is permissive (MIT, BSD, Apache-2.0, PSF,
MIT-CMU, 0BSD, Zlib, CC0), plus **usd-core under TOST-1.0** (Apache-2.0 with
modifications, which requires its notice; included). No GPL code is bundled.
Model weights are never bundled ([AI_PIPELINE.md](AI_PIPELINE.md)).

## Signing

Unsigned builds work, but:

- macOS shows *"cannot be opened because the developer cannot be verified"*:
  right-click → Open, or
  `xattr -dr com.apple.quarantine "/Applications/AI 3D Studio.app"`;
- Windows SmartScreen warns about an unknown publisher.

To sign macOS builds, add the `APPLE_*` secrets listed in `release.yml`; Tauri
signs and notarizes with them. **Not verified yet:** notarization requires
every Mach-O file inside the app to be signed with the hardened runtime,
including the engine's `.so`/`.dylib` files in `Contents/Resources/backend`,
and Tauri may not sign resources. Expect to add a `codesign` step for them the
first time a signing certificate is available. Windows signing is not
configured.

## Building locally

```bash
pnpm build:desktop-app
```

On macOS, the DMG step asks Finder to lay out the window. That needs
*Automation* permission for your terminal. Without it, the step times out
("AppleEvent timed out"). Use `CI=true pnpm build:desktop-app` to skip the
layout.
