# Changelog

Histórico append-only de mudanças relevantes do projeto.
**Cada commit deve adicionar uma entrada nova ao topo.** Não sobrescrever entradas antigas — git log faz isso melhor; este arquivo serve para narrativa humana e contexto entre colaboradores.

Formato (Keep a Changelog adaptado): cada entrada começa com `## <hash curto> — <data> — <título>`, autor, e bullets curtos por área (`Added`, `Changed`, `Fixed`, `Removed`, `Notes`).

---

## (não commitado) — Atualização da documentação pós-Release v0.4.0 + handoff pra Fase 5

**Autor:** Mateus (master)

### Changed
- `README.md`:
  - Status no topo agora reflete Fases 1-4 concluídas + Release v0.4.0 publicada
  - Diagrama de arquitetura: Fase 2/3/4 marcadas com ✅, Fase 5 vira "🔨 próxima"
  - Seção "Como Rodar":
    - **Caminho 2 promovido a "recomendado"** — comandos raw bash com URL fixa da v0.4.0 (substitui `make restore-artifacts` que não existia)
    - **Caminho 1** reescrito com comandos reais (`python -m src.parse_pdfs`, `python -m src.chunk`, `python -m src.index`) substituindo `make download/parse/chunk/index`
    - **Caminho 3** agora aponta pro `scripts/smoke_query_qdrant.py` real (substitui `make smoke` que não existia)
    - Tabela de tempos da indexação ganhou linha "RTX 3050 Mobile" com tempo medido (130 min, batch 80)
    - Setup adicionou nota CUDA Windows (instalar torch antes via `--index-url cu124`)
    - Removida menção a `chunks.jsonl` na tabela do snapshot (ficou só no Caminho 1)
  - Seção "Fase 3" reescrita como ✅ concluída com resultados reais (160.267 chunks, distribuição por tier, schema do payload)
  - Seção "Fase 4" reescrita como ✅ concluída com resultados reais (130 min em RTX 3050, snapshot 1.22 GB, validação de smoke)
  - Adicionado bloco com comandos do snapshot (POST /snapshots, docker cp)
  - Seção "Replicabilidade": link direto pra Release v0.4.0, removida menção a `make smoke`
  - Tabela "Roadmap" atualizada: Fases 1-4 ✅, Snapshot+Release ✅, Fase 5 🔨, demais 📋, adicionada linha "Makefile (nice-to-have)"
- `HANDOFF.md`:
  - Adicionado bloco **"Para o Pedro começar a Fase 5"** com tudo que ele precisa pra começar sem revalidar nada: estrutura do BM25 pickle, payload da coleção Qdrant, decisões em aberto pra ele tomar (RRF de 2 ou 3 listas, fusão nativa Qdrant vs manual, reranker GPU vs CPU, BM25 lazy load), gotchas conhecidos (UTF-8 encoding, qdrant-client API mudou em 1.17, bge-m3 cache na 1ª query)
  - Fase 3 atualizada de "código pronto" pra "executado e validado" com resultado do corpus completo
  - Fase 4 removida da lista de "Próximas fases — não iniciadas" (já feita)
  - Adicionada linha "Makefile com targets restore-artifacts/smoke" como nice-to-have livre
  - Sugestão explícita de divisão: Pedro pega Fase 5; Mateus esboça golden set ou faz Makefile

### Notes
- Sem mudanças de código nesta entrada — só docs.
- Commits anteriores desta sessão (`2d3df09`, `a63aaf6`, `a357547`, `89c95c1`) entregaram a Fase 4 + Release; este apenas atualiza a narrativa pra examinador externo encontrar o caminho mais curto.

---

## a357547 — 2026-04-25 — Publicação do GitHub Release v0.4.0

**Autor:** Mateus (master)

### Added (release pública)
- Tag `v0.4.0` criada e enviada pro `origin`
- Release **v0.4.0 — Caminho 2: snapshot pré-indexado** publicada em
  https://github.com/Mateus-Nery/desafio_nlp/releases/tag/v0.4.0
- 3 assets públicos:
  - `qdrant_snapshot.tar` (1,22 GB) — coleção Qdrant `aneel_chunks` completa
  - `bm25_index.pkl` (244 MB) — índice BM25 Okapi serializado
  - `manifest.json` (1,8 KB) — versões + SHA-256 + estatísticas
- Repo `Mateus-Nery/desafio_nlp` tornado **público** (era privado, releases retornavam 404 sem auth)

### Validação
- HTTP HEAD em todos os 3 assets retorna 200 OK
- `manifest.json` baixado direto da URL pública e parseado: `n_chunks=160.267`, `n_docs=26.731`, embedding `BAAI/bge-m3` 1024-dim, Qdrant 1.12.4
- Content-Length do snapshot (1.221.437.952 B) bate com o local

### Notes
- README/Makefile ainda não foram atualizados com a URL fixa da v0.4.0 — o README descreve o "Caminho 2" genérico mas referencia `make restore-artifacts` que ainda não existe. Próxima sessão pode (a) adicionar Makefile com o target ou (b) atualizar o README com `curl` direto pra URL exata.

---

## 2d3df09 — 2026-04-25 — Execução da Fase 4: indexação completa, snapshot, smoke do restore

**Autor:** Mateus (master, RTX 3050 6 GB Laptop)

### Added (código)
- `scripts/smoke_query_qdrant.py` — smoke test pós-restore: carrega bge-m3, encoda 5 queries de domínio (TUSD, prazo ligação, GD solar, microgeração, penalidade), faz busca dense via `client.query_points` e mostra top-3 com payload (tipo_ato, year, doc_id, tier, section_label, url, trecho). Retorna 0 se a coleção tem 160k pontos e o pipeline responde — confirma que o "Caminho 2" funciona end-to-end.

