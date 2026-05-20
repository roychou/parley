import asyncio
from datetime import date
from anthropic import AsyncAnthropic

from src.agents.fundamentals_specialist import run_fundamentals_specialist
from evals.fundamentals import GroundingEval

TICKERS = ["MSFT", "TSLA", "NVDA"]
AS_OF = date(2026, 5, 12)  # or whatever your cached fundamentals date is

async def main():
    client = AsyncAnthropic()

    eval = GroundingEval(client)

    for ticker in TICKERS:
        print(f"\n=== {ticker} ===")
        analysis = await run_fundamentals_specialist(ticker, client, AS_OF)
        print(f"Signal: {analysis.signal} (conf {analysis.confidence})")
        print(f"Reasoning: {analysis.reasoning}")

        result = await eval.run(analysis)
        print(f"\nPassed: {result.passed}  Score: {result.score}")
        print(f"Summary: {result.details.get('summary')}")
        for claim in result.details.get("claims", []):
            marker = "✓" if claim["verdict"] == "GROUNDED" else "✗"
            print(f"  {marker} {claim['claim_text']}")
            if claim["verdict"] == "UNGROUNDED":
                print(f"     → {claim['explanation']}")

if __name__ == "__main__":
    asyncio.run(main())