# Handoff — Estado WIP entre colaboradores

Este arquivo descreve **o que está em andamento agora** — coisas que `git log` não captura: WIP em outros worktrees, decisões em aberto, bloqueadores, próximas etapas combinadas.

**Atualizar sempre que:** começar fase nova, pausar trabalho, tomar decisão arquitetural não-óbvia, identificar bloqueador, terminar fase.

**Convenção:** seções com `> @<nome>` indicam quem é o owner ativo. Marcar `(livre)` quando ninguém estiver mexendo.

---

## Em execução agora

### 🔨 Fase 4 — Indexação (código pronto, falta executar dense)
- **Owner:** @pedro (worktree `kind-panini-16a380`)
- **Status:** código completo (`src/index.py`, `docker-compose.yml`, `requirements.txt`). BM25 validado em smoke (1k chunks → 2.3MB pickle, query "tarifa de uso..." retorna top-3 coerentes)
- **Falta executar:**
  1. `pip install -r requirements.txt` (FlagEmbedding, qdrant-client, torch são pesados)
  2. `docker compose up -d` para subir Qdrant em localhost:6333
  3. `python -m src.index --chunks artifacts/chunks.jsonl --bm25-out artifacts/bm25_index.pkl` — vai gerar BM25 (~rápido) e indexar 160k chunks no Qdrant via bge-m3 (cara em CPU; ~30-60min em GPU consumer)
- **Decisão pendente:** rodar local ou publicar snapshot Qdrant + bm25.pkl como GitHub Release

---

## Fases concluídas (no master)

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
