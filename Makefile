# RAG sobre Legislação ANEEL - Makefile
#
# Atalhos pros comandos do README. Default `make` mostra a lista.
#
# Variáveis ajustáveis na linha de comando:
#   make index BATCH_SIZE=64
#   make restore-artifacts QDRANT_URL=http://qdrant:6333
#   make smoke PYTHON=python3
#
# Windows: requer Make (Git for Windows traz; senão `choco install make`).

# ──────────────────────────────────────────────────────────────────────────
# Variáveis
# ──────────────────────────────────────────────────────────────────────────

# Aceita .venv/ (poetry, uv) e venv/ (python -m venv). Prefere .venv/ se existir.
ifeq ($(OS),Windows_NT)
    PYTHON ?= $(if $(wildcard .venv/Scripts/python.exe),.venv/Scripts/python.exe,venv/Scripts/python.exe)
else
    PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,venv/bin/python)
endif

QDRANT_URL  ?= http://localhost:6333
COLLECTION  ?= aneel_chunks
RELEASE_TAG ?= v0.4.0
RELEASE_URL := https://github.com/Mateus-Nery/desafio_nlp/releases/download/$(RELEASE_TAG)
ARTIFACTS   := artifacts
PDFS_DIR    := data/pdfs_aneel
JSON_DIR    := data/dados_grupo_estudos

SNAPSHOT    := $(ARTIFACTS)/qdrant_snapshot.tar
BM25        := $(ARTIFACTS)/bm25_index.pkl
MANIFEST    := $(ARTIFACTS)/manifest.json
PARSED      := $(ARTIFACTS)/parsed.jsonl
CHUNKS      := $(ARTIFACTS)/chunks.jsonl

CONCURRENCY ?= 8
WORKERS     ?= 8
BATCH_SIZE  ?= 80

.DEFAULT_GOAL := help

# ──────────────────────────────────────────────────────────────────────────
# Help (default)
# ──────────────────────────────────────────────────────────────────────────

.PHONY: help
help: ## Mostra esta ajuda
	@echo ""
	@echo "RAG ANEEL - atalhos do Makefile"
	@echo ""
	@echo "  Caminho 2 (snapshot pre-construido, recomendado p/ examinador):"
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z][a-zA-Z0-9_-]*:.*## \[2\]/ {desc=$$2; sub(/^\[2\] /, "", desc); printf "    \033[36m%-20s\033[0m %s\n", $$1, desc}' $(MAKEFILE_LIST)
	@echo ""
	@echo "  Caminho 1 (pipeline completo do zero):"
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z][a-zA-Z0-9_-]*:.*## \[1\]/ {desc=$$2; sub(/^\[1\] /, "", desc); printf "    \033[36m%-20s\033[0m %s\n", $$1, desc}' $(MAKEFILE_LIST)
	@echo ""
	@echo "  Infra & utilitarios:"
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z][a-zA-Z0-9_-]*:.*## \[u\]/ {desc=$$2; sub(/^\[u\] /, "", desc); printf "    \033[36m%-20s\033[0m %s\n", $$1, desc}' $(MAKEFILE_LIST)
	@echo ""
	@echo "  Variaveis (override na linha de comando):"
	@echo "    PYTHON=$(PYTHON)"
	@echo "    QDRANT_URL=$(QDRANT_URL)"
	@echo "    RELEASE_TAG=$(RELEASE_TAG)"
	@echo "    BATCH_SIZE=$(BATCH_SIZE)  CONCURRENCY=$(CONCURRENCY)  WORKERS=$(WORKERS)"
	@echo ""

# ──────────────────────────────────────────────────────────────────────────
# Caminho 2 - bootstrap via Release pré-construída (recomendado)
# ──────────────────────────────────────────────────────────────────────────

.PHONY: qdrant-up
qdrant-up: ## [u] Sobe Qdrant via docker compose e espera o daemon aceitar requests
	docker compose up -d
	@echo ">>> Aguardando Qdrant aceitar requests..."
	@n=0; until curl -sSf $(QDRANT_URL)/healthz >/dev/null 2>&1; do \
		n=$$((n+1)); \
		if [ $$n -gt 60 ]; then echo "Qdrant nao respondeu em 120s. Verifique 'docker logs aneel-qdrant'."; exit 1; fi; \
		sleep 2; \
	done
	@echo "Qdrant pronto em $(QDRANT_URL)"

.PHONY: qdrant-down
qdrant-down: ## [u] Para Qdrant (mantem volume com dados indexados)
	docker compose down

.PHONY: download-artifacts
download-artifacts: $(SNAPSHOT) $(BM25) $(MANIFEST) ## [2] Baixa snapshot + bm25 + manifest da Release v0.4.0

$(ARTIFACTS):
	@mkdir -p $(ARTIFACTS)

$(SNAPSHOT): | $(ARTIFACTS)
	@echo ">>> Baixando qdrant_snapshot.tar (~1.22 GB)..."
	curl -L --fail --progress-bar -o $@ $(RELEASE_URL)/qdrant_snapshot.tar

