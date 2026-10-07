"""F13: vendas e financeiro da equipe (/equipe/vendas e /equipe/carro/{id}).

Regras (só aqui):
- Venda = carro com sold_at preenchido (active=0). Mês da venda = mês de sold_at em
  America/Sao_Paulo. Carro que reaparece no site perde o sold_at e sai das vendas
  sozinho. Carros demo-* ficam fora de tudo.
- sale = sale_price_cents (valor real, digitado) ou price_cents (último preço do site →
  sale_is_estimate=True). expenses = soma de car_expenses.
- profit = sale − cost − expenses, só quando cost_cents E sale existem. Sem custo → profit
  None e o carro conta em `missing_cost`: entra no faturamento, mas não em custo/despesas/
  lucro do mês. Sem valor de venda (sob consulta no site e nada digitado) → profit None e
  conta em `missing_sale`: também fora de custo/despesas/lucro. Assim, no mês,
  lucro = faturamento dos carros completos − custo − despesas.
- Gasto do mês (spend) = custo + despesas dos carros VENDIDOS no mês (regime por carro vendido).
- Estoque: invested_cents = custo + despesas dos carros ativos.
- margin_pct = lucro / faturamento dos carros completos (1 casa); None sem base.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from .. import db
from ..scraper import brand_from_name
from .sold import DEMO_PREFIX

# R$ 20 milhões: as colunas são INTEGER (int4 no Postgres, máx. 2.147.483.647 centavos)
MAX_MONEY_CENTS = 2_000_000_000
MAX_ID = 2**31 - 1  # ids são int4 no Postgres; acima disso o carro/despesa não existe
MAX_DESCRIPTION = 200
HISTORY_MONTHS = 6
MONTHS = ("Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
          "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro")
_MONEY_RE = re.compile(r"(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{1,2}))?")
_NOT_DEMO = f"external_id NOT LIKE '{DEMO_PREFIX}%'"


def brand_of(name: str | None) -> str:
    """Marca em caixa de exibição para cars.brand ("Chevrolet", "Land Rover", "BMW"; vazio → "Outros")."""
    return brand_from_name(name)


# ---- dinheiro -------------------------------------------------------------------

def parse_money(text: str | None) -> int | None:
    """Reais digitados → centavos: '45000', '45.000', '45.000,50', 'R$ 45.000,5'.

    Vazio/None → None. Inválido, negativo ou acima de MAX_MONEY_CENTS → ValueError."""
    raw = (text or "").strip()
    if raw.upper().startswith("R$"):
        raw = raw[2:].strip()
    if not raw:
        return None
    m = _MONEY_RE.fullmatch(raw)
    if not m:
        raise ValueError(f"valor inválido: {text!r}")
    cents = int(m.group(1).replace(".", "")) * 100 + int((m.group(2) or "0").ljust(2, "0"))
    if cents > MAX_MONEY_CENTS:
        raise ValueError(f"valor acima do limite: {text!r}")
    return cents


# ---- meses ----------------------------------------------------------------------

def _shift(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def parse_month(value: str | None, today: date) -> tuple[int, int]:
    """'YYYY-MM' → (ano, mês); ausente/inválido → mês de `today`."""
    m = re.fullmatch(r"(\d{4})-(\d{2})", (value or "").strip())
    if m and 1 <= int(m.group(2)) <= 12 and int(m.group(1)) >= 2000:
        return int(m.group(1)), int(m.group(2))
    return today.year, today.month


def month_info(year: int, month: int, today: date) -> dict:
    nxt = _shift(year, month, 1)
    return {
        "key": _key(year, month),
        "label": f"{MONTHS[month - 1]}/{year}",
        "prev": _key(*_shift(year, month, -1)),
        "next": _key(*nxt) if nxt <= (today.year, today.month) else None,
    }


# ---- leitura ----------------------------------------------------------------------

def _local(value: str | None) -> datetime | None:
    dt = db.from_iso(value)
    return dt.astimezone(db.TZ) if dt else None


def _since(row) -> date:
    """Entrada no estoque: min(first_seen, listed_at) — mesma regra do aging."""
    first = _local(row["first_seen"]).date()
    listed = date.fromisoformat(row["listed_at"][:10]) if row["listed_at"] else None
    return min(first, listed) if listed else first


def _expenses_by_car(conn) -> dict[int, int]:
    return {r["car_id"]: int(r["total"] or 0) for r in conn.execute(
        "SELECT car_id, SUM(amount_cents) AS total FROM car_expenses GROUP BY car_id")}


def _sale(row, expenses: int) -> dict:
    """Números de um carro: {sale_cents, sale_is_estimate, cost_cents, expenses_cents, profit_cents}."""
    manual = row["sale_price_cents"]
    sale = manual if manual is not None else row["price_cents"]
    cost = row["cost_cents"]
    profit = sale - cost - expenses if cost is not None and sale is not None else None
    return {"sale_cents": sale, "sale_is_estimate": manual is None, "cost_cents": cost,
            "expenses_cents": expenses, "profit_cents": profit}


def _sales(conn) -> list[dict]:
    """Todas as vendas (não demo), mais recentes primeiro."""
    expenses = _expenses_by_car(conn)
    sales = []
    for r in conn.execute(
            f"""SELECT id, name, photo_url, brand, price_cents, cost_cents, sale_price_cents, sold_at,
                       first_seen, listed_at
                FROM cars WHERE sold_at IS NOT NULL AND {_NOT_DEMO}"""):
        sold_at = _local(r["sold_at"])
        sales.append({
            "car": {"id": r["id"], "name": r["name"], "photo_url": r["photo_url"],
                    "brand": r["brand"] or brand_of(r["name"])},
            "sold_at": sold_at,
            **_sale(r, expenses.get(r["id"], 0)),
            "days_to_sell": max(0, (sold_at.date() - _since(r)).days),
        })
    sales.sort(key=lambda s: (s["sold_at"], s["car"]["id"]), reverse=True)
    return sales


def _in_month(sales: list[dict], year: int, month: int) -> list[dict]:
    return [s for s in sales if (s["sold_at"].year, s["sold_at"].month) == (year, month)]


def _totals(sales: list[dict]) -> dict:
    """Somas de um conjunto de vendas. Custo/despesas/lucro só dos carros completos
    (com custo e com valor de venda)."""
    complete = [s for s in sales if s["profit_cents"] is not None]
    return {
        "sold_count": len(sales),
        "revenue_cents": sum(s["sale_cents"] or 0 for s in sales),
        "cost_cents": sum(s["cost_cents"] for s in complete),
        "expenses_cents": sum(s["expenses_cents"] for s in complete),
        "profit_cents": sum(s["profit_cents"] for s in complete),
        "missing_cost": sum(1 for s in sales if s["cost_cents"] is None),
        "missing_sale": sum(1 for s in sales if s["sale_cents"] is None),
        "costed_revenue_cents": sum(s["sale_cents"] for s in complete),
    }


def _brands(sales: list[dict]) -> list[dict]:
    groups: dict[str, dict] = {}
    for s in sales:
        g = groups.setdefault(s["car"]["brand"], {"brand": s["car"]["brand"], "count": 0, "revenue_cents": 0})
        g["count"] += 1
        g["revenue_cents"] += s["sale_cents"] or 0
    for g in groups.values():
        g["share_pct"] = round(g["count"] * 100 / len(sales), 1)
    return sorted(groups.values(), key=lambda g: (-g["count"], -g["revenue_cents"], g["brand"]))


def sales_view(conn, mes: str | None = None, today: date | None = None) -> dict:
    """Contexto de GET /equipe/vendas: {month, summary, brands, sales, history}."""
    today = today or db.now().date()
    year, month = parse_month(mes, today)
    all_sales = _sales(conn)
    sales = _in_month(all_sales, year, month)
    totals = _totals(sales)
    costed_revenue = totals.pop("costed_revenue_cents")

    expenses = _expenses_by_car(conn)
    stock = conn.execute(f"SELECT id, cost_cents FROM cars WHERE active = 1 AND {_NOT_DEMO}").fetchall()
    days = [s["days_to_sell"] for s in sales]
    summary = {
        **totals,
        "margin_pct": round(totals["profit_cents"] * 100 / costed_revenue, 1) if costed_revenue else None,
        "avg_days_to_sell": (2 * sum(days) + len(days)) // (2 * len(days)) if days else None,  # meio p/ cima
        "invested_cents": sum((r["cost_cents"] or 0) + expenses.get(r["id"], 0) for r in stock),
        "in_stock": len(stock),
    }

    history = []
    for delta in range(-(HISTORY_MONTHS - 1), 1):
        y, m = _shift(year, month, delta)
        t = _totals(_in_month(all_sales, y, m))
        history.append({"key": _key(y, m), "label": MONTHS[m - 1][:3].lower(), "sold_count": t["sold_count"],
                        "revenue_cents": t["revenue_cents"], "spend_cents": t["cost_cents"] + t["expenses_cents"],
                        "profit_cents": t["profit_cents"]})
    return {"month": month_info(year, month, today), "summary": summary, "brands": _brands(sales),
            "sales": sales, "history": history}


def car_view(conn, car_id: int) -> dict | None:
    """Contexto de GET /equipe/carro/{id}: {car, expenses, totals}; None se o carro não existe."""
    if not valid_id(car_id):
        return None
    r = conn.execute(
        """SELECT id, name, photo_url, brand, price_cents, active, sold_at, cost_cents, sale_price_cents
           FROM cars WHERE id = ?""", (car_id,)).fetchone()
    if r is None:
        return None
    expenses = [{"id": e["id"], "description": e["description"], "amount_cents": e["amount_cents"],
                 "created_at": _local(e["created_at"])}
                for e in conn.execute(
                    "SELECT id, description, amount_cents, created_at FROM car_expenses WHERE car_id = ? "
                    "ORDER BY created_at DESC, id DESC", (car_id,))]
    numbers = _sale(r, sum(e["amount_cents"] for e in expenses))
    sold_at = _local(r["sold_at"])
    return {
        "car": {"id": r["id"], "name": r["name"], "photo_url": r["photo_url"],
                "brand": r["brand"] or brand_of(r["name"]), "price_cents": r["price_cents"],
                "active": bool(r["active"]), "sold": sold_at is not None, "sold_at": sold_at,
                "cost_cents": r["cost_cents"], "sale_price_cents": r["sale_price_cents"]},
        "expenses": expenses,
        "totals": {k: numbers[k] for k in ("expenses_cents", "sale_cents", "sale_is_estimate", "profit_cents")},
    }


# ---- escrita (True = gravou; False = inválido, nada gravado) -------------------------

def valid_id(value: int) -> bool:
    """Ids fora da faixa do banco não existem (e estourariam o driver): tratar como 404."""
    return 0 < value <= MAX_ID


def _set_amount(conn, car_id: int, column: str, text: str | None, *, allow_zero: bool) -> bool:
    try:
        cents = parse_money(text)
    except ValueError:
        return False
    if cents == 0 and not allow_zero:
        return False
    conn.execute(f"UPDATE cars SET {column} = ? WHERE id = ?", (cents, car_id))
    conn.commit()
    return True


def set_cost(conn, car_id: int, text: str | None) -> bool:
    """Valor pago na compra. Vazio = limpar (carro volta a contar em missing_cost)."""
    return _set_amount(conn, car_id, "cost_cents", text, allow_zero=True)


def set_sale_price(conn, car_id: int, text: str | None) -> bool:
    """Valor real da venda. Vazio = voltar ao último preço do site."""
    return _set_amount(conn, car_id, "sale_price_cents", text, allow_zero=False)


def add_expense(conn, car_id: int, description: str | None, amount: str | None) -> bool:
    description = " ".join((description or "").replace("\x00", "").split())  # Postgres rejeita NUL
    try:
        cents = parse_money(amount)
    except ValueError:
        return False
    if not description or len(description) > MAX_DESCRIPTION or not cents:
        return False
    conn.execute("INSERT INTO car_expenses (car_id, description, amount_cents, created_at) VALUES (?, ?, ?, ?)",
                 (car_id, description, cents, db.to_iso(db.now())))
    conn.commit()
    return True


def delete_expense(conn, car_id: int, expense_id: int) -> bool:
    if not valid_id(expense_id):
        return False
    found = conn.execute("SELECT 1 FROM car_expenses WHERE id = ? AND car_id = ?", (expense_id, car_id)).fetchone()
    if found is None:
        return False
    conn.execute("DELETE FROM car_expenses WHERE id = ? AND car_id = ?", (expense_id, car_id))
    conn.commit()
    return True
