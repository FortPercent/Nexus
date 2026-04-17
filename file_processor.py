"""上传文件预处理：xlsx / csv / zip → 转 markdown 或递归解压后再送 Letta。
其他格式 (pdf / docx / txt / md) 原样透传由 Letta 自己处理。
"""
import csv as _csv
import io
import logging
import os
import zipfile
from typing import List, Tuple

from fastapi import HTTPException


ProcessedFile = Tuple[str, bytes, str]  # (filename, content_bytes, mime_type)

ZIP_MAX_DEPTH = 3
ZIP_MAX_FILES = 50
ZIP_MAX_UNCOMPRESSED = 200 * 1024 * 1024  # 200 MB

PASSTHROUGH_EXTS = {"pdf", "docx", "txt", "md"}


def _ext(filename: str) -> str:
    return (filename.rsplit(".", 1)[-1] or "").lower() if "." in filename else ""


def _xlsx_to_markdown(data: bytes) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    out = []
    for sheet in wb.sheetnames:
        ws = wb[sheet]
        out.append(f"## 📑 工作表: {sheet}")
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            out.append("_(空表)_")
            out.append("")
            continue
        # header: first non-empty row
        header_idx = 0
        while header_idx < len(rows) and all(c is None for c in rows[header_idx]):
            header_idx += 1
        if header_idx >= len(rows):
            out.append("_(空表)_")
            out.append("")
            continue
        headers = [str(c if c is not None else "") for c in rows[header_idx]]
        out.append("| " + " | ".join(headers) + " |")
        out.append("|" + "---|" * len(headers))
        for row in rows[header_idx + 1:]:
            cells = [str(c if c is not None else "") for c in row]
            # 长度对齐
            while len(cells) < len(headers):
                cells.append("")
            cells = cells[: len(headers)]
            out.append("| " + " | ".join(cells) + " |")
        out.append("")
    wb.close()
    return "\n".join(out)


def _csv_to_markdown(data: bytes) -> str:
    # 试多种编码
    text = None
    for enc in ("utf-8", "gbk", "utf-16"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = data.decode("utf-8", errors="replace")
    reader = _csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return "_(空 CSV)_"
    headers = rows[0]
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for row in rows[1:]:
        while len(row) < len(headers):
            row.append("")
        out.append("| " + " | ".join(row[: len(headers)]) + " |")
    return "\n".join(out)


def _unzip(data: bytes, parent_name: str, depth: int, counter: List[int]) -> List[ProcessedFile]:
    if depth > ZIP_MAX_DEPTH:
        raise HTTPException(400, f"zip 嵌套超过 {ZIP_MAX_DEPTH} 层")
    results: List[ProcessedFile] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            total = sum(zi.file_size for zi in z.infolist())
            if total > ZIP_MAX_UNCOMPRESSED:
                raise HTTPException(413, f"zip 解压后 {total // 1024 // 1024}MB 超过 {ZIP_MAX_UNCOMPRESSED // 1024 // 1024}MB 限制")
            for zi in z.infolist():
                if zi.is_dir():
                    continue
                # 跳过隐藏 / __MACOSX
                if zi.filename.startswith("__MACOSX/") or os.path.basename(zi.filename).startswith("."):
                    continue
                counter[0] += 1
                if counter[0] > ZIP_MAX_FILES:
                    raise HTTPException(400, f"zip 内文件数超过 {ZIP_MAX_FILES}")
                with z.open(zi) as f:
                    inner = f.read()
                sub_name = f"{parent_name.rsplit('.zip', 1)[0]}__{zi.filename}"
                # 递归处理（可能是嵌套 zip）
                results.extend(_process(sub_name, inner, depth=depth + 1, counter=counter))
    except zipfile.BadZipFile:
        raise HTTPException(400, "zip 文件损坏或非 zip 格式")
    return results


def _process(filename: str, data: bytes, depth: int = 0, counter=None) -> List[ProcessedFile]:
    counter = counter if counter is not None else [0]
    ext = _ext(filename)

    if ext in PASSTHROUGH_EXTS:
        return [(filename, data, "application/octet-stream")]

    if ext == "xlsx" or ext == "xls":
        try:
            md = _xlsx_to_markdown(data)
        except Exception as e:
            logging.warning(f"xlsx 提取失败 {filename}: {e}")
            raise HTTPException(400, f"xlsx 解析失败: {e}")
        return [(filename + ".md", md.encode("utf-8"), "text/markdown")]

    if ext == "csv":
        try:
            md = _csv_to_markdown(data)
        except Exception as e:
            raise HTTPException(400, f"csv 解析失败: {e}")
        return [(filename + ".md", md.encode("utf-8"), "text/markdown")]

    if ext == "zip":
        return _unzip(data, filename, depth=depth, counter=counter)

    raise HTTPException(400, f"不支持的文件类型: .{ext}（支持 pdf/docx/txt/md/xlsx/csv/zip）")


def process_upload(filename: str, data: bytes) -> List[ProcessedFile]:
    """入口：返回要上传到 Letta 的文件列表（pdf/docx/md 原样透传；xlsx/csv 转 md；zip 展开）"""
    if not data:
        raise HTTPException(400, "文件为空")
    return _process(filename, data)
