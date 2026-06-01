# ai-agent: 자동주식매매 에이전트 시스템 설계 문서

본 문서는 `C:\ai-team` 프로젝트의 **단일 진실 공급원(SSOT)** 이다.
모든 에이전트, 모듈, 스크립트는 이 문서의 규칙을 어겨선 안 된다.
규칙 변경은 본 파일 수정 → 코드 반영 순서로 진행한다.

---

## 1. 시스템 개요

### 1.1 목적
한국 주식시장(KRX)을 대상으로 한 **다중 에이전트 협업형 자동매매 시스템**.
- KIS Open API(한국투자증권)를 통한 실주문 및 시세 수신
- 모의투자 ↔ 실전투자 두 가지 모드를 명확히 분리
- **신용거래를 적극 활용**한 유동적 포지션 사이징
- **데이트레이딩 단일 전략** (스캘핑 미사용)
- 학습부를 통한 지속적 전략 개선 (단, 실전 운영 중에는 파라미터 잠금)

### 1.2 핵심 원칙
1. **기술적 손절 우선**: 일일 손실 한도 같은 금액 기반 halt는 사용하지 않는다. 모든 손절은 **기술적 근거(차트 패턴 이탈)** 또는 **하드 손절선 -3%** 중 먼저 도달한 쪽으로 발동한다.
2. **모드 잠금**: 실전 모드 진입 시 전략 파라미터는 읽기 전용(read-only)이 된다.
3. **에이전트 간 메시지 기반 협업**: 직접 함수 호출이 아닌 메시지 큐/이벤트로 협업한다.
4. **장애 시 안전한 정지(Fail-Safe)**: 에이전트 1개라도 죽으면 신규 진입은 즉시 중단되고 보유 포지션은 보호 매도 룰을 따른다.
5. **모든 의사결정 기록**: 매수/매도/보류/거절/취소 모든 결정은 사유와 함께 학습부 복기 로그에 남는다.

### 1.3 비범위(Non-Goals)
- 해외주식, 파생상품, 암호화폐는 범위 외
- 옵션 거래 미지원 (신용거래는 사용)
- 스캘핑 / HFT 미지원

---

## 2. 조직 구조

```
                          ┌──────────────────┐
                          │   CEO 에이전트    │
                          │ (조율/모드 관리)  │
                          └────────┬─────────┘
              ┌──────────┬─────────┼─────────┬──────────┐
              ▼          ▼         ▼         ▼          ▼
        ┌──────────┐ ┌────────┐ ┌──────┐ ┌──────┐ ┌──────────┐
        │  정보부   │ │ 분석부 │ │리스크│ │실행부│ │  학습부   │
        ├──────────┤ ├────────┤ ├──────┤ ├──────┤ ├──────────┤
        │스크리닝   │ │신호분석│ │리스크│ │주문  │ │복기기록   │
        │시장상황   │ │        │ │관리  │ │실행  │ │패턴/백테 │
        │          │ │        │ │      │ │      │ │실전복기   │
        └──────────┘ └────────┘ └──────┘ └──────┘ └──────────┘
```

### 2.1 CEO 에이전트 (`agents/ceo/`)
- **역할**: 전체 에이전트 라이프사이클 관리, 모드 전환, 비상정지(Kill Switch)
- **입력**: 운영자 명령(`switch_mode`, `kill_switch`, `pause`), 각 부서 헬스체크, 연속손절 카운터
- **출력**: 부서별 시작/정지 명령, 모드 상태(`paper`/`live`) 브로드캐스트, 일일 일지 헤더
- **권한**: 유일하게 §3의 모드 잠금 파일을 수정할 수 있는 주체 (단 하드리밋 yaml은 CEO도 변경 불가)

### 2.2 정보부 (`agents/intel/`)

#### 2.2.1 스크리닝 에이전트 (`agents/intel/screening/`)
- **가동시간**: 장전 08:00 ~ 09:00 (1회 풀스캔 + 09:05 보정)
- **역할**: 당일 매매 후보 종목 선별 → **스크리닝 점수 70점 이상**만 후보로 등록
- **점수 구성(총 100점, 비중은 모의 모드에서 튜닝 가능)**:
  - 전일 거래대금 상위(코스피 500억↑, 코스닥 200억↑): 25점
  - 시가 갭(±1%~+5%): 20점
  - 이동평균 정배열 및 위치(데이트레이딩 친화 종목): 20점
  - 업종/테마 모멘텀(상위 5개 테마 가산): 15점
  - 변동성 ATR(20) 적정 범위: 20점
  - 공시·뉴스 페널티(악재/관리종목/거래정지 이력): -20점 (필터)
- **출력**: `data/screening/{YYYYMMDD}.json`

#### 2.2.2 시장상황 에이전트 (`agents/intel/market_watch/`)
- **가동시간**: 08:30 ~ 15:35 상시
- **역할**: 매크로 컨디션 감시 → 전체 진입 허용/제한 신호 발행
- **감시 대상**: KOSPI200 선물·베이시스, 코스피·코스닥 지수, 환율(USD/KRW), 미국 야간 선물, 프로그램 매매 잔량
- **경보 등급**:
  - `GREEN`: 정상 운영
  - `YELLOW`: 신규 진입 시 신호 4개 충족 강제 (조건부 진입 금지)
  - `RED`: 신규 진입 전면 금지, 기존 포지션 보호 매도 룰 가동
  - `BLACK`: 전 시스템 비상정지(CEO에게 Kill 요청)

### 2.3 분석부 — 신호분석 에이전트 (`agents/analysis/signal/`)
> **§25 개정**: 분봉 단독 평가에서 **일봉 추세 게이트 + 5분봉 돌파 타점 복합 분석**으로
> 교체되었다(큰 추세 무시·부정확한 타점 문제 해결). 일봉 게이트가 통과해야만 분봉 타점을
> 보며, 일봉 강세는 사이즈를 키운다. 상세는 §25.
- **가동시간**: 09:00 ~ 15:20 (분봉 갱신 주기)
- **역할**: 스크리닝 통과 종목에 대해 **일봉 게이트 + 5분봉 타점**(과거 5개 지표 평가) 종합 평가
- **5개 지표**:
  1. **거래량**: 직전 20분 평균 대비 2배 이상 + 양봉 동반
  2. **RSI(14, 1분봉)**: 30~70 사이 + 상승 전환(매수) / 하락 전환(매도)
  3. **MACD(12,26,9)**: 골든크로스(매수) / 데드크로스(매도) + 히스토그램 확장
  4. **이동평균**: 5MA > 20MA > 60MA 정배열(매수) / 역배열(매도)
  5. **캔들 패턴**: 망치형/장대양봉/상승장악형(매수) · 유성형/장대음봉/하락장악형(매도)
- **진입 강도 판정**:
  - 4개 이상 충족 → `STRONG_ENTRY` (강한 진입)
  - 3개 충족 → `CONDITIONAL_ENTRY` (조건부, YELLOW 시 금지)
  - 2개 이하 → `NO_ENTRY`
- **출력 메시지**: `{symbol, score, indicators, signal, entry_candle_low, timestamp, reason_text}`
  - `entry_candle_low`는 손절 기준 (§5.4에서 사용)

### 2.4 리스크부 — 리스크관리 에이전트 (`agents/risk/risk_manager/`)
- **가동시간**: 08:55 ~ 15:35 상시
- **역할**: §4 하드리밋 절대 감시 + 동적 사이징(신용거래 포함) + 연속손절 트래킹
- **검증 항목**(주문 전 게이트):
  - 동시 보유 종목 수 ≤ 3 (HL-01)
  - 연속 손절 카운터 ≥ 3 → 1시간 쿨다운 잠금 (HL-02)
  - 진입 시간대 화이트리스트: 09:30 ~ 14:29:59 (HL-03, HL-04)
  - 슬리피지 호가 5틱 이내 (HL-05)
  - 신용 가용 한도 및 담보비율 확인 (HL-06)
- **사이징**: 1종목 비중은 **고정 상한 없음**. 신호 강도/시장상황/신용 가용 한도/변동성(ATR)에 따라 동적으로 산출하며, 50%를 초과하는 집중 진입도 허용된다.
- **출력**: `APPROVE` / `REJECT(reason)` — 실행부는 `APPROVE` 없이 주문 송신 불가

### 2.5 실행부 — 주문실행 에이전트 (`agents/execution/order/`)
- **가동시간**: 09:00 ~ 15:30
- **역할**: KIS Open API 호출, 모드별 분기
  - `paper` 모드 → KIS 모의투자 도메인(`openapivts.koreainvestment.com:29443`)
  - `live` 모드 → KIS 실전 도메인(`openapi.koreainvestment.com:9443`)
- **주문 종류**:
  - 신규 진입: 분할 매수 또는 단일(변동성·신호강도 기반)
  - **신용거래 주문 지원**: 종목별 신용 가능 여부 확인 후 자동 분기(현금/신용)
  - 익절: §5.3 룰 (3단 구조)
  - 손절: §5.4 룰 (기술적/하드)
  - 보호 매도: 시장상황 RED 또는 CEO Kill 시 시장가 청산
- **체결 후처리**: 체결 즉시 학습부 `journal`에 raw 체결 푸시

### 2.6 학습부 (`agents/learning/`)

| 하위 에이전트 | 모드 | 역할 |
|---|---|---|
| 복기기록 (`journal/`) | 항상 가동 | 모든 신호·결정·체결·취소를 timestamp 기준 append-only 로그로 저장 |
| 패턴분석/백테스트 (`pattern/`) | **모의 모드 전용** | 누적 로그로 패턴 추출, 파라미터 그리드서치, 백테스트 → 제안서 생성 |
| 실전복기패키징 (`postmortem/`) | **실전 모드 전용** | 일일 단위로 실전 거래를 검토용 패키지(`postmortem_{date}.zip`)로 만들어 운영자에게 제출 |

- 패턴분석/백테스트는 실전 모드에서 **실행 자체가 차단**된다.
- 실전복기패키징은 통계·차트만 생성하며 **자동 파라미터 변경 권한 없음**.

### 2.7 메타부 — 최적화 에이전트 (`agents/meta/optimizer/`)
- **가동시간**: 상시(관찰) + 일일 마감 집계
- **역할**: 전 에이전트(자기 자신 포함)의 성과·토큰 사용을 관찰하고, **모의 모드에서만** 전략 진화/토큰 최적화 제안을 생성한다.
  1. **성과 관찰**: 승률 / 손익비(profit factor) / 손익비율(payoff) / 신호 정확도를 `journal`에서 집계.
  2. **학습/패턴 분석**: 진입 신호 종류·청산 사유·테마별로 수익/손실 조건을 분해.
  3. **전략 진화 제안**: 근거 기반 파라미터 조정 제안. **자동 적용 금지**, CEO 승인 후에만 반영.
  4. **토큰 최적화**: 각 에이전트의 Claude API 호출(`meta.claude_call` 트레이스)을 분석해 위반(매매 결정 위임 §15.4)·중복·과다 호출을 탐지하고 줄이는 권고 생성.
  5. **자기 관찰**: `meta.optimizer` 자신도 관찰 대상(`observe_runs` 등 자기 통계 기록).
- **작동 조건**:
  - **live**: 관찰/수집만 (`propose()`는 빈 리스트, 전략 수정 불가).
  - **paper**: 진화/최적화 제안 가능.
  - **적용**: 모든 제안은 `learning.proposal`로 발행되어 CEO 승인 큐에 적재되며, `CeoAgent.approve_proposal()`(운영자 승인) 후에만 `config/strategy_params.yaml`에 반영된다. 에이전트 자신은 설정 파일을 **직접 쓰지 않는다**.
- **불변식**: 하드리밋(`config/hard_limits.yaml`, §4)은 제안/변경 대상이 될 수 없으며, 수정 가능 키는 화이트리스트(`TUNABLE_KEYS`)로 제한된다. 적용 시 주석 보존 스칼라 교체만 수행한다.

---

## 3. 모드 시스템

### 3.1 모드 정의
- **paper (모의)**: **랜덤 과거 날짜 백테스트 리플레이**(§17). 2023년 이후 랜덤 거래일을 골라, 그날 09:00~15:30을 분 단위로 **그날인 것처럼** 재생하며 전 에이전트를 가상 시각으로 구동한다. 미래 데이터는 `ReplayKisClient`가 완전 차단(룩어헤드 바이어스 제거)한다. 주문은 `PaperBroker` 가상 체결, 하루가 끝나면 다음 랜덤 거래일로 자동 진행. 전략 파라미터 수정 가능. → 실시간 장 시간을 기다리지 않고 임의 과거 거래일로 전 파이프라인을 반복 검증한다. (이전의 "실시간 시세 수신 + 가상 주문" 방식은 §17로 대체되었다.)
- **live (실전)**: KIS 실전서버 사용, 실자금 + 신용, 실주문 송신, **모든 전략 파라미터 잠금**

### 3.2 전환 규칙
- 전환 주체: 운영자 → CEO 에이전트 (`scripts/switch_mode.py`)
- 전환 조건:
  - paper → live: 보유 포지션 0 + 미체결 주문 0 + 직전 모의 30영업일 백테스트 통과
  - live → paper: 보유 포지션 0 + 미체결 주문 0 (즉시 가능)
