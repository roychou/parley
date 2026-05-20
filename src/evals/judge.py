"""
src/evals/judge.py holds the LLM-as-judge helper because grounding won't be the only eval that uses one. Consistency will too. 
Probably one function: async def judge(system_prompt: str, user_prompt: str, response_schema: type[BaseModel]) -> BaseModel. 
It wraps an Anthropic SDK call, uses structured outputs (tool use or response prefill), returns a typed Pydantic object. 
The eval itself constructs the prompts and schema; judge.py is just the API wrapper. 
This keeps the judge call site clean and makes it trivial to swap Sonnet for Haiku later if cost forces it.
"""
import json
import logging

from typing import Type, TypeVar
from pydantic import BaseModel
from anthropic import AsyncAnthropic

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# TypeVar allows us to maintain strict typing: the function returns 
# whatever specific BaseModel subclass you pass into it.
T = TypeVar("T", bound=BaseModel)

async def judge(
    client: AsyncAnthropic,
    system_prompt: str,
    user_prompt: str,
    response_schema: Type[T],
    model: str = "claude-sonnet-4-6" # Easy to swap to Haiku here
) -> T:
    """
    A pure wrapper for LLM-as-a-Judge evaluations.
    Forces Claude to respond using a specific Pydantic schema via Tool Use.
    """
    tool_name = "submit_judgment"
    
    # 1. Dynamically build the tool from the Pydantic schema
    tools = [
        {
            "name": tool_name,
            "description": "Submit the final structured evaluation.",
            "input_schema": response_schema.model_json_schema(),
        }
    ]

    # 2. Call the LLM
    response = await client.messages.create(
        model=model,
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
        tools=tools,
        # Force the model to use the tool immediately, bypassing conversational filler
        tool_choice={"type": "tool", "name": tool_name} 
    )

    logger.info(f"api_usage call_site=judge input_tokens={response.usage.input_tokens} output_tokens={response.usage.output_tokens} model={model}")
    
    # 3. Extract the tool use block
    for block in response.content:
        if block.type == "tool_use" and block.name == tool_name:
            # 4. Instantiate and return the exact Pydantic model requested
            return response_schema(**block.input)

    raise RuntimeError(f"Model failed to return the {tool_name} structured output.")