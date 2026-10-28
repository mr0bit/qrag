from __future__ import annotations

import datetime
import json
import logging as _logging
import os
import platform
import queue
import sys
import threading
import time
import traceback
from collections import deque
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import click

from . import __version__
from .config import (
    active_version_dirs,
    add_active_version,
    code_db_paths,
    docs_db_paths,
    load_global,
    repo_url,
    save_global,
    set_repo_url,
    CACHE_DIR,
)

_logger = _logging.getLogger("qrag")


class _BufferingHandler(_logging.Handler):
    """Captures all log records in memory so they can be written to an error log file."""

    def __init__(self) -> None:
        super().__init__(level=_logging.DEBUG)
        self.records: list[_logging.LogRecord] = []

    def emit(self, record: _logging.LogRecord) -> None:
        self.records.append(record)


_buf_handler = _BufferingHandler()
_logger.addHandler(_buf_handler)
_logger.setLevel(_logging.DEBUG)

_verbose = False


@contextmanager
def _spinner(msg: str) -> Iterator[None]:
    """Show a Rich spinner if Rich is installed; no-op otherwise."""
    try:
        from rich.console import Console
        from rich.status import Status
        with Status(msg, console=Console(stderr=True), spinner="dots"):
            yield
    except ImportError:
        yield


def _log_dir() -> Path:
    return Path.home() / ".qrag" / "logs"


def _new_log_path() -> Path:
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return _log_dir() / f"qrag-{ts}.log"


