# Download dos PDFs da Biblioteca ANEEL

Script de download da biblioteca legislativa da ANEEL (~20.000 PDFs).
Etapa 1 de um projeto RAG sobre legislação do setor elétrico brasileiro.

Lê os 3 JSONs de metadados, deduplica URLs e baixa todos os PDFs com:

- Concorrência controlada (asyncio + semáforo)
- Retries com backoff exponencial e jitter
- Validação de magic number (`%PDF-`) e SHA-256
- Manifest JSONL para retomada idempotente
- Failures separados para reexecução posterior
- **Bypass do Cloudflare** via `curl_cffi` com TLS impersonation de Chrome

> **Por que `curl_cffi`?** O servidor `www2.aneel.gov.br/cedoc/` está atrás de
> Cloudflare Bot Management, que bloqueia requisições com base em fingerprint
> TLS (JA3) — `httpx` puro retorna 403 mesmo com User-Agent de browser. O
> `curl_cffi` reproduz o handshake TLS do Chrome real e passa pelo bloqueio.
> Como efeito colateral, o User-Agent enviado é o do Chrome, não um custom.

---

## Setup

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Requer Python 3.11+.

---

## Uso

### Run completo

```powershell
python download_aneel_pdfs.py `
    --json-dir dados_grupo_estudos `
    --output-dir pdfs_aneel `
    --concurrency 8
```

Esperado: ~15 minutos pros ~26.785 PDFs (a ~30 PDFs/s com concorrência 8).
Taxa de sucesso esperada > 99% — falhas tendem a ser PDFs realmente
indisponíveis no servidor (404).

> **Nota:** os JSONs contêm ~250 URLs apontando para arquivos não-PDF
> (HTML, ZIP, XLSX, RAR — resoluções normativas e anexos diversos). Estes
> são **filtrados antes do download** porque o pipeline RAG só processa
> PDFs. Se precisar deles num próximo passo, rode um script separado.

### Smoke test (recomendado antes do run completo)

Baixa só 20 PDFs de 2022 pra validar que tudo está funcionando:

```powershell
python download_aneel_pdfs.py `
    --json-dir dados_grupo_estudos `
    --output-dir pdfs_aneel `
    --only-year 2022 `
    --max-downloads 20
```

### Amostra representativa (pra desenvolver pipeline em paralelo)

Baixa 10% do corpus aleatoriamente, mantendo a mesma proporção de
DSP/PRT/REN/REA/etc. e mesma distribuição por ano. Reprodutível via seed:

```powershell
python download_aneel_pdfs.py `
    --json-dir dados_grupo_estudos `
    --output-dir pdfs_aneel `
    --sample-fraction 0.1 `
    --sample-seed 42
```

> A amostra é determinística pela seed: rodar de novo com a mesma seed
> baixa exatamente os mesmos PDFs. **Compatível com o run completo
> posterior** — quando você for rodar o corpus inteiro, os PDFs da amostra
> já estão no manifest e são pulados.

### Dry-run (só conta, não baixa)

```powershell
python download_aneel_pdfs.py `
    --json-dir dados_grupo_estudos `
    --output-dir pdfs_aneel `
    --dry-run
```

### Flags úteis

| Flag | Default | O que faz |
|---|---|---|
| `--json-dir` | obrigatório | Pasta com os 3 JSONs de metadados |
| `--output-dir` | obrigatório | Destino dos PDFs + arquivos de controle |
| `--concurrency` | 8 | Downloads simultâneos (servidor da ANEEL é legado, não exagere) |
| `--max-retries` | 5 | Tentativas por URL |
| `--log-level` | INFO | DEBUG / INFO / WARNING / ERROR |
| `--dry-run` | off | Lista quantos seriam baixados e sai |
| `--only-year` | — | Limita a um ano (2016, 2021 ou 2022) |
| `--only-tipo` | — | Filtra pelo tipo do PDF (ex: `"Texto Integral:"`) |
| `--no-resume` | resume on | Ignora manifest e rebaixa tudo |
| `--max-downloads` | — | Limita N downloads (útil pra smoke test) |
| `--sample-fraction` | — | Fração `(0,1]` do corpus a amostrar (estratificada por ano+tipo de ato) |
| `--sample-seed` | 42 | Seed da amostra (reprodutibilidade) |

---

## Retomar um run interrompido

O script é **idempotente**. Se você der `Ctrl+C` no meio (ou se a rede cair), basta rodar **o mesmo comando de novo**:

