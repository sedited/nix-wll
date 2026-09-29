"""Persistent request reservations and spend accounting."""
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path


MICRODOLLARS = 1_000_000
THRESHOLD_TOKENS = 272_000
RATES = {
    "gpt-6-luna": (Decimal("0.1"), Decimal("0.01"),
                   Decimal("0.125"), Decimal("0.5")),
    "gpt-6.1-sol": (Decimal("2"), Decimal("0.1"),
                    Decimal("2.5"), Decimal("10")),
}
SNAPSHOT_MODEL = re.compile(r"(gpt-6-luna|gpt-6\.1-sol)-\d{4}-\d{2}-\d{2}")


class BudgetExceeded(Exception):
    """A request reservation would exceed the configured monthly limit."""


def _usage_values(usage):
    details = usage.get("input_tokens_details") or {}
    input_tokens = usage.get("input_tokens")
    cached_tokens = details.get("cached_tokens", usage.get("cached_tokens", 0))
    cache_write_tokens = details.get("cache_write_tokens",
                                     usage.get("cache_write_tokens"))
    output_tokens = usage.get("output_tokens")
    if input_tokens is None or output_tokens is None:
        return None
    try:
        input_tokens, cached_tokens, output_tokens = (
            int(value) for value in (input_tokens, cached_tokens, output_tokens))
        cache_write_tokens = (None if cache_write_tokens is None
                              else int(cache_write_tokens))
    except (TypeError, ValueError):
        return None
    if (input_tokens < 0 or cached_tokens < 0 or output_tokens < 0
            or (cache_write_tokens is not None and cache_write_tokens < 0)):
        return None
    if cached_tokens > input_tokens:
        return None
    if cache_write_tokens is not None and cached_tokens + cache_write_tokens > input_tokens:
        return None
    return input_tokens, cached_tokens, cache_write_tokens, output_tokens


def price_usd(model, usage):
    """Return a conservative per-response price, or None if usage is unknown.

    Missing cache-write counts use the cache-write rate for all uncached input
    tokens. The caller can detect that estimate through usage completeness.
    """
    rates = _rates_for_model(model)
    values = _usage_values(usage)
    if rates is None or values is None:
        return None
    input_tokens, cached_tokens, cache_write_tokens, output_tokens = values
    if cache_write_tokens is None:
        # Treat every uncached input token as a cache write, the highest input rate.
        cache_write_tokens = input_tokens - cached_tokens
    multiplier = Decimal(2) if input_tokens > THRESHOLD_TOKENS else Decimal(1)
    input_rate, cached_rate, write_rate, output_rate = rates
    if multiplier == 2:
        output_rate *= Decimal("1.5")
    dollars = (
        Decimal(input_tokens - cached_tokens - cache_write_tokens) * input_rate * multiplier
        + Decimal(cached_tokens) * cached_rate * multiplier
        + Decimal(cache_write_tokens) * write_rate * multiplier
        + Decimal(output_tokens) * output_rate
    ) / Decimal(1_000_000)
    return dollars.quantize(Decimal("0.000001"), rounding=ROUND_CEILING)


def _rates_for_model(model):
    rates = RATES.get(model)
    if rates is not None:
        return rates
    match = SNAPSHOT_MODEL.fullmatch(model or "")
    return RATES.get(match.group(1)) if match else None


def _micros(dollars):
    try:
        amount = Decimal(str(dollars))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("spend values must be finite and nonnegative") from error
    if not amount.is_finite() or amount < 0:
        raise ValueError("spend values must be finite and nonnegative")
    value = amount * MICRODOLLARS
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _limit_micros(limit, name):
    if limit is None:
        return None
    amount = _micros(limit)
    if amount == 0:
        raise ValueError(f"{name} must be positive")
    return amount


def _month_now():
    return datetime.now(timezone.utc).strftime("%Y-%m")


