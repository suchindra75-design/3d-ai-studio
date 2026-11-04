# Local processing & GPU setup

Settings → **This machine** (or `GET /api/v1/devices/local`) shows what AI 3D
Studio detected and why each capability is or isn't available. Use
**Re-detect** after installing anything below.

## What runs where

| Capability | Needs | macOS Apple Silicon | macOS Intel | Windows / Linux + NVIDIA | Windows / Linux, no NVIDIA |
|---|---|---|---|---|---|
| Upload, analysis, capture check | nothing extra | ✅ | ✅ | ✅ | ✅ |
| Photogrammetry: camera poses (sparse) | COLMAP | ✅ | ✅ (COLMAP CLI) | ✅ | ✅ |
| Photogrammetry: dense surfaces | COLMAP + **CUDA** | ❌ | ❌ | ✅ | ❌ |
| Local AI generation | PyTorch + GPU (≥ 6 GB VRAM typical) | ⚠️ MPS, model-dependent | ❌ | ✅ | ❌ (CPU fallback impractically slow) |

Why: COLMAP's dense stereo is CUDA-only; PyTorch has no supported GPU backend
on Intel Macs; Apple's MPS backend works only for models that support it.

## COLMAP (photogrammetry)

AI 3D Studio needs **COLMAP 4.0 or newer** (BSD licence; 4.2.1 tested). It is
found on `PATH`, or set `AI3D_COLMAP_PATH=/path/to/colmap` in `.env`. Settings
→ AI providers → *Local photogrammetry (COLMAP)* shows whether it was found.

| Platform | Install |
|---|---|
| macOS (Intel or Apple Silicon) | Prebuilt CPU build from conda-forge (no compiling; ≈ 1 GB on disk): see below. `brew install colmap` also works but builds from source on macOS 26 Intel (hours). |
| Windows | Download the CUDA build from <https://github.com/colmap/colmap/releases> and add `colmap.exe` to `PATH` |
| Linux | conda-forge (as below), or build with CUDA (<https://colmap.github.io/install.html>). Distribution packages are often COLMAP 3.x, which is too old. |

Self-contained install with micromamba (nothing outside one folder changes;
delete the folder to remove it):

```bash
T=$HOME/.local/share/ai3d-studio/tools
mkdir -p $T/bin
curl -L -o $T/bin/micromamba https://github.com/mamba-org/micromamba-releases/releases/latest/download/micromamba-osx-64   # linux-64 / osx-arm64 on other systems
chmod +x $T/bin/micromamba
MAMBA_ROOT_PREFIX=$T/mamba $T/bin/micromamba create -y -p $T/colmap -c conda-forge "colmap=4.2.1=cpu*"
echo "AI3D_COLMAP_PATH=$T/colmap/bin/colmap" >> .env
```

Optional point-cloud filtering uses Open3D: `uv sync --all-packages --all-extras`
(installs the `reconstruction` extra; Open3D 0.19 on Intel Macs, 0.20+ elsewhere).
Without it, reconstruction still works and says that no filtering was applied.

What to expect without CUDA (every Mac): camera poses, sparse points, a
coarse surface built from those points, and a texture projected from the
photos. With an NVIDIA GPU and a CUDA build of COLMAP, High quality and
Professional add dense stereo (detailed surface) — **this path is implemented
but has not been tested yet**.

## Blender (FBX export, texture baking)

Optional. With Blender **4.2 or newer** installed, the app offers FBX export
and, in the Professional profile, reduces textured models over 300,000
triangles by baking their colour onto a lighter copy. Without it, FBX is
listed as unavailable and such models are kept as they are.

Install Blender from <https://www.blender.org/download/> (check the download
against the published `.sha256` file). Blender 5.x ships only for Apple
Silicon on macOS; on an **Intel Mac use 4.5 LTS**, the last line with an
x86-64 build. Detection checks `PATH`, `/Applications/Blender.app` and the
default Windows location; the first use after installing takes up to ~30 s
while macOS checks the app.

Blender always runs headless (`--background --factory-startup`) with the
repository's own script (`local-engine/ai3d_engine/blender_scripts/`), one of
three fixed commands (`inspect`, `export_fbx`, `bake`) and a JSON request file;
never through a shell. Every FBX is re-opened in a separate Blender run and
compared with the model (triangles, size, embedded textures).

What it does not do: retopology (quad remeshing) is not offered — QuadriFlow
moved open surfaces noticeably in tests, and versions are stored as GLB, which
holds triangles only. Baking transfers the base colour only; normal,
metallic-roughness and emissive maps become plain values. FBX has no colour
tint factor, so a tinted texture is exported untinted (the export says so).
Baking runs on the CPU: about 20 s for a 330k-triangle model, about 100 s for
1.3M triangles at 4096 px on the test machine.

## NVIDIA / CUDA

1. Install the current NVIDIA driver; `nvidia-smi` must work in a terminal.
2. CUDA toolkits ship with the tools that need them (COLMAP CUDA builds,
   PyTorch wheels). Detection reads GPU name, VRAM and driver from `nvidia-smi`.

## Open3D (point-cloud filtering)

Optional (`reconstruction` extra). Open3D 0.20 provides wheels for Windows x64,
Linux x86_64/aarch64 and macOS arm64; Intel Macs get 0.19.0, the last release
with an Intel macOS wheel.

## Local AI models (Phase 6+)

Model weights are never bundled. When local AI providers arrive you will
install them yourself and accept their licences — some restrict commercial
use (Stability AI Community License above US$1M revenue; nvdiffrast is
non-commercial) or exclude regions (Hunyuan3D: EU, UK, South Korea). See
`AI_PIPELINE.md` §8.

## Disk space

Photogrammetry workspaces can reach several GB per project. Detection warns
below 5 GB free in the data folder's volume.
