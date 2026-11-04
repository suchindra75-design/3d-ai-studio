"""One heavy local job at a time.

COLMAP and the local AI engine each use every CPU core (and most of the
memory); two at once would only slow both down and risk running out of RAM.
"""

import threading

from ai3d.models.enums import JobEventType
from ai3d.workers.runner import JobContext

_HEARTBEAT_SECONDS = 2.0
_heavy_local_work = threading.Semaphore(1)


def wait_turn(ctx: JobContext, waiting_message: str) -> bool:
    """Blocks until no other heavy job runs. False if the job was cancelled meanwhile."""
    if _heavy_local_work.acquire(blocking=False):
        return True
    ctx.event(JobEventType.STATUS, waiting_message)
    while not _heavy_local_work.acquire(timeout=_HEARTBEAT_SECONDS):
        if ctx.heartbeat():
            return False
    return True


def release() -> None:
    _heavy_local_work.release()
