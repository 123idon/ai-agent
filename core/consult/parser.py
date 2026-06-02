"""자연어 → 전략 파라미터 변경 추출기 (규칙 기반, LLM 미사용 §15.4·§21).

상담 문장·노션 규칙 라인에서 화이트리스트(``TUNABLE_KEYS``) 키의 새 값을 뽑는다.
보수적으로 동작한다: 명확한 수치 패턴이 있을 때만 변경을 제안하고, 애매하면 무시한다
(잘못된 자동 적용 방지). 퍼센트는 비율(0.02)로, 구간은 인라인 리스트로 변환한다.

지원 키:
  - signal.rsi.entry_zone                  ([low, high])
  - signal.rsi.overbought
  - signal.breakout.volume_mult            (5분봉 돌파 거래량 배수 — 실제 매매에 사용)
  - signal.entry_rules.strong_min_indicators       (진입 필요/STRONG 조건 개수, 1~4)
  - signal.entry_rules.conditional_min_indicators  (CONDITIONAL 조건 개수, 1~4)
  - screening.threshold
  - stop_loss.hard_max_pct                 (음수 비율)
  - stop_loss.technical_buffer_pct         (양수 비율, 기술적 손절 버퍼)
  - stop_loss.technical_stop_enabled       (bool, 기술적 손절 on/off)
  - take_profit.step1.pct_range            ([low, high] 비율)
  - take_profit.step2.pct_range            ([low, high] 비율)
  - take_profit.step3_trailing.trail_from_high_pct  (비율)
  - entry.sizing.cash_fraction_strong      (진입 비중, 0~1)

하드리밋(§4: 동시 보유 종목 수·연속 손절 쿨다운·진입 금지 시간·슬리피지·담보비율)은
``detect_hard_limit_request`` 로 분리 감지해 명확한 거부 사유를 돌려준다(추출하지 않음).
타임스톱(시간 기반 매도)은 제거되어(§5.5) time_stop 키는 더 이상 추출하지 않는다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Suggestion:
    key: str
    value: Any
    label: str          # 화면 표시용 한글 라벨
    reason: str         # 근거가 된 원문 조각


_NUM = r"(\d+(?:\.\d+)?)"


def _pct_to_ratio(raw: str) -> float:
    """'3' 또는 '3%' → 0.03. 이미 0~1 비율(예 0.03)이면 그대로."""
    v = float(raw)
    return v / 100.0 if v >= 1.0 else v


def _pct_aware(raw: str, ctx: str) -> float:
    """문맥에 '%'가 있으면 항상 백분율로(0.5% → 0.005), 없으면 _pct_to_ratio 규칙.

    sub-1% 값(0.5%)을 비율(0.5)로 오인하지 않도록 '%' 기호를 명시 신호로 쓴다.
    """
    if "%" in ctx:
        return float(raw) / 100.0
    return _pct_to_ratio(raw)


def extract_changes(text: str) -> list[Suggestion]:
    """문장에서 변경 제안 리스트를 추출(중복 키는 마지막 우선)."""
    out: dict[str, Suggestion] = {}
    low = text.lower()

    # ── RSI 진입 구간: "RSI ... 55~65" / "rsi 55-65" / "rsi 55 에서 65" ──
    m = re.search(
        rf"rsi[^0-9]{{0,12}}{_NUM}\s*(?:~|-|–|—|에서|부터)\s*{_NUM}", low,
    )
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        if 0 < lo < hi <= 100:
            out["signal.rsi.entry_zone"] = Suggestion(
                "signal.rsi.entry_zone", [int(lo), int(hi)],
                "RSI 진입 구간", m.group(0),
            )

    # ── RSI 과매수 상단: "과매수 ... 75" / "rsi 상단 75" ──
    m = re.search(rf"(?:과매수|상단)[^0-9]{{0,10}}{_NUM}", low)
    if m:
        v = float(m.group(1))
        if 50 <= v <= 100:
            out["signal.rsi.overbought"] = Suggestion(
                "signal.rsi.overbought", int(v), "RSI 과매수 상단", m.group(0),
            )

    # ── 거래량 배수: "거래량 ... 2.5배" / "거래량 배수 2.5" ──
    #    실제 5분봉 돌파 타점에 쓰이는 signal.breakout.volume_mult 를 바꾼다(과거엔 미사용
    #    legacy 키 signal.volume_surge_multiplier 를 건드려 매매에 반영되지 않았다).
    m = re.search(rf"거래량[^0-9]{{0,12}}{_NUM}\s*배?", low)
    if m:
        v = float(m.group(1))
        if 1.0 <= v <= 10.0:
            out["signal.breakout.volume_mult"] = Suggestion(
                "signal.breakout.volume_mult", v, "돌파 거래량 배수", m.group(0),
            )

    # ── 스크리닝 임계: "스크리닝 ... 85" / "임계 85점" ──
    m = re.search(rf"(?:스크리닝|임계|선별 기준)[^0-9]{{0,12}}{_NUM}\s*점?", low)
    if m:
        v = float(m.group(1))
        if 50 <= v <= 100:
            out["screening.threshold"] = Suggestion(
                "screening.threshold", int(v), "스크리닝 점수 임계", m.group(0),
            )

    # ── 기술적 손절 버퍼: "기술적 손절 ... 0.5%" / "진입캔들 저점 1%" / "저점 이탈 0.5" ──
    #    (하드 손절보다 먼저 평가 — 아래 하드 손절 정규식의 bare "손절"과 충돌 방지)
    m = re.search(
        rf"(?:기술적?\s*손절|진입\s*캔들?\s*저점|저점\s*이탈)[^0-9%]{{0,12}}-?{_NUM}\s*%?",
        low,
    )
    if m:
        v = float(m.group(1))
        if 0 < v <= 5:
            out["stop_loss.technical_buffer_pct"] = Suggestion(
                "stop_loss.technical_buffer_pct", round(_pct_aware(str(v), m.group(0)), 4),
                "기술적 손절 버퍼", m.group(0),
            )

    # ── 기술적 손절 on/off: "기술적 손절 끄기/켜기/사용/비활성" ──
    m = re.search(r"기술적?\s*손절[^0-9]{0,8}(끄|켜|사용\s*안|비활성|해제|on|off|활성)", low)
    if m:
        word = m.group(1)
        enabled = word in ("켜", "사용", "활성", "on")
        out["stop_loss.technical_stop_enabled"] = Suggestion(
            "stop_loss.technical_stop_enabled", enabled,
            "기술적 손절 사용", m.group(0),
        )

    # ── 하드 손절: "손절 ... -2%" / "하드 손절 2%" / "최대 손절 2" (항상 음수 비율) ──
    #    "기술적 손절"은 위에서 처리하므로 bare "손절"이 그 문맥을 다시 잡지 않게 제외한다.
    m = re.search(rf"(?:하드\s*손절|최대\s*손절|손절선|손절)[^0-9%-]{{0,12}}-?{_NUM}\s*%?", low)
    if m and "기술적" not in low[max(0, m.start() - 6):m.start()]:
        v = float(m.group(1))
        if 0 < v <= 10:
            out["stop_loss.hard_max_pct"] = Suggestion(
                "stop_loss.hard_max_pct", -round(_pct_to_ratio(str(v)), 4),
                "하드 손절", m.group(0),
            )

    # ── 트레일링: "트레일링 ... 2%" / "고점 대비 2%" ──
    m = re.search(rf"(?:트레일링|고점\s*대비)[^0-9%]{{0,12}}{_NUM}\s*%?", low)
    if m:
        v = float(m.group(1))
        if 0 < v <= 10:
            out["take_profit.step3_trailing.trail_from_high_pct"] = Suggestion(
                "take_profit.step3_trailing.trail_from_high_pct",
                round(_pct_to_ratio(str(v)), 4), "트레일링 이탈폭", m.group(0),
            )

    # ── 익절 1차/2차 구간: "익절 1차 3~5%" / "1차 익절 3%" ──
    for step_no, key in ((1, "take_profit.step1.pct_range"),
                         (2, "take_profit.step2.pct_range")):
        rng = re.search(
            rf"(?:{step_no}\s*차[^0-9]{{0,8}}익절|익절[^0-9]{{0,8}}{step_no}\s*차)"
            rf"[^0-9]{{0,8}}{_NUM}\s*(?:~|-|–|—)\s*{_NUM}\s*%?",
            low,
        )
        if rng:
            lo = round(_pct_to_ratio(rng.group(1)), 4)
            hi = round(_pct_to_ratio(rng.group(2)), 4)
            if 0 < lo < hi < 1:
                out[key] = Suggestion(
                    key, [lo, hi], f"익절 {step_no}차 목표 범위", rng.group(0),
                )

    # ── 신호 진입 조건 충족 개수 (§5.2) ──
    #    "조건부 진입 3개" → CONDITIONAL / "신호 조건 4개로", "지표 4개 충족", "5개→4개" → STRONG.
    #    화살표/범위 표기(5→4)면 마지막 숫자(목표값)를 취한다(분봉 타점은 총 4개 → 1~4).
    m = re.search(rf"조건부[^0-9]{{0,12}}{_NUM}\s*개", low)
    if m:
        v = int(float(m.group(1)))
        if 1 <= v <= 4:
            out["signal.entry_rules.conditional_min_indicators"] = Suggestion(
                "signal.entry_rules.conditional_min_indicators", v,
                "조건부 진입 조건 개수", m.group(0),
            )
    # 조건부 문장은 위에서 conditional 로 처리했으므로 STRONG 추출에서 제외한다(한 숫자 중복 방지).
    if "조건부" not in low and re.search(r"신호|진입\s*조건|지표|충족", low):
        nums = re.findall(rf"{_NUM}\s*개", low)
        if nums:
            v = int(float(nums[-1]))
            if 1 <= v <= 4:
                out["signal.entry_rules.strong_min_indicators"] = Suggestion(
                    "signal.entry_rules.strong_min_indicators", v,
                    "신호 진입 조건 개수", f"{nums[-1]}개",
                )

    # ── 진입 비중(포지션 사이징): "비중 70%", "포지션 비중 0.7", "진입 비중 80%로" ──
    m = re.search(rf"(?:진입\s*비중|포지션\s*비중|매수\s*비중|비중)[^0-9%]{{0,8}}{_NUM}\s*%?", low)
    if m:
        v = _pct_aware(m.group(1), m.group(0))
        if 0.05 <= v <= 1.0:
            out["entry.sizing.cash_fraction_strong"] = Suggestion(
                "entry.sizing.cash_fraction_strong", round(v, 4),
                "진입 비중(STRONG)", m.group(0),
            )

    # (타임스톱 파싱 제거됨 — 시간 기반 매도가 폐지되어 time_stop 변경은 추출하지 않는다, §5.5.)
    return list(out.values())


# ── 하드리밋(§4) 변경 요청 감지 (추출 대상 아님 — 명확한 거부 사유 제공) ──
_HARD_LIMIT_HINTS: tuple[tuple[str, str], ...] = (
    (r"동시\s*보유|보유\s*종목\s*수|최대\s*보유|종목\s*수\s*[^0-9]{0,6}\d",
     "동시 보유 종목 수(최대 3)는 하드리밋(HL-01)이라 바꿀 수 없어요 (§4)."),
    (r"연속\s*손절|쿨다운",
     "연속 손절 쿨다운(3회·1시간)은 하드리밋(HL-02)이라 바꿀 수 없어요 (§4)."),
    (r"장\s*초반|장\s*후반|진입\s*금지\s*시간|14\s*:?\s*30|09\s*:?\s*30",
     "장초반/장후반 진입 금지 시간은 하드리밋(HL-03/HL-04)이라 바꿀 수 없어요 (§4)."),
    (r"슬리피지|틱\s*가드",
     "슬리피지 가드(5틱)는 하드리밋(HL-05)이라 바꿀 수 없어요 (§4)."),
    (r"담보\s*유지|담보\s*비율|마진콜",
     "신용 담보유지비율은 하드리밋(HL-06)이라 바꿀 수 없어요 (§4)."),
)


def detect_hard_limit_request(text: str) -> str | None:
    """하드리밋(§4) 변경 요청이면 명확한 거부 사유를, 아니면 ``None``.

    상담에서 화이트리스트 키 변경을 못 찾았을 때, "왜 안 되는지"를 분명히 알리기 위해
    쓴다(요구 5: 거부할 때만 이유 명시). 하드리밋은 모드와 무관하게 절대 변경 불가.
    """
    low = text.lower()
    for pat, reason in _HARD_LIMIT_HINTS:
        if re.search(pat, low):
            return reason
    return None
