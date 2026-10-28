# qrag — Architecture & Design Decisions

This file documents the key architectural decisions in qrag with Mermaid diagrams.
Update it whenever a design decision is made or changed. Use Mermaid syntax for all diagrams.

---

## System Overview

```mermaid
flowchart TD
    subgraph PREP["PREPARATION — runs once per team"]
        SRC["Source files\nC / C++ / Rust / Python / Go / …"]
        BUILD["Build files\nCMake / Cargo / Makefile / …"]
        DOCS["Docs\n.pdf / .html"]
        SRC & BUILD & DOCS --> CHUNKER["chunker.py\ntree-sitter-language-pack\n305+ grammars"]
        CHUNKER --> EMBEDDER["embedder.py\nall-MiniLM-L6-v2\n384-dim, local"]
        EMBEDDER --> DBPY["database.py\nSQLite + sqlite-vec"]
        DBPY --> CODEDB[("code.db\ncode_chunks\nsymbols\nvec_code")]
        DBPY --> DOCSDB[("docs.db\ndoc_sections\nvec_docs")]
        CODEDB & DOCSDB --> GHDIST["github_distribution.py\nGitHub Releases"]
    end

    GHDIST -->|"qrag hub push"| ASSET[/"GitHub Release Asset"/]
    ASSET  -->|"qrag hub download"| TEAM

    subgraph TEAM["TEAM USAGE — every developer"]
        CFG["~/.qrag/config.json\nactive_versions: v1, v2, …"]
        V1[("~/.qrag/v1/\ncode.db  docs.db")]
        V2[("~/.qrag/v2/\ncode.db  docs.db")]
        CFG --> V1 & V2
        AI["AI Agent\nClaude / Gemini CLI"] <-->|"MCP JSON-RPC"| MCP["mcp_server.py"]
        MCP --> V1 & V2
    end
```

---

## AD-1: Multi-DB Fan-Out Search (IS3)

**Decision:** Users can activate multiple independently-prepared databases at once.
All four MCP tools fan-out across every active DB in parallel, merge results by
score, and deduplicate before returning to the AI agent.

**Why:** Teams work with multiple SDKs, RTOSes, and doc sets simultaneously.
Requiring a merged re-build for each new source is prohibitive. Pre-built DBs
downloaded independently must be queryable together without rebuilding.

### Config Migration

```mermaid
flowchart LR
    OLD["config.json\nactive_version: 'v1'\n(string — legacy)"]
    -->|"auto-migrated on first load\nold key never written back"| NEW["config.json\nactive_versions: ['v1']\n(list — current)"]
```

### Fan-Out Flow

```mermaid
flowchart TD
    AGENT["AI Agent"]
    AGENT -->|"tools/call search_code { query }"| MCP["mcp_server.py"]
    MCP --> EMB["embed_one(query)\n→ 384-dim vector"]
    EMB --> POOL["ThreadPoolExecutor\nmax_workers = min(N, 8)"]
    POOL --> DB1[("code.db\nsdk-v1")]
    POOL --> DB2[("code.db\nrtos-v2")]
    POOL --> DBN[("code.db\ntrm-v3")]
    DB1 -->|"top-10"| MERGE
    DB2 -->|"top-10"| MERGE
    DBN -->|"top-10"| MERGE
    MERGE["Sort by similarity_score DESC\nDeduplicate by file_path + line_start\nReturn top-10"]
    MERGE --> AGENT
```

### Per-DB Search (inside database.py)

```mermaid
flowchart TD
    QV["query vector\n384-dim float"]
    QV --> VEC["sqlite-vec\nvec_cosine_distance(vec_code, query_vec)"]
    VEC --> SORT["ORDER BY distance ASC  LIMIT top_k"]
    SORT --> JOIN["JOIN code_chunks ON rowid"]
    JOIN --> OUT["Result row\n{ symbol_name, file_path, line_start, line_end,\n  code_snippet, type, language, similarity_score }"]
```

### Complexity

| Scenario | Wall-clock cost |
|----------|----------------|
| 1 DB | 1× single-DB search latency |
| N DBs (N ≤ 8) | ≈ 1× single-DB search latency (fully parallel) |
| N DBs (N > 8) | ≈ ⌈N/8⌉ × single-DB search latency |

sqlite-vec cosine search is **O(rows)** per DB. At ~4 MB per DB (~10k chunks),
a single search completes in <50 ms on CPU. 100 DBs with 8 workers = ~13 rounds
≈ 650 ms worst case — acceptable for an AI-agent tool call.

### CLI Usage

```
# Set one active version
qrag ai active sdk-v1

# Set multiple active versions (replaces the list)
qrag ai active sdk-v1 rtos-v2 trm-v3

# Show current active versions
qrag ai active

# build and hub download auto-add to the list
qrag build -i /path/to/code -o sdk-v1      # → active_versions gains "sdk-v1"
qrag hub download rtos-v2                    # → active_versions gains "rtos-v2"
```

---

## AD-2: Dependency Split — Consumer vs Builder (GH#13)

**Decision:** Builder dependencies (parsing, grammar, doc parsing) are optional
extras. The consumer install only needs `click + sqlite-vec + sentence-transformers`.

**Why:** `sentence-transformers` pulls in `torch` and GPU deps. `tree-sitter-language-pack`
is large. Teams that only download and query pre-built DBs shouldn't pay that cost.

```mermaid
flowchart LR
    subgraph BASE["Base install\nuv tool install qrag"]
        CL["click >= 8.1"]
        SV["sqlite-vec >= 0.1.6"]
        ST["sentence-transformers >= 3.0"]
    end

    subgraph BUILD["[build] extra"]
        TS["tree-sitter >= 0.22"]
        TSP["tree-sitter-language-pack >= 0.7.2"]
        MU["pymupdf >= 1.24"]
        H2["html2text >= 2024.2"]
        BS["beautifulsoup4 >= 4.12"]
    end

    subgraph GPU["[build-gpu] extra"]
        TO["torch >= 2.0"]
    end

    BASE --> CONSUMER["Consumer\ndownload + MCP search"]
    BASE & BUILD --> BUILDER["Builder\nqrag build"]
    BASE & BUILD & GPU --> FULL["Full\nqrag[full]"]
```

**Guard in `build()`:** `_ensure_build_deps()` probes `tree_sitter`, `fitz`,
`tree_sitter_language_pack` → prints reinstall instructions and exits 1 if missing.

---

## AD-3: Embedding Model — Bundled Local (all-MiniLM-L6-v2)

**Decision:** `all-MiniLM-L6-v2` (384-dim) is bundled inside the wheel under
`src/qrag/models/`. No HuggingFace call at runtime.

**Why:** Air-gapped embedded-systems environments cannot reach HuggingFace.
Startup time must be deterministic. The model is small (≈22 MB).

