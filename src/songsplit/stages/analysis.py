"""[3] Analysis: 템포(BPM)/키·스케일 추정.

librosa.beat.beat_track으로 템포를, chroma + Krumhansl-Schmuckler 키 프로파일로
키/스케일을 추정한다. 결과는 confidence와 함께 반환되고, 오케스트레이션
레이어(analyze.py)가 analysis.json에 저장 + manifest를 갱신한다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import librosa
import numpy as np

from songsplit.stages.beatgrid import estimate_beat_grid

PITCH_CLASSES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Krumhansl-Schmuckler 키 프로파일 (C를 기준으로 한 12개 음의 상대적 중요도)
_MAJOR_PROFILE = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR_PROFILE = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


@dataclass
class AnalysisResult:
    tempo_bpm: float
    tempo_confidence: float
    key: str  # 예: "C"
    scale: str  # 예: "major", "minor"
    key_confidence: float
    is_user_edited: bool = False
    # 비트 그리드 (analysis.json/manifest에 이 필드가 없는 예전 job은 기본값으로 읽힌다)
    beats_per_bar: int = 4
    first_downbeat_sec: float = 0.0  # 곡 시작 이후 첫 마디 첫 박의 시각
    pickup_beats: int = 0
    bar_confidence: float = 0.0
    tempo_adjust: float = 1.0  # 템포 옥타브 보정 (0.5=beat_track이 잡은 템포의 절반으로 보정)
    anchor_start_sec: float | None = None
    anchor_end_sec: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AnalysisResult":
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


def _estimate_tempo(y: np.ndarray, sr: int) -> tuple[float, float]:
    onset_env = librosa.onset.onset_strength(y=y, sr=sr)
    if onset_env.size == 0 or not np.any(onset_env):
        return 120.0, 0.0

    tempo, _ = librosa.beat.beat_track(onset_envelope=onset_env, sr=sr)
    tempo = float(np.atleast_1d(tempo)[0])

    # 신뢰도 근사치: 추정 템포 주기에서의 자기상관 값 (박이 뚜렷할수록 1에 가까움)
    ac = librosa.autocorrelate(onset_env, max_size=len(onset_env))
    ac = ac / (ac[0] + 1e-9)
    frame_rate = sr / 512  # beat_track 기본 hop_length
    lag = int(round(60.0 / tempo * frame_rate)) if tempo > 0 else 0
    confidence = float(ac[lag]) if 0 < lag < len(ac) else 0.0
    return tempo, max(0.0, min(1.0, confidence))


def _estimate_key(y: np.ndarray, sr: int) -> tuple[str, str, float]:
    chroma = librosa.feature.chroma_stft(y=y, sr=sr)
    chroma_mean = chroma.mean(axis=1)

    scores: list[float] = []
    best_score = -np.inf
    best_key, best_scale = "C", "major"
    for shift in range(12):
        major_corr = np.corrcoef(np.roll(_MAJOR_PROFILE, shift), chroma_mean)[0, 1]
        minor_corr = np.corrcoef(np.roll(_MINOR_PROFILE, shift), chroma_mean)[0, 1]
        scores.extend([major_corr, minor_corr])
        if major_corr > best_score:
            best_score, best_key, best_scale = major_corr, PITCH_CLASSES[shift], "major"
        if minor_corr > best_score:
            best_score, best_key, best_scale = minor_corr, PITCH_CLASSES[shift], "minor"

    scores_arr = np.nan_to_num(np.array(scores))
    score_range = scores_arr.max() - scores_arr.min()
    confidence = float((best_score - scores_arr.mean()) / (score_range + 1e-9))
    return best_key, best_scale, max(0.0, min(1.0, confidence))


def analyze(wav_path: Path, beats_per_bar: int = 4, tempo_scale: float | None = None) -> AnalysisResult:
    y, sr = librosa.load(wav_path, sr=None, mono=True)
    tempo_bpm, tempo_confidence = _estimate_tempo(y, sr)
    key, scale, key_confidence = _estimate_key(y, sr)

    grid = estimate_beat_grid(y, sr, beats_per_bar, tempo_scale)
    grid_fields = {}
    if grid is not None:
        tempo_bpm = grid.tempo_bpm  # 이상치를 제거한 회귀로 정밀화된 템포
        grid_fields = {
            "beats_per_bar": grid.beats_per_bar,
            "first_downbeat_sec": grid.first_downbeat_sec,
            "pickup_beats": grid.pickup_beats,
            "bar_confidence": grid.bar_confidence,
            "tempo_adjust": grid.tempo_adjust,
            "anchor_start_sec": grid.anchor_start_sec,
            "anchor_end_sec": grid.anchor_end_sec,
        }
    return AnalysisResult(
        **grid_fields,
        tempo_bpm=round(tempo_bpm, 2),
        tempo_confidence=round(tempo_confidence, 3),
        key=key,
        scale=scale,
        key_confidence=round(key_confidence, 3),
    )
