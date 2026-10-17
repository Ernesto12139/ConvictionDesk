"""Small, explicit generic CSV contract for portfolio onboarding."""

import csv
import hashlib
import io
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, Field, ValidationError

from .portfolio_entry import PortfolioEntry


MAX_CSV_BYTES = 1_000_000
ALIASES = {"shares": "quantity", "average_cost": "avg_cost", "row_type": "type"}
COLUMNS = {"type", "account", "symbol", "quantity", "avg_cost", "cash", "sector", "theme"}
REQUIRED = {"account", "symbol", "quantity", "avg_cost"}
CSV_TEMPLATE = (
    "type,account,symbol,quantity,avg_cost,cash,sector,theme\r\n"
    "position,账户A,AAPL,10,150,,信息技术,科技\r\n"
    "position,账户B,AAPL,2,155,,信息技术,科技\r\n"
    "cash,账户A,,,,500,,\r\n"
    "cash,账户B,,,,250,,\r\n"
)


class CSVTextRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_CSV_BYTES)


class CSVConfirmRequest(CSVTextRequest):
    expected_snapshot_id: str | None = None
    source_hash: str


class CSVImportError(ValueError):
    def __init__(self, code: str, row: int | None = None):
        self.code = code
        self.row = row
        super().__init__(code)


def _amount(value: str, *, positive: bool, row: int) -> str:
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise CSVImportError("INVALID_NUMBER", row) from exc
    if not number.is_finite() or number < 0 or (positive and number == 0):
        raise CSVImportError("INVALID_NUMBER", row)
    return value


def parse_portfolio_csv(text: str, expected_snapshot_id: str | None = None) -> PortfolioEntry:
    if not text.strip() or len(text.encode("utf-8")) > MAX_CSV_BYTES or "\x00" in text:
        raise CSVImportError("INVALID_FILE")
    try:
        reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff"), newline=""), strict=True,
                                restkey="__extra")
        if reader.fieldnames is None:
            raise CSVImportError("INVALID_HEADER")
        columns = [ALIASES.get(name.strip().lower(), name.strip().lower())
                   for name in reader.fieldnames]
        if (len(columns) != len(set(columns)) or not REQUIRED <= set(columns)
                or set(columns) - COLUMNS):
            raise CSVImportError("INVALID_HEADER")
        accounts: dict[str, dict] = {}
        cash_seen: set[str] = set()
        positions: list[dict] = []
        seen_positions: set[tuple[str, str]] = set()
        classifications: dict[str, dict[str, str | None]] = {}
        for raw in reader:
            line = reader.line_num
            if line > 5002 or raw.get("__extra") is not None or None in raw:
                raise CSVImportError("INVALID_ROW", line)
            row = {column: (raw.get(original) or "").strip()
                   for original, column in zip(reader.fieldnames, columns)}
            if not any(row.values()):
                continue
            account = row["account"]
            if not account or len(account) > 80:
                raise CSVImportError("INVALID_ACCOUNT", line)
            if account not in accounts:
                if len(accounts) >= 50:
                    raise CSVImportError("TOO_MANY_ACCOUNTS", line)
                accounts[account] = {"account_id": f"csv-{len(accounts) + 1}",
                                     "name": account, "cash": "0"}
            kind = row.get("type", "").lower()
            if not kind:
                kind = "cash" if row.get("cash") and not any(
                    row.get(key) for key in ("symbol", "quantity", "avg_cost")) else "position"
            if kind == "cash":
                if (account in cash_seen or not row.get("cash") or any(
                        row.get(key) for key in ("symbol", "quantity", "avg_cost", "sector", "theme"))):
                    raise CSVImportError("INVALID_CASH_ROW", line)
                accounts[account]["cash"] = _amount(row["cash"], positive=False, row=line)
                cash_seen.add(account)
                continue
            if kind != "position" or row.get("cash"):
                raise CSVImportError("INVALID_ROW", line)
            symbol = row["symbol"].upper()
            if not symbol or not row["quantity"] or not row["avg_cost"]:
                raise CSVImportError("INVALID_ROW", line)
            key = (account, symbol)
            if key in seen_positions:
                raise CSVImportError("DUPLICATE_POSITION", line)
            seen_positions.add(key)
            positions.append({"position_id": f"{accounts[account]['account_id']}:{symbol}",
                              "account_id": accounts[account]["account_id"],
                              "symbol": symbol,
                              "quantity": _amount(row["quantity"], positive=True, row=line),
                              "avg_cost": _amount(row["avg_cost"], positive=False, row=line)})
            classification = classifications.setdefault(symbol, {"sector": None, "theme": None})
            for field in ("sector", "theme"):
                value = row.get(field, "") or None
                if value and len(value) > 80:
                    raise CSVImportError("INVALID_CLASSIFICATION", line)
                if value and classification[field] and classification[field] != value:
                    raise CSVImportError("CONFLICTING_CLASSIFICATION", line)
                classification[field] = classification[field] or value
        if not accounts:
            raise CSVImportError("EMPTY_PORTFOLIO")
        overrides = {symbol: fields for symbol, fields in classifications.items()
                     if fields["sector"] or fields["theme"]}
        return PortfolioEntry.model_validate({"expected_snapshot_id": expected_snapshot_id,
                                              "accounts": list(accounts.values()),
                                              "positions": positions,
                                              "instrument_overrides": overrides})
    except csv.Error as exc:
        raise CSVImportError("MALFORMED_CSV") from exc
    except ValidationError as exc:
        raise CSVImportError("INVALID_ROW") from exc


def csv_source_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
