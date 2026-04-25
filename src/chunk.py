"""Chunker ANEEL — parsed.jsonl → chunks.jsonl

Estratégia 3-tier data-driven:

  Tier A  — doc tem `artigo` em structure → split por artigo
            (sub-split por parágrafo se artigo > MAX_ARTIGO_TOKENS).
            Anexos viram chunks próprios. Preâmbulo (texto antes do
            primeiro artigo) também vira chunk.

  Tier B  — sem artigo, mas doc é grande (> TIER_C_MAX_TOKENS) → split por
            parágrafo, merge greedy até TARGET_TOKENS, com overlap.

  Tier C  — sem artigo e curto (≤ TIER_C_MAX_TOKENS) → 1 chunk = 1 doc.

Entrada:  artifacts/parsed.jsonl   (1 linha por documento)
Saída:    artifacts/chunks.jsonl   (1 linha por chunk)

Uso:
  python -m src.chunk --in artifacts/parsed.jsonl \\
                      --out artifacts/chunks.jsonl
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable, Iterator

logger = logging.getLogger("chunk")

# ──────────────────────────────────────────────────────────────────────────────
# Parâmetros de chunking — alinhados com o README do projeto.
# Token estimado = chars / 4 (mesma heurística que parse_pdfs.n_tokens_est).
# ──────────────────────────────────────────────────────────────────────────────

CHARS_PER_TOKEN = 4

HARD_MAX_TOKENS = 1500            # nenhum chunk passa disso (margem confortável p/ bge-m3 8k)
TIER_C_MAX_TOKENS = HARD_MAX_TOKENS   # acima disso, doc sem artigo vira Tier B
TIER_B_TARGET_TOKENS = 500        # tamanho-alvo de chunk em prosa
TIER_B_OVERLAP_TOKENS = 50        # overlap entre chunks adjacentes em Tier B
TIER_A_MAX_ARTIGO_TOKENS = HARD_MAX_TOKENS   # acima disso, sub-split do artigo por §

MIN_CHUNK_CHARS = 60              # fragmentos menores são descartados
PREAMBULO_MIN_CHARS = 200         # preâmbulo só vira chunk se relevante

ANEEL_PDF_BASE = "https://www2.aneel.gov.br/cedoc/"

# Quebras de parágrafo: 2+ newlines, ou newline seguido de marcadores fortes
PARA_SPLIT_RE = re.compile(r"\n\s*\n+")


# ──────────────────────────────────────────────────────────────────────────────
# Dataclass do chunk de saída.
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    tipo_ato: str
    year: int
    tier: str               # 'A' | 'B' | 'C'
    section_type: str       # 'artigo' | 'paragrafo' | 'anexo' | 'preambulo' | 'prosa' | 'doc'
    section_label: str      # 'Art. 1º' / 'ANEXO I' / '' (Tier B/C)
    section_parent: str     # label do pai (ex: '§' herda do artigo)
    section_title: str      # título do anexo, quando houver
    title: str              # título do documento (vindo do parser)
    ementa: str
    filename: str
    url: str
    char_start: int
    char_end: int
    n_chars: int
    n_tokens_est: int
    text: str


def estimate_tokens(text: str) -> int:
    return max(1, round(len(text) / CHARS_PER_TOKEN))


def build_url(filename: str) -> str:
    return ANEEL_PDF_BASE + filename


# ──────────────────────────────────────────────────────────────────────────────
# Tier A — split por artigo (com sub-split em parágrafo se necessário)
# ──────────────────────────────────────────────────────────────────────────────


def _emit_or_split(
    doc: dict, suffix: str, section_type: str, section_label: str,
    section_parent: str, section_title: str,
    char_start: int, char_end: int, text: str, tier: str,
) -> Iterator[Chunk]:
    """Emite 1 chunk se cabe no HARD_MAX, senão divide em janelas via Tier B."""
    if estimate_tokens(text) <= HARD_MAX_TOKENS:
        yield _make_chunk(
            doc, suffix=suffix, section_type=section_type, section_label=section_label,
            section_parent=section_parent, section_title=section_title,
            char_start=char_start, char_end=char_end, text=text, tier=tier,
        )
    else:
        yield from _split_long_text_window(
            doc, text, base_offset=char_start, suffix=suffix,
            section_type=section_type, section_label=section_label,
            section_parent=section_parent, section_title=section_title, tier=tier,
        )


def chunks_tier_a(doc: dict) -> Iterator[Chunk]:
    text: str = doc["text"]
    structure: list[dict] = doc.get("structure") or []

    artigos = sorted(
        (s for s in structure if s["type"] == "artigo"),
        key=lambda s: s["start"],
    )
    anexos = sorted(
        (s for s in structure if s["type"] == "anexo"),
        key=lambda s: s["start"],
    )
    paragrafos = [s for s in structure if s["type"] == "paragrafo"]

    # Preâmbulo — texto antes do primeiro artigo (e antes do primeiro anexo)
    cut = artigos[0]["start"] if artigos else (anexos[0]["start"] if anexos else len(text))
    preambulo = text[:cut].strip()
    if len(preambulo) >= PREAMBULO_MIN_CHARS:
        yield from _emit_or_split(
            doc, suffix="preambulo",
            section_type="preambulo", section_label="Preâmbulo",
            section_parent="", section_title="",
            char_start=0, char_end=cut, text=preambulo, tier="A",
        )

    # Artigos — chunk por artigo, ou sub-split por § se grande
    for idx, art in enumerate(artigos):
        art_text = text[art["start"]:art["end"]].strip()
        if len(art_text) < MIN_CHUNK_CHARS:
            continue
        n_tok = estimate_tokens(art_text)
        label = art["label"] or f"Art. {idx + 1}"
        # Índice posicional garante unicidade mesmo com labels repetidos
        slug = f"{_slugify_label(label)}_{idx:03d}"

        if n_tok <= TIER_A_MAX_ARTIGO_TOKENS:
            yield _make_chunk(
                doc, suffix=slug,
                section_type="artigo", section_label=label,
                section_parent="", section_title=art.get("title", ""),
                char_start=art["start"], char_end=art["end"],
                text=art_text, tier="A",
            )
        else:
            # Sub-split: usa parágrafos cujo parent == label do artigo
            sub_paras = sorted(
                (p for p in paragrafos if p.get("parent") == label
                 and p["start"] >= art["start"] and p["end"] <= art["end"]),
                key=lambda p: p["start"],
            )
            if not sub_paras:
                # Sem parágrafos detectados — fallback: split em janelas de TIER_B
                yield from _split_long_text_window(
                    doc, art_text,
                    base_offset=art["start"], suffix=slug,
                    section_type="artigo", section_label=label,
                    section_parent="", section_title="", tier="A",
                )
            else:
                # Caput: trecho até o primeiro parágrafo
                caput_end = sub_paras[0]["start"]
                caput_text = text[art["start"]:caput_end].strip()
                if len(caput_text) >= MIN_CHUNK_CHARS:
                    yield from _emit_or_split(
                        doc, suffix=f"{slug}__caput",
                        section_type="artigo", section_label=label,
                        section_parent="", section_title="caput",
                        char_start=art["start"], char_end=caput_end,
                        text=caput_text, tier="A",
                    )
                for j, p in enumerate(sub_paras):
                    p_text = text[p["start"]:p["end"]].strip()
                    if len(p_text) < MIN_CHUNK_CHARS:
                        continue
                    p_label = p["label"] or f"§ {j + 1}"
                    p_slug = f"{_slugify_label(p_label)}_{j:03d}"
                    yield from _emit_or_split(
                        doc, suffix=f"{slug}__{p_slug}",
                        section_type="paragrafo", section_label=p_label,
                        section_parent=label, section_title="",
                        char_start=p["start"], char_end=p["end"],
                        text=p_text, tier="A",
                    )

    # Anexos — 1 chunk por anexo (split automático se exceder hard cap)
    for idx, anx in enumerate(anexos):
        anx_text = text[anx["start"]:anx["end"]].strip()
        if len(anx_text) < MIN_CHUNK_CHARS:
            continue
        label = anx["label"] or f"Anexo {idx + 1}"
        slug = _slugify_label(label) + f"_{idx}"
        yield from _emit_or_split(
            doc, suffix=slug,
            section_type="anexo", section_label=label,
            section_parent="", section_title=anx.get("title", ""),
            char_start=anx["start"], char_end=anx["end"],
            text=anx_text, tier="A",
        )


# ──────────────────────────────────────────────────────────────────────────────
# Tier B — split por parágrafo + merge greedy com overlap
# ──────────────────────────────────────────────────────────────────────────────


def chunks_tier_b(doc: dict) -> Iterator[Chunk]:
    text: str = doc["text"]
    yield from _split_long_text_window(
        doc, text,
        base_offset=0, suffix="b",
        section_type="prosa", section_label="",
        section_parent="", section_title="", tier="B",
    )


def _split_long_text_window(
    doc: dict, text: str, base_offset: int, suffix: str,
    section_type: str, section_label: str,
    section_parent: str, section_title: str, tier: str,
) -> Iterator[Chunk]:
    """Split por parágrafo + merge greedy. Usado por Tier B e fallback de Tier A."""

    target_chars = TIER_B_TARGET_TOKENS * CHARS_PER_TOKEN
    overlap_chars = TIER_B_OVERLAP_TOKENS * CHARS_PER_TOKEN
    hard_max_chars = HARD_MAX_TOKENS * CHARS_PER_TOKEN

    # Localiza paragrafos com offsets relativos ao `text`. Parágrafos maiores
    # que hard_max_chars são quebrados em janelas de target_chars (sem buscar
    # quebra semântica — é fallback para parágrafo único gigante).
    raw_paras: list[tuple[int, int]] = []
    pos = 0
    for m in PARA_SPLIT_RE.finditer(text):
        if m.start() > pos:
            raw_paras.append((pos, m.start()))
        pos = m.end()
    if pos < len(text):
        raw_paras.append((pos, len(text)))

    paras: list[tuple[int, int]] = []
    for s, e in raw_paras:
        if (e - s) <= hard_max_chars:
            paras.append((s, e))
        else:
            for i in range(s, e, target_chars):
                j = min(e, i + target_chars)
                paras.append((i, j))

    # Greedy merge respeitando target
    windows: list[tuple[int, int]] = []
    cur_start: int | None = None
    cur_end: int = 0
    for s, e in paras:
        if cur_start is None:
            cur_start, cur_end = s, e
            continue
        if (e - cur_start) <= target_chars:
            cur_end = e
        else:
            # Se o chunk atual já é grande o bastante, fecha e começa novo c/ overlap
            if (cur_end - cur_start) >= MIN_CHUNK_CHARS:
                windows.append((cur_start, cur_end))
            # Próxima janela começa com overlap (recua até overlap_chars antes de s,
            # mas não antes do início do chunk anterior)
            new_start = max(cur_start, s - overlap_chars) if windows else s
            cur_start, cur_end = new_start, e
    if cur_start is not None and (cur_end - cur_start) >= MIN_CHUNK_CHARS:
        windows.append((cur_start, cur_end))

    if not windows:
        # Fallback bruto: janelas de tamanho fixo
        for i in range(0, len(text), target_chars):
            j = min(len(text), i + target_chars)
            if j - i >= MIN_CHUNK_CHARS:
                windows.append((i, j))

    for k, (s, e) in enumerate(windows):
        sub = text[s:e].strip()
        if len(sub) < MIN_CHUNK_CHARS:
            continue
        yield _make_chunk(
            doc, suffix=f"{suffix}_{k:03d}",
            section_type=section_type, section_label=section_label,
            section_parent=section_parent, section_title=section_title,
            char_start=base_offset + s, char_end=base_offset + e,
            text=sub, tier=tier,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Tier C — 1 chunk por documento
# ──────────────────────────────────────────────────────────────────────────────


def chunks_tier_c(doc: dict) -> Iterator[Chunk]:
    text = doc["text"].strip()
    if len(text) < MIN_CHUNK_CHARS:
        return
    yield _make_chunk(
        doc, suffix="doc",
        section_type="doc", section_label="",
        section_parent="", section_title="",
        char_start=0, char_end=len(doc["text"]),
        text=text, tier="C",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _slugify_label(label: str) -> str:
    """'Art. 1º' → 'art1' ; '§ 3o' → 'p3' ; 'ANEXO I' → 'anexoI'."""
    s = label.lower()
    s = s.replace("§", "p").replace("º", "").replace("°", "").replace("o", "o")
    s = re.sub(r"[^\w]+", "", s, flags=re.UNICODE)
    return s or "x"


def _make_chunk(
    doc: dict, suffix: str, section_type: str, section_label: str,
    section_parent: str, section_title: str,
    char_start: int, char_end: int, text: str, tier: str,
) -> Chunk:
    chunk_id = f"{doc['doc_id']}__{suffix}"
    return Chunk(
        chunk_id=chunk_id,
        doc_id=doc["doc_id"],
        tipo_ato=doc.get("tipo_ato", ""),
        year=doc.get("year", 0),
        tier=tier,
        section_type=section_type,
        section_label=section_label,
        section_parent=section_parent,
        section_title=section_title,
        title=doc.get("title", ""),
        ementa=doc.get("ementa", ""),
        filename=doc.get("filename", ""),
        url=build_url(doc.get("filename", "")),
        char_start=char_start,
        char_end=char_end,
        n_chars=len(text),
        n_tokens_est=estimate_tokens(text),
        text=text,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Roteador de tier + processamento
# ──────────────────────────────────────────────────────────────────────────────


def classify_tier(doc: dict) -> str:
    artigos = [s for s in doc.get("structure") or [] if s["type"] == "artigo"]
    if artigos:
        return "A"
    if doc.get("n_tokens_est", 0) <= TIER_C_MAX_TOKENS:
        return "C"
    return "B"


def chunks_for_doc(doc: dict) -> Iterator[Chunk]:
    tier = classify_tier(doc)
    if tier == "A":
        yield from chunks_tier_a(doc)
    elif tier == "B":
        yield from chunks_tier_b(doc)
    else:
        yield from chunks_tier_c(doc)


def process(in_path: Path, out_path: Path, limit: int | None = None) -> dict:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n_docs = 0
    n_chunks = 0
    by_tier: dict[str, int] = {"A": 0, "B": 0, "C": 0}
    by_tipo_chunks: dict[str, int] = {}
    n_skipped = 0
    t0 = time.time()

    with in_path.open(encoding="utf-8") as fin, out_path.open("w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                doc = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning("JSON inválido: %s", e)
                n_skipped += 1
                continue

            n_docs += 1
            doc_chunks = 0
            for ck in chunks_for_doc(doc):
                fout.write(json.dumps(asdict(ck), ensure_ascii=False) + "\n")
                doc_chunks += 1
                by_tier[ck.tier] += 1
                by_tipo_chunks[ck.tipo_ato] = by_tipo_chunks.get(ck.tipo_ato, 0) + 1
            n_chunks += doc_chunks

            if n_docs % 5000 == 0:
                elapsed = time.time() - t0
                rate = n_docs / elapsed if elapsed else 0
                logger.info(
                    "Progresso: %d docs → %d chunks  (%.1f doc/s, %.2f chunks/doc)",
                    n_docs, n_chunks, rate, n_chunks / max(1, n_docs),
                )
            if limit and n_docs >= limit:
                break

    elapsed = time.time() - t0
    summary = {
        "n_docs": n_docs,
        "n_chunks": n_chunks,
        "n_skipped_docs": n_skipped,
        "chunks_per_doc": round(n_chunks / max(1, n_docs), 2),
        "elapsed_sec": round(elapsed, 1),
        "by_tier": by_tier,
        "by_tipo_top10": dict(
            sorted(by_tipo_chunks.items(), key=lambda kv: -kv[1])[:10]
        ),
    }
    return summary


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--in", dest="in_path", type=Path, required=True,
                   help="Entrada parsed.jsonl")
    p.add_argument("--out", dest="out_path", type=Path, required=True,
                   help="Saída chunks.jsonl")
    p.add_argument("--limit", type=int, default=None,
                   help="Processa apenas os N primeiros docs (smoke)")
    p.add_argument("--summary-json", type=Path, default=None,
                   help="Onde escrever o JSON de sumário (opcional)")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if not args.in_path.exists():
        logger.error("Arquivo de entrada não existe: %s", args.in_path)
        return 2

    logger.info("Lendo %s → %s", args.in_path, args.out_path)
    summary = process(args.in_path, args.out_path, limit=args.limit)
    logger.info("Concluído. Sumário:\n%s", json.dumps(summary, indent=2, ensure_ascii=False))

    if args.summary_json:
        args.summary_json.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        logger.info("Sumário gravado em %s", args.summary_json)

    return 0


if __name__ == "__main__":
    sys.exit(main())
