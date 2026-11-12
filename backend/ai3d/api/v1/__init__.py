from fastapi import APIRouter

from ai3d.api.errors import ERROR_RESPONSES
from ai3d.api.v1 import (
    devices,
    exports,
    images,
    jobs,
    models,
    projects,
    providers,
    scenes,
    system,
)

API_PREFIX = "/api/v1"

router = APIRouter(prefix=API_PREFIX, responses=ERROR_RESPONSES)
router.include_router(system.router)
router.include_router(projects.router)
router.include_router(images.router)
router.include_router(devices.router)
router.include_router(providers.router)
router.include_router(jobs.router)
router.include_router(models.router)
router.include_router(exports.router)
router.include_router(scenes.router)
