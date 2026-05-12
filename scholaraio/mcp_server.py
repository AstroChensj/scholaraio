"""
mcp_server.py -- ScholarAIO MCP 服务端
========================================

通过 MCP 协议暴露 ScholarAIO 知识库的查询和管理功能。
使用 stdio 传输，供 Claude Desktop / Claude Code 集成。

启动：
    scholaraio-mcp                        # entry point
    python -m scholaraio.mcp_server       # 直接运行

配置：
    SCHOLARAIO_ROOT=/path/to/project      # 项目根目录（含 config.yaml）
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("scholaraio")

_cfg = None
_log = logging.getLogger(__name__)


# ============================================================================
#  Config & logging helpers
# ============================================================================


_NR_TOPICS_MAP: dict[int, str | None] = {0: "auto", -1: None}


def _map_nr_topics(nr_topics: int) -> int | str | None:
    """Map MCP nr_topics sentinel to the value expected by topics.build_topics.

    0  → "auto"  (automatic topic merging/reduction)
    -1 → None    (no reduction, keep HDBSCAN clusters as-is)
    N  → N       (explicit target topic count, passed through)
    """
    return _NR_TOPICS_MAP.get(nr_topics, nr_topics)


def _get_cfg():
    """Lazy-load config singleton."""
    global _cfg
    if _cfg is None:
        from scholaraio.config import load_config

        root = os.environ.get("SCHOLARAIO_ROOT")
        if root:
            _cfg = load_config(Path(root) / "config.yaml")
        else:
            _cfg = load_config()

        _init_logging(_cfg)
        _cfg.ensure_dirs()
    return _cfg


def _init_logging(cfg):
    """File-only logging -- no stdout (stdio transport occupies stdout)."""
    root = logging.getLogger()
    # Skip if already initialised (e.g. tests)
    if any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        return
    root.setLevel(logging.DEBUG)

    log_path = cfg.log_file
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=cfg.log.max_bytes,
        backupCount=cfg.log.backup_count,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(
        logging.Formatter(
            "%(asctime)s %(name)-24s %(levelname)-5s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(fh)

    for name in ("httpx", "urllib3", "modelscope", "httpcore", "sentence_transformers"):
        logging.getLogger(name).setLevel(logging.WARNING)


# ============================================================================
#  Resolution helpers
# ============================================================================


def _resolve_paper_dir(paper_ref: str) -> Path:
    """Resolve paper_ref (dir_name, UUID, or DOI) to its directory.

    Raises:
        ValueError: If the paper is not found.
    """
    from scholaraio.index import lookup_paper
    from scholaraio.papers import iter_paper_dirs, read_meta

    cfg = _get_cfg()
    papers_dir = cfg.papers_dir

    # 1. Direct dir_name
    d = papers_dir / paper_ref
    if (d / "meta.json").exists():
        return d
    # 2. Registry lookup
    try:
        reg = lookup_paper(cfg.index_db, paper_ref)
    except FileNotFoundError:
        reg = None
    if reg:
        d = papers_dir / reg["dir_name"]
        if (d / "meta.json").exists():
            return d
    # 3. Filesystem scan fallback
    for pdir in iter_paper_dirs(papers_dir):
        try:
            data = read_meta(pdir)
        except (ValueError, FileNotFoundError):
            continue
        if data.get("id") == paper_ref or data.get("doi") == paper_ref:
            return pdir
    raise ValueError(f"Paper not found: {paper_ref}")


def _resolve_workspace_ids(workspace: str | None) -> set[str] | None:
    """Resolve workspace name to paper_ids set, or None."""
    if not workspace:
        return None
    from scholaraio import workspace as ws_mod

    cfg = _get_cfg()
    ws_dir = cfg._root / "workspace" / workspace
    return ws_mod.read_paper_ids(ws_dir) or None


def _error(code: str, message: str, **extra) -> str:
    """Return a JSON error string for MCP tool responses."""
    return json.dumps({"error": code, "message": message, **extra}, ensure_ascii=False)


# ============================================================================
#  Search tools (5)
# ============================================================================


@mcp.tool()
def search(
    query: str,
    top_k: int = 20,
    year: str | None = None,
    journal: str | None = None,
    paper_type: str | None = None,
    workspace: str | None = None,
) -> str:
    """FTS5 keyword search across paper titles, abstracts, and conclusions.

    Args:
        query: Search keywords.
        top_k: Maximum number of results (default 20).
        year: Year filter, e.g. "2023", "2020-2024", "2020-".
        journal: Journal name filter (substring match).
        paper_type: Paper type filter, e.g. "review", "journal-article".
        workspace: Optional workspace name to scope the search.
    """
    try:
        from scholaraio.index import search as _search

        cfg = _get_cfg()
        paper_ids = _resolve_workspace_ids(workspace)
        results = _search(
            query,
            cfg.index_db,
            top_k=top_k,
            cfg=cfg,
            year=year,
            journal=journal,
            paper_type=paper_type,
            paper_ids=paper_ids,
        )
        return json.dumps(results, ensure_ascii=False)
    except FileNotFoundError:
        return _error("index_not_found", "Index not built. Run: scholaraio index")
    except Exception as e:
        _log.exception("search failed")
        return _error("internal", str(e))


@mcp.tool()
def search_author(
    query: str,
    top_k: int = 20,
    year: str | None = None,
    journal: str | None = None,
    paper_type: str | None = None,
    workspace: str | None = None,
) -> str:
    """Search papers by author name (fuzzy LIKE match).

    Args:
        query: Author name or partial name.
        top_k: Maximum number of results.
        year: Year filter.
        journal: Journal name filter.
        paper_type: Paper type filter.
        workspace: Optional workspace name.
    """
    try:
        from scholaraio.index import search_author as _search_author

        cfg = _get_cfg()
        paper_ids = _resolve_workspace_ids(workspace)
        results = _search_author(
            query,
            cfg.index_db,
            top_k=top_k,
            cfg=cfg,
            year=year,
            journal=journal,
            paper_type=paper_type,
            paper_ids=paper_ids,
        )
        return json.dumps(results, ensure_ascii=False)
    except FileNotFoundError:
        return _error("index_not_found", "Index not built. Run: scholaraio index")
    except Exception as e:
        _log.exception("search_author failed")
        return _error("internal", str(e))


@mcp.tool()
def vsearch(
    query: str,
    top_k: int = 10,
    year: str | None = None,
    journal: str | None = None,
    paper_type: str | None = None,
    workspace: str | None = None,
    exclude: list[str] | None = None,
) -> str:
    """Semantic vector search using Qwen3-Embedding (FAISS cosine similarity).

    Requires the [embed] dependency group: pip install scholaraio[embed].

    Args:
        query: Natural language query.
        top_k: Maximum number of results (default 10).
        year: Year filter.
        journal: Journal name filter.
        paper_type: Paper type filter.
        workspace: Optional workspace name.
        exclude: Negative phrases for ranking-only downweighting.
    """
    try:
        from scholaraio.vectors import vsearch as _vsearch
    except ImportError:
        return _error(
            "missing_dependency", "Embedding dependencies not installed.", install_hint="pip install scholaraio[embed]"
        )
    try:
        cfg = _get_cfg()
        paper_ids = _resolve_workspace_ids(workspace)
        results = _vsearch(
            query,
            cfg.index_db,
            top_k=top_k,
            cfg=cfg,
            year=year,
            journal=journal,
            paper_type=paper_type,
            paper_ids=paper_ids,
            exclude_terms=exclude,
        )
        return json.dumps(results, ensure_ascii=False)
    except FileNotFoundError:
        return _error("vectors_not_found", "Vectors not built. Run: scholaraio embed")
    except Exception as e:
        _log.exception("vsearch failed")
        return _error("internal", str(e))


@mcp.tool()
def unified_search(
    query: str,
    top_k: int = 20,
    year: str | None = None,
    journal: str | None = None,
    paper_type: str | None = None,
    workspace: str | None = None,
    exclude: list[str] | None = None,
) -> str:
    """Hybrid search combining FTS5 keywords and FAISS semantic vectors.

    Falls back to keyword-only search if embedding dependencies are not installed.

    Args:
        query: Search query (keywords and/or natural language).
        top_k: Maximum number of results.
        year: Year filter.
        journal: Journal name filter.
        paper_type: Paper type filter.
        workspace: Optional workspace name.
        exclude: Negative phrases for ranking-only downweighting.
    """
    try:
        from scholaraio.index import unified_search as _usearch

        cfg = _get_cfg()
        paper_ids = _resolve_workspace_ids(workspace)
        results = _usearch(
            query,
            cfg.index_db,
            top_k=top_k,
            cfg=cfg,
            year=year,
            journal=journal,
            paper_type=paper_type,
            paper_ids=paper_ids,
            exclude_terms=exclude,
        )
        return json.dumps(results, ensure_ascii=False)
    except FileNotFoundError:
        return _error("index_not_found", "Index not built. Run: scholaraio index")
    except Exception as e:
        _log.exception("unified_search failed")
        return _error("internal", str(e))


@mcp.tool()
def top_cited(
    top_k: int = 20,
    year: str | None = None,
    journal: str | None = None,
    paper_type: str | None = None,
    workspace: str | None = None,
) -> str:
    """List papers ranked by citation count (highest first).

    Args:
        top_k: Number of papers to return (default 20).
        year: Year filter.
        journal: Journal name filter.
        paper_type: Paper type filter.
        workspace: Optional workspace name.
    """
    try:
        from scholaraio.index import top_cited as _top_cited

        cfg = _get_cfg()
        paper_ids = _resolve_workspace_ids(workspace)
        results = _top_cited(
            cfg.index_db,
            top_k=top_k,
            year=year,
            journal=journal,
            paper_type=paper_type,
            paper_ids=paper_ids,
        )
        return json.dumps(results, ensure_ascii=False)
    except FileNotFoundError:
        return _error("index_not_found", "Index not built. Run: scholaraio index")
    except Exception as e:
        _log.exception("top_cited failed")
        return _error("internal", str(e))


# ============================================================================
#  Paper content tools (2)
# ============================================================================


@mcp.tool()
def show_paper(paper_ref: str, layer: int = 2) -> str:
    """Show paper content at the specified detail level.

    Layer 1: metadata (title, authors, year, journal, DOI, etc.)
    Layer 2: metadata + abstract
    Layer 3: metadata + abstract + conclusion
    Layer 4: metadata + full markdown text

    Args:
        paper_ref: Paper identifier (directory name, UUID, or DOI).
        layer: Detail level 1-4 (default 2).
    """
    try:
        from scholaraio.loader import load_l1, load_l2, load_l3, load_l4

        paper_d = _resolve_paper_dir(paper_ref)
        json_path = paper_d / "meta.json"
        md_path = paper_d / "paper.md"

        result = load_l1(json_path)

        if layer >= 2:
            result["abstract"] = load_l2(json_path)
        if layer >= 3:
            result["conclusion"] = load_l3(json_path)
        if layer >= 4:
            if md_path.exists():
                result["full_text"] = load_l4(md_path)
            else:
                result["full_text"] = None

        return json.dumps(result, ensure_ascii=False)
    except ValueError as e:
        return _error("not_found", str(e))
    except Exception as e:
        _log.exception("show_paper failed")
        return _error("internal", str(e))


@mcp.tool()
def lookup_paper(paper_ref: str) -> str:
    """Look up a paper by UUID, directory name, or DOI in the registry.

    Returns basic paper info (id, dir_name, title, doi, year, first_author)
    or null if not found. Faster than show_paper for simple lookups.

    Args:
        paper_ref: Paper identifier (UUID, directory name, or DOI).
    """
    try:
        from scholaraio.index import lookup_paper as _lookup

        cfg = _get_cfg()
        result = _lookup(cfg.index_db, paper_ref)
        return json.dumps(result, ensure_ascii=False)
    except FileNotFoundError:
        return _error("index_not_found", "Index not built. Run: scholaraio index")
    except Exception as e:
        _log.exception("lookup_paper failed")
        return _error("internal", str(e))


# ============================================================================
#  Citation graph tools (3)
# ============================================================================


@mcp.tool()
def get_references(paper_ref: str, workspace: str | None = None) -> str:
    """Get the reference list of a paper (papers it cites).

    Returns two groups: references found in the local library (with metadata)
    and references only known by DOI (outside the library).

    Args:
        paper_ref: Paper identifier.
        workspace: Optional workspace name to scope results.
    """
    try:
        from scholaraio.index import get_references as _get_refs

        cfg = _get_cfg()
        paper_ids = _resolve_workspace_ids(workspace)
        # Resolve to UUID
        paper_d = _resolve_paper_dir(paper_ref)
        from scholaraio.papers import read_meta

        meta = read_meta(paper_d)
        uuid = meta["id"]

        results = _get_refs(uuid, cfg.index_db, paper_ids=paper_ids)
        return json.dumps(results, ensure_ascii=False)
    except ValueError as e:
        return _error("not_found", str(e))
    except FileNotFoundError:
        return _error("index_not_found", "Index not built. Run: scholaraio index")
    except Exception as e:
        _log.exception("get_references failed")
        return _error("internal", str(e))


@mcp.tool()
def get_citing_papers(paper_ref: str, workspace: str | None = None) -> str:
    """Find papers that cite the given paper.

    Args:
        paper_ref: Paper identifier.
        workspace: Optional workspace name to scope results.
    """
    try:
        from scholaraio.index import get_citing_papers as _get_citing

        cfg = _get_cfg()
        paper_ids = _resolve_workspace_ids(workspace)
        paper_d = _resolve_paper_dir(paper_ref)
        from scholaraio.papers import read_meta

        meta = read_meta(paper_d)
        uuid = meta["id"]

        results = _get_citing(uuid, cfg.index_db, paper_ids=paper_ids)
        return json.dumps(results, ensure_ascii=False)
    except ValueError as e:
        return _error("not_found", str(e))
    except FileNotFoundError:
        return _error("index_not_found", "Index not built. Run: scholaraio index")
    except Exception as e:
        _log.exception("get_citing_papers failed")
        return _error("internal", str(e))


@mcp.tool()
def get_shared_references(
    paper_refs: list[str],
    min_shared: int = 2,
    workspace: str | None = None,
) -> str:
    """Find references shared by multiple papers.

    Useful for discovering common foundations between papers.

    Args:
        paper_refs: List of 2+ paper identifiers.
        min_shared: Minimum number of papers that must cite a reference (default 2).
        workspace: Optional workspace name.
    """
    try:
        from scholaraio.index import get_shared_references as _get_shared
        from scholaraio.papers import read_meta

        cfg = _get_cfg()
        paper_ids = _resolve_workspace_ids(workspace)

        uuids = []
        for ref in paper_refs:
            paper_d = _resolve_paper_dir(ref)
            meta = read_meta(paper_d)
            uuids.append(meta["id"])

        results = _get_shared(uuids, cfg.index_db, min_shared=min_shared, paper_ids=paper_ids)
        return json.dumps(results, ensure_ascii=False)
    except ValueError as e:
        return _error("not_found", str(e))
    except FileNotFoundError:
        return _error("index_not_found", "Index not built. Run: scholaraio index")
    except Exception as e:
        _log.exception("get_shared_references failed")
        return _error("internal", str(e))


# ============================================================================
#  Build tools (3)
# ============================================================================


@mcp.tool()
def build_index(rebuild: bool = False) -> str:
    """Build or rebuild the FTS5 full-text search index.

    Args:
        rebuild: If True, drop and rebuild from scratch. Otherwise incremental.
    """
    try:
        from scholaraio.index import build_index as _build_index

        cfg = _get_cfg()
        count = _build_index(cfg.papers_dir, cfg.index_db, rebuild=rebuild)
        return json.dumps({"indexed": count})
    except Exception as e:
        _log.exception("build_index failed")
        return _error("internal", str(e))


@mcp.tool()
def build_vectors(rebuild: bool = False) -> str:
    """Build or rebuild the FAISS semantic vector index.

    Requires [embed] dependencies. First run downloads the Qwen3-Embedding model (~1.2GB).

    Args:
        rebuild: If True, regenerate all vectors. Otherwise incremental.
    """
    try:
        from scholaraio.vectors import build_vectors as _build_vectors
    except ImportError:
        return _error(
            "missing_dependency", "Embedding dependencies not installed.", install_hint="pip install scholaraio[embed]"
        )
    try:
        cfg = _get_cfg()
        count = _build_vectors(cfg.papers_dir, cfg.index_db, rebuild=rebuild, cfg=cfg)
        return json.dumps({"vectors": count})
    except Exception as e:
        _log.exception("build_vectors failed")
        return _error("internal", str(e))


@mcp.tool()
def build_topics(
    rebuild: bool = False,
    min_topic_size: int = 5,
    nr_topics: int = 0,
) -> str:
    """Build or rebuild the BERTopic topic model.

    Requires [topics] dependencies. nr_topics=0 triggers automatic topic merging/reduction.

    Args:
        rebuild: If True, force rebuild. Otherwise load cached model.
        min_topic_size: Minimum cluster size for HDBSCAN.
        nr_topics: Target number of topics (0 = automatic reduction, -1 = no reduction).
    """
    try:
        from scholaraio.topics import build_topics as _build_topics
        from scholaraio.topics import get_topic_overview, load_model
    except ImportError:
        return _error(
            "missing_dependency", "Topics dependencies not installed.", install_hint="pip install scholaraio[topics]"
        )
    try:
        cfg = _get_cfg()
        model_dir = cfg.topics_model_dir

        if not rebuild and (model_dir / "bertopic_model.pkl").exists():
            model = load_model(model_dir)
        else:
            nr = _map_nr_topics(nr_topics)
            model = _build_topics(
                cfg.index_db,
                cfg.papers_dir,
                min_topic_size=min_topic_size,
                nr_topics=nr,
                save_path=model_dir,
                cfg=cfg,
            )

        overview = get_topic_overview(model)
        topics_list = getattr(model, "_topics", None) or getattr(model, "topics_", [])
        n_outliers = sum(1 for t in topics_list if t == -1)
        return json.dumps(
            {
                "topics": len(overview),
                "outliers": n_outliers,
                "total_papers": sum(t.get("count", 0) for t in overview) + n_outliers,
            }
        )
    except Exception as e:
        _log.exception("build_topics failed")
        return _error("internal", str(e))


# ============================================================================
#  Topics tools (2)
# ============================================================================


@mcp.tool()
def topic_overview() -> str:
    """Get an overview of all topics in the paper library.

    Returns topic IDs, keywords, paper counts, and representative papers.
    Requires a pre-built topic model (run build_topics first).
    """
    try:
        from scholaraio.topics import get_topic_overview, load_model
    except ImportError:
        return _error(
            "missing_dependency", "Topics dependencies not installed.", install_hint="pip install scholaraio[topics]"
        )
    try:
        cfg = _get_cfg()
        model_dir = cfg.topics_model_dir
        if not (model_dir / "bertopic_model.pkl").exists():
            return _error("model_not_found", "Topic model not built. Run build_topics first.")
        model = load_model(model_dir)
        overview = get_topic_overview(model)
        return json.dumps(overview, ensure_ascii=False)
    except Exception as e:
        _log.exception("topic_overview failed")
        return _error("internal", str(e))


@mcp.tool()
def topic_papers(topic_id: int) -> str:
    """List papers belonging to a specific topic.

    Args:
        topic_id: Topic ID from topic_overview results (-1 for outliers).
    """
    try:
        from scholaraio.topics import get_topic_papers, load_model
    except ImportError:
        return _error(
            "missing_dependency", "Topics dependencies not installed.", install_hint="pip install scholaraio[topics]"
        )
    try:
        cfg = _get_cfg()
        model_dir = cfg.topics_model_dir
        if not (model_dir / "bertopic_model.pkl").exists():
            return _error("model_not_found", "Topic model not built. Run build_topics first.")
        model = load_model(model_dir)
        papers = get_topic_papers(model, topic_id)
        return json.dumps(papers, ensure_ascii=False)
    except Exception as e:
        _log.exception("topic_papers failed")
        return _error("internal", str(e))


# ============================================================================
#  Workspace tools (4)
# ============================================================================


@mcp.tool()
def workspace_list() -> str:
    """List all research workspaces."""
    try:
        from scholaraio import workspace as ws_mod

        cfg = _get_cfg()
        ws_root = cfg._root / "workspace"
        names = ws_mod.list_workspaces(ws_root)
        return json.dumps(names, ensure_ascii=False)
    except Exception as e:
        _log.exception("workspace_list failed")
        return _error("internal", str(e))


@mcp.tool()
def workspace_show(name: str) -> str:
    """Show papers in a workspace.

    Args:
        name: Workspace name.
    """
    try:
        from scholaraio import workspace as ws_mod

        cfg = _get_cfg()
        ws_dir = cfg._root / "workspace" / name
        if not ws_dir.exists():
            return _error("not_found", f"Workspace not found: {name}")
        papers = ws_mod.show(ws_dir, cfg.index_db)
        return json.dumps(papers, ensure_ascii=False)
    except Exception as e:
        _log.exception("workspace_show failed")
        return _error("internal", str(e))


@mcp.tool()
def workspace_add(name: str, paper_refs: list[str]) -> str:
    """Add papers to a workspace. Creates the workspace if it doesn't exist.

    Args:
        name: Workspace name.
        paper_refs: List of paper identifiers (UUID, directory name, or DOI).
    """
    try:
        from scholaraio import workspace as ws_mod

        cfg = _get_cfg()
        ws_dir = cfg._root / "workspace" / name
        if not ws_dir.exists():
            ws_mod.create(ws_dir)
        added = ws_mod.add(ws_dir, paper_refs, cfg.index_db)
        return json.dumps({"added": added}, ensure_ascii=False)
    except Exception as e:
        _log.exception("workspace_add failed")
        return _error("internal", str(e))


@mcp.tool()
def workspace_remove(name: str, paper_refs: list[str]) -> str:
    """Remove papers from a workspace.

    Args:
        name: Workspace name.
        paper_refs: List of paper identifiers to remove.
    """
    try:
        from scholaraio import workspace as ws_mod

        cfg = _get_cfg()
        ws_dir = cfg._root / "workspace" / name
        if not ws_dir.exists():
            return _error("not_found", f"Workspace not found: {name}")
        removed = ws_mod.remove(ws_dir, paper_refs, cfg.index_db)
        return json.dumps({"removed": removed}, ensure_ascii=False)
    except Exception as e:
        _log.exception("workspace_remove failed")
        return _error("internal", str(e))


# ============================================================================
#  Export & diagnostics (3)
# ============================================================================


@mcp.tool()
def export_bibtex(
    paper_refs: list[str] | None = None,
    all_papers: bool = False,
    year: str | None = None,
    journal: str | None = None,
) -> str:
    """Export papers as BibTeX entries.

    Either specify paper_refs for specific papers, or set all_papers=True for all.

    Args:
        paper_refs: List of paper identifiers (optional).
        all_papers: If True, export all papers.
        year: Year filter (when all_papers=True).
        journal: Journal name filter (when all_papers=True).
    """
    try:
        from scholaraio.export import export_bibtex as _export

        cfg = _get_cfg()

        if paper_refs and not all_papers:
            # Export specific papers by dir_name
            dir_names = []
            for ref in paper_refs:
                paper_d = _resolve_paper_dir(ref)
                dir_names.append(paper_d.name)
            bibtex = _export(cfg.papers_dir, paper_ids=dir_names)
        elif all_papers:
            bibtex = _export(cfg.papers_dir, year=year, journal=journal)
        else:
            return _error("invalid_args", "Specify paper_refs or set all_papers=True.")

        count = bibtex.count("@")
        return json.dumps({"bibtex": bibtex, "count": count}, ensure_ascii=False)
    except ValueError as e:
        return _error("not_found", str(e))
    except Exception as e:
        _log.exception("export_bibtex failed")
        return _error("internal", str(e))


@mcp.tool()
def audit(severity: str | None = None) -> str:
    """Audit paper data quality: missing fields, DOI duplicates, naming issues, etc.

    Args:
        severity: Filter by severity level: "error", "warning", or "info". None for all.
    """
    try:
        from scholaraio.audit import audit_papers

        cfg = _get_cfg()
        issues = audit_papers(cfg.papers_dir)

        if severity:
            issues = [i for i in issues if i.severity == severity]

        issue_dicts = [
            {"paper_id": i.paper_id, "severity": i.severity, "rule": i.rule, "message": i.message} for i in issues
        ]
        summary = {}
        for i in issue_dicts:
            summary[i["severity"]] = summary.get(i["severity"], 0) + 1

        return json.dumps({"issues": issue_dicts, "summary": summary}, ensure_ascii=False)
    except Exception as e:
        _log.exception("audit failed")
        return _error("internal", str(e))


@mcp.tool()
def setup_check() -> str:
    """Check the ScholarAIO environment: dependencies, config, data directories, API keys.

    Returns a structured diagnostic report. Useful for troubleshooting setup issues.
    """
    try:
        from scholaraio.setup import format_check_results, run_check

        cfg = _get_cfg()
        results = run_check(cfg)
        formatted = format_check_results(results)
        result_dicts = [{"label": r.label, "ok": r.ok, "detail": r.detail} for r in results]
        return json.dumps({"checks": result_dicts, "formatted": formatted}, ensure_ascii=False)
    except Exception as e:
        _log.exception("setup_check failed")
        return _error("internal", str(e))


# ============================================================================
#  Ingest & pipeline tools (4)
# ============================================================================


@mcp.tool()
def pipeline_ingest(
    preset: str = "ingest",
    dry_run: bool = False,
    no_api: bool = False,
    force: bool = False,
) -> str:
    """Run the ingestion pipeline on PDF/markdown files in data/inbox/.

    Place PDF or .md files in data/inbox/, then call this tool to process them.
    The pipeline extracts metadata, deduplicates by DOI, and moves papers
    to data/papers/. Afterwards it rebuilds the search index and vectors.

    Presets: "ingest" (full), "reindex" (rebuild index+vectors only),
    "md-only" (skip MinerU, process .md files only).

    This is a long-running operation (may take minutes for PDFs).

    Args:
        preset: Pipeline preset name (default "ingest").
        dry_run: If True, show what would happen without making changes.
        no_api: If True, skip external API calls for metadata enrichment.
        force: If True, force re-processing even if already done.
    """
    try:
        from scholaraio.ingest.pipeline import PRESETS, run_pipeline

        cfg = _get_cfg()

        if preset not in PRESETS:
            return _error("invalid_args", f"Unknown preset '{preset}'. Available: {', '.join(PRESETS)}")

        step_names = PRESETS[preset]
        opts = {
            "dry_run": dry_run,
            "no_api": no_api,
            "force": force,
            "inspect": False,
            "max_retries": 3,
            "rebuild": False,
        }
        run_pipeline(step_names, cfg, opts)
        return json.dumps({"status": "ok", "preset": preset, "dry_run": dry_run})
    except ImportError as e:
        mod = getattr(e, "name", "") or ""
        return _error("missing_dependency", f"Missing dependency: {mod}", install_hint="pip install scholaraio[full]")
    except Exception as e:
        _log.exception("pipeline_ingest failed")
        return _error("internal", str(e))


@mcp.tool()
def import_endnote(
    files: list[str],
    no_api: bool = False,
    dry_run: bool = False,
    no_convert: bool = False,
) -> str:
    """Import papers from Endnote XML or RIS export files.

    Automatically matches PDFs from the Endnote library data directory,
    converts them via MinerU, and indexes the imported papers.

    This is a long-running operation.

    Args:
        files: List of file paths to Endnote XML or RIS files.
        no_api: Skip external API calls for metadata enrichment.
        dry_run: Preview what would be imported without making changes.
        no_convert: Skip MinerU PDF conversion (import metadata only).
    """
    try:
        from scholaraio.sources.endnote import parse_endnote_full
    except ImportError:
        return _error(
            "missing_dependency",
            "Endnote import dependencies not installed.",
            install_hint="pip install scholaraio[import]",
        )
    try:
        from scholaraio.ingest.pipeline import import_external

        cfg = _get_cfg()
        paths = [Path(f) for f in files]
        for p in paths:
            if not p.exists():
                return _error("not_found", f"File not found: {p}")

        records, pdf_paths = parse_endnote_full(paths)
        if not records:
            return json.dumps({"status": "empty", "message": "No records parsed"})

        stats = import_external(
            records,
            cfg,
            pdf_paths=pdf_paths,
            no_api=no_api,
            dry_run=dry_run,
        )

        # Batch convert PDFs → paper.md + enrich (toc/l3/abstract)
        convert_stats: dict = {}
        if not dry_run and not no_convert and stats["ingested"] > 0:
            from scholaraio.ingest.pipeline import batch_convert_pdfs

            convert_stats = batch_convert_pdfs(cfg, enrich=True)

        return json.dumps({"status": "ok", **stats, "conversion": convert_stats, "dry_run": dry_run})
    except Exception as e:
        _log.exception("import_endnote failed")
        return _error("internal", str(e))


@mcp.tool()
def import_zotero(
    api_key: str | None = None,
    library_id: str | None = None,
    library_type: str = "user",
    local: str | None = None,
    collection: str | None = None,
    list_collections: bool = False,
    no_api: bool = False,
    dry_run: bool = False,
    no_convert: bool = False,
) -> str:
    """Import papers from Zotero (Web API or local SQLite database).

    Supports two modes:
    - Web API: provide api_key and library_id (or configure in config.local.yaml)
    - Local: provide the path to zotero.sqlite

    Use list_collections=True to see available collections before importing.

    This is a long-running operation.

    Args:
        api_key: Zotero Web API key (optional, uses config if not provided).
        library_id: Zotero library ID (optional, uses config if not provided).
        library_type: Library type: "user" or "group" (default "user").
        local: Path to local Zotero SQLite database (alternative to API mode).
        collection: Collection key to import (optional, imports all if not set).
        list_collections: If True, only list collections without importing.
        no_api: Skip external metadata enrichment APIs.
        dry_run: Preview without making changes.
        no_convert: Skip MinerU PDF conversion.
    """
    try:
        cfg = _get_cfg()

        # Resolve credentials
        _api_key = api_key or cfg.resolved_zotero_api_key()
        _library_id = library_id or cfg.resolved_zotero_library_id()
        _library_type = library_type or cfg.zotero.library_type

        if local:
            db_path = Path(local)
            if not db_path.exists():
                return _error("not_found", f"Zotero database not found: {db_path}")

            from scholaraio.sources.zotero import list_collections_local, parse_zotero_local

            if list_collections:
                collections = list_collections_local(db_path)
                return json.dumps(collections, ensure_ascii=False)

            records, pdf_paths = parse_zotero_local(
                db_path,
                collection_key=collection,
            )
        else:
            if not _api_key:
                return _error(
                    "missing_config",
                    "Zotero API key required. Set --api-key, config.local.yaml, or ZOTERO_API_KEY env var.",
                )
            if not _library_id:
                return _error(
                    "missing_config",
                    "Zotero library ID required. Set --library-id, config.local.yaml, or ZOTERO_LIBRARY_ID env var.",
                )

            try:
                from scholaraio.sources.zotero import fetch_zotero_api, list_collections_api
            except ImportError:
                return _error(
                    "missing_dependency",
                    "Zotero import dependencies not installed.",
                    install_hint="pip install scholaraio[import]",
                )

            if list_collections:
                collections = list_collections_api(_library_id, _api_key, library_type=_library_type)
                return json.dumps(collections, ensure_ascii=False)

            import tempfile

            pdf_dir = Path(tempfile.mkdtemp(prefix="scholaraio_zotero_"))
            records, pdf_paths = fetch_zotero_api(
                _library_id,
                _api_key,
                library_type=_library_type,
                collection_key=collection,
                download_pdfs=True,
                pdf_dir=pdf_dir,
            )

        if not records:
            return json.dumps({"status": "empty", "message": "No records found"})

        from scholaraio.ingest.pipeline import import_external

        stats = import_external(
            records,
            cfg,
            pdf_paths=pdf_paths,
            no_api=no_api,
            dry_run=dry_run,
        )

        # Batch convert PDFs → paper.md + enrich (toc/l3/abstract)
        convert_stats: dict = {}
        if not dry_run and not no_convert and stats["ingested"] > 0:
            from scholaraio.ingest.pipeline import batch_convert_pdfs

            convert_stats = batch_convert_pdfs(cfg, enrich=True)

        return json.dumps({"status": "ok", **stats, "conversion": convert_stats, "dry_run": dry_run})
    except ImportError as e:
        mod = getattr(e, "name", "") or ""
        return _error("missing_dependency", f"Missing dependency: {mod}", install_hint="pip install scholaraio[import]")
    except Exception as e:
        _log.exception("import_zotero failed")
        return _error("internal", str(e))


@mcp.tool()
def attach_pdf(paper_ref: str, pdf_path: str) -> str:
    """Attach a PDF to an existing paper, converting it to markdown via MinerU.

    Replaces any existing paper.md. After conversion, updates the abstract
    if missing and rebuilds the search index.

    Args:
        paper_ref: Paper identifier (directory name, UUID, or DOI).
        pdf_path: Absolute path to the PDF file.
    """
    try:
        import shutil

        from scholaraio.papers import read_meta, write_meta

        cfg = _get_cfg()
        paper_d = _resolve_paper_dir(paper_ref)
        src = Path(pdf_path)
        if not src.exists():
            return _error("not_found", f"PDF file not found: {pdf_path}")

        # Copy PDF
        dest_pdf = paper_d / src.name
        shutil.copy2(str(src), str(dest_pdf))

        # Convert via MinerU
        from scholaraio.ingest.mineru import ConvertOptions, check_server, convert_pdf

        mineru_opts = ConvertOptions(
            api_url=cfg.ingest.mineru_endpoint,
            output_dir=paper_d,
        )

        if check_server(cfg.ingest.mineru_endpoint):
            result = convert_pdf(dest_pdf, mineru_opts)
        else:
            api_key = cfg.resolved_mineru_api_key()
            if not api_key:
                return _error("missing_config", "MinerU not reachable and no cloud API key configured.")
            from scholaraio.ingest.mineru import convert_pdf_cloud

            result = convert_pdf_cloud(
                dest_pdf,
                mineru_opts,
                api_key=api_key,
                cloud_url=cfg.ingest.mineru_cloud_url,
            )

        if not result.success:
            return _error("conversion_failed", f"MinerU conversion failed: {result.error}")

        # Move output to paper.md
        paper_md = paper_d / "paper.md"
        if result.md_path and result.md_path != paper_md:
            if paper_md.exists():
                paper_md.unlink()
            shutil.move(str(result.md_path), str(paper_md))

        # Clean up MinerU artifacts
        for pattern in ["*_layout.json", "*_content_list.json", "*_origin.pdf"]:
            for f in paper_d.glob(pattern):
                f.unlink(missing_ok=True)
        for img_dir in paper_d.glob("*_images"):
            if img_dir.name != "images" and img_dir.is_dir():
                target = paper_d / "images"
                if target.exists():
                    shutil.rmtree(target)
                img_dir.rename(target)

        # Backfill abstract
        try:
            data = read_meta(paper_d)
            if not data.get("abstract") and paper_md.exists():
                from scholaraio.ingest.metadata import extract_abstract_from_md

                abstract = extract_abstract_from_md(paper_md, cfg)
                if abstract:
                    data["abstract"] = abstract
                    write_meta(paper_d, data)
        except (ValueError, FileNotFoundError):
            pass

        return json.dumps({"status": "ok", "paper": paper_d.name})
    except ValueError as e:
        return _error("not_found", str(e))
    except Exception as e:
        _log.exception("attach_pdf failed")
        return _error("internal", str(e))


# ============================================================================
#  Enrichment tools (4)
# ============================================================================


@mcp.tool()
def enrich_toc(paper_ref: str, force: bool = False) -> str:
    """Extract table of contents from a paper using LLM.

    Requires LLM API key (DeepSeek) in config.

    Args:
        paper_ref: Paper identifier.
        force: Re-extract even if TOC already exists.
    """
    try:
        from scholaraio.loader import enrich_toc as _enrich_toc

        cfg = _get_cfg()
        paper_d = _resolve_paper_dir(paper_ref)
        json_path = paper_d / "meta.json"
        md_path = paper_d / "paper.md"

        if not md_path.exists():
            return _error("not_found", f"No paper.md in {paper_d.name}")

        success = _enrich_toc(json_path, md_path, cfg, force=force)
        return json.dumps({"status": "ok" if success else "failed", "paper": paper_d.name})
    except ValueError as e:
        return _error("not_found", str(e))
    except Exception as e:
        _log.exception("enrich_toc failed")
        return _error("internal", str(e))


@mcp.tool()
def enrich_l3(paper_ref: str, force: bool = False) -> str:
    """Extract conclusion section from a paper using LLM.

    Requires LLM API key (DeepSeek) in config.

    Args:
        paper_ref: Paper identifier.
        force: Re-extract even if conclusion already exists.
    """
    try:
        from scholaraio.loader import enrich_l3 as _enrich_l3

        cfg = _get_cfg()
        paper_d = _resolve_paper_dir(paper_ref)
        json_path = paper_d / "meta.json"
        md_path = paper_d / "paper.md"

        if not md_path.exists():
            return _error("not_found", f"No paper.md in {paper_d.name}")

        success = _enrich_l3(json_path, md_path, cfg, force=force)
        return json.dumps({"status": "ok" if success else "failed", "paper": paper_d.name})
    except ValueError as e:
        return _error("not_found", str(e))
    except Exception as e:
        _log.exception("enrich_l3 failed")
        return _error("internal", str(e))


@mcp.tool()
def refetch(
    paper_ref: str | None = None,
    all_papers: bool = False,
    force: bool = False,
) -> str:
    """Refetch citation counts and bibliographic details from external APIs.

    Either specify a single paper or set all_papers=True.

    Args:
        paper_ref: Single paper identifier (optional).
        all_papers: If True, refetch all papers missing citation data.
        force: If True, refetch all papers regardless of existing data.
    """
    try:
        import json as _json

        from scholaraio.ingest.metadata import refetch_metadata
        from scholaraio.papers import iter_paper_dirs

        cfg = _get_cfg()

        if paper_ref:
            paper_d = _resolve_paper_dir(paper_ref)
            jp = paper_d / "meta.json"
            changed = refetch_metadata(jp)
            return _json.dumps({"status": "ok", "changed": changed, "paper": paper_d.name})
        elif all_papers:
            targets = sorted(d / "meta.json" for d in iter_paper_dirs(cfg.papers_dir))
            if not force:
                filtered = []
                for jp in targets:
                    data = _json.loads(jp.read_text(encoding="utf-8"))
                    if not data.get("doi"):
                        continue
                    if not data.get("citation_count") or not all(data.get(k) for k in ("volume", "publisher")):
                        filtered.append(jp)
                targets = filtered

            ok = fail = skip = 0
            for jp in targets:
                try:
                    changed = refetch_metadata(jp)
                    if changed:
                        ok += 1
                    else:
                        skip += 1
                except Exception:
                    fail += 1
            return _json.dumps({"status": "ok", "updated": ok, "skipped": skip, "failed": fail})
        else:
            return _error("invalid_args", "Specify paper_ref or set all_papers=True.")
    except ValueError as e:
        return _error("not_found", str(e))
    except Exception as e:
        _log.exception("refetch failed")
        return _error("internal", str(e))


@mcp.tool()
def backfill_abstract(dry_run: bool = False) -> str:
    """Backfill missing abstracts for papers that have paper.md but no abstract.

    Uses regex extraction, DOI-based fetch, and optionally LLM extraction.

    Args:
        dry_run: If True, show what would be updated without making changes.
    """
    try:
        from scholaraio.ingest.metadata import extract_abstract_from_md
        from scholaraio.papers import iter_paper_dirs, read_meta, write_meta

        cfg = _get_cfg()
        updated = skipped = 0

        for pdir in iter_paper_dirs(cfg.papers_dir):
            try:
                meta = read_meta(pdir)
            except (ValueError, FileNotFoundError):
                continue
            if meta.get("abstract"):
                continue
            md_path = pdir / "paper.md"
            if not md_path.exists():
                continue

            abstract = extract_abstract_from_md(md_path, cfg)
            if abstract:
                if not dry_run:
                    meta["abstract"] = abstract
                    write_meta(pdir, meta)
                updated += 1
            else:
                skipped += 1

        return json.dumps({"status": "ok", "updated": updated, "skipped": skipped, "dry_run": dry_run})
    except Exception as e:
        _log.exception("backfill_abstract failed")
        return _error("internal", str(e))


# ============================================================================
#  Rename tool (1)
# ============================================================================


@mcp.tool()
def rename_paper(
    paper_ref: str | None = None,
    all_papers: bool = False,
    dry_run: bool = False,
) -> str:
    """Rename paper directories to match metadata (Author-Year-Title format).

    Args:
        paper_ref: Single paper identifier (optional).
        all_papers: If True, rename all papers.
        dry_run: If True, show what would be renamed without making changes.
    """
    try:
        from scholaraio.ingest.metadata import generate_new_stem, rename_files
        from scholaraio.papers import iter_paper_dirs, read_meta

        cfg = _get_cfg()

        if paper_ref:
            targets = [_resolve_paper_dir(paper_ref)]
        elif all_papers:
            targets = sorted(iter_paper_dirs(cfg.papers_dir))
        else:
            return _error("invalid_args", "Specify paper_ref or set all_papers=True.")

        renamed = skipped = 0
        results = []
        for paper_d in targets:
            try:
                meta = read_meta(paper_d)
            except (ValueError, FileNotFoundError):
                skipped += 1
                continue

            from scholaraio.ingest.metadata import PaperMetadata

            pm = PaperMetadata()
            for k, v in meta.items():
                if hasattr(pm, k):
                    setattr(pm, k, v)

            new_stem = generate_new_stem(pm)
            if new_stem == paper_d.name:
                skipped += 1
                continue

            if not dry_run:
                md_path = paper_d / "paper.md"
                json_path = paper_d / "meta.json"
                rename_files(md_path, json_path, new_stem, dry_run=False)
            renamed += 1
            results.append({"old": paper_d.name, "new": new_stem})

        return json.dumps(
            {
                "status": "ok",
                "renamed": renamed,
                "skipped": skipped,
                "dry_run": dry_run,
                "changes": results,
            },
            ensure_ascii=False,
        )
    except ValueError as e:
        return _error("not_found", str(e))
    except Exception as e:
        _log.exception("rename_paper failed")
        return _error("internal", str(e))


# ============================================================================
#  federated_search
# ============================================================================


@mcp.tool()
def federated_search(
    query: str,
    scope: str = "main",
    top_k: int = 10,
) -> str:
    """Search across multiple sources: main library, explore silos, and arXiv.

    Args:
        query: Search query text.
        scope: Comma-separated list of sources: main / explore:NAME / explore:* / arxiv.
        top_k: Maximum results per source (default 10).
    """
    import sqlite3

    cfg = _get_cfg()
    scopes = [s.strip() for s in scope.split(",") if s.strip()] or ["main"]
    output: dict[str, list[dict]] = {}

    for src in scopes:
        if src == "main":
            if not Path(cfg.index_db).exists():
                output["main"] = [{"error": "index_not_found", "message": "Index not built. Run: scholaraio index"}]
            else:
                try:
                    from scholaraio.index import unified_search

                    results = unified_search(query, cfg.index_db, top_k=top_k, cfg=cfg)
                    output["main"] = results
                except Exception as e:
                    _log.exception("federated_search main error")
                    output["main"] = [{"error": "internal", "message": str(e)}]

        elif src.startswith("explore:"):
            explore_name = src[len("explore:") :]
            from scholaraio.explore import validate_explore_name

            if explore_name != "*" and not validate_explore_name(explore_name):
                output[src] = [
                    {
                        "error": "invalid_explore_name",
                        "message": f"Invalid explore library name '{explore_name}'. Names must be non-empty and must not contain path separators or '..'.",
                    }
                ]
                continue
            if explore_name == "*":
                from scholaraio.explore import list_explore_libs

                names = list_explore_libs(cfg)
                if not names:
                    output["explore:*"] = [
                        {
                            "error": "no_explore_libs",
                            "message": "No explore libraries found. Run: scholaraio explore fetch --name <name>",
                        }
                    ]
            else:
                names = [explore_name]
            for name in names:
                from scholaraio.explore import explore_db_path, explore_unified_search

                db = explore_db_path(name, cfg)
                if not db.exists():
                    output[f"explore:{name}"] = [{"error": "db_not_found", "message": f"Explore DB not found: {name}"}]
                    continue
                try:
                    results = explore_unified_search(name, query, top_k=top_k, cfg=cfg)
                    output[f"explore:{name}"] = results
                except Exception as e:
                    _log.exception("federated_search explore:%s error", name)
                    output[f"explore:{name}"] = [{"error": "internal", "message": str(e)}]

        elif src == "arxiv":
            from scholaraio.sources.arxiv import search_arxiv

            arxiv_results = search_arxiv(query, top_k)
            if arxiv_results:
                # Annotate which results are already in the main library.
                # Query only the DOIs present in this result set to avoid
                # loading the entire papers_registry on every call.
                arxiv_dois = [r["doi"].lower() for r in arxiv_results if r.get("doi")]
                in_lib_dois: set[str] = set()
                if arxiv_dois and Path(cfg.index_db).exists():
                    try:
                        placeholders = ",".join("?" * len(arxiv_dois))
                        with sqlite3.connect(str(cfg.index_db)) as conn:
                            rows = conn.execute(
                                f"SELECT doi FROM papers_registry WHERE LOWER(doi) IN ({placeholders})",
                                arxiv_dois,
                            ).fetchall()
                        in_lib_dois = {r[0].lower() for r in rows}
                    except Exception:
                        _log.debug("arXiv in-library annotation failed (index_db=%s)", cfg.index_db, exc_info=True)
                for r in arxiv_results:
                    r["in_main_library"] = bool(r.get("doi") and r["doi"].lower() in in_lib_dois)
            output["arxiv"] = arxiv_results

        else:
            output[src] = [
                {
                    "error": "unknown_scope",
                    "message": f"Unknown scope '{src}'. Supported: main / explore:NAME / explore:* / arxiv",
                }
            ]

    return json.dumps(output, ensure_ascii=False)


# ============================================================================
#  Entry point
# ============================================================================


def main():
    """Entry point for scholaraio-mcp command."""
    mcp.run()


if __name__ == "__main__":
    main()
