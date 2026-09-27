"""Legenda do post (template determinístico, sem API), no modelo aprovado.

Limite do Instagram: 2200 caracteres. Se passar: (1) OPCIONAIS viram texto
corrido separado por " • " sem repetir os destaques; (2) info_text é encurtado
por frases (mantendo as que citam blindagem); (3) último recurso: corta o bloco
os opcionais e, por fim, o info_text por palavras, com "…". Cabeçalho (com a linha
da blindadora), contato e hashtags nunca são cortados.
"""
from __future__ import annotations

import re
import unicodedata

MAX_CHARS = 2200
MAX_HIGHLIGHTS = 8

CONTACT = (
    "📲 WhatsApp: (21) 99647-9014 | (21) 97027-0655\n"
    "📍 Rua Teodoro da Silva, 232 – Vila Isabel, Rio de Janeiro\n"
    "🌐 www.unionrioveiculos.com.br"
)
FIXED_TAGS = ["#Seminovos", "#CarrosRJ", "#RioDeJaneiro"]

# Destaques em ordem de prioridade: (nomes de opcional aceitos, em ordem de preferência).
# Grupos com mais de um nome mostram só o primeiro presente, exceto CarPlay/Android Auto (junta).
HIGHLIGHTS: list[tuple[str, ...]] = [
    ("Blindado",),
    ("Único Dono",),
    ("Garantia de Fábrica",),
    ("Revisado em Concessionária",),
    ("IPVA Pago",),
    ("Teto panorâmico", "Teto solar"),
    ("Bancos de Couro",),
    ("Bancos elétricos",),
    ("Apple CarPlay", "Android Auto"),
    ("Piloto automático",),
    ("Câmera de ré",),
    ("Tração 4x4",),
    ("Faróis de xenon", "Farol de LED"),
    ("Chave presencial",),
]
_JOIN_ALL = {("Apple CarPlay", "Android Auto")}


def _key(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).lower().split())


def _money(cents: int) -> str:
    reais, cent = divmod(cents, 100)
    return f"{reais:,}".replace(",", ".") + f",{cent:02d}"


def _hashtag(text: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode())
    return "".join(w.capitalize() if w.isalpha() and len(w) > 3 else w for w in words)


def armor_line(detail) -> str | None:
    if not getattr(detail, "armored", False):
        return None
    company = getattr(detail, "armor_company", None)
    return f"🛡️ BLINDADO — Blindadora {company}" if company else "🛡️ BLINDADO"


def highlights(detail) -> tuple[list[str], set[str]]:
    """(rótulos dos destaques, chaves dos opcionais usados neles)."""
    present = {_key(o): o for o in (detail.options or [])}
    if getattr(detail, "armored", False):
        present.setdefault(_key("Blindado"), "Blindado")
    labels: list[str] = []
    used: set[str] = set()
    for group in HIGHLIGHTS:
        found = [present[_key(n)] for n in group if _key(n) in present]
        if not found:
            continue
        if group in _JOIN_ALL:
            labels.append(" e ".join(found))
            used.update(_key(f) for f in found)
        else:
            labels.append(found[0])
            used.add(_key(found[0]))
        if len(labels) == MAX_HIGHLIGHTS:
            break
    return labels, used


def _header(detail) -> list[str]:
    title = " ".join(p for p in (detail.brand, detail.model, detail.version) if p).upper()
    lines = [f"🚘 {title}"]
    armor = armor_line(detail)
    if armor:
        lines.append(armor)
    if detail.year_fab and detail.year_model:
        lines.append(f"📅 Ano: {detail.year_fab}/{detail.year_model}")
    elif detail.year_model or detail.year_fab:
        lines.append(f"📅 Ano: {detail.year_model or detail.year_fab}")
    if detail.km is not None:
        lines.append("🛣️ KM: 0 km" if detail.km == 0 else f"🛣️ KM: {detail.km:,}".replace(",", "."))
    tech = [f"⚙️ Câmbio: {detail.transmission}" if detail.transmission else None,
            f"⛽ Combustível: {detail.fuel}" if detail.fuel else None]
    if any(tech):
        lines.append(" | ".join(t for t in tech if t))
    lines.append(f"💰 R$ {_money(detail.price_cents)}" if detail.price_cents is not None else "💰 Preço sob consulta")
    return lines


def _tags(detail) -> str:
    tags = ["#UnionVeiculos"]
    for part in (detail.brand, detail.model):
        tag = _hashtag(part)
        if tag and f"#{tag}" not in tags:
            tags.append(f"#{tag}")
    tags += FIXED_TAGS
    if getattr(detail, "armored", False):
        tags.append("#Blindado")
    return " ".join(tags)


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text.strip()) if s.strip()]


def _assemble(header, hl, options_block, info, tags) -> str:
    blocks = ["\n".join(header)]
    if hl:
        blocks.append("⭐ DESTAQUES\n" + "\n".join(f"✅ {h}" for h in hl))
    if options_block:
        blocks.append("📋 OPCIONAIS\n" + options_block)
    if info:
        blocks.append("ℹ️ INFORMAÇÕES DO VEÍCULO\n" + info)
    blocks += [CONTACT, tags]
    return "\n\n".join(blocks)


def build_caption(detail) -> str:
    header, tags = _header(detail), _tags(detail)
    hl, used = highlights(detail)
    options = list(detail.options or [])
    info = (getattr(detail, "info_text", None) or "").strip()

    text = _assemble(header, hl, "\n".join(f"✔️ {o}" for o in options), info, tags)
    if len(text) <= MAX_CHARS:
        return text

    # 1) opcionais corridos, sem repetir destaques
    compact = " • ".join(o for o in options if _key(o) not in used)
    text = _assemble(header, hl, compact, info, tags)
    if len(text) <= MAX_CHARS:
        return text

    # 2) encurta info_text por frases (do fim para o começo), preservando as de blindagem
    sentences = _sentences(info)
    while sentences and len(text) > MAX_CHARS:
        drop = next((i for i in range(len(sentences) - 1, -1, -1) if "blind" not in _key(sentences[i])), None)
        if drop is None:
            break
        sentences.pop(drop)
        text = _assemble(header, hl, compact, " ".join(sentences), tags)
    if len(text) <= MAX_CHARS:
        return text

    # 3) corta o bloco de opcionais (preserva as frases de blindagem do info_text)
    info = " ".join(sentences)
    compact = _cut_words(compact, len(compact) - (len(text) - MAX_CHARS), sep=" • ")
    text = _assemble(header, hl, compact, info, tags)
    if len(text) <= MAX_CHARS:
        return text

    # 4) último recurso: info_text sem frases cortáveis (ex.: bloco enorme sem pontuação
    #    citando blindagem) é cortado por palavras. Cabeçalho (com a linha da
    #    blindadora), contato e hashtags nunca são cortados.
    info = _cut_words(info, len(info) - (len(text) - MAX_CHARS))
    return _assemble(header, hl, compact, info, tags)


def _cut_words(text: str, budget: int, sep: str = " ") -> str:
    """Encurta `text` para no máximo `budget` caracteres, em fronteira de `sep`, com " …"."""
    if len(text) <= budget:
        return text
    room = budget - 2  # " …"
    if room <= 0:
        return ""
    cut = text[:room]
    if sep in cut and not text[room:].startswith(sep):
        cut = cut.rsplit(sep, 1)[0]
    cut = cut.rstrip(" •")
    return f"{cut} …" if cut else ""
