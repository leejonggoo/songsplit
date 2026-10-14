"""[3b] 비트 그리드 추정: 전체 템포/비트를 먼저 잡고, 곡 중간에서 가장 정확한 구간(anchor)을
기준으로 앞/뒤로 박자 그리드를 확장해 마디 시작(downbeat) 위치를 찾는다.

1. beat_track으로 전체 비트 시각을 구한다.
2. 이상치를 제거하는 선형 회귀(비트 번호 ↔ 시각)로 전체 박 간격(=템포)을 정밀화한다.
3. 슬라이딩 윈도우로 "비트가 뚜렷하고 간격이 가장 균일한" 구간을 anchor로 고른다.
4. anchor의 위상 + 전체 박 간격으로 그리드를 곡 처음부터 끝까지 확장한다 (역추적 포함).
5. anchor 주변에서 박 위치별(마디 내 1,2,3,4박) 강세(onset + 저음 onset)를 비교해
   가장 강한 박을 마디의 첫 박으로 정한다. 그 박의 앞쪽 박 수가 pickup(못갖춘마디) 박 수가 된다.
"""

from __future__ import annotations

from dataclasses import dataclass

import librosa
import numpy as np

HOP = 512
WINDOW_BEATS = 16
VOTE_MARGIN_BEATS = 16
MIN_BEATS_FOR_GRID = 8
# 박 간격으로 잡은 격자에서 짝/홀수 박의 강도 비가 이보다 크면, 그 격자는 실제 박의 8분음표(세분박)로 본다
SUBDIVISION_RATIO = 1.35
MIN_AUTO_HALVED_BPM = 50.0


@dataclass
class BeatGrid:
    tempo_bpm: float
    beats_per_bar: int
    first_downbeat_sec: float  # 곡 시작 이후 처음 나오는 마디 첫 박의 시각
    pickup_beats: int  # 첫 마디 앞의 못갖춘 박 수 (0 ~ beats_per_bar-1)
    bar_confidence: float  # 0~1, 마디 첫 박 판정의 확신도
    tempo_adjust: float  # 적용된 옥타브 보정 (1.0=없음, 0.5=절반으로, 2.0=두 배로)
    anchor_start_sec: float | None
    anchor_end_sec: float | None


def _robust_line_fit(k: np.ndarray, t: np.ndarray, period: float) -> tuple[float, float]:
    """t ≈ intercept + slope*k. 박 간격의 20% 넘게 벗어나는 비트는 제외하고 다시 맞춘다."""
    mask = np.ones(len(k), dtype=bool)
    slope, intercept = period, float(t[0])
    for _ in range(3):
        if mask.sum() < 4:
            break
        slope, intercept = np.polyfit(k[mask], t[mask], 1)
        mask = np.abs(t - (intercept + slope * k)) < 0.2 * period
    return float(slope), float(intercept)


def _normalize(x: np.ndarray) -> np.ndarray:
    peak = float(np.max(x)) if x.size else 0.0
    return x / peak if peak > 0 else x


def _strength_at(env: np.ndarray, times: np.ndarray, sr: int) -> np.ndarray:
    """각 시각 ±40ms 안의 onset 강도 최대값."""
    frames = librosa.time_to_frames(times, sr=sr, hop_length=HOP)
    radius = max(1, int(round(0.04 * sr / HOP)))
    out = np.zeros(len(times))
    for i, f in enumerate(frames):
        lo, hi = max(f - radius, 0), min(f + radius + 1, len(env))
        out[i] = env[lo:hi].max() if lo < hi else 0.0
    return out


def _find_anchor(beat_times: np.ndarray, strengths: np.ndarray, period: float) -> tuple[int, int]:
    """비트가 뚜렷하고(강도) 간격이 균일한(회귀 잔차) 윈도우의 [시작, 끝) 인덱스."""
    window = min(WINDOW_BEATS, len(beat_times))
    k = np.arange(window, dtype=float)
    best, best_score = (0, window), -1.0
    for start in range(0, len(beat_times) - window + 1):
        seg = beat_times[start : start + window]
        slope, intercept = np.polyfit(k, seg, 1)
        rms = float(np.sqrt(np.mean((seg - (intercept + slope * k)) ** 2)))
        score = float(strengths[start : start + window].mean()) / (1.0 + (rms / (0.03 * period)) ** 2)
        if score > best_score:
            best, best_score = (start, start + window), score
    return best


