"""ONLY validation in scripts/responsive_check.py: a typo must fail loudly."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import responsive_check  # noqa: E402


def test_valid_only_names_pass():
    assert responsive_check.unknown_routes(set()) == []
    assert responsive_check.unknown_routes({"chat", "404", "admin-users"}) == []


def test_unknown_only_names_are_reported():
    assert responsive_check.unknown_routes({"chat", "caht", "profle"}) == ["caht", "profle"]


def test_main_exits_on_unknown_only_name(monkeypatch):
    monkeypatch.setattr(responsive_check, "ONLY", {"caht"})
    monkeypatch.setattr(responsive_check, "create_app",
                        lambda: pytest.fail("ran the audit despite an unknown ONLY name"))
    with pytest.raises(SystemExit) as exc:
        responsive_check.main()
    assert "caht" in str(exc.value.code)
