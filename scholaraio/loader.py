"""
loader.py — 分层内容加载 + TOC 提取 + L3 结论提取
====================================================

L1: title / authors / year / journal / doi  ← JSON 字段
L2: abstract                                ← JSON 字段
L3: conclusion                              ← JSON 字段（需先运行 enrich_l3 提取）
L4: full markdown                           ← 读 .md 文件

TOC 提取（enrich_toc）
-----------------------
1. regex 提取所有 # 标题 + 行号
2. LLM 过滤 noise（author running headers、期刊名、论文标题重复等），
   并为每个真实节标题分配层级（level）
3. 写入 JSON["toc"]：[{"line": N, "level": N, "title": "..."}]

L3 提取（enrich_l3）
---------------------
若 JSON 已有 TOC，直接从中定位结论节（跳过第一次 LLM 调用）。
否则走 Primary path：LLM 从原始标题列表选出结论节 → Python 截取 → LLM 校验。
Fallback path：LLM 直接给出起止行号 → Python 截取 → LLM 校验。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from scholaraio.log import ui

if TYPE_CHECKING:
    from scholaraio.config import Config

_log = logging.getLogger(__name__)

# Paper types for which L3 conclusion extraction is skipped.
# These long-form or non-article documents don't have a standard
# conclusion section suitable for the current extraction strategy.
L3_SKIP_TYPES = frozenset(
    {
        "thesis",
        "dissertation",
        "book",
        "monograph",
        "edited-book",
        "reference-book",
        "book-chapter",
        "book-section",
        "book-part",
        "document",
        "technical-report",
        "lecture-notes",
    }
)


# ============================================================================
#  Public load functions (L1–L4)
# ============================================================================


def load_l1(json_path: Path) -> dict:
    """加载 L1 层元数据（标题、作者、年份、期刊、DOI）。

    Args:
        json_path: 论文 JSON 元数据文件路径。

    Returns:
        包含 ``paper_id``, ``title``, ``authors``, ``year``,
        ``journal``, ``doi`` 的字典。
    """
    data = json.loads(json_path.read_text(encoding="utf-8"))
    return {
        "paper_id": data.get("id") or json_path.parent.name,
        "title": data.get("title") or "",
        "authors": data.get("authors") or [],
        "year": data.get("year"),
        "journal": data.get("journal") or "",
        "doi": data.get("doi") or "",
        "paper_type": data.get("paper_type") or "",
        "citation_count": data.get("citation_count") or {},
        "ids": data.get("ids") or {},
    }


def load_l2(json_path: Path) -> str:
    """加载 L2 层摘要文本。

    Args:
        json_path: 论文 JSON 元数据文件路径。

    Returns:
        摘要文本，无摘要时返回 ``"[No abstract available]"``。
    """
    data = json.loads(json_path.read_text(encoding="utf-8"))
    return data.get("abstract") or "[No abstract available]"


def load_l3(json_path: Path) -> str | None:
    """加载 L3 层结论文本。

    需先运行 :func:`enrich_l3` 提取结论段到 JSON。

    Args:
        json_path: 论文 JSON 元数据文件路径。

    Returns:
        结论文本，尚未提取时返回 ``None``。
    """
    data = json.loads(json_path.read_text(encoding="utf-8"))
    return data.get("l3_conclusion") or None


def load_l4(md_path: Path) -> str:
    """加载 L4 层全文 Markdown。

    Args:
        md_path: MinerU 输出的 ``.md`` 文件路径。

    Returns:
        完整 Markdown 文本。
    """
    return md_path.read_text(encoding="utf-8", errors="replace")


# ============================================================================
#  Agent notes (T2 persistent analysis notes)
# ============================================================================

_NOTES_FILENAME = "notes.md"


def load_notes(paper_dir: Path) -> str | None:
    """加载论文的 agent 分析笔记。

    笔记文件 (``notes.md``) 由 agent 在分析论文时自动创建和追加，
    用于跨会话、跨工作区复用分析结论。

    Args:
        paper_dir: 论文目录路径（包含 ``meta.json`` 的目录）。

    Returns:
        笔记文本，不存在时返回 ``None``。
    """
    notes_path = paper_dir / _NOTES_FILENAME
    if notes_path.exists():
        text = notes_path.read_text(encoding="utf-8")
        if not text.strip():
            return None
        return text
    return None


def append_notes(paper_dir: Path, section: str) -> None:
    """向论文笔记文件追加一条分析记录。

    如果 ``notes.md`` 不存在则创建。每条记录之间用空行分隔。

    Args:
        paper_dir: 论文目录路径。
        section: 要追加的笔记内容（Markdown 格式，建议以 ``## 日期 | 来源`` 开头）。
    """
    notes_path = paper_dir / _NOTES_FILENAME
    section = section.rstrip("\n")
    if notes_path.exists():
        existing = notes_path.read_text(encoding="utf-8").rstrip("\n")
        notes_path.write_text(existing + "\n\n" + section + "\n", encoding="utf-8")
    else:
        notes_path.write_text(section + "\n", encoding="utf-8")
    _log.debug("appended notes to %s", notes_path)


# ============================================================================
#  TOC extraction
# ============================================================================


def enrich_toc(
    json_path: Path,
    md_path: Path,
    config: Config,
    *,
    force: bool = False,
    inspect: bool = False,
) -> bool:
    """用 LLM 提取论文目录结构，写入 ``JSON["toc"]``。

    从 Markdown 中提取所有 ``#`` 标题，通过 LLM 过滤 running headers、
    期刊名、作者名等噪声，为真实节标题分配层级。

    Args:
        json_path: 论文 JSON 元数据文件路径（结果写回此文件）。
        md_path: 论文 Markdown 文件路径。
        config: 全局配置（用于 LLM 调用）。
        force: 为 ``True`` 时覆盖已有 TOC。
        inspect: 为 ``True`` 时打印过滤过程详情。

    Returns:
        提取成功返回 ``True``，失败返回 ``False``。
    """
    from scholaraio.papers import read_meta, write_meta

    paper_d = json_path.parent
    data = read_meta(paper_d)

    if data.get("toc") and not force:
        _log.debug("existing TOC (%d entries), skipping", len(data["toc"]))
        return True

    lines = md_path.read_text(encoding="utf-8", errors="replace").splitlines()
    raw_headers = _extract_headers(lines)

    _log.debug("regex found %d headers, sending to LLM", len(raw_headers))

    prompt = (
        "The following are ALL lines starting with '#' extracted from an academic paper "
        "markdown file (converted from PDF by MinerU). Some are real section headers; "
        "others are NOISE to discard: author running headers (e.g. '# Smith and others'), "
        "journal name headers (e.g. '# Journal of Fluid Mechanics'), repeated paper titles, "
        "or publisher metadata (e.g. '# ARTICLEINFO', '# AFFILIATIONS', '# Articles You May Be Interested In').\n\n"
        "KEEP the following as real headers (they are needed as section boundary markers):\n"
        "- Numbered/lettered sections and subsections\n"
        "- Introduction, Abstract, Conclusion, Conclusions, Concluding Remarks, Summary\n"
        "- References, Bibliography\n"
        "- Appendix (any variant)\n"
        "- Post-matter sections: Acknowledgments, Acknowledgements, Funding, "
        "CRediT authorship contribution statement, Declaration of competing interest, "
        "Conflict of interest, Data availability, Author contributions, Author ORCIDs, "
        "Declaration of interests\n\n"
        "Assign level: 1=top-level, 2=subsection (e.g. '2.1'), 3=sub-subsection (e.g. '2.1.1').\n\n"
        "Headers:\n"
        + "\n".join(f"Line {h['line']}: {'#' * h['level']} {h['text']}" for h in raw_headers)
        + "\n\nReturn JSON only:\n"
        '{"toc": [{"line": <N>, "level": <1|2|3>, "title": "<title>"}, ...]}'
    )

    try:
        result = _parse_json(_call_llm(prompt, config, timeout=config.llm.timeout_toc))
        toc = result.get("toc") or []
        if not toc:
            _log.error("LLM returned empty TOC")
            return False

        _log.debug("LLM kept %d real headers", len(toc))
        for entry in toc:
            indent = "  " * (entry.get("level", 1) - 1)
            _log.debug("  line %4d  %s%s", entry["line"], indent, entry["title"])

        data["toc"] = toc
        data["toc_extracted_at"] = datetime.now().isoformat(timespec="seconds")
        write_meta(paper_d, data)
        _log.debug("TOC written to JSON")
        return True

    except Exception as e:
        _log.error("TOC extraction failed: %s", e)
        return False


# ============================================================================
#  L3 extraction entry point
# ============================================================================


def enrich_l3(
    json_path: Path,
    md_path: Path,
    config: Config,
    *,
    force: bool = False,
    max_retries: int = 2,
    inspect: bool = False,
) -> bool:
    """用 LLM 提取结论段，写入 ``JSON["l3_conclusion"]``。

    提取策略（按优先级）:
      1. 从已有 TOC 定位结论节 → Python 截取 → LLM 校验清洗
      2. Primary path: LLM 从标题列表选出结论节 → 截取 → 校验
      3. Fallback path: LLM 直接给出起止行号 → 截取 → 校验

    Args:
        json_path: 论文 JSON 元数据文件路径（结果写回此文件）。
        md_path: 论文 Markdown 文件路径。
        config: 全局配置（用于 LLM 调用）。
        force: 为 ``True`` 时覆盖已有结论。
        max_retries: 每条路径的最大重试次数。
        inspect: 为 ``True`` 时打印提取过程详情。

    Returns:
        提取成功返回 ``True``，失败返回 ``False``。
    """
    from scholaraio.papers import read_meta, write_meta

    paper_d = json_path.parent
    data = read_meta(paper_d)

    # Skip L3 for non-article types (thesis, book, document, etc.)
    paper_type = (data.get("paper_type") or "").lower().strip()
    if paper_type in L3_SKIP_TYPES:
        if data.get("l3_extraction_method") == "skipped":
            return True  # already marked, idempotent
        _log.debug("skipping L3 for paper_type=%s: %s", paper_type, paper_d.name)
        data.pop("l3_conclusion", None)  # clear stale conclusion if any
        data["l3_extraction_method"] = "skipped"
        data["l3_extracted_at"] = datetime.now().isoformat(timespec="seconds")
        write_meta(paper_d, data)
        return True

    if data.get("l3_conclusion") and not force:
        _log.debug("existing L3 (method: %s), skipping", data.get("l3_extraction_method", "?"))
        return True

    lines = md_path.read_text(encoding="utf-8", errors="replace").splitlines()
    if inspect:
        ui(f"  [inspect] loaded paper.md: {len(lines)} lines")

    conclusion = method = None

    # --- Try locating conclusion via existing TOC (skip first LLM call) ---
    toc = data.get("toc")
    if toc:
        if inspect:
            ui(f"  [inspect] TOC available: {len(toc)} entries")
        conclusion, method = _l3_from_toc(lines, toc, config, max_retries, inspect)

    # --- Primary path (when TOC is unavailable) ---
    if conclusion is None:
        headers = _extract_headers(lines)
        if inspect:
            ui(f"  [inspect] primary path: {len(headers)} markdown headers")
        _log.debug("[Primary] found %d headers", len(headers))
        for h in headers:
            _log.debug("  line %4d  %s %s", h["line"], "#" * h["level"], h["text"])
        if headers:
            conclusion, method = _primary_path(lines, headers, config, max_retries, inspect)

    # --- Fallback path ---
    if conclusion is None:
        if inspect:
            ui("  [inspect] fallback path: sending first 100 + last 200 lines")
        _log.debug("[Fallback] Primary path failed, switching to fallback")
        conclusion, method = _fallback_path(lines, config, max_retries, inspect)

    if conclusion is None:
        _log.error("all paths failed to extract conclusion")
        return False

    # Write back
    data["l3_conclusion"] = conclusion
    data["l3_extraction_method"] = method
    data["l3_extracted_at"] = datetime.now().isoformat(timespec="seconds")
    write_meta(paper_d, data)
    _log.debug("L3 written to JSON (method: %s, %d chars)", method, len(conclusion))
    return True


# ============================================================================
#  L3 from TOC (no extra LLM call for header identification)
# ============================================================================

_CONCLUSION_KEYWORDS = re.compile(r"\b(conclusion|conclusions|concluding|summary|closing)\b", re.IGNORECASE)


def _l3_from_toc(
    lines: list[str],
    toc: list[dict],
    config: Config,
    max_retries: int,
    inspect: bool,
) -> tuple[str | None, str | None]:
    """用已有 TOC 定位结论节，Python 截取，LLM 校验。"""
    # Find conclusion entry in TOC
    conclusion_entry = None
    for entry in toc:
        if _CONCLUSION_KEYWORDS.search(entry.get("title", "")):
            conclusion_entry = entry
            break

    if not conclusion_entry:
        _log.debug("[TOC] no conclusion section found in TOC, switching to Primary")
        return None, None

    start_line = conclusion_entry["line"]
    _log.debug("[TOC] found conclusion: line %d %s", start_line, conclusion_entry["title"])

    # Find end: next TOC entry after conclusion
    end_line = None
    found = False
    for entry in toc:
        if found:
            end_line = entry["line"] - 1
            break
        if entry["line"] == start_line:
            found = True

    extracted = _slice_lines(lines, start_line, end_line)
    if inspect:
        ui(
            f"  [inspect] TOC slice: lines {start_line}-{end_line or 'EOF'}, "
            f"{_count_lines(extracted)} lines, {len(extracted)} chars"
        )
    _log.debug("[TOC] extracted lines %d-%s, %d chars", start_line, end_line or "EOF", len(extracted))

    cleaned, reason = _validate_and_clean(extracted, config)
    _log.debug("[TOC] validate: %s %s", "PASS" if cleaned else "FAIL", reason)
    if cleaned:
        return cleaned, "toc"

    return None, None


# ============================================================================
#  Primary path
# ============================================================================


_REAL_SECTION_RE = re.compile(
    r"^(?:"
    r"\d[\d.]*[\s.]|"  # 阿拉伯数字编号: 1, 1.1, 2., etc.
    r"[IVX]+[\s.)]|"  # 罗马数字: I., II., IV.
    r"[A-F][\s.)]|"  # 字母编号: A., B.
    r"(?:abstract|introduction|method|result|discussion|"
    r"conclusion|concluding|summary|reference|bibliography|"
    r"appendix|acknowledge|funding|credit|declaration|"
    r"data\s+avail|author\s+contrib|conflict)\b"
    r")",
    re.IGNORECASE,
)


def _is_real_section(title: str) -> bool:
    """判断标题是否为真实节标题（非 running header）。"""
    return bool(_REAL_SECTION_RE.match(title.strip()))


def _extract_headers(lines: list[str]) -> list[dict]:
    """提取所有 # 标题及行号（1-indexed）。"""
    headers = []
    for i, line in enumerate(lines, start=1):
        m = re.match(r"^(#{1,4})\s+(.+)", line.rstrip())
        if m:
            headers.append({"line": i, "level": len(m.group(1)), "text": m.group(2).strip()})
    return headers


