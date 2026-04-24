# Contexto da Tarefa — Download dos PDFs da Biblioteca ANEEL

## Objetivo

Construir um script Python robusto que leia os três JSONs de metadados da biblioteca legislativa da ANEEL, extraia as URLs dos PDFs e baixe todos eles para uma pasta local. Este download é a **etapa 1** de um projeto maior de RAG sobre legislação do setor elétrico brasileiro. Os PDFs serão depois parseados, chunked e indexados num vector store.

Esse script precisa ser **resiliente**: são ~18.688 documentos, o processo leva horas, e qualquer falha transiente (timeout, 503, conexão caindo) não pode invalidar o trabalho já feito. O script deve poder ser interrompido e retomado quantas vezes for necessário sem baixar nada duas vezes.

---

## Dados de entrada

### Arquivos JSON (fornecidos pelo usuário)

Três arquivos, um por ano, localizados num diretório que o usuário vai indicar (parametrizar via CLI):

- `biblioteca_aneel_gov_br_legislacao_2016_metadados.json`
- `biblioteca_aneel_gov_br_legislacao_2021_metadados.json`
- `biblioteca_aneel_gov_br_legislacao_2022_metadados.json`

### Estrutura dos JSONs

Cada arquivo é um dicionário onde a chave é uma data (`"YYYY-MM-DD"`) e o valor é um bloco contendo uma lista `registros`. Cada registro representa um ato normativo e pode conter uma lista `pdfs` com um ou mais arquivos associados.

Exemplo mínimo:

```json
{
  "2022-12-30": {
    "status": "...",
    "registros": [
      {
        "numeracaoItem": "1.",
        "titulo": "DSP - DESPACHO 021",
        "autor": "SPE/MME",
        "material": "Legislação",
        "esfera": "Esfera:Outros",
        "situacao": "Situação:NÃO CONSTA REVOGAÇÃO EXPRESSA",
        "assinatura": "Assinatura:28/12/2022",
        "publicacao": "Publicação:30/12/2022",
        "assunto": "Assunto:Indeferimento",
        "ementa": "Indefere o requerimento...",
        "pdfs": [
          {
            "tipo": "Texto Integral:",
            "url": "https://www2.aneel.gov.br/cedoc/dsp2022021spde.pdf",
            "arquivo": "dsp2022021spde.pdf",
            "baixado": true
          }
        ]
      }
    ]
  }
}
```

### Campos relevantes para esta tarefa

Por registro, interessam apenas os PDFs dentro da lista `pdfs`:

- `pdfs[].url` — URL completa para download
- `pdfs[].arquivo` — nome do arquivo sugerido (usar como filename local)
- `pdfs[].tipo` — tipo do PDF (texto integral, anexo, etc.); pode ser útil para separar

O campo `pdfs[].baixado` existente no JSON **NÃO é confiável** para o contexto local — ele reflete o estado do lado da ANEEL, não do nosso disco. A fonte de verdade sobre "já baixei" é a existência do arquivo local + o manifest (ver abaixo).

### Volume estimado

- 2016: ~4.269 documentos
- 2021: ~6.993 documentos
- 2022: ~7.426 documentos
- **Total: ~18.688 documentos**, cada um com 1+ PDFs (espere ~20.000 downloads no total)

---

## Estrutura de pastas desejada

```
<DOWNLOAD_ROOT>/
├── 2016/
│   ├── dsp2016001spde.pdf
│   ├── ren2016700.pdf
│   └── ...
├── 2021/
│   └── ...
├── 2022/
│   └── ...
├── _manifest.jsonl         # uma linha por tentativa de download (estado persistente)
├── _failures.jsonl         # URLs que falharam após esgotar retries (para reexecução)
└── _summary.json           # resumo final: total, sucesso, falha, pulados
```

### Manifest (`_manifest.jsonl`)

Append-only, uma linha JSON por tentativa bem-sucedida. Cada linha contém:

```json
{
  "url": "https://www2.aneel.gov.br/cedoc/dsp2022021spde.pdf",
  "local_path": "2022/dsp2022021spde.pdf",
  "size_bytes": 123456,
  "sha256": "abc123...",
  "downloaded_at": "2026-04-24T14:30:12Z",
  "http_status": 200,
  "ano_fonte": 2022,
  "doc_titulo": "DSP - DESPACHO 021"
}
```

Ao retomar a execução, o script **lê o manifest** para saber o que já foi baixado com sucesso. Não depende só da existência do arquivo no disco (arquivo pode estar corrompido / truncado de uma execução anterior).

### Failures (`_failures.jsonl`)

Mesmo formato, mas para downloads que falharam após esgotar retries. Contém `error`, `http_status`, `attempts`.

---

## Requisitos funcionais

### 1. Idempotência / retomada
- Se o script for interrompido e executado de novo, **não redownloadar** o que já está no manifest com sucesso
- Ao iniciar, carregar o manifest em memória como um set de URLs já concluídas
- Verificar também se o arquivo local existe e tem tamanho > 0; se estiver no manifest mas sumiu do disco, rebaixar

### 2. Paralelismo controlado
- Usar `asyncio` + `httpx` (ou `aiohttp`) com um semáforo limitando concorrência
- **Default: 8 downloads simultâneos** (parametrizar via `--concurrency`)
- O servidor da ANEEL (`www2.aneel.gov.br`) é legado e pode não aguentar muita pressão — conservador por padrão

