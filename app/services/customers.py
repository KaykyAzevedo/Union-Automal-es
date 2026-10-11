"""Clientes da loja e aniversários (tabela `customers`).

O site não envia mensagem sozinho: mostra os aniversariantes e monta o link wa.me com o texto
pronto para o funcionário mandar do próprio WhatsApp.

Dados pessoais: este módulo não escreve log, e nenhum retorno de erro carrega o que foi digitado.
Funções com `conn` fazem commit do que gravam. `today` é sempre a data em America/Sao_Paulo
(db.now().date()).
"""
from __future__ import annotations

import calendar
import re
import unicodedata
from collections.abc import Mapping
from datetime import date, timedelta
from urllib.parse import quote

from ..db import now, to_iso

SAVED, INVALID, DUPLICATE = "saved", "invalid", "duplicate"
ADMIN_NAME = "Administrador"

NAME_MIN, NAME_MAX = 2, 80
EMAIL_MAX, CAR_MAX, NOTES_MAX = 120, 120, 1000
MIN_YEAR = 1900
WEEK_DAYS = 7
SIGNATURE = "Equipe Union Veículos"

_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
# re.ASCII: só 0-9 contam como dígito. Sem isso \d casa dígitos Unicode (largura cheia,
# arábicos...) e int() os converte em silêncio.
_BIRTHDAY_BR_RE = re.compile(r"(\d{1,2})\s*[/.\-]\s*(\d{1,2})(?:\s*[/.\-]\s*(\d+))?", re.ASCII)
_DATE_ISO_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})", re.ASCII)
_PURCHASE_BR_RE = re.compile(r"(\d{1,2})\s*[/.\-]\s*(\d{1,2})\s*[/.\-]\s*(\d{4})", re.ASCII)
_COLUMNS = ("id, name, phone, email, birth_day, birth_month, birth_year, car_text, purchase_date, notes, "
            "created_by, created_by_name, created_at, updated_at, greeted_year, greeted_by_name, greeted_at")


def today_sp() -> date:
    """Hoje em America/Sao_Paulo."""
    return now().date()


# ---------- texto ----------

def _fold(text: str | None) -> str:
    """Minúsculas e sem acento, para busca."""
    plain = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in plain if not unicodedata.combining(ch)).casefold()


def _has_control(text: str) -> bool:
    return any(unicodedata.category(ch).startswith("C") for ch in text)


def _clean_line(text) -> str:
    """Uma linha: espaços colapsados, sem caracteres de controle/invisíveis."""
    collapsed = " ".join(str(text or "").split())
    return "".join(ch for ch in collapsed if not unicodedata.category(ch).startswith("C"))


def _clean_notes(text) -> str:
    """Várias linhas: mantém quebras de linha, tira os outros caracteres de controle."""
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    kept = "".join(ch for ch in raw if ch == "\n" or not unicodedata.category(ch).startswith("C"))
    return "\n".join(line.rstrip() for line in kept.split("\n")).strip()


def first_name(name: str | None) -> str:
    """'MARIA DA SILVA' → 'Maria'; 'ana' → 'Ana'; mantém caixa mista ('McQueen')."""
    words = (name or "").split()
    if not words:
        return ""
    first = words[0]
    return first.capitalize() if first.isupper() or first.islower() else first


# ---------- telefone ----------

def normalize_phone(text) -> str | None:
    """'(21) 99647-9014', '+55 21 99647-9014', '021996479014' → '21996479014'.
    Resultado: DDD + número, 10 ou 11 dígitos ASCII, sem 55. Inválido → None.
    Dígito que não seja 0-9 (largura cheia, arábico...) invalida o telefone: descartá-lo em
    silêncio gravaria OUTRO número, e o parabéns iria para um desconhecido."""
    raw = str(text or "")
    if any(not ch.isascii() and (ch.isdigit() or ch.isnumeric()) for ch in raw):
        return None
    digits = re.sub(r"[^0-9]", "", raw)
    if len(digits) in (12, 13) and digits.startswith("55"):
        digits = digits[2:]                      # +55 21 ...
    elif len(digits) in (11, 12) and digits.startswith("0"):
        digits = digits[1:]                      # 021 ...
    if len(digits) not in (10, 11):
        return None
    if digits[0] == "0" or digits[1] == "0":     # DDD vai de 11 a 99, sem zero
        return None
    number = digits[2:]
    if len(number) == 9 and number[0] != "9":    # celular: 9 dígitos começando em 9
        return None
    if number[0] in "01" or len(set(number)) == 1:
        return None
    return digits


