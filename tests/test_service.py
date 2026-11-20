"""API tests: the endpoint against a seeded test database."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from tests.conftest import unit_vector

NOW = datetime(2026, 8, 14, tzinfo=UTC)


def add_company(conn, reg, name, nature, vector, *, industry="Retail", state="Johor", status="active"):
    conn.execute(
        "INSERT INTO raw_events (message_id, body) VALUES (%s, '{}') ON CONFLICT DO NOTHING",
        (f"msg-{reg}",),
    )
    conn.execute(
        """
        INSERT INTO companies (registration_number, company_name, nature_of_business, industry,
                               state, status, updated_at, last_message_id, embedding, embedded_text)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (reg, name, nature, industry, state, status, NOW, f"msg-{reg}", vector, nature),
    )


@pytest.fixture
def client(sync_db):
    from service.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def seeded(sync_db):
    add_company(sync_db, "SYN-000001", "Target Toys", "Toy retail", unit_vector(1, 0))
    add_company(sync_db, "SYN-000002", "Close Toys", "Toy shop", unit_vector(1, 0.1))
    add_company(sync_db, "SYN-000003", "Mid Toys", "Games retail", unit_vector(1, 0.5))
    add_company(sync_db, "SYN-000004", "Far Bakery", "Bakery", unit_vector(0, 1), industry=None, state=None)
    add_company(sync_db, "SYN-000005", "Gone Toys", "Toy retail", unit_vector(1, 0), status="struck_off")
    add_company(sync_db, "SYN-000006", "No Description Sdn. Bhd.", None, None)
    return sync_db


def regs(response) -> list[str]:
    return [r["registrationNumber"] for r in response.json()["results"]]


def test_results_are_most_similar_first(client, seeded):
    response = client.get("/companies/SYN-000001/similar")

    assert response.status_code == 200
    body = response.json()
    assert body["registrationNumber"] == "SYN-000001"
    assert body["natureOfBusiness"] == "Toy retail"
    assert regs(response) == ["SYN-000002", "SYN-000003", "SYN-000004"]
    similarities = [r["similarity"] for r in body["results"]]
    assert similarities == sorted(similarities, reverse=True)


def test_results_have_what_the_card_needs(client, seeded):
    first = client.get("/companies/SYN-000001/similar").json()["results"][0]
    assert first == {
        "registrationNumber": "SYN-000002",
        "companyName": "Close Toys",
        "natureOfBusiness": "Toy shop",
        "industry": "Retail",
        "state": "Johor",
        "similarity": pytest.approx(first["similarity"]),
    }


def test_excludes_itself_struck_off_and_unembedded(client, seeded):
    found = regs(client.get("/companies/SYN-000001/similar?limit=50"))
    assert "SYN-000001" not in found  # itself
    assert "SYN-000005" not in found  # struck off
    assert "SYN-000006" not in found  # no embedding


def test_missing_card_fields_are_null_not_an_error(client, seeded):
    results = client.get("/companies/SYN-000001/similar").json()["results"]
    bakery = next(r for r in results if r["registrationNumber"] == "SYN-000004")
    assert bakery["industry"] is None and bakery["state"] is None


def test_limit(client, seeded):
    assert regs(client.get("/companies/SYN-000001/similar?limit=1")) == ["SYN-000002"]


@pytest.mark.parametrize("limit", ["0", "51", "abc"])
def test_invalid_limit_is_422(client, seeded, limit):
    assert client.get(f"/companies/SYN-000001/similar?limit={limit}").status_code == 422


def test_registration_number_is_normalised(client, seeded):
    response = client.get("/companies/%20syn-000001%20/similar")
    assert response.status_code == 200
    assert response.json()["registrationNumber"] == "SYN-000001"


def test_invalid_registration_number_is_400(client, seeded):
    assert client.get("/companies/not-a-number/similar").status_code == 400


def test_unknown_company_is_404(client, seeded):
    assert client.get("/companies/SYN-999999/similar").status_code == 404


def test_company_without_embedding_returns_empty_results(client, seeded):
    response = client.get("/companies/SYN-000006/similar")
    assert response.status_code == 200
    assert response.json() == {"registrationNumber": "SYN-000006", "natureOfBusiness": None, "results": []}
