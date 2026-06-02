"""
Forward-session reporting — a human-readable weekly digest.

The session returns a terse summary dict and the PaperBook persists a full audit
trail (decision log with rationale, positions, trades, equity). `session_digest`
renders those into a legible markdown brief — what was decided and why, what got
skipped, current positions, and P&L — so the live record is reviewable at a glance
over the months the forward clock runs. `write_digest` saves one file per session.
"""
from __future__ import annotations

from pathlib import Path

DIGEST_DIR = Path("data/forward/digests")


def _truncate(text: str, n: int = 240) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def session_digest(book, summary: dict) -> str:
    """Render one forward session into a markdown brief. Pure: reads the (post-session)
    PaperBook + the session summary, returns a string."""
    as_of = summary.get("as_of", "?")
    equity = summary.get("equity", book.cash)
    ret_pct = (equity / book.initial_cash - 1) * 100 if book.initial_cash else 0.0
    skipped = summary.get("skipped", [])
    todays = [d for d in book.decision_log if d.get("date") == as_of]

    lines = [
        f"# Forward session — {as_of}",
        "",
        f"- screened {summary.get('candidates', 0)} candidates → "
        f"decided {summary.get('decided', 0)}, skipped {len(skipped)} (missing data)",
        f"- directions: {summary.get('directions', {})}",
        f"- equity ${equity:,.0f} ({ret_pct:+.2f}% vs ${book.initial_cash:,.0f} start) | "
        f"cash ${book.cash:,.0f} | {len(book.positions)} open positions",
        f"- dividends received (cumulative): ${book.dividends_received:,.2f}",
    ]
    if skipped:
        lines.append(f"- skipped (no usable data): {', '.join(sorted(skipped))}")

    lines += ["", "## Decisions this session"]
    if todays:
        for d in sorted(todays, key=lambda x: (x.get("direction", ""), x.get("ticker", ""))):
            conf = d.get("confidence", 0.0)
            lines.append(
                f"- **{d.get('ticker')} {d.get('direction')}** (conf {conf:.2f}) — "
                f"{_truncate(d.get('rationale', ''))}"
            )
    else:
        lines.append("- (none — the screen produced no decidable candidates this week)")

    lines += ["", "## Open positions"]
    if book.positions:
        for t, pos in sorted(book.positions.items()):
            lines.append(
                f"- {t}: ${pos.get('dollars_at_entry', 0):,.0f} @ {pos.get('entry_price')} "
                f"(opened {pos.get('entry_date')})"
            )
    else:
        lines.append("- (flat)")

    closed = [t for t in book.closed_trades if t.get("exit_date") == as_of]
    if closed:
        lines += ["", "## Closed this session"]
        for t in closed:
            lines.append(
                f"- {t.get('ticker')}: {t.get('realized_pnl_pct', 0) * 100:+.2f}% "
                f"({t.get('exit_reason')})"
            )

    return "\n".join(lines) + "\n"


def write_digest(as_of: str, digest: str, directory: Path = DIGEST_DIR) -> Path:
    """Persist the digest to data/forward/digests/<as_of>.md and return the path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{as_of}.md"
    path.write_text(digest)
    return path
