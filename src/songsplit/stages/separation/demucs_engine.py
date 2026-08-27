"""Demucs 기반 SeparationEngine 구현.

htdemucs(4-stem: vocals/drums/bass/other), htdemucs_6s(6-stem: +guitar/piano)를
선택적으로 사용하고, 단일 악기만 필요하면 --two-stems 모드로 연산량을 줄인다.
`python -m demucs` CLI를 서브프로세스로 호출한다 (최초 실행 시 사전학습 가중치를
자동 다운로드하므로 인터넷 연결과 다소의 시간이 필요할 수 있음).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from songsplit.stages.separation.base import SeparationEngine

FOUR_STEM_MODEL = "htdemucs"
SIX_STEM_MODEL = "htdemucs_6s"

FOUR_STEMS = ["vocals", "drums", "bass", "other"]
SIX_STEMS = ["vocals", "drums", "bass", "guitar", "piano", "other"]


class DemucsEngine(SeparationEngine):
    name = "demucs"

    def __init__(self, model: str = FOUR_STEM_MODEL, device: str = "cpu"):
        self.model = model
        self.device = device

    def available_stems(self) -> list[str]:
        return SIX_STEMS if self.model == SIX_STEM_MODEL else FOUR_STEMS

    def separate(self, wav_path: Path, targets: list[str], output_dir: Path) -> dict[str, Path]:
        available = set(self.available_stems())
        unknown = sorted(set(targets) - available)
        if unknown:
            raise ValueError(
                f"'{self.model}' 모델은 {unknown}을(를) 단독 분리할 수 없습니다. "
                f"지원 악기: {sorted(available)}"
            )
        if not targets:
            raise ValueError("targets가 비어 있습니다")

        output_dir.mkdir(parents=True, exist_ok=True)
        # 악기를 하나만 요청한 경우 --two-stems로 연산량 절감 (target vs 나머지 전부)
        two_stems = targets[0] if len(targets) == 1 else None

        cmd = [
            sys.executable,
            "-m",
            "demucs",
            "-n",
            self.model,
            "-d",
            self.device,
            "-o",
            str(output_dir),
            "--filename",
            "{stem}.{ext}",
        ]
        if two_stems:
            cmd += ["--two-stems", two_stems]
        cmd.append(str(wav_path))

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"demucs 분리 실패:\n{result.stderr[-4000:]}")

        model_dir = output_dir / self.model
        produced: dict[str, Path] = {}
        for stem in targets:
            stem_path = model_dir / f"{stem}.wav"
            if not stem_path.exists():
                raise RuntimeError(f"demucs 실행은 성공했지만 예상 출력 파일이 없습니다: {stem_path}")
            produced[stem] = stem_path
        return produced
