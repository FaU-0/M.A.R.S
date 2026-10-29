---
name: M.A.R.S.
description: Дистанционный приём с объективной документацией лица — в облике аптечной упаковки.
colors:
  band: "#0B5563"
  band-on: "#FFFFFF"
  carton: "#FFFFFF"
  leaflet: "#EEF2F3"
  ink: "#14171A"
  ink-secondary: "#4A565B"
  rule: "#D0D9DC"
  side-left: "#0B5563"
  side-right: "#B4530A"
  series-3: "#5E7A1F"
  series-4: "#7A4E9C"
  mesh: "#7CCAD6"
  ok: "#2F9E5A"
  ok-text: "#1E6B3C"
  bad: "#C62828"
  bad-text: "#B42318"
  warn: "#E8890C"
  warn-text: "#8A4B00"
  band-contrast: "#083F4A"
  dark-carton: "#0C1113"
  dark-leaflet: "#151D20"
  dark-panel: "#1B2529"
  dark-ink: "#EEF3F4"
  dark-ink-secondary: "#A3B1B5"
  dark-rule: "#2C3A3F"
  dark-accent: "#5BB8C6"
  dark-accent-text: "#7CCAD6"
  dark-accent-on: "#06262C"
  dark-band: "#0B4650"
  dark-side-right: "#E39A57"
typography:
  display:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"SF Pro Text\", \"Segoe UI\", Roboto, system-ui, sans-serif"
    fontSize: "34px"
    fontWeight: 600
    lineHeight: 1.15
  headline:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"Segoe UI\", Roboto, system-ui, sans-serif"
    fontSize: "22px"
    fontWeight: 600
  title:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"Segoe UI\", Roboto, system-ui, sans-serif"
    fontSize: "17px"
    fontWeight: 600
  body:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"Segoe UI\", Roboto, system-ui, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.4
  label:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"Segoe UI\", Roboto, system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 400
  data:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "13px"
    fontFeature: "\"tnum\""
rounded:
  sm: "4px"
  md: "6px"
  lg: "8px"
  pill: "32px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "24px"
components:
  header-band:
    backgroundColor: "{colors.band}"
    textColor: "{colors.band-on}"
    height: "52px"
    padding: "0 16px"
  button-primary:
    backgroundColor: "{colors.band}"
    textColor: "{colors.band-on}"
    rounded: "{rounded.md}"
    padding: "0 20px"
    height: "44px"
  button-primary-large:
    backgroundColor: "{colors.band}"
    textColor: "{colors.band-on}"
    rounded: "{rounded.lg}"
    padding: "0 28px"
    height: "52px"
  button-secondary:
    backgroundColor: "{colors.leaflet}"
    textColor: "{colors.band}"
    rounded: "{rounded.md}"
    padding: "0 20px"
    height: "44px"
  input:
    backgroundColor: "{colors.carton}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "8px 12px"
    height: "44px"
  chip:
    backgroundColor: "{colors.leaflet}"
    textColor: "{colors.ink-secondary}"
    rounded: "{rounded.sm}"
    padding: "0 10px"
    height: "24px"
  call-button:
    rounded: "{rounded.pill}"
    size: "48px"
---

# Design System: M.A.R.S.

## Overview

**Аптечная упаковка.** M.A.R.S. выглядит как хорошо подписанная коробка лекарства: белый картон, одна сплошная полоса цвета морской волны, которая называет, что у вас в руках, и плотный вкладыш с подробностями. Это рабочий инструмент врача и пациента (режим Operate): раскладка, навигация и элементы управления — стандартные, мир даёт только цвет, плотность, шрифт и фирменный приём.

Ключевые черты:
- Шапка — сплошная полоса `band` с белым текстом на всех экранах; в ней всегда видно, где вы (центр, код приёма, связь).
- Основное действие окрашено тем же цветом, что и полоса: один цвет — одно значение «главное».
- Стороны лица закреплены за цветами: левая — `side-left`, правая — `side-right`; так же окрашены первые две серии на графиках динамики.
- Панели-«вкладыши» (`leaflet`) отделяют вторичное от рабочего поля без теней.

