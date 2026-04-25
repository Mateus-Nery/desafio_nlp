"""Parser ANEEL — PDF → parsed.jsonl

Pipeline (Fase 2 do RAG):
  PDF → blocos ordenados (multi-coluna) → tabelas semânticas em Markdown
      → headers/footers removidos (regex + heurística repetição)
      → numeração solta joinada → footnotes extraídas
      → estrutura (CAPÍTULO/Art./§/ANEXO) com offsets
  → 1 linha JSONL por documento

Uso:
  python -m src.parse_pdfs --pdfs-root data/pdfs_aneel \\
                           --out artifacts/parsed.jsonl \\
                           --workers 8
"""
from __future__ import annotations

import argparse
import json
import logging
import multiprocessing as mp
import re
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable

import fitz  # PyMuPDF

logger = logging.getLogger("parse_pdfs")


# ──────────────────────────────────────────────────────────────────────────────
# Padrões de boilerplate ANEEL — regex aplicados após extração por página.
# ──────────────────────────────────────────────────────────────────────────────

BOILERPLATE_PATTERNS: list[re.Pattern] = [
    # Cabeçalho oficial
    re.compile(r"^\s*AG[ÊE]NCIA NACIONAL DE ENERGIA EL[ÉE]TRICA\s*[–\-]\s*ANEEL\s*$", re.M | re.I),
    # Cabeçalho de páginas internas de notas técnicas
    re.compile(r"^\s*(?:P\.|Fl\.)\s*\d+\s*Nota\s+T[ée]cnica\s+n[ºo°]\s*[\d/\-]+.*$", re.M | re.I),
    # Footnote boilerplate de Nota Técnica
    re.compile(
        r"^\s*\*\s*A\s+Nota\s+T[ée]cnica\s+é\s+um\s+documento\s+emitido\s+pelas\s+Unidades.*?Agência\.\s*$",
        re.M | re.I,
    ),
    # Linhas de underscores (divisores)
    re.compile(r"^\s*_{20,}\s*$", re.M),
    # Carimbos de superintendência avulsos (linha solitária)
    re.compile(r"^\s*Superintendência\s+de\s+[^\n]{0,80}–\s*[A-Z]{2,5}/ANEEL\s*$", re.M),
    # "Processo nº ..." quando aparece como header repetido (linha solitária)
    re.compile(r"^\s*Processo\s+n[ºo°]\s*\d{5,}\.\d{4,6}/\d{4}-\d{2}\s*$", re.M),
    # Boilerplate de portarias
    re.compile(r"^\s*Este\s+texto\s+não\s+substitui\s+o\s+publicado\s+no\s+Boletim\s+Administrativo.*$", re.M),
    re.compile(r"^\s*Retificado\s+no\s+D\.O\.\s+de\s+\d{1,2}\.\d{1,2}\.\d{4}\.?\s*$", re.M | re.I),
    re.compile(r"^\s*\(Tornada\s+sem\s+efeito\s+pela.*\)\s*$", re.M | re.I),
    # "Texto Original" / "Voto" como linhas solitárias (separadores visuais antes do dispositivo)
    re.compile(r"^\s*Texto\s+Original\s*$", re.M),
]

# Padrões de footnotes — capturados ANTES do cleaning de boilerplate.
# Formato: linha começando com "<num> <texto>" no rodapé, geralmente após uma
# linha vazia ou ao fim da página. Aceita superíndice colado também.
FOOTNOTE_LINE_RE = re.compile(
    r"^\s*(\d{1,3})\s+((?:Documento\s+SIC|SIC)\s+n[ºo°][^\n]+|[A-Z][^\n]{8,200})$",
    re.M,
)

