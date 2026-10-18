# daytrader — 프로젝트 안내 (Claude 가 이 폴더에서 작업을 이어받을 때 먼저 읽는 문서)

한국 주식 **1~2종목** 단타 + 1박2일 자동매매 프로그램. 한국투자증권(KIS) Open API, **기본은 모의투자**. 사용자는 한국어로 대화한다(답변·주석·커밋 메시지 모두 한국어).

## 현재 상태 (정직하게)
- **수익성은 어떤 것도 검증되지 않았다.** 지금까지 나온 백테스트는 전부 합성 틱(`python -m daytrader synth`) 위의 결과라 근거가 아니다. 이 점을 흐리게 말하지 말 것.
- **KIS 연동은 실계정으로 검증되지 않았다**(REST 엔드포인트·TR_ID·웹소켓 필드 순서·재무 API·종목 상태 필드명). 문서 기준으로 작성했고 가짜 응답으로만 테스트했다. 사용자 PC에서 처음 할 일: `selftest` → `probe`(체결 방향 +1/-1 이 맞는지) → `fin 종목 --raw`.
- 사용자가 실제 1분봉/틱을 아직 넣지 않았다. 데이터 확보 방법 3가지: `bars-fetch`(KIS) / `bars-import`(HTS CSV) / `record` 후 `bars-build`.

## 명령어 (모두 이 폴더 = 저장소 루트에서 실행. data/ logs/ state/ params.json .env 가 현재 폴더 기준이다)
```
python -m pytest -q                       # 항상 전부 통과해야 함
python -m daytrader selftest 005930       # KIS 읽기 전용 점검(주문 없음)
python -m daytrader probe 005930 -n 40    # 웹소켓 원본 프레임/체결 방향 확인(장중)
python -m daytrader record 005930         # 틱·호가 기록(매매 없음)
python -m daytrader run 005930 --broker paper     # 실시간 가상체결(기본). kis-mock 은 KIS 모의계좌 주문
python -m daytrader backtest 005930
python -m daytrader analyze 005930 --last 20      # 1분봉 정밀 분석 리포트(reports/)
python -m daytrader calibrate 005930 --last 20    # 종목별 파라미터 제안(--write 는 사람이 검토 후)
python -m daytrader fin 005930 | news 005930 | regime 005930
python -m daytrader review | tune 005930 | apply <제안파일> --approve
```
README.md 에 전체 설명이 있다.

## 구조
- `daytrader/flow.py` 체결 흐름(10초/1분/5분) 점수·분봉·변동폭 / `squeeze.py` 압축→확장 돌파 / `bands.py` 이평 밭 / `levels.py` 수급 기반 동적 지지·저항
- `strategy.py` 진입·청산 판단(순수 함수) / `engine.py` 틱→판단→주문, 종목별 파라미터 / `risk.py` 일일 손실·이월 한도·킬스위치(STOP 파일)
- `analysis.py` + `minute_report.py` 1분봉 분석 / `calibrate.py` / `tune.py` / `guard.py` / `review.py`(Claude 복기, 제안만)
- `fin.py` 재무·종목 상태 게이트 / `news.py` 뉴스 게이트 / `regime.py` 일봉 국면
- `kis.py`(REST) `kis_ws.py`(웹소켓) `live.py` `replay.py` `recorder.py` `bars.py` `synth.py` `config.py`(Params, PARAM_BOUNDS, LOCKED)
- `dashboard/` 아티팩트용 대시보드 소스와 샘플 데이터 생성 스크립트(PC 에서 단독 실행되는 웹앱이 아님)
- 설정: `params.json`(공통, 지금은 `entry_mode: squeeze` 만 선택) + `params/종목코드.json`(종목별 덮어쓰기)