```powershell
python download_aneel_pdfs.py --json-dir dados_grupo_estudos --output-dir pdfs_aneel
```

A retomada funciona assim:

1. Lê `_manifest.jsonl` e monta um set de URLs já completas
2. Cruza com o disco: se o arquivo sumiu ou está com tamanho zero, rebaixa
3. Pula tudo o que está completo, processa o resto

Não há ordem garantida entre execuções — o que importa é que cada URL aparece no manifest **uma única vez** após sucesso.

---

## Saídas no `--output-dir`

```
pdfs_aneel/
├── 2016/*.pdf
├── 2021/*.pdf
├── 2022/*.pdf
├── _manifest.jsonl    ← uma linha JSON por download bem-sucedido
├── _failures.jsonl    ← URLs que esgotaram retries (4xx não-retryable, magic inválido, etc.)
├── _errors.log        ← log detalhado de cada tentativa que falhou (incl. retries)
└── _summary.json      ← resumo final do run (escrito no fim ou em shutdown limpo)
```

### Formato do manifest

Cada linha é um JSON com:

```json
{
  "url": "https://www2.aneel.gov.br/cedoc/dsp2022021spde.pdf",
  "local_path": "2022/dsp2022021spde.pdf",
  "size_bytes": 123456,
  "sha256": "abc123...",
  "downloaded_at": "2026-04-22T14:30:12.345678+00:00",
  "http_status": 200,
  "ano_destino": 2022,
  "fontes": [
    {"ano": 2022, "titulo": "DSP - DESPACHO 021", "tipo": "Texto Integral:"}
  ]
}
```

`fontes` é uma lista porque o **mesmo PDF pode ser referenciado por múltiplos registros** (ex: anexos compartilhados entre despachos). A dedup acontece por URL antes do download.

### Formato do failures

```json
{
  "url": "https://www2.aneel.gov.br/cedoc/exemplo.pdf",
  "error": "HTTP 404 (não-retryable)",
  "http_status": 404,
  "attempts": 1,
  "last_attempt_at": "2026-04-22T14:35:01.000000+00:00",
  "ano_destino": 2022,
  "arquivo": "exemplo.pdf",
  "fontes": [...]
}
```

Para reexecutar só os que falharam, basta rodar o script de novo após inspeção manual — todos os sucessos já estão no manifest e serão pulados.

---

## Comportamento de retries

- **Retryable** (até `--max-retries` vezes, com backoff exponencial + jitter):
  HTTP 403 (CF transiente) / 408 / 425 / 429 / 5xx, timeouts, connection errors, magic number inválido
- **Não-retryable** (vai direto pra `_failures.jsonl`):
  HTTP 400 / 401 / 404 / 410 / 451
- **Retry-After** em respostas 429 / 503 é respeitado (até teto de 60s)

---

## Politeness

- User-Agent: enviado pelo `curl_cffi` impersonando Chrome (necessário para passar pelo CF)
- Delay de 100ms após cada download bem-sucedido
- Concorrência conservadora (8 por padrão) — o servidor da ANEEL é legado
- URLs `http://` são automaticamente promovidas a `https://` (CF bloqueia HTTP puro)

---

## Análise da amostra (etapa de parsing/chunking)

Antes de implementar o parser, rodamos `analyze_pdfs.py` sobre a amostra de
1% (294 PDFs, ~4 min) pra informar a estratégia. Comando:

```powershell
.venv\Scripts\python.exe analyze_pdfs.py `
    --pdfs-dir pdfs_aneel `
    --report-json pdfs_aneel\_analysis.json
```

O script usa **PyMuPDF (`fitz`)** e gera, por PDF: páginas, bytes, chars
extraíveis, ratio texto/imagem, detecção de multi-coluna heurística,
metadado (creator/producer/version), flag `ocr_suspect`. Depois agrega por
**tipo de ato** (extraído do prefixo do nome do arquivo: `dsp`, `ren`,
`reh`, etc.) e mede prevalência de marcadores hierárquicos jurídicos
(`Art.`, `§`, `Inciso`, `Capítulo`, `Seção`, `Anexo`, `Tabela`).

Requer `pymupdf` no venv (`pip install pymupdf`).

### Saúde dos PDFs

