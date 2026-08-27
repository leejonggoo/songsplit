"""오디오 로드/저장/포맷 통일 유틸리티. ffmpeg(pydub)로 임의 포맷을 읽어
표준 wav(44.1kHz)로 통일하는 것이 핵심 책임이다."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import soundfile as sf
from pydub import AudioSegment

from songsplit.pipeline.config import DEFAULT_CONFIG


@dataclass
class AudioMeta:
    duration_sec: float
    sample_rate: int
    channels: int
    source_format: str


def to_wav(input_path: Path, output_path: Path, sample_rate: int = DEFAULT_CONFIG.sample_rate) -> AudioMeta:
    """임의 포맷(mp3/m4a/...)을 표준 wav(stereo, sample_rate)로 변환해 저장한다."""
    audio = AudioSegment.from_file(input_path)
    audio = audio.set_frame_rate(sample_rate).set_channels(2)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    audio.export(output_path, format="wav")
    return AudioMeta(
        duration_sec=len(audio) / 1000.0,
        sample_rate=sample_rate,
        channels=2,
        source_format=input_path.suffix.lstrip(".").lower(),
    )


def read_wav(path: Path):
    """wav 파일을 (samples, sample_rate)로 읽는다. numpy 배열 반환."""
    data, sr = sf.read(path, always_2d=False)
    return data, sr


def write_wav(path: Path, data, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, data, sample_rate)
