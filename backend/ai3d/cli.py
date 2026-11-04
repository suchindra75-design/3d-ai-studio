"""``ai3d-server`` entry point."""

import argparse
import logging
import os
import signal
import threading
from collections.abc import Callable

import psutil
import uvicorn

from ai3d.core.config import Settings

log = logging.getLogger(__name__)


def _graceful_exit() -> None:
    # uvicorn treats SIGTERM as "shut down": the job runner hands jobs back first.
    os.kill(os.getpid(), signal.SIGTERM)


def watch_parent(
    pid: int, *, interval: float = 2.0, on_exit: Callable[[], None] = _graceful_exit
) -> threading.Thread:
    """Calls ``on_exit`` once process ``pid`` no longer runs (desktop app quit or crashed)."""

    def watch() -> None:
        while psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE:
            threading.Event().wait(interval)
        log.info("parent process is gone; shutting down", extra={"parent_pid": pid})
        on_exit()

    thread = threading.Thread(target=watch, name="parent-watch", daemon=True)
    thread.start()
    return thread


def main() -> None:
    settings = Settings()
    parser = argparse.ArgumentParser(description="Run the AI 3D Studio backend.")
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--reload", action="store_true", help="Auto-reload on code changes")
    args = parser.parse_args()
    if settings.parent_pid is not None:
        watch_parent(settings.parent_pid)
    uvicorn.run(
        "ai3d.main:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_config=None,  # ai3d.core.logging configures logging
    )


if __name__ == "__main__":
    main()
