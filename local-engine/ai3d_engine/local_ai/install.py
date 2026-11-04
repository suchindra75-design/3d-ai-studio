"""Installing the local AI engine (TripoSR) into its own folder.

    ai3d-local-ai status
    ai3d-local-ai install [--dir DIR] [--yes] [--skip-test]
    ai3d-local-ai uninstall [--dir DIR]

Everything goes into one folder (default ~/.local/share/ai3d-studio/tools/local-ai):
its own Python environment made with uv, the pinned TripoSR source, and the
model files. Every download is checked against the SHA-256 in ``manifest.py``.
``engine.json`` is written last, after a real test generation; until then the
app treats the engine as not installed. Deleting the folder removes it.
"""

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from ai3d_engine.local_ai import engine, manifest

_CHUNK = 1024 * 1024


class InstallError(RuntimeError):
    pass


def _say(message: str) -> None:
    print(message, flush=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def download(artifact: manifest.Artifact, root: Path) -> Path:
    """Downloads one file (resumable by re-running) and checks its checksum."""
    target = root / artifact.path
    if target.is_file() and _sha256(target) == artifact.sha256:
        _say(f"  ✓ {artifact.name} (already downloaded)")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    request = urllib.request.Request(  # noqa: S310 - fixed https URLs from the manifest
        artifact.url, headers={"User-Agent": "ai3d-studio-local-ai-installer"}
    )
    digest = hashlib.sha256()
    received = 0
    shown = -1
    with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as out:  # noqa: S310
        total = int(response.headers.get("Content-Length") or artifact.size or 0)
        while chunk := response.read(_CHUNK):
            out.write(chunk)
            digest.update(chunk)
            received += len(chunk)
            if total:
                percent = received * 100 // total
                if percent // 10 != shown:
                    shown = percent // 10
                    _say(f"    {artifact.name}: {percent}% of {total / 1e6:,.0f} MB")
    if digest.hexdigest() != artifact.sha256:
        partial.unlink(missing_ok=True)
        raise InstallError(
            f"{artifact.name}: checksum mismatch (expected {artifact.sha256[:12]}…, got "
            f"{digest.hexdigest()[:12]}…). The file was deleted; nothing was installed."
        )
    partial.replace(target)
    _say(f"  ✓ {artifact.name} ({received / 1e6:,.1f} MB, checksum verified)")
    return target


def _extract_source(archive: Path, root: Path) -> str:
    """Extracts the TripoSR source; returns its folder relative to ``root``."""
    folder = f"src/TripoSR-{manifest.TRIPOSR_COMMIT}"
    destination = root / folder
    if destination.is_dir():
        shutil.rmtree(destination)
    with tempfile.TemporaryDirectory(dir=root) as scratch, tarfile.open(archive) as tar:
        tar.extractall(scratch, filter="data")  # refuses absolute paths, links out, devices
        extracted = Path(scratch) / f"TripoSR-{manifest.TRIPOSR_COMMIT}"
        if not (extracted / "tsr" / "system.py").is_file():
            raise InstallError("The TripoSR source archive does not have the expected layout.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(extracted), destination)
    return folder


def _uv() -> str:
    found = shutil.which("uv")
    if found is None:
        raise InstallError(
            "Installing the local AI engine needs uv (https://docs.astral.sh/uv/), which "
            "creates its Python environment. Install uv and run this again."
        )
    return found


def _create_environment(root: Path) -> None:
    uv = _uv()
    env_dir = root / "env"
    if env_dir.exists():
        shutil.rmtree(env_dir)
    _say(f"Creating the engine's Python {manifest.PYTHON_VERSION} environment…")
    subprocess.run(  # noqa: S603 - uv with code-built arguments
        [
            uv,
            "venv",
            "--quiet",
            "--python",
            manifest.PYTHON_VERSION,
            "--managed-python",
            str(env_dir),
        ],
        check=True,
    )
    _say("Installing PyTorch and the other engine packages (about 1 GB)…")
    subprocess.run(  # noqa: S603
        [
            uv,
            "pip",
            "install",
            "--quiet",
            "--python",
            str(engine.env_python(root)),
            *manifest.REQUIREMENTS,
        ],
        check=True,
    )


def _environment_versions(root: Path) -> dict[str, str]:
    probe = (
        "import importlib.metadata as m, json, platform; "
        "print(json.dumps({'python': platform.python_version(), "
        "**{d: m.version(d) for d in ('torch', 'numpy', 'transformers', 'onnxruntime')}}))"
    )
    output = subprocess.run(  # noqa: S603
        [str(engine.env_python(root)), "-I", "-c", probe],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    versions: dict[str, str] = json.loads(output)
    return versions


def _test_image(path: Path) -> None:
    """A small synthetic object on a plain background (no real photo is shipped)."""
    from PIL import Image, ImageDraw  # noqa: PLC0415 - Pillow comes with the backend

    image = Image.new("RGB", (512, 512), (235, 235, 235))
    draw = ImageDraw.Draw(image)
    draw.ellipse((136, 120, 376, 400), fill=(200, 60, 40), outline=(90, 20, 10), width=6)
    draw.rectangle((226, 70, 286, 140), fill=(70, 120, 60))
    image.save(path)


def install(root: Path, *, skip_test: bool = False) -> dict[str, object]:
    root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(root).free
    if free < manifest.REQUIRED_DISK_BYTES:
        raise InstallError(
            f"Not enough disk space in {root}: {free / 1e9:.1f} GB free, about "
            f"{manifest.REQUIRED_DISK_BYTES / 1e9:.0f} GB needed."
        )
    (root / "engine.json").unlink(missing_ok=True)  # "not installed" until the end
    started = time.monotonic()
    _create_environment(root)
    _say("Downloading the TripoSR source code and model files (about 1.9 GB)…")
    archive = download(manifest.SOURCE, root)
    source_dir = _extract_source(archive, root)
    archive.unlink()
    for artifact in manifest.WEIGHTS:
        download(artifact, root)

    record: dict[str, object] = {
        "engine": manifest.ENGINE_KEY,
        "manifest_version": manifest.MANIFEST_VERSION,
        "source_dir": source_dir,
        "installed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "platform": f"{sys.platform}-{platform.machine()}",
        "versions": _environment_versions(root),
        "artifacts": [
            {"name": a.name, "sha256": a.sha256, "licence": a.licence, "source": a.source}
            for a in (manifest.SOURCE, *manifest.WEIGHTS)
        ],
    }
    # A provisional record lets the test use the normal code path; it is removed on failure.
    (root / "engine.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    if not skip_test:
        _say("Testing the engine with a small generation (about a minute on a CPU)…")
        try:
            with tempfile.TemporaryDirectory() as scratch:
                image = Path(scratch) / "test.png"
                _test_image(image)
                result = engine.generate(image, Path(scratch) / "run", resolution=64)
        except Exception:
            (root / "engine.json").unlink(missing_ok=True)
            raise
        record["test"] = {
            "device": result["device"],
            "seconds": result["seconds"],
            "triangles": result["triangles"],
        }
        (root / "engine.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    _say(f"Installed in {time.monotonic() - started:.0f} s: {root}")
    return record


def _summary(root: Path) -> str:
    lines = [
        f"This installs the local AI engine ({manifest.ENGINE_NAME}) into {root}:",
        "  • its own Python environment with PyTorch (about 1 GB, from PyPI via uv)",
    ]
    for a in (manifest.SOURCE, *manifest.WEIGHTS):
        size = f", {a.size / 1e6:,.0f} MB" if a.size > 1_000_000 else ""
        lines.append(f"  • {a.name}{size}: {a.licence}, from {a.source}")
    lines.append(
        "Nothing is sent anywhere; photos are processed on this computer. Results are "
        "AI-generated: hidden sides are invented, sizes are not measured."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ai3d-local-ai", description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["status", "install", "uninstall"])
    parser.add_argument("--dir", type=Path, default=None, help="engine folder")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    parser.add_argument("--skip-test", action="store_true")
    options = parser.parse_args(argv)
    root = (options.dir or engine.default_dir()).expanduser().resolve()
    engine.configure(root)

    if options.command == "status":
        state = engine.status(root)
        print(
            json.dumps(
                {
                    "installed": state.installed,
                    "path": str(root),
                    "problem": state.problem,
                    **state.info,
                },
                indent=2,
            )
        )
        return
    if options.command == "uninstall":
        if root.exists():
            shutil.rmtree(root)
            print(f"Removed {root}")
        return
    print(_summary(root))
    if not options.yes and input("Install? [y/N] ").strip().lower() not in {"y", "yes"}:
        print("Nothing was installed.")
        return
    try:
        install(root, skip_test=options.skip_test)
    except (InstallError, engine.LocalAIFailedError) as exc:
        sys.exit(f"Installation failed: {exc}")
    except subprocess.CalledProcessError as exc:
        sys.exit(
            f"Installation failed: {' '.join(map(str, exc.cmd[:3]))}… exited with {exc.returncode}"
        )


if __name__ == "__main__":
    main()