# Padrões estruturais (preservar)
STRUCTURE_PATTERNS = {
    "capitulo": re.compile(r"^\s*(CAP[ÍI]TULO\s+[IVXLCDM]+)\b\s*[–\-]?\s*(.*)$", re.M),
    "secao":    re.compile(r"^\s*(Se[çc][ãa]o\s+[IVXLCDM]+)\b\s*[–\-]?\s*(.*)$", re.M),
    "artigo":   re.compile(r"^\s*(Art\.\s*\d+[ºo°]?)\b", re.M),
    "paragrafo": re.compile(r"^\s*(§\s*\d+[ºo°]?|Parágrafo\s+único)\b", re.M | re.I),
    "anexo":    re.compile(r"^\s*(ANEXO(?:\s+[IVXLCDM]+)?)\b\s*[–\-]?\s*(.*)$", re.M),
    "quadro":   re.compile(r"^\s*(Quadro\s+\d+)\b\s*[–\-:]?\s*(.*)$", re.M),
    "tabela":   re.compile(r"^\s*(Tabela\s+\d+)\b\s*[–\-:]?\s*(.*)$", re.M),
}

# Para extração de metadados de cabeçalho
PROCESSO_RE = re.compile(
    r"PROCESSO\s*(?:n[ºo°]?\s*)?[:.]?\s*(\d{5,})\.\s*(\d{4,6})\s*/\s*(\d{4})\s*-\s*(\d{2})",
    re.I,
)

# Numeração solta (linha contendo apenas "N." onde N é 1-3 dígitos)
LONE_NUMBER_RE = re.compile(r"^(\d{1,3})\.\s*$", re.M)

# Tipos de ato → derivados do prefixo do filename
TIPO_ATO_PREFIXES = (
    "ren", "reh", "rea", "prt", "dsp", "ndsp", "nreh", "nren", "nrea", "ndsp",
    "area", "adsp", "aprt", "areh", "ect", "vot",
)


# ──────────────────────────────────────────────────────────────────────────────
# Estruturas de dados
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class StructureNode:
    type: str
    label: str
    start: int
    end: int = -1
    title: str = ""
    parent: str = ""


@dataclass
class TableExtract:
    id: str
    page: int
    markdown: str
    rows: int
    cols: int


@dataclass
class Footnote:
    num: int
    text: str


@dataclass
class ParsedDoc:
    doc_id: str
    tipo_ato: str
    year: int
    filename: str
    title: str
    ementa: str
    processo: str
    n_pages: int
    n_chars: int
    n_tokens_est: int
    is_ocr_suspect: bool
    pdf_creator: str
    text: str
    structure: list[StructureNode] = field(default_factory=list)
    tables: list[TableExtract] = field(default_factory=list)
    footnotes: list[Footnote] = field(default_factory=list)

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, ensure_ascii=False)


# ──────────────────────────────────────────────────────────────────────────────
# Extração página a página
# ──────────────────────────────────────────────────────────────────────────────

def extract_blocks_sorted(page: fitz.Page) -> str:
    """Extrai texto ordenado por (y0, x0) — robusto a multi-coluna."""
    blocks = page.get_text("blocks", sort=True) or []
    # Cada bloco: (x0, y0, x1, y1, "text", block_no, block_type)
    # block_type 0 = texto, 1 = imagem
    parts: list[tuple[float, float, str]] = []
    for b in blocks:
        if len(b) < 7 or b[6] != 0:
            continue
        text = (b[4] or "").strip()
        if text:
            parts.append((round(b[1], 1), round(b[0], 1), text))
    parts.sort(key=lambda p: (p[0], p[1]))
    return "\n".join(p[2] for p in parts)


def is_table_semantic(table: Any) -> bool:
    """Heurística: retém tabela se <70% das células são puramente numéricas/coords.

    Filtra tabelas de coordenadas (vértices UTM) e listas grandes de IDs/CEG
    que não trazem conteúdo legal recuperável.
    """
    try:
        rows = table.extract()
    except Exception:
        return False
    if not rows:
        return False
    flat = [str(c) for row in rows for c in row if c]
    if not flat:
        return False
    numeric_re = re.compile(r"^[\d\.,\-\s]+$")
    n_numeric = sum(1 for c in flat if numeric_re.match(c))
    ratio = n_numeric / len(flat)
    # Também filtra tabelas muito grandes e majoritariamente numéricas
    if table.row_count > 15 and ratio > 0.6:
        return False
    return ratio < 0.7


