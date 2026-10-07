"""Public font assets and prepaint must not expose arbitrary application files."""

import pytest

pytestmark = pytest.mark.integration


async def test_font_is_public_woff2_with_safe_cache_headers(client):
    response = await client.get("/api/v1/ui/assets/inter-vietnamese-400-normal.woff2")
    assert response.status_code == 200
    assert response.content.startswith(b"wOF2")
    assert response.headers["content-type"] == "font/woff2"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "max-age=86400" in response.headers["cache-control"]


@pytest.mark.parametrize(
    "name",
    [
        "LICENSE",
        "index.html",
        "inter-latin-900-normal.woff2",
        "inter-latin-400-normal.woff2%2F..%2Findex.html",
    ],
)
async def test_asset_route_cannot_read_other_files(client, name):
    assert (await client.get("/api/v1/ui/assets/" + name)).status_code == 404


async def test_preferences_execute_before_style_and_main_script(client):
    html = (await client.get("/api/v1/ui")).text
    assert html.index("<script data-ui-prepaint>") < html.index("<style>") < html.index("<body>")
    assert html.index("const UIPreferences") < html.index("function langGet")
    assert "__UI_PREPAINT__" not in html and "__UI_ICONS__" not in html
    assert "@layer reset, tokens, base, layout, components, pages, utilities" in html
