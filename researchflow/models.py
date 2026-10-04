"""Bounded schemas for model output and human decisions."""
from pydantic import BaseModel, ConfigDict, Field, field_validator


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outline: list[str] = Field(min_length=2, max_length=6)

    @field_validator("outline")
    @classmethod
    def headings(cls, value):
        if any(not item.strip() or len(item) > 100 or "\n" in item for item in value):
            raise ValueError("Headings must be nonempty single lines of at most 100 characters")
        return [item.strip() for item in value]


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim: str = Field(min_length=12, max_length=700)
    source_id: str = Field(pattern=r"^S[1-6]$")
    quote: str = Field(min_length=20, max_length=500)


class EvidenceBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claims: list[Claim] = Field(max_length=10)


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: bool = Field(strict=True)
    outline: list[str] | None = Field(default=None, min_length=2, max_length=6)

    @field_validator("outline")
    @classmethod
    def headings(cls, value):
        return Plan.headings(value) if value is not None else None
