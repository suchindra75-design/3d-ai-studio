# Local processing & GPU setup

Settings → **This machine** (or `GET /api/v1/devices/local`) shows what AI 3D
Studio detected and why each capability is or isn't available. Use
**Re-detect** after installing anything below.

## What runs where

| Capability | Needs | macOS Apple Silicon | macOS Intel | Windows / Linux + NVIDIA | Windows / Linux, no NVIDIA |
|---|---|---|---|---|---|
| Upload, analysis, capture check | nothing extra | ✅ | ✅ | ✅ | ✅ |
| Photogrammetry: camera poses (sparse) | COLMAP | ✅ | ✅ (COLMAP CLI) | ✅ | ✅ |
| Photogrammetry: dense surfaces | **OpenMVS** (CPU), or COLMAP + CUDA | ⚠️ OpenMVS (untested) | ✅ OpenMVS (verified) | ⚠️ OpenMVS or CUDA (both untested) | ⚠️ OpenMVS (untested) |
| Local AI from one photo (TripoSR) | the local AI engine (below) | ⚠️ CPU (untested) | ✅ CPU, ~1.5 min (verified) | ⚠️ CUDA (untested) | ⚠️ CPU (untested) |

Why: COLMAP's dense stereo is CUDA-only. **Without it, real photo captures
do not give a usable surface:** on a real 67-photo capture every camera was
placed correctly, but the surface built from the sparse points was spiky and
reached into the background (the quality report now warns about this). The
synthetic test scenes looked fine; real photos do not. **Install OpenMVS**
(below) for a dense surface on the CPU. TripoSR is small enough to run on a
CPU; it was measured on a 6-core Intel Mac (macOS 26): 82 s for a 12-megapixel
phone photo at High quality, using at most 3.7 GB of memory (so 8 GB of RAM
is the minimum). NVIDIA GPUs are used automatically when present
but have not been tested yet; Apple's MPS backend is not used.

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

## OpenMVS (dense photogrammetry on the CPU)

Optional, and strongly recommended without an NVIDIA GPU. After COLMAP has
placed the cameras, OpenMVS computes depth maps for every photo, fuses them
into a dense point cloud, builds a surface through it and textures it, all
on the CPU. On a real 67-photo capture (6-core Intel Mac) the whole
reconstruction took about 7 minutes (COLMAP ~2.5, OpenMVS ~4.5) and gave a
detailed, textured, upright surface (face, curls, ears; about 400k triangles)
instead of the coarse, spiky sparse one.

```bash
T=$HOME/.local/share/ai3d-studio/tools        # the micromamba from the COLMAP section
MAMBA_ROOT_PREFIX=$T/mamba $T/bin/micromamba create -y -p $T/openmvs -c conda-forge "openmvs=2.4.0=cpu*"
```

The app finds it in `~/.local/share/ai3d-studio/tools/openmvs/bin` (or on
`PATH`, `/usr/local/bin/OpenMVS`, `/opt/homebrew/bin`); elsewhere set
`AI3D_OPENMVS_DIR` to the folder holding `DensifyPointCloud`. Settings →
This computer then lists *Photogrammetry (dense surfaces)* as available, and
photogrammetry jobs use it automatically. Quality: Fast computes depth maps at
1/4 of the photo size and keeps half the triangles; High and Professional at
1/2 with all triangles.

**Licence:** OpenMVS is AGPL-3.0 (conda-forge build: AGPL-3.0 with
BSL/MPL/BSD parts). AI 3D Studio only starts its programs as separate
processes, through a fixed allow-list with arguments built in code; it never
links, modifies or redistributes OpenMVS. If you redistribute OpenMVS
yourself, the AGPL applies to that.

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

## Local AI engine (TripoSR: one photo → 3D on this computer)

Optional. Turns a single photo into a 3D model without uploading it: the
background is removed (U²-Net), TripoSR predicts the shape and its colours,
and the app then refines the result (below). Hidden sides are invented and
the size is not measured; the app labels the model so.

**Refining** (our own code, `backend/ai3d/processing/refine.py`, about 10 s on
the CPU). TripoSR's raw output is a dense surface (up to about 200,000
triangles) with one colour per corner and often a few loose specks. Each
generation is turned into a lighter textured model:

