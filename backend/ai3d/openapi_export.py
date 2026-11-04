"""Write the OpenAPI schema without starting a server.

Used by ``pnpm gen:api`` to generate TypeScript types for the frontend:
``python -m ai3d.openapi_export <output.json>``
"""

import json
import sys
import tempfile
from pathlib import Path

from ai3d.core.config import Settings
from ai3d.main import create_app


def export(target: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(_env_file=None, data_dir=Path(tmp), log_level="WARNING")
        schema = create_app(settings).openapi()
    target.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python -m ai3d.openapi_export <output.json>")
    export(Path(sys.argv[1]))
