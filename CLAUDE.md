# Instruções para Claude (e colaboradores)

Este projeto é um **pipeline RAG sobre legislação ANEEL** (26.731 PDFs). Dois colaboradores trabalham em paralelo, ambos usando Claude Code em worktrees separados. As regras abaixo existem para evitar conflitos e perda de contexto.

---

## Ao iniciar uma sessão — leitura obrigatória (nessa ordem)

1. **`HANDOFF.md`** — descobre quem está mexendo no quê AGORA, quais fases estão livres, decisões em aberto.
2. **`CHANGELOG.md`** — vê o que mudou recentemente em narrativa humana.
3. **`git log -10 --stat`** — confirma o estado real do repo (HANDOFF/CHANGELOG podem estar desatualizados em segundos).
4. **`README.md`** — só se for uma sessão fresca sobre arquitetura geral.

## Antes de começar a mexer em uma fase

- Verifica em `HANDOFF.md` se a fase está `(livre)` ou tem owner ativo
- Se livre: anuncia em `HANDOFF.md` (`> @nome`) ANTES de começar a codar — evita que o outro Claude pegue a mesma fase
- Se ocupada: escolhe outra ou conversa com o humano

## Antes de cada commit — obrigatório

1. Adiciona uma entrada nova **no topo** de `CHANGELOG.md` (não sobrescreve as anteriores).
   - Formato: `## (não commitado) — <título da mudança>` — após o commit, atualizar o `(não commitado)` para o hash curto.
2. Atualiza `HANDOFF.md` se mudou estado de uma fase (começou, pausou, terminou, descobriu bloqueador).
3. Não commita arquivos em `artifacts/` (gitignored — são gerados pelo pipeline).

## Após o commit

- Edita `CHANGELOG.md` substituindo `(não commitado)` pelo hash curto e data.
- Faz commit pequeno separado só com essa atualização (`chore: atualiza CHANGELOG com hash`) ou inclui no próximo commit.

---

## Convenções de código

- **Token estimation:** `chars / 4` (consistente com `parse_pdfs.n_tokens_est`)
- **Hard cap de chunk:** 1500 tokens (margem confortável p/ contexto de 8k do bge-m3)
- **URLs ANEEL:** `https://www2.aneel.gov.br/cedoc/{filename}`
- **Saídas do pipeline:** sempre em `artifacts/` (gitignored)
- **Logs:** usar `logging` (não `print`); formato `%(asctime)s [%(levelname)s] %(message)s`
- **CLI:** todos os módulos em `src/` rodam via `python -m src.<modulo> --help`

## Arquitetura (resumo — detalhes em `README.md`)

```
download → parser → chunking → indexação (embeddings + Qdrant + BM25)
                              → retrieval (hybrid + RRF + rerank)
                              → geração (Claude Sonnet 4.6)
                              → avaliação (Ragas + golden set)
```

## Modelo de comunicação

- **`CHANGELOG.md`** → narrativa do que já aconteceu (append-only, histórico legível)
- **`HANDOFF.md`** → estado VIVO: WIP, owners, decisões em aberto, bloqueadores
- **`git log`** → fonte de verdade técnica
- **Memory do Claude** → preferências do humano e contexto de longo prazo (não substitui os arquivos acima — outros Claudes não enxergam memory cruzada)
