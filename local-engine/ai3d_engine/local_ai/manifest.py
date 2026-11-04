"""What the local AI engine consists of: pinned sources, checksums and licences.

Nothing here is bundled with AI 3D Studio. ``install.py`` downloads each file
on the user's request, checks its SHA-256 against this list and refuses
anything else. Licences were checked when the pins were chosen (2026-11-04).
"""

from dataclasses import dataclass

ENGINE_NAME = "TripoSR"
ENGINE_KEY = "triposr"
# Bumped whenever a pin below changes; an installed engine with another
# version is treated as "needs reinstalling".
MANIFEST_VERSION = 1
PYTHON_VERSION = "3.11"


@dataclass(frozen=True, slots=True)
class Artifact:
    name: str
    url: str
    sha256: str
    size: int  # bytes, for progress and disk checks
    path: str  # where it goes, relative to the engine folder
    licence: str
    source: str  # human-readable origin, shown before installing


TRIPOSR_COMMIT = "107cefdc244c39106fa830359024f6a2f1c78871"
TRIPOSR_WEIGHTS_REVISION = "5b521936b01fbe1890f6f9baed0254ab6351c04a"
DINO_REVISION = "f205d5d8e640a89a2b8ef0369670dfc37cc07fc2"

SOURCE = Artifact(
    name="TripoSR source code",
    url=f"https://codeload.github.com/VAST-AI-Research/TripoSR/tar.gz/{TRIPOSR_COMMIT}",
    sha256="bcb414550dcfcb9f5ea6a7b9c12f2bbff889f5b4a564a493178393360d9034ec",
    size=0,  # small; size not known in advance (codeload streams it)
    path="downloads/triposr.tar.gz",
    licence="MIT (Tripo AI & Stability AI)",
    source=f"github.com/VAST-AI-Research/TripoSR @ {TRIPOSR_COMMIT[:8]}",
)

WEIGHTS = (
    Artifact(
        name="TripoSR model configuration",
        url=f"https://huggingface.co/stabilityai/TripoSR/resolve/{TRIPOSR_WEIGHTS_REVISION}/config.yaml",
        sha256="74ca708ce086bf68e97709ea6b3d91f14717921c04691e84043f0eb8fcc68e62",
        size=987,
        path="models/triposr/config.yaml",
        licence="MIT",
        source="huggingface.co/stabilityai/TripoSR",
    ),
    Artifact(
        name="TripoSR model weights",
        url=f"https://huggingface.co/stabilityai/TripoSR/resolve/{TRIPOSR_WEIGHTS_REVISION}/model.ckpt",
        sha256="429e2c6b22a0923967459de24d67f05962b235f79cde6b032aa7ed2ffcd970ee",
        size=1_677_246_742,
        path="models/triposr/model.ckpt",
        licence="MIT",
        source="huggingface.co/stabilityai/TripoSR",
    ),
    Artifact(
        name="DINO ViT-B/16 configuration (image encoder layout)",
        url=f"https://huggingface.co/facebook/dino-vitb16/resolve/{DINO_REVISION}/config.json",
        sha256="b87c0270b97db085fd82cf114a761fd0f62ae7914fbd407c752a2260646b689c",
        size=454,
        path="models/dino-vitb16/config.json",
        licence="Apache-2.0",
        source="huggingface.co/facebook/dino-vitb16",
    ),
    Artifact(
        name="U²-Net background removal model",
        url="https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx",
        sha256="8d10d2f3bb75ae3b6d527c77944fc5e7dcd94b29809d47a739a7a728a912b491",
        size=175_997_641,
        path="models/u2net.onnx",
        licence="Apache-2.0 (U²-Net, Qin et al.)",
        source="github.com/danielgatis/rembg release v0.0.0 (U²-Net weights)",
    ),
)

# Installed with uv into the engine's own environment (never the backend's).
# PyTorch 2.2.2 is the last release with Intel-Mac wheels and needs NumPy 1.x;
# other platforms get a current PyTorch. Only the Intel-Mac set has been run.
REQUIREMENTS = (
    'torch==2.2.2; sys_platform == "darwin" and platform_machine == "x86_64"',
    'torch>=2.4,<2.9; sys_platform != "darwin" or platform_machine != "x86_64"',
    "numpy>=1.26,<2",
    "transformers==4.35.0",
    "omegaconf==2.3.0",
    "einops==0.7.0",
    "huggingface-hub==0.17.3",
    "PyMCubes==0.1.6",
    "trimesh>=4.5,<6",
    "Pillow>=10,<14",
    "onnxruntime>=1.20,<1.24",
    "imageio>=2.30,<3",
)

# Disk space the installed engine needs (environment ≈1 GB + models ≈1.9 GB),
# plus room for the download and extraction.
REQUIRED_DISK_BYTES = 4_000_000_000