def phone_display(phone: str | None) -> str:
    """'21996479014' → '(21) 99647-9014'; '2125689348' → '(21) 2568-9348'."""
    phone = phone or ""
    if len(phone) not in (10, 11) or not (phone.isascii() and phone.isdigit()):
        return phone
    return f"({phone[:2]}) {phone[2:-4]}-{phone[-4:]}"


def whatsapp_link(phone: str, text: str | None = None) -> str:
    """'https://wa.me/55<phone>?text=<urlencoded>' (sem `text` → só a conversa)."""
    base = f"https://wa.me/55{re.sub(r'[^0-9]', '', phone or '')}"
    if not text:
        return base
    return f"{base}?text={quote(text, safe='')}"


def birthday_message(customer_name: str | None, sender_name: str | None = None) -> str:
    """Texto padrão de parabéns: 1º nome do cliente, assinatura da equipe (+ 1º nome de quem envia)."""
    customer = first_name(customer_name)
    sender = first_name(sender_name)
    greeting = f"Olá, {customer}! 🎉" if customer else "Olá! 🎉"
    signature = f"{sender} — {SIGNATURE}" if sender else SIGNATURE
    return (f"{greeting}\n"
            "Hoje é o seu dia e a gente não podia deixar passar: feliz aniversário! 🎂\n"
            "Que o seu novo ano venha com muita saúde, alegria e boas estradas pela frente. 🚗✨\n\n"
            "Um abraço,\n"
            f"{signature}")


# ---------- datas ----------

def _valid_day_month(day: int, month: int) -> bool:
    return 1 <= month <= 12 and 1 <= day <= calendar.monthrange(2000, month)[1]  # 2000: 29/02 existe


def parse_birthday(text, today: date | None = None) -> tuple[int, int, int | None] | None:
    """'dd/mm', 'dd/mm/aaaa' ou ISO 'aaaa-mm-dd' → (dia, mês, ano|None).
    Data impossível, futura, ano < 1900 ou ano de 2 dígitos → None. 29/02 sem ano é aceito."""
    raw = str(text or "").strip()
    today = today or today_sp()
    iso = _DATE_ISO_RE.fullmatch(raw)
    if iso:
        year, month, day = int(iso.group(1)), int(iso.group(2)), int(iso.group(3))
    else:
        br = _BIRTHDAY_BR_RE.fullmatch(raw)
        if not br:
            return None
        day, month = int(br.group(1)), int(br.group(2))
        year_text = br.group(3)
        if year_text is not None and len(year_text) != 4:
            return None                          # '12/10/85' é ambíguo: peça os 4 dígitos
        year = int(year_text) if year_text else None
    if year is None:
        return (day, month, None) if _valid_day_month(day, month) else None
    if year < MIN_YEAR:
        return None
    try:
        born = date(year, month, day)
    except ValueError:
        return None
    return (day, month, year) if born <= today else None


def birthday_from_form(form: Mapping, today: date | None = None) -> tuple[int, int, int | None] | None:
    """Aniversário do formulário: chave `birthday` (texto) ou `birth_day`/`birth_month`/`birth_year`."""
    text = str(form.get("birthday") or "").strip()
    if text:
        return parse_birthday(text, today)
    day, month = str(form.get("birth_day") or "").strip(), str(form.get("birth_month") or "").strip()
    year = str(form.get("birth_year") or "").strip()
    if not day or not month:
        return None
    return parse_birthday(f"{day}/{month}/{year}" if year else f"{day}/{month}", today)


