from datetime import timedelta
from http import HTTPStatus

import pytest
from bs4 import BeautifulSoup


def _row_for(html_bytes, model_name):
    soup = BeautifulSoup(html_bytes, "html.parser")
    return soup.find("li", class_="model-row", attrs={"data-model": model_name})


def test_models_requires_login(client):
    resp = client.get("/models", follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND
    assert "/" in resp.headers["Location"]


def test_models_lists_active_model(app, auth_client, test_model):
    resp = auth_client.get("/models")
    assert resp.status_code == HTTPStatus.OK
    assert test_model["model_name"].encode() in resp.data


def test_models_links_to_llmhub_for_unlisted_models(app, auth_client, restore_config):
    app.config["LLMHUB_URL"] = "https://llmhub.example.edu"
    resp = auth_client.get("/models")
    assert resp.status_code == HTTPStatus.OK
    soup = BeautifulSoup(resp.data, "html.parser")
    link = soup.find("a", href="https://llmhub.example.edu")
    assert link is not None
    assert "Go to LLMHub" in link.get_text(" ", strip=True)
    assert link["target"] == "_blank"
    assert "opens in a new tab" in link.get_text(" ", strip=True)
    assert "Want a model that’s not listed?" in link.parent.get_text(" ", strip=True)


def test_models_hides_llmhub_link_when_unconfigured(app, auth_client, restore_config):
    app.config["LLMHUB_URL"] = ""
    resp = auth_client.get("/models")
    assert resp.status_code == HTTPStatus.OK
    soup = BeautifulSoup(resp.data, "html.parser")
    assert "LLMHub" not in soup.get_text()
    assert "Want a model that’s not listed?" not in soup.get_text()


def test_models_row_shows_input_and_output_modalities(app, auth_client):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        db.session.add(ModelConfig(
            model_name="pill-model",
            input_cost_per_million=1.0,
            output_cost_per_million=1.0,
            input_modalities=["text", "image"],
            output_modalities=["text"],
        ))
        db.session.commit()

    resp = auth_client.get("/models")
    assert resp.status_code == HTTPStatus.OK
    row = _row_for(resp.data, "pill-model")
    assert row is not None
    assert row.find(class_="model-modalities").get_text(strip=True) == "Text, Image \u2192 Text"


def test_models_row_unknown_modalities_shows_dash(app, auth_client, test_model):
    resp = auth_client.get("/models")
    row = _row_for(resp.data, test_model["model_name"])
    assert row is not None
    assert row.find(class_="model-modalities").get_text(strip=True) == "\u2014"  # em dash


@pytest.mark.parametrize(("inputs", "outputs", "text"), [
    (["text"], 1, "Text \u2192 \u2014"),
    (1, 1, "\u2014"),
    ("text", None, "Text \u2192 \u2014"),
    ({"kind": "text"}, ["text"], "\u2014 \u2192 Text"),
    (["text", 3, ""], ["text"], "Text \u2192 Text"),
])
def test_models_page_tolerates_malformed_modalities(app, auth_client, inputs, outputs, text):
    # The JSON columns are not schema-checked, and config sync stores e.g.
    # ``output_modalities: 1`` as is; the dashboard must still render.
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        db.session.add(ModelConfig(model_name="odd-model", input_cost_per_million=1.0, output_cost_per_million=1.0,
                                   input_modalities=inputs, output_modalities=outputs))
        db.session.commit()

    resp = auth_client.get("/models")
    assert resp.status_code == HTTPStatus.OK
    assert _row_for(resp.data, "odd-model").find(class_="model-modalities").get_text(strip=True) == text


def _make_other_owner(app):
    """Create a second user entity to own a model."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        owner = Entity(entity_type="user", email="owner@example.com", name="Owner", active=True)
        db.session.add(owner)
        db.session.commit()
        return owner.id


def test_models_owned_model_not_listed(app, auth_client, test_model, test_user):
    """A model owned by someone else (no grant) is hidden from the list."""
    from tests.conftest import set_model_owner
    with app.app_context():
        owner_id = _make_other_owner(app)
        set_model_owner(test_model["id"], owner_id)

    resp = auth_client.get("/models")
    assert resp.status_code == HTTPStatus.OK
    assert test_model["model_name"].encode() not in resp.data


def test_models_inactive_model_not_listed(app, auth_client):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        m = ModelConfig(model_name="inactive-model", input_cost_per_million=1.0, output_cost_per_million=1.0, disabled=True)
        db.session.add(m)
        db.session.commit()

    resp = auth_client.get("/models")
    assert resp.status_code == HTTPStatus.OK
    assert b"inactive-model" not in resp.data


def test_model_detail_requires_login(client, test_model):
    resp = client.get(f"/models/{test_model['model_name']}", follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND


def test_model_detail_404_unknown(auth_client):
    resp = auth_client.get("/models/does-not-exist")
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_models_panel_shows_the_charged_cache_rates(app, auth_client):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        db.session.add_all([
            ModelConfig(model_name="cached-model", input_cost_per_million=1.0, output_cost_per_million=2.0,
                        cached_input_cost_per_million=0.1),
            ModelConfig(model_name="cache-write-model", input_cost_per_million=1.0, output_cost_per_million=2.0,
                        cache_write_cost_per_million=0.5),
            ModelConfig(model_name="plain-model", input_cost_per_million=1.0, output_cost_per_million=2.0),
        ])
        db.session.commit()

    resp = auth_client.get("/models")
    assert resp.status_code == HTTPStatus.OK

    def pricing(model_name):
        row = _row_for(resp.data, model_name)
        assert row.find(class_="model-price").find(string=True, recursive=False).strip() == "1.00 / 2.00"
        dds = row.find("dl", class_="model-pricing").find_all("dd")
        return [d.get_text(strip=True) for d in dds]

    # An unconfigured read rate is billed at the input price and an unconfigured
    # write rate at 0, so the panel shows what is charged on the missing side
    # rather than a dash or a hover tip.
    assert pricing("cached-model") == ["1.00", "2.00", "0.10", "0.00"]
    assert pricing("cache-write-model") == ["1.00", "2.00", "1.00", "0.50"]
    assert pricing("plain-model") == ["1.00", "2.00", "1.00", "0.00"]


def test_model_detail_cached_input_row_only_when_set(app, auth_client):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        db.session.add_all([
            ModelConfig(model_name="cached-detail", input_cost_per_million=1.0, output_cost_per_million=2.0,
                        cached_input_cost_per_million=0.05),
            ModelConfig(model_name="cache-write-detail", input_cost_per_million=1.0, output_cost_per_million=2.0,
                        cache_write_cost_per_million=0.75),
            ModelConfig(model_name="plain-detail", input_cost_per_million=1.0, output_cost_per_million=2.0),
        ])
        db.session.commit()

    soup = BeautifulSoup(auth_client.get("/models/cached-detail").data, "html.parser")
    th = soup.find("th", string="Cached input")
    assert th is not None
    assert th.parent.find("td").get_text(strip=True) == "0.05 / 1M"
    write_soup = BeautifulSoup(auth_client.get("/models/cache-write-detail").data, "html.parser")
    write = write_soup.find("th", string="Cache write")
    assert write is not None
    assert write.parent.find("td").get_text(strip=True) == "0.75 / 1M"
    plain = BeautifulSoup(auth_client.get("/models/plain-detail").data, "html.parser")
    assert plain.find("th", string="Cached input") is None
    # An unset write rate is billed at 0, so the Cache write row always shows.
    plain_write = plain.find("th", string="Cache write")
    assert plain_write is not None
    assert plain_write.parent.find("td").get_text(strip=True) == "0.00 / 1M"
    # The SGLang note is only for a configured write rate.
    assert "Cache writes are billed only from a count" not in plain.get_text()


def test_model_detail_ok(auth_client, test_model):
    resp = auth_client.get(f"/models/{test_model['model_name']}")
    assert resp.status_code == HTTPStatus.OK
    assert test_model["model_name"].encode() in resp.data


def test_model_detail_blocked_returns_not_found(app, auth_client, test_model, test_user):
    from tests.conftest import set_model_owner
    with app.app_context():
        owner_id = _make_other_owner(app)
        set_model_owner(test_model["id"], owner_id)

    resp = auth_client.get(f"/models/{test_model['model_name']}")
    assert resp.status_code == HTTPStatus.NOT_FOUND
    assert test_model["model_name"].encode() not in resp.data


def test_model_detail_inactive_returns_not_found(app, auth_client):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        m = ModelConfig(model_name="inactive-detail", input_cost_per_million=1.0, output_cost_per_million=1.0, disabled=True)
        db.session.add(m)
        db.session.commit()

    resp = auth_client.get("/models/inactive-detail")
    assert resp.status_code == HTTPStatus.NOT_FOUND
    assert b"inactive-detail" not in resp.data


def test_model_detail_expired_returns_not_found(app, auth_client):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        from lumen.timeutils import utcnow

        m = ModelConfig(
            model_name="expired-detail",
            input_cost_per_million=1.0,
            output_cost_per_million=1.0,
            end_date=utcnow() - timedelta(seconds=1),
        )
        db.session.add(m)
        db.session.commit()

    resp = auth_client.get("/models/expired-detail")
    assert resp.status_code == HTTPStatus.NOT_FOUND
    assert b"expired-detail" not in resp.data


def test_model_readme_inactive_returns_not_found_without_fetching(app, auth_client, monkeypatch):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        m = ModelConfig(
            model_name="inactive-readme",
            input_cost_per_million=1.0,
            output_cost_per_million=1.0,
            url="https://huggingface.co/org/Some-Model",
            disabled=True,
        )
        db.session.add(m)
        db.session.commit()

    from lumen.blueprints.models_page import routes

    def fail_fetch(*args, **kwargs):
        raise AssertionError("must not fetch")

    monkeypatch.setattr(routes.http_requests, "get", fail_fetch)

    resp = auth_client.get("/models/inactive-readme/readme")
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_model_readme_blocked_returns_not_found_without_fetching(
    app, auth_client, test_model, test_user, monkeypatch
):
    from tests.conftest import set_model_owner

    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig

        model = db.session.get(ModelConfig, test_model["id"])
        model.url = "https://huggingface.co/org/private-model"
        db.session.commit()
        set_model_owner(test_model["id"], _make_other_owner(app))

    from lumen.blueprints.models_page import routes

    def fail_fetch(*args, **kwargs):
        raise AssertionError("must not fetch")

    monkeypatch.setattr(routes.http_requests, "get", fail_fetch)

    resp = auth_client.get(f"/models/{test_model['model_name']}/readme")
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_model_detail_needs_ack_remains_visible(app, auth_client, test_model):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig

        db.session.get(ModelConfig, test_model["id"]).needs_ack = True
        db.session.commit()

    resp = auth_client.get(f"/models/{test_model['model_name']}")
    assert resp.status_code == HTTPStatus.OK
    assert test_model["model_name"].encode() in resp.data


def _set_model_url(app, model_id, url):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        db.session.get(ModelConfig, model_id).url = url
        db.session.commit()


class _FakeReadme:
    def __init__(self, text="", error=None):
        self.text = text
        self._error = error

    def raise_for_status(self):
        if self._error is not None:
            raise self._error


def _fetches(monkeypatch, response):
    """Replace the HF fetch; return the list of URLs it was asked for."""
    from lumen.blueprints.models_page import routes

    urls = []

    def fake_get(url, timeout):
        urls.append(url)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(routes.http_requests, "get", fake_get)
    return urls


def test_model_readme_non_huggingface_url_returns_not_found(app, auth_client, test_model, monkeypatch):
    _set_model_url(app, test_model["id"], "https://example.com/org/model")
    urls = _fetches(monkeypatch, AssertionError("must not fetch"))

    resp = auth_client.get(f"/models/{test_model['model_name']}/readme")
    assert resp.status_code == HTTPStatus.NOT_FOUND
    assert urls == []


def test_model_readme_huggingface_url_without_repo_returns_not_found(app, auth_client, test_model, monkeypatch):
    _set_model_url(app, test_model["id"], "https://huggingface.co/org")
    urls = _fetches(monkeypatch, AssertionError("must not fetch"))

    resp = auth_client.get(f"/models/{test_model['model_name']}/readme")
    assert resp.status_code == HTTPStatus.NOT_FOUND
    assert urls == []


def test_model_readme_strips_front_matter(app, auth_client, test_model, monkeypatch):
    _set_model_url(app, test_model["id"], "https://huggingface.co/org/Some-Model/tree/main")
    urls = _fetches(monkeypatch, _FakeReadme("---\nlicense: mit\n---\n\n# Some Model\nBody\n"))

    resp = auth_client.get(f"/models/{test_model['model_name']}/readme")
    assert resp.status_code == HTTPStatus.OK
    assert resp.headers["Content-Type"] == "text/plain; charset=utf-8"
    assert resp.get_data(as_text=True) == "# Some Model\nBody\n"
    # Only the org/repo part of the model URL is kept.
    assert urls == ["https://huggingface.co/org/Some-Model/raw/main/README.md"]


def test_model_readme_without_front_matter_is_returned_unchanged(app, auth_client, test_model, monkeypatch):
    _set_model_url(app, test_model["id"], "https://huggingface.co/org/Some-Model")
    # Starts with "---" but never closes it: nothing is stripped.
    _fetches(monkeypatch, _FakeReadme("--- not front matter\n# Title\n"))

    resp = auth_client.get(f"/models/{test_model['model_name']}/readme")
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_data(as_text=True) == "--- not front matter\n# Title\n"


def test_model_readme_upstream_http_error_returns_bad_gateway(app, auth_client, test_model, monkeypatch):
    import requests

    _set_model_url(app, test_model["id"], "https://huggingface.co/org/Some-Model")
    _fetches(monkeypatch, _FakeReadme(error=requests.HTTPError("404 Client Error")))

    resp = auth_client.get(f"/models/{test_model['model_name']}/readme")
    assert resp.status_code == HTTPStatus.BAD_GATEWAY
    assert resp.data == b""


def test_model_readme_connection_error_returns_bad_gateway(app, auth_client, test_model, monkeypatch):
    import requests

    _set_model_url(app, test_model["id"], "https://huggingface.co/org/Some-Model")
    _fetches(monkeypatch, requests.ConnectionError("unreachable"))

    resp = auth_client.get(f"/models/{test_model['model_name']}/readme")
    assert resp.status_code == HTTPStatus.BAD_GATEWAY


def _add_endpoints(app, model_id, *healthy_flags):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_endpoint import ModelEndpoint
        for i, healthy in enumerate(healthy_flags):
            db.session.add(ModelEndpoint(
                model_config_id=model_id, url=f"http://ep{i}.example", api_key="k", healthy=healthy,
            ))
        db.session.commit()


def _status_badge_text(html_bytes):
    soup = BeautifulSoup(html_bytes, "html.parser")
    row = soup.find("th", string="Status").find_parent("tr")
    return row.find("span", class_="badge").get_text(strip=True)


def test_model_detail_some_endpoints_unhealthy_is_degraded(app, auth_client, test_model):
    _add_endpoints(app, test_model["id"], True, False)
    resp = auth_client.get(f"/models/{test_model['model_name']}")
    assert resp.status_code == HTTPStatus.OK
    assert _status_badge_text(resp.data) == "degraded"


def test_model_detail_all_endpoints_healthy_is_ok(app, auth_client, test_model):
    _add_endpoints(app, test_model["id"], True, True)
    resp = auth_client.get(f"/models/{test_model['model_name']}")
    assert resp.status_code == HTTPStatus.OK
    assert _status_badge_text(resp.data) == "ok"


def test_model_detail_shows_when_the_acknowledgement_was_given(app, auth_client, test_model, test_user):
    from datetime import datetime

    acked_at = datetime(2026, 3, 4, 5, 6, 7)
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_consent import EntityModelConsent
        from lumen.models.model_config import ModelConfig

        db.session.get(ModelConfig, test_model["id"]).needs_ack = True
        db.session.add(EntityModelConsent(
            entity_id=test_user["id"], model_config_id=test_model["id"], consented_at=acked_at,
        ))
        db.session.commit()

    resp = auth_client.get(f"/models/{test_model['model_name']}")
    assert resp.status_code == HTTPStatus.OK
    assert b'data-utc="2026-03-04T05:06:07Z"' in resp.data


_HOSTILE_NOTICE = (
    "**Bold** and [docs](https://example.edu/docs)\n\n"
    "- one\n- two\n\n"
    "<script>alert(1)</script>\n\n"
    '<a href="javascript:alert(2)">click</a>\n'
)


def test_model_detail_sanitises_notices(app, auth_client):
    """Admin-entered notice and ack text drop script tags and javascript: links but keep markdown."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        db.session.add(ModelConfig(
            model_name="notice-model",
            input_cost_per_million=1.0,
            output_cost_per_million=1.0,
            notice=_HOSTILE_NOTICE,
            needs_ack=True,
            ack_message=_HOSTILE_NOTICE,
        ))
        db.session.commit()

    resp = auth_client.get("/models/notice-model")
    assert resp.status_code == HTTPStatus.OK
    soup = BeautifulSoup(resp.data, "html.parser")
    for block in (soup.find(id="model-notice").find("div", class_="mt-1"), soup.find(id="model-ack-notice")):
        assert block is not None
        assert block.find("script") is None
        assert "javascript:" not in str(block)
        assert block.find("strong").get_text() == "Bold"
        assert block.find("a", href="https://example.edu/docs") is not None
        assert [li.get_text() for li in block.find_all("li")] == ["one", "two"]