**Trade-off:** Wheel is larger. Model version is pinned and must be explicitly
updated. A future model upgrade requires a new wheel release.

```mermaid
flowchart LR
    WHEEL["qrag wheel\nsrc/qrag/models/all-MiniLM-L6-v2/"]
    -->|"_get_model() loads at first embed call"| MODEL["in-process model\nno network call"]
    MODEL --> EMB["384-dim embedding vector"]
```

---

## AD-4: SQLite + sqlite-vec (no external vector DB)

**Decision:** All storage is a single SQLite file with sqlite-vec for vector
search. No Chroma, Pinecone, or other external service.

**Why:** Pre-built DBs are distributed as GitHub Release assets and downloaded
by the team. A single-file format requires zero server infra, works offline,
and is trivially versioned via GitHub Releases.

**Trade-off:** sqlite-vec cosine search is O(n) — no ANN index. At current
scale (~10k chunks per DB) this is fast enough. If a single DB exceeds ~1M
chunks, an ANN index (e.g. FAISS) would be needed.

```mermaid
flowchart LR
    subgraph DB["Single SQLite file  (~4 MB per DB)"]
        CC["code_chunks\n(symbol_name, file_path, language, …)"]
        SYM["symbols\n(name, type, language, …)"]
        VC["vec_code\n(rowid → 384-dim blob)"]
        DS["doc_sections\n(title, content, page_range, …)"]
        VD["vec_docs\n(rowid → 384-dim blob)"]
    end

    Q["query vector"] -->|"vec_cosine_distance"| VC & VD
    VC -->|"JOIN"| CC
    VD -->|"JOIN"| DS
```

---

## AD-5: Multi-Language Parsing — Registry-Driven (C0)

**Decision:** `chunker.py` uses `tree-sitter-language-pack` (305+ grammars)
with a registry-driven rule engine. `chunk_type` is a free-form string, not an enum.

**Why:** Individual `tree-sitter-c/cpp` packages don't scale to 30+ languages.
Free-form `chunk_type` means new language support never requires a DB schema
migration — just a new registry entry.

```mermaid
flowchart TD
    FILE["Input file\n(path + extension / filename)"]
    FILE --> REG{"_EXT_REGISTRY\n_FILENAME_REGISTRY"}
    REG -->|"match"| LANG["_LangConfig\n{ ts_name, rules: [_Rule] }"]
    REG -->|"no match"| SKIP["skip file"]
    LANG --> PARSER["tree-sitter-language-pack\nget_parser(ts_name)"]
    PARSER --> AST["Parse tree\nAST node traversal"]
    AST --> RULE["_Rule\n{ node_types → chunk_type\n  extract_name callable }"]
    RULE --> CHUNK["CodeChunk\n{ symbol_name, chunk_type, language\n  file_path, file_name, line_start, line_end\n  code_text, parent_name, call_depth, chunk_index=0 }"]
    CHUNK -->|"large chunk"| SPLIT["auto-split with\noverlap (512 tok max)\nchunk_index=0,1,2,…"]
    CHUNK & SPLIT --> OUT["insert into code_chunks + symbols"]
```

---

## AD-6: Rich Code Metadata — IS5

**Decision:** `code_chunks` gains four new columns: `file_name` (basename), `parent_name`
(enclosing block), `call_depth` (nesting level), `chunk_index` (sub-chunk index within
a split symbol). Existing DBs are auto-migrated via `ALTER TABLE` on open.

**Why:** The AI agent needs precise citation (file + line range), structural context
(what class/namespace owns this function), and sub-chunk tracking (which slice of a
large function it is looking at) to answer questions accurately.

```mermaid
flowchart TD
    ROOT["AST root\ndepth=0, parent=''"]
    ROOT -->|"rule.recurse=True\nname='MyClass'"| CLASS["CodeChunk\nsymbol_name='MyClass'\ncall_depth=0\nparent_name=''"]
    CLASS -->|"recurse children\ndepth=1, parent='MyClass'"| METHOD["CodeChunk\nsymbol_name='my_method'\ncall_depth=1\nparent_name='MyClass'"]
    ROOT -->|"rule.recurse=False\nname='free_fn'"| FREE["CodeChunk\nsymbol_name='free_fn'\ncall_depth=0\nparent_name=''"]
    FREE -->|"> 512 tokens"| SPLIT["sub-chunks\nfree_fn#0, free_fn#1, …\nchunk_index=0,1,…"]
```

**Schema migration:** `_open_code()` runs `ALTER TABLE code_chunks ADD COLUMN …` for
each new column on every open; `sqlite3.OperationalError` is swallowed when the column
already exists. `init_code_db` includes all columns in `CREATE TABLE IF NOT EXISTS`.

---

## AD-7: Rich Doc Metadata — IS4

**Decision:** `doc_sections` gains five new columns: `doc_name`, `doc_revision`,
`doc_status`, `word_count`, `fig_table_refs`. Extracted at parse time from the filename
and section content. Existing DBs are auto-migrated on open via `_open_docs()`.

**Why:** LLMs need to cite documents precisely (which document, which revision, which
status). Figure/table references let the agent locate companion material. Word count
helps the agent judge whether content has been truncated.

```mermaid
flowchart LR
    FILE["PDF / HTML file\n'STM32F4_RM_rev3_draft.pdf'"]
    FILE -->|"path.stem"| META["doc_name = 'STM32F4_RM_rev3_draft.pdf'\ndoc_revision = '3'  (_REV_RE)\ndoc_status = 'draft'  (_STATUS_KEYWORDS)"]
    FILE -->|"parse content"| CONTENT["section content text"]
    CONTENT -->|"_extract_fig_table_refs"| REFS["fig_table_refs = 'Figure 3-1,Table 4-2'"]
    CONTENT -->|"len(split)"| WC["word_count = N"]
    META & REFS & WC --> DB[("doc_sections row")]
```

**Revision extraction:** `_REV_RE = r'[_\\-\\s](?:rev?|ver?|version)\\.?\\s*(\\d+[.\\d]*)'`
matches `_rev3`, `_v2`, `_Rev1.2` etc. from the filename stem.

**Status extraction:** first match of `released | approved | final | review | draft | obsolete`
in the lowercased filename stem. Empty string if none matched.

---

## AD-8: Rich TUI Progress + Build Report (IS1, IS2)

**Decision:** `qrag build` displays three transient Rich progress bars
(Overall / Parse / Embed) that collapse on completion, leaving a single
uv-style summary line. A plain-text `build-report.txt` is always written
to `~/.qrag/<version>/` with per-file stats, language breakdown, and
skipped-file inventory.

**Why (IS1):** Plain `click.echo` output gave no feedback during long
builds — the tool appeared frozen on large codebases. The uv/indicatif
pattern (transient bars → single summary line) is familiar to the target
audience.

