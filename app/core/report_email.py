"""The weekly report as an email: subject, plain text and HTML, in Dutch or English.

Rendering only. Nothing sends this yet (docs/DECISIONS.md D-032): wiring it to the report job
and a per-account language is a product decision. The design follows the dashboard's price
board, built for email clients: a 600px table layout, inline styles, no images (so nothing is
lost with images off), every colour paired with a sign or word, and a dark-mode block for the
clients that honour ``prefers-color-scheme``.

Inputs are the API's own shapes: ``report_to_dict`` for the report and ``_change_dict`` rows
for its changes, so the email can never show a number the dashboard does not.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from html import escape
from typing import Any

COPY = {
    "en": {
        "subject": "Week {week}: {n} changes at your competitors",
        "subject_one": "Week {week}: 1 change at your competitors",
        "subject_none": "Week {week}: nothing moved at your competitors",
        "title": "The week for {shop}",
        "period": "Week {week} · {start} → {end}",
        "changes": "changes", "cuts": "price cuts", "rises": "price rises", "watched": "competitors watched",
        "changes_one": "change", "cuts_one": "price cut", "rises_one": "price rise", "watched_one": "competitor watched",
        "moves": "What moved",
        "col_who": "Competitor", "col_what": "Product", "col_was": "Was", "col_now": "Now", "col_delta": "Change",
        "soldout": "sold out", "back": "back in stock", "new": "new", "removed": "removed",
        "evidence": "evidence",
        "quiet": "Nothing moved this week. Sparton keeps checking.",
        "open": "Open the full report",
        "fine": "Every number comes from comparing two readings of the same product page; the AI only writes the sentences. Each line links to the page it was read from.",
        "why": "You get this because weekly reports are switched on for your Sparton account.",
        "months": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        "decimal": ".", "thousands": ",",
    },
    "nl": {
        "subject": "Week {week}: {n} veranderingen bij je concurrenten",
        "subject_one": "Week {week}: 1 verandering bij je concurrenten",
        "subject_none": "Week {week}: niets bewogen bij je concurrenten",
        "title": "De week van {shop}",
        "period": "Week {week} · {start} → {end}",
        "changes": "veranderingen", "cuts": "prijsverlagingen", "rises": "prijsverhogingen", "watched": "concurrenten gevolgd",
        "changes_one": "verandering", "cuts_one": "prijsverlaging", "rises_one": "prijsverhoging", "watched_one": "concurrent gevolgd",
        "moves": "Wat er bewoog",
        "col_who": "Concurrent", "col_what": "Product", "col_was": "Was", "col_now": "Nu", "col_delta": "Verschil",
        "soldout": "uitverkocht", "back": "weer op voorraad", "new": "nieuw", "removed": "verwijderd",
        "evidence": "bewijs",
        "quiet": "Deze week bewoog er niets. Sparton blijft meten.",
        "open": "Open het volledige rapport",
        "fine": "Elk getal komt uit het vergelijken van twee metingen van dezelfde productpagina; de AI schrijft alleen de zinnen. Elke regel linkt naar de pagina waar hij vandaan komt.",
        "why": "Je krijgt dit omdat weekrapporten aan staan voor je Sparton-account.",
        "months": ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"],
        "decimal": ",", "thousands": ".",
    },
}

INK, BOARD, CHALK, CHALK_2, WALL, PLATE, SIGNAL = "#17191b", "#202326", "#f0ede4", "#b5b3ab", "#e3e4e0", "#f7f7f4", "#f5c518"
DOWN, UP = "#a8301c", "#1c6e3f"
FONT = "Archivo, 'Arial Narrow', Arial, Helvetica, sans-serif"


def _date(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _money(value: Any, lang: str) -> str:
    if value is None:
        return ""
    c = COPY[lang]
    whole, cents = f"{Decimal(str(value)):.2f}".split(".")
    whole = f"{int(whole):,}".replace(",", c["thousands"])
    return f"€ {whole}{c['decimal']}{cents}" if lang == "nl" else f"€{whole}{c['decimal']}{cents}"


def _pct(change: dict) -> str:
    pct = change.get("delta_pct")
    if pct is None and change.get("previous_price"):
        pct = (float(change["new_price"]) - float(change["previous_price"])) / float(change["previous_price"]) * 100
    if pct is None:
        return ""
    return f"{'+' if pct > 0 else '−'}{abs(round(pct))}%"


def _cells(change: dict, lang: str) -> tuple[str, str, str, str]:
    """was, now, change text, and the colour that goes with the change's sign."""
    c = COPY[lang]
    kind = change.get("kind")
    if kind in ("price_decrease", "price_increase"):
        return (_money(change.get("previous_price"), lang), _money(change.get("new_price"), lang),
                _pct(change), DOWN if kind == "price_decrease" else UP)
    word = {"out_of_stock": c["soldout"], "back_in_stock": c["back"], "new_product": c["new"],
            "removed_product": c["removed"]}.get(kind, "")
    return ("", _money(change.get("new_price"), lang), word, INK)


