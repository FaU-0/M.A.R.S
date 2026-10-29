# -*- coding: utf-8 -*-
"""Шаблоны печатных форм. Весь пользовательский текст экранируется."""
from html import escape

CSS = """
body{font:12pt/1.4 -apple-system,'Segoe UI',Roboto,Arial,sans-serif;color:#000;background:#fff;margin:0;padding:16mm}
h1{font-size:20pt;margin:0 0 6pt}h2{font-size:14pt;margin:12pt 0 4pt}
table{width:100%;border-collapse:collapse}td,th{border-bottom:1px solid #999;padding:3pt 4pt;text-align:left}
p{margin:0}.pre{white-space:pre-wrap}.sm{font-size:10pt;margin-top:14pt}
.sign{margin-top:36pt;border-top:1px solid #000;width:60%;padding-top:4pt}
"""


TEXT_LIMIT = 8000  # столько же принимает gateway (clean_conclusion): текст заключения не обрезается молча


def _t(v, limit=TEXT_LIMIT):
    s = str(v if v is not None else "")
    return escape(s[:limit]) + (" [текст сокращён]" if len(s) > limit else "")


def conclusion_html(d):
    """d: {center, doctor, patient, date, conclusion:{complaints, anamnesis, diagnosis, icd, recommendations, followup}}"""
    c = d.get("conclusion") or {}

    def row(title, value):
        return "<h2>%s</h2><p class=\"pre\">%s</p>" % (escape(title), _t(value) or "-")

    body = [
        "<h1>%s</h1><h2>Заключение консультации</h2>" % _t(d.get("center"), 200),
        "<table><tr><th>Пациент</th><td>%s</td></tr><tr><th>Дата</th><td>%s</td></tr><tr><th>Врач</th><td>%s</td></tr></table>"
        % (_t(d.get("patient"), 200), _t(d.get("date"), 40), _t(d.get("doctor"), 200)),
        # код МКБ-10 отдельной строкой: при длинном диагнозе он не теряется
        row("Жалобы", c.get("complaints")), row("Анамнез", c.get("anamnesis")), row("Диагноз", c.get("diagnosis")),
        row("Код МКБ-10", c.get("icd")),
        row("Рекомендации", c.get("recommendations")), row("Повторный контакт", c.get("followup")),
        "<p class=\"sm\">Документ сформирован по результатам дистанционной консультации. "
        "Это заключение консультации, а не электронный рецепт.</p>",
        "<div class=\"sign\">Подпись врача ______________________ / %s /</div>" % _t(d.get("doctor"), 200),
    ]
    return ("<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\"><title>Заключение консультации</title>"
            "<style>%s</style></head><body>%s</body></html>" % (CSS, "".join(body)))
