# Your GPU server (TRELLIS)

The strongest generation the app can use without a third-party service: Microsoft's
TRELLIS model (MIT licence) running on an NVIDIA GPU machine **you** run. The app
sends the photos to that machine, gets back a 3D shape, and then cleans it up and
textures it on your computer, as it does for local AI models.

The same server also makes **whole CGI scenes from a prompt** ([section 5](#5-scenes-from-a-prompt)):
a language model plans the shot, an image model paints each object, TRELLIS shapes
it, and Blender on your computer builds the set, light and camera move and renders
the video.

| | Local AI (TripoSR, this computer) | Your GPU server (TRELLIS) | Tripo / Meshy |
|---|---|---|---|
| Model | small, 2024 | large, much sharper shapes and backs | their own |
| Runs on | this computer's CPU | a GPU machine you rent or own | their servers |
| Photos go to | nowhere | your server only | the company |
| Cost | free | the server's hourly rental | credits per model |
| Several photos | front shapes, others correct depth and colour | all views shape the model together | multi-view mode |

**Status:** the server, the app's side and the whole job flow (for scenes too, with
Blender really rendering) are tested on every change with stand-in models (no GPU).
The model parts (`gpu-server/ai3d_gpu/backends/trellis.py`, `qwen.py`, `flux.py`) and
the Docker image are written against the models' published code and files but have
**not yet run on a GPU**: the first deployment is also their first real test. If a step
below fails, the error is in the server's log (`docker logs`), and the
[troubleshooting](#troubleshooting) list covers the likely ones.

## 1. Rent a GPU machine

You need a Linux machine with:

- an NVIDIA GPU with **at least 16 GB** of memory for 3D models (TRELLIS-image-large);
  for scenes from a prompt **24 GB** works and **40 GB or more** is much faster (the
  image model's 12-billion-parameter core needs about 24 GB on its own; below 40 GB
  its layers are streamed from memory),
- for scenes, about **64 GB** of computer memory (the models not in use wait there),
- Docker with the NVIDIA container toolkit (most GPU rental images have both),
- about 40 GB of disk for 3D models only, about 90 GB with the scene models.

Any GPU rental service or cloud provider with such machines works; they bill by the
hour, so stop the machine when you are not generating. Prices change often, so check
them when you choose. The server keeps nothing it does not need: uploaded photos are
deleted when a job ends, and results as soon as the app has downloaded them (or after
24 hours).

## 2. Build and start the server

On the GPU machine, with this repository's `gpu-server/` folder copied there:

```bash
docker build -t ai3d-gpu-server gpu-server
```

```bash
export AI3D_GPU_TOKEN=$(openssl rand -hex 32)   # keep this; the app needs it
export HF_TOKEN=hf_...   # your Hugging Face read token, only for scenes (see below)
docker run -d --name ai3d-gpu --gpus all -p 127.0.0.1:8800:8800 \
  -e AI3D_GPU_TOKEN -e HF_TOKEN -v ai3d-models:/root/.cache ai3d-gpu-server
```

The first start downloads the model weights (about 4 GB, MIT licence, pinned to a
known revision) into the `ai3d-models` volume; later starts reuse them. Watch it with
`docker logs -f ai3d-gpu`; it is ready when the log says `backend trellis, model …`.

`-p 127.0.0.1:8800:8800` keeps the server reachable only from the machine itself; step
3 connects to it safely.

**For scenes:** the image model, FLUX.1-schnell (Apache-2.0), is *gated* on Hugging
Face: sign in at huggingface.co, open `black-forest-labs/FLUX.1-schnell` and accept its
access terms once, then create a read token (Settings → Access Tokens) and pass it as
`HF_TOKEN`. The token stays on the server and is never sent to the app. The scene
models download on first use (about 8 GB for the planner, 34 GB for the image model),
so the first scene takes longer. Without scenes, start with `-e AI3D_GPU_SCENES=off`
and no `HF_TOKEN`.

**Trying it without a GPU:** `-e AI3D_GPU_BACKEND=stand-in` (and no `--gpus`) runs the
same server with a stand-in model that returns an ellipsoid. Useful to test the
connection and settings before paying for a GPU.

## 3. Connect the app

The token travels with every request, so the connection must be private:

- **SSH tunnel (simplest):** on your computer, keep this running while you generate:

  ```bash
  ssh -N -L 8800:127.0.0.1:8800 you@your-gpu-machine
  ```

  then use the address `http://127.0.0.1:8800`. (Plain `http://` is accepted only for
  this computer, because the tunnel already encrypts the traffic.)
- **HTTPS:** if your rental service gives the machine an `https://` address (a proxy),
  or you put a reverse proxy with a certificate in front of port 8800, use that
  `https://…` address instead.

In the app: **Settings → AI providers → Your GPU server (TRELLIS)**, enter the server
address and the access token, then **Check connection**. It should say
`Connected: trellis (microsoft/TRELLIS-image-large@…) on NVIDIA …`. (For a server
deployment of the app, `AI3D_GPU_SERVER_URL` and `AI3D_GPU_SERVER_TOKEN` do the same.)

## 4. Generate

Choose **Your GPU server (TRELLIS)** as the provider in the Generate panel. (With
**Cloud** processing and no provider chosen, the app picks it before Tripo or Meshy;
**Automatic** still prefers engines on this computer, which send nothing anywhere.) As
with any service outside your computer, the app asks before sending the photos.

- **One photo**: TRELLIS shapes the whole object from it.
- **Several views** (a multiple-photos project, Front plus Left/Back/Right): all of
  them shape the model together, so the sides and back are far less invented.

Quality profiles set TRELLIS's sampling steps (Fast 12, High quality 25 (TRELLIS's
default), Professional 50) and what the app keeps (30,000 / 60,000 / 120,000
triangles, 1024 / 2048 / 2048 px texture).

