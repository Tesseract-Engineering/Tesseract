"""The JSONPath subset specs may use: what it matches, and what it refuses to parse."""

import pytest

from tesseract.forge.runtime import jsonpath

DOC = {
    "title": "Switch auth providers",
    "user": {"login": "jane"},
    "labels": [{"name": "auth"}, {"name": "security"}],
    "@odata.nextLink": "https://example.test/next",
    "fields": {"a": 1, "b": 2},
}


@pytest.mark.parametrize("path, expected", [
    ("$", [DOC]),
    ("$.title", ["Switch auth providers"]),
    ("$.user.login", ["jane"]),
    ("$.labels[0].name", ["auth"]),
    ("$.labels[-1].name", ["security"]),
    ("$.labels[*].name", ["auth", "security"]),
    ("$['@odata.nextLink']", ["https://example.test/next"]),
    ("$.fields[*]", [1, 2]),
    ("$.missing", []),
    ("$.labels[5].name", []),
    ("$.title.deeper", []),
])
def test_find(path, expected):
    assert jsonpath.find(path, DOC) == expected


def test_first_falls_back_to_the_default():
    assert jsonpath.first("$.user.login", DOC) == "jane"
    assert jsonpath.first("$.user.email", DOC, "none") == "none"


@pytest.mark.parametrize("path", [
    "title", "$.", "$..title", "$.labels[?(@.name)]", "$.labels[0:1]", "$.a b", "$[name]", "$.1abc",
])
def test_anything_outside_the_subset_does_not_parse(path):
    with pytest.raises(ValueError):
        jsonpath.parse(path)
