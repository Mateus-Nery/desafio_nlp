# Changelog

Histórico append-only de mudanças relevantes do projeto.
**Cada commit deve adicionar uma entrada nova ao topo.** Não sobrescrever entradas antigas — git log faz isso melhor; este arquivo serve para narrativa humana e contexto entre colaboradores.

Formato (Keep a Changelog adaptado): cada entrada começa com `## <hash curto> — <data> — <título>`, autor, e bullets curtos por área (`Added`, `Changed`, `Fixed`, `Removed`, `Notes`).

---

## a71adf8 — 2026-04-24 — Remove `contexto_download_pdfs_aneel.md` obsoleto

**Autor:** Mateus (worktree `objective-blackburn-7f6ac0`)

### Removed
- `contexto_download_pdfs_aneel.md` — briefing pré-implementação da Fase 1, hoje redundante com o README e desatualizado em pontos críticos: stack HTTP (`httpx` vs `curl_cffi` real), 403 (listava como não-retryable, mas é transiente do Cloudflare), volume (~18.688 estimado vs 26.731 real) e estratégia geral (e5-large/BERTimbau/Estratégia B vs bge-m3/RRF/bge-reranker-v2). Conteúdo histórico fica preservado no git log do `7f967a9`.

---

## d1d1fb9 / d68f3ef — 2026-04-24 — Merges para master (Fase 2 + Fase 3)

**Autor:** Pedro

### Notes
- Merge `--no-ff` dos branches `claude/naughty-tu-6a7a33` (parser) e `claude/kind-panini-16a380` (chunker + protocolo) em `master`. Sem conflitos.

---

## d25643b — 2026-04-24 — Resultados finais do parser no corpus completo

**Autor:** Pedro (worktree `naughty-tu-6a7a33`, com Claude Opus 4.7)

### Added (docs)
- README com stats finais da Fase 2: 26.731/26.731 docs (100%), 0 falhas, 29,7 min (15 doc/s)
- 201,3 M chars, 54,4 M tokens estimados, 39.390 tabelas, 8.274 footnotes
- Taxas de extração de título: 100% REN/REA/REH, 99%+ PRT/NDSP/DSP

---

## bf9209e — 2026-04-24 — Fase 2: parser PDF→parsed.jsonl com extração estrutural

**Autor:** Pedro (worktree `naughty-tu-6a7a33`, com Claude Opus 4.7)

### Added
- `src/parse_pdfs.py` — pipeline completo PyMuPDF para os ~26.7k PDFs:
  - blocks-sort para multi-coluna (33% do corpus)
  - `find_tables()` com filtro semântico (descarta UTM/CEG)
  - `detect_repeated_lines` (≥3 págs) + regex hardcoded para boilerplate ANEEL
  - `fix_line_hyphenation` letra-letra (preserva IDs/datas)
  - `extract_footnotes` em campo separado, normalize NFC, collapse blank lines
- `scripts/explore_pdfs.py` — gera amostras dos 8 tipos principais em `explore_output/`
- Schema `parsed.jsonl`: doc_id, tipo_ato, year, title, ementa, processo, n_pages, n_chars, n_tokens_est, is_ocr_suspect, pdf_creator, text, structure[] (capitulo/artigo/paragrafo/anexo com offsets+parent), tables[] (Markdown), footnotes[]

---

## 6dd84fa — 2026-04-24 — Fase 3: chunker 3-tier

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

## 11581e5 — 2026-04-24 — Protocolo de coordenação entre colaboradores

**Autor:** Pedro (worktree `kind-panini-16a380`)

### Added
- `CLAUDE.md` — instruções obrigatórias para Claude (e humanos): ordem de leitura ao iniciar sessão (HANDOFF → CHANGELOG → git log), regras antes/depois de commit, convenções acordadas
- `CHANGELOG.md` — histórico append-only de mudanças (este arquivo)
- `HANDOFF.md` — estado VIVO do trabalho em andamento: owners por fase, decisões em aberto, bloqueadores

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