def _primary_path(
    lines: list[str],
    headers: list[dict],
    config: Config,
    max_retries: int,
    inspect: bool,
) -> tuple[str | None, str | None]:
    header_list = "\n".join(f"Line {h['line']}: {'#' * h['level']} {h['text']}" for h in headers)
    prompt = (
        "Below are all section headers (with line numbers) from an academic paper markdown file.\n"
        "Identify the header that marks the START of the conclusion section "
        "(may be named 'Conclusion', 'Conclusions', 'Concluding Remarks', 'Summary', etc.).\n\n"
        f"{header_list}\n\n"
        'Return JSON only: {"line": <line_number>, "header": "<header_text>"}\n'
        'If no conclusion section exists, return: {"line": null, "header": null}'
    )
    if inspect:
        ui(
            f"  [inspect] primary prompt: {len(headers)} headers, "
            f"{_count_lines(prompt)} lines, {len(prompt)} chars"
        )

    # 1 initial attempt + max_retries retries; range(1, ...) so attempt number is 1-based
    for attempt in range(1, max_retries + 2):
        try:
            if inspect:
                ui(f"  [inspect] primary attempt {attempt}: waiting for LLM")
            result = _parse_json(_call_llm(prompt, config))
            start_line = result.get("line")
            if inspect:
                ui(
                    f"  [inspect] primary attempt {attempt}: "
                    f"line={start_line!r}, header={result.get('header')!r}"
                )
            if not start_line:
                _log.debug("[Primary #%d] LLM found no conclusion", attempt)
                return None, None

            # Find end: next REAL section header after start_line
            # Skip running headers (no section number, short text)
            end_line = None
            for h in headers:
                if h["line"] > start_line and _is_real_section(h["text"]):
                    end_line = h["line"] - 1
                    break

            extracted = _slice_lines(lines, start_line, end_line)
            if inspect:
                ui(
                    f"  [inspect] primary slice: lines {start_line}-{end_line or 'EOF'}, "
                    f"{_count_lines(extracted)} lines, {len(extracted)} chars"
                )
            _log.debug(
                "[Primary #%d] extracted lines %d-%s, %d chars", attempt, start_line, end_line or "EOF", len(extracted)
            )

            cleaned, reason = _validate_and_clean(extracted, config, inspect=inspect)
            _log.debug("[Primary #%d] validate: %s %s", attempt, "PASS" if cleaned else "FAIL", reason)
            if inspect:
                ui(f"  [inspect] primary validate: {'PASS' if cleaned else 'FAIL'} {reason}")
            if cleaned:
                return cleaned, f"primary-attempt{attempt}"

        except Exception as e:
            _log.debug("[Primary #%d] exception: %s", attempt, e)

    return None, None


