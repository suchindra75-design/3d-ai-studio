from typing import Annotated

from fastapi import APIRouter, Query

from ai3d.api.deps import DbDep, SettingsDep
from ai3d.models import LocalDevice
from ai3d.schemas.device import LocalDeviceRead
from ai3d.services import devices as device_service

router = APIRouter(prefix="/devices", tags=["local processing"])


@router.get(
    "/local",
    response_model=LocalDeviceRead,
    summary="Detect this machine's hardware, accelerators and installed tools",
)
def local_device(
    db: DbDep,
    settings: SettingsDep,
    refresh: Annotated[bool, Query(description="Bypass the 60 s detection cache")] = False,
) -> LocalDevice:
    return device_service.detect_local_device(db, settings.data_dir, refresh=refresh)
