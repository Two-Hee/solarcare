#!/usr/bin/env python3
"""태양광 발전소 DC 전류(채널/스트링) 샘플 데이터 생성기.

표준 라이브러리만 사용해 .xlsx 파일을 직접 작성한다(별도 패키지 설치 불필요).
채널분석 프로그램의 파서/분석 엔진을 검증하기 위한 용도이며,
정상 채널과 함께 아래 이상 유형을 의도적으로 주입한다.

  - 저출력      : 접속 불량/모듈 열화처럼 상시 일정 비율 낮게 발전
  - 심각 저출력 : 스트링 일부 탈락 수준의 큰 결손
  - 부분 음영   : 하루 중 특정 시간대만 급락(형상 이상 → 기준 대비 상관도 저하)
  - 중도 고장   : 특정 날짜부터 발전 정지
  - 간헐 이상   : 일부 날짜만 비정상

사용법:
    python3 tools/generate_sample_xlsx.py [출력경로]
"""

import datetime as dt
import math
import random
import struct
import sys
import zipfile

# ---------------------------------------------------------------- 데이터 생성

START_DATE = dt.date(2026, 6, 1)
DAYS = 30
INTERVAL_MIN = 5          # 계측 주기(분)
SUNRISE_H = 5.5
SUNSET_H = 19.5
PEAK_AMP = 9.2            # 맑은 날 정오 기준 스트링 전류(A)

# (채널명, 유형, 파라미터)
CHANNEL_SPEC = [
    ("CH01_DCAMP", "normal",     {}),
    ("CH02_DCAMP", "normal",     {}),
    ("CH03_DCAMP", "shading",    {"from_h": 14.5, "to_h": 17.0, "depth": 0.45}),
    ("CH04_DCAMP", "normal",     {}),
    ("CH05_DCAMP", "sporadic",   {"days": [4, 5, 12, 19, 20, 21], "ratio": 0.62}),
    ("CH06_DCAMP", "normal",     {}),
    ("CH07_DCAMP", "derated",    {"ratio": 0.82}),
    ("CH08_DCAMP", "normal",     {}),
    ("CH09_DCAMP", "outage",     {"from_day": 18}),
    ("CH10_DCAMP", "normal",     {}),
    ("CH11_DCAMP", "derated",    {"ratio": 0.55}),
    ("CH12_DCAMP", "normal",     {}),
]


def clear_sky(hour):
    """맑은 날의 시간대별 정규화 발전 곡선(0~1)."""
    if hour <= SUNRISE_H or hour >= SUNSET_H:
        return 0.0
    phase = (hour - SUNRISE_H) / (SUNSET_H - SUNRISE_H)
    return max(0.0, math.sin(math.pi * phase) ** 1.35)


def build_rows(rng):
    """[(datetime, [채널별 전류...]), ...] 생성."""
    # 날짜별 날씨 계수 및 구름 이벤트
    weather = {}
    for d in range(DAYS):
        r = rng.random()
        if r < 0.55:
            factor = rng.uniform(0.92, 1.0)      # 맑음
        elif r < 0.82:
            factor = rng.uniform(0.62, 0.88)     # 구름 많음
        else:
            factor = rng.uniform(0.25, 0.55)     # 흐림/비
        weather[d] = factor

    rows = []
    steps_per_day = 24 * 60 // INTERVAL_MIN
    for d in range(DAYS):
        day = START_DATE + dt.timedelta(days=d)
        day_factor = weather[d]
        # 구름 통과에 의한 저주파 변동 위상
        cloud_phase = rng.uniform(0, math.pi * 2)
        cloud_amp = (1.0 - day_factor) * 0.55

        for s in range(steps_per_day):
            minutes = s * INTERVAL_MIN
            ts = dt.datetime.combine(day, dt.time()) + dt.timedelta(minutes=minutes)
            hour = minutes / 60.0
            base = clear_sky(hour)
            if base <= 0:
                rows.append((ts, [0.0] * len(CHANNEL_SPEC)))
                continue

            cloud = 1.0 - cloud_amp * max(0.0, math.sin(hour * 1.7 + cloud_phase))
            site = PEAK_AMP * base * day_factor * cloud

            vals = []
            for name, kind, p in CHANNEL_SPEC:
                # 채널 고유 편차(설치 각도/모듈 편차) ±3%
                bias = 1.0 + (hash(name) % 61 - 30) / 1000.0
                v = site * bias * rng.uniform(0.985, 1.015)

                if kind == "derated":
                    v *= p["ratio"]
                elif kind == "outage" and d >= p["from_day"]:
                    v = 0.0
                elif kind == "sporadic" and d in p["days"]:
                    v *= p["ratio"]
                elif kind == "shading" and p["from_h"] <= hour <= p["to_h"]:
                    v *= p["depth"]

                vals.append(round(max(0.0, v), 3))
            rows.append((ts, vals))
    return rows


