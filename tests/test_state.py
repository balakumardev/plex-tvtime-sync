# tests/test_state.py
import json

from plex_tvtime_sync.state import OVERLAP_SECONDS, State


def test_first_run_initializes_watermark_to_now(tmp_path):
    s = State(tmp_path)
    assert s.first_run is True
    assert s.watermark > 1_700_000_000  # sane epoch


def test_roundtrip_not_first_run(tmp_path):
    s = State(tmp_path)
    s.save()
    s2 = State(tmp_path)
    assert s2.first_run is False
    assert s2.watermark == s.watermark


def test_mark_processed_advances_watermark_and_dedups(tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({"watermark": 1000, "processed": {}}))
    s = State(tmp_path)
    assert not s.is_processed("42:2000")
    s.mark_processed("42:2000", 2000)
    assert s.is_processed("42:2000")
    assert s.watermark == 2000
    s.mark_processed("41:1500", 1500)  # older item must not regress watermark
    assert s.watermark == 2000


def test_save_prunes_processed_outside_overlap(tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({"watermark": 1000, "processed": {}}))
    s = State(tmp_path)
    s.mark_processed("1:1000", 1000)
    s.mark_processed("2:50000", 50000)
    s.save()
    kept = json.loads((tmp_path / "state.json").read_text())["processed"]
    assert "2:50000" in kept
    assert "1:1000" not in kept  # 1000 < 50000 - OVERLAP_SECONDS


def test_ledger_counts_rewatches(tmp_path):
    s = State(tmp_path)
    assert s.seen_count("episodes", "123") == 0
    s.record_mark("episodes", "123")
    s.save()
    s2 = State(tmp_path)
    assert s2.seen_count("episodes", "123") == 1
    assert s2.seen_count("movies", "uuid-1") == 0


def test_corrupt_state_json_recovers_as_first_run(tmp_path):
    (tmp_path / "state.json").write_text("{truncated")
    s = State(tmp_path)
    assert s.first_run is True
    assert s.watermark > 1_700_000_000
    s.save()  # must not raise; file repaired
    assert State(tmp_path).first_run is False


def test_corrupt_ledger_json_recovers_with_default_shape(tmp_path):
    (tmp_path / "ledger.json").write_text("not json at all")
    s = State(tmp_path)
    assert s.seen_count("episodes", "1") == 0
    s.record_mark("movies", "u-1")
    assert s.seen_count("movies", "u-1") == 1


def test_valid_but_incomplete_ledger_gets_default_kinds(tmp_path):
    (tmp_path / "ledger.json").write_text("{}")
    s = State(tmp_path)
    assert s.seen_count("episodes", "1") == 0


def test_save_leaves_no_tmp_files_and_prune_keeps_boundary(tmp_path):
    import json as _json
    (tmp_path / "state.json").write_text(_json.dumps({"watermark": 1000, "processed": {}}))
    s = State(tmp_path)
    s.mark_processed("b:50000", 50000)
    s.mark_processed("a:49700", 49700)  # exactly watermark - OVERLAP_SECONDS: kept (>=)
    s.save()
    kept = _json.loads((tmp_path / "state.json").read_text())["processed"]
    assert "a:49700" in kept
    assert list(tmp_path.glob("*.tmp")) == []
