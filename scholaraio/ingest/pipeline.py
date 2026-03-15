"""
pipeline.py — 可组合步骤流水线
================================

步骤（scope）：
  inbox  — 每个 PDF 依次执行：mineru → extract → dedup → ingest
  papers — 每篇已入库论文执行：toc → l3
  global — 全局执行一次：index

预设：
  full    = mineru, extract, dedup, ingest, toc, l3, embed, index
  ingest  = mineru, extract, dedup, ingest, embed, index
  enrich  = toc, l3, embed, index
  reindex = embed, index

用法（CLI）：
  scholaraio pipeline full
  scholaraio pipeline enrich --force
  scholaraio pipeline --steps toc,l3
  scholaraio pipeline full --dry-run
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from scholaraio.config import Config
from scholaraio.log import ui
from scholaraio.metrics import timer

_log = logging.getLogger(__name__)


# ============================================================================
#  Step registration
# ============================================================================


class StepResult(Enum):
    """流水线步骤返回值。"""

    OK = "ok"
    SKIP = "skip"
    FAIL = "fail"


@dataclass
class StepDef:
    """流水线步骤定义。

    Attributes:
        fn: 步骤执行函数。
        scope: 作用域，``"inbox"`` | ``"papers"`` | ``"global"``。
        desc: 步骤描述（用于 ``--list`` 输出）。
    """

    fn: Callable
    scope: str
    desc: str


@dataclass
class InboxCtx:
    """Inbox 步骤间传递的单文件上下文。

    Attributes:
        pdf_path: 原始 PDF 路径，md-only 入库时为 ``None``。
        inbox_dir: inbox 目录路径。
        papers_dir: 已入库论文目录路径。
        existing_dois: 已入库论文的 DOI → JSON 路径映射（用于去重）。
        cfg: 全局配置。
        opts: 运行选项（dry_run, no_api, force 等）。
        pending_dir: 无 DOI 论文的待审目录。
        md_path: Markdown 文件路径（MinerU 输出或直接放入）。
        meta: 提取后的 :class:`~scholaraio.ingest.metadata.PaperMetadata`。
        status: 当前状态，``"pending"`` | ``"ingested"`` | ``"duplicate"``
            | ``"needs_review"`` | ``"failed"`` | ``"skipped"``。
    """

    pdf_path: Path | None
    inbox_dir: Path
    papers_dir: Path
    existing_dois: dict[str, Path]
    cfg: Config
    opts: dict[str, Any]

    pending_dir: Path | None = None
    md_path: Path | None = None
    meta: Any = None  # PaperMeta instance after extraction
    status: str = "pending"  # pending | ingested | duplicate | needs_review | failed | skipped
    ingested_json: Path | None = None  # set by step_ingest on success
    is_thesis: bool = False  # thesis inbox or LLM-detected thesis


# ============================================================================
#  Inbox steps
# ============================================================================


def step_office_convert(ctx: InboxCtx) -> StepResult:
    """Office 文档（DOCX / XLSX / PPTX）→ Markdown 转换（MarkItDown）。

    仅当 ``ctx.opts["office_path"]`` 存在时执行（由 ``_process_inbox`` 在扫描
    Office 文件时注入；非 Office 文件入口时 ``office_path`` 不存在，步骤直接跳过）。
    已有同名 ``.md`` 时跳过转换并直接使用已有文件。

    Args:
        ctx: Inbox 上下文，转换后 ``ctx.md_path`` 指向生成的 ``.md``。

    Returns:
        ``StepResult.OK`` 成功, ``StepResult.FAIL`` 失败。
    """
    office_path: Path | None = ctx.opts.get("office_path")
    if office_path is None:
        # Not an office file entry — skip this step
        return StepResult.OK

    md_path = ctx.inbox_dir / (office_path.stem + ".md")

    if md_path.exists():
        _log.debug(".md exists, skipping office convert: %s", md_path.name)
        ctx.md_path = md_path
        return StepResult.OK

    if ctx.opts.get("dry_run"):
        _log.debug("would convert office: %s -> %s", office_path.name, md_path.name)
        ctx.md_path = md_path
        return StepResult.OK

    try:
        from markitdown import MarkItDown
    except ImportError:
        _log.error("MarkItDown 未安装，无法转换 Office 文件。请运行: pip install 'markitdown[docx,pptx,xlsx]'")
        ctx.status = "failed"
        return StepResult.FAIL

    try:
        md_obj = MarkItDown()
        result = md_obj.convert(str(office_path))
        md_text = result.text_content or ""
        if not md_text.strip():
            _log.warning("Office 文件内容为空: %s", office_path.name)
        md_path.write_text(md_text, encoding="utf-8")
        ctx.md_path = md_path
        _log.debug("office convert OK: %s -> %s", office_path.name, md_path.name)
        return StepResult.OK
    except Exception as exc:
        _log.error("MarkItDown 转换失败 %s: %s", office_path.name, exc)
        ctx.status = "failed"
        return StepResult.FAIL


def step_mineru(ctx: InboxCtx) -> StepResult:
    """PDF → Markdown 转换（MinerU）。

    md-only 入库项（无 PDF）自动跳过。已有同名 ``.md`` 时也跳过。
    本地 MinerU 不可达时自动 fallback 到云 API（需配置 ``mineru_api_key``）。
    超长 PDF（超过 ``chunk_page_limit`` 页）自动切分后逐段转换再合并。

    Args:
        ctx: Inbox 上下文，转换后 ``ctx.md_path`` 指向生成的 ``.md``。

    Returns:
        ``StepResult.OK`` 成功, ``StepResult.FAIL`` 失败。
    """
    from scholaraio.ingest.mineru import (
        ConvertOptions,
        _convert_long_pdf,
        _convert_long_pdf_cloud,
        _get_pdf_page_count,
        check_server,
        convert_pdf,
    )

    # md-only entry (no PDF): skip MinerU entirely
    if ctx.pdf_path is None:
        if ctx.md_path and (ctx.md_path.exists() or ctx.opts.get("dry_run")):
            _log.debug("no PDF, using existing .md: %s", ctx.md_path.name)
            return StepResult.OK
        _log.error("no PDF and no .md")
        ctx.status = "failed"
        return StepResult.FAIL

    pdf_path = ctx.pdf_path
    md_path = ctx.inbox_dir / (pdf_path.stem + ".md")

    if md_path.exists():
        _log.debug(".md exists, skipping MinerU: %s", md_path.name)
        ctx.md_path = md_path
        return StepResult.OK

    if ctx.opts.get("dry_run"):
        _log.debug("would convert: %s -> %s", pdf_path.name, md_path.name)
        ctx.md_path = md_path
        return StepResult.OK

    mineru_opts = ConvertOptions(
        api_url=ctx.cfg.ingest.mineru_endpoint,
        output_dir=ctx.inbox_dir,
    )

    chunk_limit = getattr(ctx.cfg.ingest, "chunk_page_limit", 100)
    page_count = _get_pdf_page_count(pdf_path)
    is_long = page_count > chunk_limit

    if is_long:
        ui(f"Long PDF detected ({page_count} pages > {chunk_limit} limit), splitting...")

    # Try local MinerU first, fallback to cloud API
    if check_server(ctx.cfg.ingest.mineru_endpoint):
        if is_long:
            result = _convert_long_pdf(pdf_path, mineru_opts, chunk_size=chunk_limit)
        else:
            result = convert_pdf(pdf_path, mineru_opts)
    else:
        api_key = ctx.cfg.resolved_mineru_api_key()
        if not api_key:
            _log.error("MinerU unreachable and no cloud API key")
            ctx.status = "failed"
            return StepResult.FAIL
        from scholaraio.ingest.mineru import convert_pdf_cloud

        _log.debug("local MinerU unreachable, using cloud API")
        if is_long:
            result = _convert_long_pdf_cloud(
                pdf_path,
                mineru_opts,
                api_key=api_key,
                cloud_url=ctx.cfg.ingest.mineru_cloud_url,
                chunk_size=chunk_limit,
            )
        else:
            result = convert_pdf_cloud(
                pdf_path,
                mineru_opts,
                api_key=api_key,
                cloud_url=ctx.cfg.ingest.mineru_cloud_url,
            )

    if not result.success:
        _log.error("MinerU failed: %s", result.error)
        ctx.status = "failed"
        return StepResult.FAIL

    ctx.md_path = result.md_path or md_path
    return StepResult.OK


def step_extract_doc(ctx: InboxCtx) -> StepResult:
    """从非论文文档提取/生成元数据（LLM 生成标题和摘要）。

    对于缺少标题/摘要的普通文档，使用 LLM 从全文生成，确保检索可用。

    Args:
        ctx: Inbox 上下文，需要 ``ctx.md_path`` 已设置。

    Returns:
        ``StepResult.OK`` 成功, ``StepResult.FAIL`` 失败。
    """
    if ctx.opts.get("dry_run"):
        _log.debug("would extract document metadata from: %s", ctx.md_path.name if ctx.md_path else "?")
        return StepResult.OK

    if not ctx.md_path or not ctx.md_path.exists():
        _log.error("extract_doc failed: no .md file")
        ctx.status = "failed"
        return StepResult.FAIL

    from scholaraio.ingest.metadata._doc_extract import extract_document_metadata

    try:
        meta = extract_document_metadata(ctx.md_path, ctx.cfg)
    except Exception as e:
        _log.error("document extraction failed: %s", e)
        ctx.status = "failed"
        return StepResult.FAIL

    if not (meta.title or "").strip():
        _log.error("cannot determine document title")
        ctx.status = "failed"
        return StepResult.FAIL

    meta.paper_type = meta.paper_type or "document"
    ctx.meta = meta
    ui(f"Title: {meta.title[:80]}")
    ui(f"Type: {meta.paper_type} | Author: {meta.first_author or '?'} | Year: {meta.year or '?'}")
    return StepResult.OK


def step_extract(ctx: InboxCtx) -> StepResult:
    """从 Markdown 头部提取论文元数据。

    使用配置指定的提取器（regex/auto/robust/llm），结果存入 ``ctx.meta``。

    Args:
        ctx: Inbox 上下文，需要 ``ctx.md_path`` 已设置。

    Returns:
        ``StepResult.OK`` 成功, ``StepResult.FAIL`` 失败。
    """
    from scholaraio.ingest.extractor import get_extractor

    if ctx.opts.get("dry_run"):
        _log.debug("would extract metadata from: %s", ctx.md_path.name if ctx.md_path else "?")
        return StepResult.OK

    if not ctx.md_path or not ctx.md_path.exists():
        _log.error("extract failed: no .md file")
        ctx.status = "failed"
        return StepResult.FAIL

    extractor = get_extractor(ctx.cfg)
    meta = extractor.extract(ctx.md_path)
    ui(f"Title: {(meta.title or '?')[:80]}")
    ui(f"Author: {meta.first_author_lastname or '?'} | Year: {meta.year or '?'} | DOI: {meta.doi or 'none'}")
    ctx.meta = meta
    return StepResult.OK


def step_dedup(ctx: InboxCtx) -> StepResult:
    """API 查询补全 + DOI 去重检查。

    1. 调用 Crossref / S2 / OpenAlex API 补全元数据
    2. 有 DOI 时检查是否与已入库论文重复
    3. 无 DOI 时：thesis inbox 标记直接放行；否则依次 LLM 判断是否 thesis / book
    4. 无 DOI 且非 thesis/book 才转入 ``data/pending/``

    Args:
        ctx: Inbox 上下文，需要 ``ctx.meta`` 已设置。

    Returns:
        ``StepResult.OK`` 通过, ``StepResult.FAIL`` 重复/无 DOI。
    """
    from scholaraio.ingest.metadata import enrich_metadata

    if ctx.opts.get("dry_run"):
        _log.debug("would check dedup and query APIs")
        return StepResult.OK

    if ctx.meta is None:
        _log.error("dedup failed: no metadata")
        ctx.status = "failed"
        return StepResult.FAIL

    # Thesis inbox: set paper_type, skip API query and DOI dedup
    if ctx.is_thesis:
        ctx.meta.paper_type = "thesis"
        _log.debug("thesis inbox, skipping API and dedup")
        ui(f"Thesis: {ctx.meta.title or '?'}")
        return StepResult.OK

    # API query
    if not ctx.opts.get("no_api"):
        _log.debug("querying APIs")
        ctx.meta = enrich_metadata(ctx.meta)
        ui(f"DOI (after API): {ctx.meta.doi or 'none'}")
    else:
        ctx.meta.extraction_method = "local_only"
        _log.debug("skipping API query (offline mode)")

    # DOI dedup (guard against LLM returning "null"/"None" strings)
    doi = ctx.meta.doi
    if doi and doi.strip().lower() in ("null", "none", "n/a"):
        ctx.meta.doi = ""
        doi = ""
    if not doi or not doi.strip():
        # No DOI -> LLM thesis detection
        if _detect_thesis(ctx):
            ctx.meta.paper_type = "thesis"
            ctx.is_thesis = True
            ui("Detected as thesis, ingesting without DOI")
            return StepResult.OK
        # No DOI -> LLM book detection
        if _detect_book(ctx):
            ctx.meta.paper_type = "book"
            ui("Detected as book, ingesting without DOI")
            return StepResult.OK
        # Not thesis/book -> move to pending
        _log.debug("no DOI and not thesis/book, moving to pending")
        _move_to_pending(ctx)
        ctx.status = "needs_review"
        return StepResult.FAIL

    doi_key = ctx.meta.doi.lower().strip()
    if doi_key in ctx.existing_dois:
        existing_json = ctx.existing_dois[doi_key]
        existing_md = existing_json.parent / "paper.md"
        if not existing_md.exists() and ctx.md_path and ctx.md_path.exists():
            # MD missing from existing paper: restore it automatically
            pdf_stem = ctx.pdf_path.stem if ctx.pdf_path else ""
            md_stem = ctx.md_path.stem if ctx.md_path else ""
            shutil.move(str(ctx.md_path), str(existing_md))
            _log.debug("duplicate (MD missing, restored): %s", existing_md.name)
            _repair_abstract(existing_json, existing_md, ctx.cfg)
            _cleanup_inbox(ctx.pdf_path, None, dry_run=False)
            _cleanup_assets(ctx.inbox_dir, pdf_stem, md_stem)
        else:
            # Normal duplicate: move to pending for user review
            _log.debug("duplicate: DOI %s exists -> %s", ctx.meta.doi, existing_json.parent.name)
            _move_to_pending(
                ctx,
                issue="duplicate",
                message="DOI 与已入库论文重复，如需覆盖请手动处理",
                extra={"duplicate_of": existing_json.parent.name, "doi": doi_key},
            )
        ctx.status = "duplicate"
        return StepResult.FAIL

    return StepResult.OK


def step_ingest(ctx: InboxCtx) -> StepResult:
    """将论文正式写入 ``data/papers/``。

    生成标准化文件名 ``{LastName}-{year}-{Title}``，
    写入 JSON 元数据，移动 ``.md`` 文件，清理 inbox。

    Args:
        ctx: Inbox 上下文，需要 ``ctx.meta`` 和 ``ctx.md_path`` 已设置。

    Returns:
        ``StepResult.OK`` 成功, ``StepResult.FAIL`` 失败。
    """
    from scholaraio.ingest.metadata import generate_new_stem, write_metadata_json
    from scholaraio.papers import generate_uuid

    if ctx.opts.get("dry_run"):
        _log.debug("would ingest paper to papers_dir")
        ctx.status = "ingested"
        return StepResult.OK

    if ctx.meta is None:
        _log.error("ingest failed: missing meta")
        ctx.status = "failed"
        return StepResult.FAIL

    if not (ctx.meta.title or "").strip() and not (ctx.meta.abstract or "").strip():
        _log.error("ingest failed: no title and no abstract")
        ui("跳过：无标题且无摘要，无法入库")
        ctx.status = "failed"
        return StepResult.FAIL

    # Abstract fallback: extract from MD when API didn't return one
    if not ctx.meta.abstract and ctx.md_path and ctx.md_path.exists():
        from scholaraio.ingest.metadata import extract_abstract_from_md

        abstract = extract_abstract_from_md(ctx.md_path, ctx.cfg)
        if abstract:
            ctx.meta.abstract = abstract
            _log.debug("abstract backfilled from MD (%d chars)", len(abstract))

    papers_dir = ctx.papers_dir
    papers_dir.mkdir(parents=True, exist_ok=True)
    new_stem = generate_new_stem(ctx.meta)

    # Assign UUID
    ctx.meta.id = generate_uuid()

    # Create per-paper directory
    paper_d = papers_dir / new_stem
    suffix = 2
    while paper_d.exists():
        paper_d = papers_dir / f"{new_stem}-{suffix}"
        suffix += 1

    paper_d.mkdir(parents=True)
    new_json = paper_d / "meta.json"

    write_metadata_json(ctx.meta, new_json)

    if ctx.md_path and ctx.md_path.exists():
        new_md = paper_d / "paper.md"
        shutil.move(str(ctx.md_path), str(new_md))
        # Move MinerU assets (images, layout.json, etc.) if present
        md_stem = ctx.md_path.stem if ctx.md_path else ""
        pdf_stem = ctx.pdf_path.stem if ctx.pdf_path else ""
        _move_assets(ctx.inbox_dir, paper_d, pdf_stem or md_stem, md_stem)
        ui(f"Ingested: {paper_d.name}/")
        ui("  meta.json + paper.md")
    else:
        ui(f"Ingested (metadata only): {paper_d.name}/")
        ui("  meta.json")

    if ctx.meta.doi and ctx.meta.doi.strip():
        ctx.existing_dois[ctx.meta.doi.lower().strip()] = new_json

    # Update papers_registry immediately so UUID lookup works before rebuild
    _update_registry(ctx.cfg, ctx.meta, paper_d.name)

    _cleanup_inbox(ctx.pdf_path, None, dry_run=False)
    # Clean up original Office source file (DOCX/XLSX/PPTX) if present
    office_src: Path | None = ctx.opts.get("office_path")
    if office_src and office_src.exists():
        try:
            office_src.unlink()
            _log.debug("deleted office source: %s", office_src.name)
        except OSError as exc:
            _log.warning("could not delete office source %s: %s", office_src.name, exc)
    ctx.ingested_json = new_json
    ctx.status = "ingested"
    return StepResult.OK


# ============================================================================
#  Papers steps
# ============================================================================


def step_toc(json_path: Path, cfg: Config, opts: dict) -> StepResult:
    """LLM 提取 TOC 写入 JSON（papers 作用域封装）。

    Args:
        json_path: 论文 JSON 路径（meta.json）。
        cfg: 全局配置。
        opts: 运行选项。

    Returns:
        ``StepResult.OK`` 成功, ``StepResult.FAIL`` 失败, ``StepResult.SKIP`` 跳过。
    """
    from scholaraio.loader import enrich_toc

    md_path = json_path.parent / "paper.md"
    if not md_path.exists():
        _log.debug("skipping (no paper.md): %s", json_path.parent.name)
        return StepResult.SKIP

    if opts.get("dry_run"):
        _log.debug("would run toc: %s", json_path.stem)
        return StepResult.OK

    ok = enrich_toc(
        json_path,
        md_path,
        cfg,
        force=opts.get("force", False),
        inspect=opts.get("inspect", False),
    )
    return StepResult.OK if ok else StepResult.FAIL


def step_l3(json_path: Path, cfg: Config, opts: dict) -> StepResult:
    """LLM 提取结论段写入 JSON（papers 作用域封装）。

    Args:
        json_path: 论文 JSON 路径（meta.json）。
        cfg: 全局配置。
        opts: 运行选项。

    Returns:
        ``StepResult.OK`` 成功, ``StepResult.FAIL`` 失败, ``StepResult.SKIP`` 跳过。
    """
    from scholaraio.loader import enrich_l3

    md_path = json_path.parent / "paper.md"
    if not md_path.exists():
        _log.debug("skipping (no paper.md): %s", json_path.parent.name)
        return StepResult.SKIP

    if opts.get("dry_run"):
        _log.debug("would run l3: %s", json_path.stem)
        return StepResult.OK

    ok = enrich_l3(
        json_path,
        md_path,
        cfg,
        force=opts.get("force", False),
        max_retries=opts.get("max_retries", 2),
        inspect=opts.get("inspect", False),
    )
    return StepResult.OK if ok else StepResult.FAIL


# ============================================================================
#  Global steps
# ============================================================================


def step_embed(papers_dir: Path, cfg: Config, opts: dict) -> StepResult:
    """生成语义向量写入 index.db（global 作用域）。

    Args:
        papers_dir: 论文目录。
        cfg: 全局配置。
        opts: 运行选项。

    Returns:
        ``StepResult.OK``；缺少 embed 依赖时跳过并返回 ``StepResult.SKIP``。
    """
    try:
        from scholaraio.vectors import build_vectors
    except ImportError:
        ui("跳过 embed 步骤：缺少依赖，安装: pip install scholaraio[embed]")
        return StepResult.SKIP

    db_path = cfg.index_db
    rebuild = opts.get("rebuild", False)

    if opts.get("dry_run"):
        _log.debug("would %s vectors: %s -> %s", "rebuild" if rebuild else "update", papers_dir, db_path)
        return StepResult.OK

    count = build_vectors(papers_dir, db_path, rebuild=rebuild, cfg=cfg)
    ui(f"Vector index done, {count} new.")
    return StepResult.OK


def step_index(papers_dir: Path, cfg: Config, opts: dict) -> StepResult:
    """更新 SQLite FTS5 索引（global 作用域）。

    Args:
        papers_dir: 论文目录。
        cfg: 全局配置。
        opts: 运行选项。

    Returns:
        ``StepResult.OK``。
    """
    from scholaraio.index import build_index

    db_path = cfg.index_db
    rebuild = opts.get("rebuild", False)

    if opts.get("dry_run"):
        _log.debug("would %s index: %s -> %s", "rebuild" if rebuild else "update", papers_dir, db_path)
        return StepResult.OK

    ui(f"{'Rebuild' if rebuild else 'Update'} index: {papers_dir} -> {db_path}")
    count = build_index(papers_dir, db_path, rebuild=rebuild)
    ui(f"Index done, {count} papers.")
    return StepResult.OK


def step_refetch(json_path: Path, cfg: Config, opts: dict) -> StepResult:
    """重新查询 API 补全引用量等缺失字段（papers 作用域封装）。

    Args:
        json_path: 论文 JSON 路径。
        cfg: 全局配置。
        opts: 运行选项。

    Returns:
        ``StepResult.OK`` 有更新, ``StepResult.SKIP`` 跳过。
    """
    import json as _json

    data = _json.loads(json_path.read_text(encoding="utf-8"))
    doi = data.get("doi", "")
    cc = data.get("citation_count") or {}
    has_citations = bool(cc)

    if not doi:
        _log.debug("skipping (no DOI): %s", json_path.stem)
        return StepResult.SKIP

    if has_citations and not opts.get("force", False):
        return StepResult.SKIP

    if opts.get("dry_run"):
        _log.debug("would refetch: %s (doi=%s)", json_path.stem, doi)
        return StepResult.OK

    if opts.get("no_api"):
        _log.debug("skipping (--no-api): %s", json_path.stem)
        return StepResult.SKIP

    from scholaraio.ingest.metadata import refetch_metadata

    changed = refetch_metadata(json_path)
    if changed:
        _log.debug("updated: %s", json_path.stem)
    else:
        _log.debug("no change: %s", json_path.stem)
    return StepResult.OK if changed else StepResult.SKIP


# ============================================================================
#  Step registry & presets
# ============================================================================


STEPS: dict[str, StepDef] = {
    "office_convert": StepDef(
        fn=step_office_convert, scope="inbox", desc="Office 文档（DOCX/XLSX/PPTX）→ Markdown（MarkItDown）"
    ),
    "mineru": StepDef(fn=step_mineru, scope="inbox", desc="PDF → Markdown（MinerU）"),
    "extract": StepDef(fn=step_extract, scope="inbox", desc="Markdown → 元数据提取"),
    "extract_doc": StepDef(fn=step_extract_doc, scope="inbox", desc="文档 → LLM 元数据提取"),
    "dedup": StepDef(fn=step_dedup, scope="inbox", desc="API 查询 + DOI 去重"),
    "ingest": StepDef(fn=step_ingest, scope="inbox", desc="写入 data/papers/"),
    "toc": StepDef(fn=step_toc, scope="papers", desc="LLM 提取 TOC 写入 JSON"),
    "l3": StepDef(fn=step_l3, scope="papers", desc="LLM 提取结论写入 JSON"),
    "refetch": StepDef(fn=step_refetch, scope="papers", desc="重新查询 API 补全引用量等字段"),
    "embed": StepDef(fn=step_embed, scope="global", desc="生成语义向量写入 index.db"),
    "index": StepDef(fn=step_index, scope="global", desc="更新 SQLite FTS5 索引"),
}

# Document inbox uses a different step sequence (no DOI dedup).
# office_convert runs before mineru; for PDF entries it is a no-op (office_path not set).
_DOC_INBOX_STEPS = ["office_convert", "mineru", "extract_doc", "ingest"]

# Office formats scanned in any inbox when office_convert is in the step list.
# Regular inbox presets don't include office_convert, so Office files there are ignored.
_OFFICE_EXTENSIONS = (".docx", ".xlsx", ".pptx")

PRESETS: dict[str, list[str]] = {
    "full": ["mineru", "extract", "dedup", "ingest", "toc", "l3", "embed", "index"],
    "ingest": ["mineru", "extract", "dedup", "ingest", "embed", "index"],
    "enrich": ["toc", "l3", "embed", "index"],
    "reindex": ["embed", "index"],
}


# ============================================================================
#  Executor
# ============================================================================


def _process_inbox(
    inbox_dir: Path,
    papers_dir: Path,
    pending_dir: Path,
    existing_dois: dict[str, Path],
    inbox_steps: list[str],
    cfg: Config,
    opts: dict[str, Any],
    dry_run: bool,
    ingested_jsons: list[Path],
    *,
    is_thesis: bool = False,
) -> None:
    """处理单个 inbox 目录中的所有文件。

    Args:
        inbox_dir: inbox 目录路径。
        papers_dir: 已入库论文目录。
        pending_dir: 待审目录。
        existing_dois: 已入库 DOI 映射（会被原地更新）。
        inbox_steps: inbox 作用域步骤名列表。
        cfg: 全局配置。
        opts: 运行选项。
        dry_run: 是否预览模式。
        ingested_jsons: 新入库的 JSON 路径列表（会被原地追加）。
        is_thesis: 是否为 thesis inbox（跳过 DOI 去重，标记 paper_type）。
    """
    if not inbox_dir.exists():
        return

    label_prefix = "[thesis] " if is_thesis else ""

    entries: dict[str, dict[str, Path | None]] = {}
    for pdf in sorted(inbox_dir.glob("*.pdf")):
        entries.setdefault(pdf.stem, {"pdf": None, "md": None, "office": None})["pdf"] = pdf
    for md in sorted(inbox_dir.glob("*.md")):
        entries.setdefault(md.stem, {"pdf": None, "md": None, "office": None})["md"] = md
    # Scan Office files only when office_convert step is in the pipeline
    has_office_step = "office_convert" in inbox_steps
    if has_office_step:
        for ext in _OFFICE_EXTENSIONS:
            for office_file in sorted(inbox_dir.glob(f"*{ext}")):
                entries.setdefault(office_file.stem, {"pdf": None, "md": None, "office": None})["office"] = office_file

    if not entries:
        if not is_thesis:
            msg = "No PDF, .md, or Office file" if has_office_step else "No PDF or .md file"
            ui(f"{msg} in inbox: {inbox_dir}")
        return

    has_pdfs = any(e["pdf"] for e in entries.values())
    office_count = sum(1 for e in entries.values() if e.get("office") and not e["pdf"] and not e["md"])
    md_only_count = sum(1 for e in entries.values() if not e["pdf"] and e["md"])

    needs_mineru = has_pdfs and "mineru" in inbox_steps
    use_cloud_batch = False
    if needs_mineru and not dry_run:
        from scholaraio.ingest.mineru import check_server

        if not check_server(cfg.ingest.mineru_endpoint):
            if cfg.resolved_mineru_api_key():
                _log.debug("local MinerU unreachable, will use cloud API")
                use_cloud_batch = True
            else:
                _log.error("MinerU unreachable (local: %s, no cloud API key)", cfg.ingest.mineru_endpoint)
                sys.exit(1)

    extra_info = []
    if md_only_count:
        extra_info.append(f"{md_only_count} md-only")
    if office_count:
        extra_info.append(f"{office_count} Office")
    ui(f"{label_prefix}Found {len(entries)} items" + (f" ({', '.join(extra_info)})" if extra_info else ""))
    if not is_thesis:
        ui(f"data/papers/ has {len(existing_dois)} papers (by DOI)")

    # ---- Batch MinerU preflight (cloud only) ----
    mineru_time = 0.0
    long_pdf_stems: set[str] = set()  # stems of long PDFs excluded from batch
    if use_cloud_batch and needs_mineru and not dry_run:
        from scholaraio.ingest.mineru import _get_pdf_page_count

        chunk_limit = getattr(cfg.ingest, "chunk_page_limit", 100)
        pdfs_to_convert = []
        for e in entries.values():
            pdf = e["pdf"]
            if not pdf or (inbox_dir / (pdf.stem + ".md")).exists():
                continue
            # Exclude long PDFs from batch — they need chunk-based handling
            pc = _get_pdf_page_count(pdf)
            if pc > chunk_limit:
                long_pdf_stems.add(pdf.stem)
                _log.info("long PDF excluded from batch (%d pages): %s", pc, pdf.name)
                continue
            pdfs_to_convert.append(pdf)
        if pdfs_to_convert:
            from scholaraio.ingest.mineru import ConvertOptions, convert_pdfs_cloud_batch

            mineru_opts = ConvertOptions(output_dir=inbox_dir)
            t_batch_start = time.time()
            batch_results = convert_pdfs_cloud_batch(
                pdfs_to_convert,
                mineru_opts,
                api_key=cfg.resolved_mineru_api_key(),
                cloud_url=cfg.ingest.mineru_cloud_url,
                batch_size=cfg.ingest.mineru_batch_size,
            )
            mineru_time = time.time() - t_batch_start
            # Move namespaced assets back to per-stem structure
            for br in batch_results:
                did = br.pdf_path.stem
                # Rename <data_id>_images → images dir for this stem
                namespaced_images = inbox_dir / f"{did}_images"
                if namespaced_images.is_dir():
                    target = inbox_dir / f"{did}_mineru_images"
                    namespaced_images.rename(target)
                if not br.success:
                    _log.error("MinerU batch failed for %s: %s", br.pdf_path.name, br.error)
            # Update entries with generated .md paths
            for stem, e in entries.items():
                md_check = inbox_dir / (stem + ".md")
                if md_check.exists() and e["md"] is None:
                    e["md"] = md_check

    # ---- Per-file pipeline (remaining steps, or all steps if local MinerU) ----
    # If batch MinerU was used, skip mineru step per-file (md already exists)
    # BUT keep mineru for long PDFs that were excluded from batch
    per_file_steps = inbox_steps
    batch_skip_mineru = use_cloud_batch and "mineru" in per_file_steps
    if batch_skip_mineru and not long_pdf_stems:
        per_file_steps = [s for s in per_file_steps if s != "mineru"]

    has_api = "dedup" in per_file_steps and not dry_run and not opts.get("no_api") and not is_thesis
    api_delay = 2.0 if has_api else 0

    stats: dict[str, int] = {"ingested": 0, "duplicate": 0, "needs_review": 0, "failed": 0, "skipped": 0}
    step_times: dict[str, float] = {}
    if mineru_time:
        step_times["mineru"] = mineru_time
    sorted_entries = sorted(entries.items())
    for idx, (stem, paths) in enumerate(sorted_entries):
        office_path = paths.get("office")
        if paths["pdf"]:
            file_label = paths["pdf"].name
            file_type = "PDF"
        elif paths["md"]:
            # Prefer .md over Office when both exist (Office file will still be cleaned up)
            file_label = paths["md"].name
            file_type = "MD"
        elif office_path:
            file_label = office_path.name
            file_type = office_path.suffix.lstrip(".").upper()
        else:
            file_label = paths["md"].name
            file_type = "MD"
        ui(f"\n{label_prefix}[{idx + 1}/{len(sorted_entries)}] {file_type}: {file_label}")

        # For long PDFs excluded from batch, keep mineru step
        file_steps = per_file_steps
        if batch_skip_mineru and long_pdf_stems and stem in long_pdf_stems:
            file_steps = inbox_steps  # full steps including mineru
        elif batch_skip_mineru and long_pdf_stems and stem not in long_pdf_stems:
            file_steps = [s for s in per_file_steps if s != "mineru"]

        # Inject office_path for office-only entries (no PDF) so downstream steps can clean up the source file
        file_opts = dict(opts)
        if office_path and not paths["pdf"]:
            file_opts["office_path"] = office_path

        ctx = InboxCtx(
            pdf_path=paths["pdf"],
            inbox_dir=inbox_dir,
            papers_dir=papers_dir,
            existing_dois=existing_dois,
            cfg=cfg,
            opts=file_opts,
            pending_dir=pending_dir,
            md_path=paths["md"],
            is_thesis=is_thesis,
        )
        for step_name in file_steps:
            with timer(f"pipeline.inbox.{step_name}", "step") as t:
                result = STEPS[step_name].fn(ctx)
            step_times[step_name] = step_times.get(step_name, 0) + t.elapsed
            _log.debug("%s: %.1fs", step_name, t.elapsed)
            if result != StepResult.OK:
                break

        final_status = ctx.status if ctx.status != "pending" else "skipped"
        stats[final_status] += 1
        if final_status == "ingested" and ctx.ingested_json:
            ingested_jsons.append(ctx.ingested_json)

        if api_delay and idx < len(sorted_entries) - 1:
            time.sleep(api_delay)

    # Clean up stray MinerU artifacts left in inbox
    for pattern in ["*_layout.json", "*_content_list.json", "*_origin.pdf", "layout.json"]:
        for stray in list(inbox_dir.glob(pattern)):
            stray.unlink(missing_ok=True)
            _log.debug("stray cleanup: %s", stray.name)
    for stray_dir in list(inbox_dir.glob("*_mineru_images")):
        if stray_dir.is_dir():
            shutil.rmtree(stray_dir)
            _log.debug("stray cleanup dir: %s", stray_dir.name)

    ui(
        f"\n{label_prefix}inbox done: {stats['ingested']} ingested | {stats['duplicate']} duplicate | {stats['needs_review']} review | {stats['failed']} failed | {stats['skipped']} skipped"
    )
    if step_times:
        ui("Step timing:")
        for sn, st in step_times.items():
            ui(f"  {sn:12s} {st:6.1f}s")
        ui(f"  {'total':12s} {sum(step_times.values()):6.1f}s")


def run_pipeline(
    step_names: list[str],
    cfg: Config,
    opts: dict[str, Any],
) -> None:
    """执行指定步骤序列。

    按 scope 分三阶段依次执行:
      1. **inbox** — 逐个文件: mineru → extract → dedup → ingest
      2. **papers** — 逐篇已入库论文: toc → l3
      3. **global** — 全局执行一次: embed → index

    Args:
        step_names: 步骤名称列表，如 ``["extract", "dedup", "ingest"]``。
            可用步骤见 :data:`STEPS`。
        cfg: 全局配置。
        opts: 运行选项字典，支持的键:

            - ``dry_run`` (bool): 预览模式，不写文件。
            - ``no_api`` (bool): 跳过外部 API 查询。
            - ``force`` (bool): 强制重新处理（toc/l3）。
            - ``inspect`` (bool): 展示处理详情。
            - ``max_retries`` (int): l3 最大重试次数。
            - ``rebuild`` (bool): 重建索引（index/embed）。
            - ``inbox_dir`` (Path): 自定义 inbox 目录。
            - ``papers_dir`` (Path): 自定义 papers 目录。
    """
    # Validate steps
    for name in step_names:
        if name not in STEPS:
            _log.error("unknown step '%s'. available: %s", name, ", ".join(STEPS))
            sys.exit(1)

    inbox_dir: Path = opts.get("inbox_dir", cfg._root / "data/inbox")
    papers_dir: Path = opts.get("papers_dir", cfg.papers_dir)
    pending_dir: Path = cfg._root / "data" / "pending"

    inbox_steps = [n for n in step_names if STEPS[n].scope == "inbox"]
    papers_steps = [n for n in step_names if STEPS[n].scope == "papers"]
    global_steps = [n for n in step_names if STEPS[n].scope == "global"]

    dry_run = opts.get("dry_run", False)
    ingested_jsons: list[Path] = []  # track newly ingested papers

    # ---- Inbox scope ----
    if inbox_steps:
        existing_dois = _collect_existing_dois(papers_dir)

        # Process regular inbox
        _result = _process_inbox(
            inbox_dir,
            papers_dir,
            pending_dir,
            existing_dois,
            inbox_steps,
            cfg,
            opts,
            dry_run,
            ingested_jsons,
            is_thesis=False,
        )

        # Process thesis inbox (data/inbox-thesis/)
        thesis_inbox = cfg._root / "data" / "inbox-thesis"
        if thesis_inbox.exists():
            _process_inbox(
                thesis_inbox,
                papers_dir,
                pending_dir,
                existing_dois,
                inbox_steps,
                cfg,
                opts,
                dry_run,
                ingested_jsons,
                is_thesis=True,
            )

        # Process document inbox (data/inbox-doc/)
        doc_inbox = cfg._root / "data" / "inbox-doc"
        if doc_inbox.exists():
            # Documents use extract_doc + ingest (skip dedup/API queries)
            doc_steps = [s for s in _DOC_INBOX_STEPS if s in STEPS]
            _process_inbox(
                doc_inbox,
                papers_dir,
                pending_dir,
                existing_dois,
                doc_steps,
                cfg,
                opts,
                dry_run,
                ingested_jsons,
                is_thesis=False,
            )

    # ---- Papers scope ----
    if papers_steps:
        if inbox_steps and ingested_jsons:
            # Only enrich newly ingested papers, not the whole library
            json_paths = sorted(ingested_jsons)
            ui(f"\nRunning {', '.join(papers_steps)} on {len(json_paths)} new papers")
        elif inbox_steps and not ingested_jsons:
            # Inbox ran but nothing was ingested — skip papers scope
            json_paths = []
        else:
            # No inbox steps (e.g. `pipeline enrich`) — process all
            from scholaraio.papers import iter_paper_dirs

            json_paths = sorted(d / "meta.json" for d in iter_paper_dirs(papers_dir))
        if not json_paths:
            if not inbox_steps:
                ui(f"No papers in: {papers_dir}")
        else:
            ok_total = fail_total = skip_total = 0
            step_times: dict[str, float] = {}

            # Concurrent execution for papers_steps when LLM-bound steps
            # (toc, l3) are present. All papers_steps run per-paper inside
            # _process_one_paper(); different papers execute in parallel.
            llm_steps = {"toc", "l3"}
            has_llm_steps = bool(set(papers_steps) & llm_steps)
            workers = cfg.llm.concurrency if has_llm_steps else 1
            if has_llm_steps and cfg.llm.backend == "codex-cli":
                workers = min(workers, 4)

            def _process_one_paper(json_path: Path) -> tuple[str, dict[str, float]]:
                """Process all papers_steps for one paper. Returns (status, timings)."""
                paper_ok = True
                paper_skipped = False
                timings: dict[str, float] = {}
                for step_name in papers_steps:
                    with timer(f"pipeline.papers.{step_name}", "step") as t:
                        result = STEPS[step_name].fn(json_path, cfg, opts)
                    timings[step_name] = t.elapsed
                    if result == StepResult.SKIP:
                        _log.debug("%s: skipped", step_name)
                        paper_skipped = True
                    elif result == StepResult.FAIL:
                        _log.debug("%s: %.1fs FAIL", step_name, t.elapsed)
                        paper_ok = False
                    else:
                        _log.debug("%s: %.1fs OK", step_name, t.elapsed)
                if paper_skipped and paper_ok:
                    return "skip", timings
                return ("ok" if paper_ok else "fail"), timings

            if workers > 1 and len(json_paths) > 1:
                from concurrent.futures import ThreadPoolExecutor, as_completed

                ui(f"  (concurrency: {workers} workers)")
                with ThreadPoolExecutor(max_workers=min(workers, len(json_paths))) as pool:
                    futures = {pool.submit(_process_one_paper, jp): jp for jp in json_paths}
                    for done_count, fut in enumerate(as_completed(futures), 1):
                        jp = futures[fut]
                        try:
                            status, timings = fut.result()
                        except Exception:
                            _log.exception("paper failed: %s", jp.parent.name)
                            status, timings = "fail", {}
                        ui(f"  [{done_count}/{len(json_paths)}] {jp.parent.name} [{status}]")
                        if status == "skip":
                            skip_total += 1
                        elif status == "ok":
                            ok_total += 1
                        else:
                            fail_total += 1
                        for sn, st in timings.items():
                            step_times[sn] = step_times.get(sn, 0) + st
            else:
                for json_path in json_paths:
                    ui(f"\n{json_path.parent.name}")
                    try:
                        status, timings = _process_one_paper(json_path)
                    except Exception:
                        _log.exception("paper failed: %s", json_path.parent.name)
                        status, timings = "fail", {}
                    if status == "skip":
                        skip_total += 1
                    elif status == "ok":
                        ok_total += 1
                    else:
                        fail_total += 1
                    for sn, st in timings.items():
                        step_times[sn] = step_times.get(sn, 0) + st

            ui(f"\nPapers done: {ok_total} ok | {fail_total} failed | {skip_total} skipped")
            if step_times:
                ui("Step timing:")
                for sn, st in step_times.items():
                    ui(f"  {sn:12s} {st:6.1f}s")
                ui(f"  {'total':12s} {sum(step_times.values()):6.1f}s")

    # ---- Global scope ----
    for step_name in global_steps:
        with timer(f"pipeline.global.{step_name}", "step") as t:
            STEPS[step_name].fn(papers_dir, cfg, opts)
        _log.debug("%s: %.1fs", step_name, t.elapsed)


def import_external(
    records: list,
    cfg: Config,
    *,
    pdf_paths: list[Path | None] | None = None,
    no_api: bool = False,
    dry_run: bool = False,
) -> dict[str, int]:
    """从外部来源（Endnote 等）批量导入论文。

    对每条记录运行 dedup + ingest，最后一次性 embed + index。
    如提供 ``pdf_paths``（与 records 索引对齐），入库时自动复制
    PDF 到论文目录。

    Args:
        records: PaperMetadata 列表。
        cfg: 全局配置。
        pdf_paths: 与 records 对齐的 PDF 路径列表（可选）。
        no_api: 跳过 API 查询。
        dry_run: 预览模式。

    Returns:
        统计字典 ``{"ingested": N, "duplicate": N, "needs_review": N, "failed": N, "skipped": N}``。
    """
    papers_dir = cfg.papers_dir
    pending_dir = cfg._root / "data" / "pending"
    existing_dois = _collect_existing_dois(papers_dir)

    opts: dict[str, Any] = {"dry_run": dry_run, "no_api": no_api}
    stats: dict[str, int] = {"ingested": 0, "duplicate": 0, "needs_review": 0, "failed": 0, "skipped": 0}
    ingested_jsons: list[Path] = []

    has_api = not no_api and not dry_run

    for idx, meta in enumerate(records):
        ui(f"\n[{idx + 1}/{len(records)}] {meta.title[:60]}...")

        # Fast DOI dedup check before expensive API calls
        doi = meta.doi.lower().strip() if meta.doi else ""
        if doi and doi in existing_dois:
            ui(f"DOI 重复，跳过: {meta.doi}")
            stats["duplicate"] += 1
            continue

        ctx = InboxCtx(
            pdf_path=None,
            inbox_dir=cfg._root / "data" / "inbox",  # not actually used
            papers_dir=papers_dir,
            existing_dois=existing_dois,
            cfg=cfg,
            opts=opts,
            pending_dir=pending_dir,
            md_path=None,
            meta=meta,
        )

        # Run dedup (API enrich + DOI check)
        result = step_dedup(ctx)
        if result != StepResult.OK:
            final_status = ctx.status if ctx.status != "pending" else "skipped"
            stats[final_status] += 1
            if has_api and idx < len(records) - 1:
                time.sleep(1.0)
            continue

        # Run ingest
        result = step_ingest(ctx)
        final_status = ctx.status if ctx.status != "pending" else "skipped"
        stats[final_status] += 1
        if final_status == "ingested" and ctx.ingested_json:
            ingested_jsons.append(ctx.ingested_json)

            # Copy PDF to paper directory if available
            pdf_src = pdf_paths[idx] if pdf_paths and idx < len(pdf_paths) else None
            if pdf_src and not dry_run:
                paper_d = ctx.ingested_json.parent
                shutil.copy2(str(pdf_src), str(paper_d / pdf_src.name))
                ui(f"  PDF: {pdf_src.name}")

        if has_api and idx < len(records) - 1:
            time.sleep(1.0)

    ui(
        f"\n导入完成: {stats['ingested']} 入库 | {stats['duplicate']} 重复 | {stats['needs_review']} 待审 | {stats['failed']} 失败"
    )

    # Batch embed + index
    if not dry_run and ingested_jsons:
        step_embed(papers_dir, cfg, {"dry_run": False, "rebuild": False})
        step_index(papers_dir, cfg, {"dry_run": False, "rebuild": False})

    return stats


# ============================================================================
#  Helpers
# ============================================================================


def batch_convert_pdfs(
    cfg: Config,
    *,
    enrich: bool = False,
) -> dict[str, int]:
    """批量转换已入库论文的 PDF 为 paper.md，可选 enrich。

    扫描 ``data/papers/`` 中有 PDF 无 paper.md 的论文，
    云端模式使用 ``convert_pdfs_cloud_batch()`` 真正批量转换，
    本地模式逐篇调用。转换后可选运行 toc + l3 + abstract backfill，
    最后一次性 embed + index。

    Args:
        cfg: 全局配置。
        enrich: 转换后是否运行 toc + l3 + abstract backfill。

    Returns:
        统计字典 ``{"converted": N, "failed": N, "skipped": N}``。
    """
    from scholaraio.papers import iter_paper_dirs

    # Collect papers with PDF but no paper.md
    to_convert: list[tuple[Path, Path]] = []  # (paper_dir, pdf_path)
    for pdir in iter_paper_dirs(cfg.papers_dir):
        if (pdir / "paper.md").exists():
            continue
        pdfs = list(pdir.glob("*.pdf"))
        if pdfs:
            to_convert.append((pdir, pdfs[0]))

    stats: dict[str, int] = {"converted": 0, "failed": 0, "skipped": 0}
    if not to_convert:
        ui("没有需要转换的 PDF")
        return stats

    from scholaraio.ingest.mineru import ConvertOptions, check_server

    use_local = check_server(cfg.ingest.mineru_endpoint)
    api_key = None
    if not use_local:
        api_key = cfg.resolved_mineru_api_key()
        if not api_key:
            ui("错误：MinerU 不可达且无云 API key，无法批量转换")
            return stats

    ui(f"\n开始批量转换 {len(to_convert)} 个 PDF...")

    converted_dirs: list[Path] = []

    if use_local:
        # Local MinerU: sequential single-file conversion
        from scholaraio.ingest.mineru import convert_pdf

        for idx, (pdir, pdf_path) in enumerate(to_convert):
            ui(f"[{idx + 1}/{len(to_convert)}] {pdir.name}")
            mineru_opts = ConvertOptions(
                api_url=cfg.ingest.mineru_endpoint,
                output_dir=pdir,
            )
            result = convert_pdf(pdf_path, mineru_opts)
            if not result.success:
                ui(f"  转换失败: {result.error}")
                stats["failed"] += 1
                continue

            _postprocess_convert(pdir, pdf_path, result)
            converted_dirs.append(pdir)
            stats["converted"] += 1
    else:
        # Cloud MinerU: true batch conversion via convert_pdfs_cloud_batch
        import tempfile

        from scholaraio.ingest.mineru import ConvertOptions, convert_pdfs_cloud_batch

        # Collect PDF paths; detect stem collisions (batch API uses stem as data_id)
        pdf_paths: list[Path] = []
        dir_map: dict[str, Path] = {}
        for pdir, pdf in to_convert:
            if pdf.stem in dir_map:
                _log.warning(
                    "PDF stem collision: %s in %s and %s, skipping latter", pdf.stem, dir_map[pdf.stem].name, pdir.name
                )
                stats["skipped"] += 1
                continue
            dir_map[pdf.stem] = pdir
            pdf_paths.append(pdf)

        with tempfile.TemporaryDirectory(prefix="scholaraio_batch_") as tmp:
            tmp_dir = Path(tmp)
            batch_opts = ConvertOptions(output_dir=tmp_dir)

            batch_results = convert_pdfs_cloud_batch(
                pdf_paths,
                batch_opts,
                api_key=api_key,
                cloud_url=cfg.ingest.mineru_cloud_url,
                batch_size=cfg.ingest.mineru_batch_size,
            )

            for br in batch_results:
                stem = br.pdf_path.stem
                pdir = dir_map.get(stem)
                if pdir is None:
                    _log.error("batch result stem %s not in dir_map", stem)
                    stats["failed"] += 1
                    continue

                if not br.success:
                    ui(f"  {pdir.name}: 转换失败: {br.error}")
                    stats["failed"] += 1
                    continue

                # Move .md to paper_dir/paper.md
                paper_md = pdir / "paper.md"
                if br.md_path and br.md_path.exists():
                    shutil.move(str(br.md_path), str(paper_md))

                # Move namespaced images: tmp/<stem>_images → paper_dir/images
                images_src = tmp_dir / f"{stem}_images"
                if images_src.is_dir():
                    images_dst = pdir / "images"
                    if images_dst.exists():
                        shutil.rmtree(str(images_dst))
                    shutil.move(str(images_src), str(images_dst))
                    # Fix image paths in markdown (data_id_images/ → images/)
                    if paper_md.exists():
                        md_text = paper_md.read_text(encoding="utf-8")
                        fixed = md_text.replace(f"{stem}_images/", "images/")
                        if fixed != md_text:
                            paper_md.write_text(fixed, encoding="utf-8")

                # Clean up source PDF (keep only markdown)
                pdf_path = br.pdf_path
                if pdf_path.exists() and pdf_path.parent == pdir and pdf_path.name != "paper.pdf":
                    pdf_path.unlink()

                ui(f"  {pdir.name}: OK")
                converted_dirs.append(pdir)
                stats["converted"] += 1

    ui(f"批量转换完成: {stats['converted']} 成功 / {stats['failed']} 失败 / {stats['skipped']} 跳过")

    # Post-processing: abstract backfill + optional enrich (toc + l3)
    if converted_dirs:
        _batch_postprocess(converted_dirs, cfg, enrich=enrich)

    return stats


def _postprocess_convert(pdir: Path, pdf_path: Path, result) -> None:
    """Post-process a single MinerU conversion result in paper_dir."""
    paper_md = pdir / "paper.md"

    # Move output to paper.md
    if result.md_path and result.md_path != paper_md:
        if paper_md.exists():
            paper_md.unlink()
        shutil.move(str(result.md_path), str(paper_md))

    # Clean up MinerU artifacts
    for pattern in ["*_layout.json", "*_content_list.json", "*_origin.pdf"]:
        for f in pdir.glob(pattern):
            f.unlink(missing_ok=True)
    for img_dir in pdir.glob("*_images"):
        if img_dir.name != "images" and img_dir.is_dir():
            target = pdir / "images"
            if target.exists():
                shutil.rmtree(target)
            img_dir.rename(target)

    # Clean up source PDF
    if pdf_path.exists() and pdf_path.name != "paper.pdf":
        pdf_path.unlink()


def _batch_postprocess(
    converted_dirs: list[Path],
    cfg: Config,
    *,
    enrich: bool = False,
) -> None:
    """Abstract backfill + optional toc/l3 enrich + embed/index for converted papers."""
    from scholaraio.papers import read_meta, write_meta

    # Abstract backfill
    backfilled = 0
    for pdir in converted_dirs:
        paper_md = pdir / "paper.md"
        if not paper_md.exists():
            continue
        try:
            data = read_meta(pdir)
            if not data.get("abstract"):
                from scholaraio.ingest.metadata import extract_abstract_from_md

                abstract = extract_abstract_from_md(paper_md, cfg)
                if abstract:
                    data["abstract"] = abstract
                    write_meta(pdir, data)
                    backfilled += 1
        except (ValueError, FileNotFoundError) as e:
            _log.debug("failed to backfill abstract for %s: %s", pdir.name, e)
    if backfilled:
        ui(f"Abstract 已补全: {backfilled} 篇")

    # Enrich: toc + l3
    if enrich:
        enriched = 0
        failed = 0
        opts: dict[str, Any] = {"dry_run": False, "force": False, "max_retries": 2}
        for pdir in converted_dirs:
            json_path = pdir / "meta.json"
            if not json_path.exists():
                continue
            ui(f"  enrich: {pdir.name}")
            toc_res = step_toc(json_path, cfg, opts)
            l3_res = step_l3(json_path, cfg, opts)
            if toc_res == StepResult.FAIL or l3_res == StepResult.FAIL:
                failed += 1
            else:
                enriched += 1
        ui(f"Enrich 完成: {enriched} ok | {failed} failed")

    # Re-embed + re-index once
    step_embed(cfg.papers_dir, cfg, {"dry_run": False, "rebuild": False})
    step_index(cfg.papers_dir, cfg, {"dry_run": False, "rebuild": False})


def _collect_existing_dois(papers_dir: Path) -> dict[str, Path]:
    from scholaraio.papers import iter_paper_dirs

    dois: dict[str, Path] = {}
    if not papers_dir.exists():
        return dois
    for pdir in iter_paper_dirs(papers_dir):
        json_path = pdir / "meta.json"
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            doi = data.get("doi") or (data.get("ids") or {}).get("doi")
            if doi and doi.strip():
                dois[doi.lower().strip()] = json_path
        except Exception as e:
            _log.debug("failed to read %s: %s", json_path.name, e)
    return dois


def _parse_detect_json(text: str) -> dict:
    """Tolerant JSON extraction from LLM response (handles fences/extra text)."""
    text = text.strip()
    # Strip ```json ... ``` fences
    m = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Try bare JSON object (greedy to handle nested braces)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {}


def _detect_thesis(ctx: InboxCtx) -> bool:
    """LLM 判断无 DOI 论文是否为学位论文。

    读取 MD 前 30000 字符，让 LLM 判断文档类型。
    LLM 不可用时退回 False（走 pending 流程）。

    Args:
        ctx: Inbox 上下文，需要 ``ctx.md_path`` 已设置。

    Returns:
        ``True`` 如果判定为 thesis/dissertation。
    """
    if not ctx.md_path or not ctx.md_path.exists():
        return False

    try:
        with open(ctx.md_path, encoding="utf-8") as f:
            text = f.read(30000)
    except Exception as e:
        _log.debug("failed to read md for thesis detection: %s", e)
        return False

    # Fast heuristic: title/metadata already hints thesis
    title = (ctx.meta.title or "").lower() if ctx.meta else ""
    for keyword in (
        "thesis",
        "dissertation",
        "学位论文",
        "硕士论文",
        "博士论文",
        "毕业论文",
        "master's thesis",
        "doctoral dissertation",
    ):
        if keyword in title:
            _log.debug("thesis detected by title keyword: %s", keyword)
            return True

    # LLM detection
    try:
        api_key = ctx.cfg.resolved_api_key()
    except Exception as e:
        _log.debug("failed to resolve API key: %s", e)
        api_key = None
    if not api_key:
        _log.debug("no LLM API key, skipping thesis detection")
        return False

    from scholaraio.metrics import call_llm

    prompt = (
        "Analyze the following document excerpt and determine if it is a "
        "thesis or dissertation (学位论文/硕士论文/博士论文/毕业论文). "
        "Look for indicators such as: degree awarding institution, "
        "advisor/supervisor, thesis committee, degree type (PhD/Master/Bachelor), "
        "declaration of originality, or thesis-specific formatting.\n\n"
        'Respond in JSON: {"is_thesis": true/false, "reason": "brief explanation"}\n\n'
        f"--- DOCUMENT START ---\n{text}\n--- DOCUMENT END ---"
    )
    try:
        result = call_llm(prompt, ctx.cfg, purpose="detect_thesis", max_tokens=200)
        data = _parse_detect_json(result.content)
        is_thesis = bool(data.get("is_thesis", False))
        if is_thesis:
            reason = data.get("reason", "")
            _log.debug("thesis detected by LLM: %s", reason)
        return is_thesis
    except Exception as e:
        _log.debug("thesis detection LLM call failed: %s", e)

    return False


def _detect_book(ctx: InboxCtx) -> bool:
    """LLM 判断无 DOI 论文是否为书籍/专著。

    读取 MD 前 30000 字符，让 LLM 判断文档类型。
    LLM 不可用时退回 False（走 pending 流程）。

    Args:
        ctx: Inbox 上下文，需要 ``ctx.md_path`` 已设置。

    Returns:
        ``True`` 如果判定为 book/monograph。
    """
    if not ctx.md_path or not ctx.md_path.exists():
        return False

    # Fast heuristic: paper_type already set by API (Crossref/S2/OpenAlex)
    _BOOK_TYPES = {"book", "monograph", "edited-book", "reference-book"}
    if ctx.meta and ctx.meta.paper_type and ctx.meta.paper_type.lower().strip() in _BOOK_TYPES:
        _log.debug("book detected by API paper_type: %s", ctx.meta.paper_type)
        return True

    # Fast heuristic: title keywords
    title = (ctx.meta.title or "").lower() if ctx.meta else ""
    for keyword in (
        "handbook",
        "textbook",
        "monograph",
        "专著",
        "教材",
        "手册",
    ):
        if keyword in title:
            _log.debug("book detected by title keyword: %s", keyword)
            return True

    try:
        with open(ctx.md_path, encoding="utf-8") as f:
            text = f.read(30000)
    except Exception as e:
        _log.debug("failed to read md for book detection: %s", e)
        return False

    # LLM detection
    try:
        api_key = ctx.cfg.resolved_api_key()
    except Exception as e:
        _log.debug("failed to resolve API key: %s", e)
        api_key = None
    if not api_key:
        _log.debug("no LLM API key, skipping book detection")
        return False

    from scholaraio.metrics import call_llm

    prompt = (
        "Analyze the following document excerpt and determine if it is a "
        "book or monograph (书籍/专著/教材/手册). "
        "Look for indicators such as: ISBN, publisher information, "
        "table of contents with chapters, preface/foreword, "
        "book-specific formatting (parts/chapters rather than sections), "
        "or multiple self-contained chapters with distinct topics.\n\n"
        'Respond in JSON: {"is_book": true/false, "reason": "brief explanation"}\n\n'
        f"--- DOCUMENT START ---\n{text}\n--- DOCUMENT END ---"
    )
    try:
        result = call_llm(prompt, ctx.cfg, purpose="detect_book", max_tokens=200)
        data = _parse_detect_json(result.content)
        is_book = bool(data.get("is_book", False))
        if is_book:
            reason = data.get("reason", "")
            _log.debug("book detected by LLM: %s", reason)
        return is_book
    except Exception as e:
        _log.debug("book detection LLM call failed: %s", e)

    return False


def _find_assets(inbox_dir: Path, asset_prefix: str, md_stem: str) -> tuple[Path | None, list[Path], list[Path]]:
    """Locate MinerU artifacts in inbox.

    Returns:
        (images_dir, json_files, origin_pdfs) — images_dir may be None.
    """
    images_dir = None
    for candidate in [
        inbox_dir / f"{asset_prefix}_mineru_images",
        inbox_dir / f"{md_stem}_mineru_images",
        inbox_dir / "images",
    ]:
        if candidate.is_dir():
            images_dir = candidate
            break
    json_files: list[Path] = []
    origin_pdfs: list[Path] = []
    for prefix in dict.fromkeys([asset_prefix, md_stem]):
        if not prefix:
            continue
        json_files.extend(inbox_dir.glob(f"{prefix}_*.json"))
        origin_pdfs.extend(inbox_dir.glob(f"{prefix}_*_origin.pdf"))
    return images_dir, json_files, origin_pdfs


def _move_assets(inbox_dir: Path, dest_dir: Path, asset_prefix: str, md_stem: str) -> None:
    """Move MinerU assets (images, layout.json, etc.) from inbox to dest."""
    images_dir, json_files, origin_pdfs = _find_assets(inbox_dir, asset_prefix, md_stem)
    if images_dir:
        shutil.move(str(images_dir), str(dest_dir / "images"))
    for f in json_files:
        prefix = asset_prefix if f.name.startswith(asset_prefix) else md_stem
        dest_name = f.name.replace(f"{prefix}_", "", 1)
        shutil.move(str(f), str(dest_dir / dest_name))
    for f in origin_pdfs:
        prefix = asset_prefix if f.name.startswith(asset_prefix) else md_stem
        dest_name = f.name.replace(f"{prefix}_", "", 1)
        shutil.move(str(f), str(dest_dir / dest_name))


def _move_to_pending(
    ctx: InboxCtx,
    *,
    issue: str = "no_doi",
    message: str = "API 查询后仍无 DOI，需人工确认后补充 DOI 再入库",
    extra: dict | None = None,
) -> None:
    """将文件移入 pending 目录（每篇一个子目录）。

    Args:
        ctx: Inbox 上下文。
        issue: 问题类型标识（``"no_doi"`` | ``"duplicate"``）。
        message: 人类可读的问题描述。
        extra: 附加信息写入 pending.json（如重复论文的已有路径）。
    """
    from scholaraio.ingest.metadata import metadata_to_dict

    pending_dir = ctx.pending_dir or ctx.cfg._root / "data" / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)

    md_stem = ctx.md_path.stem if ctx.md_path else ""
    pdf_stem = ctx.pdf_path.stem if ctx.pdf_path else ""
    # Use PDF name as directory name (human-readable), fall back to md stem or title
    dir_name = pdf_stem or md_stem
    if not dir_name and ctx.meta and ctx.meta.title:
        from scholaraio.ingest.metadata import generate_new_stem

        dir_name = generate_new_stem(ctx.meta)
    if not dir_name:
        dir_name = "unknown"
    paper_d = pending_dir / dir_name
    # Avoid overwriting an existing pending directory
    suffix = 2
    while paper_d.exists():
        paper_d = pending_dir / f"{dir_name}-{suffix}"
        suffix += 1
    paper_d.mkdir(parents=True)

    # Move .md
    if ctx.md_path and ctx.md_path.exists():
        shutil.move(str(ctx.md_path), str(paper_d / "paper.md"))

    # Move .pdf if present
    if ctx.pdf_path and ctx.pdf_path.exists():
        shutil.move(str(ctx.pdf_path), str(paper_d / ctx.pdf_path.name))

    # Move MinerU assets (images, layout.json, etc.)
    _move_assets(ctx.inbox_dir, paper_d, pdf_stem or md_stem, md_stem)

    # Write marker JSON with extracted metadata + issue description
    marker: dict[str, Any] = {
        "issue": issue,
        "message": message,
    }
    if extra:
        marker.update(extra)
    if ctx.meta:
        marker["extracted_metadata"] = metadata_to_dict(ctx.meta)
    (paper_d / "pending.json").write_text(json.dumps(marker, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _log.debug("-> pending/%s/ (%s)", dir_name, issue)


def _repair_abstract(json_path: Path, md_path: Path, cfg: Config) -> None:
    """已入库论文 MD 补全后，检查并补写 abstract。"""
    from scholaraio.papers import read_meta, write_meta

    paper_d = json_path.parent
    data = read_meta(paper_d)
    if data.get("abstract"):
        return
    from scholaraio.ingest.metadata import extract_abstract_from_md

    abstract = extract_abstract_from_md(md_path, cfg)
    if abstract:
        data["abstract"] = abstract
        write_meta(paper_d, data)
        _log.debug("abstract backfilled from MD (%d chars)", len(abstract))


def _update_registry(cfg, meta, dir_name: str) -> None:
    """Insert/update papers_registry so UUID lookup works immediately."""
    import sqlite3

    db_path = cfg.index_db
    if not db_path.exists():
        return
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO papers_registry
                   (id, dir_name, title, doi, year, first_author)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (meta.id, dir_name, meta.title or "", meta.doi or "", meta.year, meta.first_author_lastname or ""),
            )
    except Exception as e:
        _log.debug("failed to update papers_registry: %s", e)


def _cleanup_inbox(pdf_path: Path | None, md_path: Path | None, dry_run: bool) -> None:
    if dry_run:
        if pdf_path:
            _log.debug("would delete: %s", pdf_path.name)
        if md_path and md_path.exists():
            _log.debug("would delete: %s", md_path.name)
        return
    if pdf_path and pdf_path.exists():
        pdf_path.unlink()
        _log.debug("deleted: %s", pdf_path.name)
    if md_path and md_path.exists():
        md_path.unlink()
        _log.debug("deleted: %s", md_path.name)


def _cleanup_assets(inbox_dir: Path, pdf_stem: str, md_stem: str) -> None:
    """Remove MinerU artifacts left in inbox (layout.json, content_list, origin.pdf, images)."""
    images_dir, json_files, origin_pdfs = _find_assets(inbox_dir, pdf_stem, md_stem)
    if images_dir:
        shutil.rmtree(images_dir)
        _log.debug("deleted asset dir: %s", images_dir.name)
    for f in json_files:
        f.unlink(missing_ok=True)
        _log.debug("deleted asset: %s", f.name)
    for f in origin_pdfs:
        f.unlink(missing_ok=True)
        _log.debug("deleted asset: %s", f.name)