$(BM25): | $(ARTIFACTS)
	@echo ">>> Baixando bm25_index.pkl (~244 MB)..."
	curl -L --fail --progress-bar -o $@ $(RELEASE_URL)/bm25_index.pkl

$(MANIFEST): | $(ARTIFACTS)
	@echo ">>> Baixando manifest.json..."
	curl -L --fail --progress-bar -o $@ $(RELEASE_URL)/manifest.json

.PHONY: upload-snapshot
upload-snapshot: ## [2] Envia snapshot baixado pro Qdrant local (~14s, idempotente)
	@test -f $(SNAPSHOT) || (echo "ERRO: $(SNAPSHOT) nao existe. Rode 'make download-artifacts' antes." && exit 1)
	@echo ">>> Restaurando snapshot na colecao '$(COLLECTION)'..."
	curl -X POST '$(QDRANT_URL)/collections/$(COLLECTION)/snapshots/upload?priority=snapshot' \
	     -F snapshot=@$(SNAPSHOT)
	@echo ""
	@echo ">>> Validando colecao restaurada..."
	@curl -sS $(QDRANT_URL)/collections/$(COLLECTION) | $(PYTHON) -c "import sys,json; d=json.load(sys.stdin)['result']; print(f'  status:  {d[\"status\"]}'); print(f'  pontos:  {d[\"points_count\"]:,}'); print(f'  indexed: {d[\"indexed_vectors_count\"]:,}')"

.PHONY: restore-artifacts
restore-artifacts: ## [2] Caminho 2 completo: sobe Qdrant, baixa artefatos, restaura snapshot
	@$(MAKE) qdrant-up
	@$(MAKE) download-artifacts
	@$(MAKE) upload-snapshot
	@echo ""
	@echo "OK. Proximo passo: 'make smoke' para validar que retrieval responde."

# ──────────────────────────────────────────────────────────────────────────
# Smoke / sanity
# ──────────────────────────────────────────────────────────────────────────

.PHONY: smoke
smoke: ## [2] Roda smoke_query_qdrant.py (5 queries dense, valida pipeline)
	$(PYTHON) scripts/smoke_query_qdrant.py

QUERY ?= o que é TUSD e como ela é calculada?

.PHONY: generate
generate: ## [2] Fase 6 - query interativa com Claude Sonnet 4.6 (requer .env com ANTHROPIC_API_KEY)
	@test -f .env || (echo "ERRO: crie .env a partir de .env.example e preencha ANTHROPIC_API_KEY" && exit 1)
	@export $$(grep -v '^#' .env | xargs) && \
	  $(PYTHON) -m src.generate \
	    --query "$(QUERY)" \
	    --bm25-path $(BM25) \
	    --qdrant-url $(QDRANT_URL) \
	    --top-k 10

# ──────────────────────────────────────────────────────────────────────────
# Caminho 1 - pipeline completo do zero
# ──────────────────────────────────────────────────────────────────────────

.PHONY: download
download: ## [1] Fase 1 - baixa 26.731 PDFs da ANEEL (~13 min)
	$(PYTHON) scripts/download_aneel_pdfs.py \
	  --json-dir $(JSON_DIR) \
	  --output-dir $(PDFS_DIR) \
	  --concurrency $(CONCURRENCY)

.PHONY: analyze
analyze: ## [1] Analise exploratoria do corpus (PyMuPDF, sem OCR)
	$(PYTHON) scripts/analyze_pdfs.py \
	  --pdfs-dir $(PDFS_DIR) \
	  --report-json $(PDFS_DIR)/_analysis.json

.PHONY: parse
parse: ## [1] Fase 2 - extrai texto dos PDFs (~30 min em 8 cores)
	$(PYTHON) -m src.parse_pdfs \
	  --pdfs-root $(PDFS_DIR) \
	  --out $(PARSED) \
	  --workers $(WORKERS)

.PHONY: chunk
chunk: ## [1] Fase 3 - chunking 3-tier (~10s)
	$(PYTHON) -m src.chunk \
	  --in $(PARSED) \
	  --out $(CHUNKS)

.PHONY: index
index: ## [1] Fase 4 - embeddings bge-m3 + Qdrant + BM25 (~2h em RTX 3050)
	$(PYTHON) -m src.index \
	  --chunks $(CHUNKS) \
	  --bm25-out $(BM25) \
	  --batch-size $(BATCH_SIZE)

.PHONY: all
all: download parse chunk index ## [1] Roda Fases 1-4 em sequencia (varias horas)

# ──────────────────────────────────────────────────────────────────────────
# Limpeza
# ──────────────────────────────────────────────────────────────────────────

.PHONY: clean-artifacts
clean-artifacts: ## [u] Apaga arquivos em artifacts/ (mantem PDFs e Qdrant volume)
	rm -rf $(ARTIFACTS)/*

.PHONY: clean-qdrant
clean-qdrant: ## [u] Para Qdrant E APAGA o volume (perde tudo indexado)
	docker compose down -v

.PHONY: clean-collection
clean-collection: ## [u] Apaga so a colecao aneel_chunks no Qdrant (mantem volume)
	curl -sS -X DELETE $(QDRANT_URL)/collections/$(COLLECTION)
	@echo ""
