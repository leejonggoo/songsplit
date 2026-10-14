import numpy as np
import pytest
import soundfile as sf

from songsplit.stages.analysis import analyze
from songsplit.stages.beatgrid import estimate_beat_grid

SR = 22050


def _click_track(bpm: float, first_downbeat: float, bars: int, drift_start: float = 0.0) -> np.ndarray:
    """마디 첫 박은 강한 저음 킥, 나머지 박은 약한 킥. 곡 앞부분(first_downbeat 이전)은 약한 박만 있다."""
    period = 60.0 / bpm
    total = first_downbeat + bars * 4 * period + 1.0
    y = np.zeros(int(total * SR), dtype=np.float32)
    t = np.arange(int(0.12 * SR)) / SR
    kick = (np.sin(2 * np.pi * 70 * t) * np.exp(-t * 30)).astype(np.float32)
    n_before = int(round(first_downbeat / period))
    for i in range(-n_before, bars * 4):
        pos = first_downbeat + i * period
        amp = 1.0 if i % 4 == 0 else 0.45
        s = int(pos * SR)
        y[s : s + len(kick)] += amp * kick[: len(y) - s]
    return y


def test_finds_downbeat_with_pickup_beats():
    y = _click_track(bpm=120, first_downbeat=1.0, bars=16)

    grid = estimate_beat_grid(y, SR)

    assert grid is not None
    assert grid.tempo_bpm == pytest.approx(120, abs=2)
    assert grid.first_downbeat_sec == pytest.approx(1.0, abs=0.06)
    assert grid.pickup_beats == 2
    assert grid.anchor_start_sec is not None


def test_returns_none_for_silence():
    assert estimate_beat_grid(np.zeros(SR * 5, dtype=np.float32), SR) is None


def test_analyze_exposes_grid_fields(tmp_path):
    path = tmp_path / "click.wav"
    sf.write(path, _click_track(100, 0.0, 16), SR)

    result = analyze(path)

    assert result.first_downbeat_sec == pytest.approx(0.0, abs=0.06)
    assert result.beats_per_bar == 4


def test_octave_auto_halves_when_alternate_beats_differ():
    from songsplit.stages.beatgrid import HOP, _apply_octave

    frames_per_beat = 20
    env = np.zeros(frames_per_beat * 40 + 10)
    beat_frames = np.arange(40) * frames_per_beat + 2
    env[beat_frames] = np.where(np.arange(40) % 2 == 0, 1.0, 0.4)  # 강/약 교대 = 8분음표 세분박
    beat_times = beat_frames * HOP / SR

    halved, adjust = _apply_octave(beat_times, env, SR, None)
    assert adjust == 0.5 and len(halved) == 20 and halved[0] == beat_times[0]

    env[beat_frames] = 1.0  # 균일하면 그대로 둔다
    same, adjust = _apply_octave(beat_times, env, SR, None)
    assert adjust == 1.0 and len(same) == 40

    doubled, adjust = _apply_octave(beat_times, env, SR, 2.0)
    assert adjust == 2.0 and len(doubled) == 79
