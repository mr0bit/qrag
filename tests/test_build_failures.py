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
        queue.put(None)

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
