from pathlib import Path

import numpy as np

from songsplit.core.audio_io import read_wav, write_wav
from songsplit.pipeline.config import AppConfig
from songsplit.pipeline.job import Job, Stage, StageStatus
from songsplit.stages.cleanup import CleanupParams, active_stem_paths, clean_stems, cleanup_stems

SR = 22050


def _tone(freq: float, amp: float, seconds: float = 2.0) -> np.ndarray:
    t = np.arange(int(SR * seconds)) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _band_energy(x: np.ndarray, freq: float) -> float:
    spec = np.abs(np.fft.rfft(x))
    bin_ = int(freq * len(x) / SR)
    return float(spec[max(bin_ - 3, 0) : bin_ + 4].sum())


def test_bleed_of_other_stem_is_suppressed():
    bass = _tone(110, 0.5) + _tone(1000, 0.01)  # 1kHz는 다른 악기 번짐
    other = _tone(1000, 0.5)
    params = CleanupParams(noise_reduction=0.0, silence_gate_db=None)
    out = clean_stems({"bass": bass, "other": other}, SR, params)

    assert _band_energy(out["bass"], 1000) < 0.3 * _band_energy(bass, 1000)
    assert _band_energy(out["bass"], 110) > 0.8 * _band_energy(bass, 110)


def test_band_limit_removes_out_of_range_content():
    bass = _tone(100, 0.4) + _tone(8000, 0.2)
    params = CleanupParams(bleed_strength=0.0, noise_reduction=0.0, silence_gate_db=None)
    out = clean_stems({"bass": bass}, SR, params)

    assert _band_energy(out["bass"], 8000) < 0.05 * _band_energy(bass, 8000)
    assert _band_energy(out["bass"], 100) > 0.8 * _band_energy(bass, 100)


def test_noise_floor_is_reduced_but_signal_kept():
    rng = np.random.default_rng(0)
    noise = (rng.standard_normal(SR * 4) * 0.01).astype(np.float32)
    sig = np.zeros_like(noise)
    sig[SR : 3 * SR] = _tone(440, 0.5)[: 2 * SR]
    x = sig + noise
    params = CleanupParams(bleed_strength=0.0, band_limit=False, silence_gate_db=None, noise_reduction=0.7)
    out = clean_stems({"other": x}, SR, params)["other"]

    assert np.sqrt(np.mean(out[: SR // 2] ** 2)) < 0.5 * np.sqrt(np.mean(x[: SR // 2] ** 2))
    assert _band_energy(out[SR : 3 * SR], 440) > 0.8 * _band_energy(x[SR : 3 * SR], 440)


def test_stereo_shape_preserved_and_job_integration(tmp_path: Path):
    job = Job.create("song.mp3", AppConfig(jobs_dir=tmp_path / "jobs"))
    paths = {}
    for name, f in {"bass": 110, "other": 1000}.items():
        p = job.stage_dir(Stage.SEPARATION) / f"{name}.wav"
        write_wav(p, np.stack([_tone(f, 0.4)] * 2, axis=1), SR)
        paths[name] = p
    job.update_stage(Stage.SEPARATION, StageStatus.DONE, outputs={k: str(v) for k, v in paths.items()})
    assert active_stem_paths(job) == paths

    out = cleanup_stems(job, paths)

    assert job.is_stage_done(Stage.CLEANUP)
    data, sr = read_wav(out["bass"])
    assert data.shape == (SR * 2, 2) and sr == SR
    assert active_stem_paths(job) == out
