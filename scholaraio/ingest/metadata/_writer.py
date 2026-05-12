"""JSON output, file renaming, and metadata re-fetch."""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from pathlib import Path

_log = logging.getLogger(__name__)

from ._models import PaperMetadata

# ============================================================================
#  JSON Serialization
# ============================================================================


def metadata_to_dict(meta: PaperMetadata) -> dict:
    """将 :class:`PaperMetadata` 转换为可序列化的字典。

    输出字段包括 ``title``, ``authors``, ``year``, ``doi``, ``journal``,
    ``abstract``, ``citation_count``, ``ids``, ``api_sources`` 等。

    Args:
        meta: 元数据实例。

    Returns:
        JSON 可序列化的字典。
    """
    d: dict = {
        "id": meta.id,
        "title": meta.title,
        "authors": meta.authors,
        "first_author": meta.first_author,
        "first_author_lastname": meta.first_author_lastname,
        "year": meta.year,
        "doi": meta.doi,
        "journal": meta.journal,
        "abstract": meta.abstract,
        "paper_type": meta.paper_type,
        "volume": meta.volume,
        "issue": meta.issue,
        "pages": meta.pages,
        "publisher": meta.publisher,
        "issn": meta.issn,
        "citation_count": {},
        "ids": {},
        "source_file": meta.source_file,
        "extraction_method": meta.extraction_method,
        "api_sources": meta.api_sources,
        "references": meta.references,
        "extracted_at": "",
    }
    # Citation counts
    if meta.citation_count_crossref is not None:
        d["citation_count"]["crossref"] = meta.citation_count_crossref
    if meta.citation_count_s2 is not None:
        d["citation_count"]["semantic_scholar"] = meta.citation_count_s2
    if meta.citation_count_openalex is not None:
        d["citation_count"]["openalex"] = meta.citation_count_openalex
    if meta.citation_count_ads is not None:
        d["citation_count"]["ads"] = meta.citation_count_ads
    # IDs
    if meta.doi:
        d["ids"]["doi"] = meta.doi
        d["ids"]["doi_url"] = f"https://doi.org/{meta.doi}"
    if meta.s2_paper_id:
        d["ids"]["semantic_scholar"] = meta.s2_paper_id
        d["ids"]["semantic_scholar_url"] = f"https://www.semanticscholar.org/paper/{meta.s2_paper_id}"
    if meta.openalex_id:
        d["ids"]["openalex"] = meta.openalex_id
        d["ids"]["openalex_url"] = (
            meta.openalex_id.replace("https://openalex.org/", "https://openalex.org/works/")
            if "openalex.org" in meta.openalex_id and "/works/" not in meta.openalex_id
            else meta.openalex_id
        )
    if meta.ads_bibcode:
        d["ids"]["ads_bibcode"] = meta.ads_bibcode
        d["ids"]["ads_url"] = f"https://ui.adsabs.harvard.edu/abs/{meta.ads_bibcode}/abstract"
    return d


def write_metadata_json(meta: PaperMetadata, output_path: Path) -> None:
    """将元数据写入 JSON 文件（原子写入）。

    Args:
        meta: 元数据实例。
        output_path: 输出 JSON 文件路径。
    """
    from scholaraio.papers import write_meta

    d = metadata_to_dict(meta)
    write_meta(output_path.parent, d)


# ============================================================================
#  Refetch
# ============================================================================


def refetch_metadata(json_path: Path) -> bool:
    """对已入库论文重新查询 API，补全引用量等字段。

    从 JSON 反构造 :class:`PaperMetadata`，调用 :func:`enrich_metadata`
    重新查询三个 API，然后将新数据合并回 JSON（保留 ``toc``、
    ``l3_conclusion`` 等已有富化字段）。

    Args:
        json_path: 已入库论文的 JSON 文件路径。

    Returns:
        ``True`` 表示有字段被更新，``False`` 表示无变化或查询失败。
    """
    from ._api import enrich_metadata

    data = json.loads(json_path.read_text(encoding="utf-8"))

    meta = PaperMetadata(
        id=data.get("id", ""),
        title=data.get("title", ""),
        authors=data.get("authors", []),
        first_author=data.get("first_author", ""),
        first_author_lastname=data.get("first_author_lastname", ""),
        year=data.get("year"),
        doi=data.get("doi", ""),
        journal=data.get("journal", ""),
        abstract=data.get("abstract", ""),
        paper_type=data.get("paper_type", ""),
        volume=data.get("volume", ""),
        issue=data.get("issue", ""),
        pages=data.get("pages", ""),
        publisher=data.get("publisher", ""),
        issn=data.get("issn", ""),
        source_file=data.get("source_file", ""),
        extraction_method=data.get("extraction_method", ""),
        references=data.get("references", []),
        api_sources=[],  # reset so enrich_metadata re-populates
    )
    # Restore existing IDs
    ids = data.get("ids", {})
    meta.s2_paper_id = ids.get("semantic_scholar", "")
    meta.openalex_id = ids.get("openalex", "")
    meta.crossref_doi = ids.get("doi", "")
    meta.ads_bibcode = ids.get("ads_bibcode", "")

    try:
        from scholaraio.config import load_config

        cfg = load_config()
    except Exception:
        cfg = None

    enrich_metadata(meta, cfg)

    new_data = metadata_to_dict(meta)

    # Preserve enriched fields that are not part of metadata pipeline
    for key in data:
        if key.startswith(("toc", "l3_")) and key not in new_data:
            new_data[key] = data[key]

    # Check if anything changed
    changed = False
    for key in (
        "title",
        "authors",
        "first_author",
        "first_author_lastname",
        "year",
        "journal",
        "citation_count",
        "ids",
        "api_sources",
        "abstract",
        "paper_type",
        "volume",
        "issue",
        "pages",
        "publisher",
        "issn",
        "references",
    ):
        if new_data.get(key) != data.get(key):
            changed = True
            break

    if changed:
        from scholaraio.papers import write_meta

        write_meta(json_path.parent, new_data)
        # Rename directory if metadata now yields a better name
        new_path = rename_paper(json_path)
        if new_path:
            _log.debug("renamed: %s -> %s", json_path.parent.name, new_path.parent.name)

    return changed


