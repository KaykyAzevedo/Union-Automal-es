"""python -m app.scraper → imprime o estoque ao vivo."""
from __future__ import annotations

import logging
import sys

from . import scrape_all


def _brl(cents: int | None) -> str:
    if cents is None:
        return "sem preço"
    reais = f"{cents / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {reais}"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    cars = scrape_all()
    for car in cars:
        print(f"[{car.external_id}] {car.name} | {_brl(car.price_cents)}")
        print(f"    foto: {car.photo_url or '-'}")
        print(f"    url:  {car.url}")
    print(f"\n{len(cars)} carros")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