def parse_purchase_date(text, today: date | None = None) -> date | None:
    """ISO 'aaaa-mm-dd' ou 'dd/mm/aaaa' → date. Ilegível, futura ou antes de 1900 → None."""
    raw = str(text or "").strip()
    today = today or today_sp()
    iso = _DATE_ISO_RE.fullmatch(raw)
    br = _PURCHASE_BR_RE.fullmatch(raw)
    try:
        if iso:
            parsed = date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        elif br:
            parsed = date(int(br.group(3)), int(br.group(2)), int(br.group(1)))
        else:
            return None
    except ValueError:
        return None
    return parsed if MIN_YEAR <= parsed.year and parsed <= today else None


def birthday_in_year(day: int, month: int, year: int) -> date:
    """Dia em que o aniversário cai no ano: 29/02 em ano não bissexto conta em 28/02."""
    if month == 2 and day == 29 and not calendar.isleap(year):
        return date(year, 2, 28)
    return date(year, month, day)


def next_birthday(customer: Mapping, today: date) -> date:
    """Próximo aniversário (hoje conta)."""
    day, month = int(customer["birth_day"]), int(customer["birth_month"])
    this_year = birthday_in_year(day, month, today.year)
    return this_year if this_year >= today else birthday_in_year(day, month, today.year + 1)


def days_until(customer: Mapping, today: date) -> int:
    return (next_birthday(customer, today) - today).days


def age_turning(customer: Mapping, today: date) -> int | None:
    """Idade que faz no próximo aniversário (None sem ano de nascimento)."""
    year = customer["birth_year"]
    return next_birthday(customer, today).year - int(year) if year else None


def birthday_label(day: int, month: int) -> str:
    return f"{int(day):02d}/{int(month):02d}"


def birthday_text(day, month, year=None) -> str:
    """Texto de formulário: '12/10' ou '12/10/1985'."""
    if not day or not month:
        return ""
    return birthday_label(day, month) + (f"/{int(year):04d}" if year else "")


# ---------- validação ----------

def _validate(form: Mapping, today: date) -> dict | None:
    """Formulário cru → campos prontos para gravar, ou None se algo é inválido."""
    raw_name = " ".join(str(form.get("name") or "").split())
    if _has_control(raw_name) or not NAME_MIN <= len(raw_name) <= NAME_MAX:
        return None
    phone = normalize_phone(form.get("phone"))
    birthday = birthday_from_form(form, today)
    if phone is None or birthday is None:
        return None
    email = str(form.get("email") or "").strip().lower()
    if email and (len(email) > EMAIL_MAX or _has_control(email) or not _EMAIL_RE.fullmatch(email)):
        return None
    car_text = _clean_line(form.get("car_text"))
    notes = _clean_notes(form.get("notes"))
    if len(car_text) > CAR_MAX or len(notes) > NOTES_MAX:
        return None
    purchase = None
    if str(form.get("purchase_date") or "").strip():
        purchase = parse_purchase_date(form.get("purchase_date"), today)
        if purchase is None:
            return None
    return {
        "name": raw_name, "phone": phone, "email": email or None,
        "birth_day": birthday[0], "birth_month": birthday[1], "birth_year": birthday[2],
        "car_text": car_text or None, "purchase_date": purchase.isoformat() if purchase else None,
        "notes": notes or None,
    }


def _phone_taken(conn, phone: str, except_id: int | None = None) -> bool:
    rows = conn.execute("SELECT id FROM customers WHERE phone = ?", (phone,)).fetchall()
    return any(row["id"] != except_id for row in rows)


# ---------- leitura ----------