**Why (IS2):** Zero visibility into what entered the database. Users
couldn't verify which files were parsed, which languages were detected,
or which files were silently dropped due to parse errors or zero chunks.

**Trade-off:** `rich>=13.0` is added only to the `[build]` optional-
dependencies, keeping the consumer install lean. Rich is disabled when
`--verbose` is active (JSON logs and Rich escape codes conflict on the
same stderr fd).

### Build Pipeline with Progress Instrumentation

```mermaid
flowchart TD
    subgraph WORKER["ProcessPoolExecutor workers (parallel)"]
        TCC["_timed_chunk_code_file(path)\nchunk_code_file() + monotonic timer"]
        TPD["_timed_parse_doc_file(path)\nparse_doc_file() + monotonic timer"]
    end

    TCC -->|"(chunks, elapsed)"| Q["shared queue\n(_KIND_CODE, items, elapsed, root, rel)"]
    TPD -->|"(sections, elapsed)"| Q

    subgraph CONSUMER["Main thread — _consume_and_embed()"]
        Q --> RECV["receive item from queue"]
        RECV --> REC["append _FileRecord\n(path, kind, language, chunks, elapsed, skipped)"]
        REC --> UPPARSE["_upd_overall()\nprogress.update(parse_task, advance=1)"]
        UPPARSE --> CHECK{"pending ≥ 1000?"}
        CHECK -->|yes| FLUSH["_flush_code/doc_batch()\n→ embed → insert"]
        FLUSH --> UPEMB["_upd_embed(batch_len, elapsed_batch)\nrolling avg throughput\nadaptive total estimate"]
        CHECK -->|no| RECV
    end

    CONSUMER -->|"file_records list"| REPORT["_write_build_report()\nbuild-report.txt"]
```

### Three-Bar Layout (transient=True, collapses on completion)

```mermaid
flowchart LR
    subgraph PROGRESS["Rich Progress — during build"]
        OV["Overall  [████████░░░░]  •  2m 15s  •  ETA 2m 32s\ntotal = total_files × 2\ncompleted = files_parsed + est_files_embedded"]
        PA["  Parse  [████████████░░]  87/312 files  •  45s  •  ETA 1m 12s\ntotal = total_files (known)\nadaptive ETA from per-file worker times"]
        EM["  Embed  [████░░░░░░░░░░]  4,203 chunks • 312/s  •  ETA 4m 51s\ntotal = None → re-estimated as avg_chunks/file × total_files\nrolling throughput over last 20 batches"]
    end

    PROGRESS -->|"transient=True\nbars cleared on exit"| SUMMARY["✓ 12,847 code chunks + 3,201 doc sections in 4m 32s\n  Build report: ~/.qrag/my-sdk/build-report.txt"]
```

### Adaptive ETA Model

| Bar | Known upfront | Adaptive signal | ETA formula |
|-----|--------------|-----------------|-------------|
| Parse | `total_files` | per-file elapsed from worker | Rich built-in (speed = completed / elapsed) |
| Embed | `None` → updated | avg chunks/file from completed files | `(est_total - chunks_embedded) / rolling_throughput` |
| Overall | `total_files × 2` | parse fraction + embed fraction | combined advancement of both bars |

### Build Report Structure (`build-report.txt`)

```
qrag build report
Generated : 2026-06-26 14:32:01
Output    : my-sdk

======================================================================
SUMMARY
======================================================================
Total wall-clock time  : 4m 32s
Code files processed   : 287 (2 skipped)
Doc files processed    : 23 (1 skipped)
Code chunks stored     : 12,847
Doc sections stored    : 3,201
code.db size           : 48.2 MB
docs.db size           : 12.7 MB

======================================================================
BY LANGUAGE
======================================================================
Language              Files   Chunks   Avg/file       Time
...

======================================================================
SKIPPED FILES
======================================================================
Reason          Path
zero_chunks     src/empty.h
parse_error     docs/corrupted.pdf
```

---

## AD-9: Rich TUI MVC Architecture — GH#26

**Decision:** All Rich rendering logic is extracted into `src/qrag/tui.py`. `cli.py`
owns data processing only; it fires event callbacks (`on_file_parsed`, `on_error`,
`on_embed_batch`) into a `BuildLayout` instance that manages the full terminal layout.

**Why:** AD-8's TUI lived entirely in `cli.py` as nested closures and inline Rich
imports. Adding a scrolling log panel, a status line, a worker header, and fallback
logic would have made `cli.py` unreadable. MVC separation keeps rendering isolated,
independently testable, and easy to extend.

**Trade-off:** One extra module (`tui.py`). Rich imports are still lazy (`[build]`
extras only); `tui.py` is only imported inside `build()` after `_ensure_build_deps()`
confirms Rich is present.

### Layout Hierarchy (during `qrag build`)

```mermaid
flowchart TD
    LIVE["Live(transient=True, refresh_per_second=2)"]
    LIVE --> RENDER["BuildLayout._render()"]
    RENDER --> HDR["Text: ⚙ N code workers · M doc workers (C cores total)"]
    RENDER --> RULE1["Rule (dim)"]
    RENDER --> PROG["Progress (3 bars)"]
    PROG --> OV["Overall   [bar]  count  —  ETA"]
    PROG --> PA["  Parse   [bar]  N/M files  X files/s  ETA"]
    PROG --> EM["  Embed   [bar]  N chunks  X chunks/s  ETA"]
    RENDER --> RULE2["Rule (dim)"]
    RENDER --> PANEL["Panel: Processing (scrolling log, N lines)"]
    PANEL --> LOGLINES["✓ path  N chunks  Xs\n⚠ path  0 chunks  Xs\n✗ path  error msg"]
    RENDER --> FOOTER["Text: Build report → ~/.qrag/<v>/build-report.txt (dim)"]
    RENDER --> RULE3["Rule (dim)"]
    RENDER --> STATUS["Text: N errors • M warnings • X chunks/s • A+B workers"]
```

### Fallback Mode (terminal too small)

```mermaid
flowchart LR
    SIZE{"console.size\n< MIN_HEIGHT or\n< MIN_WIDTH"}
    SIZE -->|"yes: h<12 or w<60"| FALLBACK["Text: Terminal too small\n+ Progress (3 bars only)"]
    SIZE -->|"no"| FULL["Full layout\n(header + bars + panel + status)"]
```

### Proportional CPU Split (GH#27, applied here)

```mermaid
flowchart TD
    TOTAL["total_workers = limit_cpu or cpu_count()"]
    TOTAL --> BOTH{"code files\nAND doc files?"}
    BOTH -->|"yes"| SPLIT["code_workers = max(1, round(total × code_ratio))\ndoc_workers  = max(1, total − code_workers)"]
    BOTH -->|"code only"| CONLY["code_workers = total\ndoc_workers  = 0"]
    BOTH -->|"docs only"| DONLY["code_workers = 0\ndoc_workers  = total"]
    SPLIT & CONLY & DONLY --> PROCS["ProcessPoolExecutor(max_workers=code_workers)\nProcessPoolExecutor(max_workers=doc_workers)"]
```

