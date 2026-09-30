"""Alembic migrations create exactly the schema the models describe, and can be reverted."""

from __future__ import annotations

import uuid

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.db import Base


@pytest.fixture
def fresh_database_url(database_url, tmp_path):
    """An empty, throw-away database (a temp SQLite file, or a scratch database on the test server)."""
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite":
        yield f"sqlite:///{tmp_path / 'migrations.sqlite3'}"
        return
    name = f"vp_migr_{uuid.uuid4().hex[:8]}"
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:  # noqa: BLE001
        admin.dispose()
        pytest.skip(f"cannot create a scratch database: {exc}")
    try:
        yield url.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def alembic_config(url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def test_upgrade_matches_models_and_downgrade_reverts(fresh_database_url):
    cfg = alembic_config(fresh_database_url)
    command.upgrade(cfg, "head")

    engine = create_engine(fresh_database_url)
    try:
        insp = inspect(engine)
        assert "documents" in insp.get_table_names()
        columns = {c["name"] for c in insp.get_columns("documents")}
        assert columns >= {
            "id", "original_filename", "raw_object_key", "processed_object_key", "status",
            "error_message", "created_at", "updated_at", "completed_at",
        }
        assert {i["name"] for i in insp.get_indexes("documents")} >= {
            "ix_documents_status_created_at", "ix_documents_created_at", "ix_documents_completed_at",
        }
        with engine.connect() as conn:
            drift = compare_metadata(MigrationContext.configure(conn), Base.metadata)
        assert drift == [], f"models and migrations disagree: {drift}"
    finally:
        engine.dispose()

    command.downgrade(cfg, "base")
    engine = create_engine(fresh_database_url)
    try:
        assert "documents" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_status_check_constraint_rejects_unknown_values(fresh_database_url):
    command.upgrade(alembic_config(fresh_database_url), "head")
    engine = create_engine(fresh_database_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO documents (id, original_filename, raw_object_key, status) "
                    "VALUES (:id, 'a', 'raw/a', 'PENDING')"
                ),
                {"id": uuid.uuid4().hex if engine.dialect.name == "sqlite" else str(uuid.uuid4())},
            )
        with pytest.raises(Exception, match="(?i)status_valid|check"):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO documents (id, original_filename, raw_object_key, status) "
                        "VALUES (:id, 'b', 'raw/b', 'BOGUS')"
                    ),
                    {"id": uuid.uuid4().hex if engine.dialect.name == "sqlite" else str(uuid.uuid4())},
                )
    finally:
        engine.dispose()
