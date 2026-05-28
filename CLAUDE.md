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
- **가동시간**: 09:00 ~ 15:20 (분봉 갱신 주기)
- **역할**: 스크리닝 통과 종목에 대해 **5개 지표** 종합 평가
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

---

## 3. 모드 시스템

### 3.1 모드 정의
- **paper (모의)**: KIS 모의서버 사용, 가상 자금, 전략 파라미터 수정 가능
- **live (실전)**: KIS 실전서버 사용, 실자금 + 신용, **모든 전략 파라미터 잠금**

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

### 5.3 익절 룰 (3단 구조)
- **1차 +3~5%**: 보유 수량의 **50% 청산**
  - 진입 시 기대 변동성(ATR)에 따라 +3% / +4% / +5% 중 1단계 목표가를 분석부가 사전 지정
- **2차 +6~8%**: 잔여의 **30% 청산** (≒ 초기 수량의 15%)
  - 마찬가지로 ATR에 따라 +6/+7/+8% 중 사전 지정
- **3차 트레일링**: 잔여 35% 보유분에 대해 **고점 대비 -1.5% 이탈 시 전량 청산**
  - 트레일링은 분봉 종가 기준, 장 마감 15:20까지 유효 → 15:25 일괄 청산

### 5.4 손절 룰 (기술적 우선)
- **기술적 손절 (최우선)**: **진입 캔들 저점 이탈 즉시 청산**
  - 1분봉 종가가 진입 캔들 저점을 하향 돌파하면 무조건 시장가 청산
  - 진입 근거 붕괴(거래량 소멸, MACD 데드크로스, 5MA 이탈) 2개 이상 동시 발생 시도 청산
- **하드 손절**: **최대 -3%** (기술적 트리거가 늦게 잡힐 때의 안전 하한)
- 두 조건 중 **먼저 도달한 쪽**으로 청산

### 5.5 타임스톱
- 진입 후 **30분** 경과 시 보유 포지션 재평가
  - 손익 ±0% 부근에서 방향성 미발생 + 신호 약화 → 전량 청산
  - +0.5% 미달 → 보유 수량 50% 축소
- 타임스톱 트리거는 손절 카운터(HL-02)에는 산입하지 않는다(중립 청산).

### 5.6 운영 모드
- **데이트레이딩 단일**: 평균 보유 30분~3시간
- 종목별 변동성·시장상황 등급에 따라 진입 비중/익절 목표가만 자동 조정
- 스캘핑·HFT 관련 코드는 추가하지 않는다.

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
  - `learning.proposal` (학습부 → CEO, **paper 전용**)

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
│   │   └── order/
│   └── learning/
│       ├── journal/                # 항상
│       ├── pattern/                # paper 전용
│       └── postmortem/             # live 전용
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
| 15:20 | 트레일링 스톱 종료. 보유분 종가 청산 검토 |
| 15:25 | 잔여 보유분 시장가 청산 |
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
2. **mode 일치**: 송신 메시지의 `mode`는 `config/mode.yaml.current_mode`와 항상 일치. 단 `/api/kis/chart`·`/api/kis/orderbook`은 traidair가 내부적으로 real 호스트로 강제 조회하므로 시세는 mode와 무관(설계 의도).
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
| 실행부 | `agents/execution/order/main.py` | `OrderAgent` (현금/신용 자동 분기, mode는 KisClient 자체 관리) |
| 학습부 | `agents/learning/{journal, pattern, postmortem}/main.py` | `JournalAgent`(append-only JSONL, 항상), `PatternAnalysisAgent`(**paper-only**), `PostmortemPackager`(**live-only**) |
| KIS 클라이언트 | `core/kis_client/` | `KisClient` (httpx, 12개 라우트), `KisClientConfig.from_files()`, `CreditLedger`(LOAN_DT 자동 추적), Pydantic 모델 |
| 지표 라이브러리 | `core/indicators/` | `sma`, `ema`, `rsi`, `macd`, `volume_spike_ratio`, 6개 캔들 패턴 |
| 메시지 버스 | `core/messaging/Bus` | 토픽 기반 async pub/sub, 구독자 격리, 테스트용 `collector()` |
| 스크립트 | `scripts/{run_paper, run_live, switch_mode, kill_switch, healthcheck}.py` | 운영자 엔트리포인트 |

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
       ├─ publish "order.event"
       └─ publish "order.failed"

MarketWatchAgent.poll_once()         [매 60초]
       └─ publish "market.state" → RiskAgent의 market_state_provider 갱신
                                  → BLACK이면 CEO.kill() 호출

JournalAgent                         [모든 토픽 구독 → data/journal/{YYYYMMDD}.jsonl append-only]
```

### 16.2 테스트
- `pytest tests/unit/` — 103개 단위 테스트 (httpx.MockTransport로 traidair 응답 모킹)
- 검증 범위: KIS client / 하드리밋 6개 / 지표 5개 / 5지표 분류 / Bus / 3개 핵심 에이전트 / 시장상황 4단계 / 스크리닝 점수 / 학습부 journal·pattern·postmortem / CEO 부팅·Kill·잠금 해시 / market_state 통합 / `switch_mode` CLI

---

_본 문서가 곧 시스템의 헌법이다. 코드와 문서가 충돌하면 문서 갱신 → 코드 동기화 순으로 처리한다._
