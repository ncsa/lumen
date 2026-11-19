"""scripts/check_coverage.py: the CI per-file coverage gate fails below the floor and fails closed on a broken report."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import check_coverage  # noqa: E402


def _report(tmp_path, percents):
    path = tmp_path / "coverage.json"
    files = {name: {"summary": {"percent_covered": percent}} for name, percent in percents.items()}
    path.write_text(json.dumps({"meta": {"version": "7"}, "files": files, "totals": {"percent_covered": 95.0}}))
    return path


def test_all_files_at_or_above_the_floor_pass(tmp_path, capsys):
    path = _report(tmp_path, {"lumen/a.py": 100.0, "lumen/b.py": 92.5})
    assert check_coverage.main(["--min", "90", str(path)]) == 0
    assert "all 2 files" in capsys.readouterr().out


def test_one_file_below_the_floor_fails_and_only_it_is_named(tmp_path, capsys):
    path = _report(tmp_path, {"lumen/good.py": 100.0, "lumen/bad.py": 89.9, "lumen/edge.py": 90.0})
    assert check_coverage.main(["--min", "90", str(path)]) == 1
    out = capsys.readouterr().out
    assert "lumen/bad.py: 89.90%" in out
    assert "good.py" not in out and "edge.py" not in out


def test_a_file_at_exactly_the_floor_passes(tmp_path):
    assert check_coverage.main(["--min", "90", str(_report(tmp_path, {"lumen/edge.py": 90.0}))]) == 0


def test_the_floor_defaults_to_90(tmp_path):
    assert check_coverage.main([str(_report(tmp_path, {"lumen/edge.py": 90.0}))]) == 0
    assert check_coverage.main([str(_report(tmp_path, {"lumen/bad.py": 89.99}))]) == 1


def test_a_custom_floor_is_used_instead_of_90(tmp_path, capsys):
    path = _report(tmp_path, {"lumen/mid.py": 92.0})
    assert check_coverage.main(["--min", "95", str(path)]) == 1
    assert "lumen/mid.py: 92.00% < 95%" in capsys.readouterr().out
    assert check_coverage.main(["--min", "95", str(_report(tmp_path, {"lumen/ok.py": 96.0}))]) == 0


def test_the_path_defaults_to_coverage_json(tmp_path, monkeypatch):
    _report(tmp_path, {"lumen/bad.py": 50.0})
    monkeypatch.chdir(tmp_path)
    assert check_coverage.main([]) == 1


@pytest.mark.parametrize("content, message", [
    (None, "not found"),
    ("{not json", "not valid JSON"),
    ("", "not valid JSON"),
    ("{}", "not a coverage.py JSON report"),
    ("[]", "not a coverage.py JSON report"),
    ('{"files": {"lumen/a.py": {}}}', "not a coverage.py JSON report"),
    ('{"files": {}}', "lists no files"),
], ids=["missing", "invalid-json", "empty-file", "no-files-key", "not-an-object", "no-summary", "zero-files"])
def test_a_broken_report_fails_closed(tmp_path, capsys, content, message):
    path = tmp_path / "coverage.json"
    if content is not None:
        path.write_text(content)
    assert check_coverage.main([str(path)]) == 2
    assert message in capsys.readouterr().err
