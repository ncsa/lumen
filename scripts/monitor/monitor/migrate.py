"""Run the Alembic migrations that ship with the package."""

from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def alembic_config(database_url: str) -> AlembicConfig:
    cfg = AlembicConfig(str(ALEMBIC_INI))
    # ConfigParser treats % as interpolation; URL-encoded passwords contain it.
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return cfg


def upgrade(database_url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(database_url), revision)


def downgrade(database_url: str, revision: str) -> None:
    command.downgrade(alembic_config(database_url), revision)
