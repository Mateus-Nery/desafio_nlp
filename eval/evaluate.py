"""
eval/evaluate.py — Avaliação end-to-end do pipeline RAG (Fase 7)

Métricas calculadas:
  Retrieval  (todos os tipos exceto 'negative'):
    hit@5, hit@10, hit@20 — ao menos 1 chunk relevante nos top-K recuperados
    MRR — 1 / rank do primeiro chunk relevante

  Geração (com --with-generation):
    not_found_rate_negative — % de questões 'negative' em que o LLM retornou NOT FOUND
    faithfulness   (Ragas ≥ 0.2)
    answer_relevance (Ragas ≥ 0.2)
    p50_latency_ms, p95_latency_ms

Uso:
  # apenas retrieval (rápido):
  python -m eval.evaluate

  # retrieval + geração + Ragas:
  python -m eval.evaluate --with-generation

  # geração limitada a 20 questões (para smoke rápido):
  python -m eval.evaluate --with-generation --gen-limit 20

  # opções avançadas:
  python -m eval.evaluate --with-generation --no-rerank --device cpu --top-k 10
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from tqdm import tqdm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_SET_PATH = REPO_ROOT / "eval" / "golden_set.jsonl"
RESULTS_PATH = REPO_ROOT / "eval" / "eval_results.jsonl"
SUMMARY_PATH = REPO_ROOT / "eval" / "eval_summary.json"

HIT_K_VALUES = [5, 10, 20]
DEFAULT_TOP_K = 20   # retrieve top-20 para cobrir hit@5/10/20 sem re-rodar
DEFAULT_GEN_TOP_K = 10  # top-K passados para o gerador


# ---------------------------------------------------------------------------
# .env loader
# ---------------------------------------------------------------------------

def _load_dotenv() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and not os.environ.get(key):
            os.environ[key] = val


# ---------------------------------------------------------------------------
# Métricas de retrieval
# ---------------------------------------------------------------------------

def hit_at_k(retrieved_ids: list[str], relevant_ids: set[str], k: int) -> int:
    """1 se algum chunk relevante está nos top-k recuperados, 0 caso contrário."""
    return int(any(rid in relevant_ids for rid in retrieved_ids[:k]))


def reciprocal_rank(retrieved_ids: list[str], relevant_ids: set[str]) -> float:
    """1/rank do primeiro chunk relevante, 0 se não encontrado."""
    for rank, rid in enumerate(retrieved_ids, 1):
        if rid in relevant_ids:
            return 1.0 / rank
    return 0.0


def compute_retrieval_metrics(records: list[dict]) -> dict:
    """
    Calcula hit@k e MRR apenas para questões não-negativas.
    Records devem ter 'tipo_query', 'docs_relevantes', 'retrieved_ids'.
    """
    non_neg = [r for r in records if r.get("tipo_query") != "negative"]
    if not non_neg:
        return {}

    hits = {k: [] for k in HIT_K_VALUES}
    rr_scores: list[float] = []

    for r in non_neg:
        relevant = set(r.get("docs_relevantes", []))
        retrieved = r.get("retrieved_ids", [])
        for k in HIT_K_VALUES:
            hits[k].append(hit_at_k(retrieved, relevant, k))
        rr_scores.append(reciprocal_rank(retrieved, relevant))

    n = len(non_neg)
    metrics: dict = {f"hit@{k}": round(sum(v) / n, 4) for k, v in hits.items()}
    metrics["mrr"] = round(sum(rr_scores) / n, 4)
    metrics["n_retrieval_eval"] = n
    return metrics


# ---------------------------------------------------------------------------
# Métricas de latência
# ---------------------------------------------------------------------------

def percentile(values: list[float], p: int) -> float:
    if not values:
        return 0.0
    sorted_v = sorted(values)
    idx = max(0, int(len(sorted_v) * p / 100) - 1)
    return round(sorted_v[idx], 1)


# ---------------------------------------------------------------------------
# Ragas
# ---------------------------------------------------------------------------

def run_llm_eval(samples: list[dict], anthropic_client: Any) -> dict:
    """
    Avalia faithfulness e answer_relevance via Claude (sem Ragas).

    Para cada amostra:
      - faithfulness:       Claude decide se a resposta usa APENAS informações dos contextos (0/1)
      - answer_relevance:   Claude decide se a resposta responde de fato à pergunta (0/1)

    Retorna médias das duas métricas.
    """
    import anthropic as _anthropic

    EVAL_SYSTEM = (
        "Você é um avaliador rigoroso de sistemas RAG. "
        "Responda APENAS com JSON puro, sem markdown."
    )

    def _eval_one(sample: dict) -> dict[str, int]:
        ctx = "\n---\n".join(sample["retrieved_contexts"][:5])
        prompt = (
            f"Pergunta: {sample['user_input']}\n\n"
            f"Contextos recuperados:\n{ctx[:3000]}\n\n"
            f"Resposta gerada:\n{sample['response']}\n\n"
            "Avalie:\n"
            "1. faithfulness (0 ou 1): a resposta usa APENAS informações dos contextos acima? "
            "Se inventou algo não presente nos contextos, marque 0.\n"
            "2. answer_relevance (0 ou 1): a resposta é relevante e responde à pergunta? "
            "Se desviou do tema ou respondeu outra coisa, marque 0.\n\n"
            'JSON: {"faithfulness": <0 ou 1>, "answer_relevance": <0 ou 1>}'
        )
        try:
            msg = anthropic_client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=64,
                system=EVAL_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text.strip()
            raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
            parsed = json.loads(raw)
            return {
                "faithfulness": int(bool(parsed.get("faithfulness", 0))),
                "answer_relevance": int(bool(parsed.get("answer_relevance", 0))),
            }
        except Exception as e:
            log.debug("LLM eval falhou para uma amostra: %s", e)
            return {"faithfulness": -1, "answer_relevance": -1}

    faith_scores, rel_scores = [], []
    for s in tqdm(samples, desc="llm-eval"):
        scores = _eval_one(s)
        if scores["faithfulness"] >= 0:
            faith_scores.append(scores["faithfulness"])
        if scores["answer_relevance"] >= 0:
            rel_scores.append(scores["answer_relevance"])

    result: dict = {"n_llm_eval": len(samples)}
    if faith_scores:
        result["faithfulness"] = round(sum(faith_scores) / len(faith_scores), 4)
    if rel_scores:
        result["answer_relevance"] = round(sum(rel_scores) / len(rel_scores), 4)
    return result


# ---------------------------------------------------------------------------
# Pipeline principal
# ---------------------------------------------------------------------------

def main() -> None:
    _load_dotenv()

    ap = argparse.ArgumentParser(description="Avaliação end-to-end do pipeline RAG (Fase 7)")
    ap.add_argument("--golden-set", default=str(GOLDEN_SET_PATH))
    ap.add_argument("--results-out", default=str(RESULTS_PATH))
    ap.add_argument("--summary-out", default=str(SUMMARY_PATH))
    ap.add_argument("--top-k", type=int, default=DEFAULT_TOP_K,
                    help="Chunks recuperados para métricas hit@k (default 20)")
    ap.add_argument("--gen-top-k", type=int, default=DEFAULT_GEN_TOP_K,
                    help="Chunks passados ao gerador (default 10)")
    ap.add_argument("--with-generation", action="store_true",
                    help="Executa geração + Ragas (mais lento, custa tokens)")
    ap.add_argument("--gen-limit", type=int, default=None,
                    help="Limita geração a N questões (smoke rápido)")
    ap.add_argument("--no-rerank", action="store_true")
    ap.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda", "mps"])
    ap.add_argument(
        "--bm25-path", type=Path,
        default=REPO_ROOT / "artifacts" / "bm25_index.pkl"
    )
    ap.add_argument("--qdrant-url",
                    default=os.getenv("QDRANT_URL", "http://localhost:6333"))
    args = ap.parse_args()

    # ── Carrega golden set ──────────────────────────────────────────────────
    golden: list[dict] = []
    with open(args.golden_set, encoding="utf-8") as f:
        for line in f:
            golden.append(json.loads(line))
    log.info("Golden set: %d questões", len(golden))

    # ── Carrega modelos de retrieval ────────────────────────────────────────
    from src.retrieve import load_bm25, load_embedder, load_reranker, retrieve
    from qdrant_client import QdrantClient

    log.info("Carregando BM25 (%s)…", args.bm25_path)
    bm25_data = load_bm25(args.bm25_path)

    log.info("Carregando embedder (bge-m3, device=%s)…", args.device)
    embedder = load_embedder(device=args.device)

    reranker = None
    if not args.no_rerank:
        log.info("Carregando reranker (bge-reranker-v2-m3)…")
        reranker = load_reranker(device=args.device)

    qdrant = QdrantClient(url=args.qdrant_url)

    # ── Geração (opcional) ──────────────────────────────────────────────────
    anthropic_client = None
    if args.with_generation:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            log.error("ANTHROPIC_API_KEY não encontrada. Configure no .env.")
            sys.exit(1)
        import anthropic as _anthropic
        anthropic_client = _anthropic.Anthropic(api_key=api_key)

    from src.generate import generate as rag_generate

    # ── Loop de avaliação ───────────────────────────────────────────────────
    results: list[dict] = []
    latencies_ms: list[float] = []
    neg_not_found: list[int] = []
    ragas_samples: list[dict] = []

    gen_count = 0
    gen_limit = args.gen_limit if args.gen_limit else len(golden)

    log.info("Iniciando avaliação de %d questões (retrieval top-%d)…",
             len(golden), args.top_k)

    for item in tqdm(golden, desc="avaliando"):
        query = item["pergunta"]
        tipo = item["tipo_query"]
        relevant_ids = item.get("docs_relevantes", [])

        # Filtros opcionais do golden set
        filters: dict | None = {}
        if item.get("tipo_ato_filtro"):
            filters["tipo_ato"] = item["tipo_ato_filtro"].lower()
        if item.get("year_filtro"):
            filters["year"] = item["year_filtro"]
        if not filters:
            filters = None

        t0 = time.monotonic()

        # Retrieval
        hits = retrieve(
            query=query,
            bm25_data=bm25_data,
            qdrant_client=qdrant,
            embedder=embedder,
            reranker=reranker,
            top_k=args.top_k,
            filters=filters,
        )

        retrieved_ids = [h.chunk_id for h in hits]
        retrieval_ms = int((time.monotonic() - t0) * 1000)

        record: dict = {
            "id": item["id"],
            "tipo_query": tipo,
            "pergunta": query,
            "docs_relevantes": relevant_ids,
            "retrieved_ids": retrieved_ids[:args.top_k],
            "retrieval_ms": retrieval_ms,
        }

        # Métricas de retrieval (só não-negativas)
        if tipo != "negative":
            rel_set = set(relevant_ids)
            for k in HIT_K_VALUES:
                record[f"hit@{k}"] = hit_at_k(retrieved_ids, rel_set, k)
            record["rr"] = reciprocal_rank(retrieved_ids, rel_set)

        # Geração (opcional)
        if args.with_generation and gen_count < gen_limit and anthropic_client:
            gen_t0 = time.monotonic()
            gen_result = rag_generate(
                query=query,
                hits=hits,
                client=anthropic_client,
                top_k=args.gen_top_k,
                stream=False,
            )
            total_ms = int((time.monotonic() - gen_t0) * 1000)
            latencies_ms.append(total_ms)
            gen_count += 1

            record["answer"] = gen_result.answer
            record["not_found"] = gen_result.not_found
            record["gen_latency_ms"] = total_ms
            record["input_tokens"] = gen_result.input_tokens
            record["output_tokens"] = gen_result.output_tokens
            record["cache_read_tokens"] = gen_result.cache_read_tokens
            record["n_cited"] = len(gen_result.citations)

            if tipo == "negative":
                neg_not_found.append(int(gen_result.not_found))

            # Amostra para Ragas (questões não-negativas com resposta real)
            if tipo != "negative" and not gen_result.not_found:
                ragas_samples.append({
                    "user_input": query,
                    "response": gen_result.answer,
                    "retrieved_contexts": [
                        h.payload.get("text", "") for h in hits[:args.gen_top_k]
                    ],
                    "reference": item.get("resposta_esperada", ""),
                })

        results.append(record)

    # ── Agrega métricas ─────────────────────────────────────────────────────
    retrieval_metrics = compute_retrieval_metrics(results)

    summary: dict = {
        "n_questions": len(golden),
        "n_evaluated": len(results),
        **retrieval_metrics,
    }

    if args.with_generation and latencies_ms:
        summary["n_generated"] = gen_count
        summary["p50_latency_ms"] = percentile(latencies_ms, 50)
        summary["p95_latency_ms"] = percentile(latencies_ms, 95)

        if neg_not_found:
            summary["not_found_rate_negative"] = round(
                sum(neg_not_found) / len(neg_not_found), 4
            )

        # LLM eval (faithfulness + answer_relevance via Claude)
        if ragas_samples and anthropic_client:
            log.info("Rodando LLM eval em %d amostras...", len(ragas_samples))
            llm_metrics = run_llm_eval(ragas_samples, anthropic_client)
            summary.update(llm_metrics)

    # ── Salva resultados ────────────────────────────────────────────────────
    out_path = Path(args.results_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    log.info("Resultados por questão: %s", out_path)

    summary_path = Path(args.summary_out)
    with summary_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    log.info("Sumário: %s", summary_path)

    # ── Imprime sumário no terminal ─────────────────────────────────────────
    sep = "=" * 60
    print("\n" + sep)
    print("AVALIACAO RAG - SUMARIO")
    print(sep)
    print(f"  Questoes avaliadas : {summary['n_evaluated']}")
    print()
    print("  Retrieval (questoes nao-negativas):")
    for k in HIT_K_VALUES:
        key = f"hit@{k}"
        if key in summary:
            print(f"    {key:8s} = {summary[key]:.4f}")
    if "mrr" in summary:
        print(f"    {'MRR':8s} = {summary['mrr']:.4f}")

    if args.with_generation and "n_generated" in summary:
        print()
        print(f"  Geracao ({summary['n_generated']} questoes):")
        if "p50_latency_ms" in summary:
            print(f"    p50 latencia = {summary['p50_latency_ms']:.0f} ms")
            print(f"    p95 latencia = {summary['p95_latency_ms']:.0f} ms")
        if "not_found_rate_negative" in summary:
            print(f"    NOT FOUND rate (negative) = {summary['not_found_rate_negative']:.2%}")
        if "faithfulness" in summary:
            print(f"    faithfulness      = {summary['faithfulness']:.4f}  (LLM eval)")
        if "answer_relevance" in summary:
            print(f"    answer_relevance  = {summary['answer_relevance']:.4f}  (LLM eval)")

    print(sep + "\n")


if __name__ == "__main__":
    main()
