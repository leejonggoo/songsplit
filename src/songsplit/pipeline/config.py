"""전역 설정. jobs 디렉터리 위치, 기본 엔진 선택 등."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]


@dataclass
class AppConfig:
    jobs_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "jobs")
    default_separation_engine: str = "demucs"
    default_demucs_model: str = "htdemucs"
    sample_rate: int = 44100

    def ensure_dirs(self) -> None:
        self.jobs_dir.mkdir(parents=True, exist_ok=True)


DEFAULT_CONFIG = AppConfig()
