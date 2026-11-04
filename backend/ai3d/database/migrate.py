"""Programmatic Alembic access.

Migrations ship inside the package so an installed backend (Docker image,
desktop sidecar) can upgrade its own database on start-up.
"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    # ConfigParser interpolation: escape % in passwords.
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def upgrade_to_head(engine: Engine) -> None:
    config = alembic_config(engine.url.render_as_string(hide_password=False))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def downgrade_to_base(engine: Engine) -> None:
    config = alembic_config(engine.url.render_as_string(hide_password=False))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "base")
