"""Engine and session management."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker


def _configure_sqlite(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        # WAL lets the API read while a worker writes; foreign keys are off by
        # default in SQLite; busy_timeout avoids spurious "database is locked".
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


def create_db_engine(url: str, *, echo: bool = False) -> Engine:
    connect_args: dict[str, Any] = {}
    if url.startswith("sqlite"):
        # Connections are used from FastAPI's threadpool and worker threads.
        connect_args["check_same_thread"] = False
    engine = create_engine(url, echo=echo, connect_args=connect_args, pool_pre_ping=True)
    if engine.dialect.name == "sqlite":
        _configure_sqlite(engine)
    return engine


class Database:
    """Owns the engine and session factory for one application instance."""

    def __init__(self, url: str, *, echo: bool = False) -> None:
        self.url = url
        self.engine = create_db_engine(url, echo=echo)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Session that commits on success and rolls back on error."""
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def ping(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:
            return False
        return True

    def dispose(self) -> None:
        self.engine.dispose()
