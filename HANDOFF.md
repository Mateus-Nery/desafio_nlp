# Handoff — Estado WIP entre colaboradores

Este arquivo descreve **o que está em andamento agora** — coisas que `git log` não captura: WIP em outros worktrees, decisões em aberto, bloqueadores, próximas etapas combinadas.

**Atualizar sempre que:** começar fase nova, pausar trabalho, tomar decisão arquitetural não-óbvia, identificar bloqueador, terminar fase.

**Convenção:** seções com `> @<nome>` indicam quem é o owner ativo. Marcar `(livre)` quando ninguém estiver mexendo.

---

## Em execução agora

_(nada — Fase 4 e o Release público estão fechados. Próximas livres listadas abaixo.)_

> **Sugestão de divisão pra próxima sessão:**
> - **Pedro** pega **Fase 5 (Retrieval)** — ele escreveu o `src/index.py` então tem o contexto fresco dos named vectors no Qdrant + BM25 pickle. Ver "Para o Pedro começar a Fase 5" abaixo.
> - **Mateus** pode (a) começar a esboçar o **golden set da Fase 7** (não bloqueia ninguém) ou (b) criar o **Makefile** com `restore-artifacts` / `smoke` (cosmético).
>
> Coordenar pelo arquivo antes de começar — primeiro a anunciar pega.

---

## Para o Pedro começar a Fase 5 — Retrieval

Tudo o que você precisa pra começar sem revalidar nada:

### O que já existe pronto pra consumir

- **Coleção Qdrant `aneel_chunks` populada localmente** (após `docker compose up -d` + restore do snapshot). 160.267 pontos, named vectors `dense` (1024-dim cosine) + `sparse` (lexical_weights), payload com `text` cru + `chunk_id`/`doc_id`/`tipo_ato`/`year`/`tier`/`section_label`/`section_parent`/`title`/`url`/`char_start`/`char_end`/`n_chars`/`n_tokens_est`. Payload indexes em `tipo_ato`, `year`, `tier`, `doc_id` pra filtros eficientes.
- **`artifacts/bm25_index.pkl`** (244 MB) — gerado pelo `src/index.py`. Estrutura do pickle:
  ```python
  {
      "bm25": BM25Okapi(...),     # rank_bm25
      "chunk_ids": [...],          # lista paralela aos índices internos
      "payloads": [{...}, ...],    # mesma ordem, payload mínimo
      "tokenizer": "regex_word_lower",
      "n_chunks": 160267,
  }
  ```
  → Pra busca BM25: tokeniza query igual (`re.findall(r"\w+", q.lower())`), `bm25.get_scores(tokens)` retorna array, `argsort` pega top-N, mapeia índice pro `chunk_ids[i]` + `payloads[i]`.
- **Snapshot público** em [Release v0.4.0](https://github.com/Mateus-Nery/desafio_nlp/releases/tag/v0.4.0) caso você precise restaurar de novo.
- **Smoke de retrieval dense funcionando**: `scripts/smoke_query_qdrant.py` mostra como chamar `client.query_points(collection, query=dense, using="dense", limit=N)` (a API do qdrant-client 1.17 — `search` está deprecated).

### O que falta implementar (`src/retrieve.py`)

Pipeline esperado (do README):

```
query
  → embed (bge-m3)             # já temos a cara, ver smoke_query_qdrant.py
  → dense top-30 (Qdrant)      # client.query_points(..., using="dense")
  → sparse top-30 (Qdrant)     # client.query_points(..., using="sparse") ← com SparseVector
  → BM25 top-30 (pickle)       # in-memory
  → RRF fusion (k=60)          # score = Σ 1 / (k + rank_in_list)
  → bge-reranker-v2-m3         # roda local, ~50 ms/par em CPU
  → filtros opcionais por payload (tipo_ato, year, tier)
  → return top-K com scores + payloads
```

### Decisões em aberto que você precisa tomar

1. **Quantos retrievers somar no RRF?** O bge-m3 sparse é redundante com BM25? Vale rodar dense + BM25 (2 listas) ou dense + sparse + BM25 (3 listas)?
2. **Fusão dense+sparse "nativa do Qdrant" via prefetch + multi-stage** vs. fazer 2 queries separadas e combinar fora? A primeira é mais elegante mas custa entender a API; a segunda é mais transparente.
3. **Reranker em GPU ou CPU?** RTX 3050 está livre depois da indexação. CPU seria reproducible em qualquer máquina (sem GPU obrigatório no retrieval).
4. **Onde mora o BM25 pickle no serving?** Carregar 244 MB toda vez que o módulo importa (custa ~1-2 s de startup). Singleton/lazy load?

### Gotchas conhecidos

- **`encoding="utf-8"` em qualquer `path.open()` que toque jsonl** — Windows quebra em cp1252 com texto jurídico PT-BR (byte 0x81 frequente). Já corrigido em `chunk.py` (commit `0056f65`) e `index.py` (commit `2d3df09`).
- **`qdrant-client>=1.17` removeu `client.search()`** — usar `client.query_points()`. Aviso de versão incompatível com server 1.12.4 é benigno.
- **bge-m3 baixa do HF na 1ª query** (~2 GB, 2-3 min). Cache em `~/.cache/huggingface/`. Pré-baixar com `python -c "from FlagEmbedding import BGEM3FlagModel; BGEM3FlagModel('BAAI/bge-m3')"` se quiser evitar surpresa.
- **`use_fp16=True` só em GPU** — no CPU dá warning e roda fp32 mesmo. Já tratado no autodetect do `index.py`.

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

### ✅ Fase 3 — Chunker (executado e validado)
- **Owner:** @pedro (worktree `kind-panini-16a380`)
- `src/chunk.py` 3-tier data-driven com hard cap 1500 tok
- **Resultado no corpus completo:** 26.731 docs → 160.267 chunks em 8 s, 0 duplicados
- Distribuição: Tier A 98.709 (61,6%) / Tier B 50.052 (31,2%) / Tier C 11.506 (7,2%)
- Output: `artifacts/chunks.jsonl` (343 MB, gerado localmente, gitignored)
- Commits: `6dd84fa`, `11581e5`, `3d5e2c0`, `0056f65` (UTF-8 fix do Mateus)

---

## Próximas fases — não iniciadas

### Fase 5 — Retrieval (livre — sugerido pro Pedro)
- Hybrid: dense top-30 + (sparse top-30) + BM25 top-30 → RRF → bge-reranker-v2-m3 → top-10
- **Pré-requisitos: TUDO PRONTO.** Coleção Qdrant populada (local ou via restore do Release), bm25 pickle existe, bge-m3 já está no cache, qdrant-client/FlagEmbedding instalados.
- Ver "Para o Pedro começar a Fase 5" acima pra detalhes operacionais.

### Fase 6 — Geração (livre)
- Claude Sonnet 4.6 com prompt enforçando citações por chunk_id+url
- Depende de: Fase 5

### Fase 7 — Avaliação (livre)
- Ragas + golden set ~80 perguntas
- **Pode começar a esboçar o golden set em paralelo agora** (não bloqueia ninguém — só precisa do `chunks.jsonl` pra entender o vocabulário do corpus, e ele já existe localmente)

### Fase 8 — Serving (opcional, livre)
- FastAPI + Streamlit

### Makefile com targets `restore-artifacts` / `smoke` (livre, nice-to-have)
- README hoje documenta os comandos raw bash; um Makefile facilita a vida do examinador
- Targets sugeridos: `make restore-artifacts` (curl Release v0.4.0 + upload Qdrant), `make smoke` (roda `scripts/smoke_query_qdrant.py`), `make all` (combinação)

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