## 핵심 설계 결정과 이유
- **단일 진입 기준(기본)**: 압축 → 확장 돌파 = 직전 60분 대비 20분 박스가 압축(70% 이하)된 상태에서 거래량 2배 + 매수체결 우위로 박스 고점 돌파, 손절은 박스 중간(구조적), 손익비 ≥ 1. 가설일 뿐이고 `analyze` 10절의 구성요소 제거 비교(대조군 '박스 돌파만')와 퍼널로 검증해야 한다. 기존 수급 점수는 `entry_mode: flow`.
- 사용자의 목표는 **익절 +1.3%**, 1~2종목 집중, 당일 단타 + 1박2일. 비용(왕복 약 0.4~0.5%)이 그 3분의 1 안팎이라 신호 우위가 비용을 넘는지가 핵심이다.
- **AI/튜너는 제안만 한다**: `config.LOCKED`(자금·리스크·청산시각·수수료·게이트 on/off 등)는 `guard.py` 가 절대 못 바꾸게 막고, 나머지도 `PARAM_BOUNDS` 안에서 한 번에 25% 까지만, 적용은 사람의 `--approve`.
- 이월은 시간외(NXT) 갭을 손절 못 하므로 `overnight_risk / overnight_gap_pct`(기본 0.75%/4%≈18.75%)로 총 노출을 제한. 종목마다 `calibrate` 가 갭 가정을 제안하되 **리스크를 푸는 방향은 표본 60개 이상일 때만**.
- 백테스트는 **룩어헤드 금지**: 일봉은 전일까지, 재무는 분기 종료 +50일 이후 공시분만, N분봉은 완성된 봉으로만.
- 익절/손절은 무작위 진입 기준선으로 고르지 않는다(추세 없는 가격에서 조합 간 차이는 의미 없음).

## 작업 규칙
1. **앱키·시크릿·계좌번호를 읽거나 출력하거나 커밋하지 말 것.** `.env`, `.kis_token*.json` 은 건드리지 않는다. 예시는 `.env.example` 만.
2. **기본은 모의투자.** 실전 차단(`config.REAL_GUARD`)과 `kis-mock` 브로커 가드, `STOP` 킬스위치, 일일 손실 한도를 약화시키는 변경은 사용자가 명시적으로 요청하기 전에는 하지 않는다.
3. 동작을 바꾸면 **테스트를 같이 추가**하고 `python -m pytest -q` 전체를 통과시킨 뒤 커밋한다. 실패하면 테스트를 고치기 전에 코드/테스트 중 무엇이 틀렸는지 먼저 가린다.
4. 성과를 말할 때 합성 데이터 결과와 실데이터 결과를 구분하고, **표본이 30회 미만이면 해석하지 않는다**고 적는다. 다중 비교(여러 정의를 나란히 보고 가장 좋은 것 고르기)는 과최적화라고 경고한다.
5. 파라미터 기본값에 **특정 종목 하나에서 본 값**을 박지 않는다(예시 종목은 애경케미칼 161000 이었으나 예시일 뿐).
6. 커밋 메시지는 한국어, 무엇을/왜. 큰 변경은 README.md 도 같이 갱신한다.

## 알려진 한계 / 다음에 할 일 (우선순위 순)
1. PC 에서 `selftest` → `probe` 로 KIS 연동 검증(체결 방향, 웹소켓 URL/필드, 주문 TR_ID, 일별분봉·재무·종목 상태 필드). 어긋나면 `kis.py`/`kis_ws.py`/`fin.py` 수정.
2. 선정한 1~2종목의 실제 1분봉 확보 → `analyze`(퍼널·대조군) → `calibrate` → 기준 유지/수정 결정.
3. 모의투자(paper → kis-mock)로 최소 수 주 기록·운용. 장중 내내 켜져 있는 PC/서버 필요, 종료 15:31 자동.
4. 미구현: VI(변동성완화장치) 직접 감지, 보유 중 단기과열/투자경고 지정 시 강제 청산, NXT 장전·시간외 매매, 매도 체결가의 일별체결조회 보정(현재 호가 근사), 실시간 모니터링 대시보드(엔진→화면 연결).
5. `tune` 은 느리다(`use_levels` 를 켜면 약 2배). 튜닝은 끄고 한다.
