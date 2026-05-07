"""
Tests for opened_today.OpenedTodayRegistry — JSON persistence of symbols
opened on the current UTC date for the PDT guardrail.
"""

from datetime import date

from opened_today import OpenedTodayRegistry


def test_record_open_persists_across_instances(tmp_path):
    path = tmp_path / "opened.json"
    today = date(2026, 5, 7)

    reg = OpenedTodayRegistry(str(path))
    reg.record_open("AAPL", today)

    # Fresh instance should see the persisted entry.
    reload = OpenedTodayRegistry(str(path))
    assert reload.was_opened_on("AAPL", today) is True


def test_record_close_removes_and_persists(tmp_path):
    path = tmp_path / "opened.json"
    today = date(2026, 5, 7)

    reg = OpenedTodayRegistry(str(path))
    reg.record_open("AAPL", today)
    reg.record_close("AAPL")

    reload = OpenedTodayRegistry(str(path))
    assert reload.was_opened_on("AAPL", today) is False


def test_was_opened_on_other_date_returns_false(tmp_path):
    path = tmp_path / "opened.json"
    reg = OpenedTodayRegistry(str(path))
    reg.record_open("AAPL", date(2026, 5, 7))
    assert reg.was_opened_on("AAPL", date(2026, 5, 8)) is False


def test_prune_drops_stale_entries(tmp_path):
    path = tmp_path / "opened.json"
    yesterday = date(2026, 5, 6)
    today = date(2026, 5, 7)

    reg = OpenedTodayRegistry(str(path))
    reg.record_open("AAPL", yesterday)
    reg.record_open("MSFT", today)

    reg.prune(today)

    assert reg.was_opened_on("AAPL", yesterday) is False
    assert reg.was_opened_on("MSFT", today) is True

    # Prune is persisted, so a fresh instance also sees AAPL gone.
    reload = OpenedTodayRegistry(str(path))
    assert "AAPL" not in reload._data
    assert reload.was_opened_on("MSFT", today) is True


def test_missing_file_starts_empty(tmp_path):
    reg = OpenedTodayRegistry(str(tmp_path / "does_not_exist.json"))
    assert reg.was_opened_on("AAPL", date(2026, 5, 7)) is False


def test_corrupt_file_recovers(tmp_path):
    path = tmp_path / "opened.json"
    path.write_text("not-json")
    reg = OpenedTodayRegistry(str(path))
    assert reg.was_opened_on("AAPL", date(2026, 5, 7)) is False
    # Should still be writable after a corrupt-load.
    reg.record_open("AAPL", date(2026, 5, 7))
    assert reg.was_opened_on("AAPL", date(2026, 5, 7)) is True
