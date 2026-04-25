"""Extrai texto cru dos PDFs amostra para inspecionar padrões de cleaning.

Saída: ./explore_output/<tipo>_<filename>.txt com texto bruto das primeiras N páginas.
"""
from __future__ import annotations

import sys
from pathlib import Path

import fitz  # PyMuPDF


SAMPLES = [
    ("dsp",  "2022/dsp2022021spde.pdf",  "Despacho curto (Tier C)"),
    ("prt",  "2022/prt2022sn264.pdf",     "Portaria (Tier B)"),
    ("rea",  "2022/rea20223229.pdf",      "Resolução Autorizativa (Tier B)"),
    ("ren",  "2022/ren20221008.pdf",      "Resolução Normativa (Tier A — denso jurídico)"),
    ("reh",  "2022/reh20223008ti.pdf",    "Resolução Homologatória (Tier A)"),
    ("ndsp", "2022/ndsp2022060.pdf",      "Nota Técnica DSP (Tier A — com tabelas)"),
    ("area", "2022/area202210992_1.pdf",  "Voto Área (Tier B — multi-coluna)"),
    ("nreh", "2022/nreh2022059.pdf",      "Nota Técnica REH (Tier A — longa)"),
]


def main(pdfs_root: Path, out_dir: Path, max_pages: int = 3) -> None:
    out_dir.mkdir(exist_ok=True, parents=True)
    for tipo, rel_path, descricao in SAMPLES:
        pdf_path = pdfs_root / rel_path
        if not pdf_path.exists():
            # Tenta achar qualquer PDF do mesmo tipo
            tipo_lower = tipo.lower()
            candidates = sorted(pdfs_root.rglob(f"{tipo_lower}*.pdf"))
            if not candidates:
                print(f"[SKIP] {tipo}: nenhum PDF encontrado")
                continue
            pdf_path = candidates[0]
        try:
            doc = fitz.open(pdf_path)
        except Exception as e:
            print(f"[ERR ] {tipo} {pdf_path.name}: {e}")
            continue

        out_path = out_dir / f"{tipo}__{pdf_path.stem}.txt"
        n_pages = min(doc.page_count, max_pages)
        with out_path.open("w", encoding="utf-8") as f:
            f.write(f"=== {descricao} ===\n")
            f.write(f"Arquivo: {pdf_path.relative_to(pdfs_root)}\n")
            f.write(f"Páginas totais: {doc.page_count}  |  Mostrando: {n_pages}\n")
            f.write(f"Creator: {doc.metadata.get('creator','')}\n")
            f.write(f"Producer: {doc.metadata.get('producer','')}\n")
            f.write(f"Versão PDF: {doc.metadata.get('format','')}\n")
            f.write("=" * 80 + "\n\n")

            for i in range(n_pages):
                page = doc[i]
                f.write(f"\n{'─' * 30} PÁGINA {i+1} ({page.rect.width:.0f}x{page.rect.height:.0f}) {'─' * 30}\n\n")
                texto = page.get_text("text") or ""
                f.write(texto)

                # Tabelas detectadas?
                try:
                    tabs = page.find_tables()
                    if tabs.tables:
                        f.write(f"\n\n[TABELAS DETECTADAS: {len(tabs.tables)} na página {i+1}]\n")
                        for j, tab in enumerate(tabs.tables):
                            f.write(f"\n--- Tabela {j+1} (linhas={tab.row_count}, colunas={tab.col_count}) ---\n")
                            try:
                                rows = tab.extract()
                                for row in rows[:5]:
                                    f.write(" | ".join(str(c or "") for c in row) + "\n")
                                if len(rows) > 5:
                                    f.write(f"...  (+{len(rows)-5} linhas)\n")
                            except Exception as e:
                                f.write(f"[erro extraindo tabela: {e}]\n")
                except Exception:
                    pass

        doc.close()
        print(f"[OK]   {tipo}: {out_path.name}  ({n_pages} pg)")


if __name__ == "__main__":
    pdfs_root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/pdfs_aneel")
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("explore_output")
    main(pdfs_root, out_dir)