def _write_error_log(log_path: Path, exc: BaseException | None) -> bool:
    """Write a human-readable error log. Returns False if the write itself fails."""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        plain = _logging.Formatter(
            fmt="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
        lines: list[str] = [
            "=" * 72,
            "qrag error log",
            f"  qrag version : {__version__}",
            f"  python       : {sys.version}",
            f"  platform     : {platform.platform()}",
            f"  command      : {' '.join(sys.argv)}",
            "=" * 72,
            "",
            "--- log records ---",
        ]
        for record in _buf_handler.records:
            lines.append(plain.format(record))
        lines.append("")
        if exc is not None:
            lines.append("--- exception ---")
            lines.extend(traceback.format_exception(type(exc), exc, exc.__traceback__))
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return True
    except Exception:
        return False


class _JsonFormatter(_logging.Formatter):
    def format(self, record: _logging.LogRecord) -> str:
        return json.dumps({
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        })


_SKILL_CONTENT = (Path(__file__).parent / "SKILL_qrag.md").read_text(encoding="utf-8")


_CHANGELOG = """\
v0.2.0
  - Automatic error log: on any failure, a log file is written to
    ~/.qrag/logs/ with version, platform, full command, and traceback.
    The path is printed to stderr so you can attach it to a bug report.
  - Producer errors in `build` now exit non-zero and appear in the log.

v0.1.0
  - Initial release: build, hub, ai, search, status commands.
  - Parallel code/doc indexing with Tree-sitter and Sentence-Transformers.
  - MCP server with search_code, search_docs, get_symbol, list_symbols.
"""


@click.group()
@click.version_option(__version__, message=f"%(prog)s %(version)s\n\nChangelog:\n{_CHANGELOG}")
@click.option("--verbose", is_flag=True, help="Emit structured JSON logs to stderr")
@click.pass_context
def cli(ctx, verbose: bool):
    """qrag: build semantic RAG databases from your code and docs."""
    global _verbose
    _verbose = verbose
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    if verbose:
        handler = _logging.StreamHandler(sys.stderr)
        handler.setFormatter(_JsonFormatter())
        _logger.addHandler(handler)
        _logger.setLevel(_logging.DEBUG)


# ---------------------------------------------------------------------------
# HARNESS — AI agent integration (MCP, skills, and other interfaces)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# STATUS — Check active version and configuration
# ---------------------------------------------------------------------------

@cli.command("status")
def status():
    """Show active versions and database file paths."""
    cfg = load_global()
    versions = cfg.get("active_versions", [])
    if not versions:
        click.echo("Active versions: (none)")
    else:
        click.echo(f"Active versions: {', '.join(versions)}")
        for v in versions:
            code_db = CACHE_DIR / v / "code.db"
            docs_db = CACHE_DIR / v / "docs.db"
            click.echo(f"  [{v}] code.db: {code_db} ({'exists' if code_db.exists() else 'missing'})")
            click.echo(f"  [{v}] docs.db: {docs_db} ({'exists' if docs_db.exists() else 'missing'})")


@cli.command("info")
def info():
    """Show active version metadata."""
    cfg = load_global()
    versions = cfg.get("active_versions", [])
    if not versions:
        click.echo("No active versions set. Run 'qrag ai active <version>' first.")
        sys.exit(1)
    for av in versions:
        version_cfg_path = CACHE_DIR / av / "config.json"
        click.echo(f"=== {av} ===")
        if version_cfg_path.exists():
            with open(version_cfg_path) as f:
                click.echo(json.dumps(json.load(f), indent=2))
        else:
            click.echo(f"No config.json found for version '{av}'.")


@cli.group("ai", invoke_without_command=False)
def ai():
    """Manage the AI harness (MCP server, skills, and future interfaces)."""
    pass


@ai.command("active")
@click.argument("versions", nargs=-1, required=False)
def ai_active(versions: tuple[str, ...]):
    """Show or set the active version(s).

    Pass one or more version names to replace the active list.
    Pass no arguments to show the current active versions.

    Examples:
      qrag ai active                    # show active versions
      qrag ai active my-sdk             # set one active version
      qrag ai active my-sdk my-rtos     # set multiple active versions
    """
    cfg = load_global()
    if not versions:
        current = cfg.get("active_versions", [])
        if current:
            click.echo("Active versions:")
            for v in current:
                click.echo(f"  {v}")
        else:
            click.echo("Active versions: (none)")
    else:
        missing = [v for v in versions if not (CACHE_DIR / v).exists()]
        if missing:
            for v in missing:
                click.echo(f"Error: version '{v}' not found in {CACHE_DIR}. Download it first.", err=True)
            sys.exit(1)
        cfg["active_versions"] = list(versions)
        save_global(cfg)
        click.echo(f"Active versions set to: {', '.join(versions)}")


@ai.command("setup")
@click.option("--ai", "agent", type=click.Choice(["gemini", "claude", "antigravity"]), help="AI tool to install for (required unless --global)")
@click.option("--global", "global_install", is_flag=True, help="Install AI harness system-wide for all projects (gemini, claude, and antigravity)")
@click.option("--mcp-only", is_flag=True, help="Install MCP server only (skip /qrag skill)")
@click.option("--skills-only", is_flag=True, help="Install /qrag skill only (skip MCP server)")
def ai_setup(agent: str | None, global_install: bool, mcp_only: bool, skills_only: bool):
    """Install the AI harness (MCP server + /qrag skill).

    Use --mcp-only or --skills-only to install just one component of the AI harness.
    """
    if mcp_only and skills_only:
        click.echo("Error: --mcp-only and --skills-only are mutually exclusive.", err=True)
        sys.exit(1)

    if not global_install and not agent:
        click.echo("Error: --ai=gemini|claude|antigravity is required (or use --global)", err=True)
        sys.exit(1)

    # Install MCP server (unless --skills-only)
    if not skills_only:
        if global_install:
            _mcp_install_global()
        else:
            _mcp_install(agent)

    # Install /qrag skill (unless --mcp-only)
    if not mcp_only:
        click.echo()
        if global_install:
            _skills_install_global()
        else:
            _skills_install(agent, global_install=False)


def _mcp_install(ai: str):
    """Install MCP server config for Gemini, Claude, or Antigravity."""
    import shutil
    import subprocess

    mcp_cmd = shutil.which("qrag-mcp-server")
    if not mcp_cmd:
        click.echo("Error: qrag-mcp-server not found in PATH", err=True)
        sys.exit(1)

    if ai == "gemini":
        try:
            result = subprocess.run(
                ["gemini", "mcp", "add", "qrag", mcp_cmd],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                click.echo("✓ Gemini MCP server 'qrag' registered")
                click.echo("  Run `gemini mcp list` to verify installation")
            else:
                click.echo("Error: Failed to register MCP server with Gemini", err=True)
                click.echo(f"  {result.stderr}", err=True)
                sys.exit(1)
        except FileNotFoundError:
            click.echo("Error: Gemini CLI not found in PATH", err=True)
            click.echo("  Make sure Gemini CLI is installed and in your PATH", err=True)
            sys.exit(1)

    elif ai == "claude":
        try:
            result = subprocess.run(
                ["claude", "mcp", "add", "qrag", mcp_cmd],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                click.echo("✓ Claude MCP server 'qrag' registered")
                click.echo("  Run `claude mcp list` to verify installation")
            else:
                click.echo("Error: Failed to register MCP server with Claude", err=True)
                click.echo(f"  {result.stderr}", err=True)
                sys.exit(1)
        except FileNotFoundError:
            click.echo("Error: Claude CLI not found in PATH", err=True)
            click.echo("  Make sure Claude Code is installed and in your PATH", err=True)
            sys.exit(1)

    elif ai == "antigravity":
        # Antigravity has no CLI registration command; config is written directly.
        config_file = Path.cwd() / ".agents" / "mcp_config.json"
        _write_mcp_config(mcp_cmd, config_file)
        click.echo("✓ Antigravity MCP server 'qrag' installed")
        click.echo(f"  Config: {config_file}")


def _detect_available_agents() -> list[str]:
    """Detect which CLI agents (gemini, claude, antigravity) are available."""
    import shutil

    available = []
    if shutil.which("gemini"):
        available.append("gemini")
    if shutil.which("claude"):
        available.append("claude")
    if shutil.which("agy"):
        available.append("antigravity")
    return available


def _write_mcp_config(mcp_cmd: str, config_file: Path) -> None:
    """Merge the qrag MCP server entry into an existing or new mcp_config.json."""
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config: dict = {}
    if config_file.exists():
        with open(config_file) as f:
            try:
                config = json.load(f)
            except json.JSONDecodeError:
                config = {}
    config.setdefault("mcpServers", {})["qrag"] = {"command": mcp_cmd, "args": []}
    with open(config_file, "w") as f:
        json.dump(config, f, indent=2)


def _mcp_install_global_config(mcp_cmd: str, agent: str) -> bool:
    """Install MCP server to global config for a specific agent."""
    if agent == "gemini":
        config_file = Path.home() / ".gemini" / "settings.json"
        config_file.parent.mkdir(parents=True, exist_ok=True)

        config = {}
        if config_file.exists():
            with open(config_file) as f:
                try:
                    config = json.load(f)
                except json.JSONDecodeError:
                    config = {}

        if "mcpServers" not in config:
            config["mcpServers"] = {}

        config["mcpServers"]["qrag"] = {
            "command": mcp_cmd,
            "args": [],
            "trust": True,
        }

        with open(config_file, "w") as f:
            json.dump(config, f, indent=2)

        click.echo("✓ Gemini MCP server 'qrag' installed")
        click.echo(f"  Config: {config_file}")
        return True

    elif agent == "claude":
        config_file = Path.home() / ".claude.json"

        config = {}
        if config_file.exists():
            with open(config_file) as f:
                try:
                    config = json.load(f)
                except json.JSONDecodeError:
                    config = {}

        if "mcpServers" not in config:
            config["mcpServers"] = {}

        config["mcpServers"]["qrag"] = {
            "command": mcp_cmd,
            "args": [],
        }

        with open(config_file, "w") as f:
            json.dump(config, f, indent=2)

        click.echo("✓ Claude MCP server 'qrag' installed")
        click.echo(f"  Config: {config_file}")
        return True

    elif agent == "antigravity":
        # Antigravity shares a central MCP config at ~/.gemini/config/mcp_config.json
        config_file = Path.home() / ".gemini" / "config" / "mcp_config.json"
        _write_mcp_config(mcp_cmd, config_file)
        click.echo("✓ Antigravity MCP server 'qrag' installed")
        click.echo(f"  Config: {config_file}")
        return True

    return False


def _mcp_install_global():
    """Install MCP server system-wide for all available agents."""
    import shutil

    mcp_cmd = shutil.which("qrag-mcp-server")
    if not mcp_cmd:
        click.echo("Error: qrag-mcp-server not found in PATH", err=True)
        sys.exit(1)

    available_agents = _detect_available_agents()
    if not available_agents:
        click.echo("Error: No CLI agents found (gemini, claude, or antigravity)", err=True)
        click.echo("Please install Gemini CLI, Claude Code, or Antigravity CLI first.", err=True)
        sys.exit(1)

    click.echo(f"Detected available agents: {', '.join(available_agents)}")
    click.echo()

    installed = []
    for agent in available_agents:
        if _mcp_install_global_config(mcp_cmd, agent):
            installed.append(agent)

    click.echo()
    if installed:
        agents_str = " and ".join(installed)
        click.echo(f"✓ MCP server is now available system-wide for {agents_str}!")
    else:
        click.echo("Warning: MCP server could not be installed to any agent.", err=True)


# ---------------------------------------------------------------------------
# install (top-level shortcut: auto-installs MCP for all detected agents)
# ---------------------------------------------------------------------------



def _skills_install(ai: str, global_install: bool) -> None:
    """Install /qrag skill for a specific agent."""
    if ai == "claude":
        if global_install:
            cmd_dir = Path.home() / ".claude" / "commands"
        else:
            cmd_dir = Path.cwd() / ".claude" / "commands"
        cmd_dir.mkdir(parents=True, exist_ok=True)
        skill_file = cmd_dir / "qrag.md"
        with open(skill_file, "w", encoding="utf-8") as f:
            f.write(_SKILL_CONTENT)

    elif ai == "gemini":
        if global_install:
            skill_dir = Path.home() / ".gemini" / "skills" / "qrag"
        else:
            skill_dir = Path.cwd() / ".gemini" / "skills" / "qrag"
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file = skill_dir / "SKILL.md"
        with open(skill_file, "w", encoding="utf-8") as f:
            f.write(_SKILL_CONTENT)

    elif ai == "antigravity":
        # Antigravity skills live in a subdirectory named after the skill,
        # with a required SKILL.md file inside.
        if global_install:
            skill_dir = Path.home() / ".gemini" / "antigravity-cli" / "skills" / "qrag"
        else:
            skill_dir = Path.cwd() / ".agents" / "skills" / "qrag"
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file = skill_dir / "SKILL.md"
        with open(skill_file, "w", encoding="utf-8") as f:
            f.write(_SKILL_CONTENT)

    else:
        click.echo(f"Error: Unknown AI tool '{ai}'", err=True)
        sys.exit(1)

    click.echo(f"✓ Installed /qrag skill for {ai}")
    click.echo(f"  File: {skill_file}")


def _skills_install_global() -> None:
    """Install /qrag skill for all detected agents (global)."""
    available_agents = _detect_available_agents()
    if not available_agents:
        click.echo("Error: No CLI agents found (gemini, claude, or antigravity)", err=True)
        click.echo("Please install Gemini CLI, Claude Code, or Antigravity CLI first.", err=True)
        sys.exit(1)

    click.echo(f"Detected available agents: {', '.join(available_agents)}")
    click.echo()

    installed = []
    for agent in available_agents:
        try:
            _skills_install(agent, global_install=True)
            installed.append(agent)
        except Exception as e:
            click.echo(f"Warning: Failed to install skill for {agent}: {e}", err=True)

    click.echo()
    if installed:
        agents_str = " and ".join(installed)
        click.echo(f"✓ /qrag skill is now available system-wide for {agents_str}!")
    else:
        click.echo("Warning: /qrag skill could not be installed to any agent.", err=True)


# ---------------------------------------------------------------------------
# BUILD — Create and manage indexes
# ---------------------------------------------------------------------------

def _ensure_build_deps() -> None:
    missing = []
    for module, pkg in [
        ("tree_sitter", "tree-sitter"),
        ("tree_sitter_language_pack", "tree-sitter-language-pack"),
        ("fitz", "pymupdf"),
    ]:
        try:
            __import__(module)
        except ImportError:
            missing.append(pkg)

    if not missing:
        return

    click.echo(
        f"Error: build dependencies not installed (missing: {', '.join(missing)}).\n"
        "\n"
        "Re-install qrag with the 'build' and 'cpu' extras:\n"
        "  uv tool install --reinstall 'qrag[build,cpu]'\n"
        "  pip install --upgrade 'qrag[build,cpu]'\n"
        "  pipenv install 'qrag[build,cpu]'\n"
        "  poetry add qrag --extras build --extras cpu\n"
        "\n"
        "For GPU-accelerated embedding, use 'gpu' instead of 'cpu':\n"
        "  uv tool install --reinstall 'qrag[build,gpu]'\n",
        err=True,
    )
    sys.exit(1)


def _sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def _dirs_hash(dirs: tuple[Path, ...]) -> str:
    import hashlib
    resolved = sorted(str(Path(d).resolve()) for d in dirs)
    return hashlib.sha256("\n".join(resolved).encode()).hexdigest()


_BUILD_STATE_FILE = ".qrag-build-state.json"


def _write_build_state(
    out_dir: Path,
    input_dirs: tuple[Path, ...],
    device: str,
    limit_cpu: int | None,
    batch_size: int,
    output: str,
    files_total: int,
) -> None:
    state = {
        "started_at": datetime.datetime.utcnow().isoformat() + "Z",
        "input_dirs": [str(Path(d).resolve()) for d in input_dirs],
        "input_dirs_hash": _dirs_hash(input_dirs),
        "device": device,
        "limit_cpu": limit_cpu,
        "batch_size": batch_size,
        "output": output,
        "files_total": files_total,
    }
    (out_dir / _BUILD_STATE_FILE).write_text(json.dumps(state, indent=2))


def _clear_build_state(out_dir: Path) -> None:
    (out_dir / _BUILD_STATE_FILE).unlink(missing_ok=True)


def _count_manifest_rows(out_dir: Path) -> int:
    from .database import load_manifest
    total = 0
    for db_name in ("code.db", "docs.db"):
        p = out_dir / db_name
        if p.exists():
            total += len(load_manifest(p))
    return total


@dataclass
class _FileRecord:
    path: str
    kind: str       # "code" or "doc"
    language: str
    chunks: int
    elapsed: float
    skipped: bool = False
    skip_reason: str = ""  # "zero_chunks" or "parse_error"


def _timed_chunk_code_file(abs_p: Path) -> tuple[list, float]:
    t0 = time.monotonic()
    from .chunker import chunk_code_file
    result = chunk_code_file(abs_p)
    return result, time.monotonic() - t0


def _timed_parse_doc_file(abs_p: Path) -> tuple[list, float]:
    t0 = time.monotonic()
    from .doc_parser import parse_doc_file
    result = parse_doc_file(abs_p)
    return result, time.monotonic() - t0


def _fmt_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.2f}s"
    m = int(seconds // 60)
    s = seconds % 60
    return f"{m}m {s:.0f}s"


def _detect_input_type(d: Path) -> tuple[list[Path], list[Path]]:
    """Return (code_files, doc_files) found under d.

    Code files: all source files and build system files from the language registry.
    Doc files: PDF and HTML.
    """
    from .chunker import SUPPORTED_EXTENSIONS, BUILD_FILENAMES

    seen: set[Path] = set()
    code: list[Path] = []

    def _add(p: Path) -> None:
        if p not in seen:
            seen.add(p)
            code.append(p)

    # Source and build files by extension
    for ext in SUPPORTED_EXTENSIONS:
        for p in sorted(d.rglob(f"*{ext}")):
            _add(p)

    # Build system files by exact name (CMakeLists.txt, Makefile, package.json, …)
    for fname in BUILD_FILENAMES:
        for p in sorted(d.rglob(fname)):
            _add(p)

    docs = (
        sorted(d.rglob("*.pdf"))
        + sorted(d.rglob("*.html"))
        + sorted(d.rglob("*.htm"))
    )
    return sorted(code), docs


_QUEUE_MAXSIZE = 4096
_CHECKPOINT_SIZE = 1000
_KIND_CODE = "code"
_KIND_DOC = "doc"
_KIND_ERROR = "error"


def _run_code_producer(
    to_process: dict,
    workers: int | None,
    q: queue.Queue,
    errors: list,
) -> None:
    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_timed_chunk_code_file, abs_p): (root, rel)
            for (root, rel), abs_p in to_process.items()
        }
        for future in as_completed(futures):
            root, rel = futures[future]
            try:
                chunks, elapsed = future.result()
                q.put((_KIND_CODE, chunks, elapsed, root, rel))
            except Exception as e:
                errors.append((str(Path(root) / rel), str(e)))
                q.put((_KIND_ERROR, str(e), 0.0, root, rel))
    q.put(None)  # sentinel


def _run_doc_producer(
    to_process_docs: dict,
    workers: int | None,
    q: queue.Queue,
    errors: list,
) -> None:
    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_timed_parse_doc_file, abs_p): (root, rel)
            for (root, rel), abs_p in to_process_docs.items()
        }
        for future in as_completed(futures):
            root, rel = futures[future]
            try:
                sections, elapsed = future.result()
                q.put((_KIND_DOC, sections, elapsed, root, rel))
            except Exception as e:
                errors.append((str(Path(root) / rel), str(e)))
                q.put((_KIND_ERROR, str(e), 0.0, root, rel))
    q.put(None)  # sentinel