def table_to_markdown(table: Any) -> str:
    """Converte tabela PyMuPDF em Markdown."""
    try:
        rows = table.extract() or []
    except Exception:
        return ""
    if not rows:
        return ""
    # Normaliza células (replace newlines internos por espaço, strip)
    norm = [
        [(str(c).replace("\n", " ").strip() if c else "") for c in row]
        for row in rows
    ]
    n_cols = max(len(r) for r in norm)
    norm = [r + [""] * (n_cols - len(r)) for r in norm]
    header = norm[0]
    body = norm[1:] if len(norm) > 1 else []
    md_lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * n_cols) + " |",
    ]
    for row in body:
        md_lines.append("| " + " | ".join(row) + " |")
    return "\n".join(md_lines)


def extract_page(page: fitz.Page, page_num: int) -> tuple[str, list[TableExtract]]:
    """Retorna (texto da página com placeholders de tabela, lista de tabelas)."""
    text = extract_blocks_sorted(page)
    tables: list[TableExtract] = []

    try:
        found = page.find_tables()
        page_tabs = list(found.tables) if found and found.tables else []
    except Exception:
        page_tabs = []

    for j, tab in enumerate(page_tabs):
        if not is_table_semantic(tab):
            continue
        md = table_to_markdown(tab)
        if not md:
            continue
        tid = f"p{page_num}t{j+1}"
        tables.append(TableExtract(
            id=tid,
            page=page_num,
            markdown=md,
            rows=tab.row_count,
            cols=tab.col_count,
        ))
        # Placeholder no fluxo de texto
        text += f"\n\n[[TABELA:{tid}]]\n"

    return text, tables


# ──────────────────────────────────────────────────────────────────────────────
# Cleaning de texto
# ──────────────────────────────────────────────────────────────────────────────

def detect_repeated_lines(pages_text: list[str], min_pages: int = 3) -> set[str]:
    """Detecta linhas que aparecem em ≥ min_pages páginas (provável header/footer)."""
    if len(pages_text) < min_pages:
        return set()
    line_pages: dict[str, set[int]] = {}
    for i, pt in enumerate(pages_text):
        seen_in_page: set[str] = set()
        for line in pt.splitlines():
            s = line.strip()
            if len(s) < 8 or len(s) > 200:
                continue
            if s in seen_in_page:
                continue
            seen_in_page.add(s)
            line_pages.setdefault(s, set()).add(i)
    return {ln for ln, pgs in line_pages.items() if len(pgs) >= min_pages}


