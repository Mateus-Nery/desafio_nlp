"""Retrieval híbrido ANEEL — query → top-K chunks com RRF de dense + BM25.

Fase 5 do RAG:

  query
    │
    ├──▶ embed dense (bge-m3, 1024-dim)
    │       └─ paralelo:
    │             ├─ Qdrant query_points (using='dense', top=30)
    │             └─ BM25.get_scores → argsort top-30
    │
    └──▶ RRF fusion (k=60) → top-K com payload, scores e ranks por lista

Filtros opcionais por payload: tipo_ato, year, tier (aplicados em ambos os
retrievers — Qdrant via query_filter, BM25 via numpy mask).

Decisões (ver HANDOFF.md):
  - 2 listas (dense + BM25). Sparse do bge-m3 fica como flag opcional.
  - Queries paralelas com ThreadPoolExecutor; RRF manual no cliente.
  - Autodetect device: CUDA → CPU (pula MPS por default).
  - Loads explícitos (sem singleton mágico) — chamador chama 1x e passa os
    objetos para `retrieve()`.

Uso (CLI):
  python -m src.retrieve --query "tarifa de uso do sistema de distribuição"
  python -m src.retrieve --query "..." --tipo-ato ren --year 2022 --top-k 5
  python -m src.retrieve --query "..." --device cpu --json
"""
from __future__ import annotations

import argparse
import json
import logging
import pickle
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger("retrieve")

# ──────────────────────────────────────────────────────────────────────────────
# Configuração
# ──────────────────────────────────────────────────────────────────────────────

QDRANT_DEFAULT_URL = "http://localhost:6333"
QDRANT_COLLECTION = "aneel_chunks"
DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "sparse"
BGE_M3_MODEL = "BAAI/bge-m3"
BGE_RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"
BM25_DEFAULT_PATH = Path("artifacts/bm25_index.pkl")

DENSE_TOP_N = 30          # candidatos do dense por query
BM25_TOP_N = 30           # candidatos do BM25 por query
RRF_K = 60                # constante do Reciprocal Rank Fusion (Cormack et al.)
RERANK_INPUT_TOP_N = 30   # quantos chunks do RRF entram no reranker (cross-encoder)
DEFAULT_TOP_K = 10        # quanto retornar após fusão (e rerank, se ativado)

# Mesmo tokenizer usado no índice (src/index.py)
TOKEN_RE = re.compile(r"\w+", re.UNICODE)


@dataclass
class Hit:
    chunk_id: str
    score: float                       # score final (rerank se ativado, senão RRF)
    score_rrf: float                   # sempre presente
    score_rerank: float | None         # None se rerank desligado
    rank_dense: int | None             # 1-indexed; None se não apareceu na lista dense
    rank_bm25: int | None
    payload: dict[str, Any]            # texto cru + metadados (chunk_id, doc_id, tipo_ato, etc.)


# ──────────────────────────────────────────────────────────────────────────────
# Loaders explícitos (chamados pelo entrypoint — não cacheiam por mágica)
# ──────────────────────────────────────────────────────────────────────────────


def detect_device(prefer: str = "auto") -> str:
    """auto: CUDA → CPU. Pula MPS por default por bugs históricos do
    FlagReranker em Apple Silicon. Use --device mps explicitamente se quiser."""
    if prefer != "auto":
        return prefer
    import torch
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def load_bm25(path: Path = BM25_DEFAULT_PATH) -> dict:
    """Carrega o índice BM25 do disco (~1-3s, 244 MB).

    Estrutura retornada (igual ao que `src/index.py` serializa):
        {
            "bm25": BM25Okapi(...),
            "chunk_ids": [...],
            "payloads": [{...}, ...],
            "tokenizer": "regex_word_lower",
            "n_chunks": int,
        }
    """
    if not path.exists():
        raise FileNotFoundError(
            f"BM25 index não encontrado em {path}. "
            f"Baixe do Release v0.4.0 ou rode `python -m src.index --chunks ...`."
        )
    logger.info("Carregando BM25 de %s...", path)
    t0 = time.time()
    with path.open("rb") as f:
        data = pickle.load(f)
    logger.info("BM25 carregado em %.1fs (%d chunks)", time.time() - t0, data["n_chunks"])
    return data


