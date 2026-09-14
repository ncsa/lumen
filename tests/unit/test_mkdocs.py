"""Tests for the MkDocs + GitHub Pages publishing setup (mkdocs.yml)."""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _load_mkdocs_config():
    import yaml

    return yaml.safe_load((REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8"))


def _nav_files():
    """Yield every source filename referenced by the mkdocs nav, recursively."""
    config = _load_mkdocs_config()

    def walk(entries):
        for entry in entries:
            if isinstance(entry, dict):
                for key, value in entry.items():
                    if isinstance(value, str):
                        yield value
                    elif isinstance(value, list):
                        yield from walk(value)

    return list(walk(config["nav"]))


def test_mkdocs_nav_entries_point_to_existing_docs():
    """Every mkdocs nav entry maps to a markdown file that exists in docs/."""
    config = _load_mkdocs_config()
    docs_dir = REPO_ROOT / config["docs_dir"]

    files = _nav_files()
    assert files, "nav must reference at least one page"
    for filename in files:
        assert (docs_dir / filename).is_file(), f"nav entry missing file: {filename}"


def test_mkdocs_build_strict(tmp_path):
    """`mkdocs build --strict` completes with no warnings-as-errors.

    This is the same build the GitHub Pages workflow runs (gh-deploy --strict),
    so it catches broken relative links and missing nav files.
    """
    from click.testing import CliRunner
    from mkdocs.__main__ import build_command

    runner = CliRunner()
    result = runner.invoke(
        build_command,
        [
            "--strict",
            "--site-dir",
            str(tmp_path / "site"),
            "--config-file",
            str(REPO_ROOT / "mkdocs.yml"),
        ],
    )
    assert result.exit_code == 0, result.output
