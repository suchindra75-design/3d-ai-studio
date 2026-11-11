# AI 3D Studio GPU server: TRELLIS image-to-3D on an NVIDIA GPU (CUDA 12.1).
#
#   docker build -t ai3d-gpu-server gpu-server
#   docker run --gpus all -p 8800:8800 -e AI3D_GPU_TOKEN=... \
#       -v ai3d-models:/root/.cache ai3d-gpu-server
#
# Needs an NVIDIA GPU with at least 16 GB (TRELLIS-image-large). The model weights
# (MIT, about 4 GB) download on first start into the /root/.cache volume.
# See docs/GPU_SERVER.md. Only permissively licensed parts are installed:
# no nvdiffrast, no Gaussian rasteriser, no kaolin, no plyfile (GPL).
FROM pytorch/pytorch:2.4.1-cuda12.1-cudnn9-devel

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    ATTN_BACKEND=xformers \
    SPCONV_ALGO=native \
    PYTHONPATH=/opt/trellis

RUN apt-get update && apt-get install -y --no-install-recommends git libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# TRELLIS (MIT) and FlexiCubes (Apache-2.0), pinned.
ARG TRELLIS_COMMIT=193e0502b5cb3eb5997ec457d5340e95d09b158e
ARG FLEXICUBES_COMMIT=815e075a2a400d06c48d94c347674344ed6ae5c5
RUN git clone https://github.com/microsoft/TRELLIS.git /opt/trellis \
    && git -C /opt/trellis checkout ${TRELLIS_COMMIT} \
    && git clone https://github.com/MaxtirError/FlexiCubes.git \
        /opt/trellis/trellis/representations/mesh/flexicubes \
    && git -C /opt/trellis/trellis/representations/mesh/flexicubes checkout ${FLEXICUBES_COMMIT} \
    && rm -rf /opt/trellis/.git /opt/trellis/trellis/representations/mesh/flexicubes/.git

# Libraries the image-to-mesh path uses (all permissive: BSD, Apache-2.0, MIT).
RUN pip install xformers==0.0.28 --index-url https://download.pytorch.org/whl/cu121 \
    && pip install spconv-cu120==2.3.6 \
    && pip install pillow imageio easydict opencv-python-headless scipy rembg==2.0.59 \
        onnxruntime trimesh transformers huggingface_hub \
        git+https://github.com/EasternJournalist/utils3d.git@9a4eb15e4021b67b12c460c7057d642626897ec8

# DINOv2 (Apache-2.0, TRELLIS's image encoder) and U²-Net for background removal,
# fetched at build time so the server needs no code downloads at run time.
RUN python -c "import torch; torch.hub.load('facebookresearch/dinov2', 'dinov2_vitl14_reg', pretrained=True)" \
    && python -c "import rembg; rembg.new_session('u2net')"

COPY pyproject.toml README.md /opt/ai3d-gpu/
COPY ai3d_gpu /opt/ai3d-gpu/ai3d_gpu
RUN pip install /opt/ai3d-gpu

EXPOSE 8800
CMD ["ai3d-gpu-server", "--host", "0.0.0.0", "--port", "8800"]
