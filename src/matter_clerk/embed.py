from __future__ import annotations

from sentence_transformers import SentenceTransformer

_MODEL: SentenceTransformer | None = None
_MODEL_NAME: str | None = None


def _get_model(name: str) -> SentenceTransformer:
    global _MODEL, _MODEL_NAME
    if _MODEL is None or _MODEL_NAME != name:
        _MODEL = SentenceTransformer(name)
        _MODEL_NAME = name
    return _MODEL


def embed(texts: list[str], model_name: str) -> list[list[float]]:
    model = _get_model(model_name)
    vectors = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return [v.tolist() for v in vectors]


def embedding_dimension(model_name: str) -> int:
    return _get_model(model_name).get_sentence_embedding_dimension()
