"""Query log: record, count and rank, on SQLite only."""

from tesseract import querylog


def test_record_and_rank(tmp_path):
    db = str(tmp_path / "q.sqlite")
    assert querylog.top_nodes(5, path=db) == []
    assert querylog.record("who owns x?", "MATCH (n) RETURN n", ["a", "b"], 12.5, path=db) == 1
    querylog.record("and y?", "MATCH (n) RETURN n", ["a", "c"], 3, path=db)
    querylog.record("and z?", None, [], 1, path=db)
    assert querylog.top_nodes(5, path=db) == [("a", 2), ("b", 1), ("c", 1)]
    assert len(querylog.top_nodes(1, path=db)) == 1
    assert querylog.count(path=db) == 3


def test_default_log_lives_in_the_state_folder(tmp_path):
    querylog.record("q", "", ["a"], 1)
    assert (tmp_path / "state" / "querylog.sqlite").is_file()


def test_ids_from_rows():
    assert querylog.ids_from_rows([{"id": "n1", "name": "x"}, {"owner_id": 7, "title": "t"}, "junk"]) == ["n1", 7]
    assert querylog.ids_from_rows(None) == []


def test_normalise_and_question_id():
    assert querylog.normalise("  Who OWNS  x? ") == "who owns x"
    assert querylog.question_id("Who owns x?") == querylog.question_id("who owns x") != querylog.question_id("who owns y")


def test_aggregate_merges_repeats_and_keeps_viewers():
    qs, edges = querylog._aggregate([
        (1, "2026-10-10T10:00:00", "Who owns x?", '["a", "b", "a"]', "engineer"),
        (2, "2026-10-10T11:00:00", "who owns x", '["a"]', "legal"),
        (3, "2026-10-10T12:00:00", "", '["a"]', None)])
    edge_a = next(e for e in edges if e["nid"] == "a")
    assert len(qs) == 1 and qs[0]["n"] == 2
    assert edge_a["n"] == 2 and edge_a["viewer"] == "legal" and edge_a["viewers"] == ["engineer", "legal"]


def test_record_stores_viewer_and_origin(tmp_path):
    db = str(tmp_path / "q.sqlite")
    querylog.record("q", None, ["a"], 1, path=db, viewer="csm", origin="api")
    assert querylog.count(path=db) == 1 and querylog.count(origin="api", path=db) == 1