## Colors

Стратегия — сдержанная с одной полосой: нейтральный картон и вкладыш плюс одна насыщенная морская волна, которая работает как шапка, основное действие, выбранная вкладка, фокус и выделение.

- **Морская волна (`band` #0B5563)** — шапка, основные кнопки, выбранная вкладка, рамка фокуса, отмеченные элементы. Белый текст на ней 8,4:1.
- **Картон (`carton`) и вкладыш (`leaflet`)** — фон рабочего поля и вторичные панели (левая колонка карточки, вторичные кнопки, чат собеседника).
- **Чернила (`ink`, `ink-secondary`)** — основной и вторичный текст; вторичный 7,6:1 на картоне.
- **Стороны лица** — Л `side-left`, П `side-right` (5,0:1 на белом). Не использовать эти цвета для других смыслов.
- **Состояния** — `ok`, `bad`, `warn` для заливок и точек; для текста — их `-text` варианты.
- **`mesh`** — сетка лица поверх видео; светлая в обеих темах, потому что видео тёмное.
- **Тёмная тема** — синевато-чёрный картон, акцент светлеет до `dark-accent`, текст на акцентных кнопках тёмный (`dark-accent-on`).
- **«Крупно и контрастно»** (`html.hc`) — полоса и акцент темнеют до `band-contrast`, вторичный текст и линии темнеют.

**Правило одной полосы.** Насыщенный цвет занимает шапку и одно основное действие на экране; всё остальное — картон, вкладыш и чернила.

## Typography

Одно системное семейство на всё — от заголовка до подписи к графику; табличные цифры для времени, кодов и значений. Шкала: 34 / 22 / 17 / 15 / 13 / 11 px. Базовый размер задаётся `--fs` (15px, в крупном режиме 19px); заголовки, привязанные к нему, растут вместе с текстом.

## Layout

- Приём врача: три колонки (карточка 280px, видео, панель вкладок 380px); до 1099px карточка скрывается; до 700px — одна колонка с нижней навигацией.
- Страницы-формы: колонка до 720px (широкие до 960px), отступы 24/16px.
- Ритм отступов 4/8/12/16/24px; внутри групп плотно, между группами просторно.

## Elevation & Depth

Плоско: глубина задаётся тоном (картон → вкладыш) и тонкими линиями `rule`, а не тенями. Единственные «слои» — листы (sheet) с затемнением фона и панель звонка поверх видео.

## Shapes

Небольшие скругления, как у вырубки картона: 4px (чипы, сегменты), 6px (кнопки, поля), 8px (крупные кнопки, карточки, видео). Круглые только кнопки звонка.

## Components

- **Шапка-полоса** — `band`, белые иконки, код приёма на полупрозрачной белой подложке.
- **Кнопки** — основная (полоса), вторичная (вкладыш с тонкой рамкой), опасная (`bad`), текстовая.
- **Вкладки панели** — без заливки; выбранная выделена цветом полосы и подчёркиванием 3px снизу.
- **Карточка согласия** — вся строка нажимается; отмеченная получает рамку и лёгкую заливку цветом полосы.
- **Панель звонка** — круглые кнопки с подписями под ними; на телефоне четыре главные.
- **Чипы состояний** — плоские, скругление 4px, полупрозрачная заливка цвета состояния.

## Do's and Don'ts

- **Делайте** одно основное действие цвета полосы на экран.
- **Делайте** Л и П только цветами `side-left` и `side-right`.
- **Делайте** недостоверное видимым с пометкой на месте, а не скрытым.
- **Не делайте** теней и стекла для отделения панелей — только тон и линии.
- **Не используйте** морскую волну для декора и неактивных состояний.
- **Не вводите** второй шрифт для заголовков.
