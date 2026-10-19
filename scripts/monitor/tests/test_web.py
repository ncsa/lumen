from datetime import datetime, timedelta
from html.parser import HTMLParser
from http import HTTPStatus

import pytest

from monitor import migrate, web
from monitor.config import ConfigStore
from monitor.db import make_engine, make_session_factory, utcnow
from monitor.models import Probe
from monitor.probe import LiveSnapshot, Snapshot

MODEL = {
    "id": "org/qwen", "description": "Qwen", "status": "ok", "configured": 1, "healthy": 1,
    "input_price": 0.2, "output_price": 0.8, "requests": {"last_hour": 5, "last_day": 50}, "tokens": None,
}


@pytest.fixture
def make_client(tmp_path):
    engines = []

    def make(base_path=""):
        url = f"sqlite:///{tmp_path / 'monitor.db'}"
        migrate.upgrade(url)
        path = tmp_path / "config.yaml"
        path.write_text(f"lumen: {{url: 'https://lumen.test'}}\nbase_path: '{base_path}'\nrefresh: {{page: 2m}}\n")
        engine = make_engine(url)
        engines.append(engine)
        session_factory = make_session_factory(engine)
        with session_factory() as session:
            session.add(Probe(polled_at=utcnow() - timedelta(seconds=30), success=True, response_time_ms=42.0, model_count=1, models_up=1))
            session.commit()
        snapshot = LiveSnapshot()
        snapshot.set(Snapshot(utcnow(), [MODEL]))
        return web.create_app(ConfigStore(path, env={}), session_factory, snapshot).test_client()

    yield make
    for engine in engines:
        engine.dispose()


class Markup(HTMLParser):
    """Collects the tags and attributes the checks below need."""

    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def find(self, tag, **attrs):
        return [a for t, a in self.tags if t == tag and all(a.get(k) == v for k, v in attrs.items())]


def parse(html):
    markup = Markup()
    markup.feed(html)
    return markup


@pytest.mark.parametrize("base_path, prefix", [("", ""), ("/monitor", "/monitor"), ("monitor/", "/monitor")])
def test_every_route_and_asset_works_under_base_path(make_client, base_path, prefix):
    client = make_client(base_path)
    page = client.get(f"{prefix}/")
    assert page.status_code == HTTPStatus.OK
    html = page.get_data(as_text=True)
    markup = parse(html)
    (root,) = markup.find("div", id="overview")
    assert root["data-overview-url"] == f"{prefix}/api/overview"
    assert root["data-lumen-url"] == f"{prefix}/api/lumen"
    assert root["data-model-url"] == f"{prefix}/model/"
    assert root["data-refresh-ms"] == "120000"
    assets = [a["href"] for a in markup.find("link") if a["href"].startswith("/")] + [a["src"] for a in markup.find("script") if a.get("src", "").startswith("/")]
    assert sorted(assets) == [f"{prefix}/static/monitor.css", f"{prefix}/static/overview.js"]
    for asset in assets:
        assert client.get(asset).status_code == HTTPStatus.OK
    assert client.get(f"{prefix}/api/overview").status_code == HTTPStatus.OK
    assert client.get(f"{prefix}/api/lumen?range=1h").status_code == HTTPStatus.OK
    if prefix:
        assert client.get("/").status_code == HTTPStatus.NOT_FOUND
        assert client.get("/api/overview").status_code == HTTPStatus.NOT_FOUND
        assert client.get("/static/overview.js").status_code == HTTPStatus.NOT_FOUND


def test_page_marks_the_selected_range(make_client):
    markup = parse(make_client().get("/?range=7d").get_data(as_text=True))
    buttons = markup.find("button", **{"class": "btn btn-outline-primary range-btn"})
    assert [b["data-range"] for b in buttons] == ["1h", "12h", "24h", "7d", "30d", "1y"]
    assert [b["data-range"] for b in buttons if b["aria-pressed"] == "true"] == ["7d"]
    (root,) = markup.find("div", id="overview")
    assert root["data-range"] == "7d"


def test_page_accessibility_markup(make_client):
    html = make_client().get("/").get_data(as_text=True)
    markup = parse(html)
    assert markup.find("html", lang="en")
    assert len(markup.find("h1")) == 1
    assert markup.find("main", id="main-content")
    assert markup.find("caption", **{"class": "visually-hidden"})
    assert markup.find("div", **{"class": "table-responsive"})
    assert markup.find("p", id="updated")[0]["aria-live"] == "polite"
    assert markup.find("tbody", id="model-rows")[0]["aria-live"] == "polite"
    assert markup.find("div", id="load-error")[0]["role"] == "alert"
    assert markup.find("div", role="group")[0]["aria-label"] == "Time range"
    assert markup.find("canvas")[0]["aria-label"]
    # Sortable headers are native buttons, so they are reachable from the keyboard.
    assert len([a for t, a in markup.tags if t == "th" and "data-sort" in a]) == len(markup.find("button", **{"class": "sort-btn"})) == 10
    assert "skipto" in html


def test_api_overview(make_client):
    data = make_client().get("/api/overview?range=1h&sort=requests").get_json()
    assert data["range"] == "1h" and (data["sort"], data["direction"]) == ("requests", "desc")
    (row,) = data["models"]
    assert row["id"] == "org/qwen" and row["requests_24h"] == 50 and row["tokens_24h"] is None
    assert len(row["strip"]) == 60
    assert data["cards"]["requests_last_hour"] == 5 and data["cards"]["tokens_last_hour"] is None


def test_api_lumen(make_client):
    data = make_client().get("/api/lumen?range=12h").get_json()
    assert data["range"] == "12h" and data["state"] == "UP"
    assert data["last_ms"] == 42.0 and data["uptime"] == 100
    assert len(data["buckets"]) == 240
    assert datetime.fromisoformat(data["last_at"].rstrip("Z"))


def test_unknown_range_uses_the_default(make_client):
    client = make_client()
    assert client.get("/api/lumen?range=forever").get_json()["range"] == "24h"
    assert client.get("/api/overview?range=").get_json()["range"] == "24h"
