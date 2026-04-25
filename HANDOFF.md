# Handoff — Estado WIP entre colaboradores

Este arquivo descreve **o que está em andamento agora** — coisas que `git log` não captura: WIP em outros worktrees, decisões em aberto, bloqueadores, próximas etapas combinadas.

**Atualizar sempre que:** começar fase nova, pausar trabalho, tomar decisão arquitetural não-óbvia, identificar bloqueador, terminar fase.

**Convenção:** seções com `> @<nome>` indicam quem é o owner ativo. Marcar `(livre)` quando ninguém estiver mexendo.

---

## Em execução agora

### 🏃 Fase 2 — Parser PyMuPDF
- **Worktree:** `naughty-tu-6a7a33`
- **Owner:** @amigo
- **Comando:** `python -m src.parse_pdfs --pdfs-root data/pdfs_aneel --out artifacts/parsed.jsonl --workers 8`
- **Status:** 16.471 / 26.731 docs (62%), ~14 min restantes (estimativa em 2026-04-24 ~22:00)
- **Output:** `artifacts/parsed.jsonl` (~26k linhas quando concluído)
- **Erros conhecidos:** warnings cosméticos `MuPDF error: format error: No common ancestor in structure tree` — ignoráveis, fail=0

### 🔨 Fase 3 — Chunker (código pronto, esperando input completo)
- **Worktree:** `kind-panini-16a380`
- **Owner:** @pedro
- **Status:** `src/chunk.py` implementado e validado em smoke (7k docs parciais → 39.682 chunks)
- **Próximo passo:** assim que Fase 2 terminar, rodar `python -m src.chunk --in artifacts/parsed.jsonl --out artifacts/chunks.jsonl`
- **Bloqueador:** depende de `parsed.jsonl` completo

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
