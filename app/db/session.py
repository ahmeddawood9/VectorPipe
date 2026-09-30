"""Engine / session factory creation and start-up checks."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker


def create_db_engine(database_url: str) -> Engine:
    kwargs: dict = {"pool_pre_ping": True}
    if database_url.startswith("postgresql"):
        kwargs.update(
            pool_size=5,
            max_overflow=10,
            pool_timeout=10,
            pool_recycle=1800,
            # Sensible timeouts: fail fast on connect, and never let one statement hang forever.
            connect_args={"connect_timeout": 10, "options": "-c statement_timeout=30000"},
        )
    elif database_url.startswith("sqlite"):
        # SQLite is only supported for the test-suite; wait for locks instead of failing.
        kwargs["connect_args"] = {"timeout": 30}
    return create_engine(database_url, **kwargs)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def check_database(engine: Engine) -> None:
    """Raise ``RuntimeError`` with an actionable message if the DB is unreachable or unmigrated."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            has_table = inspect(conn).has_table("documents")
    except Exception as exc:  # noqa: BLE001 - we re-raise with context
        raise RuntimeError(
            f"Cannot connect to the database ({type(exc).__name__}). "
            "Check DATABASE_URL and that PostgreSQL is running."
        ) from exc
    if not has_table:
        raise RuntimeError("The 'documents' table does not exist. Run migrations first: alembic upgrade head")
