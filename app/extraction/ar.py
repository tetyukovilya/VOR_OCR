"""
Извлечение фактов с листа АР, когда pdf не содержит текстового слоя
(шрифт сконвертирован в кривые при экспорте из CAD — типичный случай).

Стратегия: рендерим страницу/область в растр, распознаём OCR (координаты
слов), затем структуру таблицы (строки/колонки) восстанавливаем
кластеризацией слов по координатам — а не по линиям PDF, потому что
внутренние разделители таблицы часто не сплошные (см. AR4_9_L6_9.pdf).

Использовать rus+eng языковую модель Tesseract на своей машине:
    sudo apt install tesseract-ocr-rus
и указать "lang": "rus+eng" в rules.json — здесь код рассчитан именно
на это, но протестирован в песочнице только с 'eng' (без кириллицы).
"""
from __future__ import annotations

import re
import json
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber
import pytesseract
from PIL import Image

#Если tesseract.exe не в PATH, укажите путь явно
# Раскомментируйте и поправьте путь под свою установку (обычно один из
# вариантов ниже). Проверить путь можно, открыв папку установки Tesseract
# и убедившись, что файл tesseract.exe там есть.
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
# pytesseract.pytesseract.tesseract_cmd = r"C:\Users\<ВашПользователь>\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"

RULES_PATH = Path(__file__).parent / "rules_ar.json"
PT_TO_INCH = 1 / 72.0


def load_rules() -> dict:
    with open(RULES_PATH, encoding="utf-8") as f:
        return json.load(f)


@dataclass
class SourceRef:
    file: str
    page_number: int
    bbox: tuple  # (x0, top, x1, bottom) в points исходной страницы


@dataclass
class SheetInfo:
    file: str
    page_number: int
    sheet_code: str | None
    page_width: float
    page_height: float


@dataclass
class OcrWord:
    text: str
    x0: float
    top: float
    x1: float
    bottom: float
    conf: float


@dataclass
class TableCell:
    row_idx: int
    col_idx: int
    text: str


@dataclass
class ExtractedTable:
    bbox: tuple
    headers: list[str]
    rows: list[list[str]]  # rows[i][j] -> текст ячейки строки i, колонки j
    source: SourceRef


def preprocess_image(im: Image.Image, cfg: dict) -> Image.Image:
    """Предобработка растра перед OCR: перевод в градации серого + бинаризация
    по Отсу + опциональное увеличение"""
    if not cfg.get("enabled", True):
        return im

    import numpy as np

    if cfg.get("grayscale", True):
        im = im.convert("L")

    factor = cfg.get("upscale_factor", 1.0)
    if factor and factor != 1.0:
        w, h = im.size
        im = im.resize((int(w * factor), int(h * factor)), Image.LANCZOS)

    if cfg.get("binarize_otsu", True):
        arr = np.array(im.convert("L"))
        # Отсу вручную (без OpenCV): перебор порогов по гистограмме
        hist, _ = np.histogram(arr.flatten(), bins=256, range=(0, 256))
        total = arr.size
        sum_all = np.dot(np.arange(256), hist)
        sum_bg, w_bg, best_thresh, best_var = 0.0, 0, 0, 0.0
        for t in range(256):
            w_bg += hist[t]
            if w_bg == 0:
                continue
            w_fg = total - w_bg
            if w_fg == 0:
                break
            sum_bg += t * hist[t]
            mean_bg = sum_bg / w_bg
            mean_fg = (sum_all - sum_bg) / w_fg
            var_between = w_bg * w_fg * (mean_bg - mean_fg) ** 2
            if var_between > best_var:
                best_var = var_between
                best_thresh = t
        arr = np.where(arr > best_thresh, 255, 0).astype("uint8")
        im = Image.fromarray(arr)

    return im


