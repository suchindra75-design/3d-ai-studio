"""Builds the local engine that release desktop builds carry.

    pnpm bundle:engine                      # apps/desktop/src-tauri/resources/backend
    python infrastructure/scripts/bundle_engine.py --with-open3d --out DIR

The result is a relocatable CPython (uv's python-build-standalone build) with
the backend and its locked dependencies installed into it, byte-compiled, plus
a licence list of everything inside. The desktop app runs it as
``python -I -m ai3d.cli``. It is built for the machine it runs on (OS and CPU);
release CI builds it once per platform.

Only the Python standard library is used here; ``uv`` must be on PATH.
"""

import argparse
import compileall
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "apps/desktop/src-tauri/resources/backend"
PYTHON_VERSION = "3.11"
WINDOWS = sys.platform == "win32"

# Standard-library parts the engine never uses (GUI toolkits, IDLE, test suite).
STDLIB_UNUSED = ("idlelib", "tkinter", "turtledemo", "ensurepip", "test", "lib2to3")

LICENSE_SCRIPT = r"""
import json, sys
from importlib import metadata
out = []
for dist in sorted(metadata.distributions(), key=lambda d: d.metadata["Name"].lower()):
    meta = dist.metadata
    licence = meta.get("License-Expression") or ""
    if not licence:
        raw = (meta.get("License") or "").strip()
        licence = raw if raw and len(raw) < 80 and "\n" not in raw else ""
    if not licence:
        classifiers = [c.split(" :: ")[-1] for c in meta.get_all("Classifier") or []
                       if c.startswith("License ::")]
        licence = "; ".join(classifiers)
    texts = []
    for file in dist.files or []:
        name = file.name.upper()
        licence_file = name.startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE"))
        if licence_file and file.parent.name.endswith((".dist-info", "licenses")):
            try:
                texts.append(file.read_text())
            except (OSError, UnicodeDecodeError):
                pass
    out.append({"name": meta["Name"], "version": dist.version,
                "license": licence or "UNKNOWN", "texts": texts})
json.dump(out, sys.stdout)
"""


def run(args: list[str], cwd: Path | None = None) -> str:
    print("+", " ".join(args), flush=True)
    # Without VIRTUAL_ENV: `uv run` sets it, and uv would then pick that environment.
    env = {k: v for k, v in os.environ.items() if k not in {"VIRTUAL_ENV", "CONDA_PREFIX"}}
    return subprocess.run(  # noqa: S603
        args, check=True, text=True, capture_output=True, cwd=cwd, env=env
    ).stdout


def bundled_python(prefix: Path) -> Path:
    return prefix / "python.exe" if WINDOWS else prefix / "bin" / "python3"


def stdlib_dir(prefix: Path) -> Path:
    return prefix / "Lib" if WINDOWS else prefix / "lib" / f"python{PYTHON_VERSION}"


def copy_python(dest: Path) -> None:
    """Copies uv's standalone CPython (installs it first if needed)."""
    run(["uv", "python", "install", PYTHON_VERSION])
    found = Path(
        run(["uv", "python", "find", "--managed-python", "--system", PYTHON_VERSION]).strip()
    )
    probe = "import sys; print(sys.prefix); print(sys.base_prefix)"
    prefix, base = (Path(p).resolve() for p in run([str(found), "-c", probe]).splitlines())
    if prefix != base or (not WINDOWS and not (prefix / "BUILD").exists()):
        # A virtualenv or a system Python would copy the wrong, non-relocatable files.
        raise SystemExit(f"not uv's standalone CPython: {found} (prefix {prefix})")
    # Symlinks are copied as files: app bundles and installers handle plain files best.
    shutil.copytree(prefix, dest, symlinks=False)
    stdlib = stdlib_dir(dest)
    (stdlib / "EXTERNALLY-MANAGED").unlink(missing_ok=True)
    for name in STDLIB_UNUSED:
        shutil.rmtree(stdlib / name, ignore_errors=True)
    # Tcl/Tk (only tkinter uses them).
    patterns = ("tcl*", "tk*", "itcl*", "thread*", "libtcl*", "libtk*")
    for pattern in patterns if not WINDOWS else ():
        for path in (dest / "lib").glob(pattern):
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()


def install_engine(python: Path, extras: list[str], work: Path) -> None:
    requirements = work / "requirements.txt"
    export = ["uv", "export", "--frozen", "--no-dev", "--all-packages", "--no-emit-workspace"]
    export += [arg for extra in extras for arg in ("--extra", extra)]
    run([*export, "--output-file", str(requirements)], cwd=ROOT)
    # Locked versions, verified against the lockfile's hashes.
    common = ["uv", "pip", "install", "--python", str(python), "--no-cache"]
    run([*common, "--require-hashes", "--no-deps", "-r", str(requirements)], cwd=ROOT)
    run([*common, "--no-deps", "./local-engine", "./backend"], cwd=ROOT)
    # Installer tools that come with CPython; nothing in the engine uses them.
    run(["uv", "pip", "uninstall", "--python", str(python), "pip", "setuptools"], cwd=ROOT)
    run(["uv", "pip", "check", "--python", str(python)], cwd=ROOT)