- 전환 시 자동 수행:
  - 모드 잠금 파일 갱신(`config/mode.yaml`)
  - 학습부 하위 에이전트 활성/비활성 토글
  - 전 에이전트 재시작 + 헬스체크

### 3.3 잠금 메커니즘
- `config/strategy_params.yaml`은 live 모드에서 OS 파일 권한 + 애플리케이션 락 이중 보호
- CEO 에이전트는 부팅 시 `mode.yaml.current_mode == "live"`면 `strategy_params.yaml`의 SHA-256 해시를 메모리에 고정. 운영 중 해시 불일치 감지 시 즉시 Kill Switch 발동.

---

## 4. 하드리밋 (절대 변경 불가)

> 이 절의 모든 수치는 `config/hard_limits.yaml`에 정의되며 **코드/문서/CI 어디서도 수정 PR이 자동 차단**된다.

| ID | 항목 | 값 | 비고 |
|---|---|---|---|
| HL-01 | 동시 보유 종목 수 | 최대 3종목 | 부분 청산도 1포지션으로 카운트 |
| HL-02 | 연속 손절 쿨다운 | 3연속 손절 시 1시간 신규 진입 금지 | 손절 시각 기준 누적 |
| HL-03 | 장후반 진입 금지 | **14:30 이후 신규 진입 금지** | 14:30:00.000부터 적용, 청산만 허용 |
| HL-04 | 장초반 진입 금지 | **09:00~09:30 신규 진입 금지** | 변동성 확인 윈도우 |
| HL-05 | 슬리피지 가드 | 호가 5틱 이상 슬리피지 예상 시 거절 | 리스크관리 사전검증 |
| HL-06 | 신용 사용 시 담보유지비율 | KIS 정책 기준선의 +5%p 버퍼 | 마진콜 회피용 |

### 4.1 명시적으로 두지 않는 한도
- **1종목 투입 비중 상한 없음**: 신호 강도, 신용 가용 한도, 시장상황 등급, 종목 변동성을 종합한 리스크부의 동적 사이징이 단일 결정 주체이며, 50% 이상의 집중 진입도 허용된다.
- **일일 손실 한도(halt) 없음**: 모든 손절은 §5.4의 **기술적 손절** 또는 **하드 손절 -3%** 로만 발동한다. 누적 손실 금액을 이유로 자동 halt 하지 않는다(단 연속 손절 3회는 HL-02로 쿨다운).

> 즉, 본 시스템의 손실 방어는 "금액 한도"가 아닌 "각 포지션의 기술적 트리거 + 연속실패 쿨다운"로 구성된다.

---

## 5. 매매 전략 상세 (데이트레이딩 단일)

> **스캘핑 모드는 제거되었다.** 본 시스템은 데이트레이딩(분봉~수 시간 단위) 단일 전략으로 운영한다.

### 5.1 후보 선정 (스크리닝)
- 스크리닝 점수 ≥ 70점만 분석부로 전달
- 점수 < 70점은 분석부에서 자동 거절(`reason: SCREENING_BELOW_THRESHOLD`)

### 5.2 진입 판정
| 지표 충족 수 | 결과 | 비고 |
|---|---|---|
| 4개 이상 | `STRONG_ENTRY` | 1차 진입 비중 100% 또는 분할(50/30/20) |
| 3개 | `CONDITIONAL_ENTRY` | 시장 GREEN일 때만 진입, 진입 비중 70% 캡 |
| 2개 이하 | `NO_ENTRY` | 진입 금지 |

진입 메시지에는 **진입 캔들 저점**을 함께 기록하며, 해당 값은 §5.4의 기술적 손절 기준이 된다.

### 5.3 익절 룰 (3단 구조, 최소 목표 상향 — §24.x 개정)
- **1차 +3~5%**: 보유 수량의 **40% 청산**
  - 진입 시 기대 변동성(ATR)에 따라 +3% / +4% / +5% 중 1단계 목표가를 분석부가 사전 지정
- **2차 +6~10%**: 잔여의 **40% 청산**
  - 마찬가지로 ATR에 따라 +6 ~ +10% 중 사전 지정(상단을 +8%→+10%로 확대)
- **3차 트레일링**: 잔여 보유분에 대해 **고점 대비 -2% 이탈 시 전량 청산**(기존 -1.5%에서 완화)
  - 트레일링은 분봉 종가 기준, 장 마감 15:20까지 유효 → **15:20 EOD 강제청산으로 전량 일괄 청산**(예외 없음, §5.7)

### 5.4 손절 룰 (기술적 우선, §24.x 개정)
- **기술적 손절 (최우선)**: **진입 캔들 저점 -0.5% 이탈 시 청산**(너무 빡빡하지 않게 완화)
  - 분봉 종가가 진입 캔들 저점의 -0.5% 아래로 돌파하면 무조건 시장가 청산
  - 진입 근거 붕괴(거래량 소멸, MACD 데드크로스, 5MA 이탈) 2개 이상 동시 발생 시도 청산
    - **신호붕괴 유예(`signal_breakdown_grace_minutes`, 기본 5분)**: 진입 직후에는 1분봉
      노이즈(거래량 자연 감소·5MA 일시 이탈)로 신호붕괴가 즉시 트리거되어 포지션이 1분
      만에 청산되는 문제가 있어, 진입 후 유예 시간 동안은 신호붕괴 청산을 보류한다(§5.6
      평균 보유 30분~3시간 보장). **하드 손절·기술적 손절(진입캔들 저점 이탈)·EOD는
      유예와 무관하게 항상 발동**하므로 실질 위험은 그대로 차단된다.
- **하드 손절**: **최대 -2%** (기존 -3%에서 축소, 기술적 트리거가 늦게 잡힐 때의 안전 하한)
- 두 조건 중 **먼저 도달한 쪽**으로 청산

### 5.5 타임스톱 (§24.x 개정)
- 진입 후 **30분** 경과 시 보유 포지션 재평가
  - **수익 +1% 미달 시 보유 수량 50% 청산**(기준 상향 — 30분 내 +1%도 못 내면 자금 회전)
- 타임스톱 트리거는 손절 카운터(HL-02)에는 산입하지 않는다(중립 청산).

### 5.6 운영 모드
- **데이트레이딩 단일**: 평균 보유 30분~3시간
- 종목별 변동성·시장상황 등급에 따라 진입 비중/익절 목표가만 자동 조정
- 스캘핑·HFT 관련 코드는 추가하지 않는다.

### 5.7 단일 집중 운영 & 실시간 청산 루프

> 본 절은 §5.1~5.6의 전략을 **실시간 운영 루프**로 묶는 규칙이다. §4 하드리밋을 바꾸지 않으며(단일 집중은 HL-01 범위 내 운영 선택), 금액 halt를 도입하지 않는다(§4.1).

- **무보유일 때만 신규 진입(단일 집중)**: 보유 포지션이 하나라도 있으면 신규 진입을 하지 않는다. 특정 종목에 고정되지 않고, **진입 시점마다** 실시간 시황·강세 섹터·후보 점수를 다시 평가해 그 시점의 **최강 1종목**만 진입한다.
  - "강세 섹터"는 당일 후보군의 테마별 점수 합으로 산출한 상위 테마를 말하며, 동점 시 스크리닝 점수가 높은 종목을 택한다.
  - 진입 자체는 기존 게이트를 그대로 통과한다: 분석부 5지표(§5.2) → 리스크부 사이징 + HardLimitGate(HL-01~06) → 실행부.
- **보유 중에는 실시간 포지션 매니저가 청산만 수행**한다. 청산 규칙은 §5.3(익절 3단)·§5.4(기술적/하드 손절)·§5.5(타임스톱)·트레일링·**EOD 강제청산(15:20, 어떤 조건에서도 예외 없이 보유분 전량 시장가 청산 → 다음 거래일 이월 금지)**를 그대로 따른다. EOD 강제청산은 백테스트·실거래에 동일하게 적용된다.
  - 보호 청산(손절·EOD 강제)은 "무조건 청산"(§5.4) 원칙에 따라 시장가로 즉시 송신하며 진입 게이트(HL-01/03/04/02)와 무관하게 항상 허용된다.
  - 손절류 청산은 연속손절 카운터(HL-02)에 산입, 익절류는 카운터 리셋, 타임스톱은 미산입(§5.5).
- **청산되어 무보유로 복귀하면** 다시 관찰 → 최강 1종목 선택 → 진입을 반복한다.

---

## 6. 에이전트 간 통신

### 6.1 메시지 버스
- 1차: 로컬 프로세스 → Python `asyncio` + `redis-streams` (개발기), Kafka(향후 확장)
- 토픽 명세:
  - `screening.candidates` (정보부 → 분석부)
  - `market.state` (정보부 → 전 에이전트)
  - `signal.entry`, `signal.exit` (분석부 → 리스크부)
  - `risk.decision.approved`, `risk.decision.rejected` (리스크부 → 실행부 / 학습부)
  - `order.event`, `order.failed` (실행부 → 학습부, CEO)
  - `ceo.command` (CEO → 전 에이전트)
  - `learning.proposal` (학습부·메타부 → CEO 승인 큐, 제안 적용은 **paper 전용**)
  - `meta.observation` (메타부 → 학습부, 성과·토큰 관찰 리포트)
  - `meta.claude_call` (전 에이전트 → 학습부·메타부, Claude API 호출 트레이스 §15.4)

### 6.2 메시지 표준 스키마(JSON)
```json
{
  "msg_id": "uuid",
  "topic": "string",
  "ts": "ISO-8601",
  "mode": "paper|live",
  "sender": "agent_name",
  "payload": { ... },
  "trace_id": "uuid"
}
```
모든 메시지는 `mode` 필드를 포함하며, 수신측은 자신의 모드와 불일치 시 즉시 거절·경보.

---

## 7. 데이터/상태 저장소

| 경로 | 용도 | 보관 |
|---|---|---|
| `data/screening/` | 일자별 스크리닝 결과 JSON | 영구 |
| `data/market/` | 분봉/일봉 캐시(Parquet) | 90일 롤링 |
| `data/journal/` | 복기기록 append-only JSONL | 영구(연단위 압축) |
| `data/backtest/` | 백테스트 산출물 | paper 모드에서만 갱신 |
| `data/postmortem/` | 실전 복기 패키지 | live 모드에서만 갱신 |
| `logs/` | 에이전트별 운영 로그 | 30일 롤링 |
| `state/` | 런타임 상태(포지션, 카운터, 신용 잔여한도) | 매일 백업 |

---

## 8. 폴더 구조

```
C:\ai-team\
├── CLAUDE.md                       # 본 설계 문서(SSOT)
├── README.md
├── .gitignore
├── pyproject.toml
│
├── config/
│   ├── hard_limits.yaml            # §4 하드리밋(변경 금지)
│   ├── strategy_params.yaml        # 전략 파라미터(paper에서만 수정)
│   ├── mode.yaml                   # 현재 모드 + 잠금 상태
│   ├── kis_api.yaml.example
│   └── logging.yaml
│
├── agents/
│   ├── ceo/
│   ├── intel/
│   │   ├── screening/
│   │   └── market_watch/
│   ├── analysis/
│   │   └── signal/
│   ├── risk/
│   │   └── risk_manager/
│   ├── execution/
│   │   ├── order/
│   │   ├── position_manager/       # 실시간 청산 (§5.7)
│   │   └── selector.py             # 무보유 시 최강 1종목 선별 (§5.7)
│   ├── learning/
│   │   ├── journal/                # 항상
│   │   ├── pattern/                # paper 전용
│   │   └── postmortem/             # live 전용
│   └── meta/
│       └── optimizer/              # 관찰=양 모드, 제안=paper 전용 (§2.7)
│
├── core/
│   ├── kis_client/                 # KIS Open API SDK (현금+신용 라우팅)
│   ├── indicators/
│   ├── messaging/
│   ├── mode_lock/
│   ├── schemas/
│   └── time_utils/
│
├── data/
├── state/                          # gitignore
├── logs/                           # gitignore
├── scripts/
│   ├── run_paper.py
│   ├── run_live.py
│   ├── switch_mode.py
│   ├── kill_switch.py
│   └── healthcheck.py
└── tests/
    ├── unit/
    ├── integration/
    └── strategy/
```

---

## 9. 실행 흐름 (1일 운영 사이클)

