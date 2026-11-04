"""The local AI engine wrapper: install checks, requests, the process lifecycle.

The real TripoSR run is covered by backend/tests/test_local_ai.py (slow, needs an
installed engine). Here a small stand-in script replaces the generation script
so progress, failures, cancellation and timeouts are tested in seconds.
"""

import dataclasses
import json
import os
import sys
import time
from pathlib import Path

import pytest

from ai3d_engine.local_ai import engine, install, manifest


def _fake_install(root: Path, *, version: int = manifest.MANIFEST_VERSION) -> None:
    """The folder layout of an installed engine, with this interpreter as its Python."""
    python = engine.env_python(root)
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    (root / "src" / "TripoSR-x").mkdir(parents=True)
    for artifact in manifest.WEIGHTS:
        path = root / artifact.path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as fh:
            fh.truncate(artifact.size)  # sparse file of the right size
    (root / "engine.json").write_text(
        json.dumps({"manifest_version": version, "source_dir": "src/TripoSR-x"})
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = tmp_path / "local-ai"
    engine.configure(folder)
    yield folder
    engine.configure(None)


def test_status_explains_what_is_missing(root: Path) -> None:
    state = engine.status()
    assert not state.installed
    assert "ai3d-local-ai install" in (state.problem or "")

    _fake_install(root, version=manifest.MANIFEST_VERSION + 1)
    assert "reinstall" in (engine.status().problem or "")

    (root / "engine.json").unlink()
    _fake_install(root.parent / "other")
    engine.configure(root.parent / "other")
    assert engine.status().installed
    assert engine.check_local_ai() is None
    (root.parent / "other" / "models" / "triposr" / "model.ckpt").unlink()
    assert "incomplete" in (engine.status().problem or "")


def test_requests_are_built_from_checked_values(root: Path) -> None:
    info = {"source_dir": "src/TripoSR-x"}
    request = engine.build_request(
        root,
        info,
        image=Path("/in.png"),
        output=Path("/out.glb"),
        prepared_image=Path("/p.png"),
        resolution=256,
    )
    assert request["weights_dir"] == str(root / "models" / "triposr")
    assert request["resolution"] == 256
    assert request["threads"] >= 1
    with pytest.raises(ValueError, match="resolution"):
        engine.build_request(
            root, info, image=Path("a"), output=Path("b"), prepared_image=Path("c"), resolution=9
        )
    with pytest.raises(ValueError, match="device"):
        engine.build_request(
            root,
            info,
            image=Path("a"),
            output=Path("b"),
            prepared_image=Path("c"),
            resolution=64,
            device="cuda; rm -rf /",
        )


def test_the_child_gets_a_minimal_offline_environment(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AI3D_TRIPO_API_KEY", "secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy")
    env = engine._child_env(root)
    assert "AI3D_TRIPO_API_KEY" not in env
    assert "HTTPS_PROXY" not in env
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["TRANSFORMERS_OFFLINE"] == "1"


STAND_IN = """
import json, sys, time
request = json.load(open(sys.argv[1]))
mode = open(request["image"]).read().strip()
for stage in ("starting", "background", "loading", "generating", "meshing"):
    print(f"PROGRESS {stage} 0.000", flush=True)
    if mode == "slow":
        time.sleep(0.4)
if mode == "fail":
    json.dump({"ok": False, "error": "No object was found in the photo."}, open(sys.argv[2], "w"))
    sys.exit(1)
if mode == "crash":
    sys.exit(3)
open(request["output"], "wb").write(b"glTF")
json.dump({"ok": True, "device": "cpu", "seconds": 0.1, "triangles": 12,
           "env": sorted(k for k in __import__("os").environ if k.startswith("AI3D"))},
          open(sys.argv[2], "w"))
"""


@pytest.fixture
def stand_in(root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    _fake_install(root)
    script = tmp_path / "stand_in.py"
    script.write_text(STAND_IN)
    monkeypatch.setattr(engine, "SCRIPT", script)
    return tmp_path


def _photo(folder: Path, mode: str) -> Path:
    path = folder / f"{mode}.txt"
    path.write_text(mode)
    return path


def test_a_run_reports_progress_and_returns_the_model(
    stand_in: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AI3D_SECRET_KEY", "never-passed-on")
    seen: list[str] = []
    result = engine.generate(
        _photo(stand_in, "ok"),
        stand_in / "run",
        resolution=64,
        on_progress=lambda stage, _f: seen.append(stage),
        poll_seconds=0.05,
    )
    assert Path(result["glb"]).read_bytes() == b"glTF"
    assert result["triangles"] == 12
    assert result["env"] == []  # no AI3D_* settings or keys reach the engine
    assert seen == ["starting", "background", "loading", "generating", "meshing"]


def test_failures_carry_the_scripts_reason_or_the_exit_code(stand_in: Path) -> None:
    with pytest.raises(engine.LocalAIFailedError, match="No object was found"):
        engine.generate(_photo(stand_in, "fail"), stand_in / "a", resolution=64, poll_seconds=0.05)
    with pytest.raises(engine.LocalAIFailedError, match="code 3"):
        engine.generate(_photo(stand_in, "crash"), stand_in / "b", resolution=64, poll_seconds=0.05)


def test_cancel_and_timeout_stop_the_process(stand_in: Path) -> None:
    started = time.monotonic()
    with pytest.raises(engine.LocalAICancelledError):
        engine.generate(
            _photo(stand_in, "slow"),
            stand_in / "c",
            resolution=64,
            cancel=lambda: time.monotonic() - started > 0.3,
            poll_seconds=0.05,
        )
    assert time.monotonic() - started < 1.5
    with pytest.raises(engine.LocalAIFailedError, match="did not finish"):
        engine.generate(
            _photo(stand_in, "slow"), stand_in / "d", resolution=64, timeout=0.3, poll_seconds=0.05
        )


def test_not_installed_is_a_clear_error(root: Path) -> None:
    with pytest.raises(engine.LocalAINotInstalledError, match="not installed"):
        engine.generate(Path("photo.jpg"), root / "run", resolution=64)


def test_downloads_are_checked_against_the_manifest(tmp_path: Path) -> None:
    payload = b"model bytes"
    source = tmp_path / "server.bin"
    source.write_bytes(payload)
    good = manifest.Artifact(
        name="test file",
        url=source.as_uri(),
        sha256=__import__("hashlib").sha256(payload).hexdigest(),
        size=len(payload),
        path="models/test.bin",
        licence="MIT",
        source="test",
    )
    target = install.download(good, tmp_path / "engine")
    assert target.read_bytes() == payload
    assert install.download(good, tmp_path / "engine") == target  # already there: not fetched

    bad = dataclasses.replace(good, sha256="0" * 64, path="models/bad.bin")
    with pytest.raises(install.InstallError, match="checksum mismatch"):
        install.download(bad, tmp_path / "engine")
    assert not (tmp_path / "engine" / "models" / "bad.bin").exists()
    assert not (tmp_path / "engine" / "models" / "bad.bin.part").exists()


def test_every_artifact_is_pinned_and_licensed() -> None:
    for artifact in (manifest.SOURCE, *manifest.WEIGHTS):
        assert artifact.url.startswith("https://")
        assert len(artifact.sha256) == 64
        assert artifact.licence.split()[0] in {"MIT", "Apache-2.0"}
    assert manifest.TRIPOSR_COMMIT in manifest.SOURCE.url
    assert all(manifest.TRIPOSR_WEIGHTS_REVISION in a.url for a in manifest.WEIGHTS[:2])


@pytest.mark.skipif(os.name == "nt", reason="tar layout check uses POSIX paths")
def test_source_archives_with_escaping_paths_are_refused(tmp_path: Path) -> None:
    import io  # noqa: PLC0415
    import tarfile  # noqa: PLC0415

    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("../outside.txt")
        info.size = 4
        tar.addfile(info, io.BytesIO(b"evil"))
    (tmp_path / "engine").mkdir()
    with pytest.raises((tarfile.TarError, install.InstallError)):
        install._extract_source(archive, tmp_path / "engine")
    assert not (tmp_path / "outside.txt").exists()
