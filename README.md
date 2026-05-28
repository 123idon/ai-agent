# ai-agent

다중 에이전트 협업형 자동주식매매 시스템 (KRX / KIS Open API).

> **시작 전에 반드시 [`CLAUDE.md`](./CLAUDE.md) 를 읽으세요.**
> CLAUDE.md가 본 프로젝트의 단일 진실 공급원(SSOT)입니다.

## 빠른 개요
- **조직**: CEO / 정보부 / 분석부 / 리스크부 / 실행부 / 학습부
- **모드**: `paper`(모의) ↔ `live`(실전), 실전 진입 시 전략 파라미터 잠금
- **하드리밋**: 1종목 ≤15%, 동시보유 ≤3, 일손실 -2% halt, 14:30 이후/09:00~09:30 진입 금지 등

## 실행
```powershell
# 모의투자
python scripts/run_paper.py

# 실전 (모드 전환 필요)
python scripts/switch_mode.py --to live
python scripts/run_live.py

# 비상정지
python scripts/kill_switch.py
```

## 폴더 구조
`CLAUDE.md §8` 참조.
