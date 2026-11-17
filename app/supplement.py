import csv, io, json
from app.config import EVENT_COLUMNS
from app.errors import DomainError
from app.normalize import decode

# A supplement is merged below the batch input it amends.  Columns are matched
# by name (not position), so the original line evidence stays meaningful and
# late releases / expiry corrections replay as ordinary events at event time.
def header_of(text):
    first = text.split("\n", 1)[0]
    try:
        cells = next(csv.reader([first], strict=True))
    except (csv.Error, StopIteration):
        raise DomainError("HEADER", "字段名读取失败")
    return [value.strip().lstrip("﻿") for value in cells]


def text_of(supplement_raw):
    """Decode a supplement once, so identity and stored evidence agree."""
    if isinstance(supplement_raw, str):
        text = supplement_raw
    else:
        text, _ = decode(supplement_raw)
    if not text:
        raise DomainError("EMPTY", "补传文件没有数据")
    if not text.strip():
        raise DomainError("BLANK_ONLY", "补传仅含空行")
    return text


def body_of(text):
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        next(reader)
        rows = [row for row in reader]
    except csv.Error:
        raise DomainError("CSV", "CSV引号未闭合")
    if not any(any(cell.strip() for cell in row) for row in rows):
        raise DomainError("HEADER_ONLY", "补传只有字段名")
    return rows


def canonical(text):
    """Column-order- and line-ending-insensitive identity for a supplement.

    merge() re-emits rows in the base column order, so two requests that differ
    only in column order or line endings describe the same supplement and must
    share identity.  Values themselves stay significant.
    """
    header = header_of(text)
    order = sorted(range(len(header)), key=header.__getitem__)
    rows = [[row[index] for index in order]
            for row in body_of(text) if len(row) == len(header)]
    return json.dumps([sorted(header), rows], ensure_ascii=False)


def merge(base_text, supplement):
    """Return (replayable combined input, supplement evidence text)."""
    supplement = text_of(supplement)
    base_header = header_of(base_text)
    supplement_header = header_of(supplement)
    if sorted(supplement_header) != sorted(base_header):
        raise DomainError("HEADER", "补传字段名必须与原批次一致")
    order = [supplement_header.index(name) for name in base_header]
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    for row in body_of(supplement):
        if len(row) != len(supplement_header):
            raise DomainError("COLUMN_COUNT", "补传字段数不符")
        writer.writerow([row[index] for index in order])
    appended = stream.getvalue()
    combined = base_text
    if not combined.endswith("\n"):
        combined += "\n"
    return combined + appended, supplement


def rebuild(result):
    """Recover replayable input for a batch stored before raw text was kept.

    Accepted rows and quarantined rows that carry a cell mapping keep their
    original line numbers; blank padding preserves the line evidence that the
    supplement diff refers to.
    """
    stage = (result.get("stages") or [{}])[0]
    rows = [row for row in (stage.get("rows") or []) if "_line" in row]
    if not rows:
        raise DomainError("NO_SOURCE", "原批次缺少原始输入，无法补传", 409)
    header = list(result.get("header") or EVENT_COLUMNS)
    lines = {}
    for row in rows:
        lines.setdefault(int(row["_line"]), [str(row.get(column, "")) for column in header])
    for item in result.get("quarantine", []):
        cells = item.get("raw")
        try:
            number = int(item.get("line"))
        except (TypeError, ValueError):
            continue
        if isinstance(cells, dict) and cells and set(header) <= set(cells):
            lines.setdefault(number, [str(cells.get(column, "")) for column in header])
            continue
        try:
            from ast import literal_eval
            parsed = literal_eval(item.get("detail") or "")
        except (ValueError, SyntaxError):
            parsed = None
        if isinstance(parsed, (list, tuple)):
            lines.setdefault(number, [str(value) for value in parsed])
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(header)
    for number in range(2, max(lines) + 1 if lines else 2):
        writer.writerow(lines.get(number, [""]))
    return stream.getvalue()
