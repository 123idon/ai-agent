# 단일 매매AI → 멀티에이전트 이관 매핑 (CLAUDE.md §20)

traidair HTS에 내장돼 있던 **단일 Claude 자동매매 AI**(`MENTOR_SYSTEM_PROMPT` + `autoState`
+ Phase 8 매수 전 통합 점검 + 3단계 AI 검토)가 혼자 수행하던 역할을 ai-agent의 각 부서
에이전트로 분배한다. 단일 AI의 **장점·프롬프트 로직(판단 기준)을 각 에이전트가 흡수**하며,
traidair 측 단일 AI 자율매매 코드는 `AI_HANDOFF_TO_AGENTS` 플래그로 **비활성화(보존)** 한다.

> ai-agent는 매매 결정을 LLM에 위임하지 않는다(§15.4). 단일 AI가 LLM 프롬프트로 내리던
> 판단을, 동일 기준의 **결정론적 규칙**으로 각 에이전트에 이식했다.

## 역할 분배 + 흡수한 기준

| 단일 AI가 하던 일 | 이관 대상 에이전트 | 흡수한 판단 기준(프롬프트 → 규칙) |
|---|---|---|
| **시장 분석** | 시장상황 (`agents/intel/market_watch`) | 시장체력 score(RSI/20MA/거래량×), 매크로 등급화 → `MarketGrade` GREEN/YELLOW/RED/BLACK. "손실 방어 우선" → RED/BLACK에서 신규 진입 차단 |
| **종목 선정** | 스크리닝 (`agents/intel/screening`) | "거래량 전일 대비 300%+ 강한 수급", 섹터/테마 모멘텀, 이슈/수급 기반 → 거래대금 랭킹 + 5요소 점수(70점↑). 장 전 루틴(Phase 8 STEP 1~3) |
| **매매 신호** | 신호분석 (`agents/analysis/signal`) | "RSI 단타 60 돌파 모멘텀", "MACD 12/26/9 골든크로스", "볼린저 수축 후 확장", "MA 당일 20/60", "거래량 300%+" → 5지표 종합(STRONG/CONDITIONAL/NO_ENTRY). Phase 8 GO 조건 |
| **주문 실행** | 주문실행 (`agents/execution/order`) | R/R 1:1.5, 분할 진입, 현금/신용 분기. 마감 전 청산 필수 → EOD 15:20/15:25 |
| **리스크 관리** | 리스크 (`agents/risk/risk_manager`) | "종목당 -3% 손절"=하드손절, "1회 비중", 연속손절 중지(`brk`)=HL-02 쿨다운, "연속 손실 후 즉각 재진입 금지"·뇌동매매 감지=HL-02/시간대 게이트(HL-03/04) |
| **익절/청산** | 포지션매니저 (`exit_rules`) | 1차 +3%(50%)/2차 +5~+8%(30%)/트레일링, "5MA 이탈 자동 손절"(Phase 9), 타임스톱 |
| **매매일지/복기** | 학습부 (`journal`/`pattern`) + 메타 (`optimizer`) | Phase 9~12 복기(진입타점/청산타이밍/R-R/원칙준수/반복실수), 전략 진화 제안 → `learning.proposal` |
| **과거 통계 반영** | 장기 메모리 (`core/memory`, §19) | 종목별 반복손절·패턴 승률·시장등급 승률을 각 에이전트가 세션마다 참조 |

## 단일 AI 비활성화 위치 (traidair, 보존)
- `src/hts/script.js`: `const AI_HANDOFF_TO_AGENTS = true;`
  - `startAuto()` 진입 시 즉시 return(자율매매 미동작) + 이관 안내 메시지.
  - 레거시 JS 백테스트의 일별 자동매매 재활성화 블록도 동일 플래그로 가드.
- **유지되는 것**: 캔들차트·보조지표·호가창·체결창·수동 주문·멘토 챗(분석 보조). 단일 AI의
  자율 *매매 실행*만 비활성화된다.
- 되돌리려면 `AI_HANDOFF_TO_AGENTS = false`.
