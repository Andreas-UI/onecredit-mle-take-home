"""Compares embedding models on the feed's 173 unique business descriptions. Offline: no database.

Test set (descriptions.csv, pairs.csv): every unique description, labelled with its
business concept, language (en/ms), location and industry.

Metrics
  industry P@4  For each description, share of its 4 nearest other descriptions in the same industry.
  concept R@1   For each Malay description, is the nearest English description the same business type?
                (Lenient: 'Hotel butik di Penang' -> 'Boutique hotel in Ipoh' counts as correct.)
  pair R@1      Same, but the nearest English must be the exact translation, location included.
  ms/text       Embedding time per description on this machine.
"""

import csv
import sys
import time
from pathlib import Path

import numpy as np
from fastembed import TextEmbedding

EVAL_DIR = Path(__file__).parent
DEFAULT_MODELS = [
    "BAAI/bge-small-en-v1.5",
    "snowflake/snowflake-arctic-embed-s",
    "sentence-transformers/all-MiniLM-L6-v2",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
]
K = 4


def read_csv(name: str) -> list[dict[str, str]]:
    with open(EVAL_DIR / name, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def embed(model: TextEmbedding, texts: list[str]) -> np.ndarray:
    vectors = np.array(list(model.embed(texts)))
    return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)  # so dot product = cosine


def industry_precision(vectors: np.ndarray, industries: list[str]) -> float:
    sims = vectors @ vectors.T
    np.fill_diagonal(sims, -np.inf)  # a description never counts as its own neighbour
    top = np.argsort(-sims, axis=1)[:, :K]
    return float(np.mean([[industries[j] == industries[i] for j in row] for i, row in enumerate(top)]))


def evaluate(name: str, rows: list[dict[str, str]], pairs: list[dict[str, str]]) -> dict[str, float]:
    model = TextEmbedding(name, cache_dir=".cache/fastembed")
    texts = [r["description"] for r in rows]

    start = time.perf_counter()
    vectors = embed(model, texts)
    ms_per_text = (time.perf_counter() - start) * 1000 / len(texts)

    index = {t: i for i, t in enumerate(texts)}
    english = [i for i, r in enumerate(rows) if r["language"] == "en"]
    malay = [i for i, r in enumerate(rows) if r["language"] == "ms"]

    # nearest English description for every Malay description, searched among ALL English ones
    nearest = {m: english[int(np.argmax(vectors[english] @ vectors[m]))] for m in malay}
    concept_r1 = np.mean([rows[nearest[m]]["concept"] == rows[m]["concept"] for m in malay])
    pair_r1 = np.mean([texts[nearest[index[p["malay"]]]] == p["english"] for p in pairs])

    return {
        "industry P@4": industry_precision(vectors, [r["industry"] for r in rows]),
        "concept R@1": float(concept_r1),
        "pair R@1": float(pair_r1),
        "ms/text": ms_per_text,
    }


def main() -> None:
    models = sys.argv[1:] or DEFAULT_MODELS
    rows, pairs = read_csv("descriptions.csv"), read_csv("pairs.csv")
    n_ms = sum(r["language"] == "ms" for r in rows)
    print(f"{len(rows)} descriptions ({n_ms} Malay), {len(pairs)} exact Malay-English pairs\n")

    header = ["industry P@4", "concept R@1", "pair R@1", "ms/text"]
    print(f"{'model':62}" + "".join(f"{h:>14}" for h in header))
    for name in models:
        result = evaluate(name, rows, pairs)
        print(f"{name:62}" + "".join(f"{result[h]:14.3f}" for h in header), flush=True)


if __name__ == "__main__":
    main()
