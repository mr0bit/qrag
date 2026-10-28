"""Build failure reporting without downloading models or starting worker pools."""
import pytest
from click.testing import CliRunner

import qrag.cli as commands
import qrag.embedder as embedder
from qrag.database import load_manifest


@pytest.mark.parametrize("kind", ["code", "doc"])
@pytest.mark.parametrize("verbose", [False, True])
@pytest.mark.parametrize("fails", [False, True])
def test_build_outcome(isolated_qrag, tmp_path, monkeypatch, kind, verbose, fails):
    source = tmp_path / "input"
    source.mkdir()
    path = source / ("sample.c" if kind == "code" else "sample.html")
    path.write_text("test input")
    commands.save_global({"active_versions": []})
    monkeypatch.setattr(commands, "_ensure_build_deps", lambda: None)
    monkeypatch.setattr(embedder, "resolve_device", lambda device: "cpu")
    monkeypatch.setattr(
        commands, "_detect_input_type",
        lambda directory: ([path], []) if kind == "code" else ([], [path]),
    )

    def producer(files, workers, queue, errors):
        for root, rel in files:
            if fails:
                errors.append((str(path), "deliberate parser failure"))
                queue.put((commands._KIND_ERROR, "deliberate parser failure", 0.0, root, rel))
            else:
                queue.put((kind, [], 0.0, root, rel))

    monkeypatch.setattr(commands, "_run_code_producer" if kind == "code" else "_run_doc_producer", producer)
    args = (["--verbose"] if verbose else []) + ["build", "-i", str(source), "-o", "test"]
    result = CliRunner().invoke(commands.cli, args)
    out_dir = isolated_qrag / "test"
    assert result.exit_code == (1 if fails else 0), result.output
    report = (out_dir / "build-report.txt").read_text()
    assert f"Status                 : {'FAILED' if fails else 'SUCCESS'}" in report
    assert f"Files failed           : {int(fails)}" in report
    assert "Build report:" in result.output
    assert (out_dir / commands._BUILD_STATE_FILE).exists() == fails
    if fails:
        assert "deliberate parser failure" in result.output
        assert str(path) in report
        assert "deliberate parser failure" in report
        assert "✓" not in result.output
        assert "added to active versions" not in result.output
        assert "test" not in commands.load_global().get("active_versions", [])
        assert not load_manifest(out_dir / ("code.db" if kind == "code" else "docs.db"))
    else:
        assert "✓" in result.output
        assert "test" in commands.load_global()["active_versions"]


@pytest.mark.timeout(5)
@pytest.mark.parametrize("kind", ["code", "doc", "both"])
@pytest.mark.parametrize("stage", ["startup", "submit", "shutdown"])
def test_producer_pool_crash_exits_and_preserves_progress(
    isolated_qrag, tmp_path, monkeypatch, kind, stage,
):
    from concurrent.futures import Future

    source = tmp_path / "input"
    source.mkdir()
    code = source / "sample.c"
    doc = source / "sample.html"
    code.write_text("int example(void);")
    doc.write_text("<p>example</p>")
    commands.save_global({"active_versions": []})
    monkeypatch.setattr(commands, "_ensure_build_deps", lambda: None)
    monkeypatch.setattr(embedder, "resolve_device", lambda device: "cpu")
    monkeypatch.setattr(commands, "_detect_input_type", lambda directory: (
        [code] if kind in ("code", "both") else [],
        [doc] if kind in ("doc", "both") else [],
    ))

    class BrokenPool:
        def __init__(self, **kwargs):
            if stage == "startup":
                raise RuntimeError("pool startup failed")

        def __enter__(self):
            return self

        def submit(self, function, path):
            if stage == "submit":
                raise RuntimeError("pool submit failed")
            future = Future()
            future.set_result(([], 0.01))
            return future

        def __exit__(self, *args):
            if stage == "shutdown":
                raise RuntimeError("pool shutdown failed")

    monkeypatch.setattr(commands, "ProcessPoolExecutor", BrokenPool)
    result = CliRunner().invoke(commands.cli, [
        "--verbose", "build", "-i", str(source), "-o", "crash",
    ])
    assert result.exit_code == 1, result.output
    assert f"pool {stage} failed" in result.output
    assert "✓" not in result.output
    assert "crash" not in commands.load_global()["active_versions"]
    out_dir = isolated_qrag / "crash"
    assert (out_dir / commands._BUILD_STATE_FILE).exists()
    report = (out_dir / "build-report.txt").read_text()
    assert "FAILED" in report
    assert f"pool {stage} failed" in report
    for file_kind in (["code", "doc"] if kind == "both" else [kind]):
        manifest = load_manifest(out_dir / ("code.db" if file_kind == "code" else "docs.db"))
        assert len(manifest) == (1 if stage == "shutdown" else 0)


@pytest.mark.parametrize("exception", [RuntimeError("crash"), SystemExit(2), KeyboardInterrupt()])
def test_completion_signal_is_sent_once_after_crash(exception):
    import queue

    messages = queue.Queue()
    errors = []

    def crash(*args):
        raise exception

    commands._run_producer_safely(crash, "code", {}, 1, messages, errors)
    assert messages.get_nowait()[0] == commands._KIND_ERROR
    assert messages.get_nowait() is None
    assert messages.empty()
    assert len(errors) == 1
    assert type(exception).__name__ in errors[0][1]