| Step | What it does |
|---|---|
| Fragments | Loose pieces under 1% of the largest piece's area are removed. |
| Smoothing | 10 Taubin passes take out the grid's stair-steps without shrinking the model. Broad bumps TripoSR built into the shape stay. |
| Simplifying | To about 20,000 / 40,000 / 80,000 triangles (Fast / High quality / Professional). Folds and edges shared by three or more triangles that simplifying can leave are mended, so a closed model stays closed. |
| Texture | The surface is unwrapped and its colours baked into a 1024 / 2048 / 2048 px texture. |
| Photo | Where the camera saw the surface, the photo itself is projected on: much sharper than TripoSR's colours. It fades out towards grazing angles and is not used on hidden parts. Before projecting, the model's outline is lined up with the cut-out; if they overlap less than 60%, the photo is not projected and the job log says so. |

The job log reports each step (triangles before and after, fragments removed,
how much of the surface shows the photo). If refining fails, the model is kept
as TripoSR made it and the log says why. What it cannot do: the sides the
camera did not see are still TripoSR's guesses, and parts missing from the
cut-out stay missing.

**Check the cut-out first.** In a single-image project, the capture check offers
**Check cut-out**. It runs the same background removal on the same copy of the
photo (a few seconds, on this computer) and shows, in the photo's details, the
picture the AI will receive. It warns when:

- the object runs off an edge of the photo (that side will be cut flat);
- the object blends into the background: most colours along its edge also occur
  just outside it, so the mask can lose parts, thin ones first;
- the object is very small in the photo, or no object was found.

These are heuristics; the preview itself is exact, so look for missing parts
there. Photos with their own transparency need no engine; for them the
edge check also runs at upload.

Install (needs [uv](https://docs.astral.sh/uv/) and about 4 GB of free disk):

```bash
uv run ai3d-local-ai install      # shows what is downloaded and the licences, then asks
uv run ai3d-local-ai status
uv run ai3d-local-ai install-upscaler   # adds the 4.9 MB texture upscaler to an existing engine
uv run ai3d-local-ai uninstall    # or delete the folder
```

It goes to `~/.local/share/ai3d-studio/tools/local-ai` (or `--dir`, then set
`AI3D_LOCAL_AI_DIR`), in its own Python environment (PyTorch needs versions
the backend cannot share). Everything is pinned and checked:

| Part | Licence | Source |
|---|---|---|
| TripoSR code (commit `107cefdc`) | MIT | github.com/VAST-AI-Research/TripoSR |
| TripoSR weights (1.68 GB, revision `5b521936`) | MIT | huggingface.co/stabilityai/TripoSR |
| DINO ViT-B/16 configuration | Apache-2.0 | huggingface.co/facebook/dino-vitb16 |
| U²-Net background removal (176 MB) | Apache-2.0 | github.com/danielgatis/rembg release v0.0.0 |
| Real-ESRGAN compact texture upscaler (4.9 MB, `realesr-general-x4v3.pth`) | BSD-3-Clause | github.com/xinntao/Real-ESRGAN release v0.2.5.0 |
| PyTorch 2.2.2 (Intel Mac) / current elsewhere, transformers 4.35, onnxruntime, PyMCubes | BSD / Apache-2.0 / MIT | PyPI |

Every download is checked against its SHA-256; the installer runs a real test
generation before it marks the engine as installed. At run time the engine
never goes online: model lookups are redirected to the verified files, the
checkpoint is loaded tensors-only (no pickle code execution), and the process
gets a minimal environment without API keys.

Then choose **Local** processing (or **Automatic**: a single photo then stays
on this computer when the engine is installed), or pick *Local AI (TripoSR)*
as the provider. Quality: Fast/High/Professional extract the surface on a
160³/256³/320³ grid (High took 82 s on the Intel Mac above, first runs after
installing are slower while macOS checks the new libraries).

**AI texture upscaling** (Enhance → *Upscale textures*) runs the 4.9 MB
Real-ESRGAN model on this computer. Its limits:
- textures over 4096 px on a side are kept (`AI3D_UPSCALE_MAX_INPUT_PX`);
  a 4096 px input takes about five minutes on a 6-core CPU;
- the image is processed in 192 px tiles (`AI3D_UPSCALE_TILE_PX`, 32–2048).
  Smaller tiles use less memory per step and give the same image;
- before each texture, the memory it needs is estimated (about 0.6 GB plus
  the image) and compared with what is free. If it does not fit, the texture
  is kept and the step says so. Measured peaks were about 0.33 GB.

Other models (TRELLIS on NVIDIA ≥16 GB; Stable Fast 3D and Hunyuan3D have
restrictive licences) are not integrated; see `AI_PIPELINE.md` §8.

## Disk space

Photogrammetry workspaces can reach several GB per project. Detection warns
below 5 GB free in the data folder's volume.
