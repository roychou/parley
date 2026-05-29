import src.data.universe as universe

# Synthetic membership: AAA left and re-entered (two spells), BBB still a member,
# CCC a short early stint.
_CSV = """ticker,start_date,end_date
AAA,2010-01-01,2015-12-31
AAA,2020-01-01,
BBB,2012-06-01,
CCC,2008-01-01,2009-01-01
"""


def _patch(monkeypatch, tmp_path):
    p = tmp_path / "sp500_ticker_start_end.csv"
    p.write_text(_CSV)
    monkeypatch.setattr(universe, "CSV_PATH", p)


def test_membership_in_first_spell(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)
    assert universe.sp500_as_of("2013-01-01") == ["AAA", "BBB"]


def test_gap_between_spells_excludes(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)
    # 2017 is after AAA's first spell ended and before it re-entered.
    assert universe.sp500_as_of("2017-01-01") == ["BBB"]


def test_re_entry_spell_included(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)
    assert universe.sp500_as_of("2021-01-01") == ["AAA", "BBB"]


def test_boundaries_inclusive(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)
    assert "AAA" in universe.sp500_as_of("2010-01-01")   # start inclusive
    assert "AAA" in universe.sp500_as_of("2015-12-31")   # end inclusive
    assert "AAA" not in universe.sp500_as_of("2016-01-01")


def test_short_early_stint(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)
    assert universe.sp500_as_of("2008-06-01") == ["CCC"]