class Ledger:
    """A SQLite ledger that reserves monthly spend before API requests."""

    def __init__(self, path, review_limit_usd=1.00, monthly_limit_usd=None,
                 input_padding_tokens=1024):
        self.path = str(path)
        self.review_limit_micros = _limit_micros(review_limit_usd, "review_limit_usd")
        self.monthly_limit_micros = _limit_micros(monthly_limit_usd, "monthly_limit_usd")
        self.input_padding_tokens = int(input_padding_tokens)
        if self.input_padding_tokens < 0:
            raise ValueError("input_padding_tokens must be nonnegative")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS requests (
                token TEXT PRIMARY KEY,
                review_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                model TEXT NOT NULL,
                month TEXT NOT NULL,
                response_id TEXT,
                status TEXT NOT NULL,
                usage_json TEXT,
                cost_micros INTEGER,
                reserved_micros INTEGER NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS requests_month ON requests(month)")
            db.execute("CREATE INDEX IF NOT EXISTS requests_review ON requests(review_id)")

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            db.execute("PRAGMA busy_timeout = 30000")
            yield db
        finally:
            db.close()

    @staticmethod
    def _payload_bytes(payload):
        if isinstance(payload, bytes):
            return len(payload)
        if isinstance(payload, str):
            return len(payload.encode("utf-8"))
        return len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    def _reserve_cost(self, model, payload):
        rates = _rates_for_model(model)
        if rates is None:
            raise ValueError(f"No configured rates for model {model!r}")
        try:
            request = json.loads(payload) if isinstance(payload, (bytes, str)) else payload
            max_output_tokens = int(request["max_output_tokens"])
        except (TypeError, ValueError, KeyError):
            raise ValueError("payload must include max_output_tokens") from None
        if max_output_tokens < 0:
            raise ValueError("max_output_tokens must be nonnegative")
        # Byte length plus padding is a coarse preflight estimate, not a guarantee
        # of the eventual invoice amount.
        input_tokens = self._payload_bytes(payload) + self.input_padding_tokens
        multiplier = Decimal(2) if input_tokens > THRESHOLD_TOKENS else Decimal(1)
        input_rate = max(rates[:3]) * multiplier
        output_rate = rates[3] * (Decimal("1.5") if multiplier == 2 else Decimal(1))
        dollars = (Decimal(input_tokens) * input_rate
                   + Decimal(max_output_tokens) * output_rate) / Decimal(1_000_000)
        return int((dollars * MICRODOLLARS).to_integral_value(rounding=ROUND_CEILING))

    def reserve(self, stage, model, payload, review_id, reserve_floor_usd=0):
        """Reserve an upper cost estimate and return the request token."""
        amount = self._reserve_cost(model, payload)
        floor = _micros(reserve_floor_usd)
        month = _month_now()
        token = str(uuid.uuid4())
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if self.review_limit_micros is not None:
                review_current = db.execute(
                    "SELECT COALESCE(SUM(CASE WHEN cost_micros IS NOT NULL "
                    "THEN cost_micros ELSE reserved_micros END), 0) "
                    "FROM requests WHERE review_id=? AND status IN "
                    "('reserved','uncertain','unknown','settled')", (review_id,),
                ).fetchone()[0]
                if review_current + amount + floor > self.review_limit_micros:
                    db.rollback()
                    raise BudgetExceeded("review spend limit would be exceeded")
            if self.monthly_limit_micros is not None:
                current = db.execute(
                    "SELECT COALESCE(SUM(CASE WHEN cost_micros IS NOT NULL "
                    "THEN cost_micros ELSE reserved_micros END), 0) "
                    "FROM requests WHERE month=? AND status IN ('reserved','uncertain','unknown','settled')",
                    (month,),
                ).fetchone()[0]
                if current + amount > self.monthly_limit_micros:
                    db.rollback()
                    raise BudgetExceeded("monthly spend limit would be exceeded")
            db.execute("INSERT INTO requests VALUES (?, ?, ?, ?, ?, NULL, 'reserved', NULL, NULL, ?)",
                       (token, review_id, stage, model, month, amount))
            db.commit()
        return token

    def settle(self, token, response):
        """Settle a reservation from a Responses API result; return whether new."""
        response_id = response.get("id")
        usage = response.get("usage") or {}
        details = usage.get("input_tokens_details") or {}
        cached_tokens = details.get("cached_tokens", usage.get("cached_tokens", 0))
        cache_write_tokens = details.get("cache_write_tokens",
                                         usage.get("cache_write_tokens"))
        actual_model = response.get("model")
        usage_model = actual_model
        usage_json = json.dumps({
            "input_tokens": usage.get("input_tokens"),
            "cached_tokens": cached_tokens,
            "cache_write_tokens": cache_write_tokens,
            "complete": (usage.get("input_tokens") is not None
                         and usage.get("output_tokens") is not None
                         and cache_write_tokens is not None),
            "output_tokens": usage.get("output_tokens"),
        }, sort_keys=True)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT model, status, reserved_micros FROM requests WHERE token=?",
                             (token,)).fetchone()
            if row is None:
                db.rollback()
                raise KeyError(token)
            model, status, reserved = row
            if status != "reserved":
                db.rollback()
                return False
            if response_id is not None:
                duplicate = db.execute("SELECT 1 FROM requests WHERE response_id=?",
                                       (str(response_id),)).fetchone()
                if duplicate:
                    db.execute("UPDATE requests SET response_id=?, status='duplicate', "
                               "reserved_micros=0 WHERE token=?", (str(response_id), token))
                    db.commit()
                    return False
            selected_model = usage_model or model
            cost = price_usd(selected_model, usage)
            db.execute("UPDATE requests SET response_id=?, model=?, status=?, usage_json=?, "
                       "cost_micros=?, reserved_micros=? WHERE token=?",
                       (str(response_id) if response_id is not None else None,
                        selected_model, "settled" if cost is not None else "unknown",
                        usage_json, None if cost is None else _micros(cost),
                        0 if cost is not None else reserved, token))
            db.commit()
        return True

    def fail(self, token, charged_unknown=True):
        """Retain an uncertain reservation, or release a known no-charge failure."""
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM requests WHERE token=?", (token,)).fetchone()
            if row is None:
                db.rollback()
                raise KeyError(token)
            if row[0] != "reserved":
                db.rollback()
                return False
            if charged_unknown:
                db.execute("UPDATE requests SET status='uncertain' WHERE token=?", (token,))
            else:
                db.execute("UPDATE requests SET status='released', reserved_micros=0 "
                           "WHERE token=?", (token,))
            db.commit()
        return True

    def summary(self, month=None, review_id=None):
        if month is None and review_id is None:
            month = _month_now()
        with self._connection() as db:
            query = (
                "SELECT COALESCE(SUM(cost_micros), 0), "
                "COALESCE(SUM(CASE WHEN status IN ('reserved','uncertain','unknown') "
                "THEN reserved_micros ELSE 0 END), 0), "
                "SUM(CASE WHEN status IN ('reserved','uncertain','unknown') THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN status IN ('settled','unknown') AND "
                "json_extract(usage_json, '$.complete')=0 THEN 1 ELSE 0 END) "
                "FROM requests WHERE 1=1")
            params = []
            if month is not None:
                query += " AND month=?"
                params.append(month)
            if review_id is not None:
                query += " AND review_id=?"
                params.append(review_id)
            known, reserved, unknown, incomplete = db.execute(query, params).fetchone()
        return {"month": month,
                "estimated_total_usd": (known + reserved) / MICRODOLLARS,
                "reserved_total_usd": reserved / MICRODOLLARS,
                "unknown_request_count": unknown or 0,
                "incomplete_usage_count": incomplete or 0,
                "usage_complete": not (unknown or incomplete)}


class RequestBudget:
    """Apply stage-specific headroom while sharing a review ledger."""

    def __init__(self, ledger, review_id):
        self.ledger = ledger
        self.review_id = review_id

    def reserve(self, stage, data):
        model = data["model"]
        limit = self.ledger.review_limit_micros
        scale = (1 if limit is None else
                 min(1, limit / (0.60 * MICRODOLLARS)))
        if stage == "verifier":
            headroom = 0.02 * scale
        elif stage == "collator":
            headroom = 0
        else:
            headroom = 0.08 * scale
        return self.ledger.reserve(stage, model, data, self.review_id,
                                   reserve_floor_usd=headroom)

    def settle(self, token, response):
        return self.ledger.settle(token, response)

    def fail(self, token, charged_unknown=True):
        return self.ledger.fail(token, charged_unknown=charged_unknown)

    def summary(self):
        return self.ledger.summary(review_id=self.review_id)
