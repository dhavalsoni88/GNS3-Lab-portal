"""Theme switch: Light, Dark and System are offered on the sign-in page and after sign-in."""
from fastapi.testclient import TestClient

import app as portal


def test_theme_switch_is_on_every_page_and_the_stylesheet_supports_all_three():
    with TestClient(portal.app) as client:
        for page in (client.get("/login"), (client.post("/login", data={"username": "portaladmin", "password": "initial-admin-password"}), client.get("/profile"))[1]):
            assert page.status_code == 200
            for choice in ("system", "light", "dark"):
                assert f'data-theme-choice="{choice}"' in page.text
            assert 'localStorage.getItem("portal-theme")' in page.text   # applied before the stylesheet, so there is no flash
        css = client.get("/static/app.css").text
        assert ':root[data-theme="dark"]' in css and ":root:not([data-theme])" in css   # forced dark, and dark only when the system asks and nothing is chosen
