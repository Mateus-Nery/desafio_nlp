"""
eval/generate_golden_set.py — Geração do golden set para avaliação RAG (Fase 7)

Lê artifacts/chunks.jsonl, amostra chunks de forma estratificada por
tipo_query (factual / conceptual / comparative / multi_hop / negative),
chama Claude Sonnet 4.6 via API para gerar Q&A, salva em eval/golden_set.jsonl.

Uso:
    python -m eval.generate_golden_set [--limit N] [--out eval/golden_set.jsonl] [--no-cache]

Outputs:
    eval/golden_set.jsonl  — ground truth para Ragas + métricas hit@k / MRR
    eval/golden_set_raw.jsonl  — saída bruta incluindo chunk_text_ref (para revisão humana)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Any

import anthropic
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
CHUNKS_PATH = REPO_ROOT / "artifacts" / "chunks.jsonl"
DEFAULT_OUT = REPO_ROOT / "eval" / "golden_set.jsonl"
DEFAULT_RAW = REPO_ROOT / "eval" / "golden_set_raw.jsonl"

# Tipos de ato que mais interessam para avaliação regulatória
PRIORITY_TIPOS = {"ren", "reh", "prt", "nreh", "ndsp", "dsp"}

# Distribuição alvo (~80 questões)
TARGET = {
    "factual":     30,
    "conceptual":  15,
    "comparative": 10,
    "multi_hop":   15,
    "negative":    10,
}

# Sobreamostragem para compensar falhas/baixa qualidade
OVERSAMPLE = 2.0

# Modelo — mesmo que o LLM gerador da Fase 6
MODEL = "claude-sonnet-4-6"

# Janela de text enviada ao LLM (chars) — evita tokens caros em chunks grandes
MAX_TEXT_CHARS = 2000

# Seed para reprodutibilidade
RANDOM_SEED = 42


# ---------------------------------------------------------------------------
# Carrega .env manualmente (sem dep extra)
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
        # Sobrescreve se a variável não existe OU está vazia no ambiente
        if key and not os.environ.get(key):
            os.environ[key] = val


# ---------------------------------------------------------------------------
# Carrega chunks
# ---------------------------------------------------------------------------

def load_chunks(path: Path) -> list[dict]:
    log.info("Carregando chunks de %s …", path)
    chunks = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            chunks.append(json.loads(line))
    log.info("  %d chunks carregados", len(chunks))
    return chunks


# ---------------------------------------------------------------------------
# Amostragem estratificada
# ---------------------------------------------------------------------------

def _is_good_chunk(c: dict, min_chars: int = 300) -> bool:
    """Filtra chunks muito curtos ou sem texto útil."""
    return (
        len(c.get("text", "")) >= min_chars
        and c.get("tipo_ato", "") in PRIORITY_TIPOS
    )


def sample_single(chunks: list[dict], n: int) -> list[dict]:
    """Amostra n chunks com boa representação de tipo_ato e year."""
    pool = [c for c in chunks if _is_good_chunk(c)]
    # Estratifica por (tipo_ato, year)
    by_key: dict[tuple, list[dict]] = {}
    for c in pool:
        key = (c.get("tipo_ato", "?"), str(c.get("year", "?")))
        by_key.setdefault(key, []).append(c)

    selected: list[dict] = []
    keys = list(by_key.keys())
    random.shuffle(keys)
    # Round-robin pelas células até atingir n
    while len(selected) < n and keys:
        for key in list(keys):
            bucket = by_key[key]
            if bucket:
                selected.append(bucket.pop(random.randrange(len(bucket))))
            else:
                keys.remove(key)
            if len(selected) >= n:
                break
    return selected[:n]


def sample_pairs(chunks: list[dict], n: int, same_tipo: bool) -> list[tuple[dict, dict]]:
    """
    Amostra n pares de chunks.
    same_tipo=True  → comparative (mesmo tipo_ato, anos distintos)
    same_tipo=False → multi_hop   (tipos_ato distintos, docs distintos)
    """
    pool = [c for c in chunks if _is_good_chunk(c)]

    # Índice por tipo_ato
    by_tipo: dict[str, list[dict]] = {}
    for c in pool:
        t = c.get("tipo_ato", "?")
        by_tipo.setdefault(t, []).append(c)

    pairs: list[tuple[dict, dict]] = []
    attempts = 0
    tipos = list(by_tipo.keys())

    while len(pairs) < n and attempts < n * 50:
        attempts += 1
        if same_tipo:
            tipo = random.choice(tipos)
            bucket = by_tipo.get(tipo, [])
            if len(bucket) < 2:
                continue
            a, b = random.sample(bucket, 2)
            # Prefere anos diferentes
            if a.get("year") == b.get("year") and attempts < n * 10:
                continue
        else:
            t1, t2 = random.sample(tipos, 2)
            if not by_tipo[t1] or not by_tipo[t2]:
                continue
            a = random.choice(by_tipo[t1])
            b = random.choice(by_tipo[t2])

        # Garante docs distintos
        if a.get("doc_id") == b.get("doc_id"):
            continue

        pairs.append((a, b))

    return pairs[:n]


def sample_negative(chunks: list[dict], n: int) -> list[dict]:
    """
    Prefer Tier C (docs curtos) para perguntas negativas —
    menos contexto = mais fácil confirmar que algo não está lá.
    """
    tier_c = [c for c in chunks if c.get("tier") == "C" and _is_good_chunk(c, min_chars=200)]
    if len(tier_c) >= n:
        return random.sample(tier_c, n)
    # Complementa com Tier B curto
    tier_b = [c for c in chunks if c.get("tier") == "B" and _is_good_chunk(c, min_chars=300)]
    combined = tier_c + tier_b
    return random.sample(combined, min(n, len(combined)))


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def _meta(c: dict) -> str:
    parts = [
        f"tipo_ato={c.get('tipo_ato','?').upper()}",
        f"ano={c.get('year','?')}",
    ]
    if c.get("title"):
        parts.append(f"título={c['title'][:80]}")
    if c.get("section_label"):
        parts.append(f"seção={c['section_label']}")
    return ", ".join(parts)


def _text(c: dict) -> str:
    return c.get("text", "")[:MAX_TEXT_CHARS]


SYSTEM_PROMPT = (
    "Você é especialista em regulação do setor elétrico brasileiro. "
    "Gera perguntas e respostas no estilo de um examinador rigoroso, "
    "sem inventar informações além do que está no texto fornecido. "
    "Responda SEMPRE com JSON válido puro, sem markdown, sem ```json."
)


def prompt_factual(c: dict) -> str:
    return f"""Trecho de legislação ANEEL ({_meta(c)}):

