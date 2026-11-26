"""将当前函件正文和 Markdown 附表排成正式公文，返回可直接下载的 OOXML 字节流。"""
import io
import re
import zipfile
from xml.etree import ElementTree as ET


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MIME_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAIN_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
FONT_TABLE_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.fontTable+xml"
ET.register_namespace("w", W)

BODY_FONT = "仿宋_GB2312"
TITLE_FONT = "方正小标宋简体"
SECTION = re.compile(r"^(?:[一二三四五六七八九十百]+、|附表[：:])")
SUBHEADING = re.compile(r"^(（[一二三四五六七八九十百]+）[^。]+。)")
DATE = re.compile(r"\d{4}(?:年\d{1,2}月\d{1,2}日|-\d{2}-\d{2})")
INVALID_XML_TEXT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def _w(parent, tag, **attrs):
    return ET.SubElement(parent, f"{{{W}}}{tag}", {f"{{{W}}}{k}": str(v) for k, v in attrs.items()})


def _run(paragraph, text, font, size, bold=False):
    run = _w(paragraph, "r")
    props = _w(run, "rPr")
    _w(props, "rFonts", ascii=font, hAnsi=font, eastAsia=font)
    if bold:
        _w(props, "b")
    _w(props, "sz", val=size)
    node = _w(run, "t")
    node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    # 人工修订可以粘贴控制字符；XML 1.0 无法直接表示这些字符。
    node.text = INVALID_XML_TEXT.sub("\ufffd", text)


def _paragraph(body, text, kind):
    p = _w(body, "p")
    props = _w(p, "pPr")
    if kind in {"title", "recipient", "heading", "department"}:
        _w(props, "keepNext")
    if kind == "title":
        borders = _w(props, "pBdr")
        _w(borders, "bottom", val="single", sz=6, space=4, color="233E61")
    _w(props, "spacing", line=560, lineRule="exact", after=180 if kind == "title" else 0)
    if kind in {"body", "heading"}:
        _w(props, "ind", firstLineChars=200)
    if kind in {"department", "date"}:
        _w(props, "jc", val="right")
    elif kind == "title":
        _w(props, "jc", val="center")

    if kind == "body" and (heading := SUBHEADING.match(text)):
        _run(p, heading[0], BODY_FONT, 32, bold=True)
        text = text[heading.end():]
    font = {"title": TITLE_FONT, "heading": "黑体"}.get(kind, BODY_FONT)
    if text:
        _run(p, text, font, 44 if kind == "title" else 32, bold=kind in {"title", "recipient"})


def _cells(line):
    inner = line.removeprefix("|").removesuffix("|")
    return [part.strip().replace(r"\|", "|") for part in re.split(r"(?<!\\)\|", inner)]


def _table(body, rows):
    # 版心宽 156mm。正式四列表给指标名称留更多空间，自定义列数时均分。
    columns = len(rows[0])
    weights = [34, 23, 21, 22] if columns == 4 else [1] * columns
    widths = [8845 * w // sum(weights) for w in weights]
    widths[-1] += 8845 - sum(widths)
    table = _w(body, "tbl")
    props = _w(table, "tblPr")
    _w(props, "tblW", w=sum(widths), type="dxa")
    borders = _w(props, "tblBorders")
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        _w(borders, side, val="single", sz=4, color="808080")
    _w(props, "tblLayout", type="fixed")
    margins = _w(props, "tblCellMar")
    for side in ("top", "left", "bottom", "right"):
        _w(margins, side, w=75, type="dxa")
    grid = _w(table, "tblGrid")
    for width in widths:
        _w(grid, "gridCol", w=width)
    for index, row in enumerate(rows):
        tr = _w(table, "tr")
        tr_props = _w(tr, "trPr")
        _w(tr_props, "cantSplit")
        if index == 0:
            _w(tr_props, "tblHeader")
        for value, label, width in zip(row, rows[0], widths):
            cell = _w(tr, "tc")
            cell_props = _w(cell, "tcPr")
            _w(cell_props, "tcW", w=width, type="dxa")
            if index == 0:
                _w(cell_props, "shd", val="clear", fill="F1F5F9")
            p = _w(cell, "p")
            align = {"本期实测": "right", "全辖排名": "center", "数据日期": "center"}.get(label, "left")
            _w(_w(p, "pPr"), "jc", val="center" if index == 0 else align)
            _run(p, value, BODY_FONT, 21, bold=index == 0)


def _document(content, org_name):
    doc = ET.Element(f"{{{W}}}document")
    body = _w(doc, "body")
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    i = 0
    while i < len(lines):
        line = lines[i]
        if "|" in line and i + 1 < len(lines) and "|" in lines[i + 1]:
            header, divider = _cells(line), _cells(lines[i + 1])
            if len(header) == len(divider) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in divider):
                rows = [header]
                i += 2
                while i < len(lines) and "|" in lines[i] and len(cells := _cells(lines[i])) == len(header):
                    rows.append(cells)
                    i += 1
                _table(body, rows)
                continue
        if i == 0:
            kind = "title"
        elif i == 1 and (line.rstrip("：:") == org_name or line.endswith(("：", ":")) or line.startswith("致：")):
            kind = "recipient"
        elif SECTION.match(line):
            kind = "heading"
        elif line == "风险管理部":
            kind = "department"
        elif DATE.fullmatch(line):
            kind = "date"
        else:
            kind = "body"
        _paragraph(body, line, kind)
        i += 1
    section = _w(body, "sectPr")
    _w(section, "pgSz", w=11906, h=16838, orient="portrait")
    _w(section, "pgMar", top=2098, right=1474, bottom=1984, left=1587, header=0, footer=0, gutter=0)
    return doc


def _relationships(rel_type, target):
    root = ET.Element("Relationships", xmlns="http://schemas.openxmlformats.org/package/2006/relationships")
    ET.SubElement(root, "Relationship", Id="rId1", Type=f"{REL}/{rel_type}", Target=target)
    return root


def build_risk_report_docx(content: str, *, org_name: str = "") -> bytes:
    """按已保存版本排版，保持正文和附表中的业务数值原样。"""
    types = ET.Element("Types", xmlns="http://schemas.openxmlformats.org/package/2006/content-types")
    ET.SubElement(types, "Default", Extension="rels",
                  ContentType="application/vnd.openxmlformats-package.relationships+xml")
    ET.SubElement(types, "Override", PartName="/word/document.xml", ContentType=MAIN_TYPE)
    ET.SubElement(types, "Override", PartName="/word/fontTable.xml", ContentType=FONT_TABLE_TYPE)
    fonts = ET.Element(f"{{{W}}}fonts")
    for font, alternate in ((TITLE_FONT, "黑体"), (BODY_FONT, "仿宋")):
        _w(_w(fonts, "font", name=font), "altName", val=alternate)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for path, root in (
            ("[Content_Types].xml", types),
            ("_rels/.rels", _relationships("officeDocument", "word/document.xml")),
            ("word/document.xml", _document(content, org_name)),
            ("word/_rels/document.xml.rels", _relationships("fontTable", "fontTable.xml")),
            ("word/fontTable.xml", fonts),
        ):
            package.writestr(path, ET.tostring(root, encoding="utf-8", xml_declaration=True))
    return output.getvalue()