| 시각 | 동작 |
|---|---|
| 07:50 | CEO 부팅, 헬스체크, 모드 확인, 신용 가용 한도 갱신, 모드 잠금 해시 캡처 |
| 08:00 | 스크리닝 시작(전일 마감 데이터 + 야간 뉴스 반영) |
| 08:30 | 시장상황 에이전트 가동 |
| 09:00 | 분석부·리스크·실행부 가동 (단, **09:00~09:30 신규 진입 금지** HL-04) |
| 09:30 | 신규 진입 허용 윈도우 시작 |
| 09:30~14:29 | 정상 매매. 분봉 신호 → 리스크 게이트 → 주문(현금/신용 자동 분기) |
| 14:30 | **신규 진입 차단(HL-03)**, 청산/익절/손절/트레일링만 허용 |
| 15:20 | 트레일링 스톱 종료 + **EOD 강제청산: 보유분 전량 시장가 청산(어떤 조건에서도 예외 없음, 다음 거래일 이월 금지)** |
| 15:25 | (안전망) 잔여 보유분 재확인 청산 — 정상 시 15:20에 이미 무보유 |
| 15:30 | 정규장 종료, 실행부 종료 |
| 15:35 | 학습부 일일 마감 작업 |
|  | • paper: 패턴분석/백테스트 실행, 제안서 생성 |
|  | • live: 실전복기패키지 빌드 → 운영자 전송 |
| 16:00 | CEO 일일 보고서 생성, 다음 영업일 점검 큐 등록 |

---

## 10. 안전장치 / 비상정지

- **Kill Switch (`scripts/kill_switch.py`)**: 모든 신규 진입 즉시 차단 + 보유 포지션 시장가 청산 옵션
- **데드맨 스위치**: 각 에이전트는 5초 간격 heartbeat 전송, CEO가 15초 이상 미수신 시 해당 부서 격리 + 신규 진입 동결
- **모드 잠금 변조 감지**: `strategy_params.yaml` 해시 불일치 → 즉시 Kill (live 모드)
- **API 인증 실패 3회 연속**: 해당 모드 토큰 폐기 + 재인증 대기
- **체결-주문 불일치**: 미체결/오체결 감지 시 운영자 알림 + 신규 주문 중단
- **신용 마진콜 임박**: 담보유지비율이 HL-06 버퍼 하단 진입 시 신용 포지션 우선 부분 청산

---

## 11. 학습부 운영 원칙

- **복기기록**은 모드와 무관하게 항상 수집(읽기 전용 데이터). 데이터 손실 시 매매 즉시 중단.
- **패턴분석/백테스트**는 paper 모드에서만 동작하며, 산출물은 `learning.proposal` 메시지로 CEO에게 제안. CEO는 운영자 승인 없이 파라미터를 변경하지 못함(자동 적용 금지).
- **실전복기패키징**은 live 모드에서만 동작. 통계·차트·로그를 묶어 `data/postmortem/postmortem_{YYYYMMDD}.zip` 산출. 자동 학습/파라미터 변경 권한 일체 없음.

---

## 12. 기술 스택

- Python 3.11+
- 비동기: `asyncio`, `httpx`
- 데이터: `pandas`, `numpy`, `pyarrow`
- 지표: 자체 구현 우선(`core/indicators/`), 보조로 `ta-lib`/`pandas-ta`
- 메시지 버스: `redis>=7` (Streams)
- 설정 검증: `pydantic v2`
- 시각화(복기): `matplotlib`, `plotly`
- 테스트: `pytest`, `pytest-asyncio`, `hypothesis`
- 린트/포맷: `ruff`, `black`, `mypy --strict` (코어/리스크부 한정)

---

## 13. 개발 가이드라인 (Claude Code 작업 시)

1. **하드리밋 수정 PR 금지**: `config/hard_limits.yaml` 변경 시 본 CLAUDE.md §4도 함께 갱신해야 하며, 두 파일이 동시에 수정되지 않은 PR은 CI에서 거절한다.
2. **금액 기반 halt 추가 금지**: 일일/누적 손실 금액 한도를 다시 도입하려면 본 §4를 먼저 개정해야 한다. 코드 레벨에서 임의 도입 금지.
3. **스캘핑 코드 도입 금지**: 본 시스템은 데이트레이딩 단일 전략이며, 스캘핑 모드/파라미터를 추가하려면 본 §5를 먼저 개정해야 한다.
4. **실전 코드 경로에 `print` 금지**: 모든 로깅은 `logging.yaml`을 통한 구조화 로그.
5. **메시지 스키마 변경**: `core/schemas/` 갱신 → 모든 에이전트 단위테스트 통과 후 머지.
6. **신규 지표 추가**: 분석부의 5개 지표 비중을 흔드는 변경은 paper 모드에서 30영업일 백테스트 통과 + 운영자 승인 필수.
7. **테스트 우선**: 리스크부와 모드잠금 모듈은 변경 시 반드시 회귀 테스트를 함께 추가.
8. **시간 처리**: 모든 시각은 KST(`Asia/Seoul`) + 영업일 캘린더를 사용. naive datetime 사용 금지.
9. **신용거래 처리**: 주문 송신 직전 신용 가용 한도와 종목별 신용 가능 여부를 반드시 재확인.

---

## 14. 향후 로드맵 (참고)

- 멀티 계좌 매니저(법인/개인 분리)
- LLM 보조 분석(뉴스/공시 요약, 단 매매 결정권 없음)
- 실시간 대시보드(Grafana)
- 신용 외 ELW/CFD 검토(범위 외 → 별도 의사결정 필요)

---

## 15. 외부 의존성: traidair 프록시

ai-agent는 KIS Open API를 **직접 호출하지 않는다**. 모든 KIS·DART·매크로 호출은 별도 레포 `traidair`의 HTTP 프록시(`server.js`)를 거친다.

### 15.1 위치 및 역할
- 레포: <https://github.com/123idon/traidair>
- 역할: KIS Open API / DART / Yahoo Finance(매크로) 단일 진입점. **KIS App Key/Secret은 traidair에서 보관**하며, ai-agent는 매 호출 시 keypair와 `mode`를 body로 동봉한다.
- 기본 호스트: 운영자 환경에 따라 가변. `config/kis_api.yaml.traidair_base_url`로 주입.
- 토큰 캐시: traidair가 29분 메모리 캐시 운영. ai-agent는 토큰을 알지 못한다.

### 15.2 호출 규칙
1. **`ok` 필드로 판정**: traidair는 실패도 HTTP 200으로 응답(`{ok:false, error}`)하므로 상태코드만 보지 말 것.
2. **mode 처리**: ai-agent는 시세/지표/토큰 호출 시 traidair에 `mode="real"`을 보낸다(실전 키로 실데이터 수신). **paper 모드에서는 주문/잔고/매수가능액을 traidair로 보내지 않고 `PaperBroker`가 로컬 가상 처리**한다(`KisClientConfig.simulate_orders=True`). 즉 paper에서도 실주문은 절대 발생하지 않으며, 시세만 실전 호스트로 받는다. live 모드에서만 실제 주문이 traidair→KIS로 송신된다.
3. **타임아웃·재시도**: traidair는 KIS 호출에 8초 데드라인. ai-agent는 6초 데드라인 + 지수 백오프 1회 재시도(200ms → 800ms) 후 실패 처리.
4. **토큰 재발급**: `ok:false`로 인증 류 오류 감지 시 즉시 `/api/kis/token`을 한 번 호출해 재발급 트리거 후 원 요청 1회 재시도.

### 15.3 현재 노출된 라우트 (사용 가능)

| 라우트 | ai-agent 사용처 | 비고 |
|---|---|---|
| `POST /api/kis/token` | CEO 부팅 헬스체크 | 응답 토큰은 앞 10자만 노출 |
| `POST /api/kis/chart` | 분석부 5지표 계산 | body: `{code, date:"YYYY-MM-DD", tf:"1\|3\|5\|15\|60"}` → `{candles:[{t,o,h,l,c,v}], prevCount, todayCount}` |
| `POST /api/kis/orderbook` | 분석부 체결강도, 리스크부 슬리피지 5틱(HL-05) 검증 | 10단계 호가 + `strength`(체결강도) |
| `POST /api/kis/price` | 분석부 현재가 보정, 학습부 스냅샷 | `{price, open, high, low, volume, change, changePct, name}` |
| `POST /api/kis/order` | 실행부 **현금 주문만** | body: `{account, side:"buy\|sell", code, qty, price, orderType:"limit\|market"}` → `{ordNo, msg}` |
| `POST /api/kis/balance` | CEO 시작자산, 리스크부 동시보유 검증(HL-01), 학습부 스냅샷 | `{cash, totalEval, totalPnl, positions[]}` |
| `GET /api/market-data?mode=realtime` | 시장상황 매크로 등급 산출 | KOSPI / KOSDAQ / NASDAQ / S&P / DOW / VIX / USD-KRW / N225 / **KOSPI200** 9종. 캐시 3분 |
| `GET /api/market-data?mode=sim&date=&time=&tf=` | 학습부 패턴분석(paper 전용) 과거 시뮬레이션 | traidair가 시뮬 시각 이후 데이터 cutoff 자동 적용 |
| `GET /api/dart/list?days=1` | 스크리닝 페널티(악재 -20), 학습부 컨텍스트 | `corp_code` 옵션 |
| `GET /api/dart/corpcode?nm=종목명` | DART 코드 매핑 | 현재 30종목 하드코딩 (확장 필요) |

### 15.4 사용 금지 라우트 (의사결정 위임 금지)
- `POST /api/claude` — **매매 결정을 LLM에 위임 금지**. 통계·요약 보조에만 사용 가능하며, 사용 시 학습부 journal에 호출 trace 필수 기록.
- `GET /api/notion-features`, `/api/notion-lecture` — traidair UI용. ai-agent는 호출하지 않는다.
- `GET/POST /api/user-data` — traidair `/tmp` 영속화에 의존 금지(휘발성). ai-agent state는 `state/`에 자체 보관.
- `/api/save-config`, `/api/get-config`, `/api/set-claude-key` — traidair 운영 전용.

### 15.5 보강 필요 라우트 (별도 PR로 traidair에 추가)

CLAUDE.md §1.1·§2.5의 신용거래, §10의 미체결 모니터링, §2.2.1의 풀스캔 스크리닝 요구를 충족하려면 traidair에 다음 6개 라우트 추가가 필요하다.
**해당 라우트가 모두 합치되기 전까지 실전 모드(live) 진입은 금지**한다 (§3.2 전환 조건에 자동 추가).

| 신규 라우트 | KIS 원본 path | TR ID (실전/모의) | 사용 에이전트 | 목적 |
|---|---|---|---|---|
| `POST /api/kis/order-credit` | `/uapi/domestic-stock/v1/trading/order-cash` (신용 분기) | `TTTC0852U`/`TTTC0851U` (모의 미지원) | 실행부 | 신용 매수/매도 |
| `POST /api/kis/order-cancel` | `/uapi/domestic-stock/v1/trading/order-rvsecncl` | `TTTC0803U`/`VTTC0803U` | 실행부 | 정정/취소 |
| `GET /api/kis/unfilled` | `/uapi/.../inquire-psbl-rvsecncl` | `TTTC8036R`/`VTTC8036R` | 리스크부 | 체결-주문 불일치 감지 |
| `GET /api/kis/inquire-psbl-order` | `/uapi/.../inquire-psbl-order` | `TTTC8908R`/`VTTC8908R` | 리스크부 | 매수가능액·신용 가용액 (HL-06 담보비율) |
| `GET /api/kis/volume-rank` | `/uapi/.../volume-rank` | `FHPST01710000` | 스크리닝 | 거래대금 상위 풀스캔 |
| `GET /api/kis/investor` | `/uapi/.../inquire-investor` | `FHKST01010900` | 시장상황 | 외인/기관 수급, 프로그램 매매 |

### 15.6 ai-agent 측 구현체
- `core/kis_client/KisClient` — httpx 기반 비동기 클라이언트. traidair 12개 라우트 메서드 + 토큰 재발급 자동화 + 신용 LOAN_DT 자동 추적(`CreditLedger`) 포함.
- 설정 로더: `KisClientConfig.from_files(project_root=...)` — `config/mode.yaml`과 `config/kis_api.yaml`을 함께 읽는다.
- `mode='sim'`의 `/api/market-data`는 paper 모드 전용으로 강제(§11).
- 모든 비-`ok` 응답은 `KisBusinessError`/`KisAuthError`로 분리되며, `route` 필드를 통해 학습부 journal에서 토픽별 카운트가 가능하다.

### 15.7 보안 주의
- traidair `server.js:55-56`에 KIS App Key/Secret 하드코딩 fallback이 존재. ai-agent는 환경변수 경로만 신뢰하며 traidair fallback에 의존하지 않는다. 운영 전 키 회전 권장.
- HTTP 200 통일 응답은 미들박스가 오류를 캐시할 위험이 있다. ai-agent는 모든 `ok:false`를 5분 슬라이딩 윈도우로 카운트하여 임계치(분당 5회) 초과 시 CEO에 경보 + 신규 진입 동결.

---

## 16. 구현 매핑