def normalize_chars(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = (text
            .replace("\u00a0", " ")   # NBSP
            .replace("\u2009", " ")   # thin space
            .replace("\u200b", "")    # zero-width
            .replace("\u201c", '"').replace("\u201d", '"')
            .replace("\u2018", "'").replace("\u2019", "'")
            )
    return text


def remove_boilerplate(text: str, repeated: set[str]) -> str:
    for pat in BOILERPLATE_PATTERNS:
        text = pat.sub("", text)
    if repeated:
        kept = []
        for line in text.splitlines():
            if line.strip() in repeated:
                continue
            kept.append(line)
        text = "\n".join(kept)
    return text


def join_lone_paragraph_numbers(text: str) -> str:
    """Junta linhas '12.' soltas com a próxima linha não-vazia.

    Padrão comum em Votos/Notas Técnicas: o número do parágrafo aparece na 1ª
    coluna e o texto na 2ª, e a extração linear separa em linhas.
    """
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        m = LONE_NUMBER_RE.match(lines[i])
        if m:
            # Procura próxima linha não-vazia
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines):
                out.append(f"{m.group(1)}. {lines[j].strip()}")
                i = j + 1
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def collapse_blank_lines(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fix_line_hyphenation(text: str) -> str:
    """Junta palavras hifenizadas em fim de linha: 'autori-\nzação' → 'autorização'.

    Só aplica quando ambos os lados do hífen são LETRAS — preserva hifens
    estruturais em IDs (Processo nº 48500.000123/2022-45) e datas (20-25).
    """
    return re.sub(r"([A-Za-zÀ-ÿ])-\n([a-zà-ÿ])", r"\1\2", text)


def extract_footnotes(text: str) -> tuple[str, list[Footnote]]:
    """Tenta separar footnotes do corpo do texto.

    Estratégia conservadora: detecta blocos de footnote no FINAL de cada página
    (linhas iniciando com número 1-3 dígitos seguidos de texto longo, após
    bloco de linhas em branco). Para evitar falsos positivos com numeração
    de parágrafos, exige padrões discriminantes (Documento SIC, Lei nº, etc.).
    """
    footnotes: list[Footnote] = []
    seen: set[int] = set()
    # Footnotes "Documento SIC nº ..." e similares no rodapé
    fn_re = re.compile(
        r"(?m)^\s*(\d{1,3})\s+((?:Documento\s+SIC|SIC|Concessionária\s+até|Essas\s+usinas|Nota\s+Técnica\s+n[ºo°]).*?)(?=\n\s*\n|\n\s*\d{1,3}\s+(?:Documento|SIC|Concessionária|Nota)|\Z)",
        re.S,
    )
    for m in fn_re.finditer(text):
        num = int(m.group(1))
        body = m.group(2).strip()
        body = re.sub(r"\s+", " ", body)
        if num in seen or len(body) < 10:
            continue
        seen.add(num)
        footnotes.append(Footnote(num=num, text=body))

    if footnotes:
        text = fn_re.sub("", text)
    return text, footnotes


# ──────────────────────────────────────────────────────────────────────────────
# Extração de metadados
# ──────────────────────────────────────────────────────────────────────────────

def extract_title_and_ementa(text: str, tipo_ato: str) -> tuple[str, str]:
    """Heurística por tipo de ato.

    title: linha com TIPO + Nº + DATA (em maiúsculas).
    ementa: parágrafo após o título, antes de "Voto"/"Texto Original"/"O DIRETOR".
    """
    lines = [l.strip() for l in text.splitlines()]
    title = ""
    title_idx = -1

    title_patterns = [
        re.compile(r"^RESOLU[ÇC][ÃA]O\s+(?:NORMATIVA|HOMOLOGAT[ÓO]RIA|AUTORIZATIVA)\s+(?:ANEEL\s+)?N[ºo°]?\s*\S+", re.I),
        re.compile(r"^PORTARIA\s+(?:ANEEL\s+)?N[ºo°°]?\s*\S+", re.I),
        re.compile(r"^DESPACHO(?:\s+\w+){0,3}\s+N[ºo°]?\s*\S+", re.I),
        re.compile(r"^Nota\s+T[ée]cnica\s+n[ºo°]?\s*\S+", re.I),
        re.compile(r"^VOTO\s*$", re.I),  # Voto Área — título genérico
    ]
    for i, ln in enumerate(lines[:80]):
        for pat in title_patterns:
            if pat.match(ln):
                title = ln
                title_idx = i
                break
        if title:
            break

    ementa = ""
    if title_idx >= 0:
        # Pega blocos de texto após título até atingir "Voto", "O DIRETOR-GERAL", "resolve:"
        stop_re = re.compile(r"^(Voto|Texto\s+Original|O\s+DIRETOR|O\s+SUPERINTENDENTE|resolve:|I\s*[-–.])", re.I)
        buf: list[str] = []
        for ln in lines[title_idx + 1: title_idx + 30]:
            if not ln:
                if buf:
                    break
                continue
            if stop_re.match(ln):
                break
            buf.append(ln)
        ementa = " ".join(buf).strip()
        # ementa típica é 1 parágrafo ~200-500 chars
        if len(ementa) > 800:
            ementa = ementa[:800].rsplit(" ", 1)[0] + "..."

    return title, ementa


def extract_structure(text: str) -> list[StructureNode]:
    nodes: list[StructureNode] = []
    for typ, pat in STRUCTURE_PATTERNS.items():
        for m in pat.finditer(text):
            label = m.group(1).strip()
            title = ""
            if m.lastindex and m.lastindex >= 2:
                title = (m.group(2) or "").strip()
            nodes.append(StructureNode(type=typ, label=label, start=m.start(), end=-1, title=title))
    nodes.sort(key=lambda n: n.start)

    # Calcula end como o start do próximo node de mesmo nível ou superior
    rank = {"anexo": 0, "capitulo": 1, "secao": 2, "artigo": 3, "paragrafo": 4, "quadro": 5, "tabela": 5}
    for i, node in enumerate(nodes):
        node_rank = rank.get(node.type, 99)
        end = len(text)
        for j in range(i + 1, len(nodes)):
            if rank.get(nodes[j].type, 99) <= node_rank:
                end = nodes[j].start
                break
        node.end = end

    # Atribui parent: paragrafo → último artigo anterior
    last_art = ""
    for n in nodes:
        if n.type == "artigo":
            last_art = n.label
        elif n.type == "paragrafo":
            n.parent = last_art

    return nodes


# ──────────────────────────────────────────────────────────────────────────────
# Identificação por filename
# ──────────────────────────────────────────────────────────────────────────────

TIPO_RE = re.compile(r"^([a-z]+)\d", re.I)


def parse_doc_id(pdf_path: Path, pdfs_root: Path) -> tuple[str, str, int]:
    rel = pdf_path.relative_to(pdfs_root)
    doc_id = str(rel.with_suffix(""))
    stem = pdf_path.stem
    m = TIPO_RE.match(stem)
    tipo = (m.group(1).lower() if m else "outros")
    # year: tenta dos primeiros 4 dígitos após o tipo, senão da pasta pai
    year = 0
    digits = re.search(r"(\d{4})", stem)
    if digits:
        try:
            y = int(digits.group(1))
            if 1996 <= y <= 2030:
                year = y
        except ValueError:
            pass
    if year == 0 and rel.parent.name.isdigit():
        year = int(rel.parent.name)
    return doc_id, tipo, year


def is_ocr_suspect(text: str, n_pages: int) -> bool:
    """Heurística: <50 chars/página → provável PDF imagem sem OCR."""
    if n_pages == 0:
        return False
    return (len(text) / n_pages) < 50


def estimate_tokens(text: str) -> int:
    # Aproximação conservadora pt-BR: ~3.7 chars/token
    return max(1, int(len(text) / 3.7))


# ──────────────────────────────────────────────────────────────────────────────
# Pipeline principal
# ──────────────────────────────────────────────────────────────────────────────

def parse_pdf(pdf_path: Path, pdfs_root: Path) -> ParsedDoc | None:
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        logger.warning("[OPEN-FAIL] %s: %s", pdf_path.name, e)
        return None

    try:
        n_pages = doc.page_count
        creator = doc.metadata.get("creator", "") or ""

        page_texts: list[str] = []
        all_tables: list[TableExtract] = []
        for i in range(n_pages):
            try:
                page = doc[i]
                pt, tabs = extract_page(page, i + 1)
                page_texts.append(pt)
                all_tables.extend(tabs)
            except Exception as e:
                logger.warning("[PAGE-FAIL] %s p%d: %s", pdf_path.name, i + 1, e)
                page_texts.append("")

        # Detecta linhas repetidas (header/footer dinâmico)
        repeated = detect_repeated_lines(page_texts, min_pages=3)

        # Junta + cleaning sequencial
        text = "\n\n".join(page_texts)
        text = normalize_chars(text)
        text = remove_boilerplate(text, repeated)
        text = fix_line_hyphenation(text)
        text = join_lone_paragraph_numbers(text)
        text, footnotes = extract_footnotes(text)
        text = collapse_blank_lines(text)

        doc_id, tipo_ato, year = parse_doc_id(pdf_path, pdfs_root)
        title, ementa = extract_title_and_ementa(text, tipo_ato)

        proc_match = PROCESSO_RE.search(text)
        processo = (
            f"{proc_match.group(1)}.{proc_match.group(2)}/{proc_match.group(3)}-{proc_match.group(4)}"
            if proc_match else ""
        )

        structure = extract_structure(text)

        parsed = ParsedDoc(
            doc_id=doc_id,
            tipo_ato=tipo_ato,
            year=year,
            filename=pdf_path.name,
            title=title,
            ementa=ementa,
            processo=processo,
            n_pages=n_pages,
            n_chars=len(text),
            n_tokens_est=estimate_tokens(text),
            is_ocr_suspect=is_ocr_suspect(text, n_pages),
            pdf_creator=creator,
            text=text,
            structure=structure,
            tables=all_tables,
            footnotes=footnotes,
        )
        return parsed
    finally:
        doc.close()


def _worker(args: tuple[str, str]) -> str | None:
    pdf_str, root_str = args
    try:
        parsed = parse_pdf(Path(pdf_str), Path(root_str))
        if parsed is None:
            return None
        return parsed.to_json()
    except Exception as e:
        logger.warning("[PARSE-FAIL] %s: %s", pdf_str, e)
        return None


def iter_pdfs(pdfs_root: Path) -> Iterable[Path]:
    yield from sorted(pdfs_root.rglob("*.pdf"))


def load_already_parsed(out_path: Path) -> set[str]:
    if not out_path.exists():
        return set()
    seen: set[str] = set()
    with out_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
                seen.add(d["doc_id"])
            except Exception:
                continue
    return seen


def main() -> None:
    p = argparse.ArgumentParser(description="ANEEL PDF parser → parsed.jsonl")
    p.add_argument("--pdfs-root", type=Path, default=Path("data/pdfs_aneel"))
    p.add_argument("--out", type=Path, default=Path("artifacts/parsed.jsonl"))
    p.add_argument("--workers", type=int, default=max(1, mp.cpu_count() - 1))
    p.add_argument("--limit", type=int, default=0, help="0 = todos")
    p.add_argument("--resume", action="store_true", help="Pula docs já em parsed.jsonl")
    p.add_argument("--samples-only", action="store_true", help="Roda só nas 8 amostras de explore_pdfs.py")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args.out.parent.mkdir(parents=True, exist_ok=True)

    pdfs = list(iter_pdfs(args.pdfs_root))
    if args.samples_only:
        sample_stems = {
            "dsp2022021spde", "prt2022sn264", "rea20223229", "ren20221008",
            "reh20223008ti", "ndsp2022060", "area202210992_1", "nreh2022059",
            # fallbacks (presentes em explore_output)
            "prt20153774", "rea20165599ti", "nreh20162014",
        }
        pdfs = [p for p in pdfs if p.stem in sample_stems]

    if args.limit > 0:
        pdfs = pdfs[: args.limit]

    seen: set[str] = set()
    mode = "w"
    if args.resume:
        seen = load_already_parsed(args.out)
        mode = "a"
        if seen:
            logger.info("Resume: %d docs já processados", len(seen))

    todo = []
    for pdf in pdfs:
        rel = pdf.relative_to(args.pdfs_root)
        doc_id = str(rel.with_suffix(""))
        if doc_id in seen:
            continue
        todo.append((str(pdf), str(args.pdfs_root)))

    logger.info("PDFs a processar: %d (workers=%d)", len(todo), args.workers)
    t0 = time.time()
    n_ok = 0
    n_fail = 0
    tipos = Counter()

    with args.out.open(mode, encoding="utf-8") as fout:
        if args.workers <= 1:
            results = (_worker(t) for t in todo)
        else:
            pool = mp.Pool(processes=args.workers)
            results = pool.imap_unordered(_worker, todo, chunksize=8)

        for i, line in enumerate(results, 1):
            if line is None:
                n_fail += 1
            else:
                fout.write(line + "\n")
                n_ok += 1
                try:
                    tipos[json.loads(line)["tipo_ato"]] += 1
                except Exception:
                    pass
            if i % 500 == 0:
                rate = i / max(1, time.time() - t0)
                logger.info("Progresso: %d/%d  ok=%d  fail=%d  %.1f doc/s", i, len(todo), n_ok, n_fail, rate)

        if args.workers > 1:
            pool.close()
            pool.join()

    dur = time.time() - t0
    logger.info("Concluído em %.1fs — ok=%d, fail=%d, %.1f doc/s", dur, n_ok, n_fail, n_ok / max(1, dur))
    logger.info("Top tipos: %s", tipos.most_common(10))


if __name__ == "__main__":
    sys.exit(main())
