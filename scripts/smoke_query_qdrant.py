"""Smoke test de retrieval contra a coleção aneel_chunks no Qdrant.

Carrega bge-m3, encoda 5 queries de dominio, faz busca dense, mostra top-3.
Roda standalone após restore do snapshot pra validar que tudo está funcional.

Uso: python scripts/smoke_query_qdrant.py
"""
from __future__ import annotations

import sys

QUERIES = [
    "tarifa de uso do sistema de distribuição TUSD",
    "prazo para ligação nova de unidade consumidora",
    "geração distribuída de energia solar fotovoltaica",
    "microgeração e minigeração distribuída",
    "penalidade por descumprimento contratual da concessionária",
]


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    from FlagEmbedding import BGEM3FlagModel
    from qdrant_client import QdrantClient
    from qdrant_client.http import models as qm
    import torch

    # Autodetect: CUDA → CPU. Pula MPS por default (bugs históricos do FlagReranker)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    use_fp16 = (device == "cuda")
    print(f"Carregando bge-m3 (device={device}, cache local)...", flush=True)
    model = BGEM3FlagModel("BAAI/bge-m3", use_fp16=use_fp16, device=device)

    client = QdrantClient(url="http://localhost:6333", timeout=60)

    info = client.get_collection("aneel_chunks")
    print(f"Coleção aneel_chunks: {info.points_count:,} pontos, status={info.status}\n")

    for q in QUERIES:
        out = model.encode([q], return_dense=True, return_sparse=False)
        dense = out["dense_vecs"][0].tolist()

        result = client.query_points(
            collection_name="aneel_chunks",
            query=dense,
            using="dense",
            limit=3,
            with_payload=True,
        )
        hits = result.points

        print(f">>> Query: {q!r}")
        for i, h in enumerate(hits, 1):
            p = h.payload or {}
            text = (p.get("text") or "").replace("\n", " ").strip()[:160]
            print(f"  {i}. [{h.score:.3f}] {p.get('tipo_ato', '?').upper()} {p.get('year', '?')}  doc={p.get('doc_id', '?')}  tier={p.get('tier', '?')}")
            print(f"     section: {p.get('section_label') or p.get('section_type') or '-'}  url={p.get('url', '-')}")
            print(f"     trecho: {text!r}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
