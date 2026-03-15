<div align="center">

<!-- TODO: Replace with actual logo when available -->
<!-- <img src="docs/assets/logo.png" width="200" alt="ScholarAIO Logo"> -->

# ScholarAIO

**Scholar All-In-One — a knowledge infrastructure for AI agents.**

[English](README.md) | [中文](README_CN.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![MCP Tools](https://img.shields.io/badge/MCP_Tools-31-green.svg)](scholaraio/mcp_server.py)
[![Claude Code Skills](https://img.shields.io/badge/Claude_Code_Skills-22-purple.svg)](.claude/skills/)

</div>

---

Your coding agent already reads code, writes code, and runs experiments. ScholarAIO gives it a structured knowledge base of your research papers — so the same agent that writes your code can also search your literature, cross-check results against published findings, reproduce methods from papers, and draft your manuscript. One terminal, one agent, the full research loop.

<!-- TODO: Add demo GIF here -->
<!-- <div align="center">
  <img src="docs/assets/demo.gif" width="700" alt="ScholarAIO Demo">
</div> -->

## Quick Start

```bash
# 1. Install
git clone https://github.com/ZimoLiao/scholaraio.git && cd scholaraio
pip install -e ".[full]"

# 2. Configure
cp config.local.example.yaml config.local.yaml
# Add your API keys (both optional — see Configuration below)

# 3. Go
claude    # Launch Claude Code in the project directory — that's it
```

> Or use the CLI directly: `scholaraio search "your topic"` | MCP server: `scholaraio-mcp`

## What It Does

|  | Feature | Details |
|--|---------|---------|
| **PDF Parsing** | Deep structure extraction | [MinerU](https://github.com/opendatalab/MinerU) → Markdown with figures, tables, LaTeX equations preserved. Long PDFs (>100 pp) are auto-split and merged |
| **Not Just Papers** | Any document goes in | Journal articles, theses, technical reports, standards, lecture notes — three inboxes for different document types, each with tailored metadata handling |
| **Hybrid Search** | Keyword + semantic fusion | Keyword + semantic embeddings → RRF ranking |
| **Topic Discovery** | Auto-clustering | BERTopic + 6 interactive HTML visualizations — works on both your library and explore datasets |
| **Literature Exploration** | Multi-dimensional discovery | OpenAlex with 9 filter dimensions (journal, concept, author, institution, keyword, source type, year, citations, work type) → embed → cluster → search |
| **Citation Graph** | References & impact | Forward/backward citations, shared references across your library |
| **Layered Reading** | Read at the depth you need | L1 metadata → L2 abstract → L3 conclusion → L4 full text |
| **Multi-Source Import** | Bring your existing library | Endnote XML/RIS, Zotero (API + SQLite, with collection → workspace mapping), PDF, Markdown — more sources planned |
| **Workspaces** | Organize for projects | Paper subsets with scoped search and BibTeX export |
| **Persistent Notes** | Cross-session memory | Agent analysis is saved per-paper (`notes.md`). Revisiting a paper reuses prior findings instead of re-reading the full text — saves tokens and avoids redundant work |
| **Academic Writing** | AI-assisted drafting | Literature review, paper sections, citation check, rebuttal, gap analysis — every claim traceable to your own library |
| **MCP Server** | 31 tools | Works with Claude Desktop, Cursor, and any MCP client |

## Beyond Paper Management

ScholarAIO parses PDFs into clean Markdown with accurate LaTeX and figure attachments. This means your coding agent doesn't just *read* papers — it can:

- **Reproduce methods** — read an algorithm description, write the implementation, run it
- **Verify claims** — extract data from figures and tables, compute independently, cross-check
- **Explore formulas** — pick up where a derivation leaves off, test boundary cases numerically
- **Visualize results** — plot data from papers alongside your own experiments

The knowledge base is the foundation; what your agent builds on top of it is open-ended.

## Works With Your Agent

ScholarAIO is designed to be **agent-agnostic**. It currently ships with configuration for multiple agents and IDEs:

| Agent / IDE | Integration | Config file |
|-------------|-------------|-------------|
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | Full skills + instructions | `CLAUDE.md` + `.claude/skills/` |
| [Cursor](https://cursor.sh) | Instructions wrapper | `.cursorrules` |
| [Windsurf](https://codeium.com/windsurf) | Instructions wrapper | `.windsurfrules` |
| [Cline](https://github.com/cline/cline) | Instructions + skills | `.clinerules` + `.claude/skills/` |
| [GitHub Copilot](https://github.com/features/copilot) | Instructions wrapper | `.github/copilot-instructions.md` |
| [Codex](https://openai.com/codex) / OpenClaw | Full instructions + skills | `AGENTS.md` + `.agents/skills/` |

The **MCP server** (`scholaraio-mcp`, 31 tools) works with any MCP-compatible client. Skills follow the open [AgentSkills.io](https://agentskills.io) standard — `.agents/skills/` is a symlink to `.claude/skills/` for cross-agent discovery.

**Use without cloning** — install as a Claude Code plugin into any project:

```bash
/plugin marketplace add ZimoLiao/scholaraio
/plugin install scholaraio@scholaraio-marketplace
# Skills available as /scholaraio:search, /scholaraio:show, etc.
```

**Migrating from existing tools?** Import directly from Endnote (XML/RIS) and Zotero (Web API or local SQLite) — your PDFs, metadata, and references come along. More import sources are on the roadmap.

## How It Works

```
PDF → MinerU → Structured Markdown (figures + LaTeX intact)
                    ↓
          Metadata extraction (regex + LLM cross-validation)
          API enrichment (Crossref / Semantic Scholar / OpenAlex)
                    ↓
          DOI dedup → data/papers/<Author-Year-Title>/
                    ↓
      ┌─────────────┼─────────────┐
   FTS5 Index    FAISS Vectors   BERTopic
   (keyword)     (semantic)      (clustering)
      └─────────────┼─────────────┘
                    ↓
        Your agent (Claude Code / Cursor / CLI / MCP / ...)
```

## Configuration

Main config: `config.yaml` (tracked). Secrets: `config.local.yaml` (gitignored).

| Key | Purpose | Get it |
|-----|---------|--------|
| LLM backend | Metadata extraction, enrichment, academic discussion | Set `llm.api_key` in `config.local.yaml`, or use env vars: `SCHOLARAIO_LLM_API_KEY` (universal), `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY` / `GEMINI_API_KEY`. Default backend: [DeepSeek](https://platform.deepseek.com/); also supports Claude, Gemini, Ollama, any OpenAI-compatible API, and local `codex login` via `llm.backend: codex-cli` |
| `MINERU_API_KEY` | PDF → structured Markdown | Free at [mineru.net](https://mineru.net/apiManage/token) or [self-host](https://github.com/opendatalab/MinerU) |

> **Both are optional.** Without LLM: regex-only extraction. Without MinerU: place `.md` files in `data/inbox/` directly.

Embedding model (Qwen3-Embedding-0.6B, ~1.2 GB) auto-downloads on first use. Default source: ModelScope (no proxy needed in China). International users: set `embed.source: huggingface` in config.

Full config reference → [`config.yaml`](config.yaml)

## Three Ways to Use

| Mode | Best for | Command |
|------|----------|---------|
| **Agent** (recommended) | Full research workflow — conversational | `claude` / your preferred agent in project dir |
| **MCP Server** | Claude Desktop / Cursor / any MCP client | `scholaraio-mcp` |
| **CLI** | Scripting, quick queries | `scholaraio --help` |

<details>
<summary><strong>CLI command reference</strong></summary>

**Search & Read**
```
scholaraio search QUERY       Keyword search
scholaraio vsearch QUERY      Semantic vector search
scholaraio usearch QUERY      Unified search (keyword + semantic fusion)
scholaraio search-author NAME Search by author
scholaraio top-cited          Rank by citation count
scholaraio show PAPER         View paper content (L1-L4)
```

**Ingest & Enrich**
```
scholaraio pipeline PRESET    Run ingestion pipeline (full|ingest|enrich|reindex)
scholaraio index              Build keyword search index
scholaraio embed              Generate semantic vectors
scholaraio enrich-toc         Extract table of contents
scholaraio enrich-l3          Extract conclusions
scholaraio backfill-abstract  Backfill missing abstracts
scholaraio refetch            Re-fetch citation counts from APIs
```

**Citation Graph**
```
scholaraio refs PAPER         View references
scholaraio citing PAPER       View citing papers
scholaraio shared-refs A B    Shared references between papers
```

**Explore & Topics**
```
scholaraio explore fetch ...  Literature exploration (OpenAlex multi-filter)
scholaraio explore search ... Search within an explore library
scholaraio topics             BERTopic topic modeling
```

**Import & Export**
```
scholaraio import-endnote     Import from Endnote
scholaraio import-zotero      Import from Zotero
scholaraio attach-pdf         Attach PDF to existing paper
scholaraio export bibtex      Export BibTeX
scholaraio ws init NAME       Create a workspace
scholaraio ws add NAME PAPER  Add papers to workspace
scholaraio ws search NAME Q   Search within workspace
```

**Maintenance**
```
scholaraio audit              Data quality audit
scholaraio repair             Fix metadata
scholaraio rename             Standardize directory names
scholaraio migrate-dirs       Migrate legacy directory structure
scholaraio setup              Setup wizard
scholaraio metrics            View LLM usage stats
```

</details>

## Project Structure

```
scholaraio/          # Python package — CLI, MCP server, and all core modules
  ingest/            #   PDF parsing + metadata extraction pipeline
  sources/           #   Data source adapters (local / Endnote / Zotero)

.claude/skills/      # 22 agent skills (AgentSkills.io format)
.agents/skills/      # ↑ symlink for cross-agent discovery
data/papers/         # Your paper library (gitignored)
data/inbox/          # Drop PDFs here for ingestion
```

Full module reference → [`CLAUDE.md`](CLAUDE.md) or [`AGENTS.md`](AGENTS.md)

## Citation

If you use ScholarAIO in your research, please cite:

```bibtex
@software{scholaraio,
  author = {Liao, Zi-Mo},
  title = {ScholarAIO: AI-Native Research Terminal},
  year = {2026},
  url = {https://github.com/ZimoLiao/scholaraio},
  license = {MIT}
}
```

## License

[MIT](LICENSE) © 2026 Zi-Mo Liao
