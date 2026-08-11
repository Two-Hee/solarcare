/**
 * 태양광_DC전류_채널분석.html 안의 CORE 블록을 그대로 떼어내 Node에서 검증한다.
 * 브라우저를 띄우지 않고 파서 + 분석 엔진의 정확성만 확인하는 용도.
 *
 *   node tools/test_core.mjs
 */
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

const ROOT = path.resolve(import.meta.dirname, "..");
const HTML = fs.readFileSync(path.join(ROOT, "태양광_DC전류_채널분석.html"), "utf8");

const start = HTML.indexOf("var SolarCore = (function()");
const end = HTML.indexOf("CORE:END");
if (start < 0 || end < 0) throw new Error("CORE 블록을 찾을 수 없습니다.");
const code = HTML.slice(start, HTML.lastIndexOf("/*", end));

const sandbox = {
  DecompressionStream, Response, TextDecoder, DataView, Uint8Array, Float64Array, Int32Array,
  Date, Math, JSON, console, Promise, RegExp, Error, parseFloat, parseInt, isFinite, String, Number,
  Array, Object, module: { exports: {} }
};
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
const Core = sandbox.SolarCore;

let failures = 0;
function check(label, cond, detail) {
  const mark = cond ? "  ok  " : " FAIL ";
  if (!cond) failures++;
  console.log(`[${mark}] ${label}${detail ? "  — " + detail : ""}`);
}

// 샘플 생성기가 주입한 이상 채널의 정답표
const EXPECT = {
  CH03_DCAMP: { statuses: ["watch", "bad"], note: "부분 음영 → 형상 왜곡" },
  CH05_DCAMP: { statuses: ["watch"], note: "간헐 이상 → 일별 산포" },
  CH07_DCAMP: { statuses: ["bad"], note: "상시 -18%" },
  CH09_DCAMP: { statuses: ["dead"], note: "18일차부터 정지" },
  CH11_DCAMP: { statuses: ["bad"], note: "상시 -45%" }
};

async function runOn(label, sheets) {
  console.log(`\n=== ${label} ===`);
  const rows = sheets[0].rows;
  check("행을 읽었다", rows.length > 1000, `${rows.length}행`);

  const map = Core.detectColumns(rows);
  check("시각 열 자동 인식", map.timeCol === 0, `timeCol=${map.timeCol} (${map.names[map.timeCol]})`);
  check("채널 12개 자동 인식", map.channels.length === 12, `${map.channels.length}개: ${map.channels.map(c => map.names[c]).join(",")}`);

  const ds = Core.buildDataset(rows, map);
  check("데이터셋 행 수", ds.times.length === 8640, `${ds.times.length}행`);

  const first = new Date(ds.times[0]);
  check("첫 시각이 2026-06-01 00:00", Core.dayKeyOf(first) === "2026-06-01" && first.getHours() === 0,
        first.toString());

  const t0 = Date.now();
  const res = Core.analyze(ds);
  console.log(`   분석 소요: ${Date.now() - t0}ms`);

  check("30일로 집계", res.days.length === 30, `${res.days.length}일`);
  check("표본 간격 5분 추정", res.stepMs === 300000, `${res.stepMs}ms`);

  console.log("\n   채널        판정        성능비   산포    형상왜곡  상관도  이상일  이상시작");
  for (const c of res.channels) {
    console.log(
      "   " + c.name.padEnd(12) +
      c.status.padEnd(12) +
      (c.medRatio * 100).toFixed(1).padStart(6) + "%" +
      (c.spread * 100).toFixed(1).padStart(7) + "%" +
      (c.medShape * 100).toFixed(1).padStart(8) + "%" +
      (isFinite(c.medCorr) ? c.medCorr.toFixed(3) : "  —  ").padStart(8) +
      String(c.badDays.length).padStart(7) +
      "  " + (c.onsetDay || "—")
    );
  }

  console.log("");
  // 주입한 이상은 전부 잡아야 하고, 정상 채널은 정상으로 남아야 한다
  for (const c of res.channels) {
    const exp = EXPECT[c.name];
    if (exp) {
      check(`${c.name} 이상 검출 (${exp.note})`, exp.statuses.includes(c.status),
            `판정=${c.status}, 사유=${c.reasons.join(" / ")}`);
    } else {
      check(`${c.name} 정상 유지`, c.status === "normal", `판정=${c.status} · ${c.reasons.join(" / ")}`);
    }
  }

  const ch03 = res.channels.find(c => c.name === "CH03_DCAMP");
  const ch07 = res.channels.find(c => c.name === "CH07_DCAMP");
  check("음영 채널의 형상 왜곡이 상시 저출력 채널보다 크다",
        ch03.medShape > ch07.medShape * 2,
        `CH03=${(ch03.medShape * 100).toFixed(1)}% vs CH07=${(ch07.medShape * 100).toFixed(1)}%`);

  const ch11 = res.channels.find(c => c.name === "CH11_DCAMP");
  check("−45% 채널의 성능비가 55% 부근", Math.abs(ch11.medRatio - 0.55) < 0.04,
        `${(ch11.medRatio * 100).toFixed(1)}%`);

  const ch09 = res.channels.find(c => c.name === "CH09_DCAMP");
  check("고장 시작일을 2026-06-19로 특정", ch09.onsetDay === "2026-06-19", `onset=${ch09.onsetDay}`);

  const ch05 = res.channels.find(c => c.name === "CH05_DCAMP");
  check("간헐 이상 채널의 이상 일수 6일", ch05.badDays.length === 6, `${ch05.badDays.length}일: ${ch05.badDays.join(",")}`);
  check("간헐 이상 채널의 평균 성능비는 정상권", ch05.medRatio > 0.95,
        `${(ch05.medRatio * 100).toFixed(1)}% — 평균만 보면 놓치는 케이스`);

  check("판정 요약 합계가 채널 수와 일치",
        res.counts.normal + res.counts.watch + res.counts.bad + res.counts.dead === res.channels.length);

  return res;
}

