from __future__ import annotations

from dataclasses import dataclass

from app.extraction.ar import ExtractedTable, SheetInfo, find_column_index

@dataclass
class VorRow:
    razdel: str
    podrazdel: str
    naimenovanie_raboty: str
    edinitsa: str
    obyom: str
    shifr: str
    nomer_lista: str
    ishodnoe_znachenie: str
    formula: str
    status: str
    kommentariy: str


def build_floor_vor_rows(sheet: SheetInfo, floor_table: ExtractedTable | None, rules: dict) -> tuple[list[VorRow], list[str]]:
    warnings: list[str] = []
    rule = rules["floor_explication_rules"][0]

    if floor_table is None:
        warnings.append(
            "Не найдена таблица 'Экспликация полов' по ключевым словам — "
            "проверьте качество OCR (нужен пакет tesseract-ocr-rus) либо "
            "расширьте ключевые слова поиска в process_ar_sheet."
        )
        return [], warnings

    table = floor_table
    area_col = find_column_index(table.headers, rule["match_column_header_keywords"])
    if area_col is None:
        warnings.append(f"В шапке таблицы {table.bbox} не найдена колонка 'Площадь'. Шапка: {table.headers}")
        return [], warnings

    finish_col = 3 if len(table.headers) > 3 else None  # 'Состав конструкции' — 4-я типовая колонка

    rows_out: list[VorRow] = []
    for row in table.rows:
        raw_value = row[area_col].strip() if area_col < len(row) else ""
        if not raw_value:
            continue
        import re
        numbers = re.findall(r"\d+[.,]\d+", raw_value)
        if not numbers:
            continue

        finish_text = row[finish_col].strip() if finish_col is not None and finish_col < len(row) else ""
        podrazdel = rule["podrazdel_by_finish_keywords"]["default"]
        for kw, pod in rule["podrazdel_by_finish_keywords"].items():
            if kw != "default" and kw.lower() in finish_text.lower():
                podrazdel = pod
                break

        for num in numbers:
            rows_out.append(VorRow(
                razdel=rule["razdel"],
                podrazdel=podrazdel,
                naimenovanie_raboty=f"Устройство пола по составу: {finish_text[:120] or 'см. состав конструкции на листе'}",
                edinitsa=rule["unit_out"],
                obyom=num.replace(",", "."),
                shifr=sheet.sheet_code or "?",
                nomer_lista=str(sheet.page_number),
                ishodnoe_znachenie=f"{num} {rule['unit_out']} (графа 'Площадь', экспликация полов)",
                formula="=значение из ячейки как есть",
                status="Черновой (OCR — обязательна проверка человеком)",
                kommentariy=f"Источник: таблица bbox={table.bbox}. Наименование помещения распознано как: {row[0][:60]!r}",
            ))

    return rows_out, warnings