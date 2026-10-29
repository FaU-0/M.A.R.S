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


# подписи формы по языку интерфейса; текст, который написал врач, не переводится
LABELS = {
    "ru": {"title": "Заключение консультации", "patient": "Пациент", "date": "Дата", "doctor": "Врач",
           "complaints": "Жалобы", "anamnesis": "Анамнез", "diagnosis": "Диагноз", "icd": "Код МКБ-10",
           "recommendations": "Рекомендации", "followup": "Повторный контакт",
           "note": "Документ сформирован по результатам дистанционной консультации. "
                   "Это заключение консультации, а не электронный рецепт.",
           "sign": "Подпись врача"},
    "en": {"title": "Consultation report", "patient": "Patient", "date": "Date", "doctor": "Doctor",
           "complaints": "Complaints", "anamnesis": "History", "diagnosis": "Diagnosis", "icd": "ICD-10 code",
           "recommendations": "Recommendations", "followup": "Follow-up contact",
           "note": "Document produced after a remote consultation. "
                   "This is a consultation report, not an electronic prescription.",
           "sign": "Doctor's signature"},
}
# название центра по умолчанию (CENTER_NAME не задан) переводится; заданное клиникой остаётся как есть
DEFAULT_CENTER = {"en": {"Медицинский центр": "Medical center"}}


def conclusion_html(d):
    """d: {center, doctor, patient, date, lang, conclusion:{complaints, anamnesis, diagnosis, icd, recommendations, followup}}"""
    lang = d.get("lang") if d.get("lang") in LABELS else "ru"
    L = LABELS[lang]
    c = d.get("conclusion") or {}
    center = DEFAULT_CENTER.get(lang, {}).get(d.get("center"), d.get("center"))

    def row(key, value):
        return "<h2>%s</h2><p class=\"pre\">%s</p>" % (escape(L[key]), _t(value) or "-")

    body = [
        "<h1>%s</h1><h2>%s</h2>" % (_t(center, 200), escape(L["title"])),
        "<table><tr><th>%s</th><td>%s</td></tr><tr><th>%s</th><td>%s</td></tr><tr><th>%s</th><td>%s</td></tr></table>"
        % (escape(L["patient"]), _t(d.get("patient"), 200), escape(L["date"]), _t(d.get("date"), 40),
           escape(L["doctor"]), _t(d.get("doctor"), 200)),
        # код МКБ-10 отдельной строкой: при длинном диагнозе он не теряется
        row("complaints", c.get("complaints")), row("anamnesis", c.get("anamnesis")), row("diagnosis", c.get("diagnosis")),
        row("icd", c.get("icd")),
        row("recommendations", c.get("recommendations")), row("followup", c.get("followup")),
        "<p class=\"sm\">%s</p>" % escape(L["note"]),
        "<div class=\"sign\">%s ______________________ / %s /</div>" % (escape(L["sign"]), _t(d.get("doctor"), 200)),
    ]
    return ("<!doctype html><html lang=\"%s\"><head><meta charset=\"utf-8\"><title>%s</title>"
            "<style>%s</style></head><body>%s</body></html>" % (lang, escape(L["title"]), CSS, "".join(body)))
