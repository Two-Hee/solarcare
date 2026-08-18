/**
 * 엑셀 내보내기 검증.
 * HTML 안의 CORE + XLSX 블록을 떼어내 Node에서 워크북을 만들고,
 * ZIP 구조 · 파트 관계 · XML 적합성을 확인한 뒤 파일로 떨군다.
 * (실제 엑셀 호환성은 tools/check_xlsx.py 에서 LibreOffice로 확인한다.)
 *
 *   node tools/test_xlsx.mjs [출력경로]
 */
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

const ROOT = path.resolve(import.meta.dirname, "..");
const HTML = fs.readFileSync(path.join(ROOT, "dc-channel.html"), "utf8");

function block(startMarker, endMarker) {
  const s = HTML.indexOf(startMarker);
  const e = HTML.indexOf(endMarker);
  if (s < 0 || e < 0) throw new Error(`블록을 찾을 수 없습니다: ${endMarker}`);
  return HTML.slice(s, HTML.lastIndexOf("/*", e));
}

const sandbox = {
  DecompressionStream, CompressionStream, Response, TextDecoder, TextEncoder,
  DataView, Uint8Array, Uint32Array, Float64Array, Int32Array, ArrayBuffer,
  Date, Math, JSON, console, Promise, RegExp, Error, parseFloat, parseInt,
  isFinite, isNaN, String, Number, Array, Object, module: { exports: {} }
};
vm.createContext(sandbox);
vm.runInContext(block("var SolarCore = (function()", "CORE:END"), sandbox);
vm.runInContext(block("var XlsxWriter = (function()", "XLSX:END"), sandbox);

const Core = sandbox.SolarCore;
const Workbook = sandbox.SolarWorkbook;

let failures = 0;
function check(label, cond, detail) {
  if (!cond) failures++;
  console.log(`[${cond ? "  ok  " : " FAIL "}] ${label}${detail ? "  — " + detail : ""}`);
}

// 샘플 데이터를 실제로 분석해서 워크북을 만든다
const buf = fs.readFileSync(path.join(ROOT, "sample/DC전류_샘플데이터.xlsx"));
const ab = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
const sheets = await Core.readXlsx(ab);
const map = Core.detectColumns(sheets[0].rows);
const ds = Core.buildDataset(sheets[0].rows, map);
const res = Core.analyze(ds);

const t0 = Date.now();
const bytes = await Workbook.build(res, ds, {
  fileName: "DC전류_샘플데이터.xlsx",
  generatedAt: new Date(2026, 7, 9, 14, 30),
  selDay: res.days[18],
  selChannel: 0
});
console.log(`워크북 생성: ${Date.now() - t0}ms · ${(bytes.length / 1024 / 1024).toFixed(2)} MB`);

const out = process.argv[2] || path.join(ROOT, "sample/분석결과_예시.xlsx");
fs.writeFileSync(out, bytes);
console.log(`저장: ${out}\n`);

check("파일이 ZIP 시그니처로 시작", bytes[0] === 0x50 && bytes[1] === 0x4b);

// 자체 리더로 되읽어 왕복 검증
const wbBack = await Core.readXlsx(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.length));
const names = wbBack.map(s => s.name);
console.log("시트:", names.join(" · "));
check("시트 7개", wbBack.length === 7, `${wbBack.length}개`);
check("시트 이름이 기대와 일치",
  names.join(",") === "요약,채널진단,일별발전량,일별성능비,일간프로파일,일별상세,원본데이터",
  names.join(","));

const diag = wbBack[1];
check("채널진단 시트 행 수 = 헤더 + 채널", diag.rows.length === res.channels.length + 1,
  `${diag.rows.length}행`);
check("채널진단 헤더", diag.rows[0][0] === "채널" && diag.rows[0][2] === "기간 성능비",
  diag.rows[0].slice(0, 4).join(" | "));

// 값이 실제로 살아 돌아오는지 (숫자는 숫자로, 날짜는 Date로)
const worst = diag.rows[1];
check("진단 첫 행이 최저 성능 채널", worst[0] === "CH11_DCAMP", String(worst[0]));
check("성능비가 숫자로 저장", typeof worst[2] === "number" && Math.abs(worst[2] - 0.574) < 0.01,
  String(worst[2]));

const daily = wbBack[2];
check("일별발전량 행 수 = 헤더 + 일수", daily.rows.length === res.days.length + 1, `${daily.rows.length}행`);
check("일별발전량 열 수 = 날짜 + 기준 + 채널", daily.rows[0].length === res.channels.length + 2,
  `${daily.rows[0].length}열`);

const raw = wbBack[6];
check("원본데이터 행 수 = 헤더 + 데이터", raw.rows.length === ds.times.length + 1, `${raw.rows.length}행`);
check("원본 시각이 Date로 복원", raw.rows[1][0] instanceof Date, String(raw.rows[1][0]));
check("원본 시각이 원래 값과 동일",
  raw.rows[1][0] instanceof Date && raw.rows[1][0].getTime() === ds.times[0],
  `${raw.rows[1][0]} vs ${new Date(ds.times[0])}`);

const detail = wbBack[5];
check("일별상세 행 수 = 채널 × 일수", detail.rows.length === res.channels.length * res.days.length + 1,
  `${detail.rows.length}행`);

const profile = wbBack[4];
const profileRows = profile.rows.length - 4;
check("일간프로파일에 하루치 표본", profileRows === 288, `${profileRows}행 (5분 간격 하루 = 288)`);

console.log(`\n${failures === 0 ? "전체 통과" : failures + "건 실패"}`);
process.exit(failures === 0 ? 0 : 1);