def _flush_code_batch(
    pending: list,
    db_path: Path,
    device: str,
    precision: str,
    batch_size: int,
) -> int:
    from .database import insert_code_chunks_batch
    from .embedder import embed
    stored = 0
    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        embeddings = embed([c.code_text for c in batch], device=device, precision=precision)
        insert_code_chunks_batch(db_path, batch, embeddings)
        stored += len(batch)
    return stored


def _flush_doc_batch(
    pending: list,
    ddb_path: Path,
    device: str,
    precision: str,
    batch_size: int,
) -> int:
    from .database import insert_doc_sections_batch
    from .embedder import embed
    stored = 0
    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        embeddings = embed([s.content for s in batch], device=device, precision=precision)
        insert_doc_sections_batch(ddb_path, batch, embeddings)
        stored += len(batch)
    return stored


def _consume_and_embed(
    q: queue.Queue,
    num_producers: int,
    db_path: Path | None,
    ddb_path: Path | None,
    device: str,
    precision: str,
    batch_size: int,
    *,
    layout=None,
    to_process: dict | None = None,
    to_process_docs: dict | None = None,
    total_files: int = 0,
) -> tuple[int, int, set, set, list]:
    """Drain the producer queue, embed, and write to DB with periodic checkpointing.

    Returns (code_stored, docs_stored, successful_code, successful_docs, file_records).
    layout is a BuildLayout instance (or None in --verbose mode).
    """
    pending_code: list = []
    pending_doc: list = []
    successful_code: set[tuple[str, str]] = set()
    successful_docs: set[tuple[str, str]] = set()
    code_stored = 0
    docs_stored = 0
    sentinels_seen = 0
    file_records: list[_FileRecord] = []
    chunks_embedded = 0

    # GH#28: incremental manifest tracking
    _code_recv = 0
    _code_flushed = 0
    _manifest_code: list[tuple[str, str, int]] = []
    _doc_recv = 0
    _doc_flushed = 0
    _manifest_docs: list[tuple[str, str, int]] = []

    if to_process and db_path:
        from .database import upsert_manifest_rows_batch as _upsert_code_manifest
    else:
        _upsert_code_manifest = None  # type: ignore[assignment]
    if to_process_docs and ddb_path:
        from .database import upsert_manifest_rows_batch as _upsert_doc_manifest
    else:
        _upsert_doc_manifest = None  # type: ignore[assignment]

    def _flush_code_manifest() -> None:
        nonlocal _manifest_code
        if not _upsert_code_manifest or not _manifest_code:
            return
        ready = [(r, rp) for (r, rp, ei) in _manifest_code if ei <= _code_flushed]
        _manifest_code = [(r, rp, ei) for (r, rp, ei) in _manifest_code if ei > _code_flushed]
        if ready:
            _upsert_code_manifest(db_path, [  # type: ignore[arg-type]
                (rp, r, to_process[(r, rp)].stat().st_mtime, _sha256(to_process[(r, rp)]))  # type: ignore[index]
                for r, rp in ready
            ])

    def _flush_doc_manifest() -> None:
        nonlocal _manifest_docs
        if not _upsert_doc_manifest or not _manifest_docs:
            return
        ready = [(r, rp) for (r, rp, ei) in _manifest_docs if ei <= _doc_flushed]
        _manifest_docs = [(r, rp, ei) for (r, rp, ei) in _manifest_docs if ei > _doc_flushed]
        if ready:
            _upsert_doc_manifest(ddb_path, [  # type: ignore[arg-type]
                (rp, r, to_process_docs[(r, rp)].stat().st_mtime, _sha256(to_process_docs[(r, rp)]))  # type: ignore[index]
                for r, rp in ready
            ])

    def _emit_embed(n: int, elapsed: float) -> None:
        nonlocal chunks_embedded
        chunks_embedded += n
        if layout is not None:
            avg_ch = sum(r.chunks for r in file_records) / max(1, len(file_records))
            layout.on_embed_batch(n, elapsed, chunks_embedded, avg_ch)

    while sentinels_seen < num_producers:
        try:
            item = q.get(timeout=0.05)
        except queue.Empty:
            continue

        if item is None:
            sentinels_seen += 1
            continue

        kind, payload, elapsed, root, rel = item
        abs_path = str(Path(root) / rel)

        if kind == _KIND_ERROR:
            if layout is not None:
                layout.on_error(abs_path, root, payload)
            continue

        skipped = not payload
        skip_reason = "zero_chunks" if skipped else ""

        if kind == _KIND_CODE:
            pending_code.extend(payload)
            successful_code.add((root, rel))
            lang = payload[0].language if payload else "unknown"
            fr = _FileRecord(
                path=abs_path, kind="code", language=lang,
                chunks=len(payload), elapsed=elapsed,
                skipped=skipped, skip_reason=skip_reason,
            )
            if _upsert_code_manifest:
                if payload:
                    _code_recv += len(payload)
                    _manifest_code.append((root, rel, _code_recv))
                else:
                    abs_p = to_process[(root, rel)]  # type: ignore[index]
                    _upsert_code_manifest(db_path, [(rel, root, abs_p.stat().st_mtime, _sha256(abs_p))])  # type: ignore[arg-type]
        else:
            pending_doc.extend(payload)
            successful_docs.add((root, rel))
            ext = Path(rel).suffix.lower().lstrip(".")
            lang = ext if ext in ("pdf", "html", "htm") else "doc"
            fr = _FileRecord(
                path=abs_path, kind="doc", language=lang,
                chunks=len(payload), elapsed=elapsed,
                skipped=skipped, skip_reason=skip_reason,
            )
            if _upsert_doc_manifest:
                if payload:
                    _doc_recv += len(payload)
                    _manifest_docs.append((root, rel, _doc_recv))
                else:
                    abs_p = to_process_docs[(root, rel)]  # type: ignore[index]
                    _upsert_doc_manifest(ddb_path, [(rel, root, abs_p.stat().st_mtime, _sha256(abs_p))])  # type: ignore[arg-type]

        file_records.append(fr)
        if layout is not None:
            layout.on_file_parsed(abs_path, root, fr.chunks, elapsed, skipped, skip_reason)

        if db_path and len(pending_code) >= _CHECKPOINT_SIZE:
            batch, pending_code = pending_code[:_CHECKPOINT_SIZE], pending_code[_CHECKPOINT_SIZE:]
            t0 = time.monotonic()
            n = _flush_code_batch(batch, db_path, device, precision, batch_size)
            code_stored += n
            _code_flushed += len(batch)
            _flush_code_manifest()
            _emit_embed(n, time.monotonic() - t0)

        if ddb_path and len(pending_doc) >= _CHECKPOINT_SIZE:
            batch, pending_doc = pending_doc[:_CHECKPOINT_SIZE], pending_doc[_CHECKPOINT_SIZE:]
            t0 = time.monotonic()
            n = _flush_doc_batch(batch, ddb_path, device, precision, batch_size)
            docs_stored += n
            _doc_flushed += len(batch)
            _flush_doc_manifest()
            _emit_embed(n, time.monotonic() - t0)

    # Drain remainders
    if db_path and pending_code:
        t0 = time.monotonic()
        n = _flush_code_batch(pending_code, db_path, device, precision, batch_size)
        code_stored += n
        _code_flushed += len(pending_code)
        _flush_code_manifest()
        _emit_embed(n, time.monotonic() - t0)
    if ddb_path and pending_doc:
        t0 = time.monotonic()
        n = _flush_doc_batch(pending_doc, ddb_path, device, precision, batch_size)
        docs_stored += n
        _doc_flushed += len(pending_doc)
        _flush_doc_manifest()
        _emit_embed(n, time.monotonic() - t0)

    return code_stored, docs_stored, successful_code, successful_docs, file_records