### 3. Retries com backoff exponencial
- Tentar cada URL até **5 vezes**
- Backoff exponencial com jitter: `sleep = min(60, (2 ** attempt) + random.uniform(0, 1))`
- Retryable: timeouts, 5xx, `ConnectionError`
- Não retryable: 404, 403, 401 (registrar no failures direto)

### 4. Timeouts
- Connect timeout: 15s
- Read timeout: 60s (PDFs grandes)
- Total timeout por tentativa: 120s

### 5. Validação mínima de conteúdo
- Após o download, verificar que os primeiros bytes do arquivo batem com o magic number de PDF (`%PDF-`)
- Se não bater, tratar como falha e retentar (pode ser uma página de erro HTML disfarçada)
- Calcular SHA-256 e gravar no manifest

### 6. Deduplicação por URL
- O mesmo PDF pode aparecer em múltiplos registros (ementas que referenciam o mesmo anexo)
- Deduplicar a lista de URLs **antes** de começar os downloads
- Manter o mapeamento URL → [lista de doc_titulo/ano] para o manifest

### 7. Logging
- Usar `logging` com nível configurável via `--log-level`
- Progresso com `tqdm` (barra de progresso que funciona bem com asyncio via `tqdm.asyncio`)
- Log de erros detalhado num arquivo separado (`_errors.log`)

### 8. Politeness
- User-Agent identificável: `ANEEL-Legislation-Research-Bot/1.0 (contact: <configurável>)`
- Delay mínimo entre requests do mesmo worker: 100ms (evitar rajadas)
- Respeitar `Retry-After` em respostas 429 e 503

### 9. Organização por ano
- Extrair o ano a partir do nome do arquivo JSON de origem
- Salvar PDFs em subpasta do ano correspondente
- Se o mesmo PDF aparecer em JSONs de anos diferentes (raro mas possível), salvar uma vez só e registrar os múltiplos anos-fonte no manifest

---

## CLI esperada

```bash
python download_aneel_pdfs.py \
  --json-dir /caminho/para/jsons \
  --output-dir /caminho/para/pdfs_aneel \
  --concurrency 8 \
  --max-retries 5 \
  --log-level INFO
```

Flags opcionais úteis:

- `--dry-run`: lista quantos seriam baixados sem baixar
- `--only-year 2022`: limita a um ano específico (para testar)
- `--only-tipo "Texto Integral:"`: filtra por tipo de PDF
- `--resume`: default true; se false, ignora o manifest e rebaixa tudo
- `--max-downloads N`: limita a N downloads (útil para smoke test)

---

## Dependências sugeridas

```
httpx>=0.27
tqdm>=4.66
tenacity>=8.2        # (opcional, retries declarativos)
```

Se o Claude Code preferir `aiohttp` em vez de `httpx`, tudo bem — ambos servem. `httpx` tem uma API mais agradável e suporte nativo a HTTP/2, mas o servidor da ANEEL provavelmente só fala HTTP/1.1 mesmo.

---

## Entregáveis

1. **`download_aneel_pdfs.py`** — script principal, executável via CLI
2. **`requirements.txt`** — dependências mínimas
3. **`README.md`** curto explicando como rodar, retomar e consultar resultados

---

## O que NÃO fazer nesta etapa

- **Não parsear o conteúdo dos PDFs.** Parse/chunking/embedding vêm em etapas separadas
- **Não tentar consertar PDFs corrompidos.** Apenas registrar em `_failures.jsonl`
- **Não inserir em vector store.** Só download puro
- **Não criar interface web / notebook.** Script de linha de comando, só isso
- **Não fazer OCR.** Mesmo em PDFs escaneados, nesta etapa só baixa

---

## Critérios de sucesso

- Script roda de ponta a ponta sem intervenção manual
- Pode ser interrompido (Ctrl+C) e retomado sem perder progresso
- Ao final, `_summary.json` mostra: `total_urls`, `downloaded`, `failed`, `skipped_already_present`
- Taxa de sucesso esperada: >95%. Os ~5% de falha tendem a ser PDFs realmente indisponíveis no servidor da ANEEL — aceitável, vão ficar em `_failures.jsonl` para retry manual posterior
- Estrutura de pastas limpa, um PDF por arquivo, nomes estáveis (usar `arquivo` do JSON como filename)

---

## Contexto do projeto maior (informativo, não ação)

Este download alimenta um sistema RAG que vai:
- Parsear os PDFs com chunking por artigo/parágrafo
- Gerar embeddings usando `intfloat/multilingual-e5-large` e/ou `rufimelo/Legal-BERTimbau-sts-large-ma-v3`
- Aplicar a **Estratégia B de indexação**: prefixo contextual no texto que vai pro embedding (título + assunto do ato normativo), mas mantendo o texto original separado para exibição e envio ao LLM
- Usar retrieval híbrido BM25 + semântico com filtro de metadados apenas no lado semântico
- Reranker com `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`
- Geração com Claude e/ou Sabiá-3

Por isso, **preservar o mapeamento URL → metadados do registro de origem no manifest é importante** — na etapa de indexação, vou precisar casar cada PDF baixado com os metadados do JSON para fazer o enriquecimento dos chunks (Estratégia B).