| 영역 | 모듈 | 주요 객체 |
|---|---|---|
| CEO | `agents/ceo/main.py` | `CeoAgent` (token 발급/시작 잔고/모드 잠금 SHA-256 캡처/`state/KILL_SWITCH` 센티넬 폴링) |
| 정보부 스크리닝 | `agents/intel/screening/{main.py, scorer.py}` | `ScreeningAgent`, `ScoringWeights`, `total_score()` |
| 정보부 시장상황 | `agents/intel/market_watch/main.py` | `MarketWatchAgent`, `MarketGrade` (GREEN/YELLOW/RED/BLACK) |
| 분석부 | `agents/analysis/signal/{indicators.py, main.py}` | `SignalAnalyzer`, `SignalAgent`, `EntrySignal`, `Direction`, `Signal` |
| 리스크부 | `agents/risk/risk_manager/{hard_limits.py, main.py}` | `HardLimitGate`(HL-01~HL-06), `RiskAgent`(+`market_state_provider`로 RED/BLACK 차단·YELLOW에서 CONDITIONAL 차단), `StopLossTracker` |
| 실행부 주문 | `agents/execution/order/main.py` | `OrderAgent` (현금/신용 자동 분기, mode는 KisClient 자체 관리) |
| 실행부 포지션 | `agents/execution/position_manager/{exit_rules.py, main.py}` | `PositionManagerAgent`(보유 중 §5.3~5.5+트레일링+EOD 실시간 청산, 게이트 우회 시장가), `evaluate_exit()`, `ExitParams`, `LivePositionState` |
| 실행부 선별 | `agents/execution/selector.py` | `EntrySelector`(무보유 게이트 `is_flat()` + 강세 섹터 랭킹 `pick()` → §5.7 단일 집중) |
| 학습부 | `agents/learning/{journal, pattern, postmortem}/main.py` | `JournalAgent`(append-only JSONL, 항상), `PatternAnalysisAgent`(**paper-only**), `PostmortemPackager`(**live-only**) |
| 메타부 | `agents/meta/optimizer/main.py` | `OptimizerAgent`(성과·토큰 관찰=양 모드, 진화/토큰 제안=**paper-only**, 자기 관찰 포함), `OptimizationProposal`, `apply_proposal_to_file`/`set_yaml_scalar`(주석 보존·화이트리스트), CEO `approve_proposal`/`reject_proposal` 승인 후 적용 |
| KIS 클라이언트 | `core/kis_client/` | `KisClient` (httpx, 12개 라우트), `KisClientConfig.from_files()`(paper→`simulate_orders`, 실전키 폴백), `CreditLedger`(LOAN_DT 자동 추적), `PaperBroker`(모의 가상 체결/잔고, §3.1), Pydantic 모델 |
| 지표 라이브러리 | `core/indicators/` | `sma`, `ema`, `rsi`, `macd`, `volume_spike_ratio`, `atr`/`atr_pct`(변동성, §5.3 익절 동적화), 6개 캔들 패턴 |
| 메시지 버스 | `core/messaging/Bus` | 토픽 기반 async pub/sub, 구독자 격리, 테스트용 `collector()` |
| 시간/영업일 | `core/time_utils/` | `KST`, `SimClock`(가상 시계, §17), `random_business_day`/`prev_business_day`, `session_minutes` |
| 백테스트 리플레이 | `core/backtest/` | `ReplayKisClient`(룩어헤드 컷오프 + PaperBroker + 로컬 캔들 유니버스), `BacktestRunner`(랜덤일 선택·분 단위 구동·손익 집계), `BacktestDashboard`(실시간 현황 JSON), `DayResult` (§17) |
| 분봉 수집/저장 | `core/marketdata/` | `YahooClient`(분봉 fetch), `CandleStore`(`data/candles/{date}.parquet` 날짜별·무삭제·스킵), `universe`(KOSPI200/KOSDAQ150/지수). `scripts/{collect_candles,build_universe}.py` (§18) |
| 실시간 모니터(traidair) | `backtest-live.html` / `agent-monitor.html` | HTS 탭 🎬 백테스트 / 🧠 에이전트, `GET /api/backtest/state` 폴링 (§17.6) |
| 스크립트 | `scripts/{run_paper, run_backtest, run_live, switch_mode, kill_switch, healthcheck}.py` | 운영자 엔트리포인트 (`run_paper`→`run_backtest` 위임, §17) |

### 16.1 흐름
```
ScreeningAgent.screen_once()         [거래대금 랭킹 + 차트 점수]
       └─ publish "screening.candidates"
              ↓
SignalAgent.analyze_symbol()         [5지표 평가]
       └─ publish "signal.entry"
              ↓
RiskAgent.review()                   [market_state 게이팅 + 사이징 + HardLimitGate]
       ├─ publish "risk.decision.approved"
       └─ publish "risk.decision.rejected"
              ↓
OrderAgent.execute()                 [KIS 현금/신용 라우팅]
       ├─ publish "order.event" → PositionManagerAgent가 포지션 등록
       └─ publish "order.failed"

[진입 게이트] ScreeningAgent.screen_once() → EntrySelector.pick()  [무보유일 때만 최강 1종목]
       └─ SignalAgent.analyze_symbol(best) → … → OrderAgent (매수)

PositionManagerAgent.monitor_once()  [매 20초, 보유 중에만]
       ├─ evaluate_exit() (§5.3~5.5 + 트레일링 + EOD)
       ├─ 청산 시 OrderAgent.execute(SELL, 게이트 우회) → publish "order.event"
       └─ publish "signal.exit" + StopLossTracker 갱신(HL-02 연동)

MarketWatchAgent.poll_once()         [매 60초]
       └─ publish "market.state" → RiskAgent의 market_state_provider 갱신
                                  → BLACK이면 CEO.kill() 호출

JournalAgent                         [모든 토픽 구독 → data/journal/{YYYYMMDD}.jsonl append-only]
```

### 16.2 메시지 envelope (`core/schemas/`)
- 인-프로세스 Bus는 dataclass payload를 직접 전달하지만, 영속화/외부 출력 시점에 표준 envelope으로 wrap.
- `core.schemas.wrap(topic, payload, sender, mode)` → `{msg_id, topic, ts, mode, sender, payload, trace_id}` dict.
- `Topic` enum: 10개 표준 토픽 이름.
- `JournalAgent`가 envelope 포맷으로 JSONL 저장 → redis-streams 이전 시 그대로 swap-in.

### 16.3 신용 LOAN_DT 권위 동기화
- traidair `/api/kis/balance` 응답에 `loanDt`, `crdtType` 필드 추가.
- `Position.loanDt` 필드, `CreditLedger.sync_from_balance(positions)`로 KIS 값으로 권위적 덮어쓰기.
- `KisClient.sync_credit_ledger()` 헬퍼.
- `OrderAgent.execute`가 신용 매수 성공 직후 자동 호출 (실패는 비치명, 다음 회차에서 재시도).

### 16.4 백테스트 엔진 (`agents/learning/pattern/backtest.py`)
- `BacktestEngine(analyzer, params)` — walk-forward 분봉 시뮬레이션
- `BacktestParams`: 워밍업 60, position 30%, TP1 +4%/50%, TP2 +7%/30%, 트레일링 -1.5%, 하드 -3%, 타임스톱 30봉, 진입 캔들 저점 기술 손절
- `BacktestResult`: trades, 총 PnL, 승률, MDD, Sharpe
- `PatternAnalysisAgent.backtest(candles)`로 외부 노출 (paper 전용).

### 16.5 테마/공시 통합 (`agents/intel/screening/`)
- `theme.py` — `ThemeDetector` (종목명 키워드 기반, 7개 디폴트 테마: 2차전지/AI/반도체/바이오/조선/원전/방산).
- `scorer.dart_penalty(reports)` — 관리종목류 -100, 일반 악재 -20.
- `ScreeningAgent`: traidair `/api/dart/corpcode`로 종목명→corp_code 캐시 조회 → `/api/dart/list` → 페널티 적용. DART 실패는 비치명.

### 16.7 단일 집중 + 실시간 청산 루프 (§5.7)
- `agents/execution/selector.py` — `EntrySelector`. `is_flat(balance)`(보유 0일 때만 진입), `pick(candidates)`(후보 테마별 점수 합으로 강세 섹터 산출 → 강세테마 소속·점수순 최강 1종목).
- `agents/execution/position_manager/exit_rules.py` — `ExitParams.from_file()`(strategy_params.yaml의 take_profit/stop_loss/time_stop **읽기 전용**), `LivePositionState`, `evaluate_exit(state, price, now, breakdown_count)` → `ExitAction(kind, ratio, reason)`. 우선순위: **EOD 강제청산(15:20, 무조건 전량·최우선)** → 하드 -3% → 기술적(진입캔들 저점 이탈) → 트레일링(고점 -1.5%, TP1 후) → TP1 → TP2 → 타임스톱(30분, ±0.5%) → HOLD. EOD는 우회 플래그가 없으며 백테스트·실거래 동일 적용(이월 금지).
  - **익절 목표가 ATR 동적화(§5.3)**: 분석부가 진입 시 `atr_pct`(ATR/현재가)를 `EntrySignal`에 실어 보내고, `select_tp_targets(atr_pct, params)`가 `pct_range` 안에서 밴딩 선택 → 저변동성 +3%/+6%, 중 +4%/+7%, 고 +5%/+8%. `PositionManagerAgent`가 등록 시 `LivePositionState.tp1_target/tp2_target`에 사전 지정. ATR% 미상이면 범위 하단 폴백. ATR은 `core/indicators/volatility.py`(`atr`/`atr_pct`).
- `agents/execution/position_manager/main.py` — `PositionManagerAgent`. `order.event`(매수) 구독 → `LivePositionState` 등록. `monitor_once()`: 잔고 권위 확인 → 1분봉 재평가(신호붕괴 카운트) → `evaluate_exit` → 시장가 매도(게이트 우회) + `signal.exit` 발행 + `StopLossTracker` 갱신. `run_forever(stop_event)` 20초 폴링.
- `scripts/run_paper.py`: 공유 `StopLossTracker`를 HardLimitGate·PositionManager에 주입, 진입은 `EntrySelector` 게이트(무보유 시 최강 1종목)로만, `position_loop` 백그라운드 추가. `run_live.py`는 `_run` 공유로 자동 반영.

### 16.9 메타부 최적화 (§2.7)
- `agents/meta/optimizer/main.py` — `OptimizerAgent`.
  - `observe(date)`(양 모드): journal에서 `PerformanceReport`(승률/profit factor/payoff/신호 정확도) + `ConditionReport`(진입신호·청산사유·테마별 분해) + `TokenUsageReport`(에이전트별 Claude 호출·위반/중복/과다 탐지) 산출 → `meta.observation` 발행. `KNOWN_AGENTS`에 자기 자신(`meta.optimizer`) 포함.
  - `propose(report)`(**paper 전용**, live는 `[]`): 근거 기반 `OptimizationProposal` 생성 → `learning.proposal` 발행. 전략 제안은 `TUNABLE_KEYS` 화이트리스트 스칼라만, 토큰 제안은 권고(recommendations)만.
  - `apply_proposal_to_file`/`set_yaml_scalar`: 주석 보존 스칼라 교체. 하드리밋 파일·화이트리스트 외 키 거부. **CEO 승인 시에만 호출**.
- `agents/ceo/main.py` — `CeoAgent`가 `learning.proposal` 구독 → `pending_proposals` 큐. `approve_proposal(id)`(paper + 잠금 정상에서만 적용, live 거부) / `reject_proposal(id)`. 자동 적용 없음(§11).
- `scripts/run_paper.py`: `OptimizerAgent` 인스턴스 + `optimizer_loop`(기본 600초) 추가. `run_live.py`는 `_run` 공유로 관찰 전용 자동 반영.

### 16.10 테스트
- `pytest tests/unit/` (httpx.MockTransport로 traidair 응답 모킹)
- 검증 범위: KIS client / 하드리밋 6개 / 지표 5개 / 5지표 분류 / Bus / 핵심 에이전트 / 시장상황 4단계 / 스크리닝 점수+테마+DART / 학습부 journal·pattern·postmortem·backtest / CEO 부팅·Kill·잠금 해시·**제안 승인** / market_state 통합 / envelope / CreditLedger sync / `switch_mode` CLI / 청산 규칙·셀렉터·포지션 매니저 / **메타부 성과·토큰·제안 모드게이트·자기관찰·YAML 적용기(optimizer)** / **시간유틸·SimClock·영업일 / 리플레이 룩어헤드 컷오프·합성시세·KIS 유니버스 위임 / 백테스트 러너 1일구동·날짜선택·손익집계 / 대시보드 에이전트 thoughts·누적성과 / CandleStore 저장·스킵·Yahoo 파싱·로컬 분봉 백테스트 통합 (§17/§18)**

---

## 17. 랜덤 과거 날짜 백테스트 리플레이 (paper 모드)

> paper(모의) 모드는 **랜덤한 과거 거래일을 그날인 것처럼 재생**하는 백테스트로 운영한다(§3.1 개정). 실시간 장 시간을 기다리지 않고, 2023년 이후 임의 거래일로 전 파이프라인을 반복 검증한다. 하드리밋(§4)·전략(§5)은 변경하지 않는다 — 동일 게이트/규칙을 가상 시각 위에서 그대로 구동한다.

