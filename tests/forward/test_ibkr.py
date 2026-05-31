"""IBKR adapters: pure converters + IO against a mocked IB (no live Gateway in CI)."""
# The mock IB mirrors ib_async's camelCase API names exactly (the adapter calls them),
# so camelCase method/arg names here are intentional.
# ruff: noqa: N802, N803
from datetime import date
from types import SimpleNamespace

import pytest

import src.forward.ibkr as ibkr


def _bar(d, o, h, lo, c, v):
    return SimpleNamespace(date=d, open=o, high=h, low=lo, close=c, volume=v, average=0, barCount=0)


def _news(time, code, aid, headline):
    return SimpleNamespace(time=time, providerCode=code, articleId=aid, headline=headline)


# ---- pure converters -----------------------------------------------------
def test_bars_to_price_dict():
    bars = [_bar(date(2026, 5, 19), 100.1, 101.9, 99.4, 101.2, 1_234_567),
            _bar(date(2026, 5, 20), 101.0, 103.0, 100.5, 102.7, 0)]
    out = ibkr._bars_to_price_dict(bars)
    assert out["2026-05-19"] == {"open": 100.1, "high": 101.9, "low": 99.4,
                                 "close": 101.2, "volume": 1_234_567}
    assert out["2026-05-20"]["volume"] == 0  # ib_async uses -1/0 for no-volume; clamped


def test_news_to_articles_joins_bodies_and_dates():
    items = [_news("2026-05-20 14:30:00.0", "BZ", "A1", "Acme beats Q1"),
             _news("2026-05-21 09:00:00.0", "DJNL", "A2", "Acme buyback")]
    bodies = {"A1": "Full text 1", "A2": "Full text 2"}
    arts = ibkr._news_to_articles(items, bodies)
    assert arts[0] == {"title": "Acme beats Q1", "summary": "Full text 1",
                       "published": "2026-05-20", "source": "BZ", "url": ""}
    assert arts[1]["published"] == "2026-05-21" and arts[1]["source"] == "DJNL"


def test_news_source_from_store_is_sync_lookup():
    store = {"AAA": [{"title": "x", "published": "2026-05-20"}]}
    src = ibkr.news_source_from_store(store)
    assert src("AAA", "2026-05-22", 7) == store["AAA"]
    assert src("ZZZ", "2026-05-22", 7) == []  # unknown ticker -> empty


# ---- IO against a mocked IB ----------------------------------------------
class _FakeIB:
    def __init__(self, bars=None, news=None, providers=None, body="body"):
        self.bars = bars or []
        self.news = news or []
        self.providers = providers or []
        self.body = body

    async def qualifyContractsAsync(self, contract):
        contract.conId = 999
        return [contract]

    async def reqHistoricalDataAsync(self, contract, **kw):
        return self.bars

    async def reqNewsProvidersAsync(self):
        return self.providers

    async def reqHistoricalNewsAsync(self, conId, codes, start, end, total):
        return self.news  # a plain list (adapter handles .articles-or-list)

    async def reqNewsArticleAsync(self, provider, article_id):
        return SimpleNamespace(articleType=0, articleText=self.body)


@pytest.mark.asyncio
async def test_fetch_daily_bars_converts():
    ib = _FakeIB(bars=[_bar(date(2026, 5, 20), 10, 11, 9, 10.5, 100)])
    out = await ibkr.fetch_daily_bars(ib, "AAA", lookback_days=30)
    assert out["2026-05-20"]["close"] == 10.5


@pytest.mark.asyncio
async def test_refresh_price_cache_writes_each_ticker(monkeypatch):
    saved = []
    monkeypatch.setattr(ibkr, "save_prices_to_cache",
                        lambda t, data, period: saved.append((t, period, len(data))))
    ib = _FakeIB(bars=[_bar(date(2026, 5, 20), 10, 11, 9, 10.5, 100)])
    n = await ibkr.refresh_price_cache(ib, ["AAA", "BBB"], lookback_days=30, period="ibkr")
    assert n == 2
    assert {t for t, _, _ in saved} == {"AAA", "BBB"}
    assert all(p == "ibkr" for _, p, _ in saved)


@pytest.mark.asyncio
async def test_fetch_news_for_builds_store_with_bodies():
    ib = _FakeIB(
        news=[_news("2026-05-20 12:00:00", "BZ", "A1", "Acme beats")],
        providers=[SimpleNamespace(code="BZ", name="Benzinga")],
        body="the full article text",
    )
    store = await ibkr.fetch_news_for(ib, ["AAA"], "2026-05-22", lookback_days=7)
    assert store["AAA"][0]["title"] == "Acme beats"
    assert store["AAA"][0]["summary"] == "the full article text"
    assert store["AAA"][0]["published"] == "2026-05-20"
