"""The demo's setup tools: written plans, and checks that never call a surface live when it is not."""

import pytest

pytest.importorskip("mcp")

from forge_demo import server  # noqa: E402

CLOSED = "http://127.0.0.1:9"          # the discard port: nothing listens there


def test_the_plan_names_each_surface_and_says_what_has_no_template():
    out = server.plan_surfaces(["Google Docs", "slack", "Zoom meetings", "customer ticket desk", "Teams"])
    assert [p["surface"] for p in out.structured_content["plan"]] == ["docs", "slack", "zoom", "tickets"]
    assert out.structured_content["unknown"] == ["Teams"]
    assert all(p["approval"] for p in out.structured_content["plan"])


def test_a_surface_that_is_down_is_never_called_live(monkeypatch):
    monkeypatch.setattr(server, "CORE", CLOSED)
    monkeypatch.setattr(server, "MEETING", CLOSED)
    for surface in ("docs", "slack", "zoom", "tickets"):
        out = server.deploy_surface(surface)
        assert out.structured_content["live"] is False and "✗" in out.content[0].text
    assert server.smoke_test("zoom").structured_content["passed"] is False
    assert server.smoke_test("slack").structured_content["passed"] is False


def test_an_unknown_surface_is_an_error():
    assert server.deploy_surface("teams").is_error