# ============================================================================
#  File Renaming
# ============================================================================


def rename_paper(json_path: Path, *, dry_run: bool = False) -> Path | None:
    """根据 JSON 元数据重命名论文目录。

    读取 ``meta.json`` 中的 ``first_author_lastname``、``year``、``title``，
    用 :func:`generate_new_stem` 生成标准目录名。若新旧目录名一致则跳过。

    Args:
        json_path: 论文 ``meta.json`` 文件路径。
        dry_run: 为 ``True`` 时只返回新路径，不实际重命名。

    Returns:
        重命名后的新 ``meta.json`` 路径，未变更时返回 ``None``。
    """
    data = json.loads(json_path.read_text(encoding="utf-8"))
    meta = PaperMetadata(
        title=data.get("title", ""),
        first_author_lastname=data.get("first_author_lastname", ""),
        year=data.get("year"),
    )
    new_stem = generate_new_stem(meta)

    paper_d = json_path.parent
    old_stem = paper_d.name
    papers_root = paper_d.parent

    if new_stem == old_stem:
        return None

    new_dir = papers_root / new_stem

    # Avoid collision with existing directories
    if new_dir.exists():
        suffix = 2
        while True:
            candidate = f"{new_stem}-{suffix}"
            if not (papers_root / candidate).exists():
                new_stem = candidate
                new_dir = papers_root / new_stem
                break
            suffix += 1

    if dry_run:
        return new_dir / "meta.json"

    paper_d.rename(new_dir)

    # Update papers_registry if index.db exists
    uuid = data.get("id")
    if uuid:
        # papers_root is data/papers/, index.db is at data/ (papers_root.parent)
        _update_registry_dir_name(papers_root.parent / "index.db", uuid, new_dir.name)

    return new_dir / "meta.json"


def generate_new_stem(meta: PaperMetadata) -> str:
    """生成标准化文件名 stem: ``{LastName}-{year}-{FullTitle}``。

    去除变音符号、LaTeX 公式、非法字符，适用于文件系统。

    Args:
        meta: 元数据实例。

    Returns:
        文件系统安全的文件名 stem（不含扩展名）。
    """
    lastname = _strip_diacritics(meta.first_author_lastname or "Unknown")
    year_str = str(meta.year) if meta.year else "XXXX"
    clean_title = _clean_title_for_filename(meta.title)
    stem = f"{lastname}-{year_str}-{clean_title}"
    return _sanitize_for_filename(stem)


def _clean_title_for_filename(title: str) -> str:
    """Clean title for use in filename: keep full title, strip math/formulas."""
    if not title:
        return "Untitled"
    # Remove LaTeX inline math: $...$
    title = re.sub(r"\$[^$]+\$", "", title)
    # Remove LaTeX commands: \mathrm{...}, \textit{...}, etc.
    title = re.sub(r"\\[a-zA-Z]+\{[^}]*\}", "", title)
    # Remove remaining backslash commands
    title = re.sub(r"\\[a-zA-Z]+", "", title)
    # Remove standalone math symbols
    title = re.sub(r"[=+<>~^{}|\\]", "", title)
    # Collapse whitespace
    title = re.sub(r"\s+", " ", title).strip()
    return title


def _strip_diacritics(text: str) -> str:
    """Jiménez → Jimenez, François → Francois."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _sanitize_for_filename(text: str) -> str:
    """Make text filesystem-safe."""
    # Replace whitespace with hyphens
    text = re.sub(r"\s+", "-", text)
    # Keep only safe characters (including Chinese)
    text = re.sub(r"[^\w\-\u4e00-\u9fff]", "", text)
    # Collapse multiple hyphens
    text = re.sub(r"-{2,}", "-", text)
    # Strip leading/trailing hyphens
    text = text.strip("-")
    return text


def rename_files(md_path: Path, json_path: Path, new_stem: str, dry_run: bool = False) -> tuple[Path, Path]:
    """Rename paper directory to new_stem, return new (md_path, json_path)."""
    paper_d = json_path.parent
    papers_root = paper_d.parent
    new_dir = papers_root / new_stem

    # Collision avoidance
    suffix = 2
    while new_dir.exists() and new_dir != paper_d:
        new_dir = papers_root / f"{new_stem}-{suffix}"
        suffix += 1

    new_json = new_dir / "meta.json"
    new_md = new_dir / "paper.md"

    if dry_run:
        _log.debug("would rename dir: %s -> %s", paper_d.name, new_dir.name)
        return new_md, new_json

    if paper_d != new_dir:
        paper_d.rename(new_dir)
    _log.debug("renamed dir: %s -> %s", paper_d.name, new_dir.name)
    return new_md, new_json


def _update_registry_dir_name(db_path: Path, uuid: str, new_dir_name: str) -> None:
    """Best-effort update of dir_name in papers_registry after rename."""
    import sqlite3

    if not db_path.exists():
        return
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE papers_registry SET dir_name = ? WHERE id = ?",
                (new_dir_name, uuid),
            )
    except Exception as e:
        _log.debug("failed to update papers_registry after rename: %s", e)
