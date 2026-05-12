from pydantic import BaseModel, Field


class GetFundamentalsInput(BaseModel):
    ticker: str = Field(description="Stock ticker symbol, e.g. 'TSLA'")


class GetTechnicalsInput(BaseModel):
    ticker: str = Field(description="Stock ticker symbol, e.g. 'TSLA'")