### Real-Time Error Queue

```mermaid
flowchart LR
    WORKER["Worker process\nexception raised"]
    WORKER -->|"errors.append(...)"| ERRLIST["producer_errors list\n(post-build exit check)"]
    WORKER -->|"q.put((_KIND_ERROR, msg, 0.0, root, rel))"| QUEUE["shared queue"]
    QUEUE --> CONSUMER["_consume_and_embed()\nkind == _KIND_ERROR\n→ layout.on_error(...)"]
    CONSUMER --> PANEL["Log panel: ✗ path  error msg (red)\nStatus line: +1 error"]
```

### Custom Progress Columns (fixed-width grid)

| Column | Class | Min width | Content |
|--------|-------|-----------|---------|
| Count | `_FieldColumn("count")` | 15 | `"N/M files"` / `"N chunks"` / `""` |
| Rate | `_FieldColumn("rate")` | 16 | `"X files/s"` / `"X chunks/s"` / `"—"` |
| ETA | `_EtaColumn` | 8 | `fmt_eta(task.time_remaining)` |

`fmt_eta(seconds)` → `"Xs"` (< 60) / `"Xm Ys"` (< 3600) / `"Xh Ym"` (≥ 3600)

### Path Truncation (`fmt_path`)

```
Full path fits      → a/b/c/file.c
start/…/parent/file → sdk/…/gpio/gpio.c   ← preferred, grow head
…/parent/file       → …/gpio/gpio.c
…/file              → …/gpio.c
filename only       → gpio.c (truncated if still too long)
```

---

## AD-10: qrag explore — Unified Database Explorer (replaces hub)

**Decision:** `qrag hub` is replaced entirely by `qrag explore`. `explore` exposes the same operations (list, download, push, delete) as discrete subcommands for scripting/CI, and additionally opens a full-screen Rich TUI when invoked with no arguments. Multiple remote backends are supported via a registry in `~/.qrag/config.json`.

**Why:** `hub` was a thin wrapper around a single GitHub Releases remote. Teams now need to: (1) see local and remote databases in one unified view; (2) visualize database contents (language stats, keyword tags, staleness); (3) push to multiple destinations (GitHub, HuggingFace Hub, JFrog Artifactory, git+LFS); (4) safely delete with auto-deactivation. None of these fit cleanly into the existing `hub` surface.

**Trade-off:** `hub` is deprecated (hidden alias for one release cycle) before removal. New backend modules (HF Hub, JFrog, git+LFS) add optional runtime deps only when those remotes are configured.

### Revised design decisions (design grilling — sequenced delivery)

A grilling session refined AD-10 before implementation began. The command tree and multi-remote vision below still hold; these points amend the specifics, and the epic ships as **sequenced, merge-as-you-go PRs** in the #41→…→#48 dependency order.