class CrossEncoderReranker:
    """Wrapper minimalista pro bge-reranker-v2-m3 (cross-encoder XLM-RoBERTa).

    Não usamos `FlagEmbedding.FlagReranker` porque ele depende de
    `XLMRobertaTokenizer.prepare_for_model`, removido em transformers 5.x.
    Implementação direta com `AutoTokenizer(use_fast=True)` evita o problema.

    API compatível com FlagReranker pra facilitar migração futura:
        compute_score(pairs, normalize=True) -> list[float]
    """

    def __init__(self, model_name: str, device: str, use_fp16: bool = False,
                 max_length: int = 512):
        from transformers import AutoTokenizer, AutoModelForSequenceClassification
        import torch

        self.tokenizer = AutoTokenizer.from_pretrained(model_name, use_fast=True)
        self.model = AutoModelForSequenceClassification.from_pretrained(model_name)
        if use_fp16:
            self.model = self.model.half()
        self.model.to(device)
        self.model.eval()
        self.device = device
        self.max_length = max_length
        self._torch = torch

    def compute_score(self, pairs: list[tuple[str, str]],
                      normalize: bool = True, batch_size: int = 16) -> list[float]:
        scores: list[float] = []
        for i in range(0, len(pairs), batch_size):
            batch = pairs[i:i + batch_size]
            inputs = self.tokenizer(
                [p[0] for p in batch],
                [p[1] for p in batch],
                padding=True,
                truncation=True,
                max_length=self.max_length,
                return_tensors="pt",
            ).to(self.device)
            with self._torch.no_grad():
                logits = self.model(**inputs).logits
                out = self._torch.sigmoid(logits) if normalize else logits
                scores.extend(out.squeeze(-1).float().cpu().tolist())
        return scores


def load_reranker(device: str = "auto"):
    """Carrega bge-reranker-v2-m3 (~2.3 GB; baixa do HF na 1ª vez).

    Cross-encoder: recebe pares (query, passage) e cospe um score de relevância.
    Mais caro que dense/sparse (não pode pré-computar), mas dá ganho de qualidade
    significativo no top-K final.
    """
    dev = detect_device(device)
    logger.info("Carregando bge-reranker-v2-m3 (device=%s)...", dev)
    t0 = time.time()
    model = CrossEncoderReranker(BGE_RERANKER_MODEL, device=dev, use_fp16=(dev == "cuda"))
    logger.info("bge-reranker-v2-m3 carregado em %.1fs", time.time() - t0)
    return model


def load_embedder(device: str = "auto"):
    """Carrega bge-m3 (~2 GB; baixa do HF na 1ª vez, cached em ~/.cache/huggingface)."""
    from FlagEmbedding import BGEM3FlagModel
    dev = detect_device(device)
    logger.info("Carregando bge-m3 (device=%s)...", dev)
    t0 = time.time()
    model = BGEM3FlagModel(BGE_M3_MODEL, use_fp16=(dev == "cuda"), device=dev)
    logger.info("bge-m3 carregado em %.1fs", time.time() - t0)
    return model


def make_qdrant_client(url: str = QDRANT_DEFAULT_URL):
    from qdrant_client import QdrantClient
    return QdrantClient(url=url, timeout=60)


# ──────────────────────────────────────────────────────────────────────────────
# Single retriever helpers
# ──────────────────────────────────────────────────────────────────────────────


def _dense_search(
    client, embedder, query: str, top_n: int, filter_obj=None,
) -> list[tuple[str, dict, float]]:
    """Returns [(chunk_id, payload, qdrant_score), ...] ordenado por score desc."""
    out = embedder.encode([query], return_dense=True, return_sparse=False)
    dense_vec = out["dense_vecs"][0].tolist()
    result = client.query_points(
        collection_name=QDRANT_COLLECTION,
        query=dense_vec,
        using=DENSE_VECTOR_NAME,
        limit=top_n,
        with_payload=True,
        query_filter=filter_obj,
    )
    return [(p.payload["chunk_id"], p.payload, float(p.score)) for p in result.points]


def _bm25_search(
    bm25_data: dict, query: str, top_n: int, filters: dict | None = None,
) -> list[tuple[str, dict, float]]:
    """Returns [(chunk_id, payload, bm25_score), ...] ordenado por score desc.
    Aplica `filters` (dict {payload_key: valor}) zerando chunks que não casam."""
    import numpy as np

    tokens = [t.lower() for t in TOKEN_RE.findall(query)]
    if not tokens:
        return []

    scores = bm25_data["bm25"].get_scores(tokens)
    chunk_ids = bm25_data["chunk_ids"]
    payloads = bm25_data["payloads"]

    if filters:
        mask = _apply_filter(payloads, filters)
        # -inf garante que filtrados nunca entrem no top
        scores = np.where(mask, scores, -np.inf)

    top_idx = np.argsort(-scores)[:top_n]
    # Descarta chunks com score <= 0 (sem token em comum) e -inf (filtrados)
    out: list[tuple[str, dict, float]] = []
    for i in top_idx:
        s = float(scores[int(i)])
        if s <= 0 or s == float("-inf"):
            continue
        out.append((chunk_ids[int(i)], payloads[int(i)], s))
    return out


