"""Order planning + the paper-account safety guard (no live IB; pure logic)."""
import pytest

from src.backtest.strategies import Action
from src.forward.ibkr_execution import (
    NotPaperAccountError,
    OrderPlan,
    _assert_paper,
    plan_orders,
)


class _FakeIB:
    def __init__(self, accounts):
        self._a = accounts

    def managedAccounts(self):  # noqa: N802 (mirrors ib_async)
        return self._a


def test_assert_paper_accepts_paper_and_rejects_live():
    assert _assert_paper(_FakeIB(["DUQ576452"])) == "DUQ576452"
    with pytest.raises(NotPaperAccountError):
        _assert_paper(_FakeIB(["U1234567"]))      # live account
    with pytest.raises(NotPaperAccountError):
        _assert_paper(_FakeIB([]))                # no accounts
    with pytest.raises(NotPaperAccountError):
        _assert_paper(_FakeIB(["DUX1", "U999"]))  # mixed -> refuse


def test_plan_open_sizes_whole_shares_against_equity():
    actions = [Action(kind="OPEN", ticker="ADSK", position_size_pct=0.04)]
    plans = plan_orders(actions, {"ADSK": 250.0}, equity=1_000_000.0, held_shares={})
    # 0.04 * 1,000,000 = 40,000 / 250 = 160 shares
    assert plans == [OrderPlan("ADSK", "BUY", 160, "open")]


def test_plan_close_sells_exactly_held_shares():
    actions = [Action(kind="CLOSE", ticker="WMT", reason="SELL_signal")]
    plans = plan_orders(actions, {"WMT": 110.0}, equity=1_000_000.0, held_shares={"WMT": 73})
    assert plans == [OrderPlan("WMT", "SELL", 73, "SELL_signal")]


def test_plan_skips_unheld_close_no_price_and_subshare():
    actions = [
        Action(kind="CLOSE", ticker="NONE", reason="x"),            # not held -> skip
        Action(kind="OPEN", ticker="NOPX", position_size_pct=0.1),  # no price -> skip
        Action(kind="OPEN", ticker="PRICEY", position_size_pct=0.001),  # < 1 share -> skip
        Action(kind="OPEN", ticker="BAD", position_size_pct=1.5),   # bad size -> skip
    ]
    prices = {"PRICEY": 5000.0, "BAD": 10.0}  # 0.001*1e6=1000 < 5000 -> 0 shares
    plans = plan_orders(actions, prices, equity=1_000_000.0, held_shares={})
    assert plans == []


def test_plan_floors_fractional_shares():
    # 0.05 * 100_000 = 5_000 / 333 = 15.01 -> 15 shares (floor)
    actions = [Action(kind="OPEN", ticker="X", position_size_pct=0.05)]
    plans = plan_orders(actions, {"X": 333.0}, equity=100_000.0, held_shares={})
    assert plans[0].quantity == 15
