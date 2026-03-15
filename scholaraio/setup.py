"""
setup.py — ScholarAIO 环境检测与交互式安装向导
================================================

两种模式：
  scholaraio setup          交互式向导（bilingual EN/ZH）
  scholaraio setup check    环境状态诊断
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from scholaraio.config import Config, load_config

# ============================================================================
#  Bilingual strings
# ============================================================================

Lang = str  # "en" | "zh"

_S: dict[str, dict[Lang, str]] = {
    # -- check labels --
    "python_ver": {"en": "Python version", "zh": "Python 版本"},
    "core_deps": {"en": "Core deps", "zh": "核心依赖"},
    "embed_deps": {"en": "Embed deps", "zh": "嵌入依赖"},
    "topics_deps": {"en": "Topics deps", "zh": "主题依赖"},
    "import_deps": {"en": "Import deps", "zh": "导入依赖"},
    "config_yaml": {"en": "config.yaml", "zh": "config.yaml"},
    "llm_key": {"en": "LLM API key", "zh": "LLM API key"},
    "mineru": {"en": "MinerU", "zh": "MinerU"},
    "contact_email": {"en": "Contact email", "zh": "联系邮箱"},
    "directories": {"en": "Directories", "zh": "目录结构"},
    "papers_count": {"en": "Papers", "zh": "论文数量"},
    # -- check status --
    "installed": {"en": "installed", "zh": "已安装"},
    "not_installed": {"en": "not installed", "zh": "未安装"},
    "found": {"en": "found", "zh": "已找到"},
    "not_found": {"en": "not found", "zh": "未找到"},
    "configured": {"en": "configured", "zh": "已配置"},
    "not_set": {"en": "not set", "zh": "未设置"},
    "all_ok": {"en": "all exist", "zh": "全部存在"},
    # -- wizard --
    "lang_prompt": {
        "en": "Language / 语言选择:\n  1. English\n  2. 中文",
        "zh": "Language / 语言选择:\n  1. English\n  2. 中文",
    },
    "welcome": {"en": "\n=== ScholarAIO Setup Wizard ===\n", "zh": "\n=== ScholarAIO 安装向导 ===\n"},
    "step_deps": {"en": "Step 1: Checking dependencies...", "zh": "步骤 1: 检查依赖..."},
    "step_config": {"en": "Step 2: Configuration file", "zh": "步骤 2: 配置文件"},
    "step_keys": {
        "en": "Step 3: API keys (stored in config.local.yaml, not tracked by git)",
        "zh": "步骤 3: API 密钥（保存在 config.local.yaml，不进 git）",
    },
    "step_verify": {"en": "Step 4: Verification", "zh": "步骤 4: 验证"},
    "install_prompt": {
        "en": "  {group} deps missing: {pkgs}\n  Install? (pip install scholaraio[{group}])",
        "zh": "  {group} 依赖缺失: {pkgs}\n  是否安装？(pip install scholaraio[{group}])",
    },
    "yn": {"en": " [Y/n] ", "zh": " [Y/n] "},
    "skip": {"en": "  Skipped.", "zh": "  已跳过。"},
    "installing": {"en": "  Installing {group}...", "zh": "  正在安装 {group}..."},
    "install_ok": {"en": "  Installed successfully.", "zh": "  安装成功。"},
    "install_fail": {
        "en": "  Installation failed. You can install later with: pip install scholaraio[{group}]",
        "zh": "  安装失败。你可以稍后手动安装: pip install scholaraio[{group}]",
    },
    "config_exists": {"en": "  config.yaml already exists, skipping.", "zh": "  config.yaml 已存在，跳过。"},
    "config_created": {
        "en": "  Created config.yaml with default settings.",
        "zh": "  已创建 config.yaml（默认配置）。",
    },
    "llm_key_prompt": {
        "en": "  LLM API key (DeepSeek / OpenAI / Anthropic / Google).\n"
        "  Without it: metadata extraction degrades to regex-only, enrich unavailable.\n"
        "  Press Enter to skip.",
        "zh": "  LLM API key（DeepSeek / OpenAI / Anthropic / Google）。\n"
        "  不配置：元数据提取降级为纯正则，enrich 不可用。\n"
        "  按 Enter 跳过。",
    },
    "mineru_key_prompt": {
        "en": "  MinerU cloud API key (register at https://mineru.net/apiManage/token).\n"
        "  Without it: only .md files can be ingested, PDF conversion unavailable.\n"
        "  Press Enter to skip.",
        "zh": "  MinerU 云 API key（注册 https://mineru.net/apiManage/token 获取）。\n"
        "  不配置：只能入库 .md 文件，不能直接处理 PDF。\n"
        "  按 Enter 跳过。",
    },
    "email_prompt": {
        "en": "  Contact email (for Crossref polite pool — faster API responses).\n  Press Enter to skip.",
        "zh": "  联系邮箱（Crossref polite pool，配置后 API 更快）。\n  按 Enter 跳过。",
    },
    "key_saved": {"en": "  Saved to config.local.yaml.", "zh": "  已保存到 config.local.yaml。"},
    "no_keys": {
        "en": "  No keys configured. You can add them later in config.local.yaml.",
        "zh": "  未配置任何密钥。你可以稍后在 config.local.yaml 中添加。",
    },
    "import_hint": {
        "en": "\nTip: To import papers from Zotero or Endnote, use:\n"
        "  scholaraio import-endnote <xml-or-ris-file>\n"
        "  scholaraio import-zotero --source web --collection <name>\n",
        "zh": "\nTip: 导入 Zotero 或 Endnote 文献，使用：\n"
        "  scholaraio import-endnote <xml 或 ris 文件>\n"
        "  scholaraio import-zotero --source web --collection <name>\n",
    },
    "done": {
        "en": "\nSetup complete! Put papers in data/inbox/ and run:\n  scholaraio pipeline ingest\n",
        "zh": "\n配置完成！将论文放入 data/inbox/，然后运行：\n  scholaraio pipeline ingest\n",
    },
}


def t(key: str, lang: Lang) -> str:
    """Translate a string key to the specified language."""
    return _S.get(key, {}).get(lang, key)


# ============================================================================
#  Dependency checking
# ============================================================================

# (import_name, pip_name)
_DEP_GROUPS: dict[str, list[tuple[str, str]]] = {
    "core": [("requests", "requests"), ("yaml", "pyyaml")],
    "embed": [("sentence_transformers", "sentence-transformers"), ("faiss", "faiss-cpu"), ("numpy", "numpy")],
    "topics": [("bertopic", "bertopic"), ("pandas", "pandas")],
    "import": [("endnote_utils", "endnote-utils"), ("pyzotero", "pyzotero")],
}


@dataclass
class DepGroupStatus:
    """Dependency group check result."""

    name: str
    installed: bool
    missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def check_dep_group(group: str) -> DepGroupStatus:
    """Check if all packages in a dependency group are importable.

    Args:
        group: Dependency group name (core/embed/topics/import).

    Returns:
        DepGroupStatus with installed flag and list of missing pip package names.
    """
    pairs = _DEP_GROUPS.get(group, [])
    missing = []
    errors = []
    for import_name, pip_name in pairs:
        try:
            importlib.import_module(import_name)
        except ImportError:
            missing.append(pip_name)
        except Exception as exc:
            errors.append(f"{pip_name}: {exc}")
    return DepGroupStatus(name=group, installed=not missing and not errors, missing=missing, errors=errors)


# ============================================================================
#  Status checks
# ============================================================================


@dataclass
class CheckResult:
    """Single check result."""

    label: str
    ok: bool
    detail: str


def run_check(cfg: Config | None = None, lang: Lang = "zh") -> list[CheckResult]:
    """Run all environment checks.

    Args:
        cfg: Config instance. If None, loads default config.
        lang: Display language.

    Returns:
        List of CheckResult items.
    """
    if cfg is None:
        cfg = load_config()

    results: list[CheckResult] = []

    # Python version
    vi = sys.version_info
    ver_str = f"{vi.major}.{vi.minor}.{vi.micro}"
    results.append(
        CheckResult(
            label=t("python_ver", lang),
            ok=vi >= (3, 10),
            detail=ver_str + (" ✓" if vi >= (3, 10) else " (need ≥3.10)"),
        )
    )

    # Dependency groups
    for group, label_key in [
        ("core", "core_deps"),
        ("embed", "embed_deps"),
        ("topics", "topics_deps"),
        ("import", "import_deps"),
    ]:
        status = check_dep_group(group)
        if status.installed:
            pkgs = ", ".join(p for _, p in _DEP_GROUPS[group])
            results.append(CheckResult(t(label_key, lang), True, pkgs))
        else:
            hint = f"pip install scholaraio[{group}]"
            details = []
            if status.missing:
                details.append(f"{t('not_installed', lang)}: {', '.join(status.missing)}")
            if status.errors:
                details.append(f"runtime import error: {'; '.join(status.errors)}")
            results.append(
                CheckResult(
                    t(label_key, lang),
                    False,
                    f"{' | '.join(details)}  → {hint}",
                )
            )

    # config.yaml
    root = cfg._root
    config_path = root / "config.yaml"
    results.append(
        CheckResult(
            t("config_yaml", lang),
            config_path.exists(),
            t("found", lang) if config_path.exists() else t("not_found", lang),
        )
    )

    # LLM API key
    key = cfg.resolved_api_key()
    if cfg.llm.backend == "codex-cli":
        if shutil.which("codex"):
            results.append(CheckResult(t("llm_key", lang), True, "codex login"))
        else:
            results.append(CheckResult(t("llm_key", lang), False, "codex CLI not found"))
    elif key:
        masked = key[:3] + "***" + key[-3:] if len(key) > 8 else "***"
        results.append(CheckResult(t("llm_key", lang), True, f"{t('configured', lang)} ({masked})"))
    else:
        results.append(CheckResult(t("llm_key", lang), False, t("not_set", lang)))

    # MinerU
    mineru_ok, mineru_detail = _check_mineru(cfg, lang)
    results.append(CheckResult(t("mineru", lang), mineru_ok, mineru_detail))

    # Contact email
    email = cfg.ingest.contact_email
    if email:
        results.append(CheckResult(t("contact_email", lang), True, email))
    else:
        results.append(CheckResult(t("contact_email", lang), False, t("not_set", lang)))

    # Directories
    dirs_to_check = [
        cfg.papers_dir,
        cfg._root / "data" / "inbox",
        cfg._root / "data" / "pending",
        cfg._root / "workspace",
    ]
    missing_dirs = [str(d) for d in dirs_to_check if not d.exists()]
    if missing_dirs:
        results.append(
            CheckResult(
                t("directories", lang),
                False,
                f"{t('not_found', lang)}: {', '.join(missing_dirs)}",
            )
        )
    else:
        results.append(CheckResult(t("directories", lang), True, t("all_ok", lang)))

    # Papers count
    papers_dir = cfg.papers_dir
    count = 0
    if papers_dir.exists():
        count = sum(1 for d in papers_dir.iterdir() if d.is_dir() and (d / "meta.json").exists())
    results.append(CheckResult(t("papers_count", lang), True, str(count)))

    return results


def _check_mineru(cfg: Config, lang: Lang) -> tuple[bool, str]:
    """Check MinerU availability (cloud key or local server)."""
    cloud_key = cfg.resolved_mineru_api_key()
    if cloud_key:
        return True, "cloud API key " + t("configured", lang)

    try:
        import requests as _req

        r = _req.get(cfg.ingest.mineru_endpoint, timeout=2)
        if r.status_code < 500:
            return True, f"local server @ {cfg.ingest.mineru_endpoint}"
    except Exception:
        pass

    return False, t("not_set", lang)


def format_check_results(results: list[CheckResult]) -> str:
    """Format check results as a readable table.

    Args:
        results: List of CheckResult from run_check().

    Returns:
        Formatted string with [OK]/[--] prefixes.
    """
    lines = []
    max_label = max(len(r.label) for r in results) if results else 0
    for r in results:
        mark = "[OK]" if r.ok else "[--]"
        lines.append(f"  {mark} {r.label:<{max_label}}  {r.detail}")
    return "\n".join(lines)


# ============================================================================
#  Interactive wizard
# ============================================================================


def run_wizard(cfg: Config | None = None) -> None:
    """Interactive setup wizard (bilingual EN/ZH).

    Args:
        cfg: Config instance. If None, loads default config.
    """
    # Language selection
    print(_S["lang_prompt"]["en"])
    choice = input("> ").strip()
    lang: Lang = "en" if choice == "1" else "zh"

    if cfg is None:
        cfg = load_config()
    root = cfg._root

    print(t("welcome", lang))

    # Step 1: Dependencies
    print(t("step_deps", lang))
    _wizard_deps(lang)

    # Step 2: config.yaml
    print(f"\n{t('step_config', lang)}")
    _wizard_config(root, lang)

    # Reload config after generating config.yaml
    cfg = load_config()
    cfg.ensure_dirs()

    # Step 3: API keys
    print(f"\n{t('step_keys', lang)}")
    _wizard_keys(root, lang)

    # Import hint
    print(t("import_hint", lang))

    # Step 4: Verify
    print(f"{t('step_verify', lang)}")
    cfg = load_config()  # reload with new keys
    results = run_check(cfg, lang)
    print(format_check_results(results))

    print(t("done", lang))


def _wizard_deps(lang: Lang) -> None:
    """Check and optionally install missing dependency groups."""
    for group in ("core", "embed", "topics", "import"):
        status = check_dep_group(group)
        label_key = f"{group}_deps"
        if status.installed:
            pkgs = ", ".join(p for _, p in _DEP_GROUPS[group])
            print(f"  [OK] {t(label_key, lang)}: {pkgs}")
        else:
            msg = t("install_prompt", lang).format(group=group, pkgs=", ".join(status.missing))
            print(msg)
            ans = input(t("yn", lang)).strip().lower()
            if ans in ("", "y", "yes"):
                print(t("installing", lang).format(group=group))
                ret = subprocess.run(
                    [sys.executable, "-m", "pip", "install", f"scholaraio[{group}]"],
                    capture_output=True,
                    text=True,
                )
                if ret.returncode == 0:
                    print(t("install_ok", lang))
                else:
                    print(t("install_fail", lang).format(group=group))
                    if ret.stderr:
                        # show last 3 lines of error
                        err_lines = ret.stderr.strip().splitlines()[-3:]
                        for line in err_lines:
                            print(f"    {line}")
            else:
                print(t("skip", lang))


def _wizard_config(root: Path, lang: Lang) -> None:
    """Generate config.yaml if it doesn't exist."""
    config_path = root / "config.yaml"
    if config_path.exists():
        print(t("config_exists", lang))
        return

    config_path.write_text(_CONFIG_TEMPLATE, encoding="utf-8")
    print(t("config_created", lang))


