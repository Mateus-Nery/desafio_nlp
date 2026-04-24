"""
Download dos PDFs da Biblioteca Legislativa da ANEEL.

Etapa 1 do projeto RAG sobre legislação do setor elétrico brasileiro:
lê os 3 JSONs de metadados, deduplica URLs e baixa todos os PDFs com
controle de concorrência, retries com backoff exponencial, validação
de magic number e manifest JSONL para retomada.

Uso:
    python download_aneel_pdfs.py \\
        --json-dir dados_grupo_estudos \\
        --output-dir pdfs_aneel \\
        --concurrency 8

Smoke test:
    python download_aneel_pdfs.py \\
        --json-dir dados_grupo_estudos \\
        --output-dir pdfs_aneel \\
        --only-year 2022 --max-downloads 20
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import random
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.errors import RequestsError
from curl_cffi.curl import CurlError
from tqdm import tqdm


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

# O servidor da ANEEL (www2.aneel.gov.br) está atrás de Cloudflare Bot Management,
# que bloqueia requisições HTTP "puras" via fingerprint TLS (JA3). Usamos curl_cffi
# com impersonation de Chrome para passar pelo bloqueio. Como consequência o
# User-Agent enviado é o do Chrome real, não um custom — não há como ter os dois.
IMPERSONATE_PROFILE = "chrome"
PDF_MAGIC = b"%PDF-"
RETRYABLE_HTTP = {408, 425, 429, 500, 502, 503, 504}
NON_RETRYABLE_HTTP = {400, 401, 404, 410, 451}  # nota: 403 removido (CF transiente)
JSON_FILENAME_PATTERN = re.compile(r"_(\d{4})_metadados")
POLITENESS_DELAY_SECONDS = 0.1
MAX_BACKOFF_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 60.0


# ---------------------------------------------------------------------------
# Tipos
# ---------------------------------------------------------------------------


@dataclass
class Source:
    """Origem de uma URL: ano do JSON, título e tipo do PDF."""

    ano: int
    titulo: str
    tipo: str

    def to_dict(self) -> dict:
        return {"ano": self.ano, "titulo": self.titulo, "tipo": self.tipo}


@dataclass
class DownloadTask:
    """Uma URL a ser baixada (já dedupada). Ano de destino é a primeira ocorrência."""

    url: str
    arquivo: str
    ano_destino: int
    fontes: list[Source] = field(default_factory=list)


@dataclass
class DownloadResult:
    """Resultado de uma tentativa de download (sucesso ou falha definitiva)."""

    task: DownloadTask
    success: bool
    attempts: int
    http_status: Optional[int] = None
    # campos de sucesso
    local_path: Optional[str] = None  # relativo ao output-dir
    size_bytes: Optional[int] = None
    sha256: Optional[str] = None
    # campo de falha
    error: Optional[str] = None

    def to_manifest_entry(self) -> dict:
        return {
            "url": self.task.url,
            "local_path": self.local_path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "downloaded_at": datetime.now(timezone.utc).isoformat(),
            "http_status": self.http_status,
            "ano_destino": self.task.ano_destino,
            "fontes": [s.to_dict() for s in self.task.fontes],
        }

    def to_failure_entry(self) -> dict:
        return {
            "url": self.task.url,
            "error": self.error,
            "http_status": self.http_status,
            "attempts": self.attempts,
            "last_attempt_at": datetime.now(timezone.utc).isoformat(),
            "ano_destino": self.task.ano_destino,
            "arquivo": self.task.arquivo,
            "fontes": [s.to_dict() for s in self.task.fontes],
        }


# ---------------------------------------------------------------------------
# Carga e deduplicação dos JSONs
# ---------------------------------------------------------------------------


def _looks_like_pdf_url(url: str) -> bool:
    """True se a URL aponta para um arquivo .pdf (case-insensitive, ignora query)."""
    path = url.split("?", 1)[0].split("#", 1)[0]
    return path.lower().endswith(".pdf")


def load_tasks(
    json_dir: Path,
    only_year: Optional[int] = None,
    only_tipo: Optional[str] = None,
) -> tuple[list[DownloadTask], int]:
    """Lê os JSONs de metadados e produz a lista dedupada de tarefas de download.

    Itera os JSONs em ordem alfabética (ano crescente). Quando uma mesma URL
    aparece em anos diferentes, o ano de destino é o primeiro encontrado e os
    demais são registrados em ``fontes``.

    Filtra URLs que não terminam em ``.pdf`` (HTML/ZIP/XLSX/etc.) — esses
    formatos existem nos JSONs mas estão fora do escopo deste script (o
    pipeline RAG só processa PDFs). Retorna ``(tasks, n_skipped_non_pdf)``.
    """
    by_url: dict[str, DownloadTask] = {}
    skipped_non_pdf = 0
    json_paths = sorted(
        json_dir.glob("biblioteca_aneel_gov_br_legislacao_*_metadados.json")
    )
    if not json_paths:
        raise FileNotFoundError(
            f"Nenhum JSON 'biblioteca_aneel_gov_br_legislacao_*_metadados.json' em {json_dir}"
        )

    for json_path in json_paths:
        match = JSON_FILENAME_PATTERN.search(json_path.name)
        if not match:
            continue
        ano = int(match.group(1))
        if only_year is not None and ano != only_year:
            continue

        with json_path.open(encoding="utf-8") as fh:
            data = json.load(fh)

        for _date_key, bloco in data.items():
            if not isinstance(bloco, dict):
                continue
            for reg in bloco.get("registros", []) or []:
                titulo = (reg.get("titulo") or "").strip()
                for pdf in reg.get("pdfs", []) or []:
                    url = (pdf.get("url") or "").strip()
                    arquivo = (pdf.get("arquivo") or "").strip()
                    tipo = (pdf.get("tipo") or "").strip()
                    if not url or not arquivo:
                        continue
                    if only_tipo is not None and tipo != only_tipo:
                        continue
                    if not _looks_like_pdf_url(url):
                        skipped_non_pdf += 1
                        continue
                    # Cloudflare bloqueia HTTP puro (sem TLS handshake = sem
                    # fingerprint pra impersonar). Forçamos HTTPS — o servidor
                    # responde nos dois esquemas com o mesmo conteúdo.
                    if url.startswith("http://"):
                        url = "https://" + url[len("http://") :]
                    src = Source(ano=ano, titulo=titulo, tipo=tipo)
                    if url in by_url:
                        by_url[url].fontes.append(src)
                    else:
                        by_url[url] = DownloadTask(
                            url=url,
                            arquivo=arquivo,
                            ano_destino=ano,
                            fontes=[src],
                        )

    return list(by_url.values()), skipped_non_pdf


# ---------------------------------------------------------------------------
# Amostragem estratificada
# ---------------------------------------------------------------------------

# Extrai o "tipo de ato" do início do título (ex: "DSP - DESPACHO 3716/2022" -> "DSP").
# Os títulos seguem o padrão "<SIGLA> - <NOME COMPLETO> <NÚMERO>/<ANO>".
_TIPO_ATO_PATTERN = re.compile(r"^\s*([A-Z]{2,5})\b")


def _stratify_key(task: DownloadTask) -> tuple[int, str]:
    """Chave de estratificação: (ano de destino, tipo de ato extraído do título).

    Garante que uma amostra de X% mantém a mesma proporção de DSP / PRT / REN /
    REA / etc. e a mesma distribuição por ano que o corpus completo.
    """
    titulo = task.fontes[0].titulo if task.fontes else ""
    match = _TIPO_ATO_PATTERN.match(titulo)
    tipo_ato = match.group(1) if match else "OUTRO"
    return (task.ano_destino, tipo_ato)


def stratified_sample(
    tasks: list[DownloadTask],
    fraction: float,
    seed: int,
) -> tuple[list[DownloadTask], dict[tuple[int, str], tuple[int, int]]]:
    """Amostra estratificada por (ano, tipo_de_ato), com seed fixa.

    - Reprodutível: mesma seed + mesmo corpus = mesma amostra.
    - Estratos pequenos (< 1/fraction itens) retêm pelo menos 1 item.
    - Ordena por URL antes de embaralhar pra eliminar dependência do dict order.

    Retorna ``(tasks_amostradas, stats_por_estrato)`` onde stats mapeia
    ``(ano, tipo) -> (n_total_no_estrato, n_amostrado)``.
    """
    if not (0.0 < fraction <= 1.0):
        raise ValueError(f"fraction deve estar em (0, 1], recebido: {fraction}")

    rng = random.Random(seed)
    by_stratum: dict[tuple[int, str], list[DownloadTask]] = defaultdict(list)
    for t in tasks:
        by_stratum[_stratify_key(t)].append(t)

    sampled: list[DownloadTask] = []
    stats: dict[tuple[int, str], tuple[int, int]] = {}
    for stratum, items in sorted(by_stratum.items()):
        items.sort(key=lambda x: x.url)  # ordem determinística antes do shuffle
        rng.shuffle(items)
        n_take = max(1, round(len(items) * fraction))
        sampled.extend(items[:n_take])
        stats[stratum] = (len(items), n_take)
    return sampled, stats


# ---------------------------------------------------------------------------
# Manifest I/O
# ---------------------------------------------------------------------------


def load_completed_urls(manifest_path: Path, output_dir: Path) -> set[str]:
    """Lê o manifest e retorna o set de URLs com download válido (arquivo presente e > 0)."""
    if not manifest_path.exists():
        return set()

    completed: dict[str, str] = {}
    with manifest_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            url = entry.get("url")
            local_path = entry.get("local_path")
            if not url or not local_path:
                continue
            completed[url] = local_path  # última entrada ganha em caso de duplicata

    valid: set[str] = set()
    for url, rel_path in completed.items():
        full_path = output_dir / rel_path
        try:
            if full_path.is_file() and full_path.stat().st_size > 0:
                valid.add(url)
        except OSError:
            pass
    return valid


async def manifest_writer(
    queue: asyncio.Queue,
    manifest_path: Path,
    failures_path: Path,
    stats: dict,
) -> None:
    """Corotina única que serializa escritas no manifest e failures.

    Termina quando recebe ``None`` na fila.
    """
    # Abre em modo append (binário não é necessário, JSONL é texto)
    with manifest_path.open("a", encoding="utf-8") as mf, failures_path.open(
        "a", encoding="utf-8"
    ) as ff:
        while True:
            result: Optional[DownloadResult] = await queue.get()
            try:
                if result is None:
                    return
                if result.success:
                    mf.write(json.dumps(result.to_manifest_entry(), ensure_ascii=False) + "\n")
                    mf.flush()
                    stats["downloaded"] = stats.get("downloaded", 0) + 1
                else:
                    ff.write(json.dumps(result.to_failure_entry(), ensure_ascii=False) + "\n")
                    ff.flush()
                    stats["failed"] = stats.get("failed", 0) + 1
            finally:
                queue.task_done()


# ---------------------------------------------------------------------------
# Helpers de retry
# ---------------------------------------------------------------------------


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parseia o header Retry-After (segundos ou HTTP-date)."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = (dt - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delta)
    except (TypeError, ValueError):
        return None


async def sleep_with_backoff(attempt: int, retry_after: Optional[float] = None) -> None:
    """Sleep entre retries. Usa Retry-After se fornecido, senão backoff exponencial com jitter."""
    if retry_after is not None:
        sleep_for = min(MAX_BACKOFF_SECONDS, retry_after)
    else:
        sleep_for = min(MAX_BACKOFF_SECONDS, (2 ** attempt) + random.uniform(0, 1))
    await asyncio.sleep(sleep_for)


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


async def download_one(
    session: AsyncSession,
    task: DownloadTask,
    sem: asyncio.Semaphore,
    queue: asyncio.Queue,
    errors_logger: logging.Logger,
    max_retries: int,
    output_dir: Path,
) -> None:
    """Baixa uma URL com retries. Coloca o resultado (sucesso ou falha) na fila.

    PDFs são pequenos (mediana ~80 KB, máximo conhecido alguns MB), então
    baixamos o body inteiro em memória, validamos magic number e só então
    gravamos no disco via .tmp + rename atômico. Mais simples que streaming
    incremental e suficiente para o tamanho real dos arquivos.
    """
    async with sem:
        local_rel = f"{task.ano_destino}/{task.arquivo}"
        local_path = output_dir / str(task.ano_destino) / task.arquivo
        tmp_path = local_path.with_suffix(local_path.suffix + ".tmp")

        last_status: Optional[int] = None
        last_error: Optional[str] = None
        attempts_done = 0

        for attempt in range(1, max_retries + 1):
            attempts_done = attempt
            try:
                resp = await session.get(
                    task.url,
                    impersonate=IMPERSONATE_PROFILE,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                    allow_redirects=True,
                )
                last_status = resp.status_code

                if resp.status_code in NON_RETRYABLE_HTTP:
                    last_error = f"HTTP {resp.status_code} (não-retryable)"
                    errors_logger.warning(
                        "Não-retryable %s em %s", resp.status_code, task.url
                    )
                    break

                if resp.status_code != 200:
                    last_error = f"HTTP {resp.status_code}"
                    errors_logger.warning(
                        "Tentativa %d/%d falhou (HTTP %s) em %s",
                        attempt,
                        max_retries,
                        resp.status_code,
                        task.url,
                    )
                    if attempt < max_retries:
                        retry_after = parse_retry_after(resp.headers.get("retry-after"))
                        await sleep_with_backoff(attempt, retry_after)
                    continue

                # 200 OK — body em memória; valida magic antes de persistir
                body = resp.content
                if not body.startswith(PDF_MAGIC):
                    last_error = f"Magic inválido: {body[:8]!r}"
                    errors_logger.warning(
                        "Magic inválido (tent. %d/%d) em %s: %r (len=%d)",
                        attempt,
                        max_retries,
                        task.url,
                        body[:8],
                        len(body),
                    )
                    if attempt < max_retries:
                        await sleep_with_backoff(attempt)
                    continue

                try:
                    with tmp_path.open("wb") as out:
                        out.write(body)
                except OSError as e:
                    last_error = f"Erro de I/O ao gravar: {e}"
                    errors_logger.warning(
                        "Falha de I/O (tent. %d/%d) em %s: %s",
                        attempt,
                        max_retries,
                        task.url,
                        e,
                    )
                    _safe_unlink(tmp_path)
                    if attempt < max_retries:
                        await sleep_with_backoff(attempt)
                    continue

                os.replace(tmp_path, local_path)
                sha = hashlib.sha256(body).hexdigest()

                # Politeness: pequeno delay após sucesso
                await asyncio.sleep(POLITENESS_DELAY_SECONDS)

                await queue.put(
                    DownloadResult(
                        task=task,
                        success=True,
                        attempts=attempt,
                        http_status=200,
                        local_path=local_rel,
                        size_bytes=len(body),
                        sha256=sha,
                    )
                )
                return

            except (RequestsError, CurlError) as e:
                last_error = f"{type(e).__name__}: {e}"
                errors_logger.warning(
                    "Exceção retryable (tent. %d/%d) em %s: %s",
                    attempt,
                    max_retries,
                    task.url,
                    last_error,
                )
                _safe_unlink(tmp_path)
                if attempt < max_retries:
                    await sleep_with_backoff(attempt)
            except asyncio.CancelledError:
                _safe_unlink(tmp_path)
                raise
            except Exception as e:  # noqa: BLE001 — log e segue como retryable
                last_error = f"{type(e).__name__}: {e}"
                errors_logger.exception("Exceção inesperada em %s", task.url)
                _safe_unlink(tmp_path)
                if attempt < max_retries:
                    await sleep_with_backoff(attempt)

        # Esgotou retries (ou caiu em non-retryable)
        _safe_unlink(tmp_path)
        await queue.put(
            DownloadResult(
                task=task,
                success=False,
                attempts=attempts_done,
                http_status=last_status,
                error=last_error or "desconhecido",
            )
        )


def _safe_unlink(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Orquestração
# ---------------------------------------------------------------------------


def setup_logging(output_dir: Path, level: str) -> tuple[logging.Logger, logging.Logger]:
    """Configura logging de console + arquivo de erros separado."""
    log_level = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(log_level)
    # Limpa handlers prévios (re-execução em REPL etc.)
    for h in list(root.handlers):
        root.removeHandler(h)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(log_level)
    console.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    )
    root.addHandler(console)

    log = logging.getLogger("aneel-dl")

    errors_logger = logging.getLogger("aneel-dl.errors")
    errors_logger.setLevel(logging.WARNING)
    errors_logger.propagate = False
    fh = logging.FileHandler(output_dir / "_errors.log", encoding="utf-8")
    fh.setLevel(logging.WARNING)
    fh.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    errors_logger.addHandler(fh)

    return log, errors_logger


async def run(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    json_dir = Path(args.json_dir)
    if not json_dir.is_dir():
        print(f"ERRO: --json-dir não existe: {json_dir}", file=sys.stderr)
        return 2

    log, errors_logger = setup_logging(output_dir, args.log_level)

    log.info("Carregando tarefas de %s", json_dir)
    tasks, skipped_non_pdf = load_tasks(json_dir, args.only_year, args.only_tipo)
    log.info(
        "%d URLs únicas (.pdf) após dedup; %d não-PDF puladas (html/zip/xlsx/etc.)",
        len(tasks),
        skipped_non_pdf,
    )

    # Amostra estratificada (opcional). Aplicada ANTES do filtro de completed_urls
    # pra que o conjunto amostrado seja determinístico pela seed, independente do
    # estado do manifest. Assim, retomar uma amostra parcial ou rodar o corpus
    # completo depois reaproveita os PDFs já baixados.
    if args.sample_fraction is not None:
        before = len(tasks)
        tasks, sample_stats = stratified_sample(
            tasks, args.sample_fraction, args.sample_seed
        )
        log.info(
            "Amostra estratificada: %d -> %d URLs (%.2f%%) [seed=%d, %d estratos]",
            before,
            len(tasks),
            (len(tasks) / before * 100) if before else 0.0,
            args.sample_seed,
            len(sample_stats),
        )
        if log.isEnabledFor(logging.DEBUG):
            for (ano, tipo), (total, taken) in sorted(sample_stats.items()):
                log.debug("  %d/%s: %d/%d", ano, tipo, taken, total)

    # Cria pastas por ano de destino
    for ano in {t.ano_destino for t in tasks}:
        (output_dir / str(ano)).mkdir(exist_ok=True)

    manifest_path = output_dir / "_manifest.jsonl"
    failures_path = output_dir / "_failures.jsonl"

    completed_urls: set[str] = set()
    if args.resume:
        completed_urls = load_completed_urls(manifest_path, output_dir)
        log.info("%d já completas no manifest (com arquivo presente)", len(completed_urls))
    else:
        log.warning("--no-resume: ignorando manifest existente")

    pending = [t for t in tasks if t.url not in completed_urls]
    log.info("%d pendentes para download", len(pending))

    if args.dry_run:
        log.info("DRY RUN. Saindo sem baixar nada.")
        return 0

    if args.max_downloads is not None:
        pending = pending[: args.max_downloads]
        log.info("Limitando a %d downloads (--max-downloads)", len(pending))

    if not pending:
        log.info("Nada a baixar.")
        _write_summary(output_dir, len(tasks), len(completed_urls), 0, {"downloaded": 0, "failed": 0}, None, None)
        return 0

    started_at = datetime.now(timezone.utc)
    queue: asyncio.Queue = asyncio.Queue()
    sem = asyncio.Semaphore(args.concurrency)
    stats: dict = {"downloaded": 0, "failed": 0}

    pbar = tqdm(total=len(pending), desc="PDFs", unit="pdf")

    def _bump_progress(_fut: asyncio.Future) -> None:
        pbar.update(1)

    # max_clients controla pool de conexões internas do curl_cffi.
    # Damos um pequeno headroom acima do semáforo de concorrência.
    async with AsyncSession(max_clients=max(args.concurrency * 2, 16)) as session:
        writer_task = asyncio.create_task(
            manifest_writer(queue, manifest_path, failures_path, stats)
        )

        download_tasks: list[asyncio.Task] = []
        for t in pending:
            dt = asyncio.create_task(
                download_one(session, t, sem, queue, errors_logger, args.max_retries, output_dir)
            )
            dt.add_done_callback(_bump_progress)
            download_tasks.append(dt)

        try:
            await asyncio.gather(*download_tasks, return_exceptions=False)
        except (KeyboardInterrupt, asyncio.CancelledError):
            log.warning("Interrompido. Cancelando downloads pendentes...")
            for dt in download_tasks:
                if not dt.done():
                    dt.cancel()
            await asyncio.gather(*download_tasks, return_exceptions=True)
        finally:
            pbar.close()
            # finaliza writer (drena fila)
            await queue.put(None)
            await writer_task

    ended_at = datetime.now(timezone.utc)
    _write_summary(output_dir, len(tasks), len(completed_urls), len(pending), stats, started_at, ended_at)

    log.info(
        "Resumo: %d baixados, %d falharam em %.0fs",
        stats["downloaded"],
        stats["failed"],
        (ended_at - started_at).total_seconds(),
    )
    return 0


def _write_summary(
    output_dir: Path,
    total_urls: int,
    already_present: int,
    pending_at_start: int,
    stats: dict,
    started_at: Optional[datetime],
    ended_at: Optional[datetime],
) -> None:
    summary = {
        "total_urls": total_urls,
        "already_present": already_present,
        "pending_at_start": pending_at_start,
        "downloaded": stats.get("downloaded", 0),
        "failed": stats.get("failed", 0),
        "started_at": started_at.isoformat() if started_at else None,
        "ended_at": ended_at.isoformat() if ended_at else None,
        "duration_seconds": (
            (ended_at - started_at).total_seconds()
            if started_at and ended_at
            else None
        ),
    }
    with (output_dir / "_summary.json").open("w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Download dos PDFs da biblioteca legislativa da ANEEL (etapa 1 do RAG).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--json-dir", required=True, help="Pasta com os 3 JSONs de metadados.")
    p.add_argument("--output-dir", required=True, help="Destino dos PDFs e arquivos de controle.")
    p.add_argument("--concurrency", type=int, default=8, help="Downloads simultâneos.")
    p.add_argument("--max-retries", type=int, default=5, help="Tentativas por URL.")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--dry-run", action="store_true", help="Lista quantos seriam baixados e sai.")
    p.add_argument("--only-year", type=int, default=None, help="Limita a um ano (2016/2021/2022).")
    p.add_argument("--only-tipo", default=None, help='Filtra pelo tipo do PDF (ex: "Texto Integral:").')
    p.add_argument(
        "--resume",
        dest="resume",
        action="store_true",
        default=True,
        help="Pula URLs já presentes no manifest com arquivo válido (default).",
    )
    p.add_argument(
        "--no-resume",
        dest="resume",
        action="store_false",
        help="Ignora o manifest e tenta baixar tudo de novo.",
    )
    p.add_argument("--max-downloads", type=int, default=None, help="Limita N downloads (smoke test).")
    p.add_argument(
        "--sample-fraction",
        type=float,
        default=None,
        help=(
            "Amostra aleatória estratificada por (ano, tipo de ato). "
            "Ex: 0.1 = 10%% do corpus, mantendo proporções. "
            "Reprodutível via --sample-seed."
        ),
    )
    p.add_argument(
        "--sample-seed",
        type=int,
        default=42,
        help="Seed da amostra (reprodutibilidade). Default 42.",
    )
    return p


def main() -> int:
    args = build_arg_parser().parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        # Última camada — asyncio.run já tenta fazer cleanup
        print("\nInterrompido pelo usuário.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
