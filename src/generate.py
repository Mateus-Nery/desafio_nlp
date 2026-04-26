"""Geração ANEEL — hits de retrieve.py → resposta com citações (Claude Sonnet 4.6).

Fase 6 do RAG:

  query + top-K Hits (de retrieve.py)
    │
    ├─ build_context_block()   monta bloco numerado [1]…[K] com metadados
    │
    ├─ system prompt (cache_control="ephemeral") → prompt caching
    │   instrui: PT-BR, citações obrigatórias [N], resposta só pelos trechos
    │
    └─ Claude Sonnet 4.6 → resposta com [N] inline + fontes no GenerationResult

Saída (GenerationResult):
  answer      — texto gerado com marcadores [N] inline
  citations   — lista ordenada {n, chunk_id, url, tipo_ato, title, section}
                (só os chunks efetivamente citados na resposta)
  query       — query original
  n_chunks    — quantos chunks no contexto
  model       — modelo utilizado
  input_tokens, output_tokens, cache_read_tokens — métricas de uso
  latency_ms  — tempo total

Uso (CLI):
  python -m src.generate --query "o que é TUSD?"
  python -m src.generate --query "..." --top-k 8 --no-rerank --device cpu
  python -m src.generate --query "..." --tipo-ato ren --year 2022 --json
  python -m src.generate --query "..." --no-stream   # saída toda de uma vez
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger("generate")

# ──────────────────────────────────────────────────────────────────────────────
# Configuração
# ──────────────────────────────────────────────────────────────────────────────

CLAUDE_MODEL = "claude-sonnet-4-6"
MAX_TOKENS_OUTPUT = 1024
DEFAULT_TOP_K = 10

# Resposta padrão quando o LLM não encontra nos trechos
NOT_FOUND_SIGNAL = "Não encontrei informação suficiente"

# ──────────────────────────────────────────────────────────────────────────────
# System prompt (imutável → cache_control ephemeral)
# ──────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
Você é um assistente especializado em regulação do setor elétrico brasileiro, \
com acesso a documentos oficiais da ANEEL (Agência Nacional de Energia Elétrica).

## Regras de resposta

1. **Baseie-se EXCLUSIVAMENTE nos trechos fornecidos** pelo usuário. \
Não use conhecimento externo nem complemente com informações que não estejam \
nos trechos.

2. **Cite obrigatoriamente** cada afirmação com o número do trecho entre \
colchetes, por exemplo: [1], [2] ou [1][3]. Coloque a citação imediatamente \
após a afirmação que ela sustenta.

3. **Seja preciso e direto.** Quando a resposta envolver valores, prazos, \
percentuais ou artigos específicos, transcreva-os fielmente dos trechos.

4. **Caso a pergunta não possa ser respondida** pelos trechos fornecidos — \
porque a informação não está presente ou os trechos são irrelevantes —, \
responda exatamente: \
"Não encontrei informação suficiente nos documentos para responder esta pergunta."

5. **Responda sempre em português do Brasil.**

6. Não mencione "trechos", "contexto" ou "documentos fornecidos" na resposta — \
escreva como se fosse uma resposta direta e autoritativa, com as citações [N] \
como único sinal da fundamentação.
"""


# ──────────────────────────────────────────────────────────────────────────────
# Dataclasses de saída
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Citation:
    n: int                   # número inline usado na resposta
    chunk_id: str
    url: str
    tipo_ato: str
    title: str
    section: str             # section_label ou section_type


@dataclass
class GenerationResult:
    answer: str
    citations: list[Citation]
    query: str
    n_chunks: int
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    latency_ms: int
    not_found: bool          # True se o LLM emitiu o sinal NOT_FOUND_SIGNAL

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, ensure_ascii=False, indent=2)

    def pretty(self) -> str:
        """Formatação legível para saída no terminal."""
        lines: list[str] = []
        lines.append("\n" + "═" * 72)
        lines.append(f"RESPOSTA  ({self.model}  {self.latency_ms} ms)")
        lines.append("═" * 72)
        lines.append(self.answer)
        if self.citations:
            lines.append("\n" + "─" * 72)
            lines.append("FONTES")
            lines.append("─" * 72)
            for c in self.citations:
                sec = f" — {c.section}" if c.section else ""
                title = c.title or c.tipo_ato.upper()
                lines.append(f"[{c.n}] {title}{sec}")
                lines.append(f"    {c.url}")
        lines.append("─" * 72)
        lines.append(
            f"Tokens: {self.input_tokens} entrada "
            f"({self.cache_read_tokens} cache) · {self.output_tokens} saída"
        )
        lines.append("═" * 72 + "\n")
        return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Construção do bloco de contexto