// 1) xlsx 경로
const buf = fs.readFileSync(path.join(ROOT, "sample/DC전류_샘플데이터.xlsx"));
const ab = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
const sheetsX = await Core.readXlsx(ab);
check("시트 이름 유지", sheetsX[0].name === "DC전류", sheetsX[0].name);
const resX = await runOn("xlsx 파싱", sheetsX);

// 2) csv 경로 — 같은 데이터이므로 결과가 일치해야 한다
const csv = fs.readFileSync(path.join(ROOT, "sample/DC전류_샘플데이터.csv"), "utf8");
const resC = await runOn("csv 파싱", [{ name: "csv", rows: Core.parseCsv(csv) }]);

console.log("\n=== xlsx ↔ csv 교차 검증 ===");
let maxDiff = 0;
for (let i = 0; i < resX.channels.length; i++) {
  maxDiff = Math.max(maxDiff, Math.abs(resX.channels[i].medRatio - resC.channels[i].medRatio));
}
check("두 경로의 성능비가 동일", maxDiff < 1e-9, `최대 차이 ${maxDiff.toExponential(2)}`);

// 3) 경계 조건
console.log("\n=== 경계 조건 ===");
const oneCh = {
  times: Float64Array.from([0, 3600000, 7200000].map(t => new Date(2026, 5, 1, 10).getTime() + t)),
  channels: [{ name: "단일", values: Float64Array.from([5, 6, 5]) }]
};
let ok = true, msg = "";
try { const r = Core.analyze(oneCh); msg = `판정=${r.channels[0].status}`; } catch (e) { ok = false; msg = e.message; }
check("채널이 1개뿐이어도 분석이 끝난다", ok, msg);

const withGaps = {
  times: Float64Array.from([0, 1, 2, 3].map(i => new Date(2026, 5, 1, 8 + i).getTime())),
  channels: [
    { name: "A", values: Float64Array.from([3, NaN, 4, 3]) },
    { name: "B", values: Float64Array.from([3, 3, NaN, 3]) }
  ]
};
ok = true;
try { Core.analyze(withGaps); } catch (e) { ok = false; msg = e.message; }
check("결측값이 섞여도 분석이 끝난다", ok, msg);

console.log(`\n${failures === 0 ? "전체 통과" : failures + "건 실패"}`);
process.exit(failures === 0 ? 0 : 1);
