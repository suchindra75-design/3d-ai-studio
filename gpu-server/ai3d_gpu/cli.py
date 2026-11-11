"""``ai3d-gpu-server [--host H] [--port P]``: load the model, then serve the API."""

import argparse
import logging
import sys

import uvicorn

from ai3d_gpu.app import create_app
from ai3d_gpu.backends import load_backend
from ai3d_gpu.config import ConfigError, ServerConfig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI 3D Studio GPU server")
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 - inside a container
    parser.add_argument("--port", type=int, default=8800)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = ServerConfig.from_env()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    backend = load_backend(config)
    logging.info("backend %s, model %s on %s", backend.name, backend.model, backend.device)
    uvicorn.run(create_app(config, backend), host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