On your computer the app then (job log, *Refined in …*):

1. turns the model to face the front photo (TRELLIS makes objects in their own pose,
   not facing the camera; the turn is found from the outline and, for front/back
   ties, the colours),
2. removes stray pieces, smooths and simplifies the surface (keeping it closed),
3. bakes a texture from the model's own colours and projects each photo onto the
   parts its camera saw.

## 5. Scenes from a prompt

Create a project with **From a prompt** as its input. In **Make a scene**, describe
the shot (the objects, where they are, what they do), choose the length (4–15 s) and
quality, and optionally the camera move, angle, light, set and look (or let the AI
choose), then agree to send the prompt to your server and press **Make scene**.

1. *Planning* (your server): Qwen3-4B-Instruct turns the prompt into a scene plan:
   up to six objects with real sizes, places, facing and motion (spin, bob, hover,
   drive, sway), plus the light, ground, camera move and look. The app checks the
   plan and fixes what it can (unknown words, impossible sizes); a plan with no
   usable object is asked for again, twice.
2. *For each object* (your server): FLUX.1-schnell paints a reference picture of the
   object alone, and TRELLIS shapes it in 3D. On your computer the object is cleaned
   up and textured like any GPU-server model and **saved as a model of the project**,
   so you can open, edit and export it on its own.
3. *Setting the stage and rendering* (your computer, Blender 4.2 or newer): the
   objects are placed from their real sizes without overlapping, and Blender builds
   the set (a seamless studio backdrop or a procedural grass, sand, concrete, water or
   snow ground), the light (studio lamps, a physical sky with the sun at day or golden
   hour, overcast, night with stars, neon, space), the camera move (orbit, push in,
   pull out, pan, crane up, fly by, locked off) with depth of field, and the motions.

The result: an **MP4** (H.264), a **still** of the hero frame, the **Blender file**
(textures packed, ready to adjust and render again in Blender) and an animated
**GLB** (objects, lights and the camera move, for other 3D programs and engines).

| Quality | Video | Still | Objects |
|---|---|---|---|
| Fast | 1280×720, EEVEE | EEVEE | 768 px pictures, TRELLIS 12 steps |
| High quality | 1920×1080, EEVEE, motion blur | EEVEE | 1024 px pictures, 25 steps |
| Professional | 1920×1080, EEVEE with ray tracing, motion blur | Cycles | 1024 px, 50 steps |

**Render again** films the same objects with another camera move, light, set, look or
length, on your computer only. If a scene stops (a failure, a cancel, the app
closing), **Run again** keeps the objects already made and continues with the rest.

On an Intel Mac, a 6-second Fast render takes a few minutes; High quality and
Professional take longer (EEVEE uses the graphics card; Cycles stills use the CPU).
The AI models invent every surface: scenes are for looks, not measurements.

## Licences (what is installed on the server)

| Part | Licence |
|---|---|
| TRELLIS code and TRELLIS-image-large weights | MIT |
| FlexiCubes (builds the mesh) | Apache-2.0 |
| DINOv2 (image encoder) | Apache-2.0 |
| Qwen3-4B-Instruct-2507 (scene plans) | Apache-2.0 |
| FLUX.1-schnell (pictures; gated download, terms accepted on Hugging Face) | Apache-2.0 |
| transformers, diffusers, accelerate | Apache-2.0 |
| rembg, U²-Net (background removal) | MIT, Apache-2.0 |
| xformers, spconv, PyTorch | BSD, Apache-2.0, BSD |
| This server (`gpu-server/`) | Apache-2.0, like the app |

TRELLIS's renderers and its own texture baking use nvdiffrast and Inria's Gaussian
rasteriser, whose licences forbid commercial use. The server never installs or imports
them: it decodes the mesh only, and the texture is made by the app's own code. Three
modules TRELLIS imports but this path never uses are replaced by empty stand-ins
(plyfile, GPL-3.0; open3d; kaolin). Check the licences again when you change the pinned
versions in the Dockerfile.

## Troubleshooting

- *Check connection*: *"refused the access token"*: the token in Settings differs from
  `AI3D_GPU_TOKEN` on the server. *"Could not reach …"*: the tunnel is not running, the
  container is not up (`docker ps`), or the address is wrong.
- The address is refused in Settings: use `https://…`, or `http://127.0.0.1:…` with an
  SSH tunnel.
- The container exits at start: `docker logs ai3d-gpu`. *"TRELLIS needs an NVIDIA GPU"*:
  start it with `--gpus all` on a machine with the NVIDIA container toolkit. *Out of
  memory*: the GPU has less than 16 GB.
- A job fails with *"No object was found"*: background removal kept nothing; use a
  photo with one clear object (the app's **Check cut-out** helps).
- *"The GPU queue is full"*: eight jobs are waiting; try again later.
- Scenes: *"Your GPU server has no scene models"*: the server is an older version or
  runs with `AI3D_GPU_SCENES=off`; rebuild it from this repository and start it
  without that setting. *"… is gated"* in a failed scene: accept FLUX.1-schnell's
  terms on Hugging Face and start the server with `HF_TOKEN`. *"The scene planner gave
  no usable plan"*: describe the objects more plainly (what they are, not only the
  mood). *"Scenes are rendered with Blender"*: install Blender 4.2 or newer
  ([blender.org](https://www.blender.org)) on your computer.
- When EEVEE cannot render on your computer, the app switches to Cycles on the CPU by
  itself (slower); if Blender still fails, the job shows Blender's last message.