def render_report_email(report: dict, changes: list[dict], *, lang: str = "en", report_url: str = "") -> tuple[str, str, str]:
    """Return ``(subject, text, html)`` for one report in ``lang`` ("en" or "nl")."""
    lang = lang if lang in COPY else "en"
    c = COPY[lang]
    facts = report.get("facts") or {}
    end = _date(report.get("period_end")) or datetime.now()
    start = _date(report.get("period_start")) or end
    week = end.isocalendar()[1]
    shop = (facts.get("shop") or {}).get("name") or "Sparton"
    total = facts.get("total_changes", len(changes))
    by_kind = facts.get("by_kind") or {}
    watched = len(facts.get("competitors") or [])

    fmt = lambda d: f"{d.day} {c['months'][d.month - 1]}"  # noqa: E731
    period = c["period"].format(week=week, start=fmt(start), end=f"{fmt(end)} {end.year}")
    subject = (c["subject_none"] if not total else c["subject_one"] if total == 1 else c["subject"]).format(week=week, n=total)
    title = c["title"].format(shop=shop)
    label = lambda n, key: c[f"{key}_one"] if n == 1 else c[key]  # noqa: E731
    counts = [(total, label(total, "changes"))]
    for n, key in ((by_kind.get("price_decrease"), "cuts"), (by_kind.get("price_increase"), "rises"), (watched, "watched")):
        if n:
            counts.append((n, label(n, key)))

    # ------------------------------------------------------------------ text
    lines = [title, period, "", " · ".join(f"{n} {label}" for n, label in counts), ""]
    if changes:
        lines.append(c["moves"].upper())
        for ch in changes:
            was, now, delta, _ = _cells(ch, lang)
            move = f"{was} → {now}" if was else now
            lines.append(f"- {ch.get('competitor') or ''}: {ch.get('product') or ''} {move} {delta}".rstrip())
            if ch.get("evidence_url"):
                lines.append(f"  {c['evidence']}: {ch['evidence_url']}")
    else:
        lines.append(c["quiet"])
    lines += ["", f"{c['open']}: {report_url}" if report_url else "", "", c["fine"], c["why"]]
    text = "\n".join(lines).strip() + "\n"

    # ------------------------------------------------------------------ html
    e = escape
    td = f"font-family:{FONT};font-size:14px;line-height:1.4;padding:10px 8px;border-top:1px solid #000;color:{CHALK};"
    rows = []
    for ch in changes:
        was, now, delta, colour = _cells(ch, lang)
        on_board = {DOWN: "#e2553f", UP: "#4cb874"}.get(colour, SIGNAL)
        evidence = (f'<a href="{e(ch["evidence_url"])}" style="color:{CHALK};font-weight:700;">{e(c["evidence"])}</a>'
                    if ch.get("evidence_url") else "")
        struck = "text-decoration:line-through;" if ch.get("kind") in ("out_of_stock", "removed_product") else ""
        rows.append(
            "<tr>"
            f'<td style="{td}{struck}font-weight:700;">{e(ch.get("competitor") or "")}</td>'
            f'<td style="{td}{struck}">{e(ch.get("product") or "")}<br><span class="dim" style="color:{CHALK_2};font-size:12px;">{evidence}</span></td>'
            f'<td style="{td}text-align:right;white-space:nowrap;color:{CHALK_2};">{e(was)}</td>'
            f'<td style="{td}text-align:right;white-space:nowrap;font-weight:700;">{e(now)}</td>'
            f'<td style="{td}text-align:right;white-space:nowrap;font-weight:800;color:{on_board};">{e(delta)}</td>'
            "</tr>"
        )
    th = f"font-family:{FONT};font-size:11px;letter-spacing:1px;text-transform:uppercase;color:{CHALK_2};padding:6px 8px;"
    board = (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{BOARD};border-radius:6px;">'
        f'<tr><td colspan="5" style="font-family:{FONT};font-size:13px;font-weight:800;letter-spacing:1px;text-transform:uppercase;color:{CHALK};padding:14px 8px 8px;">{e(c["moves"])}</td></tr>'
        f'<tr><td style="{th}">{e(c["col_who"])}</td><td style="{th}">{e(c["col_what"])}</td>'
        f'<td style="{th}text-align:right;">{e(c["col_was"])}</td><td style="{th}text-align:right;">{e(c["col_now"])}</td>'
        f'<td style="{th}text-align:right;">{e(c["col_delta"])}</td></tr>'
        + "".join(rows) + "</table>"
    ) if changes else (
        f'<p style="font-family:{FONT};font-size:16px;color:{INK};margin:0;padding:16px;background:{PLATE};border-radius:6px;">{e(c["quiet"])}</p>'
    )
    count_html = " &nbsp;·&nbsp; ".join(
        f'<strong style="font-size:20px;">{n}</strong> {e(label)}' for n, label in counts)
    button = (
        f'<a href="{e(report_url)}" style="display:inline-block;background:{SIGNAL};color:{INK};font-family:{FONT};font-size:16px;'
        f'font-weight:700;text-decoration:none;padding:14px 22px;border-radius:3px;">{e(c["open"])}</a>'
    ) if report_url else ""

    html = f"""<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<title>{e(subject)}</title>
<style>
  @media (prefers-color-scheme: dark) {{
    .wall {{ background: #0f1112 !important; }}
    .ink {{ color: {CHALK} !important; }}
    .dim {{ color: {CHALK_2} !important; }}
  }}
  @media (max-width: 620px) {{ .pad {{ padding-left: 12px !important; padding-right: 12px !important; }} }}
</style>
</head>
<body class="wall" style="margin:0;padding:0;background:{WALL};">
<div style="display:none;max-height:0;overflow:hidden;">{e(" · ".join(f"{n} {label}" for n, label in counts))}</div>
<table role="presentation" class="wall" width="100%" cellpadding="0" cellspacing="0" style="background:{WALL};">
<tr><td align="center" style="padding:24px 8px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:100%;max-width:600px;">
  <tr><td style="background:{INK};padding:14px 20px;border-radius:6px 6px 0 0;">
    <span style="font-family:{FONT};font-size:18px;font-weight:800;letter-spacing:6px;color:{CHALK};">SPARTON</span>
  </td></tr>
  <tr><td class="pad" style="padding:24px 20px 8px;">
    <h1 class="ink" style="margin:0;font-family:{FONT};font-size:28px;line-height:1.1;font-weight:800;color:{INK};">{e(title)}</h1>
    <p class="dim" style="margin:6px 0 0;font-family:{FONT};font-size:14px;color:#43474b;">{e(period)}</p>
    <p class="ink" style="margin:16px 0 0;font-family:{FONT};font-size:14px;color:{INK};">{count_html}</p>
  </td></tr>
  <tr><td class="pad" style="padding:16px 20px;">{board}</td></tr>
  <tr><td class="pad" style="padding:8px 20px 16px;">{button}</td></tr>
  <tr><td class="pad" style="padding:8px 20px 24px;">
    <p class="dim" style="margin:0 0 8px;font-family:{FONT};font-size:12px;line-height:1.5;color:#43474b;">{e(c["fine"])}</p>
    <p class="dim" style="margin:0;font-family:{FONT};font-size:12px;line-height:1.5;color:#43474b;">{e(c["why"])}</p>
  </td></tr>
</table>
</td></tr>
</table>
</body>
</html>
"""
    return subject, text, html