def _form_dict(row) -> dict:
    """Linha do banco → dict em formato de formulário (o mesmo de draft())."""
    return {
        "id": row["id"],
        "name": row["name"],
        "phone": row["phone"],
        "phone_display": phone_display(row["phone"]),
        "email": row["email"],
        "birthday": birthday_text(row["birth_day"], row["birth_month"], row["birth_year"]),
        "birth_day": row["birth_day"],
        "birth_month": row["birth_month"],
        "birth_year": row["birth_year"],
        "car_text": row["car_text"],
        "purchase_date": row["purchase_date"],
        "notes": row["notes"],
        "created_by": row["created_by"],
        "created_by_name": row["created_by_name"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "greeted_year": row["greeted_year"],
        "greeted_by_name": row["greeted_by_name"],
    }


def get_customer(conn, customer_id: int) -> dict | None:
    row = conn.execute(f"SELECT {_COLUMNS} FROM customers WHERE id = ?", (customer_id,)).fetchone()
    return _form_dict(row) if row is not None else None


def draft(form: Mapping, id: int | None = None) -> dict:
    """O que foi digitado, no formato do get_customer (texto cru, sem validar), para
    re-renderizar o formulário depois de 'invalid'/'duplicate' sem perder nada."""
    def text(key: str) -> str:
        return str(form.get(key) or "")

    birthday = text("birthday").strip()
    if not birthday and (text("birth_day").strip() or text("birth_month").strip()):
        parts = [text("birth_day").strip(), text("birth_month").strip(), text("birth_year").strip()]
        birthday = "/".join(p for p in parts if p)
    return {
        "id": id,
        "name": text("name"),
        "phone": text("phone"),
        "phone_display": text("phone"),
        "email": text("email"),
        "birthday": birthday,
        "birth_day": text("birth_day"),
        "birth_month": text("birth_month"),
        "birth_year": text("birth_year"),
        "car_text": text("car_text"),
        "purchase_date": text("purchase_date"),
        "notes": text("notes"),
        "created_by": None,
        "created_by_name": "",
        "created_at": None,
        "updated_at": None,
        "greeted_year": None,
        "greeted_by_name": None,
    }


def can_delete(customer: Mapping | None, role: str | None, user: Mapping | None) -> bool:
    """Admin pode tudo; funcionário só o que ele mesmo cadastrou; senha única não exclui."""
    if customer is None:
        return False
    if role == "admin":
        return True
    return bool(user) and customer.get("created_by") is not None and customer["created_by"] == user.get("id")


def birthdays_today(conn, today: date) -> int:
    """Quantos fazem aniversário hoje (para o selo da aba). 29/02 conta em 28/02 fora de ano bissexto."""
    count = conn.execute("SELECT COUNT(*) FROM customers WHERE birth_month = ? AND birth_day = ?",
                         (today.month, today.day)).fetchone()[0]
    if today.month == 2 and today.day == 28 and not calendar.isleap(today.year):
        count += conn.execute(
            "SELECT COUNT(*) FROM customers WHERE birth_month = 2 AND birth_day = 29").fetchone()[0]
    return int(count)


def _item(row, today: date, sender_name: str | None) -> dict:
    upcoming = next_birthday(row, today)
    purchase = None
    if row["purchase_date"]:
        try:
            purchase = date.fromisoformat(row["purchase_date"][:10])
        except ValueError:
            purchase = None
    greeted = row["greeted_year"] is not None and int(row["greeted_year"]) == upcoming.year
    return {
        "id": row["id"],
        "name": row["name"],
        "phone": row["phone"],
        "phone_display": phone_display(row["phone"]),
        "email": row["email"],
        "birthday_label": birthday_label(row["birth_day"], row["birth_month"]),
        "age_turning": age_turning(row, today),
        "days_until": (upcoming - today).days,
        "next_birthday": upcoming,
        "car_text": row["car_text"],
        "purchase_date": purchase,
        "notes": row["notes"],
        "created_by": row["created_by"],
        "created_by_name": row["created_by_name"],
        "greeted": greeted,
        "greeted_by_name": row["greeted_by_name"] if greeted else None,
        "wa_link": whatsapp_link(row["phone"], birthday_message(row["name"], sender_name)),
        "wa_chat_link": whatsapp_link(row["phone"]),
    }


def _matches(item: dict, query: str) -> bool:
    needle = _fold(" ".join(query.split()))
    if not needle:
        return True
    if needle in _fold(f"{item['name']} {item['car_text'] or ''}"):
        return True
    digits = re.sub(r"[^0-9]", "", query)
    only_phone_chars = not re.sub(r"[0-9\s()+\-.]", "", query)
    return bool(digits) and only_phone_chars and digits in item["phone"]


def customers_view(conn, today: date, q: str | None = None, sender_name: str | None = None) -> dict:
    """Listas da tela de clientes. `today`/`week`/`month` não dependem de `q`; `all` é filtrada
    por nome, telefone ou carro (sem diferenciar maiúsculas/acentos)."""
    rows = conn.execute(f"SELECT {_COLUMNS} FROM customers").fetchall()
    items = [_item(row, today, sender_name) for row in rows]
    by_name = sorted(items, key=lambda i: (_fold(i["name"]), i["id"]))
    by_date = sorted(items, key=lambda i: (i["days_until"], _fold(i["name"]), i["id"]))
    today_list = [i for i in by_name if i["days_until"] == 0]
    week = [i for i in by_date if 1 <= i["days_until"] <= WEEK_DAYS]
    month = [i for i in by_date if i["days_until"] > WEEK_DAYS
             and (i["next_birthday"].year, i["next_birthday"].month) == (today.year, today.month)]
    return {
        "today": today_list,
        "week": week,
        "month": month,
        "all": [i for i in by_name if _matches(i, q or "")],
        "counts": {"today": len(today_list), "week": len(week), "total": len(items)},
    }


# ---------- escrita ----------

def create_customer(conn, form: Mapping, created_by: int | None = None,
                    created_by_name: str | None = None, today: date | None = None) -> str:
    """Retorna 'saved' | 'invalid' | 'duplicate' (mesmo telefone em outro cadastro)."""
    data = _validate(form, today or today_sp())
    if data is None:
        return INVALID
    if _phone_taken(conn, data["phone"]):
        return DUPLICATE
    stamp = to_iso(now())
    author = _clean_line(created_by_name)[:NAME_MAX] or ADMIN_NAME
    conn.execute(
        "INSERT INTO customers (name, phone, email, birth_day, birth_month, birth_year, car_text, "
        "purchase_date, notes, created_by, created_by_name, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (data["name"], data["phone"], data["email"], data["birth_day"], data["birth_month"],
         data["birth_year"], data["car_text"], data["purchase_date"], data["notes"],
         created_by, author, stamp, stamp))
    conn.commit()
    return SAVED


