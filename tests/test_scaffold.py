"""Offline tests for the map-reduce scaffold (stubbed LLM — no network)."""
import pytest

from src.agents.scaffold import (
    ScaffoldConfig,
    analyze_text,
    chunk_text,
    estimate_tokens,
)

_CFG = ScaffoldConfig(
    root_model="ROOT",
    leaf_model="LEAF",
    max_single_call_tokens=1000,  # small thresholds so tests are fast/clear
    chunk_tokens=500,
    overlap_tokens=20,
)


def _recording_llm():
    calls: list[tuple[str, str, str]] = []

    async def llm(model, system, user):
        calls.append((model, system, user))
        return f"[{model} out]"

    return calls, llm


# ==========================================
# chunking
# ==========================================


def test_small_text_single_chunk():
    assert chunk_text("a b c", chunk_tokens=500, overlap_tokens=20) == ["a b c"]


def test_chunks_cover_and_overlap():
    text = " ".join(f"w{i}" for i in range(4000))  # well over one window
    chunks = chunk_text(text, chunk_tokens=500, overlap_tokens=50)
    assert len(chunks) > 1
    # No chunk splits a token (every chunk starts/ends on a whole "wN").
    for c in chunks:
        assert not c.startswith(" ") and not c.endswith(" ")
    # Coverage: first and last tokens are present somewhere.
    joined = " ".join(chunks)
    assert "w0" in joined and "w3999" in joined


# ==========================================
# single-call path (small text)
# ==========================================


@pytest.mark.asyncio
async def test_single_call_uses_root_model_only():
    calls, llm = _recording_llm()
    out = await analyze_text(
        "short text",
        analysis_system="ANALYZE",
        map_system="MAP",
        llm=llm,
        config=_CFG,
    )
    assert out == "[ROOT out]"
    assert len(calls) == 1
    model, system, _ = calls[0]
    assert model == "ROOT" and system == "ANALYZE"


# ==========================================
# map-reduce path (large text)
# ==========================================


@pytest.mark.asyncio
async def test_large_text_maps_with_leaf_then_reduces_with_root():
    big = " ".join(f"w{i}" for i in range(3000))  # > max_single_call_tokens
    assert estimate_tokens(big) > _CFG.max_single_call_tokens
    calls, llm = _recording_llm()

    out = await analyze_text(
        big, analysis_system="ANALYZE", map_system="MAP", llm=llm, config=_CFG
    )

    leaf_calls = [c for c in calls if c[0] == "LEAF"]
    root_calls = [c for c in calls if c[0] == "ROOT"]
    # Every chunk mapped with the leaf model + MAP prompt.
    assert len(leaf_calls) >= 2
    assert all(system == "MAP" for _, system, _ in leaf_calls)
    # Exactly one reduce with the root model + ANALYZE prompt, over the observations.
    assert len(root_calls) == 1
    assert root_calls[0][1] == "ANALYZE"
    assert "[LEAF out]" in root_calls[0][2]  # reduce sees the mapped observations
    assert out == "[ROOT out]"
