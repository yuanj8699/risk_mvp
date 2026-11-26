"""通过下载接口解包当前修订，核对 Word 的真实段落、网格表与公文版心。"""
from io import BytesIO
from urllib.parse import unquote
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from sqlalchemy import select

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def test_download_current_risk_report_docx(api):
    from backend.models import AnalysisRun, Letter, Organization, RiskCase, SessionLocal, Snapshot

    with SessionLocal() as db:
        snapshot = db.scalars(select(Snapshot)).first()
        org = db.scalars(select(Organization).where(Organization.level == 3)).first()
        run = AnalysisRun(run_key="test-docx-export", snapshot_id=snapshot.id, status="completed")
        db.add(run)
        db.flush()
        case = RiskCase(run_id=run.id, org_id=org.id, org_name=org.name, region_name="测试",
                        data_date=snapshot.data_date, headline="导出附表")
        db.add(case)
        db.flush()
        letter = Letter(case_id=case.id, content="初稿", evidence_hash="test")
        db.add(letter)
        db.commit()
        letter_id, org_name = letter.id, org.name

    lines = [
        "经营与风险情况提示函", f"{org_name}：", "请关注下列指标。", "一、指标表现",
        "（一）拨备覆盖。拨备覆盖率为133.71%。", "特此函告。", "风险管理部", "2026年11月26日",
        "附表：实测情况", "| 监测指标 | 本期实测 | 全辖排名 | 数据日期 |", "| --- | --- | --- | --- |",
        "| 拨备覆盖率（A & B） | 133.71% | 第 77 / 82 名 | 2026-09-30 |",
    ]
    path = f"/api/letters/{letter_id}"
    # 遗留函件或人工粘贴可能包含 XML 1.0 禁止的控制字符。
    lines[2] = "请关注\x01下列指标。"
    edit = api.patch(path, json={"content": "\n".join(lines), "expected_revision": 1})
    assert edit.status_code == 200
    response = api.get(path + "/download.docx")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert unquote(response.headers["content-disposition"]) == f"attachment; filename*=UTF-8''{org_name}风险报告-v2.docx"

    with ZipFile(BytesIO(response.content)) as package:
        assert package.testzip() is None
        parts = {name: ET.fromstring(package.read(name)) for name in package.namelist()}
        main_path = parts["_rels/.rels"].find("{*}Relationship").attrib["Target"]
        assert parts["[Content_Types].xml"].find(f"{{*}}Override[@PartName='/{main_path}']") is not None
        doc = parts[main_path]
    body = doc.find(W + "body")
    paragraphs = body.findall(W + "p")
    assert ["".join(p.itertext()) for p in paragraphs] == [
        line.replace("\x01", "\ufffd") for line in lines[:9]
    ]
    assert paragraphs[0].find(f"{W}pPr/{W}jc").get(W + "val") == "center"
    assert paragraphs[0].find(f"{W}pPr/{W}pBdr/{W}bottom") is not None
    assert paragraphs[0].find(f"{W}r/{W}rPr/{W}sz").get(W + "val") == "44"
    assert paragraphs[1].find(f"{W}r/{W}rPr/{W}b") is not None
    assert paragraphs[1].find(f"{W}pPr/{W}ind") is None
    assert paragraphs[3].find(f"{W}r/{W}rPr/{W}rFonts").get(W + "eastAsia") == "黑体"
    p = paragraphs[4]
    assert p.find(f"{W}pPr/{W}ind").get(W + "firstLineChars") == "200"
    assert p.find(f"{W}pPr/{W}spacing").get(W + "line") == "560"
    heading, text = p.findall(W + "r")
    assert heading.find(W + "t").text == "（一）拨备覆盖。"
    assert heading.find(f"{W}rPr/{W}b") is not None and text.find(f"{W}rPr/{W}b") is None
    assert all(p.find(f"{W}pPr/{W}jc").get(W + "val") == "right" for p in paragraphs[6:8])
    margins = body.find(f"{W}sectPr/{W}pgMar")
    assert [margins.get(W + side) for side in ("top", "bottom", "left", "right")] == ["2098", "1984", "1587", "1474"]
    table = body.find(W + "tbl")
    header, row = table.findall(W + "tr")
    assert header.find(f"{W}trPr/{W}tblHeader") is not None
    assert all(r.find(f"{W}trPr/{W}cantSplit") is not None for r in (header, row))
    assert all(cell.find(f"{W}tcPr/{W}shd").get(W + "fill") == "F1F5F9" for cell in header.findall(W + "tc"))
    assert ["".join(cell.itertext()) for cell in row.findall(W + "tc")] == [
        "拨备覆盖率（A & B）", "133.71%", "第 77 / 82 名", "2026-09-30",
    ]
    assert api.get("/api/letters/missing/download.docx").status_code == 404
