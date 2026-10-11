"""Viewers come from the company file; reading follows visibility and ACL groups."""

import pytest

from tesseract import access


def test_without_a_company_file_there_is_one_unprivileged_viewer():
    assert access.default_viewer() == {"name": "employee", "label": "Employee (no restricted groups)",
                                       "acl_groups": []}


def test_company_file_defines_presets_and_default(company_file):
    company_file({"name": "Example Co", "default_viewer": "eng",
                  "viewers": {"eng": {"label": "Engineer"},
                              "legal": {"label": "Legal", "acl_groups": ["sharepoint:legal"]}}})
    assert access.default_viewer()["name"] == "eng"
    assert access.viewer_of("legal")["acl_groups"] == ["sharepoint:legal"]
    with pytest.raises(ValueError):
        access.viewer_of("ceo")


def test_bad_default_viewer_is_rejected(company_file):
    company_file({"default_viewer": "nobody", "viewers": {"eng": {}}})
    with pytest.raises(ValueError):
        access.default_viewer()


def test_explicit_viewer_is_validated():
    assert access.viewer_of({"name": "x", "acl_groups": ["g"]}) == {"name": "x", "acl_groups": ["g"]}
    with pytest.raises(ValueError):
        access.viewer_of({"acl_groups": "not-a-list"})


def test_readable_grant_and_owner():
    legal = {"name": "legal", "acl_groups": ["sharepoint:legal"]}
    assert access.readable("internal", [], {"acl_groups": []})
    assert not access.readable("restricted", ["sharepoint:legal"], {"acl_groups": []})
    assert access.readable("restricted", ["sharepoint:legal"], legal)
    assert access.grant(["sharepoint:legal"], legal) == "sharepoint:legal"
    assert access.owner_of(["sharepoint:legal-ops", "outlook:user:a@b.c", "slack:sec", "slack:#ops"]) == \
        "Legal Ops, a@b.c, the #sec channel owners, the #ops channel owners"


def test_restricted_note_names_groups_and_owners():
    note = access.restricted_note([{"acl": ["sharepoint:security"]}, {"acl": ["sharepoint:security"]}])
    assert note.startswith("2 matching documents are restricted (sharepoint:security)")
    assert note.endswith("ask Security for access.")


def test_company_file_beside_the_data_is_found(tmp_path, monkeypatch):
    import json

    from tesseract import config

    (tmp_path / "company.json").write_text(json.dumps({"name": "Data Co"}))
    monkeypatch.setenv("TESSERACT_DATA", str(tmp_path))
    config.settings.cache_clear()
    assert config.settings().company.name == "Data Co"


def test_sign_in_users_default_to_one_per_viewer(company_file):
    from tesseract import config

    company_file({"viewers": {"eng": {"label": "Engineer"}, "legal": {"label": "Legal"}}})
    assert [u["viewer"] for u in config.settings().company.sign_in_users()] == ["eng", "legal"]
    company_file({"viewers": {"eng": {}}, "users": [{"name": "Jane Doe", "team": "Payments"}]})
    assert config.settings().company.sign_in_users() == [{"name": "Jane Doe", "team": "Payments", "viewer": "eng"}]


def test_a_user_with_an_unknown_viewer_is_rejected(company_file):
    from tesseract import config

    company_file({"viewers": {"eng": {}}, "users": [{"name": "Jane Doe", "viewer": "ceo"}]})
    with pytest.raises(ValueError):
        config.settings()