def _apply_octave(
    beat_times: np.ndarray, env: np.ndarray, sr: int, tempo_scale: float | None
) -> tuple[np.ndarray, float]:
    """beat_track은 실제 박의 2배(세분박)나 절반을 잡는 일이 흔하다(템포 옥타브 오류).

    tempo_scale=None이면 자동 판정한다: 짝/홀수 박의 강도 차가 뚜렷하면(대개 강박/약박 교대)
    지금 박은 세분박이므로 강한 쪽만 남겨 템포를 절반으로 한다.
    0.5/2.0이면 사용자가 지정한 대로 적용한다.
    """
    strengths = _strength_at(env, beat_times, sr)
    even, odd = float(strengths[0::2].mean()), float(strengths[1::2].mean())
    parity = 0 if even >= odd else 1

    if tempo_scale is None:
        ratio = max(even, odd) / (min(even, odd) + 1e-9)
        diffs = np.diff(beat_times)
        bpm = 60.0 / float(np.median(diffs))
        if ratio >= SUBDIVISION_RATIO and bpm / 2.0 >= MIN_AUTO_HALVED_BPM:
            return beat_times[parity::2], 0.5
        return beat_times, 1.0
    if tempo_scale == 0.5:
        return beat_times[parity::2], 0.5
    if tempo_scale == 2.0:
        mids = (beat_times[:-1] + beat_times[1:]) / 2.0
        return np.sort(np.concatenate([beat_times, mids])), 2.0
    return beat_times, 1.0


def estimate_beat_grid(
    y: np.ndarray, sr: int, beats_per_bar: int = 4, tempo_scale: float | None = None
) -> BeatGrid | None:
    """비트를 충분히 찾지 못하면 None. tempo_scale: None=자동 옥타브 판정, 0.5/2.0=수동 보정."""
    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    if onset_env.size == 0 or not np.any(onset_env):
        return None

    tempo, beat_frames = librosa.beat.beat_track(onset_envelope=onset_env, sr=sr, hop_length=HOP)
    beat_times = librosa.frames_to_time(beat_frames, sr=sr, hop_length=HOP)
    tempo = float(np.atleast_1d(tempo)[0])
    if len(beat_times) < MIN_BEATS_FOR_GRID or tempo <= 0:
        return None

    # 저음(킥/베이스) onset을 같이 써서 마디 첫 박의 강세를 더 잘 드러낸다
    low_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP, fmax=200.0)
    env = 0.5 * _normalize(onset_env) + 0.5 * _normalize(low_env[: len(onset_env)])

    beat_times, tempo_adjust = _apply_octave(beat_times, env, sr, tempo_scale)
    if len(beat_times) < MIN_BEATS_FOR_GRID:
        return None

    # beat_track의 템포는 프레임 단위로 양자화되어 있어 그대로 나누면 오차가 누적되므로,
    # 인접 비트 간격의 중앙값을 박 간격 초기값으로 쓰고 비트 번호도 간격 단위로 누적한다.
    diffs = np.diff(beat_times)
    period0 = float(np.median(diffs))
    k = np.concatenate([[0.0], np.cumsum(np.maximum(np.round(diffs / period0), 1.0))])
    period, intercept = _robust_line_fit(k, beat_times, period0)

    beat_strength = _strength_at(env, beat_times, sr)
    a0, a1 = _find_anchor(beat_times, beat_strength, period)
    k_anchor = k[a0:a1]
    phase = float(np.mean(beat_times[a0:a1] - k_anchor * period))  # anchor 기준 위상

    # anchor 기준으로 곡 전체 그리드 확장 (앞쪽은 역추적)
    duration = len(y) / sr
    k_min = int(np.ceil((-phase) / period - 0.05))
    k_max = int(np.floor((duration - phase) / period))
    grid_k = np.arange(k_min, k_max + 1)
    grid_times = np.clip(phase + grid_k * period, 0.0, None)
    grid_strength = _strength_at(env, grid_times, sr)

    # anchor 주변 구간에서 박 위치별 강세 투표 (템포 드리프트 영향을 줄이기 위해 먼저 구간을 제한)
    a_lo = int(np.floor(k_anchor[0])) - VOTE_MARGIN_BEATS
    a_hi = int(np.ceil(k_anchor[-1])) + VOTE_MARGIN_BEATS
    in_region = (grid_k >= a_lo) & (grid_k <= a_hi)
    slot = (grid_k - k_min) % beats_per_bar  # grid 처음 박 기준 마디 내 위치
    votes = np.array(
        [
            grid_strength[in_region & (slot == p)].mean() if np.any(in_region & (slot == p)) else 0.0
            for p in range(beats_per_bar)
        ]
    )
    best = int(np.argmax(votes))
    others = np.delete(votes, best)
    confidence = float(np.clip((votes[best] - others.mean()) / (votes[best] + 1e-9), 0.0, 1.0))

    return BeatGrid(
        tempo_bpm=round(60.0 / period, 2),
        beats_per_bar=beats_per_bar,
        first_downbeat_sec=round(float(grid_times[best]), 3),
        pickup_beats=best,
        bar_confidence=round(confidence, 3),
        tempo_adjust=tempo_adjust,
        anchor_start_sec=round(float(beat_times[a0]), 3),
        anchor_end_sec=round(float(beat_times[a1 - 1]), 3),
    )