# ============================================================================
#  Fallback path
# ============================================================================


def _fallback_path(
    lines: list[str],
    config: Config,
    max_retries: int,
    inspect: bool,
) -> tuple[str | None, str | None]:
    n = len(lines)

    # Send first 100 + last 200 lines (conclusion is usually near the end)
    if n <= 300:
        sample = "\n".join(f"{i + 1}: {l}" for i, l in enumerate(lines))
    else:
        head = "\n".join(f"{i + 1}: {l}" for i, l in enumerate(lines[:100]))
        tail_start = max(100, n - 200)
        tail = "\n".join(f"{tail_start + i + 1}: {l}" for i, l in enumerate(lines[tail_start:]))
        sample = f"[Lines 1–100]\n{head}\n\n...[中间省略]...\n\n[Lines {tail_start + 1}–{n}]\n{tail}"

    prompt = (
        "Find the conclusion section in this academic paper (markdown format). "
        "Return the 1-indexed line number where the conclusion STARTS and where it ENDS "
        "(last line before References/Appendix/end of file).\n\n"
        f"{sample}\n\n"
        'Return JSON only: {"start_line": <N>, "end_line": <N>}\n'
        'If no conclusion exists, return: {"start_line": null, "end_line": null}'
    )
    if inspect:
        ui(f"  [inspect] fallback prompt: {_count_lines(prompt)} lines, {len(prompt)} chars")

    # 1 initial attempt + max_retries retries; range(1, ...) so attempt number is 1-based
    for attempt in range(1, max_retries + 2):
        try:
            if inspect:
                ui(f"  [inspect] fallback attempt {attempt}: waiting for LLM")
            result = _parse_json(_call_llm(prompt, config))
            start_line = result.get("start_line")
            end_line = result.get("end_line")
            if inspect:
                ui(f"  [inspect] fallback attempt {attempt}: start={start_line!r}, end={end_line!r}")
            if not start_line:
                _log.debug("[Fallback #%d] LLM found no conclusion", attempt)
                return None, None

            extracted = _slice_lines(lines, start_line, end_line)
            if inspect:
                ui(
                    f"  [inspect] fallback slice: lines {start_line}-{end_line or 'EOF'}, "
                    f"{_count_lines(extracted)} lines, {len(extracted)} chars"
                )
            _log.debug(
                "[Fallback #%d] extracted lines %d-%s, %d chars", attempt, start_line, end_line or "EOF", len(extracted)
            )

            cleaned, reason = _validate_and_clean(extracted, config, inspect=inspect)
            _log.debug("[Fallback #%d] validate: %s %s", attempt, "PASS" if cleaned else "FAIL", reason)
            if inspect:
                ui(f"  [inspect] fallback validate: {'PASS' if cleaned else 'FAIL'} {reason}")
            if cleaned:
                return cleaned, f"fallback-attempt{attempt}"

        except Exception as e:
            _log.debug("[Fallback #%d] exception: %s", attempt, e)

    return None, None


