"""
Forward decision provider — the supervisor for live/forward paper trading.

Mirrors the backtest supervisor (point-in-time fundamentals + technicals + sentiment,
then synthesize) but adds the **news** specialist, which is valid only forward (news
as of a live date is uncontaminated; see notes/productization.md 0.0). Data sources
are injected — the same loaders the backtest uses, or live IBKR-backed adapters — so
this is vendor-agnostic and testable offline; the IBKR adapters plug in at those seams.

News is included via an explicit toggle so forward paper can run the **ablation**
(news on vs off) and let the live record decide whether the news specialist earns its
keep — rather than assuming it does.
"""
from __future__ import annotations

import asyncio
import logging

from src.agents.news_specialist import NewsSource, run_news_specialist
from src.agents.scaffold import ScaffoldConfig
from src.agents.sentiment_specialist import (
    FilingSummaryCache,
    current_filing_key,
    run_sentiment_specialist,
)
from src.backtest.backtest_supervisor import (
    FundamentalsLoader,
    TechnicalsLoader,
    _call_fundamentals_with_data,
    _call_technicals_with_data,
)
from src.backtest.cache import SignalCache, cached_signal
from src.data.fundamentals import pe_band
from src.llm import MessageCreator
from src.schemas import Decision
from src.schemas.fundamentals import FundamentalsAnalysis
from src.schemas.sentiment import SentimentAnalysis
from src.schemas.technicals import TechnicalsAnalysis

logger = logging.getLogger(__name__)


async def run_forward_decision(
    messages_api: MessageCreator,
    ticker: str,
    as_of: str,
    *,
    fundamentals_loader: FundamentalsLoader,
    technicals_loader: TechnicalsLoader,
    news_source: NewsSource | None = None,
    include_news: bool = True,
    news_lookback_days: int = 7,
    signal_cache: SignalCache | None = None,
    summary_cache: FilingSummaryCache | None = None,
    scaffold_config: ScaffoldConfig | None = None,
) -> Decision | None:
    """Produce a forward Decision for (ticker, as_of) from the four specialists.

    Returns None when the core numeric inputs (fundamentals/technicals) are
    unavailable — that name simply isn't decided this period. Sentiment is dropped
    when no filing resolves; news is dropped when there's no news or it's toggled off.
    Each missing specialist just removes a vote from synthesis.
    """
    fundamentals_data = fundamentals_loader(ticker, as_of)
    technicals_data = technicals_loader(ticker, as_of)
    if fundamentals_data is None or technicals_data is None:
        logger.info(f"forward: skipping {ticker} @ {as_of} (no fundamentals/technicals)")
        return None

    fundamentals_key = f"{fundamentals_data.report_date}_pe-{pe_band(fundamentals_data.pe_ratio)}"
    coros = [
        cached_signal(
            signal_cache, "fundamentals", ticker, fundamentals_key, FundamentalsAnalysis,
            lambda: _call_fundamentals_with_data(messages_api, ticker, as_of, fundamentals_data),
        ),
        cached_signal(
            signal_cache, "technicals", ticker, as_of, TechnicalsAnalysis,
            lambda: _call_technicals_with_data(messages_api, ticker, as_of, technicals_data),
        ),
    ]

    # Sentiment (filing narrative, point-in-time): keyed by the current filing accession.
    sentiment_key = current_filing_key(ticker, as_of)
    if sentiment_key:
        coros.append(cached_signal(
            signal_cache, "sentiment", ticker, sentiment_key, SentimentAnalysis,
            lambda: run_sentiment_specialist(
                messages_api, ticker, as_of, config=scaffold_config, summary_cache=summary_cache
            ),
        ))

    # News (forward-only): not cached (per-day, low reuse) and may return None (no news).
    if include_news and news_source is not None:
        coros.append(run_news_specialist(
            messages_api, ticker, as_of, news_source,
            lookback_days=news_lookback_days, config=scaffold_config,
        ))

    signals = [s for s in await asyncio.gather(*coros) if s is not None]
    return synthesize_or_none(ticker, signals, as_of)


def synthesize_or_none(ticker: str, signals: list, as_of: str) -> Decision | None:
    from src.synthesis import synthesize
    if not signals:
        return None
    return synthesize(ticker=ticker, signals=signals, as_of=as_of)