- **Code layout:** a single `src/qrag/explore.py` holds the data layer *and* all backends; `cli.py` stays thin wrappers; interactive views live in `tui.py`.
- **`hub` is retained (not hidden).** `explore` **wraps** the existing `github_distribution.py` rather than replacing it; `hub` stays visible. `github_distribution.py` is kept as the GitHub transport.
- **Remote config:** a `remotes{}` dict keyed by name in `~/.qrag/config.json`; the legacy single `repo_url`/`repo_type` auto-migrates to `remotes["default"]` (same pattern as `active_version`→`active_versions`).
- **Extensibility is first-class:** a `RemoteBackend` ABC + `@register_backend("type")` decorator registry. Adding a backend = one subclass, no other edits. Contract: `check_auth`, `list_versions`, `download`, `push`, `can_push`, and a **protected** `delete_remote` (CLI wraps it in a strong confirmation). Entry-point plugin discovery is deferred but the lookup is designed to accept it later.
- **TUI engine:** Rich + `readchar` (a small key-input loop), not a heavyweight framework — keeps install lean.
- **Stats are lean:** language %, symbol/section/doc/word counts, size, build date, active flag. The **keyword tag cloud and file-manifest "coverage"** originally sketched here are **dropped** (no consumer justified the cost).
- **Diff (#47)** exposes an interactive expandable tree (Code: AST-tree ⇄ file-tree; Docs: file-tree) with fuzzy search, built on a reusable `TreeView` widget shared with the #46 browser, plus a `--json`/`--verbose` flat mode for CI.
- **Testing:** data layer against real temp SQLite DBs; backends via mocked transport; real remotes behind `@pytest.mark.integration`.

**Status:**
- EXPLORE-A (#41) — `feat/explore-list-stats`: `explore.py` data layer + `explore list`/`explore stats` (local, lean panel).
- EXPLORE-B (#42) — `feat/explore-github-remote`: `RemoteBackend` ABC + `@register_backend` registry + `GitHubBackend` (wraps `github_distribution.py`); `remotes{}` config with legacy `repo_url` auto-migration; unified `explore list --remote` and `explore download --remote` with origin tracking. `push`/`delete_remote` exist on the backend; their CLI surfaces in #45.
- EXPLORE-C (#43) — `feat/explore-delete`: `explore delete <version>` — local-cache delete with size summary, `[y/N]` confirmation (`--yes` to skip, non-TTY guard), and auto-deactivation via `config.remove_active_version()`.
- EXPLORE-D (#44) — `feat/explore-backends-remotes`: `HFBackend` (huggingface_hub SDK), `JFrogBackend` (`jf` CLI), `GitLFSBackend` (git + LFS via shallow clone), all self-registered; each lazily detects its tool (no new required deps). Registry management commands `add-remote` / `remove-remote` / `list-remotes` / `set-remote`. HF is unit-tested against a mocked SDK; JFrog and git+LFS are unit-tested at the command-construction level, with live-server coverage deferred behind `@pytest.mark.integration`.
- EXPLORE-E (#45) — `feat/explore-push`: `explore push VERSION --remote [NAME] --dry-run --force` (target resolves explicit → `origin_remote` → default via `resolve_push_backend`, which rejects `can_push=False` backends; `check_auth()` pre-flight before upload). `explore delete VERSION --remote NAME` is the **protected** remote wipe — requires typing the version name to confirm (or `--yes`; non-TTY refused).
- EXPLORE-F (#46) — `feat/explore-tui`: reusable `TreeView` widget (expand/collapse, subsequence fuzzy filter keeping ancestors + auto-expanding matches, j/k nav) on Rich + `readchar`; `run_explore_browser()` interactive database browser (`qrag explore` with no subcommand). `readchar` is in the `[build]` extra and imported lazily; non-TTY or missing readchar falls back to `explore list`. `TreeView`'s pure model is unit-tested without a terminal and is the shared foundation for #47's interactive diff.
- EXPLORE-G (#47) — `feat/explore-diff`: `compute_diff()` → `DiffResult` (code + doc source files added/removed/changed via `file_manifest` sha256; symbols added/removed via `symbols`, `parent_name` from `code_chunks`; language % shift). `explore diff V1 V2 --json`/`--verbose` for CI; interactive `TreeView` diff on a TTY (Code file-tree ⇄ AST/parent tree, Docs file-tree, `/` fuzzy). Pure tree builders (`build_file_tree`, `build_symbol_ast_tree`) unit-tested. Keyword-tag diff dropped with the tag cloud.
- EXPLORE-H (#48) — `feat/explore-push-all`: `explore push VERSION --all-remotes` — `push_all_remotes()` pushes to every configured remote sequentially, skips read-only backends, continue-on-failure; the CLI prints per-remote status and, on failure, the exact per-remote re-run commands + exits 1.

**Epic status: COMPLETE.** All of #41–#48 shipped. One deviation from the original plan above: `qrag hub` was **retained** (explore wraps `github_distribution.py`) rather than becoming a hidden deprecated alias.

Remaining sub-issues follow the chain.

**Remote resolution:** `resolve_remote(name)` → a named remote, else the configured `default`, else a github default synthesized from the legacy `repo_url`/`QRAG_GITHUB_URL` (backward compat), else an actionable error. `get_backend()` maps the remote's `type` through `REGISTRY` to a `RemoteBackend` instance.

### qrag explore Command Tree

```mermaid
flowchart TD
    EXP["qrag explore"]
    EXP -->|"no args"| TUI["Rich TUI\n(interactive browser)"]
    EXP --> LIST["explore list\nlocal + all remotes\nRich table"]
    EXP --> STATS["explore stats VERSION\nlanguage % · symbol taxonomy\nkeyword tags · staleness · coverage"]
    EXP --> DOWNLOAD["explore download VERSION\n--remote NAME"]
    EXP --> DELETE["explore delete VERSION\n--yes / -y"]
    EXP --> PUSH["explore push VERSION\n--remote NAME --dry-run --force"]
    EXP --> DIFF["explore diff V1 V2\n--files --symbols --json"]
    EXP --> ADDREM["explore add-remote NAME --type TYPE URL\nexplore remove-remote NAME\nexplore list-remotes"]
    EXP --> SETREM["explore set-remote VERSION REMOTE"]
```

### Multi-Remote Architecture

```mermaid
flowchart TD
    CFG["~/.qrag/config.json\nremotes:\n  default: { type: github, url: … }\n  hf-mirror: { type: huggingface, url: … }\n  ci-server: { type: jfrog, url: … }"]

    CFG --> GHB["GitHubBackend\nAuth: GITHUB_TOKEN → gh auth token"]
    CFG --> HFB["HuggingFaceBackend\nAuth: HF_TOKEN → huggingface-cli login"]
    CFG --> JFB["JFrogBackend\nAuth: JFROG_TOKEN → jf config show"]
    CFG --> LFB["GitLFSBackend\nAuth: GIT_TOKEN → system git credentials"]

    GHB & HFB & JFB & LFB -->|"list / push / download"| OPS["Remote Operations\npre-flight check → upload/download → verify SHA-256"]
```

### Origin Tracking

Every downloaded database records its source remote in `~/.qrag/<version>/config.json`:

```json
{ "origin_remote": "default", "origin_version": "v1.0" }
```

`explore push` defaults to `origin_remote`; `explore set-remote` reassigns it anytime.

### Keyword Tag Cloud — Two Sources

```mermaid
flowchart LR
    subgraph DOCS["Docs database"]
        DS["doc_sections.title\n(section headings)"] -->|"stop-word filter\ntop 40 words"| DTAGS["DMA  UART  IRQ  GPIO\nTimer  SPI  clock  reset"]
    end

    subgraph CODE["Code database"]
        SYM["symbols.name\n(function/struct/macro names)"] -->|"split camelCase + snake_case\ncount one-word tokens\ntop 40"| CTAGS["DMA  Transfer  init\nuart  config  enable"]
    end
```

### Implementation Plan (issue dependency chain)

```mermaid
flowchart LR
    A["#41 EXPLORE-A\nlist + stats\nlocal MVP"]
    B["#42 EXPLORE-B\nGitHub remote\n+ download"]
    C["#43 EXPLORE-C\ndelete\n+ deactivate"]
    D["#44 EXPLORE-D\nHF · JFrog · git+LFS\nadd-remote"]
    E["#45 EXPLORE-E\npush\npre-flight"]
    F["#46 EXPLORE-F\nTUI"]
    G["#47 EXPLORE-G\ndiff"]
    H["#48 EXPLORE-H\n--all-remotes"]

    A --> B --> C --> D --> E --> F
    A --> G
    E --> H
```

Each step is independently end-to-end testable. Epic tracked in GH#49.

---

## AD-11: CPU-Only Torch Default (GH#35)

**Decision:** `[tool.uv.sources]` in `pyproject.toml` redirects `torch` to the official PyTorch CPU-only wheel index (`https://download.pytorch.org/whl/cpu`) for all uv installs. The CUDA wheel is only fetched when the user explicitly passes `--no-sources` (uv) or installs `[full]` via pip.

**Why:** `sentence-transformers` (a base dependency) pulls in `torch`, and the default PyPI `torch` wheel on Linux bundles 15 NVIDIA/CUDA libraries totalling ~2.3 GB — all unused on CPU-only machines. The CPU wheel reduces the default install from **~2.53 GB to ~220 MB** (~91% reduction).

**Trade-off:** `[tool.uv.sources]` is uv-specific; pip and pipx users still get the CUDA wheel unless they pre-install `torch --index-url .../cpu` first. The permanent fix (GH#38) is to replace `torch` entirely with `onnxruntime` (~10 MB).

```mermaid
flowchart TD
    INST["uv tool install qrag"]
    INST --> SRC{"[tool.uv.sources]\ntorch source?"}
    SRC -->|"default (no --no-sources)"| CPU["pytorch-cpu index\nhttps://download.pytorch.org/whl/cpu\ntorch CPU wheel  ~220 MB"]
    SRC -->|"--no-sources flag"| CUDA["default PyPI\ntorch CUDA wheel + 15 NVIDIA pkgs\n~2.53 GB"]

    subgraph FUTURE["GH#38 — future"]
        ORT["onnxruntime  ~10 MB\nno torch dependency\nworks for pip + pipx + uv"]
    end
```

## AD-12: Replace sentence-transformers+torch with onnxruntime (GH#38)

**Decision:** Replace `sentence-transformers` and `torch` with `onnxruntime` + `tokenizers` for embedding inference. The ONNX-format `all-MiniLM-L6-v2` model (`Xenova/all-MiniLM-L6-v2` on HuggingFace) is bundled in the wheel and cached at `~/.qrag/models/all-MiniLM-L6-v2/` on first use when not bundled.

**Why:** `torch` is the sole reason for the 2.53 GB install size. `onnxruntime` provides the same CPU inference at ~30 MB total (onnxruntime ~10 MB + ONNX model ~22 MB). This fix benefits all package managers (pip, pipx, uv) equally — unlike AD-11 which was uv-only. Removes AD-11 `[tool.uv.sources]` entirely.

**Trade-off:** CUDA acceleration is no longer automatic; users needing GPU must install `onnxruntime-gpu` separately. Embeddings produced by onnxruntime may differ from sentence-transformers by floating-point rounding — teams rebuilding DBs should re-run `qrag build` to ensure vector consistency.

```mermaid
flowchart TD
    INST["uv/pip/pipx install qrag"]
    INST --> PKG["onnxruntime ~10 MB\ntokenizers ~3 MB\nhuggingface_hub ~5 MB\nTotal: ~30 MB"]
    PKG --> LOAD["embedder.py\n_load()"]
    LOAD --> LOCATE{"Bundled model?\n_BUNDLED_MODEL or\n_RUNTIME_MODEL"}
    LOCATE -->|found| SESS["onnxruntime.InferenceSession\nCPUExecutionProvider"]
    LOCATE -->|not found| DL["hf_hub_download\nXenova/all-MiniLM-L6-v2\n  tokenizer.json\n  onnx/model.onnx"]
    DL --> SESS
    SESS --> TOK["tokenizers.Tokenizer\nencode_batch → input_ids\n  attention_mask\n  token_type_ids"]
    TOK --> INF["ONNX inference\nlast_hidden_state [B, 128, 384]"]
    INF --> POOL["mean_pool + L2 normalize"]
    POOL --> EMB["float32 embeddings [B, 384]"]
```

---

## AD-13: ZIP-Based Database Export / Import (EXPLORE-ZIP)

**Decision:** `qrag explore export <version>` and `qrag explore import <file.zip>` provide offline peer-to-peer database sharing via a self-contained ZIP, ahead of the GitHub/JFrog remote backends (GH#42–45). All logic lives in `zip_distribution.py`; no new runtime dependencies (stdlib `zipfile`, `hashlib`, `shutil`).

**Why:** Teams need to transfer pre-built databases to air-gapped machines or share with colleagues who don't have GitHub access. ZIP is universally readable, doesn't require auth, and is trivially emailed or copied over SCP/USB.

**Trade-off:** No deduplication or delta transfer — the full ZIP is written even if only `docs.db` changed. Acceptable at MVP; future `explore push` to a proper remote will supersede this for large databases.

```mermaid
flowchart TD
    EXP["qrag explore export VERSION"]
    EXP --> VDIR["CACHE_DIR/VERSION/\ncode.db · docs.db\nconfig.json · build-report.txt"]
    VDIR --> SHA["_sha256() per file\nhashlib.sha256 / 65 KB chunks"]
    SHA --> MAN["manifest.json\n{ version, exported_at,\n  embedding_model, files: {sha256, size} }"]
    MAN --> ZIP["version.zip\nZIP_DEFLATED\nversion/ prefix on every entry"]

    IMP["qrag explore import FILE.zip"]
    IMP --> RMAN["read manifest.json\nfrom ZIP"]
    RMAN --> CONF["conflict check\nCAHCE_DIR/target exists?\n--yes or [y/N]"]
    CONF --> EXT["extract files\nstrip top-level prefix\n→ CACHE_DIR/target/"]
    EXT --> VER["verify SHA-256\nevery file in manifest.files"]
    VER -->|pass| REG["add_active_version(target)"]
    VER -->|fail| CLN["shutil.rmtree(target_dir)\nClickException"]
```

---

## AD-14: Real GPU Embedding via onnxruntime-gpu (ISSUE-008)

**Decision:** `resolve_device()` now performs real CUDA detection through `onnxruntime.get_available_providers()` instead of unconditionally returning `"cpu"`. `onnxruntime` is split out of the base dependencies into two mutually-exclusive extras — `[cpu]` (`onnxruntime`) and `[gpu]` (`onnxruntime-gpu`) — both pinned to `>=1.19,<2.0` (the version floor that requires CUDA 12.x + cuDNN 9). `[full]` becomes an alias for `[build, gpu]`.

**Why:** AD-12 replaced `torch`+`sentence-transformers` with `onnxruntime` for install-size reasons, but as a side effect `resolve_device("cuda")` was left raising a `ValueError` and `auto` always resolved to `"cpu"` — GPU embedding was advertised (`--device` flag, README `[full]` extra) but never actually reachable. Additionally, `onnxruntime` and `onnxruntime-gpu` both occupy the same import namespace and cannot coexist, so `onnxruntime-gpu` could not be safely added as a plain extra on top of the base dependency — it had to replace it. Splitting into `[cpu]`/`[gpu]` extras is the only way `pip`/`uv` can produce a reproducible environment for either path.

**Trade-off:** Every install command (including plain Consumer installs) must now explicitly pick `[cpu]` or `[gpu]` — `qrag` alone is no longer installable, since a bare install would resolve zero embedding backends. This is a one-time breaking change to install instructions, documented in the README's Consumer and Builder sections. GPU users also take on system-level CUDA/cuDNN maintenance burden that the CPU path avoids entirely; the README documents Linux/Windows/WSL prerequisites explicitly (including the WSL-specific gotcha that the NVIDIA driver must live on the Windows host, not inside the WSL distro).

```mermaid
flowchart TD
    BUILD["qrag build --device=auto|cpu|cuda"]
    BUILD --> RESOLVE["resolve_device(requested)"]
    RESOLVE -->|"auto"| CHECK{"onnxruntime.get_available_providers()\ncontains CUDAExecutionProvider?"}
    CHECK -->|yes| CUDA["device = cuda\nbatch_size = 1024"]
    CHECK -->|no| CPU["device = cpu\nbatch_size = 256"]
    RESOLVE -->|"cuda (explicit)"| CHECK2{"CUDAExecutionProvider\navailable?"}
    CHECK2 -->|yes| CUDA
    CHECK2 -->|no| ERR["ValueError:\ninstall onnxruntime-gpu (qrag[gpu])\n+ CUDA 12.x + cuDNN 9"]
    RESOLVE -->|"cpu (explicit)"| CPU

    CUDA --> LOAD["_load(device)\nInferenceSession providers=\n[CUDAExecutionProvider, CPUExecutionProvider]"]
    CPU --> LOAD2["_load(device)\nInferenceSession providers=\n[CPUExecutionProvider]"]

    subgraph PKG["pyproject.toml extras"]
        CPUEXT["[cpu] → onnxruntime>=1.19,<2.0"]
        GPUEXT["[gpu] → onnxruntime-gpu>=1.19,<2.0"]
        FULLEXT["[full] → [build] + [gpu]"]
    end
```

---

## AD-15: Pin onnxruntime-gpu to CUDA 12 line + pip-bundled CUDA/cuDNN runtime (corrects AD-14)

**Decision:** Tighten `[gpu]` to `onnxruntime-gpu[cuda,cudnn]>=1.21,<1.27` (was `>=1.19,<2.0`). Call `onnxruntime.preload_dlls(cuda=True, cudnn=True, msvc=False)` in `_load()` before creating a CUDA `InferenceSession`. Drop the README's system-wide CUDA Toolkit install instructions — only the NVIDIA driver is required at the OS level now.

**Why:** Real-world testing of AD-14 surfaced two bugs. First, `onnxruntime-gpu`'s PyPI releases silently changed their CUDA major-version dependency: 1.21–1.26 target CUDA 12 (`nvidia-cuda-runtime-cu12`), but **1.27.0 switched to CUDA 13** (`nvidia-cuda-runtime-cu13`) — confirmed via PyPI `requires_dist` metadata. AD-14's `<2.0` upper bound let the resolver pick 1.27.0, which then failed at runtime with `libcudart.so.13: cannot open shared object file` on any machine with a CUDA 12 system install (i.e., everyone following AD-14's own documented prerequisites). Second, `onnxruntime-gpu>=1.21` ships optional `[cuda]`/`[cudnn]` pip extras that install the CUDA/cuDNN runtime as ordinary Python wheels — but onnxruntime doesn't search `site-packages` for them by default; `preload_dlls()` (added in 1.21.0) is required to make the pip-installed libraries discoverable. Using both together eliminates the system-level CUDA Toolkit install entirely, which is a strictly simpler and more reliable setup than AD-14's per-OS Toolkit instructions (system Toolkit installs are a common source of version-mismatch bugs like this one).

**Trade-off:** `onnxruntime-gpu` will eventually require re-pinning again once the CUDA 13 ecosystem matures (newer PyTorch/cuDNN builds, wider driver support) — the `<1.27` ceiling is a deliberate, temporary floor-and-ceiling pin, not a permanent one. `qrag[gpu]` installs are now larger (~600 MB, pip-installed CUDA/cuDNN runtime) than AD-14 assumed (which relied on a system Toolkit and only pulled the ~10 MB `onnxruntime-gpu` wheel itself).

```mermaid
flowchart TD
    OLD["AD-14: onnxruntime-gpu>=1.19,<2.0\nresolves to 1.27.0 (latest)"]
    OLD --> BUG["1.27.0 requires libcudart.so.13\nSystem has CUDA 12 → ImportError"]

    NEW["AD-15: onnxruntime-gpu[cuda,cudnn]>=1.21,<1.27"]
    NEW --> PIP["pip installs:\nnvidia-cuda-runtime-cu12\nnvidia-cudnn-cu12"]
    PIP --> PRELOAD["_load(): ort.preload_dlls(cuda=True, cudnn=True)\nmakes onnxruntime find the pip-installed .so files"]
    PRELOAD --> SESS["InferenceSession(providers=[CUDAExecutionProvider, ...])\nsucceeds — no system Toolkit needed"]

    subgraph SYS["System-level requirement (all OSes)"]
        DRIVER["NVIDIA driver only\n(kernel module — can't be pip-installed)"]
    end
```

---

## AD-16: Stabilize Embed Task `total` to Fix Stuck ETA Column

**Decision:** In `BuildLayout.on_embed_batch()` (`tui.py`), only push a new `total` to the Rich `Progress` embed task when the rolling estimate (`est_total`) drifts more than 10% from the task's current `total`. Otherwise reuse the existing `total` unchanged.

**Why:** Rich's `Progress.update()` calls `task._reset()` — which clears the rolling speed-sample deque used to compute `time_remaining` — every time the `total` argument differs from the task's current `total`. `on_embed_batch()` recomputes `est_total` from `avg_chunks_per_file * total_files` on every single batch, and that estimate jitters (±1 chunk, rounding) almost every call. The result: the embed task's `total` changed on nearly every update, wiping the speed samples before Rich ever had two samples far enough apart to compute a speed — so `_EtaColumn.render()`'s `task.time_remaining is None` branch fired every time, showing `—` for the entire embedding phase regardless of how long the build ran.

**Trade-off:** The embed ETA now updates on a coarser total (only re-baselines on ≥10% swings), so it lags slightly when the true average chunks/file shifts sharply early in a build. This is preferable to an ETA that never renders.

```mermaid
sequenceDiagram
    participant CB as on_embed_batch()
    participant P as Rich Progress.update()
    participant T as Task

    Note over CB: before fix — every batch
    CB->>P: update(total=est_total_jittery)
    P->>T: total changed → task._reset()
    Note over T: speed samples wiped
    T-->>P: time_remaining = None
    P-->>CB: ETA column renders "—"

    Note over CB: after fix
    CB->>CB: compare est_total vs current total
    alt drift < 10%
        CB->>P: update(total=current_total unchanged)
        P->>T: total unchanged → samples kept
        T-->>P: time_remaining = computed value
    else drift >= 10%
        CB->>P: update(total=est_total)
        P->>T: total changed → task._reset() (rare, acceptable)
    end
```

---

## AD-17: Session-Scoped Database Selection (LLM Context Narrowing)

**Decision:** Add three new MCP tools — `list_databases`, `set_active_databases`,
`reset_active_databases` — that let the AI agent narrow which of the *globally
active* databases (`active_versions` in `~/.qrag/config.json`) are searched for
the current conversation, without touching the global config file. The
narrowed selection lives entirely in the running MCP server process's memory
and is discarded when that process exits.

**Why:** `qrag ai active v1 v2 v3` (AD-1) controls a durable, user-approved
candidate pool, but users routinely leave many unrelated DBs in that pool and
forget to trim it per-conversation. Fanning out search across every globally
active DB on every query re-introduces the noisy, cross-project results
multi-DB fan-out was supposed to keep useful — the LLM gets flooded with
irrelevant hits and hallucinates. The fix is a second, session-scoped layer
that narrows *which subset* of the already-approved pool gets searched in a
given conversation, driven by the LLM on the user's behalf, without requiring
a CLI round-trip or mutating the user's durable global choice.

### Why No Session-ID Scheme Is Needed

qrag's MCP server only ever runs over **stdio** (`cli.py`'s `_write_mcp_config`
writes a `"command"` entry, not a URL) — every client (Claude Code, Gemini
CLI, Antigravity) spawns its **own private subprocess** per agent session.
There is no shared server handling multiple LLMs concurrently, so the OS
process boundary already *is* the session boundary. A module-level variable
in `mcp_server.py` is sufficient isolation; an explicit session-ID/token
scheme would solve a problem (multi-tenant state collision) that cannot occur
under stdio transport. This would need revisiting only if qrag ever adds an
HTTP/SSE transport shared by multiple remote clients.

```mermaid
flowchart TD
    subgraph "Agent Session A"
        CA["Claude Code"] -->|spawns| PA["qrag mcp_server process A\n_session_dbs = {sdk-v1}"]
    end
    subgraph "Agent Session B"
        CB["Gemini CLI"] -->|spawns| PB["qrag mcp_server process B\n_session_dbs = {rtos-v2, trm-v3}"]
    end
    CFG[("~/.qrag/config.json\nactive_versions: [sdk-v1, rtos-v2, trm-v3]\n(shared, read-only from here)")]
    PA -.->|read at startup| CFG
    PB -.->|read at startup| CFG
```

### Two-Tier Scope

```mermaid
flowchart LR
    ALL["All downloaded DBs\n(on disk, CACHE_DIR)"]
    ALL -->|"qrag ai active v1 v2 v3\n(durable, CLI-managed)"| GLOBAL["Global active set\nactive_versions"]
    GLOBAL -->|"set_active_databases([...])\n(in-memory, per-process)"| SESSION["Session-scoped set\n(subset of global, default = global)"]
    SESSION -->|"search_code / search_docs / etc."| RESULTS["Fan-out results"]
```

A session selection can only be a subset of the global set (Q4) — the
checklist tool never offers, and `set_active_databases` never accepts, a DB
outside `active_versions`. Fully deactivated DBs are invisible to the whole
session-scoping feature, including the fallback-suggestion mechanism below;
global deactivation is a deliberate, durable user choice that a mid-chat tool
call must not second-guess.

### Checklist Flow

The interactive checklist itself is **not** an MCP protocol feature — the
server's stdin/stdout are fully committed to JSON-RPC framing and cannot also
carry raw keypresses. The checklist is rendered by the **agent's own host UI**
(e.g. Claude Code's `AskUserQuestion` with `multiSelect: true`), driven by
tool calls:

```mermaid
sequenceDiagram
    participant U as User
    participant L as LLM (agent host)
    participant M as mcp_server.py process

    Note over L: first qrag tool call of the session,<br/>or user asks to re-narrow
    L->>M: tools/call list_databases
    M-->>L: [{version, has_code, has_docs, size_bytes}, ...]<br/>(global active_versions only)
    L->>U: interactive checklist (arrow keys + spacebar)
    U-->>L: selection + Enter
    L->>M: tools/call set_active_databases([...])
    alt selection empty or contains unknown version
        M-->>L: error (hard reject, Q11/Q12)
    else valid non-empty subset
        M->>M: _session_dbs = selection\n_session_scoped = True
        M-->>L: ok
    end
    L->>U: (skill instructs LLM to summarize the scoping decision)
    L->>M: tools/call search_code / search_docs / ...
    M-->>L: results + excluded_active_dbs field
```

### First-Use Gating: Server-Tracked, Not LLM-Remembered

The server — not the LLM's conversational memory — is the source of truth for
"has this session been scoped yet." A module-level `_session_scoped: bool`
flag flips to `True` the first time `set_active_databases` succeeds in that
process's lifetime. Since the process *is* the session, this flag needs no
reconciliation across context-compaction or summarization: a new session is
always a new process, always starting `_session_scoped = False`.

Until scoped, `search_code`/`search_docs`/`get_symbol_definition`/
`list_symbols` still answer against the *full* global set (unchanged default
behavior — no regression for small global lists) but the response carries a
`scope_hint` the skill instructions tell the LLM to act on once, e.g.:

```json
{"results": [...], "scope_hint": "Session not yet scoped — 5 databases available via list_databases()"}
```

### Fallback Suggestion (Excluded-But-Globally-Active DBs)

Every `search_code`/`search_docs` response also carries `excluded_active_dbs`
— the set difference `active_versions - _session_dbs` — computed for free
from data already in memory (no new DB queries, no filesystem scan). The
server does **no** relevance matching; the skill instructs the LLM to reason
over that list itself (by name/keyword) when results look thin, and suggest
reactivation to the user. This keeps `mcp_server.py`/`database.py` free of
heuristic-tuning surface area and keeps the noisy-fallback-search cost at
zero on the common path.

```json
{
  "results": [ /* top-10 merged, from session-scoped DBs only */ ],
  "excluded_active_dbs": ["legacy-sdk", "old-fw"]
}
```

### New MCP Tools

| Tool | Input | Behavior |
|------|-------|----------|
| `list_databases()` | — | Returns global `active_versions` with `{version, has_code, has_docs, size_bytes}` (cheap, filesystem-derived — no DB open) |
| `set_active_databases(versions)` | `list[str]`, must be non-empty subset of global active set | Hard error on empty list or any unknown version (Q11/Q12); on success, sets `_session_dbs` + `_session_scoped = True` |
| `reset_active_databases()` | — | Clears `_session_dbs` back to the full global set in one call (Q14) — no need to re-run the checklist selecting every box |

**Trade-off:** Losing the running process (agent restart, MCP server crash)
silently drops the session narrowing back to the full global set — there is
no persistence by design (Q3). This is treated as acceptable: it fails open
to previous multi-DB-fan-out behavior rather than failing closed, and a
session that no longer exists has no state worth persisting.


## AD-18 — Producer completion signaling on failure

**Decision:** The thread entry point `_run_producer_safely` owns the completion sentinel. Code and document producers only enqueue results and per-file errors. The wrapper records unexpected producer exceptions (including thread-local SystemExit/KeyboardInterrupt) and sends exactly one sentinel in `finally`.

**Why:** Pool construction, submission, iteration, or shutdown can raise outside the per-file handler. Previously that killed the producer without signaling the consumer, which waited indefinitely. Sentinel delivery after queued results lets the consumer flush completed work before the existing failed-build report and exit-1 path runs. Failed files remain eligible for retry and resume state is retained.

```mermaid
flowchart TD
    A[Producer wrapper] --> B[Code or document producer]
    B --> C[Queue results and per-file errors]
    B -->|Unexpected exception| D[Record producer failure and queue error]
    C --> E[Finally enqueue one completion sentinel]
    D --> E
    E --> F[Consumer drains results and flushes progress]
    F --> G[Write failed report and exit 1 if errors exist]
```

**Trade-off:** This guarantees signaling when the producer unwinds. It does not impose timeouts on a living but stalled process pool, or recover from termination of the whole application. The main thread still owns user interrupt handling.