# ------------------------------------------------------------- xlsx 파일 작성

EPOCH = dt.datetime(1899, 12, 30)


def excel_serial(ts):
    return (ts - EPOCH).total_seconds() / 86400.0


def col_ref(idx):
    """0-based 열 인덱스 → A, B, ... AA 형식."""
    ref = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        ref = chr(65 + rem) + ref
    return ref


CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="DC전류" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""

WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

# numFmtId 164 = 사용자 지정 날짜/시간 형식 → 파서의 날짜 열 인식 검증용
STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy\\-mm\\-dd\\ hh:mm"/></numFmts>
<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="1"><fill><patternFill patternType="none"/></fill></fills>
<borders count="1"><border/></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
</cellXfs>
</styleSheet>"""


def esc(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_sheet_xml(rows, headers):
    """헤더는 공유 문자열(t="s"), 시각은 스타일 1의 직렬값, 전류는 일반 숫자."""
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']

    cells = "".join(
        '<c r="%s1" t="s"><v>%d</v></c>' % (col_ref(i), i) for i in range(len(headers))
    )
    out.append('<row r="1">%s</row>' % cells)

    for n, (ts, vals) in enumerate(rows, start=2):
        buf = ['<c r="A%d" s="1"><v>%.10f</v></c>' % (n, excel_serial(ts))]
        for i, v in enumerate(vals):
            buf.append('<c r="%s%d"><v>%s</v></c>' % (col_ref(i + 1), n, repr(v)))
        out.append('<row r="%d">%s</row>' % (n, "".join(buf)))

    out.append("</sheetData></worksheet>")
    return "".join(out)


def build_shared_strings(headers):
    items = "".join("<si><t>%s</t></si>" % esc(h) for h in headers)
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'count="%d" uniqueCount="%d">%s</sst>' % (len(headers), len(headers), items))


def write_xlsx(path, rows, headers):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", ROOT_RELS)
        z.writestr("xl/workbook.xml", WORKBOOK)
        z.writestr("xl/_rels/workbook.xml.rels", WORKBOOK_RELS)
        z.writestr("xl/styles.xml", STYLES)
        z.writestr("xl/sharedStrings.xml", build_shared_strings(headers))
        z.writestr("xl/worksheets/sheet1.xml", build_sheet_xml(rows, headers))


def write_csv(path, rows, headers):
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write(",".join(headers) + "\n")
        for ts, vals in rows:
            f.write(ts.strftime("%Y-%m-%d %H:%M") + "," + ",".join(str(v) for v in vals) + "\n")


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "sample/DC전류_샘플데이터.xlsx"
    rng = random.Random(20260601)
    rows = build_rows(rng)
    headers = ["측정시각"] + [name for name, _, _ in CHANNEL_SPEC]

    write_xlsx(out, rows, headers)
    print("생성 완료: %s  (%d행 × %d채널)" % (out, len(rows), len(CHANNEL_SPEC)))

    if out.endswith(".xlsx"):
        csv_path = out[:-5] + ".csv"
        write_csv(csv_path, rows, headers)
        print("생성 완료: %s" % csv_path)

    print("\n주입된 이상 채널:")
    for name, kind, p in CHANNEL_SPEC:
        if kind != "normal":
            print("  %-12s %-10s %s" % (name, kind, p))


if __name__ == "__main__":
    main()