def _write_build_report(
    out_dir: Path,
    file_records: list,
    parse_errors: list,
    total_elapsed: float,
    code_stored: int,
    docs_stored: int,
    db_path: Path | None,
    ddb_path: Path | None,
) -> Path:
    report_path = out_dir / "build-report.txt"

    code_records = [r for r in file_records if r.kind == "code"]
    doc_records = [r for r in file_records if r.kind == "doc"]
    code_skipped = [r for r in code_records if r.skipped]
    doc_skipped = [r for r in doc_records if r.skipped]

    lang_stats: dict[str, dict] = {}
    for r in file_records:
        s = lang_stats.setdefault(r.language, {"files": 0, "chunks": 0, "elapsed": 0.0})
        s["files"] += 1
        s["chunks"] += r.chunks
        s["elapsed"] += r.elapsed

    lines: list[str] = [
        "qrag build report",
        f"Generated : {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Output    : {out_dir.name}",
        "",
        "=" * 72,
        "SUMMARY",
        "=" * 72,
        f"Status                 : {'FAILED' if parse_errors else 'SUCCESS'}",
        f"Files failed           : {len(parse_errors)}",
        f"Total wall-clock time  : {_fmt_elapsed(total_elapsed)}",
        f"Code files processed   : {len(code_records)} ({len(code_skipped)} skipped)",
        f"Doc files processed    : {len(doc_records)} ({len(doc_skipped)} skipped)",
        f"Code chunks stored     : {code_stored:,}",
        f"Doc sections stored    : {docs_stored:,}",
    ]
    if db_path and db_path.exists():
        lines.append(f"code.db size           : {db_path.stat().st_size / 1_048_576:.1f} MB")
    if ddb_path and ddb_path.exists():
        lines.append(f"docs.db size           : {ddb_path.stat().st_size / 1_048_576:.1f} MB")
    lines.append("")

    lines += [
        "=" * 72,
        "BY LANGUAGE",
        "=" * 72,
        f"{'Language':<20} {'Files':>6} {'Chunks':>8} {'Avg/file':>10} {'Time':>10}",
        "-" * 58,
    ]
    for lang, s in sorted(lang_stats.items(), key=lambda x: x[1]["chunks"], reverse=True):
        avg = s["chunks"] / max(1, s["files"])
        lines.append(
            f"{lang:<20} {s['files']:>6} {s['chunks']:>8,} {avg:>10.1f} {_fmt_elapsed(s['elapsed']):>10}"
        )
    lines.append("")

    if code_records:
        lines += [
            "=" * 72,
            "CODE FILES",
            "=" * 72,
            f"{'Path':<60} {'Language':<14} {'Chunks':>6} {'Time':>8}",
            "-" * 92,
        ]
        for r in sorted(code_records, key=lambda x: x.path):
            flag = "  [zero chunks]" if r.skipped else ""
            lines.append(
                f"{r.path:<60} {r.language:<14} {r.chunks:>6} {_fmt_elapsed(r.elapsed):>8}{flag}"
            )
        lines.append("")

    if doc_records:
        lines += [
            "=" * 72,
            "DOC FILES",
            "=" * 72,
            f"{'Path':<60} {'Type':<8} {'Sections':>8} {'Time':>8}",
            "-" * 88,
        ]
        for r in sorted(doc_records, key=lambda x: x.path):
            flag = "  [zero sections]" if r.skipped else ""
            lines.append(
                f"{r.path:<60} {r.language:<8} {r.chunks:>8} {_fmt_elapsed(r.elapsed):>8}{flag}"
            )
        lines.append("")

    all_skipped = [(r.path, r.skip_reason) for r in file_records if r.skipped]
    all_skipped += [(p, "parse_error") for p, _ in parse_errors]
    if all_skipped:
        lines += [
            "=" * 72,
            "SKIPPED FILES",
            "=" * 72,
            f"{'Reason':<15} Path",
            "-" * 72,
        ]
        for path, reason in sorted(all_skipped):
            lines.append(f"{reason:<15} {path}")
        lines.append("")

    if parse_errors:
        lines += ["=" * 72, "PROCESSING ERRORS", "=" * 72]
        for path, message in parse_errors:
            lines.append(f"{path}: {message}")
        lines.append("")

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


