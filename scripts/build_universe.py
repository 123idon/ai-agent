"""config/universe.json 을 KOSPI200 / KOSDAQ150 전체로 채운다 (CLAUDE.md §18).

pykrx가 설치돼 있으면 KRX 지수 구성종목(1028=KOSPI200, 2203=KOSDAQ150)을 받아
전체 코드+종목명을 기록한다. pykrx가 없거나 실패하면 기존 시드를 유지한다.

  pip install pykrx
  python scripts/build_universe.py
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

log = logging.getLogger("build_universe")

_INDICES = [
    {"sym": "^KS11", "name": "코스피"},
    {"sym": "^KQ11", "name": "코스닥"},
    {"sym": "^IXIC", "name": "나스닥"},
]


def _fetch(index_code: str) -> list[dict]:
    from pykrx import stock  # type: ignore
    today = datetime.now().strftime("%Y%m%d")
    try:
        tickers = stock.get_index_portfolio_deposit_file(index_code)
    except TypeError:
        tickers = stock.get_index_portfolio_deposit_file(today, index_code)
    out: list[dict] = []
    for t in tickers:
        try:
            nm = stock.get_market_ticker_name(t)
        except Exception:  # noqa: BLE001
            nm = ""
        out.append({"code": str(t), "name": str(nm)})
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    root = Path(__file__).parents[1]
    path = root / "config" / "universe.json"
    univ = json.loads(path.read_text(encoding="utf-8"))

    try:
        kospi = _fetch("1028")
        kosdaq = _fetch("2203")
    except Exception as e:  # noqa: BLE001
        log.warning("pykrx 사용 불가(%s) — 기존 시드 유지. `pip install pykrx` 후 재실행.", e)
        return 0

    if kospi:
        univ["kospi200"] = kospi
    if kosdaq:
        univ["kosdaq150"] = kosdaq
    univ["indices"] = univ.get("indices") or _INDICES
    path.write_text(json.dumps(univ, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("universe.json 갱신: KOSPI200 %d, KOSDAQ150 %d", len(kospi), len(kosdaq))
    return 0


if __name__ == "__main__":
    sys.exit(main())
