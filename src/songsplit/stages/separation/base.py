"""[2] Separation 엔진 공통 인터페이스.

구현체(Demucs/Spleeter/실험적 엔진)는 이 인터페이스를 따르며,
파이프라인/UI는 구체 엔진을 몰라도 available_stems()/separate()만으로 동작한다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class SeparationEngine(ABC):
    """악기 선택적 분리를 지원하는 음원 분리 엔진의 추상 인터페이스."""

    #: UI에 표시할 엔진 이름
    name: str

    @abstractmethod
    def available_stems(self) -> list[str]:
        """현재 엔진(및 선택된 모델)이 분리할 수 있는 악기 목록.

        예: htdemucs -> ["vocals", "drums", "bass", "other"]
            htdemucs_6s -> ["vocals", "drums", "bass", "guitar", "piano", "other"]
        """

    @abstractmethod
    def separate(self, wav_path: Path, targets: list[str], output_dir: Path) -> dict[str, Path]:
        """wav_path를 targets에 해당하는 악기만 분리해 output_dir에 저장하고,
        {악기명: wav 경로} 매핑을 반환한다.

        targets는 available_stems()의 부분집합이어야 한다. 엔진이 targets 중
        일부만 단독 분리할 수 없는 경우(예: 4-stem 모델에서 guitar만 요청) ValueError를 발생시켜
        호출자(UI)가 상위 모델 사용을 안내할 수 있게 한다.
        """
