"""
Применение правил из rules_kj.json к фактам, извлечённым kj.py.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from app.extraction.kj import SpecRow, SteelSummary, SheetInfo, load_rules


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


def apply_concrete_rules(spec_rows: list[SpecRow], sheet: SheetInfo, rules: dict) -> list[VorRow]:
    out = []
    for rule in rules["concrete_rules"]:
        for row in spec_rows:
            name = row.naimenovanie
            if not any(kw in name for kw in rule["match_keywords"]):
                continue
            if any(kw in name for kw in rule.get("exclude_keywords", [])):
                continue
            try:
                value = float(row.kolichestvo.replace(",", "."))
            except ValueError:
                continue
            result = round(value * rule["coefficient"], rule["round_digits"])
            out.append(VorRow(
                razdel=rule["razdel"],
                podrazdel=rule["podrazdel"],
                naimenovanie_raboty=rule["naimenovanie_template"].format(klass="БСМ В25 W4 F75 ГОСТ7473-2010"),
                edinitsa=rule["unit_out"],
                obyom=str(result),
                shifr=sheet.sheet_code or "?",
                nomer_lista=str(sheet.page_number),
                ishodnoe_znachenie=f"{row.kolichestvo} {rule['unit_in']} (поз. '{name}')",
                formula=f"{row.kolichestvo} х {rule['coefficient']}",
                status="Черновой",
                kommentariy=f"Источник: таблица 'Материалы', bbox={row.source.bbox}",
            ))
    return out


def apply_steel_rules(steel_summary: SteelSummary, sheet: SheetInfo, rules: dict) -> list[VorRow]:
    out = []
    for rule in rules["steel_rules"]:
        for label in rule["match_keywords"]:
            if label not in steel_summary.values:
                continue
            try:
                value = float(steel_summary.values[label])
            except ValueError:
                continue
            result = round(value * rule["coefficient"], rule["round_digits"])
            out.append(VorRow(
                razdel=rule["razdel"],
                podrazdel=rule["podrazdel"],
                naimenovanie_raboty=rule["naimenovanie_template"].format(klass=""),
                edinitsa=rule["unit_out"],
                obyom=str(result),
                shifr=sheet.sheet_code or "?",
                nomer_lista=str(sheet.page_number),
                ishodnoe_znachenie=f"{steel_summary.values[label]} {rule['unit_in']} (столбец '{label}' сводной ведомости)",
                formula=f"{steel_summary.values[label]} х {rule['coefficient']}",
                status="Требует уточнения",
                kommentariy="Автоматически: пройти сверку суммы позиций спецификации до подтверждения (см. cross_check_steel)",
            ))
    return out


def cross_check_steel(spec_rows: list[SpecRow], steel_summary: SteelSummary, rules: dict) -> dict:
    cfg = rules["cross_check"]["steel_sum_vs_total"]
    exclude_kw = cfg["exclude_position_keywords"]

    total_from_spec = 0.0
    used_rows = []
    for row in spec_rows:
        if any(kw in row.naimenovanie for kw in exclude_kw):
            continue
        raw = row.masa_vsego.strip()
        # некоторые ячейки засорены посторонними пометками (см. extract.py);
        # берём последнее число в ячейке как значение массы
        import re
        nums = re.findall(r"\d+\.\d+", raw)
        if not nums:
            continue
        total_from_spec += float(nums[-1])
        used_rows.append(row.poz)

    total_from_summary = None
    if "Всего" in steel_summary.values:
        try:
            total_from_summary = float(steel_summary.values["Всего"])
        except ValueError:
            pass

    diff = None
    ok = None
    if total_from_summary is not None:
        diff = round(total_from_summary - total_from_spec, 2)
        ok = abs(diff) <= cfg["tolerance_kg"]

    return {
        "total_from_spec_kg": round(total_from_spec, 2),
        "total_from_summary_kg": total_from_summary,
        "difference_kg": diff,
        "within_tolerance": ok,
        "positions_used": used_rows,
    }


def build_vor_draft(sheet: SheetInfo, spec_rows: list[SpecRow], steel_summary: SteelSummary) -> tuple[list[VorRow], dict]:
    rules = load_rules()
    rows = []
    rows += apply_concrete_rules(spec_rows, sheet, rules)
    rows += apply_steel_rules(steel_summary, sheet, rules)
    check = cross_check_steel(spec_rows, steel_summary, rules)
    return rows, check