- **100% text-native** — 0 OCR-suspect, 0 encrypted, 0 erros de abertura
- **Origem:** 88% MS Word, 6% Acrobat PDFMaker, ~2% iText
- **PDF versions:** 1.7 (71%), 1.5 (21%), 1.6 (6%), 1.4 (1%) — todos modernos
- **Multi-coluna:** ~40% (concentrado em votos/decisões); PyMuPDF
  `get_text("text")` já lida bem com reading order
- **Conclusão:** **OCR é desnecessário**, parser único PyMuPDF basta

### Tamanho do corpus completo (extrapolado da amostra)

| Métrica | Amostra (294) | Corpus completo (~26.785 PDFs) |
|---|---|---|
| Páginas | 1.159 | **~106 mil** |
| Texto bruto | 2.3 MB | **~205 MB** (~50M tokens) |
| Média p/ PDF | 3.9 páginas / 7.7k chars | — |

### Heterogeneidade por tipo de ato → chunking em 3 tiers

A prevalência de marcadores hierárquicos varia drasticamente. Isso pede
estratégia diferente por categoria.

**Tier A — denso jurídico, alta prioridade RAG**

Documentos onde a unidade natural de recuperação é o **artigo** ou o **anexo**.

| Tipo | n | pgs avg | Art% | §% | Inciso% | Anexo% | Tabela% |
|---|---|---|---|---|---|---|---|
| REN (Resolução Normativa) | 2 | 3.5 | 100 | 100 | 100 | 100 | 0 |
| REH (Resolução Homologatória) | 6 | 6.7 | 100 | 100 | 83 | 100 | 67 |
| RES (Resolução) | 5 | 5.4 | 100 | 100 | 80 | 20 | 0 |
| NREH (Nota Técnica REH) | 2 | 31 | 100 | 50 | 100 | 100 | 50 |
| NDSP (Nota Técnica DSP) | 9 | 7.4 | 100 | 89 | 89 | 89 | 56 |
| INA (Instrução Administrativa) | 2 | 11 | 100 | 100 | 100 | 100 | 0 |

**Tier B — médio, prosa decisória**

Votos, decisões e portarias. Sem hierarquia regulamentar pura mas com
estrutura de parágrafos/seções.

| Tipo | n | pgs avg | Art% | §% | Inciso% |
|---|---|---|---|---|---|
| AREA / ADSP / APRT (votos+decisões) | 81 | ~6 | 87-89 | 0-73 | 77-83 |
| REA (Resolução Autorizativa) | 32 | 2.7 | 100 | 88 | 66 |
| PRT (Portaria) | 28 | 2.4 | 100 | 64 | 14 |

**Tier C — curto, baixa relevância regulatória**

Atos administrativos pontuais. 1 PDF cabe inteiro em 1 chunk.

| Tipo | n | pgs avg | Estrutura |
|---|---|---|---|
| DSP (Despacho) | 89 | 1.5 | 35% Art, 30% Anexo |
| ECP/ECT/EDT/AVS/ACP/ATS (extratos, avisos) | 18 | 1-2 | quase sem hierarquia |

### Recomendações para o parser (próxima etapa)

1. **Parser único:** PyMuPDF (`fitz`). Sem LayoutParser, Donut, Unstructured.
2. **Tabelas:** para tipos com `Tabela% ≥ 50%` (NDSP, NREH, REH, EMDSP,
   AREN, ALEL) usar `page.find_tables()` e serializar como markdown antes
   de chunkar — caso contrário células viram lixo dentro do chunk.
3. **Pré-processamento universal:**
   - Strip do header recorrente `* A Nota Técnica é um documento emitido...`
   - Strip de numeração de página (`FL. X de Y`)
   - Colapso de quebras de linha intra-parágrafo
4. **Chunking 3-tier:**
   - **Tier A:** split por `r'^\s*Art\.?\s*\d+'` + `Anexo` separado. Se
     artigo > 1500 tokens, sub-split por `§`.
   - **Tier B:** split por parágrafo com merge até ~500 tokens, overlap 50.
   - **Tier C:** PDF inteiro = 1 chunk (já são <2k tokens). Considerar
     indexar em coleção secundária ou com peso reduzido — é
     ato administrativo, não regulamento.
5. **Metadado obrigatório por chunk** (já vem do `_manifest.jsonl`):
   `tipo_ato`, `numero`, `ano`, `titulo`, `url_origem`, `tier`,
   `n_artigo` (quando aplicável).

Relatório completo per-PDF fica em `pdfs_aneel/_analysis.json`.
