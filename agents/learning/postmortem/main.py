"""Postmortem packager — LIVE MODE ONLY (CLAUDE.md §2.6, §11).

일일 journal을 zip으로 묶어 운영자 검토용 패키지로 산출한다.
자동 파라미터 변경 권한 없음. 통계·요약만.
"""
from __future__ import annotations

import json
import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path

from core.kis_client import Mode

log = logging.getLogger(__name__)


class LiveModeRequired(RuntimeError):
    pass


@dataclass(frozen=True)
class PostmortemArtifact:
    date: str
    zip_path: Path
    record_count: int


class PostmortemPackager:
    def __init__(self, mode: Mode, journal_dir: Path, output_dir: Path) -> None:
        if mode != Mode.LIVE:
            raise LiveModeRequired("postmortem packager is live-only (§11)")
        self._journal = journal_dir
        self._output = output_dir

    def package_day(self, date: str) -> PostmortemArtifact:
        self._output.mkdir(parents=True, exist_ok=True)
        zip_path = self._output / f"postmortem_{date}.zip"
        journal_path = self._journal / f"{date}.jsonl"
        record_count = 0
        summary_lines: list[str] = [f"# Postmortem {date}\n"]

        if journal_path.exists():
            with journal_path.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    record_count += 1
            summary_lines.append(f"- records: {record_count}\n")
        else:
            summary_lines.append("- records: 0 (no journal file)\n")

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            if journal_path.exists():
                zf.write(journal_path, arcname=f"journal_{date}.jsonl")
            zf.writestr("SUMMARY.md", "".join(summary_lines))

        log.info("postmortem packaged: %s (%d records)", zip_path, record_count)
        return PostmortemArtifact(date=date, zip_path=zip_path, record_count=record_count)