{_text(c)}

Crie UMA pergunta factual objetiva cuja resposta seja explicitamente extraível do trecho acima.
Prefira perguntas sobre: prazo, valor, percentual, obrigação específica, número de artigo/parágrafo.
A resposta deve ter 1-2 frases diretas, citando o dado do texto.

JSON esperado:
{{"pergunta": "...", "resposta_esperada": "..."}}"""


def prompt_conceptual(c: dict) -> str:
    return f"""Trecho de legislação ANEEL ({_meta(c)}):

{_text(c)}

Crie UMA pergunta conceitual do tipo "O que é X?", "Como funciona Y?" ou "Quais são os requisitos para Z?".
A pergunta deve ser respondível com 2-3 frases explicativas baseadas APENAS no trecho.

JSON esperado:
{{"pergunta": "...", "resposta_esperada": "..."}}"""


def prompt_comparative(a: dict, b: dict) -> str:
    return f"""Dois trechos de legislação ANEEL:

TRECHO A ({_meta(a)}):
{_text(a)}

TRECHO B ({_meta(b)}):
{_text(b)}

Crie UMA pergunta que exija informações dos DOIS trechos para ser respondida completamente.
A resposta deve integrar dados de A e B.

JSON esperado (apenas pergunta e resposta):
{{"pergunta": "...", "resposta_esperada": "..."}}"""


def prompt_multi_hop(a: dict, b: dict) -> str:
    return f"""Dois trechos de legislação ANEEL de documentos diferentes:

TRECHO A ({_meta(a)}):
{_text(a)}

TRECHO B ({_meta(b)}):
{_text(b)}

Crie UMA pergunta que só pode ser respondida sintetizando informações dos DOIS trechos.
Ambos devem ser necessários — não use perguntas que um trecho só já responderia.

JSON esperado (apenas pergunta e resposta):
{{"pergunta": "...", "resposta_esperada": "..."}}"""


def prompt_negative(c: dict) -> str:
    return f"""Trecho de legislação ANEEL ({_meta(c)}):

{_text(c)}

Crie UMA pergunta sobre um detalhe que um leitor esperaria encontrar neste documento
mas que NÃO está presente neste trecho específico.
A resposta deve deixar claro que a informação não consta neste documento.

