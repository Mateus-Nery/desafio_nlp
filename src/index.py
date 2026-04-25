"""Indexador ANEEL — chunks.jsonl → Qdrant (dense+sparse) + BM25 pickle.

Fase 4 do RAG:

  chunks.jsonl
       │
       ├──▶ BM25 (rank_bm25)         ──▶ artifacts/bm25_index.pkl
       │
       └──▶ bge-m3 (FlagEmbedding)
                ├─ dense  (1024-dim, cosine)
                └─ sparse (lexical token weights)
            ──▶ Qdrant collection `aneel_chunks` (named vectors)
                payload: chunk_id, doc_id, tipo_ato, year, tier,
                         section_type, section_label, title, url

Uso:
  # Subir Qdrant primeiro
  docker compose up -d

  # Indexação completa
  python -m src.index --chunks artifacts/chunks.jsonl \\
                      --bm25-out artifacts/bm25_index.pkl

  # Smoke test (200 chunks, sem Qdrant)
  python -m src.index --chunks artifacts/chunks.jsonl \\
                      --bm25-out /tmp/bm25.pkl --limit 200 --skip-dense
"""
from __future__ import annotations

import argparse
import json
import logging
import pickle
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger("index")

QDRANT_DEFAULT_URL = "http://localhost:6333"
QDRANT_COLLECTION = "aneel_chunks"
BGE_M3_MODEL = "BAAI/bge-m3"
DENSE_DIM = 1024
DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "sparse"

# Tokenização para BM25: split simples por não-palavra, lowercase. Sem stopwords
# (texto jurídico tem conectores carregando significado; rank_bm25 já modula via IDF).
TOKEN_RE = re.compile(r"\w+", re.UNICODE)


# ──────────────────────────────────────────────────────────────────────────────
# I/O
# ──────────────────────────────────────────────────────────────────────────────


def iter_chunks(path: Path, limit: int | None = None) -> Iterator[dict]:
    # encoding="utf-8" explícito: no Windows o default é cp1252 e quebra em
    # bytes >= 0x80 (texto jurídico PT-BR tem 0x81 com frequência). Mesmo
    # fix aplicado anteriormente em src/chunk.py (commit 0056f65).
    with path.open(encoding="utf-8") as f:
        for i, line in enumerate(f):
            if limit and i >= limit:
                return
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def load_chunks(path: Path, limit: int | None = None) -> list[dict]:
    chunks = list(iter_chunks(path, limit))
    logger.info("Carregados %d chunks de %s", len(chunks), path)
    return chunks


def tokenize_bm25(text: str) -> list[str]:
    return [t.lower() for t in TOKEN_RE.findall(text)]


# ──────────────────────────────────────────────────────────────────────────────
# BM25
# ──────────────────────────────────────────────────────────────────────────────


