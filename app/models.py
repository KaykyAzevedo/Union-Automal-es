"""Objetos expostos a serviços e templates (contrato do painel)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from .db import from_iso


@dataclass
class Car:
    id: int
    external_id: str
    name: str
    photo_url: str | None
    price_cents: int | None
    url: str
    posted: bool
    active: bool
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    missing_count: int = 0
    sold_at: datetime | None = None

    @property
    def sold(self) -> bool:
        return self.sold_at is not None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Car":
        return cls(
            id=row["id"],
            external_id=row["external_id"],
            name=row["name"],
            photo_url=row["photo_url"],
            price_cents=row["price_cents"],
            url=row["url"],
            posted=bool(row["posted"]),
            active=bool(row["active"]),
            first_seen=from_iso(row["first_seen"]),
            last_seen=from_iso(row["last_seen"]),
            missing_count=row["missing_count"],
            sold_at=from_iso(row["sold_at"]),
        )


@dataclass
class Ticket:
    id: int
    created_at: datetime
    status: str
    done_at: datetime | None
    car: Car

    @property
    def done(self) -> bool:
        return self.status == "done"


@dataclass
class PriceAlert:
    id: int
    created_at: datetime
    old_price_cents: int
    new_price_cents: int
    dismissed: bool
    car: Car

    @property
    def drop_cents(self) -> int:
        return self.old_price_cents - self.new_price_cents


@dataclass
class SoldAlert:
    id: int
    created_at: datetime
    dismissed: bool
    car: Car


@dataclass
class PriceHistory:
    id: int
    car_id: int
    price_cents: int | None
    recorded_at: datetime
