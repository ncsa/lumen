"""scripts/responsive_check.py failure paths: typos, wrong pages and admin mode must fail loudly."""
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


# ---------------------------------------------------------------------------
# A row only passes if the audit measured the page it meant to measure.
# ---------------------------------------------------------------------------

def _row(status=200, url="http://localhost:5001/models", overflow=0, chat_input=None):
    return {"status": status, "url": url, "overflow": overflow, "chatInput": chat_input}


def test_clean_page_passes():
    assert responsive_check.problems("models", "/models", _row()) == []


def test_server_error_fails():
    assert responsive_check.problems("models", "/models", _row(status=500)) == ["HTTP 500, expected 200"]


def test_intentional_404_passes_and_200_there_fails():
    url = "http://localhost:5001/responsive-check-missing"
    assert responsive_check.problems("404", "/responsive-check-missing", _row(status=404, url=url)) == []
    assert responsive_check.problems("404", "/responsive-check-missing", _row(status=200, url=url)) == ["HTTP 200, expected 404"]


def test_auth_redirect_fails():
    row = _row(url="http://localhost:5001/")
    assert responsive_check.problems("models", "/models", row) == ["redirected to /"]


def test_admin_analytics_redirect_to_usage_passes():
    row = _row(url="http://localhost:5001/usage")
    assert responsive_check.problems("admin-analytics", "/admin/analytics", row) == []


def test_query_string_does_not_count_as_redirect():
    row = _row(url="http://localhost:5001/device?code=ABCD-EFGH")
    assert responsive_check.problems("oauth-consent", "/device?code=ABCD-EFGH", row) == []


def test_unresolved_detail_page_fails():
    assert responsive_check.problems("group-detail", None, None) == ["no URL to visit (missing demo data?)"]


def test_chat_without_input_bar_fails():
    row = _row(url="http://localhost:5001/chat", chat_input=None)
    assert responsive_check.problems("chat", "/chat", row) == ["input missing"]


def test_chat_with_hidden_input_fails():
    row = _row(url="http://localhost:5001/chat", chat_input=False)
    assert responsive_check.problems("chat", "/chat", row) == ["input hidden"]


# ---------------------------------------------------------------------------
# Chat columns must not resize while a reply streams (RK-299).
# ---------------------------------------------------------------------------

def _layout(sidebar_after=260.0, main_after=1020.0, flex=None, lst="3 conversations"):
    return [{"list": lst, "flex": flex or dict(responsive_check.EXPECTED_FLEX),
             "before": {"sidebar": 260.0, "main": 1020.0},
             "after": {"sidebar": sidebar_after, "main": main_after}}]


def test_stable_chat_layout_passes():
    assert responsive_check.layout_problems(_layout()) == []


def test_subpixel_drift_passes():
    assert responsive_check.layout_problems(_layout(sidebar_after=260.6, main_after=1019.4)) == []


def test_shrinking_sidebar_fails():
    assert responsive_check.layout_problems(_layout(sidebar_after=194.0, main_after=1086.0, lst="empty list")) == [
        "sidebar width 260→194px (empty list)", "main width 1020→1086px (empty list)"]


def test_content_sized_flex_fails():
    flex = {"sidebar": "0 1 auto", "main": "1 1 auto"}
    assert responsive_check.layout_problems(_layout(flex=flex)) == [
        "sidebar flex 0 1 auto, expected 0 0 260px (3 conversations)",
        "main flex 1 1 auto, expected 1 1 0px (3 conversations)"]


def test_missing_chat_columns_fail():
    assert responsive_check.layout_problems(None) == ["chat columns missing"]


def test_chat_layout_problems_fail_the_row():
    row = _row(url="http://localhost:5001/chat", chat_input=True)
    row["layout"] = _layout(sidebar_after=194.0, main_after=1086.0)
    assert responsive_check.problems("chat", "/chat", row) == [
        "sidebar width 260→194px (3 conversations)", "main width 1020→1086px (3 conversations)"]


def test_phone_chat_row_has_no_layout_check():
    row = _row(url="http://localhost:5001/chat", chat_input=True)
    assert responsive_check.problems("chat", "/chat", row) == []


def test_failed_rows_fail_the_report(tmp_path, monkeypatch):
    monkeypatch.setattr(responsive_check, "OUT", str(tmp_path))
    rows = {("chat", "admin", w, h): {"problems": ["HTTP 500, expected 200", "input missing"], "status": 500,
                                      "shot": "chat.png", "offenders": []}
            for w, h in responsive_check.SIZES}
    assert responsive_check.write_report({"rows": rows}) is True
    report = (tmp_path / "report.md").read_text()
    assert "FAIL: HTTP 500, expected 200, input missing" in report


# ---------------------------------------------------------------------------
# Admin mode must actually be on before the admin pass is audited.
# ---------------------------------------------------------------------------

class _FakePage:
    def __init__(self, result):
        self.result = result

    def goto(self, *args, **kwargs):
        pass

    def evaluate(self, script):
        return self.result


@pytest.mark.parametrize("result", [
    {"status": 400, "body": {"error": "CSRF token missing"}},
    {"status": 403, "body": None},
    {"status": 200, "body": {"admin_mode": False}},
])
def test_rejected_admin_mode_aborts(result):
    with pytest.raises(RuntimeError, match="could not enable admin mode"):
        responsive_check.enable_admin_mode(_FakePage(result))


def test_accepted_admin_mode_continues():
    responsive_check.enable_admin_mode(_FakePage({"status": 200, "body": {"admin_mode": True}}))
