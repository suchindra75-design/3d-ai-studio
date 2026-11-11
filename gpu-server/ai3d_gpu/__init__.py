"""AI 3D Studio GPU server.

A small HTTP service that runs on a machine with an NVIDIA GPU (usually rented)
and turns 1–4 photos into a 3D shape with TRELLIS (Microsoft, MIT licence). The
app sends the photos, polls the job and downloads the result; it then cleans up
and textures the model on the user's computer (backend ``processing/refine.py``).

Only permissively licensed parts of TRELLIS are used: the mesh is decoded with
FlexiCubes (Apache-2.0); the Gaussian and nvdiffrast renderers (non-commercial
licences) are never imported. See docs/GPU_SERVER.md.
"""

API_VERSION = "1"