def build_tesseract_config(rules: dict, column_ocr_cfg: dict | None = None) -> tuple[str, str]:
    """Собирает (lang, config-строку для pytesseract) с учётом tessdata_best и настроек колонки."""
    ocr_cfg = rules["ocr"]
    lang = ocr_cfg["lang"]
    psm = column_ocr_cfg.get("psm", 6) if column_ocr_cfg else 6
    parts = [f"--psm {psm}"]
    if ocr_cfg.get("tessdata_dir"):
        parts.append(f'--tessdata-dir "{ocr_cfg["tessdata_dir"]}"')
    if column_ocr_cfg and column_ocr_cfg.get("tesseract_config_extra"):
        parts.append(column_ocr_cfg["tesseract_config_extra"])
    return lang, " ".join(parts)


def ocr_region(page, bbox: tuple, rules: dict, column_ocr_cfg: dict | None = None) -> list[OcrWord]:
    """
    OCR произвольной области PDF-страницы. Координаты возвращаемых слов —
    в системе координат исходной страницы PDF (в points), не пикселей,
    чтобы их можно было сопоставлять с page.crop()/find_tables() и
    сохранять как ссылку на источник.
    """
    dpi = (column_ocr_cfg or {}).get("resolution_dpi", rules["ocr"]["resolution_dpi"])
    min_conf = rules["ocr"]["min_confidence"]
    scale = dpi / 72.0  # points переводим в pixels

    crop = page.crop(bbox)
    im = crop.to_image(resolution=dpi).original
    im = preprocess_image(im, rules["ocr"].get("preprocess", {"enabled": False}))
    lang, config = build_tesseract_config(rules, column_ocr_cfg)

    data = pytesseract.image_to_data(im, lang=lang, output_type=pytesseract.Output.DICT, config=config)

    words = []
    n = len(data["text"])
    for i in range(n):
        text = data["text"][i].strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (ValueError, TypeError):
            conf = -1
        if conf < min_conf:
            continue
        px_x0, px_top = data["left"][i], data["top"][i]
        px_w, px_h = data["width"][i], data["height"][i]
        actual_scale_x = im.size[0] / (bbox[2] - bbox[0])
        actual_scale_y = im.size[1] / (bbox[3] - bbox[1])
        x0 = bbox[0] + px_x0 / actual_scale_x
        top = bbox[1] + px_top / actual_scale_y
        x1 = x0 + px_w / actual_scale_x
        bottom = top + px_h / actual_scale_y
        words.append(OcrWord(text=text, x0=x0, top=top, x1=x1, bottom=bottom, conf=conf))
    return words


def cluster_rows(words: list[OcrWord], y_gap_pt: float) -> list[list[OcrWord]]:
    """Группирует слова в строки по Y-координате (Кластеризация по разрыву)."""
    if not words:
        return []
    ws = sorted(words, key=lambda w: w.top)
    rows: list[list[OcrWord]] = [[ws[0]]]
    for w in ws[1:]:
        prev_row = rows[-1]
        prev_bottom = max(x.bottom for x in prev_row)
        if w.top - prev_bottom > y_gap_pt:
            rows.append([w])
        else:
            prev_row.append(w)
    for row in rows:
        row.sort(key=lambda w: w.x0)
    return rows


def build_column_boundaries(header_row: list[OcrWord], gap_pt: float) -> list[tuple[str, float, float]]:
    """
    Из слов шапки строит список колонок (заголовок, x_start, x_end),
    склеивая соседние слова шапки в один заголовок колонки, если разрыв
    между ними меньше gap_pt (иначе "Наименование" и "помещений"
    считались бы разными колонками)
    """
    if not header_row:
        return []
    groups: list[list[OcrWord]] = [[header_row[0]]]
    for w in header_row[1:]:
        prev = groups[-1]
        if w.x0 - prev[-1].x1 <= gap_pt:
            prev.append(w)
        else:
            groups.append([w])

    columns = []
    for i, g in enumerate(groups):
        title = " ".join(w.text for w in g)
        x_start = g[0].x0 if i > 0 else float("-inf")
        columns.append((title, x_start, g[-1].x1))

    result = []
    for i, (title, x_start, x_end) in enumerate(columns):
        next_start = columns[i + 1][1] if i + 1 < len(columns) else float("inf")
        result.append((title, x_start, next_start))
    return result


