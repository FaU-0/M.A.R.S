# -*- coding: utf-8 -*-
"""Обезличивание расшифровки консультации в три шага:

1. Известные данные: ФИО, дата рождения, телефон, адрес, email пациента из карточки и имя врача
   (во всех падежных формах).
2. Правила: телефоны, email, СНИЛС, полис ОМС, паспорт, даты, адреса, частые имена и отчества.
3. Нейросеть (Ollama) ищет остальное: другие имена, работодателей, населённые пункты, учебные заведения.
   Она только возвращает список фрагментов; фрагмент, которого нет в тексте дословно, отбрасывается,
   замену делает код. Так модель не может исказить смысл.

Каждый найденный фрагмент заменяется во всём тексте, в любой падежной форме.
Соответствие «метка и оригинал» возвращается отдельно и в обезличенный текст не попадает.
"""
import re

W = r"[А-Яа-яЁёA-Za-z0-9]"  # буква или цифра для границ слова (\b в Python понимает кириллицу, но не «ё» с дефисом)

LABEL = {"patient": "ФИО_ПАЦИЕНТА", "doctor": "ВРАЧ", "phone": "ТЕЛЕФОН", "email": "EMAIL", "snils": "СНИЛС", "oms": "ПОЛИС_ОМС",
         "passport": "ПАСПОРТ", "date": "ДАТА", "address": "АДРЕС", "NAME": "ИМЯ", "ORG": "ОРГАНИЗАЦИЯ", "CITY": "МЕСТО",
         "SCHOOL": "УЧЕБНОЕ_ЗАВЕДЕНИЕ"}
NUMBERED = {"NAME", "ORG", "CITY", "SCHOOL"}  # у этих меток номер: [ИМЯ_1], [ИМЯ_2]
PRIORITY = {"known": 0, "rule": 1, "llm": 2}

