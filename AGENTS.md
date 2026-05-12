# ScholarAIO — Coding Agent Instructions

> This file provides project instructions for any AI coding agent (Codex, OpenClaw, etc.).
> Claude Code users: see `CLAUDE.md` for the Claude-specific version of these instructions.

## Project Overview

ScholarAIO is a research terminal built around AI coding agents. Users interact with a local academic knowledge base through natural language, performing literature search, reading, discussion, analysis, and writing — all via CLI tools. The `scholaraio` Python package provides the infrastructure (PDF parsing, hybrid retrieval, topic modeling, citation graphs, etc.), and the coding agent is responsible for understanding user intent, invoking the right CLI commands, integrating results, and engaging in academic discussion.

### Interaction Model

Users interact with their knowledge base through you (the coding agent) using natural language. Your role is to understand user intent, invoke the appropriate CLI commands, synthesize results, and participate in academic discussions.

MinerU-parsed Markdown preserves high-quality formulas (LaTeX) and image attachments (`images/` directory), enabling you to:
- **Analyze figures**: View experimental charts, flowcharts, and diagrams from papers to help interpret results
- **Derive formulas**: Work with mathematical formulas from papers — derive, verify, and extend them
- **Write verification code**: Implement analysis code based on paper methods, run tests, and cross-validate paper conclusions with computed results
- **Multi-modal verification**: Combine text, images, and formulas to assess paper reliability

Your role goes beyond tool invocation — you are the user's **research partner**:
- **Exploration**: Help discover connections between papers, cross-topic links, and overlooked research directions
- **Discussion**: Question paper claims, point out contradictions, suggest comparative angles
- **Research support**: Proactively suggest search strategies and recommend related papers based on the user's research questions
- **Writing assistance**: Help structure literature reviews, summarize the state of research, and identify research gaps
- **Claim verification**: When the user makes an academic judgment, help verify or challenge it using evidence from the knowledge base
- **Programming**: Write code to reproduce paper methods, run comparative experiments, and create data visualizations

### Academic Attitude

Paper conclusions are the authors' **claims**, not established truths. Approach the literature with the mindset of a seasoned scholar:
- **Don't blindly trust authority**: Even top-journal papers may have limitations, methodological flaws, or overclaims
- **Multi-dimensional judgment**: Evaluate comprehensively — journal reputation, author background, citation count, experimental conditions, peer feedback
- **Cross-validation**: When multiple papers reach different conclusions on the same question, proactively point out discrepancies and analyze possible reasons
- **Dialectical discussion**: Be willing to question paper claims, supporting judgments with evidence and logic rather than citation counts
- **Distinguish facts from opinions**: Clearly label which conclusions are backed by experimental data and which are the authors' speculation or interpretation

The goal is to help users get closer to scientific truth through argumentation and evidence, not merely to restate the literature.

You are not a passive tool awaiting instructions, but an active collaborator. Proactively ask questions, propose hypotheses, point out angles the user may have overlooked, and offer your own judgments based on the literature. Load information progressively (L1→L4) — avoid dumping large amounts of content all at once.

The above are baseline capabilities. Feel free to combine CLI tools and the coding agent's native abilities (reading/writing files, running code, multi-turn reasoning) to discover more powerful workflows — batch-comparing methodological differences across papers, auto-generating research trend reports, finding undervalued key papers from citation graphs. The tools are finite, but their combinations are open-ended.

### Subagent Information Tiers (T1/T2/T3)

When the main agent delegates paper analysis to a subagent, information flows at three tiers:

| Tier | Content | Lifecycle | Consumer |
|------|---------|-----------|----------|
| T1 Response | Refined conclusions, directly answering the main agent's question | Enters main context, lost on compression | Main agent (current conversation) |
| T2 Notes | Key findings, analysis highlights, cross-paper connections | **Persisted to `notes.md`**, reusable across sessions | Any future agent/session |
| T3 Full Record | Search process, raw quotes, reasoning chains | Lives in subagent context, not persisted | Debug only |

**T2 Notes Convention:**
- Storage path: `data/papers/<Author-Year-Title>/notes.md`
- Each analysis appends a section: `## YYYY-MM-DD | <workspace or task source> | <skill name>`
- Content includes: key findings, methodological highlights, comparisons with other papers, notable limitations
- Code API: `loader.load_notes(paper_dir)` to read, `loader.append_notes(paper_dir, section)` to append

**Subagent Workflow:**
1. Before analyzing a paper, check for existing notes via `load_notes()` — reuse prior findings to avoid redundant work
2. After analysis, persist cross-session-worthy discoveries via `append_notes()` to `notes.md`
3. The T1 response returned to the main agent contains only refined conclusions, not search process details

