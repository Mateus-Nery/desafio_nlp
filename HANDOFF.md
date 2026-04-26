# Handoff — Estado WIP entre colaboradores

Este arquivo descreve **o que está em andamento agora** — coisas que `git log` não captura: WIP em outros worktrees, decisões em aberto, bloqueadores, próximas etapas combinadas.

**Atualizar sempre que:** começar fase nova, pausar trabalho, tomar decisão arquitetural não-óbvia, identificar bloqueador, terminar fase.

**Convenção:** seções com `> @<nome>` indicam quem é o owner ativo. Marcar `(livre)` quando ninguém estiver mexendo.

---

## Em execução agora

### 🔨 Fase 7 — Golden Set (avaliação)
- **Owner:** @mateus
- **Status:** em andamento — script `eval/generate_golden_set.py` a criar
- **Plano:** ~80 questões estratificadas (factual/conceptual/comparative/multi_hop/negative), geradas via Claude Sonnet 4.6 a partir de `artifacts/chunks.jsonl`, salvas em `eval/golden_set.jsonl`
- **Não depende de Fase 6** ✅ — Fase 6 já está pronta

---

## Fases concluídas (no master)

### ✅ Fase 6 — Geração (Claude Sonnet 4.6 + citações)
- **Owner:** @amigo (worktree `naughty-tu-6a7a33`)
- **Implementado:** `src/generate.py` — `generate(query, hits, client) → GenerationResult`
- **Decisões fechadas:**
  1. System prompt com `cache_control="ephemeral"` → prompt caching ativo em todas as chamadas
  2. Citações inline `[N]` extraídas por regex do texto gerado; `citations[]` estruturado separado
  3. Anti-alucinação hard: responde APENAS pelo contexto; fallback fixo "Não encontrei informação suficiente…"
  4. Streaming ativo por padrão no CLI; `--no-stream` / `--json` para batch/Ragas
  5. Interface Python limpa: reutilizável diretamente pela Fase 7 sem overhead de CLI
- **Makefile:** target `generate` adicionado (Caminho 2); `make generate QUERY="..."` com `.env` carregado
- **Smoke de lógica:** context block + extração de citações validados; retrieve CPU validado (3s, 5 hits coerentes para "TUSD")
- **Pendente:** smoke end-to-end com `ANTHROPIC_API_KEY` real (requer `.env` preenchido)

### ✅ Fase 5 — Retrieval híbrido (dense + BM25 + RRF + reranker)
- **Owner:** @pedro (worktree `kind-panini-16a380`)
- **Implementado:** `src/retrieve.py` — pipeline completo dense+BM25 → RRF(k=60) → CrossEncoderReranker → top-K
- **Decisões fechadas:**
  1. 2 listas (dense + BM25); sparse fora — dense já cobre polissemia, BM25 cobre jargão raro com IDF do corpus
  2. `ThreadPoolExecutor(max_workers=2)` queries em paralelo + RRF manual no cliente
  3. Autodetect CUDA → CPU; flag `--device {auto,cpu,cuda,mps}` pra forçar
  4. `load_bm25()`, `load_embedder()`, `load_reranker()` explícitos; sem singleton/cache mágico