### 17.1 동작 원리
1. **랜덤 거래일 선택**: `[BACKTEST_START(기본 2023-01-01), BACKTEST_END(기본 어제)]`에서 균등 랜덤 영업일을 고른다. 휴장/데이터 없음은 실데이터(분봉 존재 여부)로 재확인해 재추첨한다(휴장일 하드코딩에 의존하지 않음).
2. **가상 시각 전진**: `SimClock`이 09:00→15:30을 분 단위로 전진한다. 전 에이전트는 `clock: Callable[[], datetime]`로 `SimClock.now`를 주입받아 동일한 가상 시각을 본다.
3. **룩어헤드 차단**: `ReplayKisClient`가 KisClient와 동일 인터페이스(duck-typed)를 제공하되, 모든 시세를 **가상 시각 이전(≤)으로만** 잘라서 반환한다. 미래 분봉은 절대 노출되지 않는다.
4. **무보유→진입 / 보유→청산**(§5.7 그대로): 무보유 시 스크리닝→셀렉터→신호→리스크→실행으로 최강 1종목 진입, 보유 시 포지션매니저가 청산만 수행. 주문은 `PaperBroker` 가상 체결.
5. **일 마감 → 다음 랜덤일**: 15:20 EOD 강제청산으로 보유분을 전량 비운 뒤(이월 금지, 안전망으로 15:25 재확인) 손익(`DayResult`)을 집계하고, 다음 랜덤 거래일로 자동 진행한다. carry_over는 현금·누적손익만 이어가며 보유 종목은 절대 이월되지 않는다.

### 17.2 데이터 소스 (전부 무료, traidair 경유 — §15 준수)
> 유료 KRX OpenAPI는 제거되었다. 분봉/유니버스는 **로컬 Yahoo 캔들 캐시(§18)**, 매크로 지수는 Yahoo(traidair sim), 공시는 DART(traidair)로 충당한다.

| 데이터 | 소스 | 룩어헤드 방지 |
|---|---|---|
| 당일 분봉(5지표·청산) | **로컬 캔들 캐시 `data/candles/{date}.parquet`** (§18, Yahoo 수집) | 가상 시각 이전(≤) 분봉 + 직전 데이터일 분봉만 노출 |
| 현재가/호가 | 분봉에서 합성 | 실시간 시세 미사용(컷오프 분봉으로 산출) |
| 종목 데이터(스크리닝 유니버스) | **로컬 캔들**: 직전 영업일 거래대금(Σc·v) 상위 | 장전 전일 데이터 기반 → 룩어헤드 없음 |
| 코스피/코스닥 지수 | ① 로컬 지수 분봉(^KS11/^KQ11) 있으면 그것, ② 없으면(키움 수집=6자리 종목만) **유니버스 구성종목 바스켓 평균 등락률을 KOSPI 프록시**로 컷오프 산출(§21·§25.x) | 둘 다 가상 시각 이전(≤) 분봉만 사용 → 룩어헤드 없음 |
| 매크로(VIX/USDKRW/미증시) | 백테스트에서는 **미사용**(로컬 부재). VIX/USDKRW=None → 등급 판정에서 자동 제외 | — |
| 공시(DART) | DART(traidair `dart/list`·`dart/corpcode`) | traidair DART는 '최근 N일' 라우트라 과거 일자 재현 불가 → 백테스트에서는 빈 목록(페널티 0). 과거 일자 공시는 traidair에 날짜 파라미터 추가 시 활성화(§15.5) |

> **traidair `market-data?mode=sim` 미사용 결정(§21·§17 보강)**: traidair sim 라우트는 `date` 파라미터를 무시하고 **오늘 실시간 매크로**를 돌려준다(과거 일자 재현 불가 = 룩어헤드). 따라서 백테스트(`ReplayKisClient`)는 **로컬 저장소가 있으면 traidair sim 을 호출하지 않는다**(완전 로컬, 핫루프 79회/일 → 0). 로컬 저장소가 아예 없는 환경에서만 sim 폴백(라이브성). **이 매크로 산출 변경은 시장 등급(GREEN/YELLOW/RED/BLACK)→진입 게이트(§2.2.2)에 영향을 주므로 §6·§13 대상 — paper 30영업일 백테스트 검증 완료(`data/backtest/fix2_local_macro_validation.md`), 운영자 승인 후 live 신뢰.**

> 백테스트는 **로컬에 수집된 날짜만** 사용한다(`CandleStore.available_dates()` 범위에서 추첨). 로컬 분봉이 없으면 traidair KIS 분봉으로 폴백하나, KIS는 과거 일중 분봉 제공 깊이가 얕아 과거 일자는 비어 있을 수 있다 → §18 수집이 사실상의 데이터 소스다.

### 17.4 실행
- `python scripts/run_backtest.py` (또는 `scripts/run_paper.py` — paper 모드는 백테스트로 위임).
- 환경변수: `BACKTEST_START`/`BACKTEST_END`(YYYY-MM-DD), `BACKTEST_DAYS`(거래일 수, 미지정 시 무제한), `BACKTEST_SEED`(재현용 시드).
- **전제**: traidair가 가동 중이어야 하며, 선택된 과거 거래일의 분봉을 제공할 수 있어야 한다(KIS 분봉 과거 제공 깊이에 따라 선택 가능 구간이 제한될 수 있음 — 제공 불가 일자는 자동 재추첨된다).

### 17.5 구현 모듈
- `core/time_utils/` — `KST`, `SimClock`(가변 가상 시계), 영업일 캘린더(`random_business_day`/`prev_business_day`/`business_days`), `session_minutes`.
- `core/backtest/` — `ReplayKisClient`(룩어헤드 컷오프 + PaperBroker 주문/잔고 + KIS 유니버스/지수/공시 위임), `BacktestRunner`(랜덤일 선택 + 분 단위 구동 + 손익 집계, 에이전트를 콜백 주입으로 구동해 core가 agents에 비의존).
- `scripts/run_backtest.py` — 에이전트 와이어링(모두 `clock=SimClock.now` 주입) + 러너 구동. `run_paper.py`는 `main()`을 위임(`_run`은 live 전용으로 유지).
- 외부 데이터 클라이언트는 추가하지 않는다 — 지수(Yahoo)·종목(KIS)·공시(DART) 모두 기존 `KisClient`(traidair) 라우트를 재사용한다.

### 17.6 실시간 모니터링 (traidair HTS 탭)
백테스트 진행 상황과 에이전트 사고과정을 traidair HTS에 두 탭으로 노출한다.
- ai-agent: `BacktestDashboard`가 Bus 토픽을 O(1)로 수집하고, 백그라운드 태스크가
  `state/backtest_live.json`을 250ms마다 원자적 기록(핫 루프 무영향).
- traidair: `GET /api/backtest/state`가 그 파일을 서빙(mtime 신선도로 running 보정).
  - **🎬 백테스트** 탭(`backtest-live.html`): 날짜/시각·스크리닝·보유·매매·누적성과·시장.
  - **🧠 에이전트** 탭(`agent-monitor.html`, 토스 스타일·쉬운 한국어·이모지·3초): CEO/스크리닝/
    시장/신호(5지표 개별 통과여부)/리스크(차단 사유)/주문/학습/메타 8개 카드.
- 신호분석은 진입/미진입 모두 `signal.analysis` 토픽으로 5지표 상세를 발행한다.
- **HTS 캔들 차트 간격 일정성(`trading-hts.html`)**: 분봉 간격은 캔들 1개의 가로 픽셀
  (`chartCandlePx`) 단일 값으로만 제어된다. 보이는 봉 수는 `차트폭 / chartCandlePx`로
  역산하므로 백테스트로 봉이 누적돼도 **간격이 갑자기 넓어지지/좁아지지 않는다**(이전엔
  `차트폭 / 봉수`라 봉이 늘수록 간격이 변했다). 차트 **우측 상단의 −/⊙/+ 버튼**(또는 휠·
  핀치)으로 간격을 조절하며, ⊙은 기본 간격으로 리셋한다.

### 17.7 traidair에서 직접 제어 + 결과 리포트
- **제어 버튼**(🎬 백테스트 탭): server.js가 `POST /api/backtest/{start,stop,reset}`로 `run_backtest.py` 자식 프로세스를 spawn/kill한다. 시작 시 `BACKTEST_START/END/CASH/DAYS`를 env로 주입(가상잔고 기본 100만, 직접 입력·리셋 가능), 날짜 범위 선택 가능. `reset`은 실행 중지 + `state/backtest_live.json` 삭제.
- **완료 리포트**(`state.report`): 총수익률·승률·손익비·**MDD**(일별 자산곡선 고점대비 최대하락), **날짜별 손익 차트**(`dailyPnl`), **최고/최악 패턴**(메모리 §19), **메타 개선 제안**(`learning.proposal`). running=false·days>0일 때 토스 스타일 카드로 표시.

---

## 18. Yahoo 분봉 로컬 수집 (`core/marketdata/`, `scripts/collect_candles.py`)

> 백테스트(§17)가 사용할 **과거 분봉을 로컬에 적재**한다. 라이브 매매 경로(§15)와 무관한 배치 ETL이다.

- **저장**: `data/candles/{YYYYMMDD}.parquet` — 하루치 전 종목 분봉 1파일. **날짜별 저장 / 무삭제 / 이미 있는 날짜 스킵**.
- **대상**: `config/universe.json` (KOSPI200 + KOSDAQ150 종목 + 지수 ^KS11/^KQ11/^IXIC). `scripts/build_universe.py`가 pykrx로 전체 구성종목을 채울 수 있으며(미설치 시 시드 유지), Yahoo 티커는 `{code}.KS`/`.KQ`.
- **수집**: `scripts/collect_candles.py` — 최초 60일 백필 + `--incremental`(최근 2일, 당일 누적). Yahoo 1분봉은 최근 ~30일만, 요청당 ≤7일 제공 → 더 과거 구간은 자연 스킵.
- **스케줄**: Windows 작업 스케줄러 `ai-team-candle-collect`가 **매일 15:40** `collect_candles.py --incremental` 실행(`schtasks`로 등록).
- **백테스트 연동**: `CandleStore`가 분봉/유니버스 소스. `ReplayKisClient(candle_store=…)`가 로컬에서 분봉(컷오프)·전일 거래대금 랭킹을 구성하고, 러너는 `available_dates()` 범위에서만 거래일을 추첨한다.

### 18.1 키움 REST API 수집 경로 (`core/marketdata/kiwoom.py`, `scripts/collect_candles_kiwoom.py`)
> Yahoo는 1분봉을 최근 ~30일만 제공해 과거 깊이가 얕다. **키움 REST API**를 **데이터 수집 전용**(라이브 매매 §15와 완전 분리)으로 추가해 더 풍부한 과거 분봉을 적재한다. 기존 KIS/Yahoo 경로는 그대로 유지된다.

- **클라이언트**: `KiwoomClient`(`api.kiwoom.com`) — `POST /oauth2/token`(appkey/secretkey) 토큰 발급 + `POST /api/dostk/chart`(헤더 `api-id: ka10080` 주식분봉차트) 과거 방향 페이지네이션(`cont-yn`/`next-key`). 가격 부호 접두 정규화, 정규장(09:00~15:30) 분봉만 보관. 출력은 `CandleRow`(Yahoo와 동일 스키마) → `CandleStore`에 그대로 적재.
- **수집**: `scripts/collect_candles_kiwoom.py` — 코스피200+코스닥150 전종목(`config/universe.json`, 키움은 6자리 코드 사용)을 종목 단위 격리(실패 종목만 스킵)로 수집. **날짜별 저장 / 무삭제 / 기존 날짜 스킵**(`--overwrite`로 강제). 옵션 `--interval`(1m~60m)·`--max-pages`(과거 깊이)·`--throttle`.
- **키 우선순위**: `config/kis_api.yaml`의 `kiwoom_app_key`/`kiwoom_app_secret`/`kiwoom_base_url` → 루트 `{계좌}_appkey.txt`/`{계좌}_secretkey.txt` → env `KIWOOM_APP_KEY`/`KIWOOM_APP_SECRET`.
- **대량/장시간·재개 가능**: `--start`(기본 2023-01-01)~`--end`(기본 오늘) 경계까지 종목별로 과거 페이지네이션. **진행률(%)+ETA** 표시, 페이지/종목 throttle + 429/5xx 지수 백오프(레이트리밋 안전), 완료 종목을 `data/candles/_kiwoom_progress.json`에 체크포인트 → **중간에 끊겨도(Ctrl+C 포함) 남은 종목만 이어서** 수집(부분 배치는 `CandleStore.merge_day`로 날짜 파일에 병합 누적, `(symbol,t)` 중복은 키움 우선). `interval`/`start` 변경 시 체크포인트 자동 초기화. `--reset`로 처음부터.
- **데이터 깊이 한계(중요)**: 키움 ka10080 1분봉은 **최근 ~1년**만 제공한다(약 103페이지에서 `cont-yn=N`). 즉 본 수집기는 시작일을 2023-01-01로 줘도 실제로는 **약 1년치(예: 2025-06~2026-06)** 만 적재된다. 2023~2024 구간 1분봉은 키움/Yahoo(최근 ~30일) 등 무료 소스로는 확보 불가 — 더 깊은 과거가 필요하면 별도(유료) 분봉 소스가 필요하다. 백테스트는 `[BACKTEST_START, END]`를 **로컬 보유 날짜로 자동 클램프**하므로 수집된 ~1년 범위에서 랜덤 재생한다(설정은 2023+로 두되 데이터가 권위).

