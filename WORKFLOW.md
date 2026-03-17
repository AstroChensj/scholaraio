# ScholarAIO Basic Workflow

This file is a practical guide to using ScholarAIO from the starting point of a research idea.

For each step, there are two ways to work:

- Agent mode: talk to Codex naturally in this repo
- CLI mode: run `scholaraio ...` commands directly


## 1. Start From an Idea

Example idea:

`multiscale outflow of AGN: from X-ray UFO, to UV BAL, to molecular outflow`

At this point, you usually choose one of two paths:

- Path A: discover related papers from the web first
- Path B: you already have papers/PDFs and want to ingest them directly

### Agent mode

Ask Codex:

- `Find related literature for AGN multiscale outflow and help me plan a search strategy.`
- `I want papers connecting X-ray UFO, UV BAL, and molecular outflow. What should I search first?`

### CLI mode

No mandatory command yet. This is the planning stage.


## 2. Discover Papers From the Web

ScholarAIO's `explore` workflow fetches metadata/abstracts from OpenAlex, then builds local embeddings on the fetched set.

Important:

- `explore` does not download full PDFs or `paper.md`
- it is for discovery and scouting

### Agent mode

Ask Codex:

- `Use ScholarAIO explore to fetch papers related to AGN multiscale outflow.`
- `Build me an external scouting set for soft X-ray excess in AGN, then search it for warm corona vs reflection papers.`

### CLI mode

Fetch an explore set:

```bash
scholaraio explore fetch \
  --keyword "AGN outflow UFO BAL molecular outflow" \
  --year-range 2015-2026 \
  --source-type journal \
  --name agn-outflow
```

Build embeddings for semantic search:

```bash
scholaraio explore embed --name agn-outflow
```
This step may be time-consuming, depending on the number of raw entries from `explore fetch`.

Search that external set:

```bash
scholaraio explore search --name agn-outflow "connection between X-ray UFO UV BAL and molecular outflow" --mode unified
```

Optional topic clustering:

```bash
scholaraio explore topics --name agn-outflow --build
```


## 3. Pick Papers To Read Locally

After `explore`, choose the papers you actually want in your local library.

This step is manual in practice:

- inspect the most relevant external results
- download PDFs yourself
- place them into ScholarAIO inboxes

You can also skip `explore` entirely if you already have PDFs.

### Agent mode

Ask Codex:

- `From the explore results, tell me which 10 papers are the best candidates to download and ingest.`
- `I already have a few AGN soft-excess PDFs. Help me organize which ones should go into the main library first.`

### CLI mode

Put files into these inboxes:

- normal papers: `data/inbox/`
- theses: `data/inbox-thesis/`
- non-paper docs: `data/inbox-doc/`


## 4. Ingest Local Papers

This is where ScholarAIO builds your real local knowledge base.

For PDFs:

- MinerU converts PDF -> Markdown
- ScholarAIO extracts metadata
- DOI dedup runs
- accepted papers land in `data/papers/`

For Markdown:

- place `.md` directly into inbox
- MinerU is skipped

### Agent mode

Ask Codex:

- `I put several new PDFs into data/inbox. Ingest them and tell me if any metadata looks wrong.`
- `Run the full ingestion workflow on my local papers and check for duplicates or pending items.`

### CLI mode

Ingest:

```bash
scholaraio pipeline ingest
```

Or full pipeline:

```bash
scholaraio pipeline full
```

What you get after successful ingest:

- `data/papers/<Author-Year-Title>/meta.json`
- `data/papers/<Author-Year-Title>/paper.md`
- optional MinerU assets like `images/`


## 5. Build Search Indexes

There are two different index layers:

- `index`: keyword/full-text search
- `embed`: semantic vector search

Usually:

- `pipeline reindex` updates both
- `search` does not need embeddings
- `vsearch` and `usearch` do need embeddings

### Agent mode

Ask Codex:

- `Rebuild the indexes for my local library and verify that semantic search works.`
- `I changed some metadata. Reindex the library and confirm search is healthy.`

### CLI mode

Rebuild both:

```bash
scholaraio pipeline reindex
```

Only keyword index:

```bash
scholaraio index --rebuild
```

Only embeddings:

```bash
scholaraio embed --rebuild
```


## 6. Search the Local Library

Once papers are in `data/papers/`, ScholarAIO becomes your local literature index.

Three main search styles:

