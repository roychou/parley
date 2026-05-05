import pandas as pd
import pytest
from src.data.technicals import sma


def test_sma_known_values():
    prices = pd.Series(range(1, 21), dtype=float)
    result = sma(prices, window=20)
    assert result.iloc[-1] == pytest.approx(10.5)
    assert result.iloc[:-1].isna().all()

def test_sma_known_values_list():
    # Average of 1..20 is 10.5
    prices = list(range(1, 21))
    result = sma(prices, window=20)
    assert result.iloc[-1] == pytest.approx(10.5)
    assert result.iloc[:-1].isna().all()
