from typing_extensions import Final

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
import psycopg
from pgvector.psycopg import register_vector_async
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from consumer.parser import normalize_registration_number

pg_port = os.environ.get("POSTGRES_PORT", "5432")
DB_URL: Final = os.environ.get(
    "POSTGRES_URL",
    f"postgresql://postgres:postgres@localhost:{pg_port}/companies",
)

TARGET_SQL: Final = """
SELECT embedding, nature_of_business FROM companies WHERE registration_number = %s
"""

SIMILAR_SQL: Final = """
SELECT registration_number AS "registrationNumber", company_name AS "companyName",
       nature_of_business AS "natureOfBusiness", industry, state,
       1 - (embedding <=> %(vec)s) AS similarity
FROM companies
WHERE registration_number <> %(reg)s AND status = 'active' AND embedding IS NOT NULL
ORDER BY embedding <=> %(vec)s
LIMIT %(limit)s
"""


class Company(BaseModel):
    registrationNumber: str
    companyName: str | None
    natureOfBusiness: str | None
    industry: str | None
    state: str | None
    similarity: float


class SimilarCompanies(BaseModel):
    registrationNumber: str
    natureOfBusiness: str | None
    results: list[Company]


async def configure(conn: psycopg.AsyncConnection) -> None:
    await register_vector_async(conn)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    pool = AsyncConnectionPool(
        DB_URL,
        min_size=1,
        max_size=10,
        open=False,
        configure=configure,
        kwargs={"autocommit": True, "row_factory": dict_row},
    )
    await pool.open(wait=True)
    app.state.pool = pool
    yield
    await pool.close()


app = FastAPI(title="Similar companies", lifespan=lifespan)


@app.get("/companies/{registration_number}/similar", response_model=SimilarCompanies)
async def get_similar_companies(
    request: Request,
    registration_number: str,
    limit: int = Query(default=4, ge=1, le=50, description="Max results to return"),
) -> SimilarCompanies:
    try:
        registration_number = normalize_registration_number(registration_number)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    async with request.app.state.pool.connection() as conn:
        target = await (
            await conn.execute(TARGET_SQL, (registration_number,))
        ).fetchone()
        if target is None:
            raise HTTPException(
                status_code=404,
                detail=f"Company with registration number {registration_number} not found",
            )

        params = {
            "reg": registration_number,
            "limit": limit,
            "vec": target["embedding"],
        }

        rows = []
        if target["embedding"] is not None:
            rows = await (await conn.execute(SIMILAR_SQL, params)).fetchall()

    return SimilarCompanies(
        registrationNumber=registration_number,
        natureOfBusiness=target["nature_of_business"],
        results=[Company(**row) for row in rows],
    )