- `search`: keyword search
- `vsearch`: semantic search
- `usearch`: hybrid search

### Agent mode

Ask Codex:

- `Find my local papers related to warm corona and soft excess, and rank the most relevant ones.`
- `Search my library for AGN outflow coupling across X-ray, UV, and molecular scales.`

### CLI mode

Keyword:

```bash
scholaraio search "soft excess warm corona"
```

Semantic:

```bash
scholaraio vsearch "relation between soft X-ray and UV in AGN"
```

Hybrid:

```bash
scholaraio usearch "AGN multiscale outflow feedback"
```


## 7. Read Papers in Layers

ScholarAIO uses layered reading:

- L1: metadata
- L2: abstract
- L3: conclusion
- L4: full `paper.md`

### Agent mode

Ask Codex:

- `Compare Chen 2025 and Jiang 2025 on the physical origin of the soft excess.`
- `Read the most relevant local papers and tell me what the current picture is.`

### CLI mode

Show metadata:

```bash
scholaraio show PAPER_ID
```

Show full text:

```bash
scholaraio show PAPER_ID --level 4
```

Optional enrichment for structured conclusion:

```bash
scholaraio enrich-l3 PAPER_ID --force --inspect
```

In practice, for small focused tasks, asking Codex directly is often more efficient than precomputing `L3`.


## 8. Explore Citation Structure

ScholarAIO also provides citation-graph style queries over your local library.

### Agent mode

Ask Codex:

- `Check whether these two papers rely on the same citation background.`
- `Trace which local papers cite Chen 2025 and what they add.`

### CLI mode

Backward references:

```bash
scholaraio refs PAPER_ID
```

Forward citations inside the local library:

```bash
scholaraio citing PAPER_ID
```

Shared references:

```bash
scholaraio shared-refs PAPER_A PAPER_B
```


## 9. Organize a Project Workspace

Use workspaces to create a focused subset for one research question or one paper draft.

### Agent mode

Ask Codex:

- `Create a workspace for AGN soft excess and add the most relevant local papers.`
- `Build me a workspace for AGN multiscale outflow papers.`

### CLI mode

Create a workspace:

```bash
scholaraio ws init soft-excess
```

Add papers:

```bash
scholaraio ws add soft-excess PAPER_ID
```

Search inside the workspace:

```bash
scholaraio ws search soft-excess "warm corona"
```


## 10. Ask Research Questions

At this stage, the most powerful mode is usually agent mode: let Codex use ScholarAIO as infrastructure and synthesize the answer for you.

Typical tasks:

- summarize the current understanding of a topic
- compare competing models
- identify contradictions across papers
- propose which papers to read next
- draft literature-review paragraphs

### Agent mode

Ask Codex:

- `Based only on my local papers, what is our current understanding of AGN soft excess?`
- `Compare the evidence for warm corona vs ionized reflection in my library.`
- `Write a short literature review from the papers in my workspace.`

### CLI mode

CLI alone is less suitable for full synthesis. It is better for retrieval and inspection:

```bash
scholaraio search "soft excess"
scholaraio show PAPER_ID --level 2
scholaraio show PAPER_ID --level 4
```

Then ask Codex to synthesize the result.


## 11. Export and Write

Once you have a good local subset, you can export citations or ask the agent to help write.

### Agent mode

Ask Codex:

- `Draft a related-work section using the papers in my workspace.`
- `Export the references for my AGN soft-excess workspace.`

### CLI mode

Export BibTeX:

```bash
scholaraio export bibtex --workspace soft-excess
```


## Recommended Minimal Workflow

If you want the simplest useful pattern:

1. Start from an idea
2. Optionally use `explore fetch -> explore embed -> explore search`
3. Download a small number of strong candidate PDFs
4. Put them in `data/inbox/`
5. Run `scholaraio pipeline ingest`
6. Run `scholaraio pipeline reindex`
7. Use `search` / `vsearch` / `usearch`
8. Ask Codex research questions based on your local papers


## Practical Division of Labor

Use ScholarAIO directly when you need:

- ingestion
- indexing
- search
- citation graph
- workspace organization
- external scouting with `explore`

Use Codex as your research partner when you need:

- comparison across papers
- critical reading
- synthesis
- discussion of contradictions
- literature review writing
- research-gap analysis

The most effective workflow is usually:

- ScholarAIO for retrieval and structure
- Codex for reasoning and writing