def _apply_filter(payloads: list[dict], filters: dict):
    """Retorna numpy bool mask True onde payload casa com TODOS os filtros."""
    import numpy as np
    mask = np.ones(len(payloads), dtype=bool)
    for k, v in filters.items():
        col = np.array([p.get(k) for p in payloads], dtype=object)
        mask = mask & (col == v)
    return mask


# ──────────────────────────────────────────────────────────────────────────────
# RRF (Reciprocal Rank Fusion — Cormack et al., 2009)
# ──────────────────────────────────────────────────────────────────────────────


def rrf_fuse(rankings: list[list[str]], k: int = RRF_K) -> dict[str, float]:
    """Funde N rankings em um dict {chunk_id: rrf_score}.

    score(c) = Σ_i  1 / (k + rank_i(c) + 1)
    onde rank_i(c) é a posição 0-indexed de c na lista i (omitido se ausente).

    k=60 é o padrão do paper original; insensível a magnitudes de score.
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, cid in enumerate(ranking):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
    return scores


# ──────────────────────────────────────────────────────────────────────────────
# Orquestrador
# ──────────────────────────────────────────────────────────────────────────────


def retrieve(
    query: str,
    bm25_data: dict,
    qdrant_client,
    embedder,
    reranker=None,
    top_k: int = DEFAULT_TOP_K,
    filters: dict | None = None,
    parallel: bool = True,
) -> list[Hit]:
    """Hybrid retrieval: dense + BM25 → RRF → (rerank opcional) → top_k Hits.

    Parameters
    ----------
    query        : pergunta do usuário (texto cru)
    bm25_data    : dict carregado por `load_bm25()`
    qdrant_client: instância de `qdrant_client.QdrantClient`
    embedder     : `BGEM3FlagModel` carregada por `load_embedder()`
    reranker     : `FlagReranker` opcional carregada por `load_reranker()`.
                   Se fornecida, refina o top-`RERANK_INPUT_TOP_N` do RRF
                   com cross-encoder e ordena pelo score do reranker.
    top_k        : quantos resultados retornar
    filters      : dict opcional {payload_key: value}; aplicado em ambos
                   retrievers (Qdrant via query_filter, BM25 via numpy mask)
    parallel     : True → roda dense e BM25 em threads; False → sequencial

    Returns
    -------
    list[Hit] ordenada por score (rerank se ativado, senão RRF) desc,
    tamanho ≤ top_k.
    """
    from qdrant_client.http import models as qm

    qfilter = None
    if filters:
        conditions = [
            qm.FieldCondition(key=k, match=qm.MatchValue(value=v))
            for k, v in filters.items()
        ]
        qfilter = qm.Filter(must=conditions)

    if parallel:
        with ThreadPoolExecutor(max_workers=2) as exe:
            fut_dense = exe.submit(_dense_search, qdrant_client, embedder, query, DENSE_TOP_N, qfilter)
            fut_bm25 = exe.submit(_bm25_search, bm25_data, query, BM25_TOP_N, filters)
            dense = fut_dense.result()
            bm25 = fut_bm25.result()
    else:
        dense = _dense_search(qdrant_client, embedder, query, DENSE_TOP_N, qfilter)
        bm25 = _bm25_search(bm25_data, query, BM25_TOP_N, filters)

    dense_ids = [c for c, _, _ in dense]
    bm25_ids = [c for c, _, _ in bm25]

    rrf_scores = rrf_fuse([dense_ids, bm25_ids])

    dense_rank = {cid: i + 1 for i, cid in enumerate(dense_ids)}
    bm25_rank = {cid: i + 1 for i, cid in enumerate(bm25_ids)}
    # Payloads: dense traz payload completo (com texto); BM25 só payload mínimo.
    # Damos preferência ao do dense quando há colisão.
    payload_map: dict[str, dict] = {c: p for c, p, _ in bm25}
    payload_map.update({c: p for c, p, _ in dense})

    rrf_sorted_ids = sorted(rrf_scores.keys(), key=lambda c: -rrf_scores[c])

    rerank_scores: dict[str, float] = {}
    if reranker is not None and rrf_sorted_ids:
        # Reranqueia os top-N do RRF (limita custo do cross-encoder)
        candidates = rrf_sorted_ids[:RERANK_INPUT_TOP_N]
        # Filtra candidatos sem texto no payload (BM25-only quando dense não trouxe)
        pairs = [(query, payload_map.get(cid, {}).get("text", "")) for cid in candidates]
        scores = reranker.compute_score(pairs, normalize=True)
        # compute_score pode retornar float (1 par) ou list[float] (N pares)
        if isinstance(scores, (int, float)):
            scores = [float(scores)]
        rerank_scores = {cid: float(s) for cid, s in zip(candidates, scores)}
        # Reordena por score do reranker (desc); empate decidido pelo RRF
        final_ids = sorted(
            candidates,
            key=lambda c: (-rerank_scores[c], -rrf_scores[c]),
        )[:top_k]
    else:
        final_ids = rrf_sorted_ids[:top_k]

    hits = []
    for cid in final_ids:
        rrf_s = rrf_scores[cid]
        rerank_s = rerank_scores.get(cid)
        hits.append(Hit(
            chunk_id=cid,
            score=rerank_s if rerank_s is not None else rrf_s,
            score_rrf=rrf_s,
            score_rerank=rerank_s,
            rank_dense=dense_rank.get(cid),
            rank_bm25=bm25_rank.get(cid),
            payload=payload_map.get(cid, {"chunk_id": cid}),
        ))
    return hits


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────


def _format_hits_human(hits: list[Hit], query: str, elapsed_ms: float) -> str:
    lines = [f"\n{len(hits)} resultados em {elapsed_ms:.0f}ms para query: {query!r}\n"]
    for i, h in enumerate(hits, 1):
        p = h.payload
        text = (p.get("text") or "").replace("\n", " ").strip()[:200]
        sect = p.get("section_label") or p.get("section_type") or "-"
        rd = f"#{h.rank_dense}" if h.rank_dense else "-"
        rb = f"#{h.rank_bm25}" if h.rank_bm25 else "-"
        score_label = (
            f"rerank={h.score_rerank:.4f}  rrf={h.score_rrf:.4f}"
            if h.score_rerank is not None
            else f"rrf={h.score_rrf:.4f}"
        )
        lines.append(
            f"{i}. [{score_label}  dense={rd}  bm25={rb}]  "
            f"{(p.get('tipo_ato') or '?').upper()} {p.get('year', '?')}  "
            f"tier={p.get('tier', '?')}"
        )
        lines.append(f"   {sect}  |  doc={p.get('doc_id', '?')}")
        lines.append(f"   url: {p.get('url', '-')}")
        lines.append(f"   {text}")
        lines.append("")
    return "\n".join(lines)


def _format_hits_json(hits: list[Hit]) -> str:
    out = []
    for h in hits:
        d = asdict(h)
        # payload já tem text — se for muito grande e não for desejável no JSON,
        # usuário pode pós-processar. Por default mantemos completo.
        out.append(d)
    return json.dumps(out, ensure_ascii=False, indent=2)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--query", required=True, help="Pergunta do usuário")
    p.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    p.add_argument("--qdrant-url", default=QDRANT_DEFAULT_URL)
    p.add_argument("--bm25-path", type=Path, default=BM25_DEFAULT_PATH)
    p.add_argument("--device", default="auto", choices=("auto", "cpu", "cuda", "mps"))
    p.add_argument("--tipo-ato", help="Filtra por tipo (ren, reh, dsp, ...)")
    p.add_argument("--year", type=int, help="Filtra por ano")
    p.add_argument("--tier", choices=("A", "B", "C"), help="Filtra por tier do chunk")
    p.add_argument("--no-parallel", action="store_true", help="Roda dense e BM25 sequenciais")
    p.add_argument("--no-rerank", action="store_true",
                   help="Pula o reranker cross-encoder; ranqueia só por RRF")
    p.add_argument("--json", action="store_true", help="Saída JSON em vez de tabela humana")
    p.add_argument("--log-level", default="WARNING")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    bm25 = load_bm25(args.bm25_path)
    embedder = load_embedder(args.device)
    reranker = None if args.no_rerank else load_reranker(args.device)
    client = make_qdrant_client(args.qdrant_url)

    filters: dict[str, Any] = {}
    if args.tipo_ato:
        filters["tipo_ato"] = args.tipo_ato.lower()
    if args.year:
        filters["year"] = args.year
    if args.tier:
        filters["tier"] = args.tier

    t0 = time.time()
    hits = retrieve(
        args.query, bm25, client, embedder,
        reranker=reranker,
        top_k=args.top_k,
        filters=filters or None,
        parallel=not args.no_parallel,
    )
    elapsed_ms = (time.time() - t0) * 1000

    if args.json:
        print(_format_hits_json(hits))
    else:
        print(_format_hits_human(hits, args.query, elapsed_ms))

    return 0


if __name__ == "__main__":
    sys.exit(main())