@cli.command()
@click.option("-i", "--input", "input_dirs", multiple=True, required=True,
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Input directory (code and/or docs); repeatable")
@click.option("-o", "--output", required=True, help="Database name, e.g. my-project")
@click.option("--device", default="auto", show_default=True,
              type=click.Choice(["auto", "cpu", "cuda"]),
              help="Embedding device: auto detects CUDA and falls back to CPU")
@click.option("--limit-cpu", default=None, type=int,
              help="Max CPU cores for parallel chunking (default: all available cores)")
@click.option("--batch-size", "batch_size", default=None, type=int,
              help="Embedding batch size (default: 256 for CPU, 1024 for CUDA)")
@click.option("--force", is_flag=True,
              help="Force full rebuild, ignoring incremental state")
@click.option("--yes", "-y", "yes_flag", is_flag=True,
              help="Skip confirmation prompt when --force would delete existing databases")
@click.option("--no-resume", "no_resume", is_flag=True,
              help="Ignore any interrupted build state and start fresh")
def build(input_dirs: tuple[Path, ...], output: str, device: str, limit_cpu: int | None, batch_size: int | None, force: bool, yes_flag: bool, no_resume: bool):
    """Parse, embed, and store code and/or docs into a named database.

    Each -i directory is scanned automatically: source files and build system
    files (C, C++, Rust, Python, Go, JS/TS, Java, CMake, Makefile, Cargo.toml,
    and many more) go into code.db; .pdf/.html/.htm files go into docs.db.
    Pass -i multiple times to combine directories.

    On re-run, only changed files are re-embedded. Use --force to rebuild
    everything from scratch.
    """
    _ensure_build_deps()
    from .embedder import default_batch_size, resolve_device

    if limit_cpu is not None and limit_cpu > (os.cpu_count() or 1):
        click.echo(
            f"Error: --limit-cpu={limit_cpu} exceeds available cores ({os.cpu_count()}).",
            err=True,
        )
        sys.exit(1)

    try:
        resolved_device = resolve_device(device)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    if batch_size is None:
        batch_size = default_batch_size(resolved_device)

    precision = "float32"

    click.echo(f"[build] device={resolved_device}  batch-size={batch_size}  precision={precision}")

    out_dir = CACHE_DIR / output
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── GH#31: detect interrupted build ───────────────────────────────────
    if not force and (out_dir / _BUILD_STATE_FILE).exists():
        try:
            _state = json.loads((out_dir / _BUILD_STATE_FILE).read_text())
        except Exception:
            _state = {}
        _files_done = _count_manifest_rows(out_dir)
        _files_total = _state.get("files_total", 0)
        _started_raw = _state.get("started_at", "")
        _started_fmt = _started_raw[:16].replace("T", " ") if len(_started_raw) >= 16 else _started_raw
        _pct = f"{_files_done / max(1, _files_total):.0%}"
        click.echo(
            f"⚡ Interrupted build detected "
            f"(started {_started_fmt}, {_files_done}/{_files_total} files done [{_pct}]):"
        )
        _saved_dirs = _state.get("input_dirs", [])
        if _saved_dirs:
            click.echo("  " + "  ".join(f"-i {d}" for d in _saved_dirs))
        _saved_hash = _state.get("input_dirs_hash", "")
        if _saved_hash and _dirs_hash(input_dirs) != _saved_hash:
            click.echo("  Note: -i dirs differ from the interrupted run — proceeding with current dirs.")
        if no_resume:
            click.echo("[build] --no-resume: discarding interrupted state, starting fresh.")
            _clear_build_state(out_dir)
        elif not sys.stdin.isatty():
            click.echo("[build] Non-interactive mode: auto-resuming.")
        else:
            if not click.confirm("Resume?", default=True):
                click.echo("[build] Discarding interrupted state, starting fresh.")
                _clear_build_state(out_dir)

    # ── GH#29: clear interrupted state on --force ─────────────────────────
    if force:
        _clear_build_state(out_dir)

    # Group files by their input directory (needed for manifest rel_path computation)
    code_by_dir: dict[Path, list[Path]] = {}
    doc_by_dir: dict[Path, list[Path]] = {}

    for d in input_dirs:
        code_files, doc_files = _detect_input_type(d)
        if code_files:
            exts = sorted({f.suffix.lower() or f.name for f in code_files})
            ext_str = "/".join(exts[:6]) + ("…" if len(exts) > 6 else "")
            click.echo(f"[code] {d} — {len(code_files)} file(s) [{ext_str}]")
            code_by_dir[d] = code_files
        if doc_files:
            click.echo(f"[docs] {d} — {len(doc_files)} .pdf/.html file(s)")
            doc_by_dir[d] = doc_files
        if not code_files and not doc_files:
            click.echo(f"[warn] {d} — no supported source, build, or doc files found, skipping")

    all_code_files = [f for files in code_by_dir.values() for f in files]
    all_doc_files = [f for files in doc_by_dir.values() for f in files]

    if not all_code_files and not all_doc_files:
        click.echo("No supported source, build, or doc files found in any input directory.", err=True)
        sys.exit(1)

    _logger.info("build: %d code file(s), %d doc file(s)", len(all_code_files), len(all_doc_files))

    # ── GH#29: warn and confirm before --force deletes existing databases ───
    if force:
        _dbs_to_delete: list[Path] = []
        if all_code_files and (out_dir / "code.db").exists():
            _dbs_to_delete.append(out_dir / "code.db")
        if all_doc_files and (out_dir / "docs.db").exists():
            _dbs_to_delete.append(out_dir / "docs.db")
        if _dbs_to_delete:
            from .database import load_manifest as _load_manifest_for_warn
            click.echo("Warning: --force will permanently delete:")
            for _p in _dbs_to_delete:
                _size_mb = _p.stat().st_size / 1_048_576
                _mtime = datetime.datetime.fromtimestamp(_p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                _nfiles = len(_load_manifest_for_warn(_p))
                click.echo(f"  {_p}  ({_size_mb:.1f} MB, {_nfiles} files indexed, last built {_mtime})")
            if not yes_flag:
                if not sys.stdin.isatty():
                    click.echo(
                        "Error: --force with existing database(s) requires --yes in non-interactive mode.",
                        err=True,
                    )
                    sys.exit(1)
                if not click.confirm("Delete these databases and rebuild from scratch?", default=False):
                    click.echo("Aborted.")
                    sys.exit(0)

    db_path: Path | None = None
    ddb_path: Path | None = None
    to_process: dict[tuple[str, str], Path] = {}
    to_process_docs: dict[tuple[str, str], Path] = {}
    code_changed = False
    docs_changed = False

    # ── Code delta computation ─────────────────────────────────────────────
    if all_code_files:
        from .database import (
            delete_chunks_for_file, init_code_db,
            load_manifest, upsert_manifest_row, delete_manifest_row,
        )

        db_path = out_dir / "code.db"
        if force and db_path.exists():
            db_path.unlink()
        init_code_db(db_path)

        walk: dict[tuple[str, str], float] = {}
        for d, files in code_by_dir.items():
            root = str(d.resolve())
            for f in files:
                walk[(root, str(f.relative_to(d)))] = f.stat().st_mtime

        manifest = load_manifest(db_path)

        # GH#32: dropped roots — warn and clean up their chunks
        if manifest and not force:
            stored_roots = {r for (r, _) in manifest}
            current_roots = {root for d, files in code_by_dir.items() for root in [str(d.resolve())]}
            dropped_roots = stored_roots - current_roots
            if dropped_roots:
                for dr in sorted(dropped_roots):
                    n_drop = sum(1 for (r, _) in manifest if r == dr)
                    click.echo(f"[build] Removing {n_drop} file(s) from dropped root: {dr}")

        for (root, rel) in set(manifest) - set(walk):
            delete_chunks_for_file(db_path, str(Path(root) / rel))
            delete_manifest_row(db_path, rel, root)
            code_changed = True

        for (root, rel) in set(walk) - set(manifest):
            to_process[(root, rel)] = Path(root) / rel

        for (root, rel) in set(walk) & set(manifest):
            curr_mtime = walk[(root, rel)]
            stored_mtime, stored_sha = manifest[(root, rel)]
            if curr_mtime != stored_mtime:
                abs_p = Path(root) / rel
                if _sha256(abs_p) != stored_sha:
                    delete_chunks_for_file(db_path, str(abs_p))
                    to_process[(root, rel)] = abs_p
                else:
                    upsert_manifest_row(db_path, rel, root, curr_mtime, stored_sha)

        if to_process:
            code_changed = True

    # ── Docs delta computation ─────────────────────────────────────────────
    if all_doc_files:
        from .database import (
            delete_sections_for_source, init_docs_db,
            load_manifest, upsert_manifest_row, delete_manifest_row,
        )

        ddb_path = out_dir / "docs.db"
        if force and ddb_path.exists():
            ddb_path.unlink()
        init_docs_db(ddb_path)

        walk_docs: dict[tuple[str, str], float] = {}
        for d, files in doc_by_dir.items():
            root = str(d.resolve())
            for f in files:
                walk_docs[(root, str(f.relative_to(d)))] = f.stat().st_mtime

        manifest_docs = load_manifest(ddb_path)

        # GH#32: dropped roots — warn and clean up their sections
        if manifest_docs and not force:
            stored_roots = {r for (r, _) in manifest_docs}
            current_roots = {root for d, files in doc_by_dir.items() for root in [str(d.resolve())]}
            dropped_roots = stored_roots - current_roots
            if dropped_roots:
                for dr in sorted(dropped_roots):
                    n_drop = sum(1 for (r, _) in manifest_docs if r == dr)
                    click.echo(f"[build] Removing {n_drop} doc file(s) from dropped root: {dr}")

        for (root, rel) in set(manifest_docs) - set(walk_docs):
            delete_sections_for_source(ddb_path, str(Path(root) / rel))
            delete_manifest_row(ddb_path, rel, root)
            docs_changed = True

        for (root, rel) in set(walk_docs) - set(manifest_docs):
            to_process_docs[(root, rel)] = Path(root) / rel

        for (root, rel) in set(walk_docs) & set(manifest_docs):
            curr_mtime = walk_docs[(root, rel)]
            stored_mtime, stored_sha = manifest_docs[(root, rel)]
            if curr_mtime != stored_mtime:
                abs_p = Path(root) / rel
                if _sha256(abs_p) != stored_sha:
                    delete_sections_for_source(ddb_path, str(abs_p))
                    to_process_docs[(root, rel)] = abs_p
                else:
                    upsert_manifest_row(ddb_path, rel, root, curr_mtime, stored_sha)

        if to_process_docs:
            docs_changed = True

    # ── GH#31: write state file so an interruption can be detected next run ─
    _files_remaining = len(to_process) + len(to_process_docs)
    if _files_remaining > 0:
        _write_build_state(out_dir, input_dirs, resolved_device, limit_cpu, batch_size or 0, output, _files_remaining)

    # ── Concurrent parse → embed → checkpoint ─────────────────────────────
    num_producers = (1 if to_process else 0) + (1 if to_process_docs else 0)
    t_build_start = time.monotonic()
    file_records: list[_FileRecord] = []

    if num_producers > 0:
        total_files = len(to_process) + len(to_process_docs)
        q: queue.Queue = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        producer_errors: list[tuple[str, str]] = []
        threads = []

        # Proportional CPU split — avoids over-subscribing with 2× cpu_count processes
        _total_workers = limit_cpu or os.cpu_count() or 1
        if to_process and to_process_docs:
            _code_ratio = len(to_process) / (len(to_process) + len(to_process_docs))
            code_workers = max(1, round(_total_workers * _code_ratio))
            doc_workers = max(1, _total_workers - code_workers)
        elif to_process:
            code_workers, doc_workers = _total_workers, 0
        else:
            code_workers, doc_workers = 0, _total_workers

        if to_process:
            t = threading.Thread(
                target=_run_code_producer,
                args=(to_process, code_workers, q, producer_errors),
                daemon=True,
            )
            t.start()
            threads.append(t)

        if to_process_docs:
            t = threading.Thread(
                target=_run_doc_producer,
                args=(to_process_docs, doc_workers, q, producer_errors),
                daemon=True,
            )
            t.start()
            threads.append(t)

        if not _verbose:
            from .tui import BuildLayout
            with BuildLayout(total_files, out_dir, code_workers, doc_workers) as layout:
                code_stored, docs_stored, successful_code, successful_docs, file_records = _consume_and_embed(
                    q, num_producers, db_path, ddb_path, resolved_device, precision, batch_size,
                    layout=layout, to_process=to_process, to_process_docs=to_process_docs,
                    total_files=total_files,
                )
                for t in threads:
                    t.join()
        else:
            click.echo(
                f"[build] parsing {len(to_process)} code file(s) + "
                f"{len(to_process_docs)} doc file(s) concurrently "
                f"(code_workers={code_workers}, doc_workers={doc_workers})..."
            )
            code_stored, docs_stored, successful_code, successful_docs, file_records = _consume_and_embed(
                q, num_producers, db_path, ddb_path, resolved_device, precision, batch_size,
                layout=None, to_process=to_process, to_process_docs=to_process_docs,
                total_files=total_files,
            )
            for t in threads:
                t.join()

        for path, msg in producer_errors:
            click.echo(f"  Warning: {path}: {msg}", err=True)
            _logger.error("producer error: %s: %s", path, msg)

        # GH#28: manifest rows are now written incrementally inside _consume_and_embed.
        # Log counts for diagnostics only.
        if successful_code:
            _logger.info("build: stored %d code chunks in %s", code_stored, db_path)
        if successful_docs:
            _logger.info("build: stored %d doc sections in %s", docs_stored, ddb_path)

        # IS2 — build report
        total_elapsed = time.monotonic() - t_build_start
        report_path = _write_build_report(
            out_dir, file_records, producer_errors, total_elapsed,
            code_stored, docs_stored, db_path, ddb_path,
        )

        if producer_errors:
            click.echo(
                f"[build] FAILED: {len(producer_errors)} file(s) failed to process.",
                err=True,
            )
            click.echo(f"  Build report: {report_path}", err=True)
            sys.exit(1)

        # IS1 — uv-style final summary (bars already collapsed via transient=True)
        parts = []
        if code_stored:
            parts.append(f"{code_stored:,} code chunks")
        if docs_stored:
            parts.append(f"{docs_stored:,} doc sections")
        click.echo(f"✓ {' + '.join(parts) if parts else 'nothing new'} in {_fmt_elapsed(total_elapsed)}")
        click.echo(f"  Build report: {report_path}")

    if not code_changed:
        click.echo("[build] nothing changed (code)")
    if not docs_changed:
        click.echo("[build] nothing changed (docs)")

    version_cfg = {"embedding_model": "all-MiniLM-L6-v2"}
    with open(out_dir / "config.json", "w") as f:
        json.dump(version_cfg, f, indent=2)

    add_active_version(output)
    click.echo(f"Version '{output}' added to active versions.")
    _clear_build_state(out_dir)  # GH#31: clean completion — no interrupted state


# ---------------------------------------------------------------------------
# SEARCH — Query indexes locally
# ---------------------------------------------------------------------------

class _SearchGroup(click.Group):
    """A Click Group that routes unknown leading tokens to the group callback.

    Click puts the first non-option token in ``_protected_args`` and tries to
    resolve it as a subcommand, raising UsageError for unrecognised tokens.
    This subclass moves unrecognised first tokens back into ``ctx.args`` so
    Click's ``invoke_without_command`` path runs the group callback instead.
    Registered subcommands (``code``, ``docs``, ``symbol``) route normally.
    """

    def invoke(self, ctx: click.Context) -> object:
        all_args = [*ctx._protected_args, *ctx.args]
        if all_args and all_args[0] not in self.commands:
            ctx.args = all_args
            ctx._protected_args = []
        return super().invoke(ctx)


@cli.group("search", cls=_SearchGroup, invoke_without_command=True,
           context_settings={"allow_extra_args": True, "allow_interspersed_args": False})
@click.option("--top-k", default=5, show_default=True, help="Number of results to return")
@click.pass_context
def search(ctx, top_k: int):
    """Search code and/or docs. Without a subcommand, searches all three."""
    if ctx.invoked_subcommand is not None:
        return

    if not ctx.args:
        click.echo(ctx.get_help())
        sys.exit(0)

    query = " ".join(ctx.args)

    # Search all three: code, docs, symbol (if exact match)
    from .database import search_code as db_search_code, search_docs as db_search_docs, get_symbol as db_get_symbol
    from .embedder import embed_one

    found_any = False
    with _spinner("Searching…"):
        q_emb = embed_one(query)

    # Try exact symbol match first across all active code DBs
    for code_db in code_db_paths():
        if code_db.exists():
            result = db_get_symbol(code_db, query)
            if result is not None:
                click.echo(f"\n[SYMBOL] {result['symbol_name']}  ({result['type']})")
                click.echo(f"File   : {result['file_path']}:{result['line_start']}-{result['line_end']}")
                click.echo(f"\n{result['code_text']}")
                found_any = True
                break

    # Search code across all active code DBs
    all_code: list[dict] = []
    for code_db in code_db_paths():
        if code_db.exists():
            all_code.extend(db_search_code(code_db, q_emb, top_k=top_k))
    all_code.sort(key=lambda r: r.get("similarity_score", 0), reverse=True)
    seen_code: set[tuple] = set()
    merged_code = []
    for r in all_code:
        key = (r.get("file_path"), r.get("line_start"))
        if key not in seen_code:
            seen_code.add(key)
            merged_code.append(r)
        if len(merged_code) >= top_k:
            break
    if merged_code:
        if found_any:
            click.echo("\n" + "="*70)
        click.echo("\n[CODE]")
        for i, r in enumerate(merged_code, 1):
            click.echo(
                f"[{i}] {r['symbol_name']}  ({r['type']})  score={r['similarity_score']}\n"
                f"    {r['file_path']}:{r['line_start']}-{r['line_end']}"
            )
        found_any = True

    # Search docs across all active docs DBs
    all_docs: list[dict] = []
    for docs_db in docs_db_paths():
        if docs_db.exists():
            all_docs.extend(db_search_docs(docs_db, q_emb, top_k=top_k))
    all_docs.sort(key=lambda r: r.get("similarity_score", 0), reverse=True)
    seen_docs: set[tuple] = set()
    merged_docs = []
    for r in all_docs:
        key = (r.get("source_path"), r.get("page_range"))
        if key not in seen_docs:
            seen_docs.add(key)
            merged_docs.append(r)
        if len(merged_docs) >= top_k:
            break
    if merged_docs:
        if found_any:
            click.echo("\n" + "="*70)
        click.echo("\n[DOCS]")
        for i, r in enumerate(merged_docs, 1):
            tags = ", ".join(r["feature_tags"]) if r["feature_tags"] else ""
            page = f"  p.{r['page_range']}" if r["page_range"] else ""
            click.echo(
                f"[{i}] {r['title']}  score={r['similarity_score']}{page}\n"
                f"    Tags: {tags}"
            )
        found_any = True

    if not found_any:
        click.echo("No results found in code or docs.")
        sys.exit(1)


@search.command("code")
@click.argument("query")
@click.option("--top-k", default=5, show_default=True, help="Number of results to return")
def search_code(query: str, top_k: int):
    """Semantic search over indexed code chunks."""
    from .database import search_code as db_search
    from .embedder import embed_one

    dbs = [p for p in code_db_paths() if p.exists()]
    if not dbs:
        click.echo("No active code databases. Run `qrag ai active <version>` first.", err=True)
        sys.exit(1)

    with _spinner("Searching…"):
        q_emb = embed_one(query)
    all_results: list[dict] = []
    for db in dbs:
        _logger.debug("search-code: query=%r top_k=%d db=%s", query, top_k, db)
        all_results.extend(db_search(db, q_emb, top_k=top_k))

    all_results.sort(key=lambda r: r.get("similarity_score", 0), reverse=True)
    seen: set[tuple] = set()
    results = []
    for r in all_results:
        key = (r.get("file_path"), r.get("line_start"))
        if key not in seen:
            seen.add(key)
            results.append(r)
        if len(results) >= top_k:
            break

    _logger.info("search-code: %d result(s) for query=%r across %d db(s)", len(results), query, len(dbs))

    if not results:
        click.echo("No results found.")
        return

    for i, r in enumerate(results, 1):
        click.echo(
            f"\n[{i}] {r['symbol_name']}  ({r['type']})  score={r['similarity_score']}\n"
            f"    {r['file_path']}:{r['line_start']}-{r['line_end']}\n"
            f"    {r['code_snippet'][:120].replace(chr(10), ' ')}"
        )



@search.command("docs")
@click.argument("query")
@click.option("--top-k", default=5, show_default=True, help="Number of results to return")
def search_docs(query: str, top_k: int):
    """Semantic search over indexed documentation sections."""
    from .database import search_docs as db_search
    from .embedder import embed_one

    dbs = [p for p in docs_db_paths() if p.exists()]
    if not dbs:
        click.echo("No active docs databases. Run `qrag ai active <version>` first.", err=True)
        sys.exit(1)

    with _spinner("Searching…"):
        q_emb = embed_one(query)
    all_results: list[dict] = []
    for db in dbs:
        _logger.debug("search-docs: query=%r top_k=%d db=%s", query, top_k, db)
        all_results.extend(db_search(db, q_emb, top_k=top_k))

    all_results.sort(key=lambda r: r.get("similarity_score", 0), reverse=True)
    seen: set[tuple] = set()
    results = []
    for r in all_results:
        key = (r.get("source_path"), r.get("page_range"))
        if key not in seen:
            seen.add(key)
            results.append(r)
        if len(results) >= top_k:
            break

    _logger.info("search-docs: %d result(s) for query=%r across %d db(s)", len(results), query, len(dbs))

    if not results:
        click.echo("No results found.")
        return

    for i, r in enumerate(results, 1):
        tags = ", ".join(r["feature_tags"]) if r["feature_tags"] else ""
        page = f"  p.{r['page_range']}" if r["page_range"] else ""
        click.echo(
            f"\n[{i}] {r['title']}  score={r['similarity_score']}{page}\n"
            f"    Tags: {tags}\n"
            f"    {r['content'][:160].replace(chr(10), ' ')}"
        )



@search.command("symbol")
@click.argument("name")
def search_symbol(name: str):
    """Look up exact symbol definition by name."""
    from .database import get_symbol as db_get_symbol

    dbs = [p for p in code_db_paths() if p.exists()]
    if not dbs:
        click.echo("No active code databases. Run `qrag ai active <version>` first.", err=True)
        sys.exit(1)

    result = None
    for db in dbs:
        result = db_get_symbol(db, name)
        if result is not None:
            break
    if result is None:
        click.echo(f"Symbol '{name}' not found.")
        sys.exit(1)

    click.echo(
        f"Symbol : {result['symbol_name']}  ({result['type']})\n"
        f"File   : {result['file_path']}:{result['line_start']}-{result['line_end']}\n"
        f"\n{result['code_text']}"
    )


# ---------------------------------------------------------------------------
# HUB — Share & manage indexes via GitHub
# ---------------------------------------------------------------------------

@cli.group("hub")
def hub():
    """Manage and share indexes via GitHub."""


@hub.command("list")
def hub_list():
    """List available versions on the configured repository."""
    url = repo_url()
    if not url:
        click.echo("Error: No repo URL configured. Set it with environment or config.", err=True)
        sys.exit(1)

    from .github_distribution import list_databases as gh_list
    gh_list(url)


@hub.command("download")
@click.argument("version")
def hub_download(version: str):
    """Download a version database from the repository."""
    url = repo_url()
    if not url:
        click.echo("Error: No repo URL configured. Set it with environment or config.", err=True)
        sys.exit(1)

    from .github_distribution import download_database
    with _spinner(f"Downloading '{version}'…"):
        download_database(url, version, CACHE_DIR)

    add_active_version(version)
    click.echo(f"Version '{version}' added to active versions.")


@hub.command("push")
@click.argument("version")
@click.option("--force", is_flag=True, help="Overwrite existing release")
def hub_push(version: str, force: bool):
    """Push version databases to the repository."""
    url = repo_url()
    if not url:
        click.echo("Error: No repo URL configured. Set it with environment or config.", err=True)
        sys.exit(1)

    version_dir = CACHE_DIR / version
    if not version_dir.exists():
        click.echo(f"Version '{version}' not found in {CACHE_DIR}.", err=True)
        sys.exit(1)

    from .github_distribution import push_to_github
    with _spinner(f"Pushing '{version}'…"):
        push_to_github(url, version, version_dir, force=force)


@hub.command("delete")
@click.argument("version")
def hub_delete(version: str):
    """Delete a local version database."""
    version_dir = CACHE_DIR / version
    from .github_distribution import delete_database
    delete_database(version_dir)


# ---------------------------------------------------------------------------
# EXPLORE — Local export / import
# ---------------------------------------------------------------------------

@cli.group("explore", invoke_without_command=True)
@click.pass_context
def explore(ctx):
    """Explore local qrag databases. With no subcommand, open the interactive browser."""
    if ctx.invoked_subcommand is not None:
        return
    # No subcommand → interactive browser, falling back to a plain list.
    from . import explore as _explore
    if sys.stdin.isatty() and sys.stdout.isatty():
        from .tui import run_explore_browser
        if run_explore_browser():
            return
    versions = _explore.gather_local_versions()
    if not versions:
        click.echo("No local databases found in ~/.qrag.")
        click.echo("Build one with:  qrag build -i <path> -o <version>")
        return
    _render_version_table(versions)


def _render_version_table(versions: list) -> None:
    """Render `qrag explore list` — Rich table if available, plain text otherwise."""
    from .explore import human_age, human_size, lang_percentages

    def _content(v) -> str:
        return "+".join(k for k, present in (("code", v.has_code), ("docs", v.has_docs)) if present)

    def _langs(v, top: int = 3) -> str:
        pcts = lang_percentages(v.languages)[:top]
        return "  ".join(f"{lang} {pct:.0f}%" for lang, pct in pcts)

    try:
        from rich.box import SIMPLE_HEAVY
        from rich.console import Console
        from rich.table import Table
    except ImportError:
        for v in versions:
            flag = " *" if v.active else ""
            click.echo(
                f"{v.name:<20} {_content(v):<9} {human_size(v.size_bytes):>9}  "
                f"sym={v.symbols:<6} sec={v.sections:<5} [{_langs(v)}]  "
                f"{human_age(v.built_at)}{flag}"
            )
        click.echo("\n* = active   (activate with: qrag ai active <version>)")
        return

    table = Table(title="qrag databases", box=SIMPLE_HEAVY, title_justify="left")
    table.add_column("Version", style="bold cyan", no_wrap=True)
    table.add_column("Content")
    table.add_column("Size", justify="right")
    table.add_column("Symbols", justify="right")
    table.add_column("Sections", justify="right")
    table.add_column("Languages")
    table.add_column("Built")
    table.add_column("Active", justify="center")
    for v in versions:
        table.add_row(
            v.name,
            _content(v),
            human_size(v.size_bytes),
            f"{v.symbols:,}",
            f"{v.sections:,}",
            _langs(v),
            human_age(v.built_at),
            "[green]●[/green]" if v.active else "",
        )
    Console().print(table)


def _render_stats(stats) -> None:
    """Render `qrag explore stats VERSION` — lean panel (Rich if available)."""
    from .explore import human_age, human_size, lang_percentages

    lang_line = "  ".join(f"{lang} {pct:.0f}%" for lang, pct in lang_percentages(stats.languages)) or "—"
    header = f"{human_age(stats.built_at)} · {human_size(stats.size_bytes)}"
    if stats.active:
        header += " · active"
    if stats.embedding_model:
        header += f" · {stats.embedding_model}"

    lines = [header]
    if stats.has_code:
        lines.append(f"Code: {stats.code_chunks:,} chunks · {stats.symbols:,} symbols")
        lines.append(f"  {lang_line}")
    if stats.has_docs:
        lines.append(f"Docs: {stats.sections:,} sections · {stats.docs:,} docs · {stats.words:,} words")

    try:
        from rich.console import Console
        from rich.panel import Panel
        Console().print(Panel("\n".join(lines), title=f"[bold]{stats.name}[/bold]",
                              title_align="left", expand=False))
    except ImportError:
        click.echo(f"{stats.name}")
        for line in lines:
            click.echo(f"  {line}")


def _render_unified_table(rows: list) -> None:
    """Render a merged local+remote listing (Rich if available, else plain)."""
    from .explore import human_age, human_size, lang_percentages

    def _langs(local, top: int = 3) -> str:
        if local is None:
            return ""
        return "  ".join(f"{lang} {pct:.0f}%" for lang, pct in lang_percentages(local.languages)[:top])

    def _size(row) -> str:
        return human_size(row.local.size_bytes) if row.local else "—"

    def _built(row) -> str:
        if row.local:
            return human_age(row.local.built_at)
        if row.remote:
            return human_age(row.remote.updated_at)
        return "—"

    try:
        from rich.box import SIMPLE_HEAVY
        from rich.console import Console
        from rich.table import Table
    except ImportError:
        for row in rows:
            active = " *" if (row.local and row.local.active) else ""
            click.echo(
                f"{row.name:<20} {row.location:<13} {_size(row):>9}  "
                f"[{_langs(row.local)}]  {_built(row)}{active}"
            )
        click.echo("\n* = active   (activate with: qrag ai active <version>)")
        return

    table = Table(title="qrag databases", box=SIMPLE_HEAVY, title_justify="left")
    table.add_column("Version", style="bold cyan", no_wrap=True)
    table.add_column("Location")
    table.add_column("Size", justify="right")
    table.add_column("Languages")
    table.add_column("Updated")
    table.add_column("Active", justify="center")
    for row in rows:
        loc_styled = {
            "local+remote": "[green]local+remote[/green]",
            "local": "local",
            "remote": "[dim]remote[/dim]",
        }.get(row.location, row.location)
        table.add_row(
            row.name,
            loc_styled,
            _size(row),
            _langs(row.local),
            _built(row),
            "[green]●[/green]" if (row.local and row.local.active) else "",
        )
    Console().print(table)


@explore.command("list")
@click.option("--remote", "-r", "remote", is_flag=False, flag_value="", default=None,
              help="Also list versions from a remote (default remote if no name given).")
def explore_list(remote: str | None):
    """List local qrag databases (and optionally a remote's) with a summary."""
    from . import explore as _explore

    locals_ = _explore.gather_local_versions()

    if remote is None:
        if not locals_:
            click.echo("No local databases found in ~/.qrag.")
            click.echo("Build one with:  qrag build -i <path> -o <version>")
            return
        _render_version_table(locals_)
        return

    remotes: list = []
    warning: str | None = None
    try:
        with _spinner("Fetching remote versions…"):
            remotes = _explore.remote_versions(remote or None)
    except Exception as e:  # RemoteError and any transport failure
        warning = str(e)

    if not locals_ and not remotes:
        click.echo("No local databases found, and the remote returned nothing.")
        if warning:
            click.echo(f"Remote: {warning}", err=True)
        return

    _render_unified_table(_explore.merge_versions(locals_, remotes))
    if warning:
        click.echo(f"\nWarning: remote unavailable — {warning}", err=True)


@explore.command("stats")
@click.argument("version")
def explore_stats(version: str):
    """Show detailed statistics for a local database VERSION."""
    from . import explore as _explore
    if version not in _explore.local_version_names():
        click.echo(f"Error: version '{version}' not found in ~/.qrag.", err=True)
        available = _explore.local_version_names()
        if available:
            click.echo(f"Available: {', '.join(available)}", err=True)
        sys.exit(1)
    _render_stats(_explore.compute_stats(version))


def _render_diff_verbose(diff) -> None:
    """Flat, CI-friendly diff output."""
    def _files(label, fd):
        click.echo(f"{label}:  +{len(fd.added)}  -{len(fd.removed)}  ~{len(fd.changed)}")
        for p in fd.added:
            click.echo(f"  + {p}")
        for p in fd.removed:
            click.echo(f"  - {p}")
        for p in fd.changed:
            click.echo(f"  ~ {p}")

    click.echo(f"diff {diff.v1} → {diff.v2}")
    _files("Code files", diff.code_files)
    _files("Doc files", diff.doc_files)
    click.echo(f"Symbols:  +{len(diff.symbols_added)}  -{len(diff.symbols_removed)}")
    for s in diff.symbols_added:
        click.echo(f"  + {s.name} ({s.type})")
    for s in diff.symbols_removed:
        click.echo(f"  - {s.name} ({s.type})")
    shifts = [(l, a, b) for l, a, b in diff.lang_shift if a != b]
    if shifts:
        click.echo("Language shift:")
        for lang, a, b in shifts:
            click.echo(f"  {lang}  {a:.0f}% → {b:.0f}%")


@explore.command("diff")
@click.argument("v1")
@click.argument("v2")
@click.option("--json", "as_json", is_flag=True, help="Emit the diff as JSON (for CI).")
@click.option("--verbose", is_flag=True, help="Print flat text instead of the interactive viewer.")
def explore_diff(v1: str, v2: str, as_json: bool, verbose: bool):
    """Compare two local versions V1 and V2."""
    from . import explore as _explore

    for v in (v1, v2):
        if v not in _explore.local_version_names():
            click.echo(f"Error: version '{v}' not found in ~/.qrag.", err=True)
            sys.exit(1)

    diff = _explore.compute_diff(v1, v2)

    if as_json:
        click.echo(json.dumps(diff.to_dict(), indent=2))
        return

    if not verbose and sys.stdin.isatty() and sys.stdout.isatty():
        from .tui import run_diff_viewer
        if run_diff_viewer(diff):
            return

    _render_diff_verbose(diff)


@explore.command("download")
@click.argument("version")
@click.option("--remote", "-r", default=None,
              help="Remote to download from (default remote if omitted).")
def explore_download(version: str, remote: str | None):
    """Download VERSION from a remote and register it as active."""
    from . import explore as _explore
    from .config import add_active_version

    try:
        backend = _explore.get_backend(remote)
        backend.check_auth()
    except _explore.RemoteError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    with _spinner(f"Downloading '{version}' from '{backend.name}'…"):
        backend.download(version, CACHE_DIR)

    _explore.write_origin(version, backend.name)
    add_active_version(version)
    click.echo(f"✓ Downloaded '{version}' from '{backend.name}'. Added to active versions.")


def _remote_delete(version: str, remote: str, yes: bool) -> None:
    """Protected remote wipe: removes a published version for the whole team."""
    from . import explore as _explore

    try:
        backend = _explore.get_backend(remote or None)
    except _explore.RemoteError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    click.echo(
        f"⚠  About to DELETE published version '{version}' from remote "
        f"'{backend.name}' ({backend.type})."
    )
    click.echo("   This removes it for the whole team and cannot be undone.")

    if not yes:
        if not sys.stdin.isatty():
            click.echo("Error: remote delete requires --yes in non-interactive mode.", err=True)
            sys.exit(1)
        typed = click.prompt(f"Type the version name '{version}' to confirm",
                             default="", show_default=False)
        if typed != version:
            click.echo("Name did not match. Aborted.")
            return

    try:
        backend.check_auth()
        with _spinner(f"Deleting '{version}' from '{backend.name}'…"):
            backend.delete_remote(version)
    except _explore.RemoteError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    click.echo(f"✓ Deleted '{version}' from remote '{backend.name}'.")


@explore.command("delete")
@click.argument("version")
@click.option("--remote", "-r", default=None,
              help="Delete the PUBLISHED version from this remote instead of the local cache.")
@click.option("--yes", "-y", is_flag=True, help="Delete without confirmation.")
def explore_delete(version: str, remote: str | None, yes: bool):
    """Delete VERSION from the local cache, or (--remote) from a remote."""
    from . import explore as _explore
    from .explore import human_age, human_size

    if remote is not None:
        _remote_delete(version, remote, yes)
        return

    if version not in _explore.local_version_names():
        click.echo(f"Error: version '{version}' not found in ~/.qrag.", err=True)
        available = _explore.local_version_names()
        if available:
            click.echo(f"Available: {', '.join(available)}", err=True)
        sys.exit(1)

    info = _explore.gather_version(version)
    content = "+".join(k for k, present in (("code", info.has_code), ("docs", info.has_docs)) if present)
    click.echo(f"About to delete '{version}':")
    click.echo(f"  {content}  ·  {human_size(info.size_bytes)}  ·  built {human_age(info.built_at)}")
    if info.active:
        click.echo("  (currently active — it will be deactivated)")

    if not yes:
        if not sys.stdin.isatty():
            click.echo("Error: deleting requires --yes in non-interactive mode.", err=True)
            sys.exit(1)
        if not click.confirm("Proceed?", default=False):
            click.echo("Aborted.")
            return

    was_active = _explore.delete_local(version)
    click.echo(f"✓ Deleted '{version}'" + (" and deactivated it." if was_active else "."))


def _push_all_remotes(version: str, dry_run: bool, force: bool) -> None:
    """Push VERSION to every configured remote, continue-on-failure."""
    from . import explore as _explore
    from .config import get_remotes

    if not get_remotes():
        click.echo("Error: no remotes configured. Add one with 'qrag explore add-remote'.", err=True)
        sys.exit(1)

    outcomes = _explore.push_all_remotes(version, force=force, dry_run=dry_run)
    icons = {"ok": "✓", "skipped": "·", "failed": "✗", "dry-run": "?"}
    click.echo(f"Push '{version}' → all remotes:")
    failed = []
    for o in outcomes:
        line = f"  {icons.get(o.status, '?')} {o.remote}: {o.status}"
        if o.detail:
            line += f" — {o.detail}"
        click.echo(line)
        if o.status == "failed":
            failed.append(o.remote)

    if failed:
        click.echo("\nRe-run the failed remotes individually:")
        for remote in failed:
            click.echo(f"  qrag explore push {version} --remote {remote}")
        sys.exit(1)


@explore.command("push")
@click.argument("version")
@click.option("--remote", "-r", default=None,
              help="Target remote (default: the version's origin, else the default remote).")
@click.option("--all-remotes", is_flag=True, help="Push to every configured remote, continue-on-failure.")
@click.option("--dry-run", is_flag=True, help="Show what would be pushed without uploading.")
@click.option("--force", is_flag=True, help="Overwrite an existing remote release.")
def explore_push(version: str, remote: str | None, all_remotes: bool, dry_run: bool, force: bool):
    """Publish a local VERSION to a remote (or every remote with --all-remotes)."""
    from . import explore as _explore
    from .explore import human_size

    if version not in _explore.local_version_names():
        click.echo(f"Error: version '{version}' not found in ~/.qrag.", err=True)
        sys.exit(1)

    if all_remotes:
        _push_all_remotes(version, dry_run, force)
        return

    try:
        backend = _explore.resolve_push_backend(version, remote)
    except _explore.RemoteError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    version_dir = CACHE_DIR / version
    files = [f for f in ("code.db", "docs.db", "config.json", "build-report.txt", "manifest.json")
             if (version_dir / f).exists()]
    click.echo(f"Push '{version}' → remote '{backend.name}' ({backend.type})")
    for fname in files:
        click.echo(f"  {fname}  ({human_size((version_dir / fname).stat().st_size)})")

    if dry_run:
        click.echo("(dry run — nothing uploaded)")
        return

    try:
        backend.check_auth()
    except _explore.RemoteError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    with _spinner(f"Pushing '{version}' to '{backend.name}'…"):
        backend.push(version, version_dir, force=force)
    click.echo(f"✓ Pushed '{version}' to '{backend.name}'.")


@explore.command("add-remote")
@click.argument("name")
@click.argument("url")
@click.option("--type", "-t", "remote_type", default="github",
              help="Backend type (github, huggingface, jfrog, gitlfs).")
def explore_add_remote(name: str, url: str, remote_type: str):
    """Register a named remote NAME pointing at URL."""
    from . import explore as _explore
    from .config import add_remote, get_remote

    if remote_type not in _explore.backend_types():
        click.echo(
            f"Error: unknown remote type '{remote_type}'. "
            f"Known: {', '.join(_explore.backend_types())}.", err=True)
        sys.exit(1)
    if get_remote(name):
        click.echo(f"Remote '{name}' already exists — updating it.")
    add_remote(name, remote_type, url)
    click.echo(f"✓ Remote '{name}' → {remote_type}  {url}")


@explore.command("remove-remote")
@click.argument("name")
def explore_remove_remote(name: str):
    """Remove the named remote NAME."""
    from .config import remove_remote
    if remove_remote(name):
        click.echo(f"✓ Removed remote '{name}'.")
    else:
        click.echo(f"Error: no remote named '{name}'.", err=True)
        sys.exit(1)


@explore.command("list-remotes")
def explore_list_remotes():
    """List configured remotes."""
    from .config import default_remote, get_remotes

    remotes = get_remotes()
    if not remotes:
        click.echo("No remotes configured.")
        click.echo("Add one with:  qrag explore add-remote <name> --type github <url>")
        return

    dflt = default_remote()
    default_name = dflt[0] if dflt else None

    try:
        from rich.box import SIMPLE_HEAVY
        from rich.console import Console
        from rich.table import Table
    except ImportError:
        for name, cfg in remotes.items():
            flag = " (default)" if name == default_name else ""
            click.echo(f"{name:<16} {cfg.get('type', ''):<12} {cfg.get('url', '')}{flag}")
        return

    table = Table(title="qrag remotes", box=SIMPLE_HEAVY, title_justify="left")
    table.add_column("Name", style="bold cyan", no_wrap=True)
    table.add_column("Type")
    table.add_column("URL")
    table.add_column("Default", justify="center")
    for name, cfg in remotes.items():
        table.add_row(name, cfg.get("type", ""), cfg.get("url", ""),
                      "[green]●[/green]" if name == default_name else "")
    Console().print(table)


@explore.command("set-remote")
@click.argument("version")
@click.argument("remote")
def explore_set_remote(version: str, remote: str):
    """Reassign the origin REMOTE for a local VERSION."""
    from . import explore as _explore
    from .config import get_remote

    if version not in _explore.local_version_names():
        click.echo(f"Error: version '{version}' not found in ~/.qrag.", err=True)
        sys.exit(1)
    if not get_remote(remote):
        click.echo(
            f"Error: no remote named '{remote}'. "
            "Add it with 'qrag explore add-remote'.", err=True)
        sys.exit(1)
    _explore.set_origin_remote(version, remote)
    click.echo(f"✓ '{version}' origin set to '{remote}'.")


@explore.command("export")
@click.argument("version")
@click.option("--output", "-o", type=click.Path(), default=None,
              help="Output ZIP path (default: <cwd>/<version>.zip)")
def explore_export(version: str, output: str | None):
    """Package VERSION into a shareable ZIP file."""
    from .zip_distribution import export_version
    out = Path(output) if output else Path.cwd() / f"{version}.zip"
    export_version(version, out)


@explore.command("import")
@click.argument("zip_path", type=click.Path(exists=True))
@click.option("--version", "-v", "version_override", default=None,
              help="Override the version name on import")
@click.option("--yes", "-y", is_flag=True, help="Overwrite without confirmation")
def explore_import(zip_path: str, version_override: str | None, yes: bool):
    """Import a ZIP export and register it as an active database."""
    from .zip_distribution import import_version
    import_version(Path(zip_path), version_override, yes)


def _apply_system_certs() -> None:
    """Make Python use the OS certificate store (same as browsers, git, curl).

    truststore hooks into Python's ssl module so that all HTTPS clients
    (requests, httpx, urllib) trust whatever the OS already trusts —
    including corporate/proxy CAs installed by IT — without any user config.
    """
    try:
        import truststore
        truststore.inject_into_ssl()
    except ImportError:
        pass


def main() -> None:
    """Entry point wrapper — catches all exceptions and exits with a clean English message."""
    _apply_system_certs()
    log_path = _new_log_path()
    _save_log = False
    _exc: BaseException | None = None
    _exit_code = 0

    try:
        cli(standalone_mode=False)
    except click.exceptions.Exit as e:
        _exit_code = e.exit_code or 0
        if _exit_code != 0:
            _save_log = True
    except (click.exceptions.Abort, KeyboardInterrupt):
        click.echo("\nInterrupted.", err=True)
        _exit_code = 130
        _save_log = True
    except SystemExit as e:
        _exit_code = e.code if e.code is not None else 0
        if _exit_code not in (None, 0):
            _save_log = True
    except BaseException as e:
        _save_log = True
        _exc = e
        _exit_code = 1
        click.echo(f"\nError: {e}", err=True)

    if _save_log:
        ok = _write_error_log(log_path, _exc)
        if ok:
            print(
                f"\n{'─' * 60}\n"
                f"Something went wrong. A log file has been saved:\n\n"
                f"  {log_path}\n\n"
                f"To report this issue:\n"
                f"  1. Open https://github.com/inegmdev/qrag/issues/new\n"
                f"  2. Describe what you were doing\n"
                f"  3. Attach the log file above (drag & drop into the issue)\n"
                f"{'─' * 60}",
                file=sys.stderr,
            )

    sys.exit(_exit_code)
