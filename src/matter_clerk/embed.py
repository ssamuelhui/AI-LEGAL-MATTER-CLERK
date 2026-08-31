from __future__ import annotations

import json
import os
import threading

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

from .paths import embedding_model_dir

# --------------------------------------------------------------------------
# Local embeddings via ONNX Runtime.
#
# Phase 3 Session 3 replaced sentence-transformers + torch with onnxruntime +
# tokenizers. Same model (BAAI/bge-small-en-v1.5), same 384-dim normalised
# vectors, ~700 MB less to ship. See docs/ARCHITECTURE.md.
#
# The model is three ops in a trench coat, and all three are reproduced here
# explicitly rather than inherited from a framework:
#   1. WordPiece tokenize (lowercasing lives in tokenizer.json's normalizer)
#   2. BERT forward pass -> last_hidden_state
#   3. CLS pooling (token 0) then L2 normalise
# Step 3 is what bge-small-en-v1.5's own modules.json specifies: Transformer ->
# Pooling(pooling_mode_cls_token=true) -> Normalize. It is NOT mean pooling,
# and it does NOT use the BERT pooler dense layer.
# --------------------------------------------------------------------------

# The one model this backend can serve. EMBEDDING_MODEL is still read from the
# environment by callers; if it names anything else we raise rather than
# silently return bge vectors under another model's name -- which would poison
# a Chroma collection in a way that is invisible until retrieval quality drops.
BUNDLED_MODEL = "BAAI/bge-small-en-v1.5"

MAX_SEQ_LENGTH = 512   # sentence_bert_config.json
BATCH_SIZE = 32        # bounds peak memory; unrelated to output values

_LOCK = threading.Lock()
_SESSION: ort.InferenceSession | None = None
_TOKENIZER: Tokenizer | None = None
_DIMENSION: int | None = None


def _check_model_name(name: str) -> None:
    if name != BUNDLED_MODEL:
        raise ValueError(
            f"EMBEDDING_MODEL is {name!r} but this build bundles only "
            f"{BUNDLED_MODEL!r}. Downloading a different model at run time was "
            "removed in Phase 3 Session 3 so the application works offline."
        )


def _load() -> tuple[ort.InferenceSession, Tokenizer, int]:
    """Load the ONNX session and tokenizer once, under a lock.

    The lock matters: the Flask server is threaded, and two concurrent first
    requests would otherwise both pay the ~1 s session construction and race
    on the globals.
    """
    global _SESSION, _TOKENIZER, _DIMENSION
    with _LOCK:
        if _SESSION is not None and _TOKENIZER is not None and _DIMENSION is not None:
            return _SESSION, _TOKENIZER, _DIMENSION

        d = embedding_model_dir()
        onnx_file = d / "onnx" / "model.onnx"
        tok_file = d / "tokenizer.json"
        cfg_file = d / "config.json"
        for f in (onnx_file, tok_file, cfg_file):
            if not f.is_file():
                raise FileNotFoundError(
                    f"Embedding model file missing: {f}. In a source checkout "
                    "run scripts/fetch_model.ps1; in a bundle this indicates a "
                    "broken install."
                )

        tokenizer = Tokenizer.from_file(str(tok_file))
        tokenizer.enable_truncation(max_length=MAX_SEQ_LENGTH)
        pad_id = tokenizer.token_to_id("[PAD]")
        tokenizer.enable_padding(pad_id=pad_id if pad_id is not None else 0,
                                 pad_token="[PAD]")

        opts = ort.SessionOptions()
        # Default is all cores. On a laptop that also runs OCR and the Flask
        # server, letting ORT saturate every core makes the UI stutter without
        # making ingest meaningfully faster -- the batch is small.
        opts.intra_op_num_threads = min(4, os.cpu_count() or 4)
        session = ort.InferenceSession(
            str(onnx_file), sess_options=opts, providers=["CPUExecutionProvider"]
        )

        dimension = int(json.loads(cfg_file.read_text(encoding="utf-8"))["hidden_size"])

        _SESSION, _TOKENIZER, _DIMENSION = session, tokenizer, dimension
        return session, tokenizer, dimension


def _encode_batch(
    session: ort.InferenceSession, tokenizer: Tokenizer, batch: list[str]
) -> np.ndarray:
    encodings = tokenizer.encode_batch(batch)
    input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
    attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
    token_type_ids = np.array([e.type_ids for e in encodings], dtype=np.int64)

    (last_hidden_state,) = session.run(
        ["last_hidden_state"],
        {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "token_type_ids": token_type_ids,
        },
    )

    cls = last_hidden_state[:, 0, :]                       # CLS pooling
    norms = np.linalg.norm(cls, axis=1, keepdims=True)
    norms[norms == 0] = 1.0                                # a zero vector stays zero
    return cls / norms                                     # L2 normalise


def embed(texts: list[str], model_name: str) -> list[list[float]]:
    _check_model_name(model_name)
    if not texts:
        return []
    session, tokenizer, _ = _load()

    out: list[list[float]] = []
    for start in range(0, len(texts), BATCH_SIZE):
        vectors = _encode_batch(session, tokenizer, texts[start : start + BATCH_SIZE])
        out.extend(v.tolist() for v in vectors)
    return out


def embedding_dimension(model_name: str) -> int:
    _check_model_name(model_name)
    return _load()[2]
