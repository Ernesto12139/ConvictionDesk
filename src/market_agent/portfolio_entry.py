"""Native portfolio entry, independent of legacy migration formats."""

import hashlib
import re
from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from .domain import (Account, FrozenModel, InstrumentOverride, PortfolioContent,
                     PortfolioSnapshot, Position, SourceMetadata, portfolio_content_hash, utc)


class EntryAccount(Account):
    account_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        if not value.strip():
            raise ValueError("account name is required")
        return value.strip()


class EntryPosition(Position):
    account_id: str = Field(min_length=1, max_length=100)
    position_id: str = Field(min_length=1, max_length=150)
    note: str = Field(default="", max_length=500)

    @field_validator("symbol")
    @classmethod
    def valid_symbol(cls, value):
        symbol = value.strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9.^/\-]{0,14}", symbol):
            raise ValueError("invalid US equity symbol")
        return symbol


class PortfolioEntry(FrozenModel):
    expected_snapshot_id: str | None = None
    accounts: list[EntryAccount] = Field(min_length=1, max_length=50)
    positions: list[EntryPosition] = Field(max_length=5000)
    watchlist: list[str] = Field(default_factory=list, max_length=500)
    instrument_overrides: dict[str, InstrumentOverride] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_names(self):
        if len({item.name for item in self.accounts}) != len(self.accounts):
            raise ValueError("duplicate account names")
        return self

    def snapshot(self, now: datetime, *, source_kind: Literal["manual_entry", "csv_import"] = "manual_entry",
                 source_hash: str | None = None) -> PortfolioSnapshot:
        now = utc(now)
        source_hash = source_hash or "sha256:" + hashlib.sha256(self.model_dump_json().encode()).hexdigest()
        content = PortfolioContent(
            as_of=now, accounts=tuple(self.accounts),
            positions=tuple(Position(**{
                **item.model_dump(), "position_id": f"{item.account_id}:{item.symbol}",
            }) for item in self.positions),
            watchlist=tuple(self.watchlist), instrument_overrides=self.instrument_overrides,
            source=SourceMetadata(kind=source_kind, imported_at=now, source_hash=source_hash),
        )
        return PortfolioSnapshot(**content.model_dump(), snapshot_id=str(uuid4()),
                                 content_hash=portfolio_content_hash(content))