### Changed
- `requirements.txt`: bloco de comentário documentando que no Windows `pip install torch` puro instala build CPU-only; para GPU NVIDIA é preciso instalar `torch` antes via `--index-url https://download.pytorch.org/whl/cu124`. Sem isso o passo 4 vai pra ~4-6 h em vez de ~2 h na 3050.
- `HANDOFF.md`: marca Fase 4 como concluída; adiciona entrada com tempos reais (130,1 min dense+sparse a 20,5 ch/s), tamanhos dos artefatos e resultado do smoke. Decisão arquitetural pendente resolvida — vamos publicar como GitHub Release.

### Fixed
- `src/index.py:60` (`iter_chunks`): faltava `encoding="utf-8"` no `path.open()`. No Windows o default é cp1252 e quebra em bytes ≥ 0x80 (frequente em texto jurídico PT-BR). Mesma família do bug que eu já tinha corrigido no `src/chunk.py` em `0056f65`. Sem o fix a Fase 4 não roda no Windows.

### Operacional (artefatos gerados, gitignored — irão para GitHub Release)
- `artifacts/bm25_index.pkl` — 244 MB, sha256 `fba807625c2367...`
- `artifacts/qdrant_snapshot.tar` — 1,22 GB, sha256 `fc3ea6e810d691...` (validado: SHA-256 do arquivo bate com o que o Qdrant reportou na criação)
- `artifacts/manifest.json` — schema_version 1, versões (bge-m3 commit `5617a9f6...`, Qdrant 1.12.4), hashes, n_chunks=160267, batch_size=80, throughput=20.5 ch/s, hint de restore via API REST

### Notes
- Coleção `aneel_chunks` no Qdrant local: 160.267 pontos, 316.427 indexed vectors (dense+sparse contam separados), 8 segments, status green, payload indexes em `tipo_ato`, `year`, `tier`, `doc_id`.
- **Smoke do restore validado:** drop coleção → upload snapshot via `POST /collections/{name}/snapshots/upload?priority=snapshot` → 14s → mesmas estatísticas → 5 queries dense retornam top-3 coerentes (ex: "prazo ligação" → REN 1000 Art 528 que diz "conexão... em até 10 dias úteis").
- Upload do GitHub Release **fica para próxima sessão** (combinado).

---

## 74fd136 — 2026-04-24 — README: deixa explícita a autossuficiência do Caminho 2

**Autor:** Pedro (worktree `kind-panini-16a380`)

### Changed
- `README.md` (seção "Caminho 2 — Bootstrap"): deixa explícito que o examinador NÃO precisa baixar PDFs nem rodar parser/chunker para usar o sistema. Substitui lista plana do snapshot por tabela com obrigatório/opcional, marca `chunks.jsonl` como opcional (só serve para re-indexar). Documenta que o `bge-m3` baixa automaticamente do HuggingFace na primeira query (~2 GB, ~2-3 min uma vez), com comando para pré-baixar. Esclarece que a autossuficiência vem da decisão de design da Fase 4 de armazenar o texto cru no payload do Qdrant.

---

## 17cca7e — 2026-04-24 — Fase 4: indexação (bge-m3 + Qdrant + BM25)

**Autor:** Pedro (worktree `kind-panini-16a380`)

### Added
- `src/index.py` — pipeline de indexação:
  - BM25 (`rank_bm25.BM25Okapi`) com tokenizer `\w+` lowercase, sem stopwords (texto jurídico precisa dos conectores; IDF cuida)
  - Dense + sparse via `FlagEmbedding.BGEM3FlagModel` num único forward (1024-dim cosine + lexical_weights)
  - Qdrant com named vectors (`dense` + `sparse`), payload indexes em `tipo_ato`, `year`, `tier`, `doc_id`
  - Autodetect device: CUDA → MPS (Apple) → CPU; fp16 quando não-CPU
  - Idempotência via UUID determinístico do `chunk_id` (uuid5)
- `docker-compose.yml` — Qdrant 1.12.4 com volume persistente, ports 6333/6334
- `requirements.txt` atualizado: FlagEmbedding, qdrant-client, rank_bm25, torch, anthropic, ragas, fastapi, streamlit
- CLI flags: `--skip-bm25`, `--skip-dense`, `--limit`, `--batch-size`

### Notes
- BM25 validado em smoke (1k chunks → 2.3MB pickle); query "tarifa de uso..." retorna top-3 coerentes
- Indexação dense pendente de execução (precisa Qdrant up + ~30-60min em GPU para 160k chunks)

---

## 0056f65 — 2026-04-24 — Fix encoding UTF-8 explícito no chunker (Windows)

**Autor:** Mateus (worktree `objective-blackburn-7f6ac0`)

### Fixed
- `src/chunk.py:398`: `process()` abria `parsed.jsonl` e `chunks.jsonl` sem `encoding="utf-8"`. Em Windows o default é cp1252, o que fazia `UnicodeDecodeError: 'charmap' codec can't decode byte 0x81` ao processar o corpus completo (texto jurídico em PT-BR tem byte 0x81 frequente). Funcionava no Mac/Linux por terem UTF-8 default. Agora explícito nos dois lados (leitura e escrita).

---

## d4e9fad — 2026-04-24 — Atualiza README pós-remoção do contexto_download

**Autor:** Mateus (worktree `objective-blackburn-7f6ac0`)

### Changed
- `README.md`: remove referência a `contexto_download_pdfs_aneel.md` da árvore de "Estrutura do Repositório" (arquivo deletado em `a71adf8`).

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