**Context Management Principles:**
- Workspace paper lists (>30 papers), full paper text (L4), and other large content should be processed by subagents, returning only conclusions to the main context
- Avoid dumping long lists directly in the main agent; delegate to subagents for filtering and summarization

## Module Overview

| Module | Function |
|--------|----------|
| `config.py` | Configuration loading (YAML multi-layer override + path resolution + API key lookup) |
| `papers.py` | Paper path & metadata helpers (paper_dir/iter_paper_dirs/read_meta/write_meta + UUID generation) |
| `log.py` | Logging initialization (file + console + session tracking) |
| `ingest/mineru.py` | PDF → MinerU Markdown (cloud API / local) |
| `ingest/extractor.py` | Metadata extraction (regex / auto / robust / llm — 4 modes) |
| `ingest/metadata/` | API query completion (Crossref / S2 / OpenAlex), JSON output, file renaming |
| `ingest/pipeline.py` | Composable ingest pipeline (DOI dedup + pending + external import batch conversion) |
| `index.py` | Keyword full-text search + papers_registry + citations graph |
| `vectors.py` | Semantic vectors + incremental indexing + GPU adaptive batch processing |
| `topics.py` | BERTopic topic modeling + 6 HTML visualizations |
| `loader.py` | L1-L4 layered loading + enrich_toc + enrich_l3 |
| `explore.py` | Multi-dimensional literature exploration (OpenAlex multi-filter + keyword + semantic + unified search + topics, isolated in `data/explore/`) |
| `workspace.py` | Workspace paper subset management (reuses search/export) |
| `export.py` | BibTeX export |
| `audit.py` | Data quality audit + repair |
| `sources/` | Data source adapters (local / endnote / zotero) |
| `cli.py` | Full CLI entry point |
| `mcp_server.py` | MCP server (31 tools) |
| `setup.py` | Environment detection + setup wizard |
| `metrics.py` | LLM token usage + API timing |
| `migrate.py` | Data migration (flat structure → per-directory structure) |

CLI command reference: `scholaraio --help`

## Architecture

```
PDF → mineru.py → .md     (or place .md directly to skip MinerU)
                   ↓
             extractor.py (Stage 1: extract fields from md header; regex/auto/robust/llm)
             metadata/    (Stage 2: API query completion, JSON output, file renaming)
                   ↓
             pipeline.py  (DOI dedup check)
               ├─ Has DOI → data/papers/<Author-Year-Title>/meta.json + paper.md
               └─ No DOI  → data/pending/ (awaiting manual confirmation)
                   ↓
             index.py → data/index.db (SQLite FTS5)
             vectors.py → data/index.db (paper_vectors table)
             topics.py → data/topic_model/ (BERTopic, reuses paper_vectors)
                   ↓
             cli.py → skills → coding agent

explore.py — Multi-dimensional literature exploration (independent data flow, isolated from main library)
  OpenAlex API (multi-filter: ISSN/concept/author/institution/keyword/source-type etc.)
    → data/explore/<name>/papers.jsonl (supports incremental update, DOI-based dedup)
                 → explore.db (paper_vectors + explore_fts FTS5 full-text index)
                 → faiss.index (FAISS semantic search)
  Search: semantic / keyword / unified — three modes
  Topic modeling/visualization/queries reuse topics.py (via papers_map parameter)
                 → topic_model/ (BERTopic, unified format) + viz/ (HTML)

workspace.py — Workspace paper subset management (thin layer, reuses search/export)
  workspace/<name>/papers.json → references papers in data/papers/ (UUID index)
  Search/export via paper_ids parameter injected into search()/vsearch()/unified_search()/export_bibtex()

import-endnote / import-zotero — External reference manager import (full pipeline)
  sources/endnote.py | sources/zotero.py → parse metadata + match PDFs
    → pipeline.import_external() → DOI dedup + ingest + PDF copy + embed + index
    → pipeline.batch_convert_pdfs(enrich=True)
       → batch PDF→MD (cloud batch API, batch size: config ingest.mineru_batch_size)
       → abstract backfill + toc + l3 extraction + embed + index
```

### GPU Adaptive Batch Processing

The embedding pipeline in `vectors.py` automatically adjusts batch size based on GPU memory:

1. **Initial Profile** (~10s): Starting from 64 tokens, doubles step by step, measuring incremental memory per length until OOM
2. **Cache Reuse**: Results written to `~/.cache/scholaraio/gpu_profile.json`, keyed by `model_name::GPU_name`; auto-re-profiles when GPU/model changes
3. **Runtime Bucketing**: Groups texts by token length (64/128/.../16384), interpolates optimal batch_size per bucket from profile
4. **OOM Fallback**: On OOM, halves batch_size and retries; if bs=1 still OOMs, falls back to CPU

