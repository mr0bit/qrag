# qrag — Project Memory

> **How to use this file:** Read it at the start of every new Claude session. Update it before ending any session where significant design decisions, context, or accumulated discussion happened. It is intentionally dense — prefer references to full docs over inline prose.

---

## What is qrag?

**qrag** (née *raghub*) is a Python CLI + MCP server that builds semantic RAG databases from source code (any language) and technical documentation (PDFs, HTML). It is designed for embedded-systems teams and software teams working with large vendor SDKs, TRMs, and multi-language codebases.

**Core value proposition:** One team member prepares the index once (parsing + embedding); the whole team downloads pre-built SQLite databases and gets instant semantic + structural code/doc search inside their AI agent (Claude, Gemini CLI).

**Version:** `0.2.0` (as of 2026-06-24)
**Install:** `uv tool install git+https://github.com/inegmdev/qrag.git@main`

---

## Product Features

| Feature | Status |
|---------|--------|
| Multi-language parsing: C, C++, Rust, Python, Go, JS, TS, Java, C#, Ruby, Swift, Kotlin, Scala, Lua, PHP, Bash, Zig, Elixir, Haskell, OCaml, Erlang, Dart, R, Fortran, Verilog, VHDL + more | ✅ PR #12 |
| Build system files indexed as first-class code (CMakeLists.txt, Makefile, Cargo.toml, package.json, go.mod, pom.xml, conanfile.py, *.cmake, *.gradle, *.toml) | ✅ PR #12 |
| Language-agnostic `chunk_type` (function/class/struct/interface/enum/macro/type_alias/module/constant) | ✅ PR #12 |
| `language` column in `code_chunks` + `symbols`; returned in all search/list results | ✅ PR #12 |
| PDF/HTML doc parsing + section chunking | ✅ Implemented |
| Local embeddings via `all-MiniLM-L6-v2` (384-dim, bundled in wheel) | ✅ PR #8 |
| SQLite + sqlite-vec for vector storage/search | ✅ Implemented |
| MCP server (JSON-RPC 2.0 over stdio) exposing 4 tools | ✅ Implemented |
| MCP server exceptions surface as JSON-RPC errors; logged to `~/.qrag/logs/mcp_errors.log` | ✅ PR #11 |
| `qrag build` — build `code.db` + `docs.db` | ✅ Implemented |
| `qrag hub push/download/list` — GitHub Releases distribution | ✅ Implemented |
| `qrag ai setup` — auto-install MCP config for Claude/Gemini | ✅ Implemented |
| `qrag search code/docs/symbol` — CLI search | ✅ Implemented |
| Automatic error log on failure (`~/.qrag/logs/`) | ✅ PR #8 |
| `uv` as primary install method | ✅ PR #8 |
| Rich TUI with progress bars + ETA | ✅ PR #20 |
| Post-build audit report file | ✅ PR #20 |
| Multi-database search (fan-out across N active DBs) | ✅ PR #16 |
| Session-scoped database narrowing (`list_databases`/`set_active_databases`/`reset_active_databases`) | ✅ AD-17 |
| Rich doc metadata (name, revision, status, page, section hierarchy) | ✅ PR #17 |
| Rich code metadata (parent block, call depth, chunk index within parent) | ✅ PR #17 |
| PyPI publish | ❌ Not done |
| GPU embedding / multicore chunking | ✅ ISSUE-008 / AD-14 / AD-15 |

---

## MCP Tools (exposed to AI agents)

| Tool | Description | Returns |
|------|-------------|---------|
| `search_code(query)` | Semantic search over code chunks | symbol, file, line range, snippet, type, **language**, score |
| `search_docs(query)` | Semantic search over doc sections | title, content, page range, feature tags, doc_type, score |
| `get_symbol_definition(symbol)` | Exact definition lookup by name | full code text, file, line range, type, **language** |
| `list_symbols(pattern="")` | List all indexed symbols | name, type, **language**, file, line |

---

## Architecture