# ──────────────────────────────────────────────────────────────────────────────

def _label_for_hit(payload: dict[str, Any]) -> str:
    """Linha de cabeçalho de cada chunk no contexto."""
    tipo = (payload.get("tipo_ato") or "").upper()
    title = payload.get("title") or ""
    section = payload.get("section_label") or payload.get("section_type") or ""
    year = payload.get("year") or ""
    parts = [p for p in [tipo, str(year) if year else "", title, section] if p]
    return " | ".join(parts)


def build_context_block(hits: list[Any]) -> tuple[str, list[dict[str, Any]]]:
    """Retorna (bloco de texto formatado, lista de metadados por índice 1-based)."""
    meta: list[dict[str, Any]] = []
    parts: list[str] = []
    for i, hit in enumerate(hits, 1):
        payload = hit.payload if hasattr(hit, "payload") else hit
        text = payload.get("text", "").strip()
        if not text:
            continue
        label = _label_for_hit(payload)
        url = payload.get("url", "")
        parts.append(
            f"[{i}] {label}\n"
            f"URL: {url}\n"
            f"{'─' * 60}\n"
            f"{text}"
        )
        meta.append({
            "n": i,
            "chunk_id": payload.get("chunk_id", ""),
            "url": url,
            "tipo_ato": payload.get("tipo_ato", ""),
            "title": payload.get("title", ""),
            "section": payload.get("section_label") or payload.get("section_type") or "",
        })
    context = "\n\n".join(parts)
    return context, meta


# ──────────────────────────────────────────────────────────────────────────────
# Extração de citações do texto gerado
# ──────────────────────────────────────────────────────────────────────────────

def extract_cited_indices(answer: str) -> list[int]:
    """Retorna lista ordenada e deduplicada dos índices [N] presentes na resposta."""
    found = re.findall(r"\[(\d+)\]", answer)
    seen: set[int] = set()
    ordered: list[int] = []
    for s in found:
        n = int(s)
        if n not in seen:
            seen.add(n)
            ordered.append(n)
    return ordered


# ──────────────────────────────────────────────────────────────────────────────
# Função principal de geração
# ──────────────────────────────────────────────────────────────────────────────

