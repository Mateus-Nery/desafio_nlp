# RAG sobre Legislação ANEEL

Sistema RAG (Retrieval-Augmented Generation) sobre a biblioteca legislativa da
ANEEL — Agência Nacional de Energia Elétrica. Cobre **26.731 PDFs** dos anos
2016, 2021 e 2022, totalizando ~117 mil páginas e ~53 milhões de tokens de
legislação do setor elétrico brasileiro.

> **Status atual:** Fases 1-4 **concluídas** (download, parser, chunking,
> indexação) e [GitHub Release v0.4.0](https://github.com/Mateus-Nery/desafio_nlp/releases/tag/v0.4.0)
> publicada com snapshot pré-indexado pronto para uso. Fases 5-8 (retrieval,
> geração, avaliação, serving) **planejadas**, em construção.

---

## Sumário

1. [Visão Geral & Objetivo](#visão-geral--objetivo)
2. [Arquitetura Completa](#arquitetura-completa)
3. [Stack Tecnológica](#stack-tecnológica)
4. [Estrutura do Repositório](#estrutura-do-repositório)
5. [Como Rodar — 3 Caminhos](#como-rodar--3-caminhos)
6. [Fases do Pipeline](#fases-do-pipeline)
7. [Replicabilidade](#replicabilidade)
8. [Avaliação & Golden Set](#avaliação--golden-set)
9. [Análise do Corpus](#análise-do-corpus)
10. [Decisões de Arquitetura](#decisões-de-arquitetura)
11. [Roadmap](#roadmap)

---

## Visão Geral & Objetivo

Construir um sistema RAG completo capaz de responder perguntas sobre legislação
ANEEL com **qualidade alta** e **replicabilidade total** — examinador deve
conseguir clonar o repo e ter o sistema funcionando sem dor.

**Princípios diretores:**

1. **Qualidade não é negociável** — modelos SOTA (bge-m3, Claude Sonnet,
   bge-reranker-v2-m3), sem fallbacks degradados.
2. **Replicabilidade é como entregamos**, não o que cortamos — Docker,
   versões pinadas, snapshots pré-construídos, smoke test rápido.
3. **Robustez** — pipeline idempotente, retomável, com retries em todas as
   camadas que tocam rede.
4. **Decisões fundamentadas em dados** — análise empírica do corpus
   (n=26.731) informa cada escolha (chunking, parser, etc.), não chutes.

---

## Arquitetura Completa

```
┌──────────────────────────────────────────────────────────────────┐
│  FASE 1 — INGESTÃO (✅ concluída)                                │
│     3 JSONs ANEEL  →  download_aneel_pdfs.py  →  pdfs_aneel/     │
│     • curl_cffi (bypass Cloudflare via TLS impersonation)        │
│     • asyncio + semaphore (concorrência 8)                       │
│     • Retries c/ backoff + manifest JSONL idempotente            │
│     • Output: 26.731 PDFs, 4.04 GB                                │
└──────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│  FASE 2 — PARSER (✅ concluída)                                  │
│     PDFs → PyMuPDF → parsed.jsonl (1 doc por linha)              │
│     • Texto limpo (strip headers, FL X de Y, hífens)             │
│     • Tabelas via page.find_tables() → markdown                  │
│     • Detecção e preservação de hierarquia (Art./§/Inciso)       │
│     • Output: 26.731 docs, 54,4 M tokens, 39.390 tabelas         │
└──────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│  FASE 3 — CHUNKING 3-TIER (✅ concluída)                         │
│     Tier A → split por Art./Anexo  (REN, REH, RES, NDSP…)        │
│     Tier B → parágrafo + merge ~500 tok  (AREA, REA, PRT…)       │
│     Tier C → 1 PDF = 1 chunk         (DSP curto, ECT, AVS…)      │
│     Output: 160.267 chunks (A=98.7k, B=50.0k, C=11.5k)           │
└──────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│  FASE 4 — INDEXAÇÃO (✅ concluída)                               │
│     • Dense embeddings: BAAI/bge-m3 (1024-dim, multilingual)     │
│     • Sparse (lexical): bge-m3 sparse + BM25 (rank_bm25)         │
│     • Vector store: Qdrant 1.12.4 (docker), payload indexado     │
│     • Metadata fields: tipo_ato, year, tier, doc_id              │
│     • Snapshot público: GitHub Release v0.4.0 (1,46 GB total)    │
└──────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│  FASE 5 — RETRIEVAL (🔨 próxima)                                 │
│     query  →  embed (bge-m3)                                     │
│            →  [dense top-30] + [BM25 top-30]                     │
│            →  RRF fusion                                          │
│            →  bge-reranker-v2-m3  →  top-5 a 10                  │
│            →  filtros opcionais por metadata                     │
└──────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│  FASE 6 — GERAÇÃO (📋 planejada)                                 │
│     Claude Sonnet 4.6 (API)                                      │
│     Prompt enforça citações por chunk_id + url                   │
│     Output: resposta + lista de fontes (tipo_ato, ano, art., URL)│
└──────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│  FASE 7 — AVALIAÇÃO (📋 planejada)                               │
│     Golden set ~80 perguntas (geradas via LLM + revisão humana)  │
│     Métricas:                                                    │
│       • hit@k, MRR (retrieval)                                   │
│       • faithfulness, answer relevance (Ragas, LLM-as-judge)     │
│       • p95 latency end-to-end                                   │
└──────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│  FASE 8 — SERVING (opcional, se houver tempo)                    │
│     FastAPI /query endpoint + Streamlit UI demo                  │
└──────────────────────────────────────────────────────────────────┘
```

---

## Stack Tecnológica

| Camada | Tecnologia | Versão alvo | Justificativa |
|---|---|---|---|
| Linguagem | Python | 3.11+ | Ecossistema NLP/RAG |
| HTTP (Fase 1) | `curl_cffi` | 0.7+ | TLS impersonation Chrome para bypass de Cloudflare Bot Management |
| Async | `asyncio` + semaphore | stdlib | Concorrência controlada de downloads |
| PDF parsing | `PyMuPDF` (`fitz`) | 1.27+ | Validado em 26.731 PDFs sem erros, suporta `find_tables()` |
| Embeddings | `BAAI/bge-m3` | latest | SOTA multilingual; gera dense+sparse+colbert num passe; forte em PT-BR |
| Vector store | **Qdrant** (Docker) | 1.10+ | Hybrid search nativo, filtros por payload, snapshots restauráveis |
| Sparse search | `rank_bm25` | 0.2+ | Pure Python, complementa bge-m3 sparse |
| Reranker | `BAAI/bge-reranker-v2-m3` | latest | Padrão da indústria, ganho de qualidade significativo |
| LLM gerador | **Claude Sonnet 4.6** (API) | claude-sonnet-4-6 | Excelente em PT-BR jurídico, citações confiáveis, contexto longo |
| Avaliação | `ragas` + golden set custom | 0.2+ | Faithfulness + answer relevance, métricas determinísticas |
| API | `FastAPI` | 0.115+ | (opcional) demo |
| UI | `Streamlit` | 1.40+ | (opcional) demo |
| Infra | Docker Compose | 2.20+ | Sobe Qdrant + app com 1 comando |

---

## Estrutura do Repositório

```
desafio_nlp/
├── README.md                          ← este arquivo
├── pyproject.toml                     ← versões fixas (poetry/uv)
├── requirements.txt                   ← idem, gerado pinned
├── docker-compose.yml                 ← Qdrant + app
├── Dockerfile                         ← imagem multi-stage
├── Makefile                           ← orquestração de tarefas
├── .env.example                       ← ANTHROPIC_API_KEY=
├── .gitignore
│
├── data/
│   ├── dados_grupo_estudos/           ← 3 JSONs ANEEL (entrada)
│   └── pdfs_aneel/                    ← 26.731 PDFs + manifests
│       ├── 2016/*.pdf
│       ├── 2021/*.pdf
│       ├── 2022/*.pdf
│       ├── _manifest.jsonl
│       ├── _failures.jsonl
│       ├── _summary.json
│       └── _analysis.json             ← saída de analyze_pdfs.py
│
├── artifacts/                         ← gerados pelo pipeline (gitignored)
│   ├── chunks.jsonl                   ← Fase 3
│   ├── qdrant_snapshot.tar            ← Fase 4 (snapshot Qdrant)
│   ├── bm25_index.pkl                 ← Fase 4 (BM25 serializado)
│   └── manifest.json                  ← versões + hashes
│
├── src/
│   ├── __init__.py
│   ├── parse_pdfs.py                  ← Fase 2
│   ├── chunk.py                       ← Fase 3
│   ├── index.py                       ← Fase 4 (embeddings + Qdrant + BM25)
│   ├── retrieve.py                    ← Fase 5 (hybrid + RRF + rerank)
│   ├── generate.py                    ← Fase 6 (Claude + prompt + citações)
│   ├── evaluate.py                    ← Fase 7 (Ragas + métricas)
│   ├── serve.py                       ← Fase 8 (FastAPI/Streamlit)
│   └── pipeline.py                    ← orquestrador end-to-end
│
├── eval/
│   ├── golden_set.jsonl               ← ~80 perguntas+respostas+docs
│   └── eval_report.json               ← saída de evaluate.py
│
├── scripts/                           ← scripts standalone Fase 1
│   ├── download_aneel_pdfs.py         ← (Fase 1) ✅ funciona
│   └── analyze_pdfs.py                ← análise exploratória ✅ funciona
│
└── tests/
    ├── test_smoke.py                  ← <30s, valida pipeline ponta-a-ponta
    └── test_*.py                      ← unit tests por módulo
```

---

## Como Rodar — 3 Caminhos

### Pré-requisitos comuns

- Python 3.11+ (3.10 também funciona)
- Docker + Docker Compose
- (opcional) GPU CUDA ou Apple MPS — autodetectada
- ~5 GB livres em disco para o Caminho 2 (modelo bge-m3 + snapshot + Qdrant volume)
- ~10 GB livres em disco para o Caminho 1 (acima + 4 GB de PDFs)

### Setup inicial (todos os caminhos)

```bash
git clone https://github.com/Mateus-Nery/desafio_nlp.git
cd desafio_nlp

# Cria venv
python -m venv .venv
source .venv/bin/activate         # Windows: .venv\Scripts\activate

# (opcional, mas recomendado se tem GPU NVIDIA no Windows)
# Sem isso o pip install vem com torch CPU-only e a Fase 4 fica 4-6 h em vez de 2 h
pip install torch --index-url https://download.pytorch.org/whl/cu124

pip install -r requirements.txt

# Copia template de env e adiciona sua chave Claude (necessária só pra Fase 6)
cp .env.example .env
# editar .env: ANTHROPIC_API_KEY=sk-ant-...

# Sobe Qdrant em localhost:6333 (volume persistente)
docker compose up -d
```

### Caminho 2 — Bootstrap com snapshot pré-construído ⚡ (recomendado)

Pula a parte cara restaurando os artefatos pré-computados publicados como
[GitHub Release v0.4.0](https://github.com/Mateus-Nery/desafio_nlp/releases/tag/v0.4.0).
**Esse caminho é autossuficiente** — o examinador *não* precisa baixar os
4 GB de PDFs nem rodar parser/chunker. Só Qdrant + BM25 + código + chave
Claude bastam para responder qualquer query.

```bash
mkdir -p artifacts

# Baixa snapshot Qdrant (1.22 GB) + índice BM25 (244 MB)
curl -L -o artifacts/qdrant_snapshot.tar \
  https://github.com/Mateus-Nery/desafio_nlp/releases/download/v0.4.0/qdrant_snapshot.tar
curl -L -o artifacts/bm25_index.pkl \
  https://github.com/Mateus-Nery/desafio_nlp/releases/download/v0.4.0/bm25_index.pkl

# Restaura snapshot na coleção aneel_chunks (~14 s)
curl -X POST 'http://localhost:6333/collections/aneel_chunks/snapshots/upload?priority=snapshot' \
     -F snapshot=@artifacts/qdrant_snapshot.tar

# Smoke: 5 queries dense, valida que retrieval responde
python scripts/smoke_query_qdrant.py
```

**O que tem na Release v0.4.0:**

| Arquivo | Tamanho | Obrigatório? | Para quê |
|---|---|---|---|
| `qdrant_snapshot.tar` | 1,22 GB | ✅ sim | Coleção `aneel_chunks` com 160.267 pontos: dense (bge-m3 1024-dim cosine) + sparse (lexical_weights), payload com texto cru e metadados |
| `bm25_index.pkl` | 244 MB | ✅ sim | Índice BM25 Okapi serializado, tokenizer regex `\w+` lowercase |
| `manifest.json` | 1,8 KB | ✅ sim | Versões dos modelos + SHA-256 dos artefatos |

**Por que isso é autossuficiente:** o `src/index.py` armazena o texto cru
de cada chunk dentro do payload do Qdrant (e do BM25 pickle). Quando o
retrieval traz um chunk, ele já vem com `text`, `url`, `tipo_ato`, etc.
inline — não há lookup posterior em arquivos locais.

**O que ainda precisa baixar automaticamente na primeira query** (uma vez só):

- `BAAI/bge-m3` (~2 GB) do HuggingFace, cacheado em `~/.cache/huggingface/`.
  Necessário porque o examinador precisa **embedar a query nova** dele com o
  mesmo modelo usado para indexar. Demora ~2-3 min no primeiro uso, depois
  é instantâneo. Para acelerar, pode ser pré-baixado com:
  `python -c "from FlagEmbedding import BGEM3FlagModel; BGEM3FlagModel('BAAI/bge-m3')"`.

### Caminho 1 — Tudo do zero (fiel ao código)

Reproduz **todas as fases** do zero a partir dos 3 JSONs ANEEL. Lento mas
total. Útil pra examinador rigoroso que quer validar a reprodutibilidade.

```bash
# Fase 1 — baixa 26.731 PDFs da ANEEL (~13 min)
python scripts/download_aneel_pdfs.py \
  --json-dir data/dados_grupo_estudos \
  --output-dir data/pdfs_aneel \
  --concurrency 8

# Análise exploratória do corpus (opcional, valida saúde dos PDFs)
python scripts/analyze_pdfs.py \
  --pdfs-dir data/pdfs_aneel \
  --report-json data/pdfs_aneel/_analysis.json

# Fase 2 — extrai texto dos PDFs (~30 min com 8 cores)
python -m src.parse_pdfs \
  --pdfs-root data/pdfs_aneel \
  --out artifacts/parsed.jsonl \
  --workers 8

# Fase 3 — chunking 3-tier (~10 s)
python -m src.chunk \
  --in artifacts/parsed.jsonl \
  --out artifacts/chunks.jsonl

# Fase 4 — embeddings + Qdrant + BM25 (depende do hardware, ver tabela abaixo)
python -m src.index \
  --chunks artifacts/chunks.jsonl \
  --bm25-out artifacts/bm25_index.pkl \
  --batch-size 80
```

**Tempos esperados na Fase 4** (a mais cara — 160.267 chunks bge-m3):

| Hardware | Indexação dense+sparse |
|---|---|
| GPU desktop (RTX 3080+) | 30-60 min |
| GPU A100 | 10-15 min |
| **GPU laptop (RTX 3050 6 GB)** | **~130 min** (medido) |
| CPU 8-core (batched, FP16) | 1-3 h |
| CPU 4-core | 4-6 h |

### Caminho 3 — Smoke test rápido

Valida que tudo está corretamente instalado e responde. Pré-requisito:
Caminho 2 já restaurou o snapshot.

```bash
# Carrega bge-m3 (cache local), encoda 5 queries de domínio,
# faz busca dense via Qdrant, mostra top-3 com payload completo
python scripts/smoke_query_qdrant.py
```

Saída esperada: top-3 coerente para "TUSD", "prazo de ligação",
"microgeração distribuída", etc. Tempo total: ~20 s (incluindo carga
do bge-m3 do cache, ~3 s).

### Query interativa

> Disponível após Fase 5/6 (ver Roadmap). Hoje, a forma de inspecionar
> resultados é via `scripts/smoke_query_qdrant.py` ou consultando o Qdrant
> diretamente via REST/dashboard em `http://localhost:6333/dashboard`.

---

## Fases do Pipeline

### Fase 1 — Ingestão (✅ concluída)

Script: [`scripts/download_aneel_pdfs.py`](scripts/download_aneel_pdfs.py)

Lê 3 JSONs de metadados ANEEL, deduplica URLs, baixa todos os PDFs com:

- Concorrência controlada (asyncio + semaphore, default 8)
- Retries com backoff exponencial e jitter (até 5x)
- Validação de magic number (`%PDF-`) e SHA-256
- Manifest JSONL para retomada idempotente
- Falhas separadas em `_failures.jsonl` para reexecução
- **Bypass Cloudflare** via `curl_cffi` com TLS impersonation Chrome

> **Por que `curl_cffi`?** O servidor `www2.aneel.gov.br/cedoc/` está atrás de
> Cloudflare Bot Management, que bloqueia requisições com base em fingerprint
> TLS (JA3) — `httpx` puro retorna 403 mesmo com User-Agent de browser. O
> `curl_cffi` reproduz o handshake TLS do Chrome real e passa pelo bloqueio.

**Resultado:**

| Métrica | Valor |
|---|---|
| URLs únicas (após dedup) | 26.768 |
| Não-PDFs filtrados (HTML/ZIP/XLSX) | 254 |
| Baixados com sucesso | **26.731 (99,82%)** |
| Falhas | 47 (43× HTTP 404 reais + 4× URL malformada) |
| Tamanho em disco | 4,04 GB |
| Duração | ~13 min @ 30-40 PDFs/s |

#### Comando

```bash
python scripts/download_aneel_pdfs.py \
    --json-dir data/dados_grupo_estudos \
    --output-dir data/pdfs_aneel \
    --concurrency 8
```

#### Flags úteis

| Flag | Default | Descrição |
|---|---|---|
| `--json-dir` | obrigatório | Pasta com os 3 JSONs |
| `--output-dir` | obrigatório | Destino dos PDFs |
| `--concurrency` | 8 | Downloads simultâneos |
| `--max-retries` | 5 | Tentativas por URL |
| `--dry-run` | off | Conta sem baixar |
| `--only-year` | — | Limita a 2016, 2021 ou 2022 |
| `--max-downloads` | — | Limita N downloads (smoke test) |
| `--sample-fraction` | — | Fração `(0,1]`, estratificada por ano+tipo |
| `--sample-seed` | 42 | Seed da amostra |

#### Saídas

```
data/pdfs_aneel/
├── 2016/*.pdf
├── 2021/*.pdf
├── 2022/*.pdf
├── _manifest.jsonl    ← 1 linha JSON por download bem-sucedido
├── _failures.jsonl    ← URLs que esgotaram retries
├── _errors.log        ← log detalhado de retries
└── _summary.json      ← resumo final do run
```

#### Comportamento de retries

- **Retryable** (com backoff): 403 (CF transiente), 408, 425, 429, 5xx,
  timeouts, connection errors, magic inválido
- **Não-retryable** (vai pra failures): 400, 401, 404, 410, 451
- **Retry-After** respeitado em 429/503 (até teto de 60s)

#### Politeness

- User-Agent de Chrome real (necessário pra passar pelo CF)
- Delay de 100ms após cada download bem-sucedido
- Concorrência conservadora (servidor ANEEL é legado)
- HTTP automaticamente promovido a HTTPS

---

### Fase 2 — Parser (✅ concluída)

Módulo: `src/parse_pdfs.py`

Lê todos os PDFs em `data/pdfs_aneel/` e gera `artifacts/parsed.jsonl` —
1 linha JSON por documento. Schema:

```json
{
  "doc_id": "2022/ren20221008",
  "tipo_ato": "ren",
  "year": 2022,
  "filename": "ren20221008.pdf",
  "title": "RESOLUÇÃO NORMATIVA ANEEL Nº 1.008, DE 15 DE MARÇO DE 2022",
  "ementa": "Dispõe sobre a Conta Escassez Hídrica...",
  "processo": "48500.006312/2021-55",
  "n_pages": 22, "n_chars": 12090, "n_tokens_est": 3267,
  "is_ocr_suspect": false,
  "pdf_creator": "Microsoft® Word para Microsoft 365",
  "text": "...full cleaned text...",
  "structure": [
    {"type": "capitulo", "label": "CAPÍTULO I",
     "title": "DISPOSIÇÕES PRELIMINARES",
     "start": 762, "end": 1313, "parent": ""},
    {"type": "artigo", "label": "Art. 1º",
     "start": 798, "end": 1313, "parent": ""},
    {"type": "paragrafo", "label": "§ 1º",
     "start": 891, "end": 950, "parent": "Art. 1º"}
  ],
  "tables": [{"id": "p2t1", "page": 2,
              "markdown": "| col1 | col2 |...",
              "rows": 7, "cols": 4}],
  "footnotes": [{"num": 1, "text": "Documento SIC nº ..."}]
}
```

#### Pipeline de cleaning

```
PDF
 │
 ├─► page.get_text("blocks", sort=True)   ← robusto a multi-coluna (33% do corpus)
 │
 ├─► page.find_tables() + heurística      ← descarta tabelas de coordenadas
 │   semântica (≥70% numérico)              UTM/CEG, mantém tabelas de prosa
 │
 ├─► detect_repeated_lines (≥3 págs)       ← header/footer dinâmico por doc
 │
 ├─► remove_boilerplate (regex hardcoded)
 │     • "AGÊNCIA NACIONAL DE ENERGIA ELÉTRICA – ANEEL" (linha solta)
 │     • "P. X Nota Técnica nº ..." / "Fl. X Nota Técnica nº ..."
 │     • "* A Nota Técnica é um documento emitido pelas Unidades..."
 │     • Linhas de underscores (divisores visuais)
 │     • "Este texto não substitui o publicado no Boletim Administrativo..."
 │     • "Retificado no D.O. de ..." / "(Tornada sem efeito pela...)"
 │     • Carimbos isolados de superintendência
 │
 ├─► fix_line_hyphenation                  ← só junta letra-letra
 │     "autori-\nzação" → "autorização"      (preserva "2021-\n55")
 │
 ├─► join_lone_paragraph_numbers           ← Voto/Nota: "12.\ntexto" → "12. texto"
 │
 ├─► extract_footnotes                     ← rodapés "1 Documento SIC nº ..."
 │   (saem do texto principal e vão               viram footnotes[]
 │    para campo separado)
 │
 ├─► normalize_chars                       ← NFC, NBSP→space, aspas curvas→retas
 │
 └─► collapse_blank_lines                  ← \n\n\n+ → \n\n
                                              espaços múltiplos → 1
```

#### Extração estrutural (`structure[]`)

Regex captura os marcadores e calcula offsets `(start, end)` dentro do
`text` final. Hierarquia: **Anexo > Capítulo > Seção > Artigo > §**. Cada
nó conhece seu parent (ex.: `§ 1º` → `parent: "Art. 1º"`).

| Tipo       | Regex                                               | Exemplo                  |
| ---------- | --------------------------------------------------- | ------------------------ |
| capitulo   | `^CAP[ÍI]TULO [IVXLCDM]+ – TÍTULO`                  | `CAPÍTULO I - DISPOSIÇÕES` |
| artigo     | `^Art\. \d+[ºo°]?`                                  | `Art. 12.`               |
| paragrafo  | `^§ \d+[ºo°]? \| Parágrafo único`                   | `§ 2º`, `Parágrafo único`|
| anexo      | `^ANEXO( [IVXLCDM]+)?`                              | `ANEXO I`                |
| quadro     | `^Quadro \d+`                                       | `Quadro 2 – Informações` |
| tabela     | `^Tabela \d+`                                       | `Tabela 1: Usinas`       |

Esse índice é o que vai habilitar o **chunking Tier A** (Art./Anexo/§) na
Fase 3 — sem precisar reparsing.

#### Decisões de design (gravitando para qualidade)

| Decisão                | Escolha                | Motivo                                                |
| ---------------------- | ---------------------- | ----------------------------------------------------- |
| Footnotes              | campo separado          | mantém texto principal limpo, ainda permite citar     |
| Tabelas                | só semânticas (Markdown)| descarta coordenadas UTM/listas de CEG (não-recuperáveis) |
| Multi-coluna           | sempre `blocks`+sort    | robusto, custa pouco                                  |
| Headers/footers        | regex + heurística repetição | combina precisão (regex) com cobertura (heurística) |
| Hifenização            | só letra-letra          | preserva IDs (`2021-55`), datas, códigos              |
| Numeração solta        | join com próxima linha não-vazia | resolve `1.\nresolve:` em Votos/Notas        |
| Workers                | `mp.cpu_count() - 1`    | paraleliza por documento (sem GIL)                    |
| Retomada               | `--resume` + dedup pelo `doc_id` | idempotente em re-runs                       |

#### Comando

```bash
# Corpus completo (~26.7k PDFs, ~30-60 min com 8 cores)
python -m src.parse_pdfs \
  --pdfs-root data/pdfs_aneel \
  --out artifacts/parsed.jsonl \
  --workers 8

# Smoke test nas 8 amostras de explore_pdfs.py
python -m src.parse_pdfs --samples-only \
  --out artifacts/parsed_samples.jsonl --workers 1

# Retomar interrompido
python -m src.parse_pdfs --resume \
  --out artifacts/parsed.jsonl --workers 8
```

#### Validação nas amostras

8/8 amostras processadas em 12.6s (1.6s/doc com 1 worker). Validação manual:

| Doc                       | Title extraído              | Processo extraído     | Struct | Tables | Footnotes |
| ------------------------- | --------------------------- | --------------------- | ------ | ------ | --------- |
| `ren20221008` (RES Norm)  | ✅ `RESOLUÇÃO NORMATIVA ANEEL Nº 1.008...` | ✅ `48500.006312/2021-55` | 92     | 3      | 0         |
| `reh20223008ti` (RES Hom) | ✅ `RESOLUÇÃO HOMOLOGATÓRIA Nº 3.008...`   | ✅ `48500.004911/2021-34` | 20     | 5      | 0         |
| `rea20165599ti` (RES Aut) | ✅ `RESOLUÇÃO AUTORIZATIVA Nº 5.599...`    | ✅ `48500.000939/2014-73` | 7      | 0      | 0         |
| `prt20153774` (Portaria)  | ✅ `PORTARIA N° 3.774...`                  | ✅ `48500.005223/2015-43` | 2      | 0      | 0         |
| `dsp2022021spde` (Despacho) | ✅ `DESPACHO DECISÓRIO Nº 21/2022/SPE`   | ✅ `48000.001295/1992-12` | 1      | 3      | 0         |
| `ndsp2022060` (Nota Téc)  | ✅ `Nota Técnica nº 01/2022-SGT/ANEEL`     | ✅ `48500.006465/2021-01` | 8      | 126    | 3         |
| `nreh20162014` (Nota Téc) | ✅ `Nota Técnica n° 004/2016-SGT-SRM/ANEEL`| ✅ `48500.000315/2015-37` | 7      | 9      | 2         |
| `area202210992_1` (Voto)  | ✅ `VOTO`                                  | ✅ `48500.003989/2012-41` | 5      | 2      | 4         |

**Cleaning verificado** em ndsp2022060 (128 págs):
- 0 ocorrências de footnote boilerplate
- 0 ocorrências de "P. X Nota Técnica" headers de página
- 0 divisores `_____`
- 1 ocorrência residual de "AGÊNCIA NACIONAL..." (legítima, no corpo)
- 3/3 footnotes extraídas para campo separado

#### Resultados — corpus completo (n=26.731)

Execução em 8 workers (cores), 1.780s = 29.7 min, **15.0 doc/s**, **0 falhas**.

| Métrica                  | Valor             |
| ------------------------ | ----------------- |
| Docs processados         | 26.731 / 26.731 (**100%**) |
| Falhas                   | 0                 |
| Total chars              | 201,3 M           |
| Tokens estimados         | 54,4 M            |
| OCR-suspect              | 7 (0,03%)         |
| Texto vazio (<100 chars) | 7 (0,03%)         |
| Tabelas extraídas        | 39.390            |
| Footnotes extraídas      | 8.274             |
| Struct nodes médio       | 4,1 / doc         |
| Tamanho `parsed.jsonl`   | 275 MB            |

**Title extraction rate por tipo (atos principais):**

| Tipo  | N total | Com title  | Taxa    |
| ----- | ------- | ---------- | ------- |
| `ren` | 154     | 154        | **100,0%** |
| `rea` | 3.894   | 3.894      | **100,0%** |
| `reh` | 474     | 474        | **100,0%** |
| `prt` | 3.174   | 3.167      | 99,8%   |
| `ndsp`| 478     | 476        | 99,6%   |
| `dsp` | 9.932   | 9.844      | 99,1%   |
| `nreh`| 224     | 221        | 98,7%   |
| `aprt`| 562     | 553        | 98,4%   |
| `area`| 3.919   | 3.836      | 97,9%   |
| `areh`| 475     | 463        | 97,5%   |
| `adsp`| 2.098   | 1.856      | 88,5%   |
| `aren`| 205     | 178        | 86,8%   |

Tipos com taxa baixa (`ect`=2%, `acp`=20%, `aap`=12%) são **extratos** e
**comunicados** que **não têm cabeçalho formal** — comportamento esperado,
não é bug. Esses tipos representam < 3% do corpus.

**Processo extraction rate**: ≥95% para `rea`/`reh`/`prt`, ≥89% para
`dsp`/`adsp`/`ndsp`/`aprt`, com média geral de ~88%.

#### Saídas

```
artifacts/
├── parsed.jsonl          # 1 linha por doc, 26.731 linhas, 275 MB
└── parse.log             # logs de execução, throughput, falhas
```

---

### Fase 3 — Chunking (✅ concluída)

Módulo: `src/chunk.py`

Estratégia em **3 tiers** baseada na análise empírica de heterogeneidade
do corpus (ver [Análise do Corpus](#análise-do-corpus)).

#### Tier A — denso jurídico, alta prioridade RAG

Documentos onde a unidade natural de recuperação é o **artigo** ou o
**anexo**.

- **Tipos:** REN, REH, RES, NREH, NDSP, INA
- **Estratégia:** split por regex `r'^\s*Art\.?\s*\d+'` + `Anexo` separado
- **Sub-split:** se artigo > 1500 tokens, divide por `§`
- **Overlap:** zero (artigos são unidades naturais)

#### Tier B — médio, prosa decisória

- **Tipos:** AREA, ADSP, APRT, REA, PRT
- **Estratégia:** split por parágrafo, merge até ~500 tokens
- **Overlap:** 50 tokens

#### Tier C — curto, baixa relevância regulatória

- **Tipos:** DSP, ECP, ECT, EDT, AVS, ACP, ATS
- **Estratégia:** PDF inteiro = 1 chunk (quase todos <2k tokens)

#### Resultados (corpus completo)

- 26.731 docs → **160.267 chunks** em 8 s
- Tier A: 98.709 (61,6%) — REA/AREA/PRT/ADSP/DSP dominam
- Tier B:  50.052 (31,2%)
- Tier C:  11.506 (7,2%)
- 0 chunks duplicados, hard cap de 1500 tokens respeitado

#### Metadado por chunk (schema final)

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
  "section_title": "",
  "title": "RESOLUÇÃO NORMATIVA ANEEL Nº 1.008, DE 15 DE MARÇO DE 2022",
  "ementa": "Dispõe sobre a Conta Escassez Hídrica...",
  "filename": "ren20221008.pdf",
  "url": "https://www2.aneel.gov.br/cedoc/ren20221008.pdf",
  "char_start": 798, "char_end": 1313,
  "n_chars": 515, "n_tokens_est": 129,
  "text": "Art. 1º Esta Resolução estabelece..."
}
```

---

### Fase 4 — Indexação (✅ concluída)

Módulo: `src/index.py`

#### Embeddings (dense + sparse num único forward)

- Modelo: **`BAAI/bge-m3`** commit `5617a9f6` (1024-dim, multilingual, contexto até 8k)
- Backend: `FlagEmbedding.BGEM3FlagModel` (gera dense + sparse em 1 forward)
- GPU autodetect (CUDA → Apple MPS → CPU); fp16 quando não-CPU
- `batch-size` configurável (default 32; **80 recomendado em GPU 6+ GB**)

#### Vector store

- **Qdrant 1.12.4** rodando via `docker-compose.yml`
- Coleção: `aneel_chunks` (named vectors `dense` + `sparse`)
- Distance: cosine; sparse usa `lexical_weights` do bge-m3
- Payload indexado nos campos: `tipo_ato`, `year`, `tier`, `doc_id`
- **Texto cru no payload** (decisão chave de design — examinador não precisa
  de lookup posterior em arquivos locais)

#### Sparse / lexical

- **bge-m3 sparse** (vem do mesmo passe do dense, no Qdrant como named vector)
- **BM25 Okapi** via `rank_bm25` como redundância textual independente
  (tokenizer regex `\w+` lowercase, sem stopwords — texto jurídico precisa
  dos conectores; IDF cuida do peso)

#### Resultados (corpus completo, RTX 3050 6 GB Laptop)

| Métrica | Valor |
|---|---|
| Chunks indexados | **160.267 / 160.267 (100%)** |
| Tempo BM25 | 31 s → 244 MB pickle |
| Tempo dense+sparse | **130,1 min @ 20,5 ch/s, batch 80** |
| VRAM em uso | 3,1 GB / 6 GB (folga p/ batch maior em GPU >6 GB) |
| Snapshot Qdrant | 1,22 GB |
| Smoke restore | drop → upload → 14 s → mesmas estatísticas → 5 queries dense top-3 coerente |

#### Comando

```bash
# Corpus completo (~2 h em RTX 3050 com batch 80)
python -m src.index \
  --chunks artifacts/chunks.jsonl \
  --bm25-out artifacts/bm25_index.pkl \
  --batch-size 80

# Smoke (200 chunks, sem mexer no Qdrant principal)
python -m src.index \
  --chunks artifacts/chunks.jsonl \
  --bm25-out /tmp/bm25_smoke.pkl \
  --collection aneel_chunks_smoke \
  --limit 200

# Só BM25 (já indexou dense, refazer só BM25)
python -m src.index \
  --chunks artifacts/chunks.jsonl \
  --bm25-out artifacts/bm25_index.pkl \
  --skip-dense
```

#### Snapshot e Release

Após indexação, geramos snapshot do Qdrant via API e publicamos como
[GitHub Release v0.4.0](https://github.com/Mateus-Nery/desafio_nlp/releases/tag/v0.4.0).
Detalhes do uso em [Caminho 2](#caminho-2--bootstrap-com-snapshot-pré-construído-).

```bash
# Cria snapshot (~6 s, 1,22 GB)
curl -X POST http://localhost:6333/collections/aneel_chunks/snapshots

# Copia do volume Docker pra disco local
docker cp aneel-qdrant:/qdrant/snapshots/aneel_chunks/<snapshot-name>.snapshot \
          artifacts/qdrant_snapshot.tar
```

---

### Fase 5 — Retrieval (🔨 próxima)

Módulo: `src/retrieve.py`

```
query
  → embed (bge-m3)
  → [dense top-30 do Qdrant] + [BM25 top-30]
  → RRF fusion (k=60)
  → bge-reranker-v2-m3 (top-30 → top-10)
  → filtros de metadata (tipo_ato, ano, tier) opcionais
  → return top-k chunks com scores
```

**RRF (Reciprocal Rank Fusion):**

```
score(chunk) = Σ 1 / (k + rank_in_list_i)   para cada lista i
```

Sem tunar pesos, robusto.

**Reranker** roda local (CPU OK, ~50ms/par; GPU mais rápido).

---

### Fase 6 — Geração (📋 planejada)

Módulo: `src/generate.py`

- **LLM:** Claude Sonnet 4.6 (`claude-sonnet-4-6`) via Anthropic API
- **Prompt:** enforça citação por chunk (URL + tipo_ato + número/ano +
  artigo quando aplicável)
- **Output JSON:**

```json
{
  "answer": "Conforme o art. 23 da REN 1.000/2021...",
  "sources": [
    {
      "chunk_id": "ren2021001000__art23",
      "tipo_ato": "REN",
      "numero": "1000/2021",
      "art": "23",
      "url": "https://...",
      "trecho_relevante": "..."
    }
  ],
  "confidence": "high"
}
```

---

### Fase 7 — Avaliação (📋 planejada)

Módulo: `src/evaluate.py`

#### Golden set

`eval/golden_set.jsonl` — ~80 perguntas geradas via LLM e revisadas
manualmente (estratégia híbrida).

Cada entrada:

```json
{
  "id": "q001",
  "pergunta": "Qual o prazo máximo para a distribuidora atender solicitação de ligação nova?",
  "tipo_query": "factual",
  "resposta_esperada": "Até 2 dias úteis...",
  "docs_relevantes": ["ren2021001000"],
  "tipo_ato_filtro": ["REN", "REH"]
}
```

#### Métricas

| Categoria | Métrica | O que mede |
|---|---|---|
| Retrieval | hit@k (k=5,10,20) | Doc relevante apareceu nos top-k |
| Retrieval | MRR | Posição média do primeiro relevante |
| Generation | Faithfulness (Ragas) | Resposta é suportada pelos docs recuperados |
| Generation | Answer relevance (Ragas) | Resposta de fato responde a pergunta |
| Latency | p50/p95 end-to-end | Tempo total query→resposta |

---

### Fase 8 — Serving (opcional)

- **FastAPI:** `POST /query` retorna JSON estruturado
- **Streamlit:** UI interativa com filtros por tipo de ato e ano,
  exibe fontes inline com links

Será feito **se houver tempo** após as Fases 2-7.

---

## Replicabilidade

### Estratégia de defesa em camadas

1. **Versões fixadas** em `requirements.txt` (`==`, não `>=`)
2. **Docker Compose** sobe Qdrant determinístico, sem instalar nada local
3. **Modelos cacheados** automaticamente pelo HuggingFace (`~/.cache/huggingface`)
4. **GPU autodetect** com fallback CPU avisado (warning explícito de tempo)
5. **Snapshot pré-construído** publicado como [GitHub Release v0.4.0](https://github.com/Mateus-Nery/desafio_nlp/releases/tag/v0.4.0) — examinador pula a parte cara em ~5-10 min
6. **Smoke test** rápido via `scripts/smoke_query_qdrant.py` (~20 s) valida que retrieval responde após restore
7. **Idempotência total** — rodar 2× não quebra nada (UUIDs determinísticos por `chunk_id`, `--resume` em downloader/parser)
8. **Erros descritivos** — falta de chave/modelo dá mensagem clara, não stacktrace cru

### Cross-platform

Todos os scripts são testados em:

- macOS (Apple Silicon e Intel)
- Linux (Ubuntu 22.04+)
- Windows (PowerShell)

Único ponto de atenção: paths usam `pathlib.Path` (não strings).

### O que está versionado vs gerado

| Categoria | Versionado? |
|---|---|
| Código (`src/`, `scripts/`) | ✅ Sim |
| Configs (`pyproject.toml`, `Makefile`, `docker-compose.yml`) | ✅ Sim |
| Golden set (`eval/golden_set.jsonl`) | ✅ Sim |
| JSONs ANEEL (`data/dados_grupo_estudos/`) | ❌ Não (compartilhados separadamente) |
| PDFs baixados (`data/pdfs_aneel/`) | ❌ Não (4 GB; regerado via `make download`) |
| Artefatos (`artifacts/`) | ❌ Não (publicados como GitHub Release) |
| Modelos | ❌ Não (cacheados pelo HuggingFace) |

---

## Avaliação & Golden Set

### Estratégia híbrida de criação do golden set

Para conciliar qualidade com viabilidade de tempo:

1. **Geração automática** via LLM lendo amostras representativas dos
   tipos de ato (DSP, REN, REH, RES, PRT, NDSP) — produz ~80 candidatas
2. **Revisão humana** corrige perguntas ambíguas, ajusta `docs_relevantes`,
   remove perguntas trivais demais (~30 min de trabalho)

Cobertura desejada:

| Categoria de pergunta | Quantidade |
|---|---|
| Factuais simples ("qual o prazo de X?") | ~30 |
| Conceituais ("o que é tarifa de uso?") | ~15 |
| Comparativas ("diferença entre X e Y") | ~10 |
| Multi-hop (requer 2+ docs) | ~15 |
| Negativas/edge cases (resposta = "não consta") | ~10 |

### Critérios de sucesso

Metas mínimas:

- **hit@10 ≥ 0.85** (retrieval encontra doc relevante 85% do tempo)
- **MRR ≥ 0.55** (média do inverso da posição do primeiro relevante)
- **Faithfulness ≥ 0.85** (resposta suportada pelos docs)
- **Answer relevance ≥ 0.85** (resposta endereça a pergunta)
- **p95 latency ≤ 5s** (com Claude API, sem rerank em GPU)

---

## Análise do Corpus

Roda em todos os 26.731 PDFs baixados (script:
[`scripts/analyze_pdfs.py`](scripts/analyze_pdfs.py)):

```bash
python scripts/analyze_pdfs.py \
    --pdfs-dir data/pdfs_aneel \
    --report-json data/pdfs_aneel/_analysis.json
```

Usa **PyMuPDF (`fitz`)** e gera, por PDF: páginas, bytes, chars extraíveis,
ratio texto/imagem, detecção heurística de multi-coluna, metadado
(creator/producer/version) e flag `ocr_suspect`.

### Saúde dos PDFs (n=26.731)

- ✅ **100% text-native** — 4 OCR-suspect (0,01%), 0 encrypted, 0 erros
- ✅ **Origem:** 90,8% MS Word, 7,1% Acrobat PDFMaker, 0,8% iText
- ✅ **PDF versions:** 1.7 (73,4%), 1.5 (21,1%), 1.6 (5,3%), 1.4 (0,2%)
- ✅ **Multi-coluna:** 33,7% (concentrado em votos/decisões)
- ✅ **Conclusão:** OCR é desnecessário; **parser único PyMuPDF basta**

### Tamanho do corpus

| Métrica | Valor |
|---|---|
| PDFs baixados | 26.731 |
| Tamanho em disco | 4,04 GB |
| Páginas totais | 117.005 |
| Texto bruto extraído | ~202 MB |
| Tokens estimados (~4 chars/tok) | **~52,9 milhões** |
| Média de páginas por PDF | 4,38 |
| Média de chars por página | 1.809 |

### Distribuição por tipo de ato (top 10 = 95% do corpus)

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

Relatório completo per-PDF em `data/pdfs_aneel/_analysis.json`.

---

## Decisões de Arquitetura

### Por que bge-m3 (e não OpenAI/e5-base)?

- **Qualidade:** SOTA em multilingual retrieval, especialmente PT-BR
- **Versatilidade:** gera dense + sparse + ColBERT-style num único forward
- **Sem dependência de API:** roda local, examinador não precisa de chave
- **Custo:** zero recorrente
- **Tradeoff aceito:** pesa 2 GB e é mais lento em CPU que e5-base —
  compensado por snapshot pré-construído

### Por que Qdrant (e não Chroma/FAISS/pgvector)?

- **Filtros nativos por payload** — essencial para `tipo_ato`, `ano`, `tier`
- **Hybrid search built-in** (dense + sparse no mesmo query)
- **Snapshots restauráveis** — chave da estratégia de replicabilidade
- **Performance** em corpus grande (~200k pontos é tranquilo)
- **Tradeoff aceito:** precisa Docker — examinadores autorizam Docker

### Por que Claude Sonnet 4.6 (e não GPT-4/local)?

- **PT-BR:** qualidade superior a GPT-4 em nuances jurídicas brasileiras
- **Citações:** segue instruções de citar fontes melhor que outros
- **Contexto longo:** 200k tokens permite passar muitos chunks sem perder
- **Custo previsível:** ~$3/$15 por M tokens
- **Tradeoff aceito:** requer API key — documentado e justificado

### Por que 3-tier chunking (e não chunking uniforme)?

- **Heterogeneidade extrema** do corpus (DSP de 1 pg vs NREH de 33 pgs)
- **Estrutura jurídica clara** — artigos são unidades semânticas naturais
- **Análise empírica** mostra que prevalência de marcadores (`Art.`,
  `§`) varia drasticamente entre tipos
- **Eficiência de retrieval** — chunks com granularidade adequada ao
  conteúdo dão melhor recall

### Por que RRF (e não weighted sum)?

- **Robusto sem tuning** — não precisa otimizar pesos α dense + (1-α)
  sparse
- **Insensível a magnitudes** — scores de modelos diferentes não são
  comparáveis em valor absoluto
- **Padrão da indústria** — Vespa, Elasticsearch, Cohere usam

---

## Roadmap

| Fase | Status | Detalhes |
|---|---|---|
| 1. Ingestão (download) | ✅ Concluída | 26.731 PDFs em 13 min |
| 1b. Análise exploratória | ✅ Concluída | 100% text-native, 0 OCR |
| 2. Parser PyMuPDF | ✅ Concluída | 26.731 docs em 29,7 min, 54,4 M tokens |
| 3. Chunking 3-tier | ✅ Concluída | 160.267 chunks em 8 s |
| 4. Indexação (embed + Qdrant + BM25) | ✅ Concluída | 130 min em RTX 3050 (batch 80) |
| Snapshot + Release | ✅ Concluída | v0.4.0 publicada (1,46 GB de assets) |
| 5. Retrieval (hybrid + rerank) | 🔨 Próxima | dense+BM25 → RRF → bge-reranker-v2-m3 |
| 6. Geração (Claude + prompt) | 📋 Planejada | Sonnet 4.6 com citações enforçadas |
| 7. Avaliação (Ragas + golden) | 📋 Planejada | hit@k, MRR, faithfulness, answer relevance |
| 8. Serving (FastAPI + Streamlit) | 📋 Opcional | endpoint `/query` + UI demo |
| Makefile com targets `make restore-artifacts`/`make smoke` | 📋 Nice-to-have | hoje os comandos são raw bash |

---

## Setup mínimo (legacy, Fase 1 standalone)

Caso queira rodar **só o downloader** sem o resto do pipeline:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install curl_cffi tqdm

python scripts/download_aneel_pdfs.py `
    --json-dir data/dados_grupo_estudos `
    --output-dir data/pdfs_aneel `
    --concurrency 8
```

Requer Python 3.11+. Para a análise exploratória adicional, instale também
`pymupdf`.
