from src.backtest.cache import SignalCache, cached_signal
from src.backtest.metrics import (
    StrategyMetrics,
    annualized_return,
    compute_metrics,
    hit_rate,
    max_drawdown,
    sharpe_ratio,
    total_return,
)
from src.backtest.portfolio import EquitySnapshot, Portfolio, Position, Trade
from src.backtest.replay import (
    BacktestConfig,
    BacktestResult,
    StrategyOutcome,
    run_backtest,
)
from src.backtest.strategies import (
    Action,
    MultiAgentStrategy,
    PERankingStrategy,
    RandomStrategy,
    RSIStrategy,
    SPYHoldStrategy,
    Strategy,
    compute_rsi,
)

__all__ = [
    "EquitySnapshot",
    "Portfolio",
    "Position",
    "Trade",
    "StrategyMetrics",
    "annualized_return",
    "compute_metrics",
    "hit_rate",
    "max_drawdown",
    "sharpe_ratio",
    "total_return",
    "Action",
    "MultiAgentStrategy",
    "PERankingStrategy",
    "RandomStrategy",
    "RSIStrategy",
    "SPYHoldStrategy",
    "Strategy",
    "compute_rsi",
    "SignalCache",
    "cached_signal",
    "BacktestConfig",
    "BacktestResult",
    "StrategyOutcome",
    "run_backtest",
]