### 18.2 백테스트(paper) 성능·안정성 보강
> "3일째 자동 중지" 버그와 저속 문제를 해결한다(판단 로직 불변, 결과 동일성 검증).

- **자동 중지 금지**(`scripts/run_backtest.py`): 어떤 최상위 예외도 프로그램을 종료시키지 않는다 — 정확한 원인+스택을 매 회차 로그에 남기고 **무한 자동 재시작**(`MAX_RESTARTS` 기본 0=무제한). 실행 중 에러는 러너가 그 날짜/콜백만 스킵하고 계속 진행. 데이터 없는 날짜는 다음 랜덤 날짜로 자동 이동(풀 기반 추첨). 무제한 모드에서 데이터가 전혀 없으면 종료 대신 재시도. **유일한 종료 경로는 Ctrl+C 또는 `kill_switch.py`**(`state/KILL_SWITCH` 센티넬을 stop-watcher가 감지).
- **속도 최적화(≈55배, 891s→16s/3일, 동일 결과)**: ① `CandleStore.daily_aggregate` 영구 일봉 캐시 — `get_daily_chart`가 매 분·매 후보마다 parquet 17개를 재파싱하던 병목 제거. ② `ReplayKisClient` 분(cutoff) 단위 `get_chart` truncate 캐시 — 같은 분의 중복 호출(가격/호가/잔고/신호) 재필터링 제거. ③ 전일 거래대금 랭킹 세션당 1회 캐시. ④ 핫 루프 로그(NO_ENTRY 등) DEBUG 강등 + 파일 로그 버퍼링(`MemoryHandler`).

---

## 19. 에이전트 장기 메모리 (`core/memory/`)

> 학습부 저널을 집계해 **종목·패턴·시장등급별 승률 통계**를 만들고, 에이전트가 세션마다 읽어 판단에 반영한다. **메타 에이전트가 매 거래일 마감에 `MemoryStore.rebuild()`로 총괄 갱신**한다. 저장 위치 `data/memory/`.

- **집계(`MemoryStore.rebuild`)**: 최근 N일 저널을 시간순으로 훑어, 단일 집중(§5.7) 가정하에 `order.event(buy)` 직전 `signal.analysis`로 진입 패턴(신호강도+통과지표 조합)을, 직전 `market.state`로 시장 등급을 묶고, 뒤따르는 `signal.exit` 손익으로 승/패를 귀속한다. → `symbol_stats`/`pattern_stats`/`grade_stats`/`summary` JSON.
- **에이전트 반영(`MemoryView`)**:
  - 스크리닝: `symbol_score_adjust(code)` — 반복 손절(≥2회)·저승률 종목은 점수 차감(기준 강화). 예: "이 종목 최근 손절 3회 → -21점".
  - 신호분석: `pattern_confidence(signal, passed)` — 그 패턴 과거 승률이 floor(기본 35%) 미만·표본 충분(≥5)이면 **진입 보류(NO_ENTRY로 하향)** + 사유 기록.
  - 리스크: `grade_winrate(grade)` — 그 시장 등급 과거 승률이 낮으면(표본 충분) 신규 진입 거절(`MEMORY_GRADE`). **단 GREEN(정상 운영)은 제외**한다(아래 불변식).
- **메모리 생존 불변식(죽음의 나선 방지)**: 메모리 보정은 *넛지*일 뿐 시스템을 **무거래로 자가정지시켜선 안 된다**(§5.7 "무보유→진입 반복" 위반 금지). 백테스트가 손실 구간을 학습하면 승률이 floor 아래로 떨어지는데, 이때 보정이 전 단계를 동시에 차단하면 신규 진입이 0이 되고 더는 새 데이터가 쌓이지 않아 영구 정지된다. 이를 막기 위해:
  1. **리스크 등급 게이트는 기본 등급 GREEN을 거절하지 않는다** — GREEN은 정상 운영 등급이므로 메모리 사유 전면 거절 대상에서 제외(위험 등급 YELLOW 등에만 적용; RED/BLACK은 시장상태로 이미 차단).
  2. **스크리닝은 유니버스를 통째로 비우지 않는다** — 메모리 감점으로 임계 통과 후보가 0이 되면 최고점 후보 1개를 폴백으로 발행한다(단 관리종목/거래정지 하드 필터 -100은 폴백으로도 부활 금지). 하류 신호분석·리스크 게이트가 최종 판정한다.
- **리포트 연동**: `best_pattern()`/`worst_pattern()`이 §17.7 리포트의 "가장 잘 된/안 된 패턴"에 쓰인다.
- 메모리는 `data/memory/*.json`에 영속화되어 다음 실행 시 그대로 로드된다(런 간 누적).

### 19.1 구현 매핑 추가
| 영역 | 모듈 | 주요 객체 |
|---|---|---|
| 장기 메모리 | `core/memory/` | `MemoryStore`(저널 rebuild·영속), `MemoryView`(`symbol_score_adjust`/`pattern_confidence`/`grade_winrate`/`best·worst_pattern`) |
| 백테스트 제어 | `traidair/server.js` | `/api/backtest/{start,stop,reset}` (python spawn/kill, 기본 가상잔고 100만·`BACKTEST_AUTO_SPEED=1`), `backtest-live.html` 컨트롤 바·리포트 |
| 백테스트 HTS 연동 | `traidair/trading-hts.html` | ▶ 백테스트 버튼→`agentBTStart`(run_backtest.py), `/api/backtest/state` 1초 폴링→하단 푸터(평가손익·수익률·매매횟수)·진행 패널·캔들 매수(▲)/매도(▼) 마커(`_drawAgentMarkers`)·완료 리포트 |
| 자동 배속 | `core/backtest/runner.py`, `scripts/run_backtest.py` | `BacktestRunner(pacer=…)` 훅 + `AutoSpeedGovernor`(psutil CPU 부하 기반, 임계 88% 초과 시 미세 감속, 한가하면 최고속) |
| 메타 진화 실행 | `scripts/run_evolve.py`, `traidair/server.js` | 🧬 진화+ 버튼→`/api/backtest/evolve`(run_evolve.py spawn)→`OptimizerAgent.observe/propose`(기본 **자동적용 안 함**, `EVOLVE_APPLY=1`만 일괄적용), `/api/backtest/evolve-result`로 제안 서빙 |
| 제안 개별 적용+검수 | `scripts/apply_proposal.py`, `traidair/server.js` | 제안 카드 **✅ 적용** 버튼→`/api/backtest/apply-proposal`(POST `{id}`)→`apply_proposal.py` spawn: ①`apply_proposal_to_file` 수정 ②재읽기 yaml 검증 ③`git commit`. **paper 한정**(live=`locked`), 화이트리스트 외/검증 실패 시 구체 사유 반환. stdout UTF-8 강제 |

---

## 20. 단일 매매AI → 멀티에이전트 이관 + HTS 통합

- **단일 AI 비활성화**: traidair HTS에 내장됐던 단일 Claude 자동매매(`MENTOR_SYSTEM_PROMPT`/`autoState`/`startAuto`)를 `AI_HANDOFF_TO_AGENTS` 플래그로 **비활성화(코드 보존)**. 그 역할·판단기준은 각 부서 에이전트가 흡수했다. 상세 매핑은 `agents/AI_HANDOFF.md`.
  - 시장분석→시장상황, 종목선정→스크리닝, 매매신호→신호분석, 주문→주문실행, 리스크→리스크, 익절/청산→포지션매니저, 일지/복기→학습부+메타, 과거통계→장기메모리(§19).
- **HTS 기능 유지**: 캔들차트·보조지표·호가창·체결창·주문창·**수동 주문** 전부 그대로. 자율매매 *실행*만 비활성화되며 멘토 챗(분석 보조)은 유지된다.
- **에이전트 주문 실시간 표시**: HTS에 떠 있는 `🤖 에이전트 체결` 위젯이 `/api/backtest/state`의 `todayTrades`를 2초마다 폴링해 수동 주문창과 **공존**하며 표시한다.
- **▶ 백테스트 버튼 = ai-agent 백테스트**: HTS 툴바의 `🚀 백테스트` 버튼(`openBacktestDialog`)은 내장 JS 엔진이 아닌 **ai-agent `run_backtest.py`**를 실행한다(`/api/backtest/start`). 가상잔고 기본 **100만원**(입력·리셋 가능), 배속은 **시스템 부하를 보며 자동 최고속**(`AutoSpeedGovernor`, 사용자 선택 불필요). 진행 상황은 `/api/backtest/state` 1초 폴링으로 **기존 HTS 하단 푸터**(평가손익·수익률·매매횟수)·진행 패널에 실시간 반영되고, 에이전트가 보는 종목/날짜로 차트를 자동 전환해 **캔들차트 위에 매수(▲)/매도(▼) 마커**를 표시한다. 완료 시 수익률·MDD·승률·손익비·패턴·메타 제안 리포트 모달을 띄운다. 기존 내장 JS 백테스트 함수(`startBacktest` 등)는 코드 보존(미연결).
- **🧬 진화+ 버튼 = 메타 최적화 실행**: `runAgentEvolve`가 `/api/backtest/evolve`로 `run_evolve.py`를 실행 → 메타 `OptimizerAgent`가 최근 저널을 관찰·제안한다(live는 관찰만, §3.3). 결과는 **토스 스타일 제안 카드**(`agentEvolveShowResult`)로 표시: 각 제안을 쉬운 한국어(`_proposalFriendly`, 예 "⏱ 방향 안 나올 때 더 빨리 정리하기 (현재 30분 → 25분)")로 변환한 개별 카드 + **✅ 적용** 버튼.
  - **자동 적용 안 함**: 진화는 더 이상 일괄 자동반영하지 않는다(기본 `EVOLVE_APPLY=0`). 각 카드의 적용 버튼이 **개별** 제안만 반영한다.
  - **적용 버튼 = 실제 수정 + 검수 + 자동 커밋**(§22.x, `apply_proposal.py`): ①`strategy_params.yaml` 수정 → ②재읽기로 값 변경 검증 → ③성공 시 `git commit`. 성공이면 카드에 "✅ 적용 완료 (30→25) · 커밋 abc1234", 실패면 "❌ 적용 실패: [구체 사유]"(화이트리스트 외 키/스칼라 미발견/검증 불일치/파일 오류), live면 "🔒 실전 모드에서는 전략 수정 불가". **paper 모드에서만** 적용된다.
  - 백테스트 완료 리포트(`agentBTShowReport`)의 **가장 잘 된/못 된 패턴**도 토스 카드(🏆 초록/⚠️ 빨강 + 신호·지표 태그 + 거래/승률/손절 수치)로, 메타 제안 요약도 친화 카드로 표시한다. 기존 JS GA 엔진(`toggleEvolvePlus`)은 코드 보존(미연결).
- **start.bat 자동 오픈**: 런처가 traidair 준비(포트 3000) 후 브라우저로 `http://localhost:3000/hts`를 자동으로 연다(server.js `/hts` 라우트).

---

## 21. 작업·토큰 소모 최적화

- **백테스트 핫 루프 네트워크 109회/일 → 0회** (측정 기준 1거래일):
  - 매크로 지수(`get_market_data`)를 **로컬에서 산출**: 지수 분봉(^KS11/^KQ11/^IXIC)이 있으면 그것, 없으면(키움 수집=6자리 종목만) **유니버스 구성종목 바스켓 평균 등락률을 KOSPI 프록시**로 컷오프 산출(`ReplayKisClient._basket_macro_proxy`, 분 단위 캐시) → traidair `market-data` 호출 79회 제거. **주의: traidair sim 은 과거 일자를 무시하고 오늘 실시간 매크로를 반환(룩어헤드)하므로 백테스트에서 미사용**(§17.2). 등급이 그날 실제 시황으로 산출되어 이전(오늘장 기준 always-GREEN 경향)과 달라진다 → §6·§13 검증 대상(30영업일 백테스트 완료, 운영자 승인 대기).
  - 백테스트는 과거 DART 재현이 불가하므로 스크리닝 `enable_dart=False` → `dart/corpcode` 호출 30회 제거(페널티는 어차피 0).
  - 결과: 백테스트가 **완전 로컬**로 동작(traidair 없이도 실행), 약 3초/거래일.
- **토큰(LLM) 소모 0**: ai-agent 에이전트는 전원 규칙 기반으로 Claude를 호출하지 않는다(`meta.claude_call` 발행 코드 없음). traidair 단일 AI 자율매매도 비활성화(§20)되어 백그라운드 토큰 소모가 없다. 향후 LLM 보조 도입 시 메타부가 `meta.claude_call` 트레이스로 위반·중복·과다 호출을 탐지·억제한다(§2.7).
- **부수 수정**: `BACKTEST_CASH`(traidair 가상잔고 입력) 오버라이드가 러너에 누락돼 broker(100M)와 대시보드(입력값) 기준이 어긋나던 버그 수정 → 손익률 정상화.

---

## 22. ai-agent ↔ traidair 통합 API (`/api/agent/*`)

> §15가 정의한 저수준 `/api/kis/*` 래핑 위에, ai-agent 각 부서가 직접 쓰는 **고수준 통합 API**를 traidair에 추가한다. KIS App Key/Secret은 **traidair만 보관·호출**하며(§15.1), ai-agent는 KIS 키를 모른 채 **X-Agent-Key 헤더로 HTTP만** 호출한다.