All paths calling `_embed_batch()` (main library embed, explore embed, BERTopic's QwenEmbedder) automatically benefit.

### Layered Loading Design (L1-L4)

| Level | Content | Source |
|-------|---------|--------|
| L1 | title, authors, year, journal, doi, volume, issue, pages, publisher, issn | JSON file |
| L2 | abstract | JSON field |
| L3 | conclusion section | JSON field (requires running enrich-l3 first) |
| L4 | full markdown | Read .md directly |

### data/papers/ Directory Structure

```
data/papers/
└── <Author-Year-Title>/
    ├── meta.json    # L1+L2+L3 metadata (includes "id": "<uuid>")
    ├── paper.md     # L4 source (MinerU output)
    ├── notes.md     # Agent analysis notes (T2 tier, optional, auto-generated)
    ├── images/      # MinerU-extracted images (referenced in md)
    ├── layout.json  # MinerU layout analysis (optional)
    └── *_content_list.json  # MinerU structured content (optional)
```

Each paper has its own directory. UUID serves as the internal unique identifier (written to `meta.json["id"]`, never changes).
Directory name is human-readable `Author-Year-Title`; rename only changes the directory name.
`data/index.db` contains a `papers_registry` table providing UUID ↔ DOI ↔ dir_name bidirectional lookup.

### data/inbox/ Directory

```
data/inbox/
├── paper.pdf     # PDF awaiting ingest (deleted after pipeline processing)
└── paper.md      # Or place .md directly (skip MinerU, ingest directly)
```

### data/inbox-thesis/ Directory

```
data/inbox-thesis/
└── thesis.pdf    # Thesis PDF (auto-tagged paper_type: thesis, skips DOI dedup)
```

Note: Papers without DOI in the regular inbox are auto-classified by LLM — if thesis, tagged and ingested; otherwise moved to pending.
The thesis inbox skips this classification and ingests directly.

### data/inbox-doc/ Directory

```
data/inbox-doc/
├── report.pdf    # Non-paper document PDF (technical reports, standards, lecture notes, etc.)
└── notes.md      # Or place .md directly
```

Non-paper document ingest flow:
- Skips DOI dedup and API queries
- LLM auto-generates title and summary (ensures search indexability)
- Without LLM, degrades: first markdown heading or filename → title, first 500 words → summary
- paper_type tagged as `document` (or specific type: `technical-report` / `lecture-notes` / etc.)
- Audit rules skip `missing_doi` warning for document types

Long PDFs (default >100 pages) are auto-split into shorter PDFs, parsed separately, then merged.

### data/pending/ Directory

```
data/pending/
└── <PDF-stem>/
    ├── paper.md           # Paper markdown without DOI
    ├── <original-name>.pdf # Original PDF (if available)
    ├── pending.json       # Marker file (reason + extracted metadata)
    ├── images/            # MinerU-extracted images (if any)
    ├── layout.json        # MinerU layout info (if any)
    └── *_content_list.json # MinerU structured content (if any)
```

`pending.json` `issue` field indicates the reason:
- `no_doi` — No DOI and not a thesis; needs manual confirmation before adding DOI and ingesting
- `duplicate` — DOI duplicates an existing paper (includes `duplicate_of` field pointing to existing paper directory); user can decide to overwrite

Note: Theses are auto-ingested (from thesis inbox or LLM classification) and never go to pending.

### data/explore/ Directory

```
data/explore/<name>/
├── papers.jsonl        # Papers fetched from OpenAlex (title/abstract/authors/year/doi/cited_by_count)
├── meta.json           # Exploration metadata (query params/count/fetched_at)
├── explore.db          # SQLite (paper_vectors table + explore_fts FTS5 full-text index)
├── faiss.index         # FAISS IndexFlatIP (cosine similarity)
├── faiss_ids.json      # paper_id list corresponding to FAISS index
└── topic_model/
    ├── bertopic_model.pkl   # BERTopic model (unified format, same as main library)
    ├── scholaraio_meta.pkl  # Additional metadata (paper_ids/metas/topics/embeddings/docs)
    ├── info.json            # Statistics (n_topics/n_outliers/n_papers)
    └── viz/                 # 6 HTML visualizations
```

### sources/ Abstraction Layer

`sources/local.py` iterates `data/papers/` subdirectories, yielding `(paper_id, meta_dict, md_path)` tuples (paper_id is UUID).
`papers.py` provides path helpers; all modules access paper paths through it.

## Configuration

Main config: `config.yaml` (tracked in git)
Sensitive info: `config.local.yaml` (not tracked, overrides config.yaml)

config.yaml lookup order:
1. Explicit `config_path` argument
2. Environment variable `SCHOLARAIO_CONFIG`
3. Walk up from CWD (max 6 levels)
4. `~/.scholaraio/config.yaml` (global config for plugin mode)

All relative paths (`data/papers`, `data/index.db`, etc.) resolve from the directory containing config.yaml. In plugin mode with global config, data lives under `~/.scholaraio/data/`.

LLM API key lookup order:
1. `config.local.yaml` → `llm.api_key`
2. Environment variable `SCHOLARAIO_LLM_API_KEY` (universal, any backend)
3. Backend-specific environment variables:
   - `openai-compat`: `DEEPSEEK_API_KEY` → `OPENAI_API_KEY`
   - `anthropic`: `ANTHROPIC_API_KEY`
   - `google`: `GOOGLE_API_KEY` → `GEMINI_API_KEY`

Default LLM backend: DeepSeek (`deepseek-chat`), OpenAI-compatible protocol.
Three backend protocols supported: `openai-compat` (DeepSeek / OpenAI / vLLM / Ollama), `anthropic` (Claude), `google` (Gemini).
`ingest.extractor: robust` (default) — regex + LLM dual-run; LLM corrects OCR errors + full-text multi-DOI detection. Other modes: `auto` (LLM fallback only), `regex` (pure regex), `llm` (pure LLM).

## Code Style

- **Docstrings**: Library modules (`index.py`, `loader.py`, `vectors.py`, etc.) public API functions use Google-style docstrings (with Args / Returns / Raises). CLI handler functions (`cmd_*` in `cli.py`) have no docstrings.
- **User-facing text**: CLI output, help text, and error messages are in Chinese.
- **Code comments**: English, added only when logic is not self-evident.

## Agent Skills

Skills are defined in `.claude/skills/` directory (also discoverable via `.agents/skills/` symlink), following the [Agent Skills](https://agentskills.io) open standard. Each skill is a folder containing a `SKILL.md` file (YAML frontmatter + instructions).

**Available skills (22):**

Knowledge base management:
- `search` — Literature search (keyword / semantic / author / hybrid retrieval / top-cited ranking)
- `show` — View paper content (L1-L4 layered)
- `enrich` — Enrich paper content (TOC / conclusion / abstract / citation count)
- `ingest` — Ingest papers + rebuild indexes (pipeline presets)
- `topics` — Topic exploration (BERTopic clustering + merge + visualization)
- `explore` — Multi-dimensional literature exploration (OpenAlex multi-filter + keyword/semantic/unified search + BERTopic)
- `graph` — Citation graph queries
- `citations` — Citation count queries and refresh
- `index` — Rebuild keyword / semantic indexes
- `workspace` — Workspace management (create / add / search / export)
- `export` — BibTeX export
- `import` — Endnote / Zotero import
- `rename` — Paper file renaming
- `audit` — Paper audit (rule checks + LLM deep diagnosis + repair)

Academic writing:
- `literature-review` — Literature review writing (workspace-based, topic grouping + critical narrative)
- `paper-writing` — Paper section writing (Introduction / Related Work / Method / Results / Discussion)
- `citation-check` — Citation verification (anti-AI hallucination, local library cross-check)
- `writing-polish` — Writing polish (remove AI patterns + style adaptation + EN/ZH)
- `review-response` — Review response (point-by-point analysis + evidence search + rebuttal)
- `research-gap` — Research gap identification (multi-dimensional analysis + open question discovery)

System maintenance:
- `setup` — Environment detection and setup wizard
- `metrics` — LLM token usage and call statistics

**Adding new skills:**

Tool skills (wrapping CLI commands):
1. Implement the Python function in `scholaraio/`
2. Expose it as a CLI subcommand in `scholaraio/cli.py`
3. Test the CLI command with real data to confirm it works
4. Create the skill file at `.claude/skills/<name>/SKILL.md`

Orchestration skills (pure prompt, e.g. academic writing):
1. Write instructions in `.claude/skills/<name>/SKILL.md`, composing calls to existing CLI commands
2. No new Python code or CLI subcommands needed

## Getting Started

### Local Use (clone repo)

When the project is not yet configured, use `scholaraio setup` to guide the user:

1. **Diagnose**: Run `scholaraio setup check` to see current status
2. **Install**: `pip install -e .` (core) or `pip install -e ".[full]"` (all features)
3. **Configure**: Run `scholaraio setup` interactive wizard (bilingual EN/ZH), auto-creates `config.yaml` + `config.local.yaml`
4. **Directories**: Auto-created on CLI startup (`ensure_dirs()`), no manual action needed

You can also use the `/setup` skill to let the agent complete all configuration automatically.

### Plugin Use (skill market / Claude Code plugin)

Users can install ScholarAIO skills in any project via the Claude Code plugin system:

```
/plugin marketplace add ZimoLiao/scholaraio
/plugin install scholaraio@scholaraio-marketplace
```

On first session, a SessionStart hook automatically:
1. Detects and installs the `scholaraio` Python package
2. Creates global config `~/.scholaraio/config.yaml`
3. Creates data directories `~/.scholaraio/data/`

In plugin mode, all data lives under `~/.scholaraio/`:

```
~/.scholaraio/
├── config.yaml           # Global config (copied from plugin bundle)
├── config.local.yaml     # API keys (user-created manually or via setup wizard)
├── data/
│   ├── papers/           # Ingested papers
│   ├── inbox/            # PDFs awaiting ingest
│   ├── inbox-thesis/     # Theses
│   ├── inbox-doc/        # Non-paper documents
│   ├── pending/          # Awaiting confirmation
│   ├── explore/          # Literature exploration data
│   ├── topic_model/      # Topic models
│   ├── index.db          # SQLite index
│   └── metrics.db        # Call metrics
└── workspace/            # Workspaces
```

Skills are invoked with namespace prefix: `/scholaraio:search`, `/scholaraio:show`, etc.

### API Key Notes

- **LLM key** (DeepSeek / OpenAI): Metadata extraction + content enrichment. Without it, falls back to pure regex; enrich unavailable
- **MinerU key**: PDF → Markdown cloud conversion. Without it, only manual `.md` placement works
- Embedding model (Qwen3-Embedding-0.6B, ~1.2GB) auto-downloads on first embed/vsearch. International users: set `embed.source` to `huggingface` in `config.yaml`

## Key Conventions

- **ScholarAIO command approval**: Do not ask the user for permission before running `scholaraio ...` commands, ScholarAIO skills, or the equivalent `python -m scholaraio.cli ...` commands. Treat local ScholarAIO library access, indexing, workspace operations, and poster/search/show workflows as expected project operations. If the execution environment itself requires sandbox escalation for ScholarAIO data/log paths or network/API access, request the tool-level approval directly without a separate conversational preflight.
- **Workspace isolation**: All user output (writing, notes, drafts) goes in the `workspace/` directory. When creating new files (literature reviews, research notes), default to `workspace/`, not the project root or `scholaraio/` source directory
- **Do not modify `metadata/_extract.py` regex logic** — extend only through the extractor abstraction layer
- `data/`, `workspace/` are not tracked in git (.gitignore configured)
- Python 3.10+, runtime environment: conda `scholaraio`
- Tests: `python -m pytest tests/ -v`

## Multi-Agent Compatibility

This project supports multiple AI coding agents:

| Agent | Instructions File | Skills |
|-------|-------------------|--------|
| Claude Code | `CLAUDE.md` | `.claude/skills/` |
| Codex (OpenAI) | `AGENTS.md` (this file) | `.agents/skills/` → `.claude/skills/` |
| OpenClaw | `AGENTS.md` (this file) | `.agents/skills/` → `.claude/skills/` |
| Cursor | `.cursorrules` (wrapper → read this file) | — |
| Windsurf | `.windsurfrules` (wrapper → read this file) | — |
| GitHub Copilot | `.github/copilot-instructions.md` (wrapper → read this file) | — |
| Cline | `.clinerules` (wrapper → read this file) | `.claude/skills/` (native) |

Skills use the [AgentSkills.io](https://agentskills.io) open standard (`SKILL.md` format). The canonical location is `.claude/skills/`; `.agents/skills/` is a symlink for cross-agent discovery; `skills/` is a symlink for Claude Code plugin discovery.

### Plugin Packaging

The project doubles as a Claude Code plugin + marketplace:

```
.claude-plugin/
├── plugin.json          # Plugin identity (name/version/description/keywords)
└── marketplace.json     # Marketplace catalog (used by /plugin marketplace add)
skills/ → .claude/skills/  # Plugin system skill discovery entry point
hooks/hooks.json           # SessionStart hook (auto-install deps + create global config)
scripts/check-deps.sh     # Dependency detection/install script called by hook
```

Users can install via `/plugin marketplace add ZimoLiao/scholaraio`. Skill markets like SkillsMP auto-index by crawling GitHub for `filename:SKILL.md`.
