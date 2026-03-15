"""
pdf_to_markdown.py — 使用本地 MinerU 服务将 PDF 转换为 Markdown
================================================================

调用本地部署的 MinerU API (默认 http://localhost:8000) 将 PDF 文件转换为
Markdown 格式。支持单文件转换和批量处理整个目录。

前置条件
--------
    需要先启动 MinerU API 服务，默认监听 http://localhost:8000。
    可用 ``python3 pdf_to_markdown.py status`` 检查服务是否在线。

工作流程
--------
1. 扫描 PDF 文件 (单个或目录批量)
2. 检查是否已有同名 .md 输出 — 有则跳过 (--force 可强制重新转换)
3. 调用 MinerU POST /file_parse 接口上传 PDF 并获取 Markdown
4. 将 Markdown 写入指定目录 (默认与 PDF 同目录), 文件名 = PDF名.md
5. 可选同时保存 content_list JSON (--save-content-list)

输出文件
--------
    <pdf_stem>.md               主输出, MinerU 转换后的 Markdown
    <pdf_stem>_content_list.json  (可选) MinerU 结构化内容列表

    输出位置: 默认与 PDF 同目录, 可通过 -o/--output-dir 指定统一输出目录。

命令行用法
----------
    # 检查 MinerU 服务状态
    python3 pdf_to_markdown.py status
    python3 pdf_to_markdown.py status --api-url http://host:port

    # 单文件转换
    python3 pdf_to_markdown.py convert paper.pdf
    python3 pdf_to_markdown.py convert paper.pdf -o ./output/       # 指定输出目录
    python3 pdf_to_markdown.py convert paper.pdf --start-page 0 --end-page 10
    python3 pdf_to_markdown.py convert paper.pdf --backend vlm-auto-engine
    python3 pdf_to_markdown.py convert paper.pdf --lang en          # 英文 PDF
    python3 pdf_to_markdown.py convert paper.pdf --parse-method ocr # 强制 OCR
    python3 pdf_to_markdown.py convert paper.pdf --no-formula --no-table
    python3 pdf_to_markdown.py convert paper.pdf --save-content-list
    python3 pdf_to_markdown.py convert paper.pdf --dry-run          # 预览, 不写文件

    # 批量处理
    python3 pdf_to_markdown.py batch ./papers/                      # 目录下所有 PDF
    python3 pdf_to_markdown.py batch ./papers/ -r                   # 递归子目录
    python3 pdf_to_markdown.py batch ./papers/ -o ./md_output/      # 指定输出目录
    python3 pdf_to_markdown.py batch ./papers/ --force              # 重新转换已有 .md 的
    python3 pdf_to_markdown.py batch ./papers/ --dry-run            # 干跑预览
    python3 pdf_to_markdown.py batch ./papers/ --backend pipeline --lang ch

公共选项 (convert / batch 共用)
-------------------------------
    -o, --output-dir DIR    输出目录 (默认: 与 PDF 同目录)
    --api-url URL           MinerU 服务地址 (默认: http://localhost:8000)
    --backend BACKEND       解析后端 (见下方, 默认: pipeline)
    --lang LANG             OCR 语言: ch(中英), en, latin 等 (默认: ch)
    --parse-method METHOD   PDF 解析方式: auto / txt / ocr (默认: auto)
    --no-formula            禁用公式解析
    --no-table              禁用表格解析
    --save-content-list     同时保存 content_list JSON
    --dry-run               预览操作, 不实际写文件

MinerU 后端选项 (--backend)
---------------------------
    pipeline            通用, 支持多语言, 无幻觉 (默认, 推荐)
    vlm-auto-engine     本地算力高精度, 仅中英文
    vlm-http-client     远程算力高精度 (OpenAI 兼容), 仅中英文
    hybrid-auto-engine  新一代本地高精度, 支持多语言
    hybrid-http-client  远程+少量本地算力, 支持多语言

依赖
----
    Python 3.10+, requests
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import requests

_log = logging.getLogger(__name__)

# ============================================================================
#  Constants
# ============================================================================

DEFAULT_API_URL = "http://localhost:8000"
PARSE_ENDPOINT = "/file_parse"

VALID_BACKENDS = [
    "pipeline",
    "vlm-auto-engine",
    "vlm-http-client",
    "hybrid-auto-engine",
    "hybrid-http-client",
]

DEFAULT_BACKEND = "pipeline"
DEFAULT_LANG = "ch"
API_TIMEOUT = 600  # PDF parsing can take a long time


# ============================================================================
#  Data Structures
# ============================================================================


@dataclass
class ConvertResult:
    """单次 PDF → Markdown 转换的结果。

    Attributes:
        pdf_path: 原始 PDF 文件路径。
        md_path: 输出的 Markdown 文件路径，转换失败时为 ``None``。
        success: 转换是否成功。
        pages_parsed: 解析的页数。
        elapsed_seconds: 转换耗时（秒）。
        error: 失败时的错误信息，成功时为 ``None``。
        md_size: 输出 Markdown 的字节数。
    """

    pdf_path: Path
    md_path: Path | None = None
    success: bool = False
    pages_parsed: int = 0
    elapsed_seconds: float = 0.0
    error: str | None = None
    md_size: int = 0  # bytes


@dataclass
class ConvertOptions:
    """PDF 转换选项。

    Attributes:
        api_url: MinerU 服务地址。
        output_dir: 输出目录，为 ``None`` 时与 PDF 同目录。
        backend: MinerU 解析后端（``pipeline`` | ``vlm-auto-engine`` 等）。
        lang: OCR 语言（``ch`` | ``en`` | ``latin`` 等）。
        parse_method: 解析方式（``auto`` | ``txt`` | ``ocr``）。
        formula_enable: 是否启用公式解析。
        table_enable: 是否启用表格解析。
        start_page: 起始页（0-indexed）。
        end_page: 结束页（0-indexed）。
        save_content_list: 是否同时保存 content_list JSON。
        force: 是否强制重新转换已有 ``.md`` 的文件。
        dry_run: 预览模式，不写文件。
    """

    api_url: str = DEFAULT_API_URL
    output_dir: Path | None = None
    backend: str = DEFAULT_BACKEND
    lang: str = DEFAULT_LANG
    parse_method: str = "auto"
    formula_enable: bool = True
    table_enable: bool = True
    start_page: int = 0
    end_page: int = 99999
    save_content_list: bool = False
    force: bool = False
    dry_run: bool = False


# ============================================================================
#  MinerU API
# ============================================================================


def check_server(api_url: str = DEFAULT_API_URL) -> bool:
    """检查 MinerU 服务是否可达。

    Args:
        api_url: MinerU 服务地址，默认 ``http://localhost:8000``。

    Returns:
        可达返回 ``True``，不可达返回 ``False``。
    """
    try:
        resp = requests.get(f"{api_url}/docs", timeout=5)
        return resp.status_code == 200
    except requests.ConnectionError:
        return False


def convert_pdf(pdf_path: Path, opts: ConvertOptions) -> ConvertResult:
    """通过 MinerU API 将单个 PDF 转换为 Markdown。

    将 PDF 上传到本地 MinerU 服务，接收 Markdown 内容并写入磁盘。

    Args:
        pdf_path: PDF 文件路径。
        opts: 转换选项（API 地址、后端、输出目录等）。

    Returns:
        :class:`ConvertResult` 实例，包含转换结果和状态。
    """
    result = ConvertResult(pdf_path=pdf_path)
    t0 = time.time()

    # Determine output path
    if opts.output_dir:
        out_dir = opts.output_dir
        out_dir.mkdir(parents=True, exist_ok=True)
    else:
        out_dir = pdf_path.parent

    md_path = out_dir / (pdf_path.stem + ".md")
    result.md_path = md_path

    # Dry run: just report what would happen
    if opts.dry_run:
        exists_tag = " (exists, would overwrite)" if md_path.exists() else ""
        _log.debug("dry-run: %s%s", md_path.name, exists_tag)
        result.success = True
        return result

    # Skip if already exists (unless --force)
    if md_path.exists() and not opts.force:
        _log.debug("skip (already exists): %s", md_path.name)
        result.success = True
        result.md_path = md_path
        return result

    # Build multipart form data
    url = f"{opts.api_url}{PARSE_ENDPOINT}"

    form_data = {
        "backend": (None, opts.backend),
        "parse_method": (None, opts.parse_method),
        "formula_enable": (None, str(opts.formula_enable).lower()),
        "table_enable": (None, str(opts.table_enable).lower()),
        "return_md": (None, "true"),
        "return_middle_json": (None, "false"),
        "return_content_list": (None, str(opts.save_content_list).lower()),
        "return_model_output": (None, "false"),
        "return_images": (None, "false"),
        "start_page_id": (None, str(opts.start_page)),
        "end_page_id": (None, str(opts.end_page)),
    }

    # lang_list needs to be sent as repeated form fields
    # requests handles this via the files parameter
    try:
        with open(pdf_path, "rb") as f:
            files = {
                "files": (pdf_path.name, f, "application/pdf"),
            }
            # Add lang_list as a form field
            form_data["lang_list"] = (None, opts.lang)

            resp = requests.post(url, files={**files, **form_data}, timeout=API_TIMEOUT)

    except requests.ConnectionError:
        result.error = f"Cannot connect to MinerU server at {opts.api_url}"
        result.elapsed_seconds = time.time() - t0
        return result
    except requests.Timeout:
        result.error = f"Request timed out after {API_TIMEOUT}s"
        result.elapsed_seconds = time.time() - t0
        return result

    result.elapsed_seconds = time.time() - t0

    if resp.status_code != 200:
        result.error = f"HTTP {resp.status_code}: {resp.text[:200]}"
        return result

    # Parse response
    try:
        data = resp.json()
    except ValueError:
        result.error = "Invalid JSON response from server"
        return result

    # Extract markdown content from response
    # MinerU API returns a list (one entry per uploaded file)
    md_content = _extract_markdown(data)
    if md_content is None:
        result.error = f"No markdown content in response. Keys: {list(data.keys()) if isinstance(data, dict) else type(data).__name__}"
        return result

    # Write markdown
    md_path.write_text(md_content, encoding="utf-8")
    result.success = True
    result.md_size = len(md_content.encode("utf-8"))

    # Optionally save content_list JSON
    if opts.save_content_list:
        cl = _extract_field(data, "content_list")
        if cl:
            cl_path = out_dir / (pdf_path.stem + "_content_list.json")
            cl_path.write_text(json.dumps(cl, ensure_ascii=False, indent=2), encoding="utf-8")

    _log.info("-> %s (%s, %.1fs)", md_path.name, _fmt_size(result.md_size), result.elapsed_seconds)
    return result


def _extract_markdown(data) -> str | None:
    """Extract markdown text from MinerU API response.

    Actual response format (MinerU ≥2.7):
        {
          "backend": "pipeline",
          "version": "2.7.6",
          "results": {
            "<filename_stem>": {
              "md_content": "..."
            }
          }
        }
    """
    if not isinstance(data, dict):
        return None

    # Primary path: results → {filename} → md_content
    results = data.get("results")
    if isinstance(results, dict):
        for _filename, entry in results.items():
            if isinstance(entry, dict):
                md = entry.get("md_content")
                if isinstance(md, str) and md.strip():
                    return md

    # Fallback: direct md_content at top level
    for key in ("md_content", "md", "markdown", "content"):
        if key in data and isinstance(data[key], str) and data[key].strip():
            return data[key]

    return None


def _extract_field(data, field_name):
    """Extract a named field from MinerU API response.

    Navigates: data["results"][first_key][field_name]
    """
    if not isinstance(data, dict):
        return None
    results = data.get("results")
    if isinstance(results, dict):
        for _filename, entry in results.items():
            if isinstance(entry, dict) and field_name in entry:
                return entry[field_name]
    return data.get(field_name)


# ============================================================================
#  Cloud API
# ============================================================================

CLOUD_API_URL = "https://mineru.net/api/v4"
CLOUD_POLL_INTERVAL = 5  # seconds between status checks
CLOUD_TIMEOUT = 600  # max wait time for cloud parsing


def convert_pdf_cloud(
    pdf_path: Path,
    opts: ConvertOptions,
    *,
    api_key: str,
    cloud_url: str = CLOUD_API_URL,
) -> ConvertResult:
    """通过 MinerU 云 API 将单个 PDF 转换为 Markdown。

    工作流程: 获取签名上传 URL → PUT 上传 PDF → 轮询解析结果 → 下载 Markdown。
    本地 MinerU 不可达时作为 :func:`convert_pdf` 的 fallback 使用。

    Args:
        pdf_path: PDF 文件路径。
        opts: 转换选项（输出目录、后端、语言等，复用本地 API 的选项结构）。
        api_key: MinerU 云 API 密钥（Bearer token）。
        cloud_url: MinerU 云 API 基础 URL，默认 ``https://mineru.net/api/v4``。

    Returns:
        :class:`ConvertResult` 实例，包含转换结果和状态。
    """
    result = ConvertResult(pdf_path=pdf_path)
    t0 = time.time()

    out_dir = opts.output_dir if opts.output_dir else pdf_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / (pdf_path.stem + ".md")
    result.md_path = md_path

    if opts.dry_run:
        exists_tag = " (exists, would overwrite)" if md_path.exists() else ""
        _log.debug("dry-run [cloud]: %s%s", md_path.name, exists_tag)
        result.success = True
        return result

    if md_path.exists() and not opts.force:
        _log.debug("skip (already exists): %s", md_path.name)
        result.success = True
        return result

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # Step 1: request signed upload URL
    data_id = pdf_path.stem
    payload: dict = {
        "files": [{"name": pdf_path.name, "data_id": data_id}],
        "model_version": opts.backend,
        "enable_formula": opts.formula_enable,
        "enable_table": opts.table_enable,
        "language": opts.lang,
    }
    if opts.parse_method == "ocr":
        payload["is_ocr"] = True

    try:
        resp = requests.post(
            f"{cloud_url}/file-urls/batch",
            headers=headers,
            json=payload,
            timeout=30,
        )
    except requests.RequestException as e:
        result.error = f"云 API 请求失败: {e}"
        result.elapsed_seconds = time.time() - t0
        return result

    if resp.status_code != 200:
        result.error = f"云 API HTTP {resp.status_code}: {resp.text[:200]}"
        result.elapsed_seconds = time.time() - t0
        return result

    try:
        resp_data = resp.json()
    except ValueError:
        result.error = "云 API 返回非 JSON 响应"
        result.elapsed_seconds = time.time() - t0
        return result
    if resp_data.get("code") != 0:
        result.error = f"云 API 错误: {resp_data.get('msg', resp.text[:200])}"
        result.elapsed_seconds = time.time() - t0
        return result

    batch_data = resp_data.get("data", {})
    batch_id = batch_data.get("batch_id", "")
    file_urls = batch_data.get("file_urls", [])
    if not file_urls:
        result.error = "云 API 未返回上传 URL"
        result.elapsed_seconds = time.time() - t0
        return result

    # file_urls is a list of URL strings
    upload_url = file_urls[0] if isinstance(file_urls[0], str) else file_urls[0].get("url", "")
    if not upload_url:
        result.error = "云 API 返回的上传 URL 为空"
        result.elapsed_seconds = time.time() - t0
        return result

    # Step 2: upload PDF via PUT
    try:
        with open(pdf_path, "rb") as f:
            put_resp = requests.put(
                upload_url,
                data=f,
                timeout=120,
            )
        if put_resp.status_code not in (200, 201):
            result.error = f"PDF 上传失败: HTTP {put_resp.status_code}"
            result.elapsed_seconds = time.time() - t0
            return result
    except requests.RequestException as e:
        result.error = f"PDF 上传失败: {e}"
        result.elapsed_seconds = time.time() - t0
        return result

    _log.debug("PDF uploaded, cloud parsing (batch_id: %s...)", batch_id[:12])

    # Step 3: poll for results
    poll_headers = {"Authorization": f"Bearer {api_key}"}
    deadline = time.time() + CLOUD_TIMEOUT

    while time.time() < deadline:
        time.sleep(CLOUD_POLL_INTERVAL)
        try:
            poll_resp = requests.get(
                f"{cloud_url}/extract-results/batch/{batch_id}",
                headers=poll_headers,
                timeout=30,
            )
        except requests.RequestException:
            continue

        if poll_resp.status_code != 200:
            continue

        try:
            poll_data = poll_resp.json()
        except ValueError:
            continue
        if poll_data.get("code") != 0:
            continue

        extract_results = poll_data.get("data", {}).get("extract_result", [])
        if not extract_results:
            continue

        item = extract_results[0]
        state = item.get("state", "")

        if state == "failed":
            result.error = f"云端解析失败: {item.get('err_msg', 'unknown')}"
            result.elapsed_seconds = time.time() - t0
            return result

        if state == "done":
            md_content = _download_cloud_result(item, out_dir)
            if md_content is None:
                result.error = f"无法从云端结果提取 Markdown。响应键: {list(item.keys())}"
                result.elapsed_seconds = time.time() - t0
                return result

            md_path.write_text(md_content, encoding="utf-8")
            result.success = True
            result.md_size = len(md_content.encode("utf-8"))
            result.elapsed_seconds = time.time() - t0
            _log.info(
                "-> [cloud] %s (%s, %.1fs)",
                md_path.name,
                _fmt_size(result.md_size),
                result.elapsed_seconds,
            )
            return result

        if state == "running":
            extracted = item.get("extracted_pages", "?")
            total = item.get("total_pages", "?")
            _log.debug("cloud parsing... %s/%s pages", extracted, total)

    result.error = f"云端解析超时（{CLOUD_TIMEOUT}s）"
    result.elapsed_seconds = time.time() - t0
    return result


_DEFAULT_CLOUD_BATCH_SIZE = 20  # max files per batch request


def convert_pdfs_cloud_batch(
    pdf_paths: list[Path],
    opts: ConvertOptions,
    *,
    api_key: str,
    cloud_url: str = CLOUD_API_URL,
    batch_size: int = _DEFAULT_CLOUD_BATCH_SIZE,
) -> list[ConvertResult]:
    """通过 MinerU 云 API 批量转换 PDF 为 Markdown。

    所有 PDF 在一个 batch 内提交，并行上传，统一轮询。
    超过 ``batch_size`` 时自动分批。

    Args:
        pdf_paths: PDF 文件路径列表。
        opts: 转换选项。
        api_key: MinerU 云 API 密钥。
        cloud_url: MinerU 云 API 基础 URL。
        batch_size: 每批提交文件数上限，默认 20。可通过
            ``config.yaml`` 的 ``ingest.mineru_batch_size`` 配置。

    Returns:
        与 ``pdf_paths`` 等长的 :class:`ConvertResult` 列表。
    """
    if not pdf_paths:
        return []

    # Split into chunks
    all_results: list[ConvertResult] = []
    for chunk_start in range(0, len(pdf_paths), batch_size):
        chunk = pdf_paths[chunk_start : chunk_start + batch_size]
        chunk_results = _convert_chunk_cloud(chunk, opts, api_key=api_key, cloud_url=cloud_url)
        all_results.extend(chunk_results)
    return all_results


def _convert_chunk_cloud(
    pdf_paths: list[Path],
    opts: ConvertOptions,
    *,
    api_key: str,
    cloud_url: str,
) -> list[ConvertResult]:
    """Process a single batch chunk of PDFs via cloud API."""
    import concurrent.futures

    t0 = time.time()
    out_dir = opts.output_dir if opts.output_dir else pdf_paths[0].parent
    out_dir.mkdir(parents=True, exist_ok=True)

    # Build per-file results and output paths
    results: dict[str, ConvertResult] = {}
    data_id_to_path: dict[str, Path] = {}
    files_payload = []

    for pdf_path in pdf_paths:
        data_id = pdf_path.stem
        md_path = out_dir / (pdf_path.stem + ".md")
        result = ConvertResult(pdf_path=pdf_path, md_path=md_path)

        if md_path.exists() and not opts.force:
            _log.debug("skip (already exists): %s", md_path.name)
            result.success = True
            results[data_id] = result
            continue

        results[data_id] = result
        data_id_to_path[data_id] = pdf_path
        files_payload.append({"name": pdf_path.name, "data_id": data_id})

    if not files_payload:
        return list(results.values())

    # Step 1: request signed upload URLs for all files
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload: dict = {
        "files": files_payload,
        "model_version": opts.backend,
        "enable_formula": opts.formula_enable,
        "enable_table": opts.table_enable,
        "language": opts.lang,
    }
    if opts.parse_method == "ocr":
        payload["is_ocr"] = True

    try:
        resp = requests.post(
            f"{cloud_url}/file-urls/batch",
            headers=headers,
            json=payload,
            timeout=30,
        )
    except requests.RequestException as e:
        for did in data_id_to_path:
            results[did].error = f"云 API 请求失败: {e}"
            results[did].elapsed_seconds = time.time() - t0
        return list(results.values())

    if resp.status_code != 200:
        for did in data_id_to_path:
            results[did].error = f"云 API HTTP {resp.status_code}: {resp.text[:200]}"
            results[did].elapsed_seconds = time.time() - t0
        return list(results.values())

    try:
        resp_data = resp.json()
    except ValueError:
        for did in data_id_to_path:
            results[did].error = "云 API 返回非 JSON 响应"
            results[did].elapsed_seconds = time.time() - t0
        return list(results.values())

    if resp_data.get("code") != 0:
        for did in data_id_to_path:
            results[did].error = f"云 API 错误: {resp_data.get('msg', resp.text[:200])}"
            results[did].elapsed_seconds = time.time() - t0
        return list(results.values())

    batch_data = resp_data.get("data", {})
    batch_id = batch_data.get("batch_id", "")
    file_urls = batch_data.get("file_urls", [])

    if len(file_urls) != len(files_payload):
        for did in data_id_to_path:
            results[did].error = f"云 API 返回 URL 数量不匹配: {len(file_urls)} vs {len(files_payload)}"
            results[did].elapsed_seconds = time.time() - t0
        return list(results.values())

    # Step 2: parallel upload all PDFs
    ordered_data_ids = [f["data_id"] for f in files_payload]

    def _upload_one(idx: int) -> str | None:
        did = ordered_data_ids[idx]
        url = file_urls[idx] if isinstance(file_urls[idx], str) else file_urls[idx].get("url", "")
        pdf_path = data_id_to_path[did]
        try:
            with open(pdf_path, "rb") as f:
                put_resp = requests.put(url, data=f, timeout=120)
            if put_resp.status_code not in (200, 201):
                return f"HTTP {put_resp.status_code}"
        except requests.RequestException as e:
            return str(e)
        return None

    _log.info("Uploading %d PDFs to MinerU cloud (batch %s)...", len(files_payload), batch_id[:12])
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        upload_errors = list(pool.map(_upload_one, range(len(files_payload))))

    for idx, err in enumerate(upload_errors):
        if err:
            did = ordered_data_ids[idx]
            results[did].error = f"PDF 上传失败: {err}"
            results[did].elapsed_seconds = time.time() - t0

    pending_ids = {did for idx, did in enumerate(ordered_data_ids) if not upload_errors[idx]}
    if not pending_ids:
        return list(results.values())

    _log.info("All uploaded, waiting for cloud parsing (%d files)...", len(pending_ids))

    # Step 3: poll for all results
    poll_headers = {"Authorization": f"Bearer {api_key}"}
    deadline = time.time() + CLOUD_TIMEOUT
    done_ids: set[str] = set()

    while time.time() < deadline and done_ids != pending_ids:
        time.sleep(CLOUD_POLL_INTERVAL)
        try:
            poll_resp = requests.get(
                f"{cloud_url}/extract-results/batch/{batch_id}",
                headers=poll_headers,
                timeout=30,
            )
        except requests.RequestException:
            continue

        if poll_resp.status_code != 200:
            continue
        try:
            poll_data = poll_resp.json()
        except ValueError:
            continue
        if poll_data.get("code") != 0:
            continue

        extract_results = poll_data.get("data", {}).get("extract_result", [])
        running_count = 0
        for item in extract_results:
            did = item.get("data_id", "")
            if did not in pending_ids or did in done_ids:
                continue
            state = item.get("state", "")

            if state == "done":
                # Per-file output dir for images etc.
                file_out_dir = out_dir / did
                file_out_dir.mkdir(parents=True, exist_ok=True)
                md_content = _download_cloud_result(item, file_out_dir)
                if md_content is None:
                    results[did].error = "无法从云端结果提取 Markdown"
                else:
                    md_path = results[did].md_path
                    md_path.write_text(md_content, encoding="utf-8")
                    # Move assets from per-file subdir to out_dir (flat)
                    _flatten_assets(file_out_dir, out_dir, did)
                    results[did].success = True
                    results[did].md_size = len(md_content.encode("utf-8"))
                    _log.info(
                        "-> [cloud] %s (%s)",
                        md_path.name,
                        _fmt_size(results[did].md_size),
                    )
                results[did].elapsed_seconds = time.time() - t0
                done_ids.add(did)

            elif state == "failed":
                results[did].error = f"云端解析失败: {item.get('err_msg', 'unknown')}"
                results[did].elapsed_seconds = time.time() - t0
                done_ids.add(did)

            elif state == "running":
                running_count += 1

        if running_count and not done_ids - pending_ids:
            done_count = len(done_ids)
            _log.info("Cloud parsing: %d/%d done, %d running...", done_count, len(pending_ids), running_count)

    # Mark timed-out files
    for did in pending_ids - done_ids:
        results[did].error = f"云端解析超时（{CLOUD_TIMEOUT}s）"
        results[did].elapsed_seconds = time.time() - t0

    elapsed = time.time() - t0
    ok = sum(1 for did in pending_ids if results[did].success)
    _log.info("Batch done: %d/%d succeeded (%.1fs total)", ok, len(pending_ids), elapsed)

    return list(results.values())


def _flatten_assets(src_dir: Path, out_dir: Path, data_id: str) -> None:
    """Move images/ and other assets from per-file subdir to out_dir, namespaced by data_id."""
    images_src = src_dir / "images"
    if images_src.is_dir():
        # Move entire images dir, namespaced to avoid collisions
        images_dst = out_dir / f"{data_id}_images"
        if images_dst.exists():
            import shutil

            shutil.rmtree(str(images_dst))
        images_src.rename(images_dst)

    # Move other assets (layout.json, content_list.json). Skip origin PDFs:
    # they are just MinerU echoes of the input PDF and would be mistaken for
    # fresh inbox items on the next pipeline run.
    for f in src_dir.iterdir():
        if f.is_file():
            if f.suffix.lower() == ".pdf" and "origin" in f.stem.lower():
                f.unlink(missing_ok=True)
                continue
            dest = out_dir / f"{data_id}_{f.name}"
            f.rename(dest)

    # Clean up empty subdir
    try:
        src_dir.rmdir()
    except OSError:
        pass


def _download_cloud_result(item: dict, out_dir: Path) -> str | None:
    """Download markdown (and images) from cloud API result.

    Tries multiple response formats: direct md_content field,
    full_zip_url (download zip and extract all files), or md_url.

    CDN download bypasses HTTP proxy (domestic CDN + proxy = SSL errors).

    Args:
        item: Single extract result dict from cloud API.
        out_dir: Directory to extract images and other assets into.

    Returns:
        Markdown text, or ``None`` on failure.
    """
    def _direct_get(url: str, *, timeout: int):
        # requests.get(..., proxies={...}) is not sufficient in some proxy
        # environments; use a dedicated session with trust_env=False so CDN
        # downloads bypass user-configured SOCKS/HTTP proxies completely.
        sess = requests.Session()
        sess.trust_env = False
        try:
            return sess.get(url, timeout=timeout)
        finally:
            sess.close()

    # Direct markdown content
    md = item.get("md_content")
    if isinstance(md, str) and md.strip():
        return md

    # Download zip and extract all files (md + images/)
    zip_url = item.get("full_zip_url")
    if zip_url:
        try:
            import io
            import zipfile

            # Bypass proxy for CDN downloads (domestic CDN through proxy causes SSL errors)
            resp = _direct_get(zip_url, timeout=120)
            if resp.status_code == 200:
                md_content = None
                with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                    for name in zf.namelist():
                        if name.endswith("/"):
                            continue
                        if name.endswith(".md"):
                            md_content = zf.read(name).decode("utf-8")
                        else:
                            # Extract all assets (images/, layout.json, etc.)
                            dest = out_dir / name
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            dest.write_bytes(zf.read(name))
                return md_content
        except Exception as e:
            _log.debug("failed to download/extract zip result: %s", e)

    # Direct markdown URL
    md_url = item.get("md_url")
    if md_url:
        try:
            resp = _direct_get(md_url, timeout=60)
            if resp.status_code == 200:
                return resp.text
        except Exception as e:
            _log.debug("failed to download markdown from %s: %s", md_url, e)

    return None


# ============================================================================
#  Utilities
# ============================================================================


def _fmt_size(nbytes: int) -> str:
    """Format byte count as human-readable string."""
    if nbytes < 1024:
        return f"{nbytes} B"
    elif nbytes < 1024 * 1024:
        return f"{nbytes / 1024:.1f} KB"
    else:
        return f"{nbytes / (1024 * 1024):.1f} MB"


def _find_pdfs(dirpath: Path, recursive: bool = False) -> list[Path]:
    """Find all PDF files in a directory."""
    pattern = "**/*.pdf" if recursive else "*.pdf"
    return sorted(dirpath.glob(pattern))


# ============================================================================
#  Long PDF splitting & merging
# ============================================================================

DEFAULT_CHUNK_PAGES = 100


def _get_pdf_page_count(pdf_path: Path) -> int:
    """Get the number of pages in a PDF.

    Tries pymupdf first, falls back to pikepdf.

    Returns:
        Page count, or -1 if unable to determine.
    """
    try:
        import pymupdf

        with pymupdf.open(pdf_path) as doc:
            return len(doc)
    except ImportError:
        pass
    try:
        import pikepdf

        with pikepdf.open(pdf_path) as pdf:
            return len(pdf.pages)
    except ImportError:
        pass
    _log.warning("cannot detect page count (install pymupdf or pikepdf): %s", pdf_path.name)
    return -1


def _split_pdf(pdf_path: Path, chunk_size: int = DEFAULT_CHUNK_PAGES, output_dir: Path | None = None) -> list[Path]:
    """Split a long PDF into multiple smaller PDFs.

    Args:
        pdf_path: Original PDF file path.
        chunk_size: Maximum pages per chunk.
        output_dir: Where to write chunks (default: ``.{stem}_chunks/``
            next to the PDF).

    Returns:
        List of chunk PDF paths in page order.
        If total pages <= chunk_size, returns ``[pdf_path]`` unchanged.
    """
    try:
        import pymupdf
    except ImportError:
        raise ImportError("pymupdf is required for splitting long PDFs. Install it with: pip install pymupdf")

    page_count = _get_pdf_page_count(pdf_path)
    if page_count <= chunk_size:
        return [pdf_path]

    if output_dir is None:
        output_dir = pdf_path.parent / f".{pdf_path.stem}_chunks"
    output_dir.mkdir(parents=True, exist_ok=True)

    chunks: list[Path] = []
    with pymupdf.open(pdf_path) as src_doc:
        for start in range(0, page_count, chunk_size):
            end = min(start + chunk_size, page_count)  # exclusive
            chunk_name = f"{pdf_path.stem}_p{start:04d}-{end - 1:04d}.pdf"
            chunk_path = output_dir / chunk_name

            if chunk_path.exists():
                # Idempotent: reuse existing chunk
                chunks.append(chunk_path)
                continue

            chunk_doc = pymupdf.open()
            chunk_doc.insert_pdf(src_doc, from_page=start, to_page=end - 1)
            chunk_doc.save(str(chunk_path))
            chunk_doc.close()
            chunks.append(chunk_path)

    _log.info("split %s (%d pages) into %d chunks of ≤%d pages", pdf_path.name, page_count, len(chunks), chunk_size)
    return chunks


def _merge_chunk_results(
    chunk_results: list[ConvertResult],
    original_pdf: Path,
    output_dir: Path,
) -> ConvertResult:
    """Merge multiple chunk ConvertResults into a single result.

    Handles:
    - Markdown text concatenation
    - Image file deduplication/renaming (``c{idx}_{name}`` prefix)
    - Error aggregation for partial failures

    Args:
        chunk_results: Ordered ConvertResult list (one per chunk).
        original_pdf: The original unsplit PDF path.
        output_dir: Final output directory for merged result.

    Returns:
        Merged ConvertResult.
    """
    merged = ConvertResult(pdf_path=original_pdf)
    final_md_path = output_dir / (original_pdf.stem + ".md")
    final_images_dir = output_dir / "images"

    md_parts: list[str] = []
    errors: list[str] = []
    total_elapsed = 0.0

    for idx, cr in enumerate(chunk_results):
        total_elapsed += cr.elapsed_seconds

        if not cr.success:
            errors.append(f"chunk {idx}: {cr.error}")
            continue

        if not cr.md_path or not cr.md_path.exists():
            errors.append(f"chunk {idx}: md file not found")
            continue

        chunk_md = cr.md_path.read_text(encoding="utf-8", errors="replace")

        # Find chunk's images directory
        chunk_images_dir = None
        for candidate in [
            cr.md_path.parent / "images",
            cr.md_path.parent / f"{cr.md_path.stem}_mineru_images",
            cr.md_path.parent / f"{cr.md_path.stem}_images",
        ]:
            if candidate.is_dir():
                chunk_images_dir = candidate
                break

        if chunk_images_dir and any(chunk_images_dir.iterdir()):
            final_images_dir.mkdir(parents=True, exist_ok=True)
            for img_file in sorted(chunk_images_dir.iterdir()):
                if not img_file.is_file():
                    continue
                new_name = f"c{idx:02d}_{img_file.name}"
                new_path = final_images_dir / new_name
                shutil.copy2(img_file, new_path)
                # Remap image references in markdown
                old_ref = f"images/{img_file.name}"
                new_ref = f"images/{new_name}"
                chunk_md = chunk_md.replace(old_ref, new_ref)
                # Also handle _mineru_images/ variant
                for prefix in [f"{cr.md_path.stem}_mineru_images", f"{cr.md_path.stem}_images"]:
                    old_ref2 = f"{prefix}/{img_file.name}"
                    chunk_md = chunk_md.replace(old_ref2, new_ref)

        md_parts.append(chunk_md)

    if not md_parts:
        merged.error = "all chunks failed: " + "; ".join(errors)
        merged.elapsed_seconds = total_elapsed
        return merged

    final_md = "\n\n".join(md_parts)
    final_md_path.write_text(final_md, encoding="utf-8")

    merged.success = True
    merged.md_path = final_md_path
    merged.md_size = len(final_md.encode("utf-8"))
    merged.elapsed_seconds = total_elapsed

    if errors:
        _log.warning("some chunks failed during merge: %s", "; ".join(errors))

    return merged


def _convert_long_pdf(pdf_path: Path, opts: ConvertOptions, chunk_size: int = DEFAULT_CHUNK_PAGES) -> ConvertResult:
    """Handle a long PDF: split → convert each chunk → merge results.

    Uses the local MinerU API for each chunk sequentially.
    """
    out_dir = opts.output_dir if opts.output_dir else pdf_path.parent
    chunks_dir = out_dir / f".{pdf_path.stem}_chunks"

    chunk_paths = _split_pdf(pdf_path, chunk_size=chunk_size, output_dir=chunks_dir)

    chunk_results: list[ConvertResult] = []
    for i, chunk_pdf in enumerate(chunk_paths):
        _log.info("converting chunk %d/%d: %s", i + 1, len(chunk_paths), chunk_pdf.name)
        chunk_opts = ConvertOptions(
            api_url=opts.api_url,
            output_dir=chunks_dir,
            backend=opts.backend,
            lang=opts.lang,
            parse_method=opts.parse_method,
            formula_enable=opts.formula_enable,
            table_enable=opts.table_enable,
            save_content_list=opts.save_content_list,
            force=opts.force,
            dry_run=opts.dry_run,
        )
        cr = convert_pdf(chunk_pdf, chunk_opts)
        chunk_results.append(cr)

    merged = _merge_chunk_results(chunk_results, pdf_path, out_dir)

    if merged.success and chunks_dir.exists():
        shutil.rmtree(chunks_dir)
        _log.debug("cleaned up chunks dir: %s", chunks_dir)

    return merged


def _convert_long_pdf_cloud(
    pdf_path: Path,
    opts: ConvertOptions,
    *,
    api_key: str,
    cloud_url: str,
    chunk_size: int = DEFAULT_CHUNK_PAGES,
) -> ConvertResult:
    """Handle a long PDF via cloud API: split → batch upload → merge."""
    out_dir = opts.output_dir if opts.output_dir else pdf_path.parent
    chunks_dir = out_dir / f".{pdf_path.stem}_chunks"

    chunk_paths = _split_pdf(pdf_path, chunk_size=chunk_size, output_dir=chunks_dir)

    chunk_opts = ConvertOptions(
        output_dir=chunks_dir,
        backend=opts.backend,
        lang=opts.lang,
        parse_method=opts.parse_method,
        formula_enable=opts.formula_enable,
        table_enable=opts.table_enable,
        save_content_list=opts.save_content_list,
    )
    batch_results = convert_pdfs_cloud_batch(
        chunk_paths,
        chunk_opts,
        api_key=api_key,
        cloud_url=cloud_url,
    )

    merged = _merge_chunk_results(batch_results, pdf_path, out_dir)

    if merged.success and chunks_dir.exists():
        shutil.rmtree(chunks_dir)
        _log.debug("cleaned up chunks dir: %s", chunks_dir)

    return merged


# ============================================================================
#  CLI Commands
# ============================================================================


def cmd_status(args: argparse.Namespace) -> None:
    """Check MinerU server status."""
    api_url = args.api_url
    _log.info("Checking MinerU server at %s", api_url)
    if check_server(api_url):
        _log.info("Server is UP and reachable")
    else:
        _log.error("Server is DOWN or unreachable at %s", api_url)
        sys.exit(1)


def cmd_convert(args: argparse.Namespace) -> None:
    """Convert a single PDF file."""
    pdf_path = Path(args.file).resolve()
    if not pdf_path.exists():
        _log.error("File not found: %s", pdf_path)
        sys.exit(1)
    if pdf_path.suffix.lower() != ".pdf":
        _log.warning("%s does not have .pdf extension", pdf_path.name)

    opts = _build_options(args)

    _log.info("Converting: %s", pdf_path.name)
    if opts.dry_run:
        _log.debug("dry run - no files will be written")

    result = convert_pdf(pdf_path, opts)

    if not result.success:
        _log.error("FAILED: %s", result.error)
        sys.exit(1)


def cmd_batch(args: argparse.Namespace) -> None:
    """Batch-convert all PDFs in a directory."""
    dirpath = Path(args.directory).resolve()
    if not dirpath.is_dir():
        _log.error("Not a directory: %s", dirpath)
        sys.exit(1)

    opts = _build_options(args)

    # Find PDFs
    all_pdfs = _find_pdfs(dirpath, recursive=args.recursive)
    if not all_pdfs:
        _log.info("No PDF files found in %s", dirpath)
        return

    # Determine output dir for skip-check
    out_dir = opts.output_dir if opts.output_dir else None

    # Filter already-converted (unless --force)
    if opts.force or opts.dry_run:
        targets = all_pdfs
    else:
        targets = []
        for p in all_pdfs:
            check_dir = out_dir if out_dir else p.parent
            md_file = check_dir / (p.stem + ".md")
            if not md_file.exists():
                targets.append(p)

    skipped = len(all_pdfs) - len(targets)
    if not targets:
        msg = "No unprocessed PDFs in %s"
        if all_pdfs:
            msg += " (use --force to reconvert)"
        _log.info(msg, dirpath)
        return

    if skipped:
        _log.info("Found %d PDF(s) to convert (%d skipped, already have .md)", len(targets), skipped)
    else:
        _log.info("Found %d PDF(s) to convert", len(targets))
    if opts.dry_run:
        _log.debug("dry run - no files will be written")

    # Check server before starting batch
    if not opts.dry_run:
        if not check_server(opts.api_url):
            _log.error("MinerU server not reachable at %s", opts.api_url)
            sys.exit(1)

    succeeded = 0
    failed = 0
    total = len(targets)

    for i, pdf_path in enumerate(targets, 1):
        _log.info("[%d/%d] %s", i, total, pdf_path.name)
        try:
            result = convert_pdf(pdf_path, opts)
            if result.success:
                succeeded += 1
            else:
                _log.error("FAILED: %s", result.error)
                failed += 1
        except Exception as e:
            _log.error("ERROR: %s", e)
            failed += 1

    # Summary
    _log.info("Batch complete: %d succeeded, %d failed, %d skipped", succeeded, failed, skipped)


def _build_options(args: argparse.Namespace) -> ConvertOptions:
    """Build ConvertOptions from parsed CLI arguments."""
    opts = ConvertOptions(
        api_url=args.api_url,
        backend=args.backend,
        lang=args.lang,
        formula_enable=not args.no_formula,
        table_enable=not args.no_table,
        force=getattr(args, "force", False),
        dry_run=getattr(args, "dry_run", False),
        save_content_list=getattr(args, "save_content_list", False),
    )
    if hasattr(args, "output_dir") and args.output_dir:
        opts.output_dir = Path(args.output_dir).resolve()
    if hasattr(args, "start_page") and args.start_page is not None:
        opts.start_page = args.start_page
    if hasattr(args, "end_page") and args.end_page is not None:
        opts.end_page = args.end_page
    if hasattr(args, "parse_method") and args.parse_method:
        opts.parse_method = args.parse_method
    return opts


# ============================================================================
#  Argument Parser
# ============================================================================


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    """Add arguments shared by convert and batch subcommands."""
    parser.add_argument(
        "-o",
        "--output-dir",
        type=str,
        default=None,
        help="Output directory for .md files (default: same as PDF)",
    )
    parser.add_argument(
        "--api-url",
        type=str,
        default=DEFAULT_API_URL,
        help=f"MinerU server URL (default: {DEFAULT_API_URL})",
    )
    parser.add_argument(
        "--backend",
        type=str,
        default=DEFAULT_BACKEND,
        choices=VALID_BACKENDS,
        help=f"MinerU parsing backend (default: {DEFAULT_BACKEND})",
    )
    parser.add_argument(
        "--lang",
        type=str,
        default=DEFAULT_LANG,
        help=f"OCR language: ch, en, latin, etc. (default: {DEFAULT_LANG})",
    )
    parser.add_argument(
        "--parse-method",
        type=str,
        default="auto",
        choices=["auto", "txt", "ocr"],
        help="PDF parse method (default: auto)",
    )
    parser.add_argument(
        "--no-formula",
        action="store_true",
        help="Disable formula parsing",
    )
    parser.add_argument(
        "--no-table",
        action="store_true",
        help="Disable table parsing",
    )
    parser.add_argument(
        "--save-content-list",
        action="store_true",
        help="Also save content_list JSON from MinerU",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be done, without writing files",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="scholaraio ingest mineru",
        description="Convert PDF files to Markdown using local MinerU server.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- status ---
    p_status = sub.add_parser("status", help="Check MinerU server status")
    p_status.add_argument(
        "--api-url",
        type=str,
        default=DEFAULT_API_URL,
        help=f"MinerU server URL (default: {DEFAULT_API_URL})",
    )

    # --- convert (single file) ---
    p_convert = sub.add_parser("convert", help="Convert a single PDF to Markdown")
    p_convert.add_argument("file", type=str, help="Path to PDF file")
    p_convert.add_argument(
        "--start-page",
        type=int,
        default=None,
        help="Start page (0-indexed, default: 0)",
    )
    p_convert.add_argument(
        "--end-page",
        type=int,
        default=None,
        help="End page (0-indexed, default: all pages)",
    )
    _add_common_args(p_convert)

    # --- batch ---
    p_batch = sub.add_parser("batch", help="Batch-convert all PDFs in a directory")
    p_batch.add_argument("directory", type=str, help="Directory containing PDF files")
    p_batch.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="Recurse into subdirectories",
    )
    p_batch.add_argument(
        "--force",
        action="store_true",
        help="Reconvert PDFs that already have .md output",
    )
    _add_common_args(p_batch)

    args = parser.parse_args()
    if args.command == "status":
        cmd_status(args)
    elif args.command == "convert":
        cmd_convert(args)
    elif args.command == "batch":
        cmd_batch(args)


if __name__ == "__main__":
    main()
