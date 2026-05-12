import asyncio
import logging
import sys

from anthropic import Anthropic
from dotenv import load_dotenv

from datetime import date
from src.schemas import Decision
from src.agents.fundamentals_specialist import run_fundamentals_specialist
from src.agents.technicals_specialist import run_technicals_specialist
from src.synthesis import synthesize

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


async def run_supervisor(ticker: str, as_of: str | None = None) -> Decision:
    """Dispatch specialists in parallel, synthesize their signals into a Decision.

    as_of defaults to today. In backtest mode, pass the historical date.
    """
    as_of = as_of or date.today().isoformat()
    # async def run_fundamentals_specialist(ticker: str, client: Anthropic) -> FundamentalsAnalysis:

    # Instantiate the client at the top level
    try:
        client = Anthropic()
    except Exception as e:
        logger.error(f"Failed to initialize Anthropic client: {e}")
        sys.exit(1)

    signals = await asyncio.gather(
        run_fundamentals_specialist(ticker, client, as_of=as_of),
        run_technicals_specialist(ticker, client, as_of=as_of),
    )

    return synthesize(ticker=ticker, signals=list(signals), as_of=as_of)


if __name__ == "__main__":
    import asyncio

    decision = asyncio.run(run_supervisor("NVDA"))
    print(decision.model_dump_json(indent=2))
