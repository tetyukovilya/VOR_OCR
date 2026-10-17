"""
Извлечение фактов из листа раздела КЖ (спецификации армирования и материалы). Расчёт объёмов ВОР —
отдельный шаг (rules_engine.py), использующий rules.json.
"""
from __future__ import annotations

import re
import json
from dataclasses import dataclass, field
from pathlib import Path
import pdfplumber

RULES_PATH = Path(__file__).parent / "rules_kj.json"


@dataclass
class SourceRef:
    file: str
    page_number: int
    bbox: tuple


@dataclass
class SheetInfo:
    file: str
    page_number: int
    sheet_code: str | None
    page_width: float
    page_height: float


@dataclass
class SpecRow:
    """Одна строка таблицы 'Спецификация к армированию сечений' + Материалы."""
    poz: str
    naimenovanie: str
    kolichestvo: str
    masa_ed: str
    masa_vsego: str
    source: SourceRef


@dataclass
class SteelSummary:
    """Сводная ведомость расхода стали, кг — тотал по классам арматуры."""
    values: dict = field(default_factory=dict)
    source: SourceRef | None = None


def load_rules() -> dict:
    with open(RULES_PATH, encoding="utf-8") as f:
        return json.load(f)


def detect_sheet_code(page, rules: dict) -> str | None:
    """
    Шифр документа ищется в зоне штампа,
    """
    zone = rules["sheet_stamp_zone"]
    pattern = rules["sheet_code_regex"]
    x_min = page.width * zone["x_min_ratio"]
    y_min = page.height * zone["y_min_ratio"]

    candidates = []
    for w in page.extract_words():
        if w["x0"] >= x_min and w["top"] >= y_min:
            m = re.fullmatch(pattern, w["text"])
            if m:
                candidates.append(w["text"])
    if not candidates:
        return None
    return max(set(candidates), key=candidates.count)


def find_table_by_headers(page, required_headers: list[str]):
    page_area = page.width * page.height
    for t in page.find_tables():
        tx0, ttop, tx1, tbottom = t.bbox
        table_area = (tx1 - tx0) * (tbottom - ttop)
        if table_area > 0.5 * page_area:
            continue
        data = t.extract()
        header_blob = " ".join(
            " ".join(c for c in row if c) for row in data[:3]
        )
        if all(h in header_blob for h in required_headers):
            return t, data
    return None, None


def extract_specification_and_materials(page, file_name: str) -> list[SpecRow]:
    """
    Таблица 'Спецификация к армированию сечений' + примыкающий блок 'Материалы'.
    На листе это одна векторная таблица (найдено find_tables), но НЕ каждая
    строка полноценна: у составных позиций (∅12, ∅10 и т.п.) название и
    количество/масса лежат в соседних физических строках. Склеиваем по
    правилу: если в строке нет Кол./Масса — это заголовок позиции, значения
    берём из следующей строки.
    """
    table, data = find_table_by_headers(
        page, required_headers=["Обозначение", "Наименование", "Масса"]
    )
    if table is None:
        return []

    rows: list[SpecRow] = []
    pending_name = None
    header_done = False

    for raw in data:
        cells = [(c or "").strip() for c in raw]
        if not header_done:
            # пропускаем строку заголовка таблицы
            if "Наименование" in " ".join(cells):
                header_done = True
            continue
        if not any(cells):
            continue

        poz, obozn, naim, kol, masa_ed, masa_total = (cells + [""] * 6)[:6]
        lines_poz = poz.split("\n")
        lines_kol = kol.split("\n")
        lines_ed = masa_ed.split("\n")
        lines_total = masa_total.split("\n")
        lines_naim = naim.split("\n")

        real_split = len(lines_kol) > 1 and len(lines_kol) == len(lines_total)

        if real_split:
            n = len(lines_kol)
            sub_rows = []
            for i in range(n):
                sub_rows.append((
                    lines_poz[i] if i < len(lines_poz) else (lines_poz[-1] if lines_poz else ""),
                    lines_naim[i] if i < len(lines_naim) else (lines_naim[-1] if lines_naim else ""),
                    lines_kol[i],
                    lines_ed[i] if i < len(lines_ed) else lines_ed[-1],
                    lines_total[i],
                ))
        else:
            sub_rows = [(
                lines_poz[-1] if lines_poz else poz,
                " ".join(lines_naim) if lines_naim else naim,
                lines_kol[-1] if lines_kol else kol,
                lines_ed[-1] if lines_ed else masa_ed,
                lines_total[-1] if lines_total else masa_total,
            )]

        for s_poz, s_naim, s_kol, s_ed, s_total in sub_rows:
            combined_name = " — ".join(x for x in (obozn, s_naim) if x)
            if combined_name and not s_kol and not s_total:
                pending_name = combined_name
                continue

            display_name = combined_name if combined_name else (pending_name or "")
            if not (s_kol or s_total):
                continue

            rows.append(
                SpecRow(
                    poz=s_poz,
                    naimenovanie=display_name,
                    kolichestvo=s_kol,
                    masa_ed=s_ed,
                    masa_vsego=s_total,
                    source=SourceRef(file_name, 1, table.bbox),
                )
            )
            pending_name = None

    return rows


