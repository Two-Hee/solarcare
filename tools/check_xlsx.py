#!/usr/bin/env python3
"""생성된 .xlsx가 실제 엑셀에서 열리는지 검증한다.

1) ZIP 무결성(CRC 포함)
2) 모든 XML 파트의 적합성
3) [Content_Types].xml 이 모든 파트를 선언하는지
4) 모든 관계(Relationship) 대상 파트가 실제로 존재하는지
5) 차트가 참조하는 시트가 워크북에 있는지
6) 독립 리더(openpyxl)로 실제 열어보기 — 서식·차트까지 확인

사용법:
    python3 tools/check_xlsx.py sample/분석결과_예시.xlsx
"""

import os
import posixpath
import sys
import zipfile
import xml.etree.ElementTree as ET

REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
CT_NS = "{http://schemas.openxmlformats.org/package/2006/content-types}"
CHART_NS = "{http://schemas.openxmlformats.org/drawingml/2006/chart}"
MAIN_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

failures = []


def check(label, cond, detail=""):
    print("[%s] %s%s" % ("  ok  " if cond else " FAIL ", label, ("  — " + detail) if detail else ""))
    if not cond:
        failures.append(label)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "sample/분석결과_예시.xlsx"
    print("검사 대상: %s (%.2f MB)\n" % (path, os.path.getsize(path) / 1024 / 1024))

    z = zipfile.ZipFile(path)

    # 1) ZIP 무결성
    bad = z.testzip()
    check("ZIP 무결성 (CRC)", bad is None, bad or "이상 없음")

    names = set(z.namelist())
    print("파트 %d개\n" % len(names))

    # 2) XML 적합성
    bad_xml = []
    for n in sorted(names):
        if not n.endswith(".xml") and not n.endswith(".rels"):
            continue
        try:
            ET.fromstring(z.read(n))
        except ET.ParseError as e:
            bad_xml.append("%s: %s" % (n, e))
    check("모든 XML 파트가 적합", not bad_xml, "; ".join(bad_xml) or "%d개 파싱 성공" % len(names))

    # 3) Content Types
    ct = ET.fromstring(z.read("[Content_Types].xml"))
    defaults = {d.get("Extension").lower() for d in ct.findall(CT_NS + "Default")}
    overrides = {o.get("PartName").lstrip("/") for o in ct.findall(CT_NS + "Override")}
    undeclared = []
    for n in names:
        if n == "[Content_Types].xml":
            continue
        if n in overrides:
            continue
        if n.rsplit(".", 1)[-1].lower() in defaults:
            continue
        undeclared.append(n)
    check("모든 파트가 Content_Types에 선언됨", not undeclared, ", ".join(undeclared) or "누락 없음")

    missing_override = [o for o in overrides if o not in names]
    check("Content_Types의 Override가 실재하는 파트를 가리킴", not missing_override,
          ", ".join(missing_override) or "모두 실재")

    # 4) 관계 대상 존재 확인
    dangling = []
    rel_count = 0
    for n in sorted(names):
        if not n.endswith(".rels"):
            continue
        base = posixpath.dirname(posixpath.dirname(n))  # _rels 의 부모
        for rel in ET.fromstring(z.read(n)).findall(REL_NS + "Relationship"):
            if rel.get("TargetMode") == "External":
                continue
            rel_count += 1
            target = posixpath.normpath(posixpath.join(base, rel.get("Target")))
            if target not in names:
                dangling.append("%s → %s" % (n, rel.get("Target")))
    check("모든 관계가 실재하는 파트를 가리킴", not dangling,
          ", ".join(dangling) or "관계 %d개 모두 유효" % rel_count)

    # 시트가 drawing을 참조하면 그 rels가 반드시 있어야 한다
    missing_rels = []
    for n in sorted(names):
        if not n.startswith("xl/worksheets/sheet"):
            continue
        root = ET.fromstring(z.read(n))
        if root.find(MAIN_NS + "drawing") is not None:
            rels = "xl/worksheets/_rels/%s.rels" % posixpath.basename(n)
            if rels not in names:
                missing_rels.append(n)
    check("drawing을 참조하는 시트에 rels가 있음", not missing_rels, ", ".join(missing_rels) or "정상")

    # 5) 차트가 참조하는 시트 이름이 워크북에 존재하는지
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    sheet_names = [s.get("name") for s in wb.iter(MAIN_NS + "sheet")]
    print("워크북 시트: %s" % " · ".join(sheet_names))

    charts = sorted(n for n in names if n.startswith("xl/charts/chart"))
    series_total = 0
    bad_ref = []
    for cn in charts:
        root = ET.fromstring(z.read(cn))
        sers = root.iter(CHART_NS + "ser")
        for ser in sers:
            series_total += 1
        for f in root.iter(CHART_NS + "f"):
            ref = f.text or ""
            if "!" not in ref:
                bad_ref.append("%s: %s" % (cn, ref))
                continue
            sheet = ref.split("!")[0].strip("'").replace("''", "'")
            if sheet not in sheet_names:
                bad_ref.append("%s: 알 수 없는 시트 %s" % (cn, sheet))
    check("차트 %d개 생성" % len(charts), len(charts) > 0, ", ".join(posixpath.basename(c) for c in charts))
    check("차트 참조가 모두 실재하는 시트를 가리킴", not bad_ref, "; ".join(bad_ref[:3]) or
          "계열 %d개, 참조 모두 유효" % series_total)

    # 6) 독립 리더(openpyxl)로 실제 열기 — 엑셀 호환성 확인
    #    (이 환경의 LibreOffice는 설치가 깨져 있어 일반 CSV조차 열지 못하므로 검증에 쓰지 않는다.)
    try:
        import openpyxl
    except ImportError:
        print("\n[ skip ] openpyxl 미설치 — `pip3 install openpyxl` 후 다시 실행하면 더 검증합니다.")
        print("\n%s" % ("전체 통과" if not failures else "%d건 실패" % len(failures)))
        return 1 if failures else 0

    print()
    wb = openpyxl.load_workbook(path)
    check("openpyxl이 워크북을 염", True, " · ".join(wb.sheetnames))
    check("시트 이름이 워크북 선언과 일치", wb.sheetnames == sheet_names)

    ws = wb["채널진단"]
    check("채널진단 틀 고정", ws.freeze_panes == "B2", str(ws.freeze_panes))
    check("채널진단 자동 필터", ws.auto_filter.ref is not None, str(ws.auto_filter.ref))
    check("성능비 칸에 백분율 서식", ws.cell(2, 3).number_format == "0.0%", ws.cell(2, 3).number_format)
    check("성능비 값이 숫자", isinstance(ws.cell(2, 3).value, float), repr(ws.cell(2, 3).value))
    check("판정 칸에 배경색", ws.cell(2, 2).fill.fgColor.rgb not in (None, "00000000"),
          str(ws.cell(2, 2).fill.fgColor.rgb))
    check("헤더가 굵게", ws.cell(1, 1).font.bold is True)

    hm = wb["일별성능비"]
    colors = {hm.cell(r, 5).fill.fgColor.rgb for r in range(5, 5 + len(sheet_names))}
    check("히트맵 칸마다 색이 다름", len(colors) > 1, ", ".join(sorted(str(c) for c in colors))[:90])

    raw = wb["원본데이터"]
    import datetime as _dt
    check("원본 시각이 날짜로 읽힘", isinstance(raw.cell(2, 1).value, _dt.datetime), repr(raw.cell(2, 1).value))
    check("원본 시각 서식", "yyyy" in raw.cell(2, 1).number_format, raw.cell(2, 1).number_format)

    total_charts = 0
    for name in wb.sheetnames:
        for ch in wb[name]._charts:
            total_charts += 1
            print("   차트: %-12s %-6s 계열 %d개" % (name, ch.tagname, len(ch.series)))
    check("openpyxl이 차트를 읽음", total_charts == len(charts), "%d개" % total_charts)

    print("\n%s" % ("전체 통과" if not failures else "%d건 실패: %s" % (len(failures), ", ".join(failures))))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
