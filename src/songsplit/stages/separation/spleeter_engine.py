"""Spleeter 기반 SeparationEngine 구현. Demucs보다 가볍고 빠른 대안 엔진.

`python -m spleeter separate`를 서브프로세스로 호출한다. Demucs와 달리 스템을
부분적으로만 분리하는 옵션은 없고, 선택한 config(2/4/5stems)가 한 번에 전체를
분리한 뒤 요청한 stem만 골라 반환한다.

플랫폼 참고: spleeter 2.4.2는 tensorflow==2.12.1을 강하게 고정하는데, 이 버전이
요구하는 tensorflow-io-gcs-filesystem==0.32.0은 Windows용 wheel이 PyPI에
게시되어 있지 않다 (Windows에서는 0.31.0까지만 존재, 2026-08 기준 확인).
따라서 이 프로젝트의 기본 venv(Windows)에는 spleeter를 설치할 수 없고,
`pip install -e ".[spleeter]"`는 Linux/WSL/macOS 환경에서만 성공한다.
그런 환경에서는 아래 구현이 그대로 동작한다.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from songsplit.stages.separation.base import SeparationEngine

STEM_CONFIGS: dict[str, list[str]] = {
    "2stems": ["vocals", "accompaniment"],
    "4stems": ["vocals", "drums", "bass", "other"],
    "5stems": ["vocals", "drums", "bass", "piano", "other"],
}

_NOT_INSTALLED_HINT = (
    "spleeter가 설치되어 있지 않습니다. Windows에서는 spleeter 2.4.2가 요구하는 "
    "tensorflow==2.12.1의 하위 의존성(tensorflow-io-gcs-filesystem==0.32.0)에 "
    "Windows wheel이 없어 설치가 불가능합니다. Linux/WSL/macOS 환경에서 "
    "`pip install -e \".[spleeter]\"`로 설치한 뒤 사용하세요. 그전까지는 Demucs 엔진을 이용해 주세요."
)


class SpleeterEngine(SeparationEngine):
    name = "spleeter"

    def __init__(self, config: str = "4stems"):
        if config not in STEM_CONFIGS:
            raise ValueError(f"지원하지 않는 config: {config}. 사용 가능: {sorted(STEM_CONFIGS)}")
        self.config = config

    def available_stems(self) -> list[str]:
        return STEM_CONFIGS[self.config]

    def separate(self, wav_path: Path, targets: list[str], output_dir: Path) -> dict[str, Path]:
        available = set(self.available_stems())
        unknown = sorted(set(targets) - available)
        if unknown:
            raise ValueError(
                f"'{self.config}' 설정은 {unknown}을(를) 분리할 수 없습니다. 지원 악기: {sorted(available)}"
            )
        if not targets:
            raise ValueError("targets가 비어 있습니다")

        output_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            sys.executable,
            "-m",
            "spleeter",
            "separate",
            "-p",
            f"spleeter:{self.config}",
            "-o",
            str(output_dir),
            str(wav_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            if "No module named" in result.stderr and "spleeter" in result.stderr:
                raise RuntimeError(_NOT_INSTALLED_HINT)
            raise RuntimeError(f"spleeter 분리 실패:\n{result.stderr[-4000:]}")

        track_dir = output_dir / wav_path.stem
        produced: dict[str, Path] = {}
        for stem in targets:
            stem_path = track_dir / f"{stem}.wav"
            if not stem_path.exists():
                raise RuntimeError(f"spleeter 실행은 성공했지만 예상 출력 파일이 없습니다: {stem_path}")
            produced[stem] = stem_path
        return produced
