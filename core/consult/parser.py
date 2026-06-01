"""자연어 → 전략 파라미터 변경 추출기 (규칙 기반, LLM 미사용 §15.4·§21).

상담 문장·노션 규칙 라인에서 화이트리스트(``TUNABLE_KEYS``) 키의 새 값을 뽑는다.
보수적으로 동작한다: 명확한 수치 패턴이 있을 때만 변경을 제안하고, 애매하면 무시한다
(잘못된 자동 적용 방지). 퍼센트는 비율(0.02)로, 구간은 인라인 리스트로 변환한다.

지원 키:
  - signal.rsi.entry_zone                  ([low, high])
  - signal.rsi.overbought
  - signal.volume_surge_multiplier
  - screening.threshold
  - stop_loss.hard_max_pct                 (음수 비율)
  - stop_loss.technical_buffer_pct         (양수 비율, 기술적 손절 버퍼)
  - stop_loss.technical_stop_enabled       (bool, 기술적 손절 on/off)
  - take_profit.step1.pct_range            ([low, high] 비율)
  - take_profit.step2.pct_range            ([low, high] 비율)
  - take_profit.step3_trailing.trail_from_high_pct  (비율)
  - time_stop.evaluation_minutes           (메인 체크 분)
  - time_stop.min_profit_pct               (메인 체크 수익 기준 비율)
  - time_stop.action                       (reduce_50 | exit_all)
  - time_stop.first_check_minutes          (1차 체크 분)
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
    m = re.search(rf"거래량[^0-9]{{0,12}}{_NUM}\s*배?", low)
    if m:
        v = float(m.group(1))
        if 1.0 <= v <= 10.0:
            out["signal.volume_surge_multiplier"] = Suggestion(
                "signal.volume_surge_multiplier", v, "거래량 급증 배수", m.group(0),
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

    # ── 1차 타임스톱: "1차 타임스톱 15분" / "타임스톱 1차 15분" / "1차 체크 15분" ──
    #    (메인 타임스톱보다 먼저 — 아래 메인 정규식과 충돌 방지) ──
    m = re.search(
        rf"(?:1\s*차[^0-9]{{0,8}}(?:타임\s*스톱|체크|점검)|타임\s*스톱[^0-9]{{0,6}}1\s*차)"
        rf"[^0-9]{{0,8}}{_NUM}\s*분?",
        low,
    )
    if m:
        v = float(m.group(1))
        if 0 <= v <= 120:
            out["time_stop.first_check_minutes"] = Suggestion(
                "time_stop.first_check_minutes", int(v), "1차 타임스톱(분)", m.group(0),
            )

    # ── 타임스톱 수익 기준: "타임스톱 수익 1%" / "타임스톱 기준 수익 0.5%" ──
    m = re.search(rf"타임\s*스톱[^0-9%]{{0,12}}(?:수익|기준)[^0-9%]{{0,8}}{_NUM}\s*%?", low)
    if m:
        v = float(m.group(1))
        if 0 < v <= 5:
            out["time_stop.min_profit_pct"] = Suggestion(
                "time_stop.min_profit_pct", round(_pct_aware(str(v), m.group(0)), 4),
                "타임스톱 수익 기준", m.group(0),
            )

    # ── 타임스톱 동작: "타임스톱 전량 청산"→exit_all / "타임스톱 절반/50%"→reduce_50 ──
    m = re.search(r"타임\s*스톱[^.\n]{0,16}(전량|모두|전부|절반|반량|50\s*%|reduce|exit)", low)
    if m:
        w = m.group(1)
        act = "exit_all" if w in ("전량", "모두", "전부", "exit") else "reduce_50"
        out["time_stop.action"] = Suggestion(
            "time_stop.action", act, "타임스톱 동작", m.group(0),
        )

    # ── 타임스톱(메인): "타임스톱 ... 30분" / "타임스톱 25" ──
    #    "1차" 문맥 매치는 건너뛰어 위 first_check 와 충돌하지 않게 한다(둘 다 동시 지정 가능).
    for m in re.finditer(rf"타임\s*스톱[^0-9]{{0,12}}{_NUM}\s*분?", low):
        if "1차" in low[max(0, m.start() - 6):m.start()]:
            continue
        v = float(m.group(1))
        if 1 <= v <= 240:
            out["time_stop.evaluation_minutes"] = Suggestion(
                "time_stop.evaluation_minutes", int(v), "타임스톱(분)", m.group(0),
            )
        break

    return list(out.values())
