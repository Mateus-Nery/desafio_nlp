"""Análise exploratória da amostra de PDFs baixados — informa estratégia de parsing/chunking.

Roda PyMuPDF em todos os PDFs em --pdfs-dir e gera:
  - Por PDF: páginas, bytes, chars extraíveis, ratio texto/imagem, OCR-suspeito
  - Agregado por tipo de ato (DSP/PRT/REN/REA/...): contagens, distribuições
  - Amostras textuais (primeiros 600 chars) de 2-3 PDFs representativos por tipo
  - Detecção de tabelas e multi-coluna heurística
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

import fitz  # PyMuPDF

TIPO_PATTERN = re.compile(r"^([a-z]+?)(?=\d|_|$)", re.IGNORECASE)


def extrair_tipo_ato(filename: str) -> str:
    """Extrai prefixo alfa do nome do arquivo (ex: 'dsp2022021spde.pdf' -> 'dsp')."""
    name = Path(filename).stem.lower()
    m = TIPO_PATTERN.match(name)
    return m.group(1) if m else "outro"


def analisar_pdf(path: Path) -> dict:
    info: dict = {
        "filename": path.name,
        "size_bytes": path.stat().st_size,
        "tipo_ato": extrair_tipo_ato(path.name),
        "ano": path.parent.name,
    }
    try:
        doc = fitz.open(path)
    except Exception as e:
        info["error"] = f"open_failed: {e}"
        return info

    try:
        info["n_pages"] = doc.page_count
        info["pdf_version"] = doc.metadata.get("format", "")
        info["creator"] = (doc.metadata.get("creator") or "").strip()
        info["producer"] = (doc.metadata.get("producer") or "").strip()
        info["encrypted"] = doc.is_encrypted
        info["needs_pass"] = doc.needs_pass

        # Tem assinatura digital?
        try:
            info["has_signatures"] = any(
                w.field_type == fitz.PDF_WIDGET_TYPE_SIGNATURE
                for page in doc for w in page.widgets() or []
            )
        except Exception:
            info["has_signatures"] = None

        total_chars = 0
        total_images = 0
        total_blocks_text = 0
        total_blocks_image = 0
        page_widths: list[float] = []
        page_heights: list[float] = []
        multi_col_pages = 0
        first_text = ""

        # Amostra: até 5 primeiras páginas para evitar custo em PDFs gigantes
        sample_pages = min(doc.page_count, 5)
        for i in range(sample_pages):
            page = doc[i]
            page_widths.append(page.rect.width)
            page_heights.append(page.rect.height)

            text = page.get_text("text") or ""
            if i == 0:
                first_text = text[:600]
            total_chars += len(text)

            blocks = page.get_text("blocks") or []
            text_blocks = [b for b in blocks if len(b) >= 7 and b[6] == 0]
            image_blocks = [b for b in blocks if len(b) >= 7 and b[6] == 1]
            total_blocks_text += len(text_blocks)
            total_blocks_image += len(image_blocks)
            total_images += len(page.get_images(full=True) or [])

            # Heurística multi-coluna: se text_blocks têm centros x agrupados em >=2 clusters
            if len(text_blocks) >= 4:
                centers = sorted((b[0] + b[2]) / 2 for b in text_blocks)
                page_w = page.rect.width
                left = sum(1 for c in centers if c < page_w * 0.45)
                right = sum(1 for c in centers if c > page_w * 0.55)
                if left >= 2 and right >= 2:
                    multi_col_pages += 1

        info["sample_pages"] = sample_pages
        info["chars_first_pages"] = total_chars
        info["chars_per_page_avg"] = total_chars / sample_pages if sample_pages else 0
        info["images_first_pages"] = total_images
        info["text_blocks"] = total_blocks_text
        info["image_blocks"] = total_blocks_image
        info["multi_col_pages"] = multi_col_pages
        info["page_w_avg"] = statistics.mean(page_widths) if page_widths else 0
        info["page_h_avg"] = statistics.mean(page_heights) if page_heights else 0
        # Heurística OCR-suspeito: muito pouco texto + tem imagens
        info["ocr_suspect"] = (
            info["chars_per_page_avg"] < 100 and total_images > 0
        )
        info["first_text_sample"] = first_text
        return info
    finally:
        doc.close()


def aggregate(results: list[dict]) -> dict:
    ok = [r for r in results if "error" not in r]
    err = [r for r in results if "error" in r]
    by_tipo: dict[str, list[dict]] = defaultdict(list)
    for r in ok:
        by_tipo[r["tipo_ato"]].append(r)

    out: dict = {
        "n_total": len(results),
        "n_ok": len(ok),
        "n_error": len(err),
        "errors_sample": [r["filename"] + ": " + r["error"] for r in err[:5]],
        "n_encrypted": sum(1 for r in ok if r.get("encrypted")),
        "n_signed": sum(1 for r in ok if r.get("has_signatures")),
        "n_ocr_suspect": sum(1 for r in ok if r.get("ocr_suspect")),
        "n_multi_col": sum(1 for r in ok if (r.get("multi_col_pages") or 0) > 0),
        "creators_top": Counter(r.get("creator", "") for r in ok).most_common(8),
        "producers_top": Counter(r.get("producer", "") for r in ok).most_common(8),
        "pdf_versions": Counter(r.get("pdf_version", "") for r in ok).most_common(),
        "tipos_ato": {},
    }

    for tipo, rs in sorted(by_tipo.items(), key=lambda x: -len(x[1])):
        pages = [r["n_pages"] for r in rs if r.get("n_pages") is not None]
        chars_avg = [r["chars_per_page_avg"] for r in rs]
        sizes_kb = [r["size_bytes"] / 1024 for r in rs]
        out["tipos_ato"][tipo] = {
            "n": len(rs),
            "pages_min": min(pages) if pages else 0,
            "pages_max": max(pages) if pages else 0,
            "pages_p50": statistics.median(pages) if pages else 0,
            "pages_p90": statistics.quantiles(pages, n=10)[8] if len(pages) >= 10 else max(pages) if pages else 0,
            "chars_per_page_avg": statistics.mean(chars_avg) if chars_avg else 0,
            "size_kb_p50": statistics.median(sizes_kb) if sizes_kb else 0,
            "size_kb_p90": statistics.quantiles(sizes_kb, n=10)[8] if len(sizes_kb) >= 10 else max(sizes_kb) if sizes_kb else 0,
            "n_ocr_suspect": sum(1 for r in rs if r.get("ocr_suspect")),
            "n_multi_col": sum(1 for r in rs if (r.get("multi_col_pages") or 0) > 0),
            "samples": [
                {"file": r["filename"], "ano": r["ano"], "pages": r["n_pages"], "first_text": r.get("first_text_sample", "")[:300]}
                for r in rs[:2]
            ],
        }
    return out


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdfs-dir", required=True)
    ap.add_argument("--report-json", default=None)
    args = ap.parse_args()

    root = Path(args.pdfs_dir)
    pdfs = sorted([p for p in root.rglob("*.pdf") if p.is_file()])
    print(f"Encontrados {len(pdfs)} PDFs em {root}")

    results: list[dict] = []
    for i, p in enumerate(pdfs, 1):
        if i % 50 == 0:
            print(f"  {i}/{len(pdfs)}...", flush=True)
        results.append(analisar_pdf(p))

    agg = aggregate(results)

    # Salva JSON ANTES dos prints, pra não perder relatório se algo quebrar no console
    if args.report_json:
        Path(args.report_json).write_text(
            json.dumps({"summary": agg, "per_pdf": results}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Relatório completo salvo em {args.report_json}")

    print("\n=== RESUMO GERAL ===")
    print(f"Total: {agg['n_total']} | OK: {agg['n_ok']} | Erros: {agg['n_error']}")
    if agg["errors_sample"]:
        print("Sample erros:", agg["errors_sample"])
    print(f"Encrypted: {agg['n_encrypted']} | Signed: {agg['n_signed']}")
    print(f"OCR-suspect (<100 chars/page com imagens): {agg['n_ocr_suspect']}")
    print(f"Multi-coluna detectado: {agg['n_multi_col']}")
    print(f"PDF versions: {agg['pdf_versions']}")
    print(f"\nTop creators: {agg['creators_top']}")
    print(f"Top producers: {agg['producers_top']}")

    print("\n=== POR TIPO DE ATO ===")
    for tipo, info in agg["tipos_ato"].items():
        print(f"\n[{tipo}]  n={info['n']}")
        print(f"  páginas: min={info['pages_min']} p50={info['pages_p50']} p90={info['pages_p90']:.0f} max={info['pages_max']}")
        print(f"  chars/página (avg): {info['chars_per_page_avg']:.0f}")
        print(f"  tamanho KB: p50={info['size_kb_p50']:.0f} p90={info['size_kb_p90']:.0f}")
        print(f"  ocr_suspect={info['n_ocr_suspect']}  multi_col={info['n_multi_col']}")
        for s in info["samples"]:
            snippet = " ".join((s["first_text"] or "").split())
            print(f"  - {s['ano']}/{s['file']} ({s['pages']}p) :: {snippet[:200]!r}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
