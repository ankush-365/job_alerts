"""Test database selection.

By default every test gets its own SQLite file. Set TEST_DATABASE_URL to a disposable PostgreSQL
database to run the same tests against Postgres (the production engine); its public schema is
wiped before each test that asks for a database.
"""

import os

from sqlalchemy import text

from database.repository import make_engine

PG_URL = os.environ.get("TEST_DATABASE_URL")
if PG_URL:
    os.environ.setdefault("DB_NULLPOOL", "1")  # many short-lived engines in tests: don't keep connections open


def fresh_db_url(tmp_path, name: str = "test") -> str:
    if not PG_URL:
        return f"sqlite:///{tmp_path / (name + '.db')}"
    engine = make_engine(PG_URL)
    with engine.begin() as conn:
        conn.execute(text("SET lock_timeout = '10s'"))
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    return PG_URL
