# AI 3D Studio GPU server

Runs Microsoft's TRELLIS image-to-3D model (MIT) on an NVIDIA GPU machine you run,
for the app's **Your GPU server (TRELLIS)** provider, and for scenes from a prompt
Qwen3-4B-Instruct (scene plans) and FLUX.1-schnell (pictures), both Apache-2.0. Setup, connection and licences:
[docs/GPU_SERVER.md](../docs/GPU_SERVER.md).

```bash
docker build -t ai3d-gpu-server .
docker run -d --gpus all -p 127.0.0.1:8800:8800 -e AI3D_GPU_TOKEN=... -e HF_TOKEN=... \
  -v ai3d-models:/root/.cache ai3d-gpu-server
```

Without a GPU, `-e AI3D_GPU_BACKEND=stand-in` serves a stand-in model (an ellipsoid)
to try the connection. Tests: `uv run pytest gpu-server/tests` from the repository root.
