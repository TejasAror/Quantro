"""Postgres helpers for Quantro persistence."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources


class DatabaseUnavailable(RuntimeError):
    """Raised when the Postgres driver is not installed."""


class Database:
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - exercised only without optional deps
            raise DatabaseUnavailable("Install psycopg to use DATABASE_URL persistence") from exc

        self._psycopg = psycopg
        self._row_factory = dict_row

    @contextmanager
    def connect(self) -> Iterator:
        with self._psycopg.connect(self._database_url, row_factory=self._row_factory) as conn:
            yield conn

    def migrate(self) -> None:
        migration_pkg = "quantro_api.migrations"
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    create table if not exists quantro_schema_migrations (
                        version text primary key,
                        applied_at timestamptz not null default now()
                    )
                    """
                )
                for migration in sorted(resources.files(migration_pkg).iterdir()):
                    if migration.suffix != ".sql":
                        continue
                    version = migration.name
                    cur.execute(
                        "select 1 from quantro_schema_migrations where version = %s",
                        (version,),
                    )
                    if cur.fetchone() is not None:
                        continue
                    cur.execute(migration.read_text())
                    cur.execute(
                        "insert into quantro_schema_migrations (version) values (%s)",
                        (version,),
                    )
            conn.commit()