def build_bm25(chunks: list[dict], out_path: Path) -> None:
    from rank_bm25 import BM25Okapi

    logger.info("Tokenizando %d chunks para BM25...", len(chunks))
    t0 = time.time()
    corpus_tokens = [tokenize_bm25(c["text"]) for c in chunks]
    logger.info("Tokenização concluída em %.1fs", time.time() - t0)

    logger.info("Construindo índice BM25 (Okapi BM25)...")
    t0 = time.time()
    bm25 = BM25Okapi(corpus_tokens)
    logger.info("Índice construído em %.1fs", time.time() - t0)

    chunk_ids = [c["chunk_id"] for c in chunks]
    payload_min = [
        {k: c.get(k) for k in ("chunk_id", "doc_id", "tipo_ato", "year", "tier",
                               "section_type", "section_label", "title", "url")}
        for c in chunks
    ]
    artifact = {
        "bm25": bm25,
        "chunk_ids": chunk_ids,
        "payloads": payload_min,
        "tokenizer": "regex_word_lower",
        "n_chunks": len(chunks),
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as f:
        pickle.dump(artifact, f, protocol=pickle.HIGHEST_PROTOCOL)
    size_mb = out_path.stat().st_size / 1e6
    logger.info("BM25 serializado em %s (%.1f MB)", out_path, size_mb)


# ──────────────────────────────────────────────────────────────────────────────
# Dense + Sparse via bge-m3 → Qdrant
# ──────────────────────────────────────────────────────────────────────────────


def detect_device() -> str:
    """Autodetect: CUDA → MPS (Apple) → CPU."""
    import torch
    if torch.cuda.is_available():
        return "cuda"
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def ensure_collection(client, collection: str) -> None:
    from qdrant_client.http import models as qm

    existing = {c.name for c in client.get_collections().collections}
    if collection in existing:
        logger.info("Coleção '%s' já existe", collection)
        return

    logger.info("Criando coleção '%s' (dense=%d, sparse)", collection, DENSE_DIM)
    client.create_collection(
        collection_name=collection,
        vectors_config={
            DENSE_VECTOR_NAME: qm.VectorParams(size=DENSE_DIM, distance=qm.Distance.COSINE),
        },
        sparse_vectors_config={
            SPARSE_VECTOR_NAME: qm.SparseVectorParams(),
        },
    )
    # Índices de payload para filtros eficientes
    for field in ("tipo_ato", "year", "tier", "doc_id"):
        client.create_payload_index(
            collection_name=collection,
            field_name=field,
            field_schema=qm.PayloadSchemaType.KEYWORD if field != "year"
                          else qm.PayloadSchemaType.INTEGER,
        )


def index_dense_sparse(
    chunks: list[dict],
    qdrant_url: str,
    collection: str,
    batch_size: int,
) -> None:
    from FlagEmbedding import BGEM3FlagModel
    from qdrant_client import QdrantClient
    from qdrant_client.http import models as qm

    device = detect_device()
    logger.info("Carregando bge-m3 (device=%s)...", device)
    model = BGEM3FlagModel(BGE_M3_MODEL, use_fp16=(device != "cpu"), device=device)

    client = QdrantClient(url=qdrant_url, prefer_grpc=False, timeout=120)
    ensure_collection(client, collection)

    payload_keys = ("chunk_id", "doc_id", "tipo_ato", "year", "tier",
                    "section_type", "section_label", "section_parent",
                    "title", "ementa", "filename", "url",
                    "char_start", "char_end", "n_chars", "n_tokens_est")

    n_total = len(chunks)
    n_done = 0
    t0 = time.time()
    for i in range(0, n_total, batch_size):
        batch = chunks[i:i + batch_size]
        texts = [c["text"] for c in batch]

        out = model.encode(
            texts,
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=False,
        )
        dense_vecs = out["dense_vecs"]            # np.ndarray (B, 1024)
        sparse_list = out["lexical_weights"]      # list[dict[token_id_str → float]]

        points = []
        for j, c in enumerate(batch):
            sparse = sparse_list[j]
            sparse_vec = qm.SparseVector(
                indices=[int(k) for k in sparse.keys()],
                values=[float(v) for v in sparse.values()],
            )
            payload = {k: c.get(k) for k in payload_keys}
            payload["text"] = c["text"]   # fica no payload p/ serving sem outro lookup
            points.append(
                qm.PointStruct(
                    id=_chunk_id_to_uuid(c["chunk_id"]),
                    vector={
                        DENSE_VECTOR_NAME: dense_vecs[j].tolist(),
                        SPARSE_VECTOR_NAME: sparse_vec,
                    },
                    payload=payload,
                )
            )

        client.upsert(collection_name=collection, points=points, wait=False)
        n_done += len(batch)

        if (i // batch_size) % 20 == 0 or n_done == n_total:
            elapsed = time.time() - t0
            rate = n_done / elapsed if elapsed else 0
            eta_min = (n_total - n_done) / rate / 60 if rate else 0
            logger.info(
                "Progresso: %d/%d (%.1f%%)  %.1f ch/s  ETA %.1f min",
                n_done, n_total, 100 * n_done / n_total, rate, eta_min,
            )

    logger.info("Indexação dense+sparse concluída em %.1f min", (time.time() - t0) / 60)


def _chunk_id_to_uuid(chunk_id: str) -> str:
    """Qdrant aceita int ou UUID. Derivamos UUID determinístico do chunk_id."""
    import uuid
    return str(uuid.uuid5(uuid.NAMESPACE_URL, chunk_id))


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--chunks", type=Path, required=True, help="Entrada chunks.jsonl")
    p.add_argument("--bm25-out", type=Path, default=Path("artifacts/bm25_index.pkl"),
                   help="Saída do índice BM25")
    p.add_argument("--qdrant-url", default=QDRANT_DEFAULT_URL,
                   help=f"URL do Qdrant (default {QDRANT_DEFAULT_URL})")
    p.add_argument("--collection", default=QDRANT_COLLECTION)
    p.add_argument("--batch-size", type=int, default=32,
                   help="Batch p/ encode bge-m3 (32 OK em GPU consumer)")
    p.add_argument("--limit", type=int, default=None, help="Processa só N chunks (smoke)")
    p.add_argument("--skip-bm25", action="store_true")
    p.add_argument("--skip-dense", action="store_true")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if not args.chunks.exists():
        logger.error("Chunks não encontrados: %s", args.chunks)
        return 2

    chunks = load_chunks(args.chunks, limit=args.limit)
    if not chunks:
        logger.error("Nenhum chunk carregado")
        return 2

    if not args.skip_bm25:
        build_bm25(chunks, args.bm25_out)

    if not args.skip_dense:
        index_dense_sparse(
            chunks,
            qdrant_url=args.qdrant_url,
            collection=args.collection,
            batch_size=args.batch_size,
        )

    logger.info("Indexação concluída.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
