"""Tests for the SEO guide pages."""

from markupsafe import escape

from readafp.app import create_app
from readafp.guides import GUIDES, GUIDES_BY_SLUG


def _client():
    return create_app().test_client()


def test_guide_index_lists_all_guides() -> None:
    html = _client().get("/guide").get_data(as_text=True)
    assert "AFP" in html
    for g in GUIDES:
        assert f"/guide/{g.slug}" in html  # each is linked


def test_each_guide_renders_with_seo_tags() -> None:
    c = _client()
    for g in GUIDES:
        r = c.get(f"/guide/{g.slug}")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        # canonical points at this guide, and Article structured data present
        assert f'href="https://readafp.com/guide/{g.slug}"' in html
        assert '"@type": "Article"' in html
        # meta description rendered (HTML-escaped, e.g. apostrophes)
        assert str(escape(g.description)) in html


def test_guide_pages_link_back_to_the_tool() -> None:
    html = _client().get("/guide/what-is-an-afp-file").get_data(as_text=True)
    assert 'href="/"' in html  # CTA / header link to the app


def test_related_guides_resolve() -> None:
    # Every related slug must be a real guide (no dead internal links).
    for g in GUIDES:
        for slug in g.related:
            assert slug in GUIDES_BY_SLUG


def test_unknown_guide_404() -> None:
    assert _client().get("/guide/not-a-real-guide").status_code == 404


def test_sitemap_includes_guides() -> None:
    xml = _client().get("/sitemap.xml").get_data(as_text=True)
    assert "https://readafp.com/guide" in xml
    for g in GUIDES:
        assert f"https://readafp.com/guide/{g.slug}" in xml


def test_home_links_to_guides() -> None:
    html = _client().get("/").get_data(as_text=True)
    assert 'href="/guide"' in html


def test_private_page_offers_no_upload_options() -> None:
    html = _client().get("/private").get_data(as_text=True)
    assert "git clone https://github.com/bone1966/readAFP" in html  # run local
    assert "docker build -t readafp" in html  # self-host
    assert 'href="https://readafp.com/private"' in html  # canonical


def test_home_and_sitemap_link_private() -> None:
    assert 'href="/private"' in _client().get("/").get_data(as_text=True)
    assert "https://readafp.com/private" in (
        _client().get("/sitemap.xml").get_data(as_text=True))
