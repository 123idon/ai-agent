"""수집 대상 유니버스 (CLAUDE.md §18).

``config/universe.json`` 구조:
{
  "indices":  [{"sym": "^KS11", "name": "코스피"}, ...],
  "kospi200": [{"code": "005930", "name": "삼성전자"}, ...],
  "kosdaq150":[{"code": "247540", "name": "에코프로비엠"}, ...]
}

- 종목 저장 키(store code)는 6자리 종목코드, Yahoo 티커는 ``{code}.KS``(코스피)/
  ``{code}.KQ``(코스닥). 지수는 Yahoo 심볼(^KS11/^KQ11/^IXIC)을 그대로 저장 키로 쓴다.
- ``scripts/build_universe.py``가 pykrx로 KOSPI200/KOSDAQ150 전체를 채울 수 있으며,
  실패 시 본 파일의 시드(주요 종목)만으로도 동작한다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Target:
    yahoo: str        # Yahoo 티커 (005930.KS, ^KS11 등)
    code: str         # 저장 키 (6자리 코드 또는 지수 심볼)
    name: str
    kind: str         # "kospi" | "kosdaq" | "index"


def yahoo_ticker(code: str, kind: str) -> str:
    if kind == "kospi":
        return f"{code}.KS"
    if kind == "kosdaq":
        return f"{code}.KQ"
    return code   # index symbol


def load_universe(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def all_targets(univ: dict) -> list[Target]:
    out: list[Target] = []
    for idx in univ.get("indices", []):
        out.append(Target(yahoo=idx["sym"], code=idx["sym"], name=idx.get("name", ""),
                          kind="index"))
    for s in univ.get("kospi200", []):
        out.append(Target(yahoo=yahoo_ticker(s["code"], "kospi"), code=s["code"],
                          name=s.get("name", ""), kind="kospi"))
    for s in univ.get("kosdaq150", []):
        out.append(Target(yahoo=yahoo_ticker(s["code"], "kosdaq"), code=s["code"],
                          name=s.get("name", ""), kind="kosdaq"))
    return out


def name_map(univ: dict) -> dict[str, str]:
    m: dict[str, str] = {}
    for s in univ.get("kospi200", []) + univ.get("kosdaq150", []):
        m[s["code"]] = s.get("name", "")
    for idx in univ.get("indices", []):
        m[idx["sym"]] = idx.get("name", "")
    return m