# ============================================================================
#  LLM validation + cleaning
# ============================================================================


def _validate_and_clean(text: str, config: Config, *, inspect: bool = False) -> tuple[str | None, str]:
    """校验并清理提取的结论文本。

    返回 (cleaned_text, reason)：
    - cleaned_text 为 None 表示文本不包含有效结论内容
    - cleaned_text 为清理后的纯结论文本（去除标题行、Acknowledgments 等）
    """
    if len(text.strip()) < 100:
        return None, "文本过短"

    prompt = (
        "The following text was extracted as the conclusion section of an academic paper. "
        "Your tasks:\n"
        "1. Check if it contains actual conclusion content (summary of findings, contributions, or future work).\n"
        "2. If yes, return a CLEANED version:\n"
        "   - Remove the section header line (e.g. '# 6. Conclusion', '# Concluding Remarks')\n"
        "   - Remove any in-text running headers (e.g. '# Author and others', '# Journal Name')\n"
        "   - Remove everything AFTER the conclusion ends: Acknowledgments, Funding statements, "
        "CRediT authorship statements, Declaration of interests/competing interest, "
        "Data availability, Author ORCIDs, Author contributions, conflict of interest, etc.\n"
        "   - Keep only the actual conclusion/summary paragraphs. Do NOT truncate mid-sentence.\n"
        "3. If it contains NO conclusion content at all, set conclusion to null.\n\n"
        f"{text}\n\n"
        'Return JSON only: {"conclusion": "<cleaned text or null>", "reason": "<one sentence>"}'
    )
    if inspect:
        ui(
            f"  [inspect] clean prompt: {_count_lines(text)} input lines, {len(text)} input chars, "
            f"{_count_lines(prompt)} prompt lines, {len(prompt)} prompt chars"
        )
    try:
        if inspect:
            ui("  [inspect] clean step: waiting for LLM")
        result = _parse_json(_call_llm(prompt, config, timeout=config.llm.timeout_clean))
        cleaned = result.get("conclusion")
        reason = result.get("reason") or ""
        if not cleaned or len(cleaned.strip()) < 50:
            return None, reason or "无有效结论内容"
        return cleaned.strip(), reason
    except Exception as e:
        return None, f"校验异常：{e}"


# ============================================================================
#  LLM + JSON utilities
# ============================================================================


def _call_llm(prompt: str, config: Config, timeout: int | None = None) -> str:
    from scholaraio.metrics import call_llm

    result = call_llm(prompt, config, timeout=timeout, purpose="loader")
    return result.content


def _parse_json(text: str) -> dict:
    text = text.strip()
    # Strip markdown code fences if present
    text = re.sub(r"^```\w*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Fix unescaped backslashes (e.g. LaTeX: \alpha, \vec, \frac).
        # Only runs when initial parse fails. Valid JSON escapes are
        # preserved: \" \\ \/ \b \f \n \r \t \uXXXX
        fixed = re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", text)
        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            # If escaping made things worse, raise with original text
            return json.loads(text)


def _slice_lines(lines: list[str], start: int, end: int | None) -> str:
    """1-indexed, inclusive on both ends."""
    s = max(0, start - 1)
    e = end if end is not None else len(lines)
    return "\n".join(lines[s:e]).strip()


def _count_lines(text: str) -> int:
    """Return the number of lines in a possibly empty text block."""
    if not text:
        return 0
    return text.count("\n") + 1