def assign_row_to_columns(row_words: list[OcrWord], columns: list[tuple[str, float, float]]) -> list[str]:
    cells = [[] for _ in columns]
    for w in row_words:
        center = (w.x0 + w.x1) / 2
        for idx, (_, x_start, x_end) in enumerate(columns):
            if x_start <= center < x_end:
                cells[idx].append(w)
                break
    LINE_BUCKET_PT = 4
    return [
        " ".join(
            w.text for w in sorted(cw, key=lambda w: (round(w.top / LINE_BUCKET_PT) * LINE_BUCKET_PT, w.x0))
        )
        for cw in cells
    ]


def extract_table_by_bbox(page, bbox: tuple, file_name: str, rules: dict) -> ExtractedTable:
    words = ocr_region(page, bbox, rules)
    rows = cluster_rows(words, rules["row_clustering"]["y_gap_pt"])
    if not rows:
        return ExtractedTable(bbox=bbox, headers=[], rows=[], source=SourceRef(file_name, 1, bbox))

    header_row, *data_rows = rows
    columns = build_column_boundaries(header_row, rules["column_clustering"]["header_word_gap_pt"])
    headers = [c[0] for c in columns]

    out_rows = [assign_row_to_columns(r, columns) for r in data_rows]
    return ExtractedTable(
        bbox=bbox, headers=headers, rows=out_rows, source=SourceRef(file_name, 1, bbox)
    )


def extract_table_fixed_columns(
    page, bbox: tuple, file_name: str, rules: dict,
    column_boundaries_ratio: list[float], column_names: list[str], header_height_pt: float,
    column_ocr_cfg: dict | None = None, data_top_extra_pt: float = 0,) -> ExtractedTable:
    x0, top, x1, bottom = bbox
    data_top = top + header_height_pt - data_top_extra_pt
    table_width = x1 - x0

    col_cfg = column_ocr_cfg or {}
    type_by_column = col_cfg.get("type_by_column", ["text"] * (len(column_boundaries_ratio) - 1))

    all_words: list[OcrWord] = []
    col_bounds = []
    for i in range(len(column_boundaries_ratio) - 1):
        cx0 = x0 + column_boundaries_ratio[i] * table_width
        cx1 = x0 + column_boundaries_ratio[i + 1] * table_width
        col_bounds.append((column_names[i], cx0, cx1))

        col_type = type_by_column[i] if i < len(type_by_column) else "text"
        if col_type == "skip":
            continue

        type_cfg = col_cfg.get(col_type, {})
        col_words = ocr_region(page, (cx0, data_top, cx1, bottom), rules, column_ocr_cfg=type_cfg)
        all_words.extend(col_words)

    rows = cluster_rows(all_words, rules["row_clustering"]["y_gap_pt"])
    out_rows = [assign_row_to_columns(r, col_bounds) for r in rows]
    return ExtractedTable(
        bbox=bbox, headers=column_names, rows=out_rows, source=SourceRef(file_name, 1, bbox)
    )


def find_candidate_table_bboxes(page) -> list[tuple]:
    page_area = page.width * page.height
    out = []
    for t in page.find_tables():
        x0, top, x1, bottom = t.bbox
        area = (x1 - x0) * (bottom - top)
        if area > 0.5 * page_area or area < 0.01 * page_area:
            continue
        out.append(t.bbox)
    return out


def detect_sheet_code(page, file_name: str, rules: dict) -> str | None:
    """Шифр листа через OCR зоны штампа (текстового слоя на листе нет вовсе)."""
    zone = rules["sheet_stamp_zone"]
    bbox = (
        page.width * zone["x_min_ratio"],
        page.height * zone["y_min_ratio"],
        page.width,
        page.height,
    )
    words = ocr_region(page, bbox, rules)
    blob = " ".join(w.text for w in words)
    m = re.search(rules["sheet_code_regex"], blob)
    return m.group(0) if m else None


def select_table_by_header_keywords(tables: list[ExtractedTable], keywords: list[str]) -> ExtractedTable | None:
    for t in tables:
        blob = " ".join(t.headers) + " " + " ".join(" ".join(r) for r in t.rows)
        blob = blob.lower()
        if any(kw.lower() in blob for kw in keywords):
            return t
    return None


