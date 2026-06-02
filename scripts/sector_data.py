"""학습부 섹터 데이터 CLI / API 백엔드 (CLAUDE.md §2.2.1 섹터 가산점).

GET /learning/sector-data?date=YYYY-MM-DD 와 동일한 JSON 을 산출한다(traidair 가
이 함수를 래핑해 라우트로 노출). 입력 날짜 D 의 **전일 종가 기준** 섹터 데이터를 반환:

    {
      "date": "전일날짜(YYYYMMDD)",
      "sectors": [
        {"name": "반도체", "change_pct": 2.3, "top5_stocks": ["삼성전자", ...]},
        ...
      ]
    }

데이터 소스는 로컬 분봉(``CandleStore`` 일봉 집계, §18). 룩어헤드 없음.

사용:
    python scripts/sector_data.py 2026-06-02
    python scripts/sector_data.py 20260602   # YYYYMMDD 도 허용
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from agents.learning.sector import SectorDataProvider  # noqa: E402
from core.marketdata import CandleStore, load_universe, name_map  # noqa: E402


def sector_data_for(date: str) -> dict:
    """GET /learning/sector-data?date= 의 응답 dict. date 는 'YYYY-MM-DD' 또는 'YYYYMMDD'."""
    ymd = date.replace("-", "").strip()
    root = Path(__file__).parents[1]
    store = CandleStore(root)
    upath = root / "config" / "universe.json"
    names = name_map(load_universe(upath)) if upath.exists() else {}
    provider = SectorDataProvider.from_root(root, store, names=names)
    return provider.sector_data(ymd).to_dict()


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python scripts/sector_data.py <YYYY-MM-DD>", file=sys.stderr)
        return 2
    out = sector_data_for(argv[1])
    # UTF-8 강제(Windows 콘솔 한글), 라우트가 그대로 직렬화할 수 있게 ensure_ascii=False.
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
