from pathlib import Path
from typing import Final, Sequence

import numpy as np
from fastembed import TextEmbedding

MODEL_NAME: Final = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DIMENSIONS: Final = 384


class Embedder:
    def __init__(
        self, model_name: str = MODEL_NAME, cache_dir: Path = Path(".cache/fastembed")
    ) -> None:
        self._model = TextEmbedding(model_name, cache_dir=str(cache_dir))

    def embed(self, texts: Sequence[str]) -> list[np.ndarray]:
        if isinstance(texts, str):
            raise TypeError("embed() takes a list of strings, not a single string")
        # This model doesn't L2-normalise its output. Cosine distance in pgvector
        # doesn't need it, but unit vectors keep stored data consistent with the
        # evaluation script and make dot product == cosine similarity.
        return [v / np.linalg.norm(v) for v in self._model.embed(list(texts))]
