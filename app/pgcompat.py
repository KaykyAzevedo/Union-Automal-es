"""Postgres (psycopg 3) com a mesma API usada do sqlite3.

Serviços e testes escrevem SQL no estilo sqlite3 (`?`, row["col"], row[0],
cursor.lastrowid). Este wrapper traduz para o psycopg:
- `?` → `%s` (e `%` literal → `%%`, ex.: LIKE 'demo-%');
- INSERT em tabela com coluna id ganha `RETURNING id` para preencher lastrowid;
- linhas aceitam índice e nome (tuple(row) funciona);
- executescript executa comando a comando (split_statements: ignora comentários e
  só divide em `;` fora de strings).
"""
from __future__ import annotations

import re

import psycopg

# tabelas com PK `id` serial (lastrowid via RETURNING id)
ID_TABLES = {"cars", "price_history", "tickets", "price_alerts", "sold_alerts", "car_expenses", "runs"}
_INSERT_RE = re.compile(r"^\s*INSERT\s+INTO\s+(\w+)", re.IGNORECASE)


def split_statements(script: str) -> list[str]:
    """Divide um script SQL em comandos, sem os comentários.

    Remove `-- até o fim da linha` e `/* ... */` e divide em `;`, sempre fora de
    strings ('...', com '' como aspa escapada) e de identificadores "...". Um `;`
    dentro de comentário ou de string NÃO divide o comando. Comandos vazios somem.
    """
    statements: list[str] = []
    current: list[str] = []
    quote: str | None = None
    i, n = 0, len(script)
    while i < n:
        ch = script[i]
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None  # '' reabre a string no próximo caractere: mesmo efeito
        elif ch in ("'", '"'):
            quote = ch
            current.append(ch)
        elif script.startswith("--", i):
            end = script.find("\n", i)
            i = n if end == -1 else end
            continue  # mantém a quebra de linha
        elif script.startswith("/*", i):
            end = script.find("*/", i + 2)
            i = n if end == -1 else end + 2
            current.append(" ")
            continue
        elif ch == ";":
            statements.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    statements.append("".join(current))
    return [s.strip() for s in statements if s.strip()]


def normalize_url(url: str) -> str:
    """postgres:// e postgresql+psycopg:// → postgresql://"""
    url = url.strip()
    for prefix in ("postgresql+psycopg://", "postgresql+psycopg2://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix):]
    return url


class Row(tuple):
    """Linha acessível por índice e por nome de coluna, como sqlite3.Row."""

    def __new__(cls, values, index: dict[str, int]):
        obj = super().__new__(cls, values)
        obj._index = index
        return obj

    def __getitem__(self, key):
        if isinstance(key, str):
            return tuple.__getitem__(self, self._index[key])
        return tuple.__getitem__(self, key)

    def keys(self) -> list[str]:
        return list(self._index)


def _row_factory(cursor):
    if cursor.description is None:
        return tuple
    index = {d.name: i for i, d in enumerate(cursor.description)}
    return lambda values: Row(values, index)


def translate(sql: str) -> tuple[str, bool]:
    """(sql no formato psycopg, se foi acrescentado RETURNING id)."""
    returning = False
    m = _INSERT_RE.match(sql)
    if m and m.group(1).lower() in ID_TABLES and "returning" not in sql.lower():
        sql = sql.rstrip().rstrip(";") + " RETURNING id"
        returning = True
    return sql.replace("%", "%%").replace("?", "%s"), returning


class PgCursor:
    def __init__(self, cursor: psycopg.Cursor, lastrowid: int | None = None):
        self._cur = cursor
        self.lastrowid = lastrowid

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def description(self):
        return self._cur.description

    def fetchone(self):
        return self._cur.fetchone() if self._cur.description is not None and self.lastrowid is None else None

    def fetchall(self):
        return self._cur.fetchall() if self._cur.description is not None and self.lastrowid is None else []

    def __iter__(self):
        return iter(self.fetchall())


class PgConnection:
    """Conexão Postgres com a interface de sqlite3.Connection usada no app."""

    is_postgres = True

    def __init__(self, url: str):
        # prepare_threshold=None: sem prepared statements automáticos (PgBouncer/pooler do Neon)
        self._conn = psycopg.connect(normalize_url(url), connect_timeout=10, row_factory=_row_factory,
                                     prepare_threshold=None)

    def execute(self, sql: str, params=()) -> PgCursor:
        query, returning = translate(sql)
        cur = self._conn.cursor()
        cur.execute(query, tuple(params or ()))
        lastrowid = cur.fetchone()[0] if returning else None
        return PgCursor(cur, lastrowid)

    def executemany(self, sql: str, seq) -> PgCursor:
        query, _ = translate(sql)
        query = query.removesuffix(" RETURNING id")
        cur = self._conn.cursor()
        cur.executemany(query, [tuple(p) for p in seq])
        return PgCursor(cur)

    def executescript(self, script: str) -> None:
        for statement in split_statements(script):
            self._conn.execute(statement)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False