def update_customer(conn, customer_id: int, form: Mapping, today: date | None = None) -> str:
    """Retorna 'saved' | 'invalid' (inclui id inexistente) | 'duplicate'. Não muda quem cadastrou."""
    data = _validate(form, today or today_sp())
    if data is None or get_customer(conn, customer_id) is None:
        return INVALID
    if _phone_taken(conn, data["phone"], except_id=customer_id):
        return DUPLICATE
    conn.execute(
        "UPDATE customers SET name = ?, phone = ?, email = ?, birth_day = ?, birth_month = ?, "
        "birth_year = ?, car_text = ?, purchase_date = ?, notes = ?, updated_at = ? WHERE id = ?",
        (data["name"], data["phone"], data["email"], data["birth_day"], data["birth_month"],
         data["birth_year"], data["car_text"], data["purchase_date"], data["notes"],
         to_iso(now()), customer_id))
    conn.commit()
    return SAVED


def delete_customer(conn, customer_id: int) -> bool:
    """False se o id não existe."""
    if get_customer(conn, customer_id) is None:
        return False
    conn.execute("DELETE FROM customers WHERE id = ?", (customer_id,))
    conn.commit()
    return True


def mark_greeted(conn, customer_id: int, sender_name: str | None, today: date, value: bool) -> bool:
    """Marca/desmarca 'parabéns enviado'.
    Marcar (value=True) só vale quando o aniversário é HOJE (`today` em America/Sao_Paulo):
    página aberta antes da meia-noite e clicada depois gravaria o aniversário do ano seguinte.
    Desfazer (value=False) vale sempre.
    Retorna False, sem gravar nada, se o id não existe ou se tentou marcar fora do dia."""
    row = conn.execute(f"SELECT {_COLUMNS} FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if row is None:
        return False
    if value:
        if next_birthday(row, today) != today:
            return False
        conn.execute(
            "UPDATE customers SET greeted_year = ?, greeted_by_name = ?, greeted_at = ? WHERE id = ?",
            (today.year, _clean_line(sender_name)[:NAME_MAX] or ADMIN_NAME,
             to_iso(now()), customer_id))
    else:
        conn.execute(
            "UPDATE customers SET greeted_year = NULL, greeted_by_name = NULL, greeted_at = NULL WHERE id = ?",
            (customer_id,))
    conn.commit()
    return True