### 22.1 인증
- 모든 `/api/agent/*` 호출은 헤더 `X-Agent-Key: <key>`를 요구한다. 서버 키는 `process.env.AGENT_KEY`(기본 `traidair-agent-dev`), 클라이언트 키는 `KisClientConfig.agent_key`(`config/kis_api.yaml: agent_key` 또는 `TRAIDAIR_AGENT_KEY`, 기본 동일).
- 불일치 시 `401 {ok:false,error}`. WebSocket은 `X-Agent-Key` 헤더 또는 `?key=` 쿼리로 인증.

### 22.2 엔드포인트 ↔ 에이전트 매핑 (traidair `server.js`)
| 엔드포인트 | 메서드 | 사용 에이전트 | 래핑 대상(내부 루프백) |
|---|---|---|---|
| `/api/agent/screen/candidates` | GET | 스크리닝 | `/api/kis/volume-rank` (거래대금 상위) |
| `/api/agent/market/snapshot` | GET | 시장상황 | `/api/market-data?mode=realtime` |
| `/api/agent/quote/:code/indicators` | GET | 신호분석 | `/api/kis/chart` + JS 지표계산(RSI/MACD/MA/거래량비) |
| `/api/agent/risk/check` | GET | 리스크 | `/api/kis/inquire-psbl-order` + `/api/kis/orderbook` + `/api/kis/balance` |
| `/api/agent/positions` | GET | 리스크 | `/api/kis/balance` |
| `/api/agent/order` | POST | 주문실행 | `/api/kis/order`(현금) · `/api/kis/order-credit`(신용) 자동 분기 |
| `/api/agent/journal`·`/journal/today` | POST·GET | 학습부 | `data/journal/{YYYYMMDD}.jsonl` append/read |
| `/api/agent/backtest/run` | POST | 백테스트 | `run_backtest.py` 구동(§17, HTS 백테스트 버튼 연동) |

- 구현 원칙: 검증된 기존 라우트를 **내부 루프백(`callSelf`)**으로 재사용하고, traidair 보관 키(`runtimeConfig.kisAppKey/Secret/Account`)를 주입한다 → 기존 `/api/kis/*` 로직 무수정.

### 22.3 WebSocket `/ws/market`
- 시장상황 에이전트 실시간 구독용. `ws` 의존성 없이 RFC6455 텍스트 프레임을 직접 구현(서버→클라이언트). 접속 즉시 1회 + 이후 3초 간격으로 `{type:"market", ts, data}`(매크로 스냅샷) 푸시. 인증은 §22.1.

### 22.4 ai-agent 측 클라이언트
- `core/kis_client/AgentApiClient` — httpx 비동기. 7개 엔드포인트 메서드(`screen_candidates`/`market_snapshot`/`quote_indicators`/`risk_check`/`positions`/`order`/`journal_append`/`journal_today`/`backtest_run`). `ok` 판정·6초 데드라인·1회 백오프 재시도(§15.2 계승), `ok:false`→`KisBusinessError`, 401→`KisAuthError`.
- `KisClientConfig.agent_key` 추가. 기존 `KisClient`(저수준 `/api/kis/*`)는 백테스트 엔진·리플레이·테스트가 의존하므로 **유지**되며, `AgentApiClient`가 라이브 통합의 정면(front door)이다. 부서별 데이터 소스를 `AgentApiClient`로 옮기는 전환은 §6 규칙(30영업일 재검증)을 따라 단계 적용한다.

### 22.5 구현 매핑 추가
| 영역 | 모듈 | 주요 객체 |
|---|---|---|
| 통합 API(서버) | `traidair/server.js` | `/api/agent/*` 7종 + `agentAuthOk`/`callSelf`/`computeIndicators`/`wsFrame`, `server.on('upgrade')`(/ws/market) |
| 통합 클라이언트 | `core/kis_client/agent_api.py` | `AgentApiClient`, `KisClientConfig.agent_key` |

---

## 23. 학습부 노션 지식 동기화 (외부 전략 지식 → 전 부서 반영)

> 학습부가 **Notion 페이지(단타 트레이딩 마스터 커리큘럼 등)를 읽어** 매매 전략 지식으로
> 분류하고, 각 부서 에이전트가 세션 시작 시 참조한다. 학습부 원칙(§11)에 따라 **전략
> 파라미터를 직접 바꾸지 않으며**, 노션 지식은 §19 메모리와 동일하게 *참고(넛지)* 위상이다.
> 실제 파라미터 변경은 여전히 §6·§13 절차(백테스트 + 운영자 승인, 메타부 제안)로만 이뤄진다.

### 23.1 작동
1. **읽기**: `NotionClient`(httpx)가 `api.notion.com`에서 페이지+자식페이지 블록을 재귀 수집.
   토큰은 `config/kis_api.yaml`의 `notion.token`(또는 `NOTION_TOKEN` env), 페이지는 `notion.page_id`.
2. **분류**(규칙 기반, LLM 미사용 §15.4·§21): 5개 부서 카테고리로 귀속.
   - 스크리닝 기준 → 스크리닝 / 매수 진입 조건 → 신호분석 / 손절·익절 → 리스크
   - 시간대별·시장 환경 → 시장상황 / 기타 매매 원칙(심리·복기) → CEO
3. **저장**: `data/memory/notion_knowledge.json`(원자적 기록).
4. **참조**: 각 에이전트가 생성 시 `notion_knowledge=NotionKnowledgeView` 주입 → `📚` 로그.
5. **주기 확인(매일 1회)**: Windows 작업 `ai-team-notion-sync`(16:10)가 `sync_notion.py` 실행.
   콘텐츠 해시 변경 시에만 재기록 + `data/memory/notion_updates.log` 변경 내역 append +
   `learning.notion` 토픽 발행. 라이브 세션 시작 시에도 1회 best-effort 동기화.
6. **상담 탭 현황**: traidair `💬 상담` 탭 "📚 노션 학습 현황" — 마지막 반영/확인 시각,
   부서별 반영 건수·샘플, 변경 내역, **🔄 지금 업데이트** 버튼(수동 동기화).

### 23.2 구현 매핑
| 영역 | 모듈 | 주요 객체 |
|---|---|---|
| 노션 클라이언트 | `core/notion_client/client.py` | `NotionClient`(재귀 블록/자식페이지 수집), `NotionConfig.from_files`(kis_api.yaml `notion` 섹션·env 폴백), `NotionPage`/`NotionSection` |
| 지식 분류 | `core/notion_client/classifier.py` | `classify_knowledge`(5카테고리 키워드 분류), `AGENT_CATEGORIES` |
| 지식 뷰 | `core/notion_client/knowledge.py` | `NotionKnowledgeView`(`for_agent`/`rules`/`summary_line`, 파일 없으면 빈 뷰) |
| 학습부 동기화 | `agents/learning/notion_sync/main.py` | `NotionSyncAgent`(`sync`/`status`, 해시 변경 감지, `notion_updates.log`, `learning.notion`) |
| CLI/스케줄 | `scripts/sync_notion.py` | `--force`/`--status`/`--json`/`--install-schedule`(매일 16:10) |
| 에이전트 참조 | screening/signal/risk/market_watch/ceo `main.py` | 생성자 `notion_knowledge` kwarg(선택), 세션 시작 시 `📚` 로그 |
| 상담 탭(서버) | `traidair/server.js` | `GET /api/agent/notion/status`, `POST /api/agent/notion/sync`(token env 폴백) |
| 상담 탭(UI) | `traidair/trading-hts.html` | `notionStatusLoad`/`notionSync`/`notionRenderStatus`, 사이드바 "📚 노션 학습 현황" 패널 |
| 토픽 | `learning.notion` | 학습부 → (구독자) 노션 지식 갱신 알림 |

---

## 24. 자기개선 파이프라인: 상담·복기·노션 → 실제 반영 + 영구 기억

> "상담에서 고치겠다고 해도 실제로 안 바뀜 / 복기가 다음 매매에 반영 안 됨 / 매 세션
> 기억 초기화" 문제를 해결한다. 상담(💬)·복기(학습)·노션(§23)에서 도출된 모든
> ``strategy_params.yaml`` 변경을 **단일 진입점(`StrategyEditor`)**으로 실제 파일에
> 반영하고, 그 변경을 **영구 기억(ImprovementLog/ConsultLog)**으로 누적해 다음 세션이
> 이어받는다. 하드리밋(§4)·잠금(§3.3)·화이트리스트(§13)는 그대로 강제된다.

### 24.1 단일 적용 진입점 (`core/strategy/editor.py`)
- `StrategyEditor.apply(key, value, *, source, reason, …)` 한 곳에서:
  ① 모드 게이트(live 잠금 §3.3) → ② 화이트리스트(`TUNABLE_KEYS`, §4·§13) →
  ③ 주석 보존 leaf 교체(`set_yaml_leaf`, **스칼라+인라인 리스트** RSI구간/익절범위 지원) →
  ④ 재읽기 검증 → ⑤ git 자동 커밋 → ⑥ `ImprovementLog` 영구 기록.
- 변경 전후 값을 `display`("RSI 진입 구간: [50, 65] → [55, 65]로 변경됨")로 돌려준다.
- `TUNABLE_KEYS` 확장: RSI 구간·과매수, 거래량 배수, 스크리닝 임계, 하드 손절, 익절
  1·2차 범위/비율, 트레일링, 타임스톱. 하드리밋 파일은 절대 제외.

### 24.2 상담 → 즉시 반영 (`scripts/consult_apply.py`)
- `--text "RSI 기준 55~65로"` : 규칙 기반 파서(`core/consult/parser.py`, LLM 미사용
  §15.4·§21)가 자연어에서 화이트리스트 키 변경을 추출해 일괄 적용.
- 수치 없는 막연한 상담은 적용 없이 **경고**("상담만 하고 안 고치면 의미 없음").
- 모든 발화·결과는 `ConsultLog`(`data/memory/consult_log.json`)에 누적 → 다음 상담이
  맥락 자동 로드(`context_brief`), "지난번에 RSI 바꿨는데 어땠어?"는
  `last_change_for_key` + `ImprovementLog`로 응답.

### 24.3 복기 → 학습 → 자동 반영 (`scripts/auto_learn.py`, `core/learning/review.py`)
- `ReviewLearner.analyze(journal)`가 반복 손실 패턴을 코드화한 개선안 생성:
  - **손절 N회 연속** → 진입 RSI 하단 +5(질 강화)
  - **타임스톱 청산 과다(≥40%)** → 평가 주기 −5분(자금 회전)
- paper에서 `StrategyEditor`로 자동 반영 + `ImprovementLog` 기록. `--dry-run`은 추출만.
- 변경 전후 성과 비교(`evaluate_effects`) → **효과 없는 변경은 롤백 후보**(`rollback_candidates`).
- 결과는 `state/learn_result.json`(HTS "최근 적용된 변경사항"·타임라인·롤백 제안).

### 24.4 영구 기억 (`core/memory/{improvement_log,consult_log,session}.py`)
- `ImprovementLog`(`data/memory/improvement_log.json`): 모든 변경의 `{출처, 키, 전/후,
  사유, 기대효과, 커밋, 전후 성과, verdict}` 누적. `timeline`/`rollback_candidates`/`session_brief`.
- `CeoAgent.load_session_memory()`(부팅 시 호출): `session_learning_brief`로 직전 개선·
  상담 맥락·노션 반영을 `🧠` 로그로 떠올린다 → **세션 간 기억 초기화 해결**.

### 24.5 노션 실시간 반영 (§23 확장)
- `sync_notion.py --apply`: 동기화 후 `extract_strategy_rules`로 노션 규칙을 추출해
  `StrategyEditor`로 **우선 반영**(노션 > strategy_params.yaml). 하드리밋은 화이트리스트
  밖이라 노션도 덮어쓰기 불가.
- 충돌(직전 상담/복기와 다른 값) → `conflicts`로 표시(노션 우선 적용하되 알림).
- 미반영 고급 규칙(R/R 게이트·VWAP·OBV 등) → `pending_rules`("도입 예정").
- `--install-watch`: **5분마다** 변경 감지+반영 Windows 작업(`ai-team-notion-watch`).
- `NotionSyncAgent.status()`에 `applied_rules`/`pending_rules`/`conflicts`/`strategy_applied_at`
  노출 → HTS 상담 탭 노션 패널.

### 24.6 구현 매핑 추가
| 영역 | 모듈 | 주요 객체 |
|---|---|---|
| 전략 적용 진입점 | `core/strategy/editor.py` | `StrategyEditor.apply`, `StrategyApplyResult` |
| YAML leaf 교체 | `agents/meta/optimizer/main.py` | `set_yaml_leaf`(스칼라+리스트), 확장 `TUNABLE_KEYS`/`LIST_KEYS` |
| 자연어 파서 | `core/consult/parser.py` | `extract_changes`, `Suggestion` |
| 복기 학습 | `core/learning/review.py` | `ReviewLearner.analyze`, `ReviewSuggestion` |
| 영구 기억 | `core/memory/{improvement_log,consult_log,session}.py` | `ImprovementLog`, `ConsultLog`, `session_learning_brief` |
| 노션 추출 | `core/notion_client/strategy_extract.py` | `extract_strategy_rules`, `PendingRule` |
| 노션 반영 | `agents/learning/notion_sync/main.py` | `NotionSyncAgent.apply_to_strategy`, 확장 `status()` |
| CLI | `scripts/{consult_apply,auto_learn,sync_notion}.py` | `--text`/`--dry-run`/`--apply`/`--install-watch` |
| 세션 기억 | `agents/ceo/main.py` | `CeoAgent.load_session_memory()`(부팅 시) |