def generate(
    query: str,
    hits: list[Any],
    client: Any,                       # anthropic.Anthropic
    top_k: int | None = None,
    stream: bool = True,
) -> GenerationResult:
    """Gera resposta fundamentada em `hits` para `query`.

    Parameters
    ----------
    query   : pergunta do usuário
    hits    : lista de Hit (de retrieve.py) ou de dicts com chave 'payload'
    client  : instância de `anthropic.Anthropic`
    top_k   : se fornecido, trunca hits para os primeiros top_k
    stream  : True → imprime resposta token a token no stdout (CLI interativo)

    Returns
    -------
    GenerationResult com answer, citations, tokens e latência.
    """
    if top_k is not None:
        hits = hits[:top_k]

    context_block, meta = build_context_block(hits)
    n_chunks = len(meta)

    if n_chunks == 0:
        return GenerationResult(
            answer=NOT_FOUND_SIGNAL + " nos documentos para responder esta pergunta.",
            citations=[],
            query=query,
            n_chunks=0,
            model=CLAUDE_MODEL,
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            latency_ms=0,
            not_found=True,
        )

    user_content = (
        f"Trechos relevantes da legislação ANEEL:\n\n"
        f"{context_block}\n\n"
        f"{'═' * 60}\n\n"
        f"Pergunta: {query}"
    )

    t0 = time.monotonic()
    answer_parts: list[str] = []
    input_tok = output_tok = cache_read_tok = 0

    if stream:
        with client.messages.stream(
            model=CLAUDE_MODEL,
            max_tokens=MAX_TOKENS_OUTPUT,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_content}],
        ) as s:
            for text in s.text_stream:
                print(text, end="", flush=True)
                answer_parts.append(text)
            print()  # newline final
            msg = s.get_final_message()
            usage = msg.usage
            input_tok = usage.input_tokens
            output_tok = usage.output_tokens
            cache_read_tok = getattr(usage, "cache_read_input_tokens", 0) or 0
    else:
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=MAX_TOKENS_OUTPUT,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_content}],
        )
        answer_parts = [block.text for block in msg.content if hasattr(block, "text")]
        usage = msg.usage
        input_tok = usage.input_tokens
        output_tok = usage.output_tokens
        cache_read_tok = getattr(usage, "cache_read_input_tokens", 0) or 0

    latency_ms = int((time.monotonic() - t0) * 1000)
    answer = "".join(answer_parts).strip()

    # Extrai quais índices o LLM citou e monta lista de Citation
    cited_ns = extract_cited_indices(answer)
    meta_by_n = {m["n"]: m for m in meta}
    citations = [
        Citation(**meta_by_n[n])
        for n in cited_ns
        if n in meta_by_n
    ]

    not_found = NOT_FOUND_SIGNAL.lower() in answer.lower()

    return GenerationResult(
        answer=answer,
        citations=citations,
        query=query,
        n_chunks=n_chunks,
        model=CLAUDE_MODEL,
        input_tokens=input_tok,
        output_tokens=output_tok,
        cache_read_tokens=cache_read_tok,
        latency_ms=latency_ms,
        not_found=not_found,
    )


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Fase 6: query → retrieve → generate com Claude Sonnet 4.6"
    )
    ap.add_argument("--query", "-q", required=True, help="Pergunta em linguagem natural")
    ap.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help="Chunks no contexto")
    ap.add_argument("--tipo-ato", help="Filtro por tipo (ren, reh, dsp…)")
    ap.add_argument("--year", type=int, help="Filtro por ano")
    ap.add_argument("--no-rerank", action="store_true", help="Desativa reranker")
    ap.add_argument("--no-stream", action="store_true", help="Saída em lote (sem streaming)")
    ap.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda", "mps"])
    ap.add_argument("--json", action="store_true", help="Saída em JSON")
    ap.add_argument(
        "--bm25-path", type=Path, default=Path("artifacts/bm25_index.pkl")
    )
    ap.add_argument("--qdrant-url", default=os.getenv("QDRANT_URL", "http://localhost:6333"))
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )

    # Lazy imports pesados
    import anthropic
    from qdrant_client import QdrantClient
    from src.retrieve import load_bm25, load_embedder, load_reranker, retrieve

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error(
            "ANTHROPIC_API_KEY não definida. "
            "Copie .env.example para .env e preencha a chave."
        )
        return 1

    logger.info("Carregando BM25…")
    bm25_data = load_bm25(args.bm25_path)

    logger.info("Carregando embedder (bge-m3)…")
    embedder = load_embedder(device=args.device)

    reranker = None
    if not args.no_rerank:
        logger.info("Carregando reranker (bge-reranker-v2-m3)…")
        reranker = load_reranker(device=args.device)

    qdrant = QdrantClient(url=args.qdrant_url)
    client = anthropic.Anthropic(api_key=api_key)

    filters: dict | None = {}
    if args.tipo_ato:
        filters["tipo_ato"] = args.tipo_ato
    if args.year:
        filters["year"] = args.year
    if not filters:
        filters = None

    logger.info("Retrieving top-%d…", args.top_k)
    hits = retrieve(
        query=args.query,
        bm25_data=bm25_data,
        qdrant_client=qdrant,
        embedder=embedder,
        reranker=reranker,
        top_k=args.top_k,
        filters=filters,
    )
    logger.info("Retrieved %d hits. Gerando resposta…", len(hits))

    stream = not args.no_stream and not args.json
    result = generate(
        query=args.query,
        hits=hits,
        client=client,
        stream=stream,
    )

    if args.json:
        print(result.to_json())
    else:
        if not stream:
            print(result.pretty())
        else:
            # Streaming já imprimiu o texto; imprime só as fontes
            if result.citations:
                print("\n" + "─" * 72)
                print("FONTES")
                print("─" * 72)
                for c in result.citations:
                    sec = f" — {c.section}" if c.section else ""
                    title = c.title or c.tipo_ato.upper()
                    print(f"[{c.n}] {title}{sec}")
                    print(f"    {c.url}")
            print("─" * 72)
            print(
                f"Tokens: {result.input_tokens} entrada "
                f"({result.cache_read_tokens} cache) · {result.output_tokens} saída "
                f"· {result.latency_ms} ms"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
