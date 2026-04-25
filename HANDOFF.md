# Handoff — Estado WIP entre colaboradores

Este arquivo descreve **o que está em andamento agora** — coisas que `git log` não captura: WIP em outros worktrees, decisões em aberto, bloqueadores, próximas etapas combinadas.

**Atualizar sempre que:** começar fase nova, pausar trabalho, tomar decisão arquitetural não-óbvia, identificar bloqueador, terminar fase.

**Convenção:** seções com `> @<nome>` indicam quem é o owner ativo. Marcar `(livre)` quando ninguém estiver mexendo.

---

## Em execução agora

_(nada — Fase 4 e o Release público estão fechados. Próximo livre: Fase 5 — Retrieval, ou Makefile com target `restore-artifacts` que automatiza o "Caminho 2".)_

---

## Fases concluídas (no master)

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

### ✅ Fase 3 — Chunker (código pronto)
- **Owner:** @pedro (worktree `kind-panini-16a380`)
- `src/chunk.py` 3-tier data-driven com hard cap 1500 tok
- Validado em smoke (7k docs parciais → 39.682 chunks, 0 dups)
- **Próximo passo:** rodar contra `parsed.jsonl` completo: `python -m src.chunk --in artifacts/parsed.jsonl --out artifacts/chunks.jsonl`
- Commits: `6dd84fa`, `11581e5`, `3d5e2c0`

---

## Próximas fases — não iniciadas

### Fase 4 — Indexação (livre)
- Embeddings BAAI/bge-m3 (dense + sparse) + Qdrant (Docker) + BM25
- Pré-requisitos antes de codar: `docker-compose.yml` para Qdrant, `requirements.txt` atualizado com `sentence-transformers`, `qdrant-client`, `rank_bm25`, `FlagEmbedding`
- Quem pegar: comunica aqui antes

### Fase 5 — Retrieval (livre)
- Hybrid: dense top-30 + BM25 top-30 → RRF → bge-reranker-v2-m3 → top-10
- Depende de: Fase 4 concluída

### Fase 6 — Geração (livre)
- Claude Sonnet 4.6 com prompt enforçando citações por chunk_id+url
- Depende de: Fase 5

### Fase 7 — Avaliação (livre)
- Ragas + golden set ~80 perguntas
- Pode começar a esboçar o golden set em paralelo (não bloqueia)

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
