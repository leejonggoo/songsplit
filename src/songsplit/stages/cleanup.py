"""[2b] Cleanup 스테이지: 분리된 스템 wav에서 잡음/다른 악기 번짐(bleed)/불필요한
대역을 제거해 01b_cleanup/{stem}.wav로 저장한다. 원본 스템은 그대로 둔다.

STFT 영역에서 네 가지 마스크를 곱해 적용한다.
  1. bleed   : 다른 스템들과의 에너지 비율(Wiener식)이 낮은 bin = 이 악기 소리가 아닌 것으로 보고 억제
               (스템이 2개 이상일 때만 동작)
  2. noise   : 스템별 주파수 bin의 잡음 바닥(낮은 percentile)을 추정해 그 근처의 성분을 억제(spectral gate)
  3. band    : 악기별로 나올 수 없는 대역(예: 베이스의 고음역, 보컬의 초저역) 차단
  4. silence : 거의 무음인 구간의 잔여 소음을 0으로 페이드
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import librosa
import numpy as np
from scipy.ndimage import uniform_filter

from songsplit.core.audio_io import read_wav, write_wav
from songsplit.pipeline.job import Job, Stage, StageStatus

N_FFT = 2048
HOP = 512
EPS = 1e-10

# 악기별 허용 대역(Hz). 없는 스템은 대역 제한을 하지 않는다.
BAND_LIMITS: dict[str, tuple[float, float]] = {
    "vocals": (70.0, 16000.0),
    "bass": (25.0, 4000.0),
    "guitar": (70.0, 10000.0),
    "piano": (25.0, 12000.0),
}


@dataclass(frozen=True)
class CleanupParams:
    bleed_strength: float = 0.5  # 0~1, 0이면 bleed 제거 안 함
    noise_reduction: float = 0.5  # 0~1, 0이면 spectral gate 안 함
    band_limit: bool = True
    silence_gate_db: float | None = -55.0  # 스템 피크 대비 dB. None이면 사용 안 함


def _smoothstep(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    t = np.clip((x - lo) / max(hi - lo, EPS), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _stft_channels(data: np.ndarray) -> np.ndarray:
    """(samples,) 또는 (samples, ch) -> (ch, freq, frames) complex64"""
    if data.ndim == 1:
        data = data[:, None]
    return np.stack(
        [librosa.stft(data[:, c].astype(np.float32), n_fft=N_FFT, hop_length=HOP) for c in range(data.shape[1])]
    )


def _mono_mag(spec: np.ndarray) -> np.ndarray:
    return np.abs(spec).mean(axis=0).astype(np.float32)


def bleed_mask(own_mag: np.ndarray, total_power: np.ndarray, strength: float) -> np.ndarray:
    ratio = own_mag**2 / (total_power + EPS)
    lo = 0.25 * strength
    return _smoothstep(ratio, lo, lo + 0.35)


def noise_gate_mask(mag: np.ndarray, reduction: float) -> np.ndarray:
    # bin별 잡음 바닥: 가장 조용한 10% 프레임의 크기. 그 몇 배 이하는 잡음으로 간주.
    floor = np.percentile(mag, 10, axis=1, keepdims=True)
    thresh_ratio = 1.0 + 2.0 * reduction
    mask = _smoothstep(mag / (floor + EPS), thresh_ratio, thresh_ratio * 2.0)
    # musical noise(깜빡이는 잔여음) 방지를 위해 시간/주파수로 부드럽게
    return uniform_filter(mask, size=(3, 5))


def band_mask(stem: str, sample_rate: int, n_bins: int) -> np.ndarray:
    limits = BAND_LIMITS.get(stem)
    if limits is None:
        return np.ones((n_bins, 1), dtype=np.float32)
    freqs = np.linspace(0, sample_rate / 2, n_bins)
    lo, hi = limits
    low = _smoothstep(freqs, lo * 0.5, lo)
    high = 1.0 - _smoothstep(freqs, hi, hi * 1.25)
    return (low * high)[:, None].astype(np.float32)


def silence_mask(signal: np.ndarray, gate_db: float, n_frames: int) -> np.ndarray:
    mono = np.abs(signal).max(axis=1) if signal.ndim == 2 else np.abs(signal)
    rms = librosa.feature.rms(y=mono, frame_length=N_FFT, hop_length=HOP)[0][:n_frames]
    thresh = max(float(np.abs(mono).max()), EPS) * 10 ** (gate_db / 20)
    mask = _smoothstep(rms, thresh * 0.7, thresh * 1.4)
    mask = uniform_filter(mask, size=9)
    if len(mask) < n_frames:
        mask = np.pad(mask, (0, n_frames - len(mask)), constant_values=1.0)
    return mask[None, :].astype(np.float32)


def clean_stems(
    stems: dict[str, np.ndarray], sample_rate: int, params: CleanupParams
) -> dict[str, np.ndarray]:
    """{스템: 샘플배열} -> 정리된 {스템: 샘플배열}. 순수 함수(파일 I/O 없음)."""
    specs = {name: _stft_channels(data) for name, data in stems.items()}
    mags = {name: _mono_mag(spec) for name, spec in specs.items()}
    total_power = sum(m**2 for m in mags.values()) if len(mags) > 1 else None

    cleaned: dict[str, np.ndarray] = {}
    for name, data in stems.items():
        spec = specs[name]
        mag = mags[name]
        n_bins, n_frames = mag.shape
        mask = np.ones_like(mag)

        if total_power is not None and params.bleed_strength > 0:
            mask = mask * bleed_mask(mag, total_power, params.bleed_strength)
        if params.noise_reduction > 0:
            mask = mask * noise_gate_mask(mag, params.noise_reduction)
        if params.band_limit:
            mask = mask * band_mask(name, sample_rate, n_bins)
        if params.silence_gate_db is not None:
            mask = mask * silence_mask(data, params.silence_gate_db, n_frames)

        length = data.shape[0]
        channels = [
            librosa.istft(spec[c] * mask, hop_length=HOP, n_fft=N_FFT, length=length)
            for c in range(spec.shape[0])
        ]
        out = np.stack(channels, axis=1) if data.ndim == 2 else channels[0]
        peak = float(np.abs(out).max())
        if peak > 1.0:
            out = out / peak
        cleaned[name] = out.astype(np.float32)
    return cleaned


def _rms_db(x: np.ndarray) -> float:
    return float(20 * np.log10(np.sqrt(np.mean(np.square(x))) + EPS))


def cleanup_stems(
    job: Job, stem_wav_paths: dict[str, Path], params: CleanupParams | None = None
) -> dict[str, Path]:
    params = params or CleanupParams()
    param_dict = {**asdict(params), "stems": list(stem_wav_paths)}
    job.update_stage(Stage.CLEANUP, StageStatus.RUNNING, params=param_dict)
    try:
        stems: dict[str, np.ndarray] = {}
        sample_rate = None
        for name, path in stem_wav_paths.items():
            data, sr = read_wav(Path(path))
            if sample_rate not in (None, sr):
                raise ValueError("스템들의 샘플레이트가 서로 다릅니다.")
            sample_rate = sr
            stems[name] = data

        cleaned = clean_stems(stems, sample_rate, params)

        out_dir = job.stage_dir(Stage.CLEANUP)
        outputs: dict[str, str] = {}
        rms_change: dict[str, float] = {}
        for name, data in cleaned.items():
            out_path = out_dir / f"{name}.wav"
            write_wav(out_path, data, sample_rate)
            outputs[name] = str(out_path)
            rms_change[name] = round(_rms_db(data) - _rms_db(stems[name]), 2)
    except Exception as exc:
        job.update_stage(Stage.CLEANUP, StageStatus.ERROR, error=str(exc))
        raise

    job.update_stage(
        Stage.CLEANUP,
        StageStatus.DONE,
        params={**param_dict, "rms_change_db": rms_change},
        outputs=outputs,
    )
    return {name: Path(p) for name, p in outputs.items()}


def active_stem_paths(job: Job) -> dict[str, Path]:
    """다운스트림(분석/MIDI/내보내기)이 사용할 스템: 정리본이 있으면 그것, 없으면 분리 원본."""
    for stage in (Stage.CLEANUP, Stage.SEPARATION):
        entry = job.get_stage(stage)
        if entry and entry.get("status") == StageStatus.DONE.value:
            return {k: Path(v) for k, v in entry["outputs"].items()}
    return {}
