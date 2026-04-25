# Changelog

Histórico append-only de mudanças relevantes do projeto.
**Cada commit deve adicionar uma entrada nova ao topo.** Não sobrescrever entradas antigas — git log faz isso melhor; este arquivo serve para narrativa humana e contexto entre colaboradores.

Formato (Keep a Changelog adaptado): cada entrada começa com `## <hash curto> — <data> — <título>`, autor, e bullets curtos por área (`Added`, `Changed`, `Fixed`, `Removed`, `Notes`).

---

## (não commitado) — Fase 3: chunker 3-tier

**Autor:** Pedro (worktree `kind-panini-16a380`)

### Added
- `src/chunk.py` — chunker 3-tier data-driven:
  - Tier A: doc com `artigo` em `structure` → split por artigo (sub-split por § se >1500 tok), preâmbulos e anexos viram chunks próprios
  - Tier B: prosa sem estrutura jurídica + grande → janelas de ~500 tok com overlap de 50
  - Tier C: doc curto sem `artigo` → 1 chunk por doc
  - Hard cap de 1500 tokens via `_emit_or_split` (margem confortável p/ bge-m3 8k)
  - IDs únicos garantidos por índice posicional no slug
- CLI: `python -m src.chunk --in artifacts/parsed.jsonl --out artifacts/chunks.jsonl`

### Notes
- Validado em smoke (7000 docs do `parsed.jsonl` parcial): 39.682 chunks, 0 duplicados, p50=404 tok, max=1546 tok
- Aguardando Fase 2 (parser, em execução no worktree `naughty-tu-6a7a33`) terminar para rodar contra os 26.731 docs completos

---

## 4bfb66e — 2026-04-24 — Reorganiza repo para estrutura do pipeline RAG

### Changed
- Layout do repositório alinhado com a arquitetura completa (`src/`, `scripts/`, `data/`, `artifacts/`, `eval/`)

---

## 555373c — 2026-04-24 — Arquitetura completa do pipeline RAG

### Added
- `README.md` com arquitetura ponta-a-ponta das 8 fases (ingestão → parser → chunking → indexação → retrieval → geração → avaliação → serving)
- Stack tecnológica fixada (PyMuPDF, bge-m3, Qdrant, Claude Sonnet 4.6, Ragas)
- 3 caminhos de execução documentados (do zero, snapshot, smoke)
- Análise empírica do corpus (n=26.731) embasando decisões de chunking 3-tier

---

## 7f967a9 — 2026-04-24 — Primeiro commit: scraping (Fase 1)

### Added
- `scripts/download_aneel_pdfs.py` — downloader assíncrono com bypass Cloudflare via `curl_cffi`, retries com backoff, manifest JSONL idempotente
- `scripts/analyze_pdfs.py` — análise exploratória do corpus (PyMuPDF, sem OCR)
- 26.731 PDFs baixados (4,04 GB) em `data/pdfs_aneel/`
- `data/pdfs_aneel/_analysis.json` — saúde do corpus (100% text-native, 0 erros)
