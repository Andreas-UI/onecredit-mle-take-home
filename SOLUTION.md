# Similar companies

A consumer that reads company events from RabbitMQ into Postgres + pgvector, and an API that returns the most similar
companies for the portal's "Similar companies" panel. Matching is based on each company's nature of business, using a
multilingual embedding model, because many descriptions in the feed are in Malay.

Design decisions, assumptions, the 10x plan and what's left are in **[DESIGN.md](DESIGN.md)**. The original
assessment brief is in [README.md](README.md).

## Requirements

- Docker
- [uv](https://docs.astral.sh/uv/) (installs Python 3.14 for you)
- [just](https://github.com/casey/just)

## Quick start

Run these from the repo root.

```bash
just setup      # install dependencies
just up         # start Postgres (with pgvector) and RabbitMQ
just publish    # put the registry feed on the queue
just consume    # terminal 1: load the queue into Postgres (keeps running; Ctrl+C to stop)
just serve      # terminal 2: start the API on http://127.0.0.1:8000
```

The first `just consume` downloads the embedding model (about 240 MB) into `.cache/fastembed/`. Later runs use the
cached copy.

The consumer keeps running and waiting for new messages after the feed is loaded. To check it has finished, run:

```bash
docker compose exec postgres psql -U postgres -d companies -c \
  "SELECT count(*) AS companies, count(embedding) AS with_embedding FROM companies"
```

Once the queue is drained, this shows **2,000 companies, 1,961 with an embedding**. The other 39 have no nature of
business in the feed; see [DESIGN.md](DESIGN.md#whats-left). You can also watch the queue empty in the RabbitMQ UI at
<http://localhost:15672> (user `guest`, password `guest`).

Then call the API:

```bash
curl "http://127.0.0.1:8000/companies/SYN-001550/similar?limit=2"
```

```json
{
  "registrationNumber": "SYN-001550",
  "natureOfBusiness": "Operating cafes and coffee shops in the Klang Valley",
  "results": [
    {
      "registrationNumber": "SYN-000539",
      "companyName": "Seri Sejahtera Food Sdn. Bhd.",
      "natureOfBusiness": "Operating cafes and coffee shops in the Klang Valley",
      "industry": "Food & Beverage",
      "state": "Johor",
      "similarity": 1.0
    },
    {
      "registrationNumber": "SYN-001650",
      "companyName": "Everbright Pacific Food Sdn. Bhd.",
      "natureOfBusiness": "Mengusahakan kedai kopi dan kafe di the Klang Valley",
      "industry": "Food & Beverage",
      "state": "Kuala Lumpur",
      "similarity": 0.9497
    }
  ]
}
```

The second result is in Malay, matched to an English description. Interactive API docs are at
<http://127.0.0.1:8000/docs>.

## API

`GET /companies/{registration_number}/similar?limit=4`

| Parameter | Description |
| --- | --- |
| `registration_number` | For example `SYN-001550`. Lowercase and surrounding spaces are accepted. |
| `limit` | Optional. Number of results, 1–50. Default 4. |

Results come most similar first. A response never includes the company itself, struck-off companies, or companies
with no description.

| Case | Status |
| --- | --- |
| Found | `200` |
| Company has no nature of business | `200` with `"results": []` |
| Registration number not in `SYN-######` form | `400` |
| Unknown company | `404` |
| `limit` not a number, or outside 1–50 | `422` |

`companyName`, `industry` and `state` can be `null` when the feed never provided them.

## Commands

| Command | What it does |
| --- | --- |
| `just setup` | Install dependencies |
| `just up` | Start Postgres and RabbitMQ, and wait until both are healthy |
| `just publish` | Publish the whole feed (`data/feed.jsonl`) to the queue |
| `just consume` | Run the consumer. Safe to stop and restart: it carries on from the queue and keeps existing data |
| `just serve` | Run the API on port 8000, reloading on code changes |
| `just test` | Run the tests (needs `just up` first) |
| `just reset` | **Delete all data**, then start the stack and republish the feed. Run `just consume` afterwards |
| `just down` | Stop everything and delete the data |

Publishing the feed twice is safe: the consumer ignores duplicates and older events, so the result is the same
2,000 companies.

## Tests

```bash
just up
just test
```

There are 61 tests, covering the parser, the consumer (ordering, duplicates, partial updates, strike-offs), how the
consumer handles errors (including losing the database connection mid-run), and the API.

The database tests create and use a separate `companies_test` database, so they never touch your `companies` data.
They use a fake embedder, so they don't need the model download. If Postgres isn't running, the database tests are
skipped and the parser tests still run.

## Embedding model comparison

```bash
uv run python evaluate/compare_models.py
```

This compares four embedding models on the feed's 173 unique descriptions, using a labelled test set in `evaluate/`.
It doesn't need the database. The results, and why I chose `paraphrase-multilingual-MiniLM-L12-v2`, are in
[DESIGN.md](DESIGN.md#embedding-model-comparison). The first run downloads all four models.

## Configuration

Everything works with the defaults. If a port is already taken, export these before `just up`, and use the same shell
for the other commands.

| Variable | Default | Used by |
| --- | --- | --- |
| `POSTGRES_PORT` | `5432` | Docker, consumer, API, tests |
| `RABBITMQ_PORT` | `5672` | Docker, producer, consumer |
| `RABBITMQ_UI_PORT` | `15672` | Docker |
| `POSTGRES_URL` | `postgresql://postgres:postgres@localhost:$POSTGRES_PORT/companies` | Consumer, API |
| `RABBITMQ_URL` | `amqp://guest:guest@localhost:$RABBITMQ_PORT/` | Consumer |

## Project layout

```text
consumer/
  consume.py      RabbitMQ loop: store, parse, upsert, embed, ack; retries and reconnects
  parser.py       Validates and cleans one message
  embedding.py    Embedding model wrapper
  schema.sql      Tables and vector index, applied when the consumer starts
service/
  main.py         The API
tests/            Parser, consumer, consumer error handling and API tests
evaluate/         Embedding model comparison: script and labelled test set
producer/         Replays the feed onto the queue (provided)
data/feed.jsonl   The registry feed (provided)
```

## Troubleshooting

- **`address already in use` on port 8000:** an earlier `just serve` is still running. Stop it with Ctrl+C in its
  terminal, or find it with `ss -ltnp | grep 8000` and `kill` the process.
- **Port 5432, 5672 or 15672 taken:** set the variables under [Configuration](#configuration) and run `just up` again.
- **Results look wrong after changing the embedding model:** stored vectors from the old model don't match new ones.
  Run `just reset`, then `just consume`.