MONTHS = "января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря"
RULES = [
    ("email", r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    ("snils", r"(?<!\d)\d{3}[- ]?\d{3}[- ]?\d{3}[- ]\d{2}(?!\d)"),
    ("oms", r"(?<!\d)\d{4}[ ]?\d{4}[ ]?\d{4}[ ]?\d{4}(?!\d)"),
    ("phone", r"(?<!\d)(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)"),
    ("phone", r"(?<!\d)\d{3}[\s-]\d{2}[\s-]\d{2}(?!\d)"),
    ("passport", r"(?<!\d)\d{2}\s?\d{2}\s?№?\s?\d{6}(?!\d)"),
    ("date", r"(?<!\d)\d{1,2}[./-]\d{1,2}[./-](?:\d{4}|\d{2})(?!\d)"),
    ("date", r"(?<!\d)\d{1,2}(?:-?го)?\s+(?:%s)(?:\s+\d{4}(?:\s*(?:года|г\.?))?)?" % MONTHS),
    ("date", r"(?<!\d)(?:19|20)\d{2}\s*(?:года|году|год|г\.)\s+рождения"),
    ("address", r"(?:ул\.|улиц[аеуыи]|проспект[аеу]?|пр-т|переул(?:ок|ке|ка)|пер\.|шоссе|бульвар[аеу]?|набережн(?:ая|ой|ую)|площад[ьи])"
                r"\s+[А-ЯЁ][\w-]*(?:\s+[А-ЯЁ][\w-]*)?(?:,?\s*(?:д\.|дом)\s*\d+[а-я]?)?(?:,?\s*(?:корп\.|корпус)\s*\d+)?(?:,?\s*(?:кв\.|квартир[аеуы])\s*\d+)?"),
    ("address", r"(?:дом|д\.)\s*\d+[а-я]?,?\s*(?:корпус\s*\d+,?\s*)?(?:кв\.|квартир[аеуы])\s*\d+"),
]
RULES = [(k, re.compile(p, re.I)) for k, p in RULES]

# частые русские имена (словарь, без падежей: формы ловит основа слова)
FIRST_NAMES = """александр алексей анатолий андрей антон аркадий артём артем борис вадим валентин валерий василий виктор виталий
владимир владислав вячеслав геннадий георгий глеб григорий давид денис дмитрий евгений егор иван игорь илья кирилл константин
леонид максим матвей михаил никита николай олег павел пётр петр руслан святослав семён семен сергей станислав
степан тимофей тимур фёдор федор юрий ярослав саша алёша алеша лёша леша миша коля серёжа сережа дима женя вова володя паша
петя ваня гоша костя толя федя
александра алина алла анастасия ангелина анна антонина валентина валерия вероника виктория галина дарья диана ева евгения
екатерина елена елизавета жанна зинаида зоя инна ирина карина кира кристина ксения лариса лидия людмила маргарита марина
мария милана наталья наталия нина оксана олеся ольга полина раиса светлана софья софия таисия тамара татьяна ульяна юлия
яна маша катя лена оля таня света наташа даша настя ира юля аня галя люда надя соня""".split()
# без имён, совпадающих с обычными словами: вера, надежда, любовь, роман, лев, слава, марк
NAME_STEMS = {}
PATRONYMIC = re.compile(r"(?<!%s)[А-ЯЁ][а-яё]+(?:(?:ович|евич|ьич)(?:а|у|ем|е)?|(?:овн|евн|ичн|иничн)(?:а|ы|е|у|ой))(?!%s)" % (W, W))
STOP = {"врач", "доктор", "пациент", "пациентка", "здравствуйте", "спасибо", "да", "нет", "хорошо", "медсестра", "мама", "папа",
        "сын", "дочь", "муж", "жена", "брат", "сестра", "бабушка", "дедушка"}
# лекарства и термины, которые модель иногда принимает за имена: такие фрагменты не заменяются
DRUGS = """парацетамол ибупрофен аспирин нурофен анальгин цитрамон но-шпа ношпа амоксициллин азитромицин метформин эналаприл
лозартан амлодипин бисопролол омепразол пантопразол лоратадин цетиризин супрастин валидол корвалол глицин ацикловир
диклофенак кеторол мезим фестал смекта лоперамид""".split()


def stem(word):
    """Грубая основа русского слова: без падежного окончания. Короче трёх букв не режется."""
    w = word.lower().replace("ё", "е")
    for end in ("ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими", "ой", "ей", "ом", "ем", "ам", "ям", "ах", "ях", "ую", "юю",
                "ая", "яя", "ов", "ев", "ы", "и", "а", "я", "у", "ю", "е", "о", "ь", "й"):
        if w.endswith(end) and len(w) - len(end) >= 3:
            return w[:-len(end)]
    return w


for _n in FIRST_NAMES:
    NAME_STEMS.setdefault(stem(_n), _n)
DRUG_STEMS = {stem(d) for d in DRUGS}
MEDICAL = """анализ анализы кровь крови моча голова голову боль болит болела сыпь кашель температура давление горло нос живот желудок
изжога поясница спина сердце аллергия аллергии лекарство таблетки укол скорая скорую узи рентген мрт экг терапевт кардиолог""".split()
# общие слова, которые модель принимает за названия в начале фразы («Аптека на...», «Дата рождения...»)
COMMON = """аптека дата почта адрес телефон номер паспорт полис снилс карта карточка рецепт больница поликлиника школа завод
магазин компания работа город деревня село улица дом квартира семья родственники соседи""".split()
MED_STEMS = {stem(m) for m in MEDICAL} | {stem(c) for c in COMMON}


def variant_re(fragment):
    """Регулярное выражение для фрагмента во всех падежных формах: каждое слово как основа + до 4 букв окончания."""
    words = re.findall(r"[\w«»\"'-]+", fragment)
    parts = []
    for w in words:
        core = w.strip("«»\"'")
        if not core:
            continue
        if core.isdigit() or len(core) < 4:
            parts.append(re.escape(core))
        else:
            s = stem(core)
            pat = "".join("[её]" if ch in "её" else re.escape(ch) for ch in s)
            parts.append(pat + r"[а-яёa-z]{0,4}")
    if not parts:
        return None
    body = r"[\s«»\"'-]*".join(parts)
    return re.compile(r"(?<!%s)[«\"]?%s[»\"]?(?!%s)" % (W, body, W), re.I)


class Found:
    def __init__(self, kind, source, key, fragment):
        self.kind, self.source, self.key, self.fragment = kind, source, key, fragment


def known_items(known):
    """Шаг 1: данные из карточки. known: {patient: {full_name, birth, phone, email, address}, doctor: {name}}."""
    out = []
    pat = (known or {}).get("patient") or {}
    doc = (known or {}).get("doctor") or {}
    full = (pat.get("full_name") or "").strip()
    if full:
        out.append(Found("patient", "known", "patient", full))
        for part in full.split():  # отдельно имя, фамилия, отчество: «Пётр Иванович», «Петров», «Ли»
            if len(part) >= 2:
                out.append(Found("patient", "known", "patient", part))
        parts = full.split()
        if len(parts) >= 3:
            out.append(Found("patient", "known", "patient", parts[1] + " " + parts[2]))
    dname = (doc.get("name") or "").strip()
    if dname:
        out.append(Found("doctor", "known", "doctor", dname))
        dp = dname.split()
        if len(dp) >= 3:
            out.append(Found("doctor", "known", "doctor", dp[1] + " " + dp[2]))
        for part in dname.split():
            if len(part) >= 3 and part.lower().rstrip(".") not in ("врач", "доктор"):
                out.append(Found("doctor", "known", "doctor", part))
    for k, kind in (("phone", "phone"), ("email", "email"), ("address", "address"), ("birth", "date")):
        v = (pat.get(k) or "").strip()
        if len(v) >= 4:
            out.append(Found(kind, "known", kind, v))
    return out


NUMBERED_PLACE = re.compile(r"(?<!%s)(?:школ[аеуыой]*|гимнази[яиюей]*|лице[йяюе]м?|поликлиник[аеиуой]*|больниц[аеуыой]*|"
                            r"детск(?:ий|ом|ого|ому)\s+сад[уае]?|роддом[аеу]?|колледж[аеу]?|техникум[аеу]?|кабинет[аеу]?)"
                            r"\s+(?:номер|№|N)\s*\d+" % W, re.I)
NAMED_SCHOOL = re.compile(r"(?<!%s)(?:колледж|техникум|училищ)[а-яё]*\s+[а-яё]+(?:и|а|ы|я|в)(?!%s)" % (W, W), re.I)
SETTLEMENT = re.compile(r"(?<!%s)(?:город[еау]?|г\.|сел[еоау]|деревн[еяию]|пос[её]лк[еау]?|пос[её]лок|станиц[еау]|район[еау]?)"
                        r"\s+[А-ЯЁ][\w-]+" % W)
# крупные города России и соседних стран (основа слова ловит падежи)
CITIES = """москва санкт-петербург петербург питер новосибирск екатеринбург казань нижний новгород челябинск самара омск ростов уфа
красноярск воронеж пермь волгоград краснодар саратов тюмень тольятти ижевск барнаул ульяновск иркутск хабаровск ярославль
владивосток махачкала томск оренбург кемерово новокузнецк рязань астрахань пенза липецк киров чебоксары тула калининград курск
ставрополь сочи тверь магнитогорск иваново брянск белгород сургут владимир архангельск чита калуга смоленск волжский курган
орёл орел череповец вологда саранск мурманск якутск тамбов грозный стерлитамак кострома петрозаводск нижневартовск йошкар-ола
новороссийск таганрог сыктывкар нальчик шахты дзержинск орск братск ангарск энгельс благовещенск великий псков подольск
балашиха химки мытищи королёв королев люберцы зеленоград минск киев алматы астана ташкент бишкек ереван тбилиси баку""".split()
CITY_STEMS = {stem(c) for c in CITIES if len(c) >= 4}
SURNAME = re.compile(r"[А-ЯЁ][а-яё]+(?:ов|ев|ёв|ин|ын|ск|цк|их|ых)(?:а|у|ым|ой|ом|е|ий|ая|ого|ую|им|ей)?$")


def rule_spans(text):
    """Шаг 2: правила. Возвращает [(start, end, kind)]."""
    spans = []
    for kind, rx in RULES:
        for m in rx.finditer(text):
            spans.append((m.start(), m.end(), kind))
    for m in NUMBERED_PLACE.finditer(text):
        low = m.group().lower()
        spans.append((m.start(), m.end(), "ORG" if low.startswith(("поликлин", "больниц", "роддом", "кабинет")) else "SCHOOL"))
    for m in NAMED_SCHOOL.finditer(text):
        spans.append((m.start(), m.end(), "SCHOOL"))
    for m in SETTLEMENT.finditer(text):
        spans.append((m.start(), m.end(), "CITY"))
    for m in re.finditer(r"«[^«»]{2,40}»", text):  # название в кавычках: магазин, компания, детский сад
        spans.append((m.start(), m.end(), "ORG"))
    names = []
    for m in PATRONYMIC.finditer(text):
        names.append((m.start(), m.end()))
    for m in re.finditer(r"(?<!%s)[А-ЯЁ][а-яё-]{2,}(?!%s)" % (W, W), text):
        st = stem(m.group())
        if st in NAME_STEMS:
            names.append((m.start(), m.end()))
        elif st in CITY_STEMS or st.replace("ё", "е") in CITY_STEMS:
            spans.append((m.start(), m.end(), "CITY"))
    # фамилия рядом с найденным именем: «Сергей Кузьмин», «Кузьмину Сергею»
    for a, b in list(names):
        nx = re.match(r"\s+([А-ЯЁ][а-яё]+)", text[b:])
        if nx and (SURNAME.match(nx.group(1)) or stem(nx.group(1)) not in CITY_STEMS):
            names.append((b + nx.start(1), b + nx.end(1)))
        pv = re.search(r"([А-ЯЁ][а-яё]+)\s+$", text[:a])
        if pv and SURNAME.match(pv.group(1)):
            names.append((pv.start(1), pv.end(1)))
    spans += [(a, b, "NAME") for a, b in names]
    return spans


def clean_llm(items, text):
    """Ответ модели: оставить только короткие фрагменты, которые есть в тексте дословно."""
    out, low = [], text.lower()
    for it in items or []:
        if not isinstance(it, dict):
            continue
        frag, typ = str(it.get("text") or "").strip(" .,;:!?"), str(it.get("type") or "").upper()
        if typ not in NUMBERED or not frag or len(frag) > 60 or len(frag.split()) > 5:
            continue
        if frag.lower() not in low:
            continue  # модель придумала или переписала фрагмент
        if "[" in frag or not re.search(r"[А-ЯЁA-Z«\"\d]", frag):
            continue  # имена, места и названия пишутся с заглавной, в кавычках или с номером; «анализ крови» - нет
        words = [w for w in re.findall(r"\w+", frag.lower())]
        if not words or all(w in STOP or stem(w) in MED_STEMS for w in words) or any(stem(w) in DRUG_STEMS for w in words):
            continue
        out.append(Found(typ, "llm", None, frag))
    return out


def group_key(kind, fragment):
    """Один человек или место под одной меткой: «Марина» и «Марине» -> ИМЯ_n."""
    if kind in ("patient", "doctor"):
        return kind
    if kind in NUMBERED:
        return kind + ":" + " ".join(stem(w) for w in re.findall(r"\w+", fragment))
    return kind + ":" + re.sub(r"\W", "", fragment.lower())


def compile_variants(found):
    out = []
    for f in found:
        if len(f.fragment) < 3:  # короткая фамилия («Ли», «Ян»): только с заглавной, иначе заденет частицу «ли»
            out.append((re.compile(r"(?<!%s)%s(?!%s)" % (W, re.escape(f.fragment), W)), f))
            continue
        rx = variant_re(f.fragment) if f.kind in ("patient", "doctor", "NAME", "CITY", "ORG", "SCHOOL") else re.compile(re.escape(f.fragment), re.I)
        if rx is not None:
            out.append((rx, f))
    return out


def choose(text, variants):
    """Что заменить в тексте: [(start, end, kind, source)] без пересечений, по порядку."""
    spans = []
    for rx, f in variants:
        for m in rx.finditer(text):
            spans.append((m.start(), m.end(), f.kind, f.source))
    for a, b, kind in rule_spans(text):
        spans.append((a, b, kind, "rule"))
    # пересечения: сначала источник надёжнее, потом длиннее
    spans.sort(key=lambda x: (PRIORITY[x[3]], -(x[1] - x[0]), x[0]))
    taken, chosen = [], []
    for a, b, kind, src in spans:
        if any(a < tb and ta < b for ta, tb in taken):
            continue
        if kind in NUMBERED and all(w in STOP for w in re.findall(r"\w+", text[a:b].lower())):
            continue
        taken.append((a, b))
        chosen.append((a, b, kind, src))
    chosen.sort()
    # соседние части одного человека («Анна» «Сергеевна», «Зинаиде» «Фёдоровне») сливаются в одну метку
    merged = []
    for a, b, kind, src in chosen:
        if merged and kind in ("patient", "doctor", "NAME") and merged[-1][2] == kind and not text[merged[-1][1]:a].strip():
            merged[-1] = (merged[-1][0], b, kind, merged[-1][3])
        else:
            merged.append((a, b, kind, src))
    return merged


def masked(text, chosen):
    """Текст для модели: уже найденное шагами 1 и 2 скрыто метками без номера.
    Модели меньше переписывать в ответ, и она не тратит время на то, что уже найдено."""
    res, pos = [], 0
    for a, b, kind, _ in chosen:
        res += [text[pos:a], "[%s]" % LABEL[kind]]
        pos = b
    res.append(text[pos:])
    return "".join(res)


def deidentify(segments, known=None, llm=None):
    """segments: [{role, t, text}]. llm: функция(текст) -> [{text, type}] или None (только шаги 1 и 2).
    Возвращает (обезличенные сегменты, замены [{label, type, source, originals}], статистика)."""
    texts = [str(s.get("text") or "") for s in segments]
    found = known_items(known)
    llm_calls = 0
    if llm is not None:
        base = compile_variants(found)
        pre = [dict(seg, text=masked(t, choose(t, base))) for seg, t in zip(segments, texts)]
        for window in windows(pre):
            llm_calls += 1
            found += clean_llm(llm(window), window)
    variants = compile_variants(found)
    labels, counters, repl = {}, {}, {}

    def label_for(kind, frag):
        key = group_key(kind, frag)
        if key not in labels:
            base = LABEL[kind]
            if kind in NUMBERED:
                counters[kind] = counters.get(kind, 0) + 1
                base = "%s_%d" % (base, counters[kind])
            labels[key] = "[%s]" % base
        return labels[key]

    out = []
    for seg, text in zip(segments, texts):
        res, parts, pos = [], [], 0
        for a, b, kind, src in choose(text, variants):
            frag = text[a:b]
            lab = label_for(kind, frag)
            r = repl.setdefault(lab, {"label": lab, "type": kind, "source": src, "originals": []})
            if frag not in r["originals"]:
                r["originals"].append(frag)
            if PRIORITY[src] < PRIORITY[r["source"]]:
                r["source"] = src
            res += [text[pos:a], lab]
            parts += [text[pos:a], {"l": lab, "o": frag}]
            pos = b
        res.append(text[pos:])
        parts.append(text[pos:])
        # parts: текст кусками и замены с оригиналом; нужны gateway для «Отменить замену», наружу не уходят
        out.append(dict(seg, text="".join(res), parts=[x for x in parts if x != ""]))
    stats = {"replacements": sum(len(r["originals"]) for r in repl.values()), "labels": len(repl), "llm_calls": llm_calls}
    return out, sorted(repl.values(), key=lambda r: r["label"]), stats


ROLE_RU = {"doctor": "Врач", "patient": "Пациент"}


def windows(segments, limit=700):
    """Текст для модели короткими окнами: малая модель на длинном тексте пропускает больше."""
    buf, n = [], 0
    for s in segments:
        line = "%s: %s" % (ROLE_RU.get(s.get("role"), "Говорящий"), s.get("text") or "")
        if buf and n + len(line) > limit:
            yield "\n".join(buf)
            buf, n = [], 0
        buf.append(line)
        n += len(line) + 1
    if buf:
        yield "\n".join(buf)