JSON esperado:
{{"pergunta": "...", "resposta_esperada": "..."}}"""


# ---------------------------------------------------------------------------
# Chamada à API
# ---------------------------------------------------------------------------

def call_claude(client: anthropic.Anthropic, user_prompt: str, retries: int = 3) -> str | None:
    for attempt in range(retries):
        try:
            msg = client.messages.create(
                model=MODEL,
                max_tokens=512,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return msg.content[0].text
        except anthropic.RateLimitError:
            wait = 2 ** attempt * 5
            log.warning("Rate limit — aguardando %ds …", wait)
            time.sleep(wait)
        except anthropic.APIError as e:
            log.warning("API error (attempt %d/%d): %s", attempt + 1, retries, e)
            time.sleep(2)
    return None


def parse_json_response(raw: str) -> dict | None:
    """Extrai JSON da resposta, tolerando markdown residual."""
    # Remove blocos ```json ... ```
    raw = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Tenta extrair o primeiro objeto JSON
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    return None


# ---------------------------------------------------------------------------
# Pipeline principal
# ---------------------------------------------------------------------------

def generate(
    chunks: list[dict],
    client: anthropic.Anthropic,
    target: dict[str, int],
    oversample: float,
) -> list[dict]:
    rng = random.Random(RANDOM_SEED)
    random.seed(RANDOM_SEED)

    results: list[dict] = []
    gs_id = 0

    def make_record(tipo: str, pergunta: str, resposta: str,
                    chunk_ids: list[str], chunk_refs: list[dict],
                    tipo_ato: str, year: int | None) -> dict:
        nonlocal gs_id
        gs_id += 1
        return {
            "id": f"gs_{gs_id:03d}",
            "pergunta": pergunta,
            "tipo_query": tipo,
            "resposta_esperada": resposta,
            "docs_relevantes": chunk_ids,
            "tipo_ato_filtro": tipo_ato.upper() if tipo_ato else None,
            "year_filtro": year,
            # Para revisão humana — removido no eval final
            "_chunk_text_ref": [
                {"chunk_id": r["chunk_id"], "text": r.get("text", "")[:400]}
                for r in chunk_refs
            ],
        }

    # ── 1. Factual ──────────────────────────────────────────────────────────
    n_factual = int(target["factual"] * oversample)
    log.info("Gerando %d questões factual (alvo=%d) …", n_factual, target["factual"])
    pool = sample_single(chunks, n_factual)
    for c in tqdm(pool, desc="factual"):
        raw = call_claude(client, prompt_factual(c))
        if not raw:
            continue
        parsed = parse_json_response(raw)
        if not parsed or not parsed.get("pergunta") or not parsed.get("resposta_esperada"):
            log.debug("factual: resposta inválida para %s", c["chunk_id"])
            continue
        results.append(make_record(
            tipo="factual",
            pergunta=parsed["pergunta"],
            resposta=parsed["resposta_esperada"],
            chunk_ids=[c["chunk_id"]],
            chunk_refs=[c],
            tipo_ato=c.get("tipo_ato", ""),
            year=c.get("year"),
        ))

    # ── 2. Conceptual ───────────────────────────────────────────────────────
    n_conceptual = int(target["conceptual"] * oversample)
    log.info("Gerando %d questões conceptual (alvo=%d) …", n_conceptual, target["conceptual"])
    pool = sample_single(chunks, n_conceptual)
    for c in tqdm(pool, desc="conceptual"):
        raw = call_claude(client, prompt_conceptual(c))
        if not raw:
            continue
        parsed = parse_json_response(raw)
        if not parsed or not parsed.get("pergunta") or not parsed.get("resposta_esperada"):
            continue
        results.append(make_record(
            tipo="conceptual",
            pergunta=parsed["pergunta"],
            resposta=parsed["resposta_esperada"],
            chunk_ids=[c["chunk_id"]],
            chunk_refs=[c],
            tipo_ato=c.get("tipo_ato", ""),
            year=c.get("year"),
        ))

    # ── 3. Comparative ──────────────────────────────────────────────────────
    n_comparative = int(target["comparative"] * oversample)
    log.info("Gerando %d questões comparative (alvo=%d) …", n_comparative, target["comparative"])
    pairs = sample_pairs(chunks, n_comparative, same_tipo=True)
    for a, b in tqdm(pairs, desc="comparative"):
        raw = call_claude(client, prompt_comparative(a, b))
        if not raw:
            continue
        parsed = parse_json_response(raw)
        if not parsed or not parsed.get("pergunta") or not parsed.get("resposta_esperada"):
            continue
        results.append(make_record(
            tipo="comparative",
            pergunta=parsed["pergunta"],
            resposta=parsed["resposta_esperada"],
            chunk_ids=[a["chunk_id"], b["chunk_id"]],
            chunk_refs=[a, b],
            tipo_ato=a.get("tipo_ato", ""),
            year=None,
        ))

    # ── 4. Multi-hop ────────────────────────────────────────────────────────
    n_multi = int(target["multi_hop"] * oversample)
    log.info("Gerando %d questões multi_hop (alvo=%d) …", n_multi, target["multi_hop"])
    pairs = sample_pairs(chunks, n_multi, same_tipo=False)
    for a, b in tqdm(pairs, desc="multi_hop"):
        raw = call_claude(client, prompt_multi_hop(a, b))
        if not raw:
            continue
        parsed = parse_json_response(raw)
        if not parsed or not parsed.get("pergunta") or not parsed.get("resposta_esperada"):
            continue
        results.append(make_record(
            tipo="multi_hop",
            pergunta=parsed["pergunta"],
            resposta=parsed["resposta_esperada"],
            chunk_ids=[a["chunk_id"], b["chunk_id"]],
            chunk_refs=[a, b],
            tipo_ato="",
            year=None,
        ))

    # ── 5. Negative ─────────────────────────────────────────────────────────
    n_negative = int(target["negative"] * oversample)
    log.info("Gerando %d questões negative (alvo=%d) …", n_negative, target["negative"])
    pool = sample_negative(chunks, n_negative)
    for c in tqdm(pool, desc="negative"):
        raw = call_claude(client, prompt_negative(c))
        if not raw:
            continue
        parsed = parse_json_response(raw)
        if not parsed or not parsed.get("pergunta") or not parsed.get("resposta_esperada"):
            continue
        results.append(make_record(
            tipo="negative",
            pergunta=parsed["pergunta"],
            resposta=parsed["resposta_esperada"],
            chunk_ids=[c["chunk_id"]],
            chunk_refs=[c],
            tipo_ato=c.get("tipo_ato", ""),
            year=c.get("year"),
        ))

    return results


def trim_to_target(results: list[dict], target: dict[str, int]) -> list[dict]:
    """Limita cada tipo ao alvo, priorizando os primeiros gerados."""
    counts: dict[str, int] = {t: 0 for t in target}
    trimmed = []
    for r in results:
        t = r["tipo_query"]
        if counts.get(t, 0) < target.get(t, 0):
            trimmed.append(r)
            counts[t] += 1
    return trimmed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    _load_dotenv()

    parser = argparse.ArgumentParser(description="Gera golden set para avaliação RAG (Fase 7)")
    parser.add_argument("--chunks", default=str(CHUNKS_PATH), help="Caminho para chunks.jsonl")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="Saída: golden_set.jsonl")
    parser.add_argument("--raw-out", default=str(DEFAULT_RAW), help="Saída bruta com chunk_text_ref")
    parser.add_argument("--limit", type=int, default=None,
                        help="Limita N questões por tipo (debug)")
    parser.add_argument("--oversample", type=float, default=OVERSAMPLE,
                        help="Fator de sobreamostragem (default 2.0)")
    parser.add_argument("--seed", type=int, default=RANDOM_SEED)
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.error("ANTHROPIC_API_KEY não encontrada. Configure no .env ou como variável de ambiente.")
        sys.exit(1)

    random.seed(args.seed)
    client = anthropic.Anthropic(api_key=api_key)

    chunks = load_chunks(Path(args.chunks))

    target = TARGET.copy()
    if args.limit:
        target = {k: min(v, args.limit) for k, v in target.items()}

    log.info("Alvo: %s = %d questões", target, sum(target.values()))
    log.info("Modelo: %s", MODEL)

    results = generate(chunks, client, target, args.oversample)
    results = trim_to_target(results, target)

    # Stats
    from collections import Counter
    stats = Counter(r["tipo_query"] for r in results)
    log.info("Geradas: %s — total=%d", dict(stats), len(results))

    # Salva raw (com _chunk_text_ref)
    raw_path = Path(args.raw_out)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    with raw_path.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    log.info("Raw salvo em %s", raw_path)

    # Salva golden set final (sem _chunk_text_ref)
    out_path = Path(args.out)
    with out_path.open("w", encoding="utf-8") as f:
        for r in results:
            clean = {k: v for k, v in r.items() if not k.startswith("_")}
            f.write(json.dumps(clean, ensure_ascii=False) + "\n")
    log.info("Golden set salvo em %s (%d questões)", out_path, len(results))


if __name__ == "__main__":
    main()