```
PREPARATION (runs once per SDK/docs update):
  Source files (C/C++/Rust/Python/Go/…) + Build files (CMakeLists.txt/Cargo.toml/…) + Docs (.pdf/.html)
    → chunker.py (tree-sitter-language-pack, 305+ grammars / BeautifulSoup+PyMuPDF)
    → embedder.py (all-MiniLM-L6-v2, 384-dim, bundled in wheel)
    → database.py (SQLite + sqlite-vec)
    → ~/.qrag/<version>/code.db + docs.db
    → github_distribution.py → GitHub Releases

TEAM USAGE:
  qrag hub download <version>
  qrag ai setup --ai=claude|gemini
  → AI agent calls MCP tools → mcp_server.py → database.py
```

**Key source files:**

| File | Role |
|------|------|
| `src/qrag/cli.py` | Click CLI; `main()` entry point wraps `cli()` for error logging |
| `src/qrag/mcp_server.py` | JSON-RPC 2.0 MCP server over stdio; errors surface + logged |
| `src/qrag/database.py` | SQLite + sqlite-vec; code/doc search, symbol lookup; `language` column |
| `src/qrag/embedder.py` | Sentence-Transformers wrapper; bundled `all-MiniLM-L6-v2` |
| `src/qrag/chunker.py` | Registry-driven multi-language parser (305+ grammars via tree-sitter-language-pack); `_EXT_REGISTRY` + `_FILENAME_REGISTRY` |
| `src/qrag/doc_parser.py` | PDF (PyMuPDF) + HTML (BeautifulSoup) → doc sections |
| `src/qrag/config.py` | `~/.qrag/config.json` load/save |
| `src/qrag/github_distribution.py` | GitHub Releases API for push/download/list |

**User data layout:**
```
~/.qrag/
├── config.json          # { repo_url, active_version, cache_dir }
├── logs/                # qrag-YYYYMMDD-HHMMSS.log on any failure; mcp_errors.log for MCP
├── <version>/
│   ├── code.db          # code_chunks (language col), symbols (language col), vec_code
│   ├── docs.db          # doc_sections, vec_docs
│   └── manifest.json
```

---

## Backlog (Top Open Items)

Full authoritative list: [`docs/BACKLOG.md`](docs/BACKLOG.md).

| ID | Severity | Summary |
|----|----------|---------|
| ~~**IS1–IS5**~~ | ~~Critical~~ | Resolved |
| ~~**C0, C1, C2, C3**~~ | ~~Critical~~ | Resolved |
| ~~**GH#13, GH#18, GH#23, GH#26, GH#28, GH#29, GH#31, GH#32**~~ | ~~High~~ | Resolved |
| ~~**GH#35**~~ | ~~High~~ | Resolved by GH#38 / PR #55 |
| ~~**GH#38**~~ | ~~High~~ | Resolved in PR #55 — onnxruntime + tokenizers, ~30 MB |
| ~~**ISSUE-008**~~ | ~~High~~ | Resolved — real CUDA detection + `[cpu]`/`[gpu]` extras (AD-14); pin corrected + pip-bundled CUDA/cuDNN runtime after real-machine testing (AD-15) |
| **H0** | High | Build-source relationship metadata (cmake targets ↔ source files) |
| **H1–H6, GH#27, GH#30** | High | See `docs/BACKLOG.md` |
| **GH#49** | Feature | [EPIC] `qrag explore` — replaces `hub`; TUI + multi-remote (GH#41–48) |
| **GH#41** | Feature | [EXPLORE-A] `explore list` + `explore stats` — local DB visualization (MVP) |
| **GH#42** | Feature | [EXPLORE-B] GitHub remote integration + download + origin tracking |
| **GH#43** | Feature | [EXPLORE-C] `explore delete` with confirmation + auto-deactivation |
| **GH#44** | Feature | [EXPLORE-D] HuggingFace Hub, JFrog, git+LFS backends + `add-remote` |
| **GH#45** | Feature | [EXPLORE-E] `explore push` with pre-flight check + remote selection |
| **GH#46** | Feature | [EXPLORE-F] `qrag explore` interactive TUI |
| **GH#47** | Feature | [EXPLORE-G] `explore diff v1 v2` — version comparison |
| **GH#48** | Feature | [EXPLORE-H] `explore push --all-remotes` — multi-remote sync |
| **M1–M6** | Medium | See `docs/BACKLOG.md` |
| **L1–L8** | Low | See `docs/BACKLOG.md` |