def _visible_text(tag):
    """Text a sighted user sees: the tag's text without its visually-hidden parts."""
    tag = BeautifulSoup(str(tag), "html.parser")
    for hidden in tag.find_all(class_="visually-hidden"):
        hidden.decompose()
    return " ".join(tag.get_text().split())


def _pricing_output_cell(html_bytes):
    """The Output price cell of the detail page's Pricing table."""
    soup = BeautifulSoup(html_bytes, "html.parser")
    table = soup.find("caption", string="Model pricing per million tokens").parent
    return table.find("th", string="Output").parent.find("td")


def _add_embedding_and_text_models(app):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        db.session.add_all([
            ModelConfig(model_name="embed-model", input_cost_per_million=1.0, output_cost_per_million=0,
                        output_modalities=["embedding"]),
            ModelConfig(model_name="text-model", input_cost_per_million=1.0, output_cost_per_million=2.0,
                        output_modalities=["text"]),
        ])
        db.session.commit()


def test_models_embedding_model_shows_dash_for_output_price(app, auth_client):
    _add_embedding_and_text_models(app)
    resp = auth_client.get("/models")
    assert resp.status_code == HTTPStatus.OK

    embed = _row_for(resp.data, "embed-model")
    price = embed.find(class_="model-price")
    assert _visible_text(price) == "1.00 / —"
    assert "no output price" in price.get_text()
    embed_dds = embed.find("dl", class_="model-pricing").find_all("dd")
    assert _visible_text(embed_dds[1]) == "—"
    assert embed_dds[1].find(class_="visually-hidden").get_text() == "no output price"
    # The sort attribute keeps the stored price, which is always 0 for an embedding model.
    assert float(embed["data-sort-output"]) == 0

    text = _row_for(resp.data, "text-model")
    assert _visible_text(text.find(class_="model-price")) == "1.00 / 2.00"
    assert text.find("dl", class_="model-pricing").find_all("dd")[1].get_text(strip=True) == "2.00"


def test_model_detail_embedding_model_shows_dash_for_output_price(app, auth_client):
    _add_embedding_and_text_models(app)

    td = _pricing_output_cell(auth_client.get("/models/embed-model").data)
    assert _visible_text(td) == "—"
    assert "/ 1M" not in td.get_text()
    assert td.find(class_="visually-hidden").get_text() == "no output price"

    assert _pricing_output_cell(auth_client.get("/models/text-model").data).get_text(strip=True) == "2.00 / 1M"


def test_models_price_sort_puts_embedding_model_first_at_equal_input_price(app, auth_client):
    _add_embedding_and_text_models(app)
    soup = BeautifulSoup(auth_client.get("/models?sort=price&dir=asc").data, "html.parser")
    order = [r["data-model"] for r in soup.find_all("li", class_="model-row")]
    assert order.index("embed-model") < order.index("text-model")
