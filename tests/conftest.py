"""Shared fixtures.

Database tests run against a throwaway `companies_test` database on the same
Postgres that `just up` starts, so the real `companies` data is never touched.
If Postgres isn't reachable those tests are skipped; the parser tests still run.
"""

import hashlib
import os
from pathlib import Path

import numpy as np
import psycopg
import pytest
from pgvector.psycopg import register_vector, register_vector_async

PG_PORT = os.environ.get("POSTGRES_PORT", "5432")
ADMIN_URL = f"postgresql://postgres:postgres@localhost:{PG_PORT}/postgres"
TEST_DB = "companies_test"
TEST_URL = f"postgresql://postgres:postgres@localhost:{PG_PORT}/{TEST_DB}"

# service.main reads POSTGRES_URL at import time, so set it before any test imports it.
os.environ["POSTGRES_URL"] = TEST_URL

SCHEMA_PATH = Path(__file__).parents[1] / "consumer" / "schema.sql"
DIM = 384


def unit_vector(*weights: float) -> np.ndarray:
    """A normalised 384-d vector whose first components are `weights`."""
    v = np.zeros(DIM, dtype=np.float32)
    v[: len(weights)] = weights
    return v / np.linalg.norm(v)


class FakeEmbedder:
    """Deterministic stand-in for the real model: same text -> same vector.

    Records every text it is asked to embed, so tests can check when the
    consumer does or doesn't call the model.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, texts):
        self.calls.extend(texts)
        out = []
        for text in texts:
            seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "big")
            v = np.random.default_rng(seed).standard_normal(DIM).astype(np.float32)
            out.append(v / np.linalg.norm(v))
        return out


@pytest.fixture(scope="session")
def test_database() -> str:
    """Create a fresh test database with the schema applied, once per session."""
    try:
        admin = psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("Postgres is not running (start it with `just up`)")
    with admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {TEST_DB}")
    with psycopg.connect(TEST_URL) as conn:
        conn.execute(SCHEMA_PATH.read_bytes())
    return TEST_URL


def _truncate(conn) -> None:
    conn.execute("TRUNCATE companies, raw_events")


@pytest.fixture
async def db(test_database):
    """Async connection to an empty test database (the consumer's view)."""
    conn = await psycopg.AsyncConnection.connect(test_database)
    await register_vector_async(conn)
    await conn.execute("TRUNCATE companies, raw_events")
    await conn.commit()
    yield conn
    await conn.close()


@pytest.fixture
def sync_db(test_database):
    """Sync connection to an empty test database, for seeding service tests."""
    with psycopg.connect(test_database, autocommit=True) as conn:
        register_vector(conn)
        _truncate(conn)
        yield conn