> **불변식**: 본 파이프라인은 §3.3 잠금(live), §4 하드리밋, §13 화이트리스트, §19 죽음의
> 나선 방지를 모두 보존한다. live에서는 어떤 경로(상담·복기·노션)도 파라미터를 바꾸지
> 못하며, 화이트리스트 밖/하드리밋 키는 추출·적용 단계에서 거부된다.

---

## 25. 일봉+분봉 복합 전략 + 친화 사유 (1% 미만 손익 반복 문제 개선)

> 분봉만 보고 진입해 큰 추세를 무시하고, 손절/익절 범위가 좁아 1% 미만 손익이 반복되던
> 문제를 해결한다. **일봉 추세 게이트 + 5분봉 돌파 타점** 복합 분석으로 교체하고, 손절/익절
> 범위를 넓히며, 모든 매매 사유를 쉬운 한국어로 표기한다.

### 25.1 매수 조건 (`agents/analysis/signal/indicators.py`)
- **일봉 게이트(전부 충족 필수)**: ① 일봉 종가 > 20MA  ② 일봉 RSI ≥ 50  ③ 최근 3일 중 양봉
  ≥ 2개  ④ 전일 거래량 ≥ 5일 평균 × 150%.
- **5분봉 진입 타점**: ① 직전 고점 돌파  ② 돌파 캔들 거래량 ≥ 직전 캔들 × 200%  ③ RSI 50~65
  ④ MACD 히스토그램 양전환. 4개 → `STRONG_ENTRY` / 3개 → `CONDITIONAL_ENTRY`.
- **진입 금지(하나라도 해당 → NO_ENTRY)**: 일봉 저항선 근처(고점 대비 -2% 이내) · 일봉 RSI ≥ 70 ·
  3일 연속 음봉. 분봉 마지막이 음봉이면(추격 금지) 무조건 NO_ENTRY.
- **일봉 데이터 부족 시**(백테스트 초기 등) 일봉 게이트는 '미확인'으로 보고 분봉 타점만으로
  판정하되(§19 무거래 자가정지 방지) `daily_strong=False`로 사이즈를 보수화한다.
- 일봉 캔들은 `SignalAgent._daily_candles`가 `kis.get_daily_chart`(live=traidair tf="D",
  backtest=`ReplayKisClient`가 `CandleStore` 분봉을 일봉으로 집계, 룩어헤드 없음)로 best-effort 조회.

### 25.2 익절/손절/타임스톱 (`exit_rules.py`, `strategy_params.yaml`)
- 익절: 1차 +3~5% **40%** · 2차 +6~**10%** **40%** · 트레일링 고점 대비 **-2%** 잔량 전량(§5.3).
- 손절: 기술적 = 진입 캔들 저점 **-0.5%** 이탈 · 하드 **-2%**(기존 -3%)(§5.4).
- 타임스톱: 30분 내 **+1% 미달** 시 50% 청산(§5.5).

### 25.3 포지션 사이징 (`agents/risk/risk_manager/main.py`)
- 일봉 추세 강할수록 비중 ↑: **STRONG + 일봉 강세 → 가용현금 × 2 × 0.7** / CONDITIONAL → × 2 × 0.4.
- STRONG이라도 일봉 강세(`EntrySignal.daily_strong`)가 아니면 0.4로 보수화. 사이징 권위는
  리스크부(§2.4)이며, 실행부(`order/main.py`)는 일봉 강세·사이즈 근거를 로깅해 추적성 보장.

### 25.4 매매 사유 친화 변환 (`exit_rules.friendly_exit_reason`)
- 모든 청산 사유를 쉬운 한국어 + 손익률로 표기. 예:
  - `technical_stop` → "📉 진입할 때 저점 밑으로 떨어져서 손절했어요 (-1.11%)"
  - `time_stop` → "⏱ 30분 기다렸는데 방향이 안 나와서 절반 팔았어요 (-0.47%)"
  - TP1/TP2 → "✅ 1·2차 목표가 도달! …익절했어요", EOD → "🔔 장 마감이라…", 트레일링 → "📈 고점에서 밀려서…".
- 진입 사유도 `indicators._compose_reason`이 친화 문장으로 생성("🚀 일봉 추세 양호 + 분봉 타점
  4/4 충족 → 강하게 진입", "⛔ 진입 안 함 — …"). `PositionManagerAgent`가 `ExitEvent.reason`에
  친화 사유를 실어 대시보드(`todayTrades`)·저널에 노출한다.

### 25.5 구현 매핑 추가
| 영역 | 모듈 | 주요 객체 |
|---|---|---|
| 복합 신호 | `agents/analysis/signal/indicators.py` | `SignalAnalyzer.evaluate(daily_candles=)`, `DailyCheck`, `_eval_daily`/`_eval_breakout`, `SignalDecision.daily_strong` |
| 일봉 데이터 | `core/backtest/replay_client.py`, `core/kis_client/client.py` | `get_daily_chart`(집계/패스스루) |
| 친화 사유 | `agents/execution/position_manager/exit_rules.py` | `friendly_exit_reason`, `_portion_word` |
| 청산 적용 | `agents/execution/position_manager/main.py` | `_do_exit`(친화 사유 → `ExitEvent.reason`) |
| 사이징 | `agents/risk/risk_manager/main.py` | `SizingParams`(0.7/0.4), `_size`(daily_strong) |
| 플러밍 | `agents/analysis/signal/main.py`, `order/main.py` | `EntrySignal.daily_strong`, 5분봉 기본 tf, 사이즈 근거 로깅 |

> **불변식**: §4 하드리밋·§5.7 EOD 강제청산·§19 무거래 자가정지 방지는 그대로 보존된다.
> 일봉 게이트가 막혀도 시스템이 영구 무거래에 빠지지 않도록 데이터 부족 시 분봉 판정으로 폴백한다.

---

## 26. 백테스트 실행 경로 단일화 (이중 실행 충돌 제거)

> 이전에는 백테스트(=paper 모드 §17) 실행 경로가 **둘**이었다 — ① `start.bat` 메뉴의
> `run_paper.py` 직접 실행, ② traidair HTS '▶ 백테스트' 버튼(`run_backtest.py` spawn).
> 두 경로가 **같은 `state/` 제어판**(`backtest_live.json` 상태파일 + `BACKTEST_STOP`/
> `BACKTEST_PAUSE`/`KILL_SWITCH` 센티넬)을 공유해, HTS에서 백테스트를 누르면 `start.bat`
> 쪽 엔진이 STOP 센티넬을 감지해 종료되고, 두 엔진이 상태파일을 두고 경합해 "즉시 종료
> 후 결과창만 뜨는" 충돌이 발생했다. 본 절은 **실행 경로를 traidair HTS 하나로 통일**한다.

### 26.1 단일 실행 경로
- **유일한 실행 경로 = traidair HTS '▶ 백테스트' 버튼**. traidair `server.js`가
  `run_backtest.py` 자식 프로세스를 단독으로 spawn/관리하며, 실행 상태(`btProc` +
  상태파일 신선도)도 서버가 단독 보유한다.
- `start.bat`의 메뉴 1번은 더 이상 `run_paper.py`를 직접 띄우지 않는다 — **traidair 서버
  보장 + 브라우저(`/hts`) 열기**만 수행한다(이중 실행 원천 차단, 요구 1·4).
- `scripts/run_paper.py`(→ `run_backtest.py` 위임)는 **개발용 터미널 직접 실행 경로로만**
  존속한다. 단, 아래 단일 실행 가드로 traidair 엔진과 동시 실행은 자동 차단된다.

### 26.2 단일 실행 보장(한 번에 하나의 백테스트만, 요구 2·3)
- **서버 측(`server.js` `startBacktest`)**:
  - 재진입 가드(`btStarting`)로 동시 시작 클릭에 엔진이 두 개 뜨지 않는다.
  - 추적 중인 엔진(`btProc`)이 살아있으면 **중복 시작을 거절**(정지■/리셋↺ 후 재시작 안내).
  - 추적이 끊긴 고아 엔진(상태파일만 신선)은 거절 대신 graceful 정지로 **자가 복구** 후 시작.
  - 시작 직전 `BACKTEST_STOP`/`PAUSE`/`KILL_SWITCH`/상태파일을 모두 제거 → 시작 즉시 종료
    (STOP/KILL 잔재)·이전 완료 리포트로 결과창만 뜨던 버그 차단. 직전 엔진의 마지막 파일
    쓰기 정착을 위해 250ms settle 후 spawn.
- **엔진 측(`run_backtest.py` 원자적 단일 실행 락)**:
  - 부팅 시 `state/backtest.lock`을 `O_CREAT|O_EXCL`로 **원자적 생성**해 락을 잡는다 —
    **정확히 하나의 엔진만** 생성에 성공한다. 진 쪽은 `FileExistsError`를 받고, 락 파일
    **mtime이 신선(<`_LOCK_TTL`=15s)**하면 상대가 살아있다고 보고 시작을 양보(코드 0 종료).
    죽은 엔진의 잔재(mtime이 TTL 초과)는 회수 후 재시도한다. **상태파일 파싱이 아닌 파일
    mtime**으로 신선도를 보므로 부분 기록 레이스가 없고, Windows에서 `os.kill`로 pid를
    죽일 위험도 없다.
  - **동시 기동 레이스 차단(핵심)**: 이전 `_foreign_engine_active`(상태파일 신선도+pid)는
    *이미 상태파일을 쓰는* 엔진만 감지해 **같은 순간 둘이 동시에 시작하면 둘 다 통과**하는
    구멍이 있었다(상태파일이 아직 없음). 이 구멍이 두 엔진이 같은 `state/` 제어판을 두고
    싸워 한쪽의 정지/정리가 다른 쪽을 조기 종료시키는 "며칠 만에 끝남" 증상의 근본 원인이었다.
    원자적 락은 이 레이스까지 닫는다.
  - 보유 엔진은 `_stop_watcher`가 **~2초마다 락 mtime을 갱신**(하트비트)하고, 재시작 백오프
    동안에도 `_touch_lock()`으로 갱신해 장시간 실행 중 stale 오판을 막는다. 종료(정상/예외/
    Ctrl+C) 시 `atexit`로 **우리 락만** 해제한다(남의 락 불침).
  - traidair `server.js`는 새 엔진 spawn 직전(이전 엔진 정지 보장 후) `backtest.lock`도 함께
    제거 → 강제종료로 atexit가 못 돈 잔재 락이 있어도 정상 재시작된다.
  - 개발용 의도적 병렬 실행은 `BACKTEST_ALLOW_PARALLEL=1`로 우회(락 미획득).
- **진단 로그(요구)**: 다일 루프가 매 거래일을 `▶ N일차 시작(날짜)` → `✔/✖ N일차 종료(사유:
  정상마감/데이터없음·에러/정지)` → `➡ 다음 날짜 선택` → 시작 시 `데이터 보유 거래일 풀 N개`,
  종료 시 `🏁 루프 종료 — 완주 N일/목표 M일(사유)`로 남겨 조기 종료 지점을 즉시 식별한다.

### 26.3 구현 매핑 추가
| 영역 | 모듈 | 주요 객체 |
|---|---|---|
| 단일 실행(서버) | `traidair/server.js` | `startBacktest`(재진입 가드 `btStarting`·중복 거절·고아 자가복구·전 센티넬+`backtest.lock` 제거+settle), `btKillSentinel`, `btLockFile` |
| 단일 실행(엔진) | `scripts/run_backtest.py` | `_acquire_singleton_lock`/`_release_singleton_lock`/`_touch_lock`(원자적 `O_EXCL` 락·mtime 신선도·atexit 해제·하트비트), `main()` 가드(`BACKTEST_ALLOW_PARALLEL` 우회). `_foreign_engine_active`는 폐기 |
| 다일 진단 로그 | `core/backtest/runner.py` | `run_forever`(N일차 시작/종료 사유·다음 날짜·전체 진행 N/M·루프 종료 사유), `_pool_brief`(데이터 보유 풀 요약) |
| 런처 단일화 | `start.bat` | 메뉴 1 = traidair 보장 + `/hts` 브라우저 열기(직접 `run_paper.py` 실행 제거) |
| 단일 실행 회귀 테스트 | `tests/unit/test_backtest_singleton_lock.py` | 동시 기동 시 정확히 1개만 획득·stale 회수·소유 락만 해제 |

---

_본 문서가 곧 시스템의 헌법이다. 코드와 문서가 충돌하면 문서 갱신 → 코드 동기화 순으로 처리한다._