---

## Git & PR State (as of 2026-07-01)

Update 2026-10-28: PR #84 and #77 are merged. PR [#85](https://github.com/mr0bit/qrag/pull/85), branch `fix/producer-crash-hang`, is open: H4 producer completion signaling fix (AD-18), with 133 targeted tests passing, including 12 new crash cases. Real-model integration tests were not run.

Update 2026-10-28: PR [#84](https://github.com/mr0bit/qrag/pull/84), branch `fix/build-failure-report`, is open for review. H3's nonzero-exit guard was already present; failed builds now retain a fresh report with explicit failure status and error details before exiting. 121 targeted tests passed, including eight new build-outcome regression cases; real-model integration tests were not run.

| PR / Issue | Branch | State |
|------------|--------|-------|
| #10 | `feat/uv-install-and-error-logging` | Merged |
| #11 | `fix/c1-mcp-silent-exception-swallow` | Merged |
| #12 | `feat/c0-multi-language-support` | Merged |
| #15 | `feat/gh13-role-based-deps` | Merged |
| #16 | `feat/is3-multi-db-fanout` | Merged |
| #17 | `feat/is4-is5-rich-metadata` | Merged |
| #19 | `refactor/rename-prepare-to-build` | Merged |
| #20 | `feat/is1-is2-rich-tui-report` | Merged |
| #22 | `feat/gh18-antigravity-support` | Merged |
| #33 | `feat/gh26-tui-improvements` | Merged — Rich TUI MVC, log panel, proportional CPU split |
| #34 | `fix/gh28-gh29-build-safety` | Merged — incremental manifest writes + --force confirmation |
| ~~#39~~ | `fix/gh35-cpu-only-torch-default` | Superseded by PR #55 (GH#38) |
| ~~GH#35~~ | — | Resolved by GH#38 / PR #55 |
| ~~GH#38~~ | — | Resolved — onnxruntime refactor done in PR #55 |
| #55 | `fix/gh38-onnxruntime-embedder` | Merged — onnxruntime replaces torch+sentence-transformers (~30 MB) |
| GH#49 | — | Open — [EPIC] qrag explore |
| GH#41–48 | — | Open — explore sub-issues (implement in order) |
| #58 | `claude/gpu-enabling-docs-la8bet` | Merged — real GPU embedding (ISSUE-008/AD-14): fixed `resolve_device` CUDA detection, split `onnxruntime`/`onnxruntime-gpu` into `[cpu]`/`[gpu]` extras, documented Linux/Windows/WSL GPU prerequisites in README |
| #59 | `claude/gpu-enabling-docs-la8bet` | Merged — fixes `_ensure_build_deps()` stale `build-gpu` extra reference left over after PR #58's `[cpu]`/`[gpu]` split; found via a real `qrag[gpu]` install |
| #60 | `claude/gpu-enabling-docs-la8bet` | Merged — AD-15: real-machine `qrag[gpu]` install hit `libcudart.so.13` — `onnxruntime-gpu`'s `<2.0` pin let the resolver pick 1.27.0, which needs CUDA 13 not 12. Retightened to `onnxruntime-gpu[cuda,cudnn]>=1.21,<1.27`, added `ort.preload_dlls()`, dropped system-wide CUDA Toolkit install steps from README (NVIDIA driver only now needed) |
| #62 | `claude/gpu-enabling-docs-la8bet` | Open — awaiting review — docs-only: clarifies the `device_discovery.cc GetGpuDevices` sysfs warning is benign (confirmed on real GPU hardware, utilization spiked during build despite the warning) |
| #61 | `claude/eta-embedding-display-scno5x` | Open — fix M8: embed row ETA stuck on `—` in `qrag build` TUI; `on_embed_batch()` was resetting Rich's speed samples every batch by pushing a jittery `total`. See AD-16. |
| — | `claude/database-deactivation-llm-load-iggb26` | Open — AD-17: session-scoped database selection (`list_databases`/`set_active_databases`/`reset_active_databases` MCP tools + `SKILL_qrag.md` checklist workflow) |

---

## Design Decisions (Accumulated)

- **SQLite + sqlite-vec** chosen over Chroma/Pinecone for zero-dependency single-file distribution
- **`all-MiniLM-L6-v2`** (384-dim) chosen for local/free/fast; bundled in wheel (no HuggingFace runtime call)
- **`tree-sitter-language-pack`** (single dep, 305+ grammars) replaced individual `tree-sitter-c/cpp` packages
- **Build system files indexed as code** — CMakeLists.txt, Makefile, Cargo.toml etc. are first-class code chunks
- **Language-agnostic chunk_type** — free-form string; new languages never require a schema migration
- **`language` column** in `code_chunks` and `symbols` — AI agent knows result language
- **Function-level chunking** — tree-sitter identifies exact boundaries; large symbols auto-split with overlap
- **Section-level doc chunking** — preserves heading hierarchy and page references
- **Multi-DB fan-out** (PR #16) — `active_versions` list; MCP tools fan-out via ThreadPoolExecutor, merge by score, dedup
- **`uv tool install`** as primary install method; `[tool.uv.sources]` pins torch to CPU wheel (AD-11, superseded by AD-12)
- **`onnxruntime` split into `[cpu]`/`[gpu]` extras** — no longer a base dependency; every install must pick one explicitly; `[full]` aliases `[build, gpu]` (AD-14)
- **GPU detection is real** — `resolve_device()` checks `onnxruntime.get_available_providers()` for `CUDAExecutionProvider` instead of hardcoding CPU (AD-14)
- **No system-wide CUDA Toolkit needed** — `[gpu]` pins `onnxruntime-gpu[cuda,cudnn]>=1.21,<1.27` (excludes 1.27.0, which needs CUDA 13 not 12) and calls `ort.preload_dlls()`; the CUDA/cuDNN runtime comes from pip packages, only the NVIDIA driver is a system-level requirement (AD-15)
- **`qrag hub` → `qrag explore`** — hub is deprecated; explore replaces it with TUI + multi-remote support (AD-10)
- **Session-scoped DB selection** (AD-17) — new `list_databases`/`set_active_databases`/`reset_active_databases` MCP tools let the LLM narrow the *globally active* DB set to a per-conversation subset, in-memory/per-process (stdio = 1 process per session, no session-ID needed); checklist UI rendered by the agent host, not the MCP server; fallback suggestions expose `excluded_active_dbs` but do no server-side relevance matching — LLM reasons over it
- **Multi-remote backends** planned: GitHub Releases (current), HuggingFace Hub, JFrog Artifactory, git+LFS
- **Origin tracking** — downloaded DBs store `origin_remote` in `~/.qrag/<v>/config.json`
- **Keyword tags** — docs: top words from section titles; code: camelCase/snake_case token split from symbol names
- Entry point: `qrag.cli:main` (wraps `cli()` for error-log writing on non-zero exit)
- MCP server errors surface as JSON-RPC error responses AND log to `mcp_errors.log`

---

## How to Start a New Session

1. Read this file (`MEMORY.md`)
2. Read [`docs/BACKLOG.md`](docs/BACKLOG.md)
3. Sync GitHub issues: fetch open issues and add any not yet in backlog
4. Check `git log --oneline -10` for recent changes
5. Priority order: GH#41 (explore MVP) → H0/H3/H4/H6 → M items
6. Before ending: update `docs/BACKLOG.md` checkboxes, update this file, update `docs/ARCHITECTURE.md`