def _wizard_keys(root: Path, lang: Lang) -> None:
    """Interactively configure API keys, write to config.local.yaml."""
    local_path = root / "config.local.yaml"
    local_data: dict = {}
    if local_path.exists():
        local_data = yaml.safe_load(local_path.read_text(encoding="utf-8")) or {}

    changed = False

    # LLM key
    print(t("llm_key_prompt", lang))
    key = input("  > ").strip()
    if key:
        local_data.setdefault("llm", {})["api_key"] = key
        changed = True

    # MinerU key
    print(t("mineru_key_prompt", lang))
    key = input("  > ").strip()
    if key:
        local_data.setdefault("ingest", {})["mineru_api_key"] = key
        changed = True

    # Contact email
    print(t("email_prompt", lang))
    email = input("  > ").strip()
    if email:
        local_data.setdefault("ingest", {})["contact_email"] = email
        changed = True

    if changed:
        local_path.write_text(
            yaml.dump(local_data, allow_unicode=True, default_flow_style=False),
            encoding="utf-8",
        )
        print(t("key_saved", lang))
    else:
        print(t("no_keys", lang))


# ============================================================================
#  Config template
# ============================================================================

_CONFIG_TEMPLATE = """\
# ScholarAIO configuration
# Sensitive values (API keys) go in config.local.yaml (git-ignored).

paths:
  papers_dir: data/papers
  index_db: data/index.db

# LLM backend (multi-provider support)
# API key: set in config.local.yaml or env var
#   SCHOLARAIO_LLM_API_KEY (generic fallback), or provider-specific:
#   DEEPSEEK_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY / GOOGLE_API_KEY / GEMINI_API_KEY
llm:
  backend: openai-compat   # openai-compat | anthropic | google
  model: deepseek-chat
  base_url: https://api.deepseek.com
  timeout: 30
  timeout_toc: 120
  timeout_clean: 90

# Ingestion pipeline
ingest:
  extractor: robust         # auto | regex | llm | robust
  mineru_endpoint: http://localhost:8000
  mineru_cloud_url: https://mineru.net/api/v4
  abstract_llm_mode: verify # off | fallback | verify

# Semantic embeddings (Qwen3-Embedding-0.6B, ~1.2 GB, auto-downloaded)
embed:
  model: Qwen/Qwen3-Embedding-0.6B
  cache_dir: ~/.cache/modelscope/hub/models
  device: auto              # auto | cpu | cuda
  top_k: 10
  source: modelscope        # modelscope | huggingface

search:
  top_k: 20

logging:
  level: INFO
  file: data/scholaraio.log
  max_bytes: 10000000
  backup_count: 3
  metrics_db: data/metrics.db

topics:
  min_topic_size: 5
  nr_topics: -1             # 0=auto, -1=no merging, positive=target count
  model_dir: data/topic_model
"""