- **Patch não previsto:** `FlagReranker` quebrado no `transformers 5.6+`; substituído por `CrossEncoderReranker` interno usando `AutoTokenizer(use_fast=True)` + `AutoModelForSequenceClassification`
- **Smoke validado (CPU, M-series):** query "TUSD" → top-1 REH 2022 Art. 13 (rerank=0.9976, era dense #4 antes do rerank). Latência 10.8s

### ✅ Makefile com atalhos pros 3 caminhos
- **Owner:** @mateus
- **Motivação:** o Claude do Pedro relatou atrito ao tentar o Caminho 2 sem `make`.
- **Targets implementados:**
  - **Caminho 2:** `restore-artifacts` (chain qdrant-up + download-artifacts + upload-snapshot), `smoke`
  - **Caminho 1:** `download`, `analyze`, `parse`, `chunk`, `index`, `all`
  - **Infra:** `qdrant-up`, `qdrant-down`, `clean-artifacts`, `clean-qdrant`, `clean-collection`
  - **Help:** `make` (sem argumentos) mostra menu organizado por categoria
- **Variáveis customizáveis** (`make X VAR=valor`): `PYTHON`, `QDRANT_URL`, `COLLECTION`, `RELEASE_TAG`, `BATCH_SIZE`, `CONCURRENCY`, `WORKERS`
- **Detecta OS** (Windows usa `.venv/Scripts/python.exe`, Linux/Mac usa `.venv/bin/python`)
- **Validado end-to-end:** `make restore-artifacts` rodou em 14s, restaurou os 160.267 pontos; `make smoke` retornou top-3 coerente em todas as 5 queries (mesmo resultado da sessão anterior).
- **README atualizado** com `make` em todos os 3 Caminhos + instalação do make em cada OS.

### ✅ Fase 4 — Indexação (executada e validada)
- **Owner:** @mateus (master, RTX 3050 6 GB Laptop)
- **Resultado:** 160.267 chunks indexados em Qdrant (dense bge-m3 1024-dim cosine + sparse lexical_weights, payload com texto cru e metadados). Coleção `aneel_chunks`, status green.
- **Tempo:** 130,1 min de dense+sparse a 20,5 ch/s (batch 80 em GPU). BM25 separadamente em 31s.
- **Artefatos do "Caminho 2"** (gerados em `artifacts/`, gitignored, prontos pra Release):
  - `qdrant_snapshot.tar` — 1,22 GB (sha256 `fc3ea6e810d691...`)
  - `bm25_index.pkl` — 244 MB (sha256 `fba807625c2367...`)
  - `manifest.json` — versões + hashes
- **Restore validado:** drop coleção → upload snapshot via API REST → 14s → 160.267 pontos restaurados → 5 queries dense retornam top-3 coerentes (REN 1000 Art 528 pra "prazo de ligação", NREH pra "TUSD", REN 1000 Art 655-B pra "microgeração", etc).
- **Bug corrigido durante a execução:** `src/index.py` lia `chunks.jsonl` sem `encoding="utf-8"` — mesmo bug do `chunk.py` (commit `0056f65`), agora também resolvido aqui.
- **GitHub Release publicada:** `v0.4.0` em https://github.com/Mateus-Nery/desafio_nlp/releases/tag/v0.4.0 — 3 assets públicos validados (HTTP 200, content-length bate, manifest baixado e parseado OK). Repo agora é público.

### ✅ Fase 1 — Ingestão (download + análise)
- 26.731 PDFs baixados (`data/pdfs_aneel/`), 100% text-native
- Commits: `7f967a9`, `555373c`, `4bfb66e`

### ✅ Fase 2 — Parser
- **Owner:** @amigo (worktree `naughty-tu-6a7a33`)
- 26.731/26.731 docs parseados em 29,7 min, 0 falhas
- 54,4 M tokens, 39.390 tabelas, 8.274 footnotes
- Output esperado: `artifacts/parsed.jsonl` (NOTA: arquivo é gerado localmente, não está no git)
- Commits: `bf9209e`, `d25643b`

### ✅ Fase 3 — Chunker (executado e validado)
- **Owner:** @pedro (worktree `kind-panini-16a380`)
- `src/chunk.py` 3-tier data-driven com hard cap 1500 tok
- **Resultado no corpus completo:** 26.731 docs → 160.267 chunks em 8 s, 0 duplicados
- Distribuição: Tier A 98.709 (61,6%) / Tier B 50.052 (31,2%) / Tier C 11.506 (7,2%)
- Output: `artifacts/chunks.jsonl` (343 MB, gerado localmente, gitignored)
- Commits: `6dd84fa`, `11581e5`, `3d5e2c0`, `0056f65` (UTF-8 fix do Mateus)

---

## Próximas fases — não iniciadas

### Fase 8 — Serving (opcional, livre)
- FastAPI + Streamlit


---

## Decisões em aberto

_(nenhuma no momento — adicionar aqui qualquer escolha arquitetural que precise alinhamento)_

---

## Convenções acordadas

- **Worktrees por fase:** cada Claude trabalha em seu worktree, faz merge para `master` quando uma fase fecha
- **Hard cap de chunk = 1500 tokens** (definido na Fase 3, alinha com bge-m3 8k de contexto com folga)
- **Token estimado = chars / 4** (mesma heurística usada por `parse_pdfs.n_tokens_est`)
- **URLs ANEEL:** padrão `https://www2.aneel.gov.br/cedoc/{filename}` (já embutido em `chunk.py`)
- **Atualizar `CHANGELOG.md` antes de cada commit** (entrada no topo)
- **Atualizar `HANDOFF.md` ao começar/pausar/terminar fase**