def strip_and_compile(prefix: Path) -> None:
    for cache in list(prefix.rglob("__pycache__")):
        shutil.rmtree(cache, ignore_errors=True)
    # The app bundle is read-only, so Python could never cache byte-code itself.
    ok = compileall.compile_dir(
        prefix, quiet=1, workers=0, legacy=False, stripdir=str(prefix), prependdir="/"
    )
    if not ok:
        print("warning: some files did not byte-compile (usually harmless test files)")


def write_licences(python: Path, out: Path) -> int:
    packages = json.loads(run([str(python), "-I", "-c", LICENSE_SCRIPT]))
    lines = [
        "Third-party software in the AI 3D Studio local engine",
        "=" * 54,
        "",
        f"CPython {PYTHON_VERSION} (python-build-standalone): PSF-2.0, with the licences",
        "of its bundled libraries in python/share and python/lib (see BUILD info).",
        "",
    ]
    lines += [f"{p['name']} {p['version']}: {p['license']}" for p in packages]
    lines += ["", "Full licence texts", "=" * 18]
    for p in packages:
        for text in p["texts"]:
            lines += ["", f"--- {p['name']} {p['version']} ---", text.strip()]
    (out / "THIRD_PARTY_LICENSES.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    unknown = [p["name"] for p in packages if p["license"] == "UNKNOWN"]
    if unknown:
        print("warning: no licence metadata for:", ", ".join(unknown))
    return len(packages)


def smoke_test(python: Path) -> dict[str, object]:
    """Starts the bundled engine with a token and checks it answers."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    token = "bundle-smoke-test-" + os.urandom(8).hex()
    with tempfile.TemporaryDirectory() as data:
        env = {
            "AI3D_DATA_DIR": data,
            "AI3D_API_TOKEN": token,
            "AI3D_LOG_LEVEL": "WARNING",
            "PATH": os.environ.get("PATH", ""),
        }
        if WINDOWS:
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
        args = [str(python), "-I", "-m", "ai3d.cli", "--host", "127.0.0.1", "--port", str(port)]
        process = subprocess.Popen(args, cwd=data, env=env)  # noqa: S603
        base = f"http://127.0.0.1:{port}/api/v1"
        try:
            deadline = time.monotonic() + 120
            while True:
                try:
                    with urllib.request.urlopen(f"{base}/health", timeout=2):  # noqa: S310
                        break
                except OSError:  # refused, reset or timed out while starting
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise SystemExit("the bundled engine did not start") from None
                    time.sleep(0.5)
            request = urllib.request.Request(  # noqa: S310
                f"{base}/exports/formats", headers={"Authorization": f"Bearer {token}"}
            )
            # The first call checks Blender and USD (several seconds on a cold start).
            with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
                formats = json.load(response)["items"]
            return {f["format"]: f["available"] for f in formats}
        finally:
            process.terminate()
            process.wait(timeout=30)


def size_of(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--without-usdz", action="store_true", help="leave out Pixar USD (~250 MB)")
    parser.add_argument(
        "--with-open3d", action="store_true", help="add Open3D point filtering (~290 MB)"
    )
    parser.add_argument("--skip-smoke-test", action="store_true")
    parser.add_argument(
        "--smoke-test-only", action="store_true", help="test an existing bundle in --out"
    )
    options = parser.parse_args()

    extras = [] if options.without_usdz else ["usdz"]
    if options.with_open3d:
        extras.append("reconstruction")
    out: Path = options.out.resolve()
    if options.smoke_test_only:
        print("exports:", smoke_test(bundled_python(out / "python")))
        return
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    prefix = out / "python"
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as work:
        copy_python(prefix)
        python = bundled_python(prefix)
        install_engine(python, extras, Path(work))
    strip_and_compile(prefix)
    count = write_licences(python, out)
    manifest: dict[str, object] = {
        "python": PYTHON_VERSION,
        "platform": sys.platform,
        "extras": extras,
        "entry": ["-I", "-m", "ai3d.cli"],
        "packages": count,
    }
    if not options.skip_smoke_test:
        manifest["exports"] = smoke_test(python)
        print("smoke test: engine answered; exports", manifest["exports"])
    size = manifest["size_bytes"] = size_of(out)
    (out / "bundle.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(
        f"bundled engine: {out} ({size / 1e6:.0f} MB, {count} packages, "
        f"{time.monotonic() - started:.0f} s)"
    )


if __name__ == "__main__":
    main()
