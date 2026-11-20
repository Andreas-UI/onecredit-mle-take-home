set shell := ["bash", "-euo", "pipefail", "-c"]

default: setup

# Install the producer's dependencies.
setup:
    uv sync --locked

# Start Postgres (with pgvector) and RabbitMQ, and wait until both are healthy.
up:
    docker compose up -d --wait

# Stop everything and delete the volumes.
down:
    docker compose down -v

# Replay the registry feed onto the company-events queue.
publish:
    uv run python -m producer.publish

# Run the consumer.
consume:
    uv run python -m consumer.consume

# delete all data, start the stack, and republish the whole feed.
reset: down up publish

# Run the similar-companies API on http://127.0.0.1:8000.
serve:
    uv run uvicorn service.main:app --host 127.0.0.1 --port 8000 --reload

# Run the tests
test:
    uv run pytest -v