def extract_steel_summary(page, file_name: str, rules: dict) -> SteelSummary:
    """
    Сводная 'Ведомость расхода стали, кг'. Векторная таблица здесь неполная
    (find_tables теряет строку значений), поэтому строку итогов ищем по
    словам рядом с заголовком, сопоставляя x-координаты слово->колонка.
    """
    table, header_data = find_table_by_headers(
        page, required_headers=["Марка", "Арматура класса"]
    )
    if table is None:
        return SteelSummary()

    x0, top, x1, bottom = table.bbox
    ROW_BAND = 30
    search_bbox = (x0, top, x1, min(bottom + ROW_BAND, page.height))
    crop = page.crop(search_bbox)
    words = crop.extract_words()

    header_words = [w for w in words if w["top"] <= bottom]
    value_words = [w for w in words if w["top"] > bottom]

    col_labels = ["∅12", "∅10", "∅8", "Итого", "∅6", "Всего"]
    header_positions = [w for w in header_words if w["text"] in col_labels]

    if not header_positions or not value_words:
        return SteelSummary(source=SourceRef(file_name, 1, table.bbox))

    num_re = re.compile(r"^\d+[.,]\d+$")  # только "настоящие" дробные значения массы,
    # чтобы не подхватить служебный номер позиции ("1") без дробной части
    numeric_values = [w for w in value_words if num_re.match(w["text"])]
    MAX_X_DIST = 40
    result = {}
    used_ids = set()
    label_counts: dict[str, int] = {}
    for val in sorted(numeric_values, key=lambda w: w["x0"]):
        available = [h for h in header_positions if id(h) not in used_ids]
        if not available:
            break
        nearest = min(available, key=lambda h: abs(h["x0"] - val["x0"]))
        if abs(nearest["x0"] - val["x0"]) > MAX_X_DIST:
            continue
        used_ids.add(id(nearest))
        label = nearest["text"]
        label_counts[label] = label_counts.get(label, 0) + 1
        key = label if label_counts[label] == 1 else f"{label}_{label_counts[label]}"
        result[key] = val["text"].replace(",", ".")

    return SteelSummary(values=result, source=SourceRef(file_name, 1, table.bbox))


def process_kj_sheet(pdf_path: str) -> dict:
    rules = load_rules()
    file_name = Path(pdf_path).name

    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0]
        sheet = SheetInfo(
            file=file_name,
            page_number=1,
            sheet_code=detect_sheet_code(page, rules),
            page_width=page.width,
            page_height=page.height,
        )
        spec_rows = extract_specification_and_materials(page, file_name)
        steel_summary = extract_steel_summary(page, file_name, rules)

    return {
        "sheet": sheet,
        "spec_rows": spec_rows,
        "steel_summary": steel_summary,
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        pdf_path = sys.argv[1]
    else:
        pdf_path = input("Укажите путь к PDF-файлу листа КЖ: ").strip().strip('"')

    result = process_kj_sheet(pdf_path)
    print("Шифр листа:", result["sheet"].sheet_code)
    print(f"\nСтрок в спецификации/материалах: {len(result['spec_rows'])}")
    for r in result["spec_rows"]:
        print(f"  {r.poz:>6} | {r.naimenovanie[:45]:45} | кол={r.kolichestvo:>6} | "
              f"масса_ед={r.masa_ed:>6} | масса_всего={r.masa_vsego}")
    print("\nСводная ведомость стали:", result["steel_summary"].values)