def select_table_by_numeric_last_column(tables: list[ExtractedTable], min_rows: int = 3, min_fraction: float = 0.5) -> ExtractedTable | None:
    num_re = re.compile(r"^\d+[.,]\d+$")
    best = None
    best_score = 0.0
    for t in tables:
        if len(t.rows) < min_rows:
            continue
        hits = 0
        counted = 0
        for row in t.rows:
            non_empty = [c for c in row if c.strip()]
            if not non_empty:
                continue
            last = non_empty[-1].strip()
            counted += 1
            if any(num_re.match(tok) for tok in last.replace(",", ".").split()) or num_re.match(last):
                hits += 1
        if counted == 0:
            continue
        score = hits / counted
        if score > best_score and score >= min_fraction:
            best_score = score
            best = t
    return best


def find_column_index(headers: list[str], keywords: list[str]) -> int | None:
    for idx, h in enumerate(headers):
        if any(kw.lower() in h.lower() for kw in keywords):
            return idx
    return None


def check_tesseract_available() -> None: #Оставлено для того, чтобы можно было поймать ошибку.
    # Долго мучался именно с ней. Возможно, будет полезным кому-то
    try:
        pytesseract.get_tesseract_version()
    except Exception as e:
        raise RuntimeError(
            "Tesseract OCR не найден. Это отдельная программа (не Python-пакет), "
            "её нужно установить отдельно: https://github.com/UB-Mannheim/tesseract/wiki "
            "(Windows) или 'sudo apt install tesseract-ocr tesseract-ocr-rus' (Linux). "
            "Если Tesseract уже установлен, но не в PATH — укажите путь явно в начале "
            "extract.py: pytesseract.pytesseract.tesseract_cmd = r'C:\\Program Files\\Tesseract-OCR\\tesseract.exe'"
        ) from e


def process_ar_sheet(pdf_path: str) -> dict:
    check_tesseract_available()
    rules = load_rules()
    file_name = Path(pdf_path).name

    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0]
        sheet = SheetInfo(
            file=file_name,
            page_number=1,
            sheet_code=detect_sheet_code(page, file_name, rules),
            page_width=page.width,
            page_height=page.height,
        )
        candidate_bboxes = find_candidate_table_bboxes(page)
        rough_tables = [extract_table_by_bbox(page, bbox, file_name, rules) for bbox in candidate_bboxes]

        floor_cfg = rules.get("floor_explication_table")
        floor_table = None
        if floor_cfg:
            selected = select_table_by_numeric_last_column(rough_tables)
            if selected is None:
                selected = select_table_by_header_keywords(
                    rough_tables, ["площад", "m2", "м2", "экспликация полов"]
                )
            if selected is not None:
                left_extend = floor_cfg.get("left_edge_extend_pt", 0)
                corrected_bbox = (
                    selected.bbox[0] - left_extend,
                    selected.bbox[1],
                    selected.bbox[2],
                    selected.bbox[3],
                )
                floor_table = extract_table_fixed_columns(
                    page, corrected_bbox, file_name, rules,
                    column_boundaries_ratio=floor_cfg["column_boundaries_ratio"],
                    column_names=floor_cfg["column_names"],
                    header_height_pt=floor_cfg["header_height_pt"],
                    column_ocr_cfg=floor_cfg.get("column_ocr"),
                    data_top_extra_pt=floor_cfg.get("data_top_extra_pt", 0),
                )

    tables = rough_tables if floor_table is None else [
        floor_table if t.bbox == floor_table.bbox else t for t in rough_tables
    ]
    return {"sheet": sheet, "tables": tables, "floor_table": floor_table}


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        pdf_path = sys.argv[1]
    else:
        pdf_path = input("Укажите путь к PDF-файлу листа АР: ").strip().strip('"')

    result = process_ar_sheet(pdf_path)
    print("Шифр листа:", result["sheet"].sheet_code)
    for i, t in enumerate(result["tables"]):
        print(f"\n=== Таблица {i} bbox={t.bbox} ===")
        print("Заголовки:", t.headers)
        for row in t.rows:
            print(" ", row)