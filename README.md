# RAG sobre Legislação ANEEL

Sistema RAG (Retrieval-Augmented Generation) sobre a biblioteca legislativa da
ANEEL — Agência Nacional de Energia Elétrica. Cobre **26.731 PDFs** dos anos
2016, 2021 e 2022, totalizando ~117 mil páginas e ~54 milhões de tokens de
legislação do setor elétrico brasileiro.

Snapshot pré-indexado disponível em
[GitHub Release v0.4.0](https://github.com/Mateus-Nery/desafio_nlp/releases/tag/v0.4.0)
— restaurável em ~14 s via `make restore-artifacts`.

---

## Sumário

1. [Arquitetura](#arquitetura)
2. [Stack Tecnológica](#stack-tecnológica)
3. [Estrutura do Repositório](#estrutura-do-repositório)
4. [Como Rodar](#como-rodar)
5. [Pipeline](#pipeline)
6. [Avaliação](#avaliação)
7. [Análise do Corpus](#análise-do-corpus)
8. [Decisões de Arquitetura](#decisões-de-arquitetura)

---

## Arquitetura

```
                    ┌─────────────────┐
                    │   3 JSONs ANEEL │
                    └────────┬────────┘
                             │  download_aneel_pdfs.py (curl_cffi + asyncio)
                             ▼
                    ┌─────────────────┐
                    │   26.731 PDFs   │
                    └────────┬────────┘
                             │  parse_pdfs.py (PyMuPDF + cleaning)
                             ▼
                    ┌─────────────────┐
                    │  parsed.jsonl   │  54,4 M tokens, 39.390 tabelas
                    └────────┬────────┘
                             │  chunk.py (3-tier data-driven, hard cap 1500 tok)
                             ▼
                    ┌─────────────────┐
                    │ 160.267 chunks  │  Tier A 61% / B 31% / C 7%
                    └────────┬────────┘
                             │  index.py (bge-m3 + Qdrant + BM25)
                             ▼
                    ┌─────────────────┐
                    │  Qdrant + BM25  │
                    └────────┬────────┘
                             │
                             ▼
        query  ──▶  embed (bge-m3 1024-dim)
                             │
                             ├──▶ Qdrant dense top-30 ┐
                             │                        │  RRF (k=60)
                             └──▶ BM25 top-30  ───────┘   (paralelo)
                                              │
                                              ▼
                             bge-reranker-v2-m3  →  top-10
                                              │
                                              ▼
                             Claude Sonnet 4.6 (prompt caching, citações [N])
                                              │
                                              ▼
                                          resposta
```

---

## Stack Tecnológica

| Camada | Tecnologia | Justificativa |
|---|---|---|
| Linguagem | Python 3.11+ | Ecossistema NLP/RAG |
| HTTP (download) | `curl_cffi` | TLS impersonation Chrome para bypass de Cloudflare |
| PDF parsing | `PyMuPDF` (`fitz`) | Validado em 26.731 PDFs sem erros, suporta `find_tables()` |
| Embeddings | `BAAI/bge-m3` (1024-dim) | SOTA multilingual; gera dense+sparse num passe; forte em PT-BR |
| Vector store | **Qdrant** (Docker) | Hybrid search nativo, filtros por payload, snapshots restauráveis |
| Sparse search | `rank_bm25` | Pure Python, IDF do corpus complementa dense |
| Reranker | `BAAI/bge-reranker-v2-m3` | Cross-encoder padrão da indústria |
| LLM gerador | **Claude Sonnet 4.6** (API) | PT-BR jurídico, citações confiáveis, prompt caching |
| Avaliação | golden set + LLM-as-judge (Claude Haiku) | hit@k/MRR + faithfulness/answer_relevance |
| Infra | Docker Compose | Sobe Qdrant com 1 comando |

---

## Estrutura do Repositório

```
desafio_nlp/
├── README.md
├── requirements.txt              ← deps pinadas
├── docker-compose.yml            ← Qdrant
├── Makefile                      ← orquestração (3 caminhos)
├── .env.example                  ← template de variáveis
│
├── data/                         (gitignored)
│   ├── dados_grupo_estudos/      ← 3 JSONs ANEEL (entrada)
│   └── pdfs_aneel/               ← 26.731 PDFs + manifests
│
├── artifacts/                    (gitignored, publicado via Release)
│   ├── parsed.jsonl              ← saída do parser
│   ├── chunks.jsonl              ← saída do chunker
│   ├── qdrant_snapshot.tar       ← snapshot do Qdrant
│   ├── bm25_index.pkl            ← BM25 serializado
│   └── manifest.json             ← versões + hashes
│
├── src/
│   ├── parse_pdfs.py             ← PyMuPDF + cleaning + estrutura
│   ├── chunk.py                  ← chunking 3-tier
│   ├── index.py                  ← embeddings + Qdrant + BM25
│   ├── retrieve.py               ← hybrid + RRF + rerank
│   └── generate.py               ← Claude + prompt + citações
│
├── eval/
│   ├── generate_golden_set.py    ← gera golden set via Claude
│   ├── evaluate.py               ← métricas hit@k/MRR + LLM eval
│   └── golden_set.jsonl          ← 79 questões estratificadas
│
└── scripts/
    ├── download_aneel_pdfs.py    ← download dos PDFs
    ├── analyze_pdfs.py           ← análise exploratória do corpus
    ├── explore_pdfs.py           ← extrai amostras p/ inspeção
    └── smoke_query_qdrant.py     ← smoke test rápido
```

---

## Como Rodar

### Pré-requisitos

- Python 3.11+ (3.10 também funciona)
- Docker + Docker Compose
- **GNU Make** (atalhos):
  - Linux: já vem (`apt install make` se faltar)
  - macOS: `brew install make` (ou já vem com Xcode CLT)
  - Windows: `choco install make` / `scoop install make` / `mingw32-make` (Git for Windows)
- (opcional) GPU CUDA ou Apple MPS — autodetectada
- ~5 GB livres para o Caminho 1 (modelo bge-m3 + snapshot + Qdrant volume)
- ~10 GB livres para o Caminho 2 (acima + 4 GB de PDFs)

### Setup inicial

```bash
git clone https://github.com/Mateus-Nery/desafio_nlp.git
cd desafio_nlp

python -m venv .venv
source .venv/bin/activate         # Windows: .venv\Scripts\activate

# (opcional, recomendado em Windows com GPU NVIDIA — sem isso o pip vem com torch CPU-only)
pip install torch --index-url https://download.pytorch.org/whl/cu124

pip install -r requirements.txt

# Chave Claude (necessária só para geração)
cp .env.example .env
# editar .env: ANTHROPIC_API_KEY=sk-ant-...

docker compose up -d
```

### Caminho 1 — Snapshot pré-construído ⚡ (recomendado)

Restaura os artefatos pré-computados publicados como
[GitHub Release v0.4.0](https://github.com/Mateus-Nery/desafio_nlp/releases/tag/v0.4.0).
**Não precisa baixar os 4 GB de PDFs nem rodar parser/chunker.**

```bash
make restore-artifacts   # sobe Qdrant, baixa snapshot+bm25+manifest, restaura (~5-10 min)
make smoke               # 5 queries dense, valida que retrieval responde
```

Pronto — agora dá pra fazer queries (ver [Fazendo uma query](#fazendo-uma-query)
abaixo). `make evaluate` reporta as métricas em 79 questões do golden set
(opcional, ~3 min, sem custo de API).

Equivalente raw bash (sem make):

```bash
mkdir -p artifacts
docker compose up -d
curl -L -o artifacts/qdrant_snapshot.tar \
  https://github.com/Mateus-Nery/desafio_nlp/releases/download/v0.4.0/qdrant_snapshot.tar
curl -L -o artifacts/bm25_index.pkl \
  https://github.com/Mateus-Nery/desafio_nlp/releases/download/v0.4.0/bm25_index.pkl
curl -L -o artifacts/manifest.json \
  https://github.com/Mateus-Nery/desafio_nlp/releases/download/v0.4.0/manifest.json
curl -X POST 'http://localhost:6333/collections/aneel_chunks/snapshots/upload?priority=snapshot' \
     -F snapshot=@artifacts/qdrant_snapshot.tar
python scripts/smoke_query_qdrant.py
```

**Conteúdo da Release v0.4.0:**

| Arquivo | Tamanho | Para quê |
|---|---|---|
| `qdrant_snapshot.tar` | 1,22 GB | Coleção `aneel_chunks` com 160.267 pontos: dense (bge-m3 1024-dim cosine) + sparse (lexical_weights), payload com texto cru e metadados |
| `bm25_index.pkl` | 244 MB | Índice BM25 Okapi serializado, tokenizer regex `\w+` lowercase |
| `manifest.json` | 1,8 KB | Versões dos modelos + SHA-256 dos artefatos |

**Por que é autossuficiente:** o `src/index.py` armazena o texto cru de cada
chunk dentro do payload do Qdrant (e do BM25 pickle). Quando o retrieval
traz um chunk, ele já vem com `text`, `url`, `tipo_ato`, etc. inline — sem
lookup posterior em arquivos locais.

**O que ainda baixa automaticamente na primeira query** (uma vez só):
`BAAI/bge-m3` (~2 GB) do HuggingFace, cacheado em `~/.cache/huggingface/`.
Para acelerar, pode ser pré-baixado:
```bash
python -c "from FlagEmbedding import BGEM3FlagModel; BGEM3FlagModel('BAAI/bge-m3')"
```

### Caminho 2 — Pipeline completo do zero

Reproduz **toda a indexação** a partir dos 3 JSONs ANEEL.

```bash
make all   # download + parse + chunk + index, em sequência (~3-4 horas)

# ou fase por fase, pra checar cada saída:
make download    # baixa 26.731 PDFs (~13 min)
make analyze     # análise exploratória do corpus (opcional)
make parse       # extrai texto dos PDFs (~30 min em 8 cores)
make chunk       # chunking 3-tier (~10 s)
make index       # embeddings + Qdrant + BM25 (~2 h em RTX 3050)
```

**Tempos esperados na indexação** (parte mais cara — 160.267 chunks bge-m3):

| Hardware | Tempo |
|---|---|
| GPU desktop (RTX 3080+) | 30-60 min |
| GPU A100 | 10-15 min |
| **GPU laptop (RTX 3050 6 GB)** | **~130 min** (medido) |
| CPU 8-core (batched, FP16) | 1-3 h |
| CPU 4-core | 4-6 h |

### Fazendo uma query

Pré-requisito: Caminho 1 ou 2 já populou o Qdrant (`make restore-artifacts` ou
`make all`).

#### Modo 1 — Resposta completa com citações (Claude Sonnet 4.6)

Requer `ANTHROPIC_API_KEY` no `.env`. Roda o pipeline inteiro (retrieval +
rerank + LLM) e devolve uma resposta em PT-BR com citações inline `[N]`.

```bash
make generate QUERY="o que é TUSD e como ela é calculada?"
```

Equivalente raw (sem make):

```bash
python -m src.generate --query "o que é TUSD e como ela é calculada?"
```

**Output esperado** (streaming, depois fontes e métricas):

```
A TUSD (Tarifa de Uso do Sistema de Distribuição) é a tarifa que remunera
o uso da rede de distribuição pelos consumidores [1]. Ela é calculada com
base na potência contratada e no nível de tensão de fornecimento, conforme
estabelece o Art. 13 da REH 2.914/2021 [2].

────────────────────────────────────────────────────────────────────────
FONTES
────────────────────────────────────────────────────────────────────────
[1] RESOLUÇÃO NORMATIVA ANEEL Nº 1.000 — Art. 5º
    https://www2.aneel.gov.br/cedoc/ren20221000.pdf
[2] RESOLUÇÃO HOMOLOGATÓRIA Nº 2.914 — Art. 13
    https://www2.aneel.gov.br/cedoc/reh20212914.pdf
────────────────────────────────────────────────────────────────────────
Tokens: 4280 entrada (312 cache) · 310 saída · 3214 ms
```

Flags úteis:

```bash
# Filtrar por tipo de ato e ano
python -m src.generate --query "..." --tipo-ato ren --year 2022 --top-k 8

# Saída JSON estruturada (sem streaming)
python -m src.generate --query "..." --json --no-stream > result.json

# Sem reranker (mais rápido em CPU, qualidade um pouco menor)
python -m src.generate --query "..." --no-rerank
```

#### Modo 2 — Apenas retrieval (sem custo de API)

Mostra os top-K chunks recuperados com scores, sem chamar Claude. Útil pra
debug e pra usuários sem chave Anthropic.

```bash
python -m src.retrieve --query "tarifa de uso do sistema de distribuição"
```

**Output esperado:**

```
10 resultados em 2922ms para query: 'tarifa de uso do sistema de distribuição'

1. [rrf=0.0325  dense=#1  bm25=#2]  NREN 2016  tier=A
   ANEXO II  |  doc=2016/nren2016704
   url: https://www2.aneel.gov.br/cedoc/nren2016704.pdf
   ...respectivas Tarifas de Uso do Sistema de Distribuição (TUSD) vigentes
   na DRP, conforme a fórmula abaixo: CSDDRP = ∑(MUSDP × TUSDP + ...

2. [rrf=0.0257  dense=#22  bm25=#14]  NREH 2021  tier=B
   ...
```

Flags úteis:

```bash
python -m src.retrieve --query "..." --top-k 5 --tipo-ato ren --year 2022
python -m src.retrieve --query "..." --no-rerank --json
python -m src.retrieve --query "..." --device cpu   # força CPU (default: autodetect)
```

#### Modo 3 — Smoke test (validação rápida)

Roda 5 queries pré-definidas para validar que o pipeline está respondendo.
Não usa LLM nem reranker — só dense search no Qdrant.

```bash
make smoke
```

Equivale a `python scripts/smoke_query_qdrant.py`. Saída esperada: top-3
coerente para "TUSD", "prazo de ligação", "microgeração distribuída",
"penalidade por descumprimento" e "geração distribuída". Tempo total: ~20 s.

---

## Pipeline

### Ingestão (download)

Script: [`scripts/download_aneel_pdfs.py`](scripts/download_aneel_pdfs.py)

Lê 3 JSONs de metadados ANEEL, deduplica URLs, baixa todos os PDFs com:

- Concorrência controlada (asyncio + semaphore, default 8)
- Retries com backoff exponencial e jitter (até 5×)
- Validação de magic number (`%PDF-`) e SHA-256
- Manifest JSONL para retomada idempotente
- Falhas separadas em `_failures.jsonl` para reexecução
- **Bypass Cloudflare** via `curl_cffi` com TLS impersonation Chrome

> **Por que `curl_cffi`?** O servidor `www2.aneel.gov.br/cedoc/` está atrás de
> Cloudflare Bot Management, que bloqueia requisições com base em fingerprint
> TLS (JA3) — `httpx` puro retorna 403 mesmo com User-Agent de browser.
> O `curl_cffi` reproduz o handshake TLS do Chrome real e passa pelo bloqueio.

**Resultado:** 26.731 / 26.768 URLs únicas (99,82%), 4,04 GB, ~13 min @ 30-40 PDFs/s.

```bash
python scripts/download_aneel_pdfs.py \
    --json-dir data/dados_grupo_estudos \
    --output-dir data/pdfs_aneel \
    --concurrency 8
```

Flags úteis: `--max-retries`, `--dry-run`, `--only-year`, `--max-downloads`,
`--sample-fraction`, `--sample-seed`. Saídas: `_manifest.jsonl`,
`_failures.jsonl`, `_errors.log`, `_summary.json`.

---

### Parser

Módulo: `src/parse_pdfs.py`

Lê todos os PDFs em `data/pdfs_aneel/` e gera `artifacts/parsed.jsonl` —
1 linha JSON por documento, com texto limpo, estrutura hierárquica, tabelas
e footnotes.

**Schema:**

```json
{
  "doc_id": "2022/ren20221008",
  "tipo_ato": "ren",
  "year": 2022,
  "title": "RESOLUÇÃO NORMATIVA ANEEL Nº 1.008, DE 15 DE MARÇO DE 2022",
  "ementa": "Dispõe sobre a Conta Escassez Hídrica...",
  "processo": "48500.006312/2021-55",
  "n_pages": 22, "n_chars": 12090, "n_tokens_est": 3267,
  "is_ocr_suspect": false,
  "text": "...full cleaned text...",
  "structure": [
    {"type": "capitulo", "label": "CAPÍTULO I", "title": "DISPOSIÇÕES PRELIMINARES",
     "start": 762, "end": 1313, "parent": ""},
    {"type": "artigo", "label": "Art. 1º", "start": 798, "end": 1313, "parent": ""},
    {"type": "paragrafo", "label": "§ 1º", "start": 891, "end": 950, "parent": "Art. 1º"}
  ],
  "tables": [{"id": "p2t1", "page": 2, "markdown": "...", "rows": 7, "cols": 4}],
  "footnotes": [{"num": 1, "text": "..."}]
}
```

**Pipeline de cleaning:**

```
PDF
 ├─► page.get_text("blocks", sort=True)   ← robusto a multi-coluna (33% do corpus)
 ├─► page.find_tables() + heurística      ← descarta tabelas de coordenadas UTM/CEG,
 │                                          mantém tabelas de prosa
 ├─► detect_repeated_lines (≥3 págs)       ← header/footer dinâmico por doc
 ├─► remove_boilerplate (regex hardcoded)  ← cabeçalhos ANEEL, "P. X Nota Técnica",
 │                                          divisores, retificações, carimbos
 ├─► fix_line_hyphenation                  ← "autori-\nzação" → "autorização"
 │                                          (preserva IDs como "2021-55")
 ├─► join_lone_paragraph_numbers           ← Voto/Nota: "12.\ntexto" → "12. texto"
 ├─► extract_footnotes                     ← rodapés saem do texto principal
 ├─► normalize_chars                       ← NFC, NBSP→space, aspas curvas→retas
 └─► collapse_blank_lines                  ← \n\n\n+ → \n\n
```

**Extração estrutural:** regex captura marcadores e calcula offsets `(start, end)`.
Hierarquia: **Anexo > Capítulo > Seção > Artigo > §**. Habilita o chunking
**Tier A** sem reparsing.

```bash
python -m src.parse_pdfs \
  --pdfs-root data/pdfs_aneel \
  --out artifacts/parsed.jsonl \
  --workers 8
```

Flags: `--samples-only` (smoke), `--resume` (idempotente), `--workers`.

**Resultado em corpus completo (n=26.731):** 29,7 min @ 8 workers, **15,0 doc/s**,
**0 falhas**, 54,4 M tokens, 39.390 tabelas, 8.274 footnotes. Title extraction
≥97,9% nos atos principais (REN/REA/REH/PRT/NDSP/DSP/AREA).

---

### Chunking

Módulo: `src/chunk.py`

Estratégia em **3 tiers** baseada na análise empírica de heterogeneidade
do corpus.

**Tier A — denso jurídico (REN, REH, RES, NREH, NDSP, INA)**
- Split por regex `r'^\s*Art\.?\s*\d+'` + `Anexo` separado
- Sub-split por `§` se artigo > 1500 tokens
- Overlap zero (artigos são unidades naturais)

**Tier B — médio, prosa decisória (AREA, ADSP, APRT, REA, PRT)**
- Split por parágrafo, merge até ~500 tokens
- Overlap 50 tokens

**Tier C — curto (DSP, ECP, ECT, EDT, AVS, ACP, ATS)**
- PDF inteiro = 1 chunk (quase todos < 2k tokens)

**Resultado em corpus completo:** 26.731 docs → **160.267 chunks** em 8 s,
0 duplicados, hard cap de 1500 tokens respeitado.
Distribuição: Tier A 98.709 (61,6%) / Tier B 50.052 (31,2%) / Tier C 11.506 (7,2%).

**Schema do chunk:**

```json
{
  "chunk_id": "2022/ren20221008__art1",
  "doc_id": "2022/ren20221008",
  "tipo_ato": "ren",
  "year": 2022,
  "tier": "A",
  "section_type": "artigo",
  "section_label": "Art. 1º",
  "section_parent": "",
  "title": "RESOLUÇÃO NORMATIVA ANEEL Nº 1.008, DE 15 DE MARÇO DE 2022",
  "url": "https://www2.aneel.gov.br/cedoc/ren20221008.pdf",
  "char_start": 798, "char_end": 1313,
  "n_chars": 515, "n_tokens_est": 129,
  "text": "Art. 1º Esta Resolução estabelece..."
}
```

---

### Indexação

Módulo: `src/index.py`

**Embeddings:**
- `BAAI/bge-m3` (1024-dim, multilingual, contexto até 8k)
- Backend: `FlagEmbedding.BGEM3FlagModel` (gera dense + sparse em 1 forward)
- GPU autodetect (CUDA → Apple MPS → CPU); fp16 quando não-CPU
- `batch-size` configurável (default 32; **80 recomendado em GPU 6+ GB**)

**Vector store:**
- **Qdrant 1.12.4** via `docker-compose.yml`
- Coleção `aneel_chunks` (named vectors `dense` + `sparse`)
- Distance: cosine; sparse via `lexical_weights` do bge-m3
- Payload indexado em `tipo_ato`, `year`, `tier`, `doc_id`
- **Texto cru no payload** — sem lookup posterior em arquivos locais

**Sparse / lexical:**
- bge-m3 sparse (mesmo passe do dense)
- BM25 Okapi via `rank_bm25` como redundância textual independente
  (regex `\w+` lowercase, sem stopwords — texto jurídico precisa dos
  conectores, IDF cuida do peso)

**Resultado em corpus completo (RTX 3050 6 GB):** 160.267 chunks indexados,
130,1 min @ 20,5 ch/s (batch 80), VRAM 3,1 / 6 GB. BM25 separadamente em 31 s
→ pickle de 244 MB. Snapshot Qdrant: 1,22 GB.

```bash
python -m src.index \
  --chunks artifacts/chunks.jsonl \
  --bm25-out artifacts/bm25_index.pkl \
  --batch-size 80
```

Flags: `--limit` (smoke), `--collection`, `--skip-dense` (refazer só BM25).

**Snapshot e Release:**

```bash
# Cria snapshot (~6 s, 1,22 GB)
curl -X POST http://localhost:6333/collections/aneel_chunks/snapshots

# Copia do volume Docker pra disco local
docker cp aneel-qdrant:/qdrant/snapshots/aneel_chunks/<snapshot-name>.snapshot \
          artifacts/qdrant_snapshot.tar
```

---

### Retrieval

Módulo: `src/retrieve.py`

Pipeline híbrido em 5 etapas:

1. **Embedding (bge-m3):** transforma query em dense (1024-dim)
2. **Dense search:** top-30 do Qdrant por similarity cosine
3. **BM25 search:** top-30 via índice BM25 serializado
4. **RRF fusion:** combina rankings com Reciprocal Rank Fusion (k=60)
   ```
   score(chunk) = Σ 1 / (k + rank_in_list_i)   para cada lista i
   ```
   Sem tuning de pesos, robusto a variações entre modelos.
5. **Reranking:** `bge-reranker-v2-m3` reordena top-30 → top-10, refina scores

**Implementação:**
- Queries dense + BM25 em paralelo (`ThreadPoolExecutor(max_workers=2)`)
- Device autodetection (CUDA → CPU; flag `--device {auto,cpu,cuda,mps}` força)
- Loaders explícitos `load_bm25()`, `load_embedder()`, `load_reranker()`
  (sem singletons mágicos — caller controla lifecycle)
- Filtros opcionais por payload (`tipo_ato`, `year`, `tier`) aplicados em
  ambos os retrievers (Qdrant via `query_filter`, BM25 via numpy mask)
- Retorna `List[Hit]` com `score`, `score_rrf`, `score_rerank`, `rank_dense`,
  `rank_bm25`, `payload` inline

```bash
# Query simples
python -m src.retrieve --query "tarifa de uso do sistema de distribuição"

# Com filtros e top-K customizado
python -m src.retrieve --query "..." --tipo-ato ren --year 2022 --top-k 5

# Sem reranker (mais rápido), forçando CPU, saída JSON
python -m src.retrieve --query "..." --no-rerank --device cpu --json
```

**Performance típica:** ~5-10 s por query (embedding 1-2 s, rerank 7-9 s em CPU).

---

### Geração

Módulo: `src/generate.py`

Recebe a lista de `Hit` da etapa de retrieval e gera resposta fundamentada
via **Claude Sonnet 4.6**, com citações obrigatórias inline `[N]`.

```
query + top-K Hits
  │
  ├─ build_context_block()
  │    numera cada chunk [1]…[K] com metadados (tipo, ano, seção, URL)
  │
  ├─ system prompt (cache_control="ephemeral" → prompt caching sempre ativo)
  │    • responde APENAS pelos trechos fornecidos
  │    • cita obrigatoriamente com [N] inline
  │    • resposta em PT-BR
  │    • fallback sem alucinação: "Não encontrei informação suficiente…"
  │
  ├─ Claude Sonnet 4.6  (streaming no CLI, batch para avaliação)
  │
  └─ extrai [N] citados programaticamente → monta citations[]
```

**Schema de saída `GenerationResult`:**

```json
{
  "answer": "A TUSD é calculada conforme os Procedimentos de Distribuição [1]. No caso de autoprodução, aplica-se desconto de 50% [2].",
  "citations": [
    {"n": 1, "chunk_id": "2022/ren20221000__art5",
     "url": "https://www2.aneel.gov.br/cedoc/ren20221000.pdf",
     "tipo_ato": "ren", "title": "RESOLUÇÃO NORMATIVA ANEEL Nº 1.000",
     "section": "Art. 5º"},
    {"n": 2, "chunk_id": "2022/reh20223000__art2",
     "url": "https://www2.aneel.gov.br/cedoc/reh20223000.pdf",
     "tipo_ato": "reh", "title": "RESOLUÇÃO HOMOLOGATÓRIA Nº 3.000",
     "section": "Art. 2º"}
  ],
  "query": "o que é TUSD e como é calculada?",
  "n_chunks": 10,
  "model": "claude-sonnet-4-6",
  "input_tokens": 4280, "output_tokens": 310, "cache_read_tokens": 312,
  "latency_ms": 2100,
  "not_found": false
}
```

**Decisões de design:**

| Decisão | Escolha | Motivo |
|---|---|---|
| Prompt caching | `cache_control="ephemeral"` no system | Reutiliza cache em chamadas consecutivas, reduz latência e custo |
| Anti-alucinação | Regra explícita no system + fallback padronizado | LLM não pode inventar; resposta fora do contexto dispara sinal fixo |
| Citations | Inline `[N]` extraídas por regex | Formato limpo + `citations[]` estruturado para integrações |
| Streaming | Ativado por padrão no CLI | Feedback imediato; desativável com `--no-stream` ou `--json` |
| Interface Python | `generate(query, hits, client)` → `GenerationResult` | Reutilizável pela avaliação sem overhead de CLI |

```bash
# Pré-requisito: .env com ANTHROPIC_API_KEY
make generate QUERY="o que é TUSD e como ela é calculada?"

# Com filtros
python -m src.generate \
  --query "prazo para ligação nova de baixa tensão" \
  --tipo-ato ren --year 2022 --top-k 8

# Saída JSON (para integração)
python -m src.generate --query "..." --json --no-stream > result.json
```

---

## Avaliação

Módulo: `eval/evaluate.py` + golden set em `eval/golden_set.jsonl`.

### Golden Set

**79 questões** estratificadas por tipo de ato e ano, geradas via Claude
Sonnet 4.6 a partir de amostras representativas do corpus.

**Distribuição:**

| Tipo | N | Descrição |
|---|---|---|
| Factual | 30 | "qual o prazo de X?", "o que é Y?" |
| Conceitual | 15 | "explique o conceito de tarifa de uso" |
| Comparativa | 9 | "diferença entre X e Y" |
| Multi-hop | 15 | requer 2+ documentos distintos |
| Negativa | 10 | resposta esperada = "não consta" |

**Schema:**

```json
{
  "id": "gs_001",
  "pergunta": "Qual o prazo máximo para a distribuidora atender solicitação de ligação nova?",
  "tipo_query": "factual",
  "resposta_esperada": "Até 2 dias úteis conforme REN 2019/2020",
  "docs_relevantes": ["2022/ren20221000"],
  "tipo_ato_filtro": "ren",
  "year_filtro": 2022
}
```

### Métricas de Retrieval

**69 questões não-negativas, top-20 com reranker:**

| Métrica | Valor |
|---|---|
| **hit@5**  | **71,0%** |
| **hit@10** | **72,5%** |
| **hit@20** | **81,2%** |
| **MRR**    | **0,619** |

**Quebra por tipo de questão:**

| Tipo | N | hit@5 | hit@10 | hit@20 | MRR |
|---|---|---|---|---|---|
| factual | 30 | **86,7%** | 86,7% | 96,7% | 0,794 |
| conceptual | 15 | 66,7% | 66,7% | 80,0% | 0,576 |
| comparative | 9 | 55,6% | 55,6% | 66,7% | 0,506 |
| multi_hop | 15 | 53,3% | 60,0% | 60,0% | 0,380 |

### Latência

| Estágio | p50 | p95 |
|---|---|---|
| Retrieval (embedding + RRF + rerank) | 2,3 s | 6,8 s |
| End-to-end com geração Claude | 3,0 s | 21,9 s |

### Comandos

```bash
# Gerar golden set (~14 min, custa tokens — só rodar 1× ou quando o corpus mudar)
make golden-set

# Avaliar só retrieval (rápido, ~3 min, sem custo de API)
make evaluate

# Avaliação completa: retrieval + geração + LLM eval (~30 min, custa tokens)
make evaluate-full

# Smoke test de geração com limite de questões
make evaluate-full GEN_LIMIT=10
```

Saídas: `eval/eval_results.jsonl` (por questão), `eval/eval_summary.json`
(agregado).

---

## Análise do Corpus

Roda em todos os 26.731 PDFs baixados ([`scripts/analyze_pdfs.py`](scripts/analyze_pdfs.py)):

```bash
python scripts/analyze_pdfs.py \
    --pdfs-dir data/pdfs_aneel \
    --report-json data/pdfs_aneel/_analysis.json
```

**Saúde dos PDFs (n=26.731):**

- ✅ **100% text-native** — 4 OCR-suspect (0,01%), 0 encrypted, 0 erros
- ✅ Origem: 90,8% MS Word, 7,1% Acrobat PDFMaker, 0,8% iText
- ✅ PDF versions: 1.7 (73,4%), 1.5 (21,1%), 1.6 (5,3%), 1.4 (0,2%)
- ✅ Multi-coluna: 33,7% (concentrado em votos/decisões)
- ✅ **OCR é desnecessário; parser único PyMuPDF basta**

**Tamanho do corpus:**

| Métrica | Valor |
|---|---|
| PDFs baixados | 26.731 |
| Tamanho em disco | 4,04 GB |
| Páginas totais | 117.005 |
| Tokens estimados (~4 chars/tok) | **~54 milhões** |
| Média de páginas por PDF | 4,38 |

**Distribuição por tipo de ato (top 10 = 95% do corpus):**

| Tipo | n | % | pgs p50 | Tier | multi-col |
|---|---|---|---|---|---|
| DSP (Despacho) | 9.932 | 37,2% | 1 | C | 12% |
| AREA (Voto Área) | 3.919 | 14,7% | 6 | B | 82% |
| REA (Resolução Autorizativa) | 3.894 | 14,6% | 3 | B | 18% |
| PRT (Portaria) | 3.174 | 11,9% | 2 | B | 8% |
| ADSP (Voto DSP) | 2.098 | 7,8% | 6 | B | 72% |
| APRT (Voto Portaria) | 562 | 2,1% | 4 | B | 75% |
| NDSP (Nota Técnica DSP) | 478 | 1,8% | 8 | A | 78% |
| AREH (Voto REH) | 475 | 1,8% | 11 | B | 89% |
| REH (Resolução Homologatória) | 474 | 1,8% | 8 | A | 74% |
| ECT (Extrato) | 350 | 1,3% | 1 | C | 0% |

---

## Decisões de Arquitetura

### Por que bge-m3 (e não OpenAI/e5-base)?

- **Qualidade:** SOTA em multilingual retrieval, especialmente PT-BR
- **Versatilidade:** gera dense + sparse + ColBERT-style num único forward
- **Sem dependência de API:** roda local, sem chave externa
- **Tradeoff aceito:** pesa 2 GB e é mais lento em CPU que e5-base —
  compensado por snapshot pré-construído

### Por que Qdrant (e não Chroma/FAISS/pgvector)?

- **Filtros nativos por payload** — essencial para `tipo_ato`, `year`, `tier`
- **Hybrid search built-in** (dense + sparse na mesma query)
- **Snapshots restauráveis** — chave da estratégia de replicabilidade
- **Performance** em corpus grande (~200k pontos é tranquilo)

### Por que Claude Sonnet 4.6 (e não GPT-4/local)?

- **PT-BR jurídico:** qualidade superior em nuances jurídicas brasileiras
- **Citações:** segue instruções de citar fontes melhor que outros
- **Contexto longo:** 200k tokens permite passar muitos chunks
- **Prompt caching:** reduz latência e custo em workloads repetidos

### Por que 3-tier chunking (e não chunking uniforme)?

- **Heterogeneidade extrema** do corpus (DSP de 1 pg vs NREH de 33 pgs)
- **Estrutura jurídica clara** — artigos são unidades semânticas naturais
- **Análise empírica** mostra que prevalência de marcadores (`Art.`, `§`)
  varia drasticamente entre tipos
- **Recall melhor** — chunks com granularidade adequada ao conteúdo

### Por que RRF (e não weighted sum)?

- **Robusto sem tuning** — não precisa otimizar pesos α dense + (1-α) sparse
- **Insensível a magnitudes** — scores de modelos diferentes não são
  comparáveis em valor absoluto
- **Padrão da indústria** — Vespa, Elasticsearch, Cohere usam

### Por que dense + BM25 (e não dense + sparse + BM25)?

Dense já cobre polissemia/sinônimos (vetores semânticos), BM25 cobre jargão
raro com IDF do corpus. Sparse do bge-m3 é dominado por essa combinação —
sua contribuição marginal é pequena e custa uma terceira query. Caso o golden
set indique ganho, basta adicionar `_sparse_search` como 3ª lista no
`rrf_fuse`.
