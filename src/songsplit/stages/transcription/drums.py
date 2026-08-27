"""[4] Transcription (드럼): onset detection + 스펙트럴 센트로이드 기반 분류로
킥/스네어/클로즈 하이햇 3종만 General MIDI 드럼 맵에 매핑한다.

정교한 드럼 채보는 어렵기 때문에 단순 휴리스틱으로 시작한다
(PLAN.md 리스크 섹션 참고 — 필요시 이후 개선).
"""

from __future__ import annotations

from pathlib import Path

import librosa
import numpy as np
import pretty_midi

KICK = 36
SNARE = 38
CLOSED_HIHAT = 42

_NOTE_DURATION_SEC = 0.1
_WINDOW_SEC = 0.05
_KICK_CENTROID_HZ = 400
_SNARE_CENTROID_HZ = 2000


def _classify(centroid_hz: float) -> int:
    if centroid_hz < _KICK_CENTROID_HZ:
        return KICK
    if centroid_hz < _SNARE_CENTROID_HZ:
        return SNARE
    return CLOSED_HIHAT


def transcribe_drums(wav_path: Path, tempo_bpm: float) -> pretty_midi.PrettyMIDI:
    y, sr = librosa.load(wav_path, sr=None, mono=True)
    onset_times = librosa.onset.onset_detect(y=y, sr=sr, units="time")

    midi = pretty_midi.PrettyMIDI(initial_tempo=tempo_bpm)
    drum = pretty_midi.Instrument(program=0, is_drum=True, name="drums")

    window_samples = int(_WINDOW_SEC * sr)
    for onset in onset_times:
        start_sample = int(onset * sr)
        end_sample = min(start_sample + window_samples, len(y))
        segment = y[start_sample:end_sample]
        if segment.size < 2:
            continue

        rms = float(np.sqrt(np.mean(segment**2)))
        n_fft = min(2048, len(segment))
        centroid = float(librosa.feature.spectral_centroid(y=segment, sr=sr, n_fft=n_fft).mean())

        drum.notes.append(
            pretty_midi.Note(
                velocity=int(np.clip(60 + rms * 400, 40, 127)),
                pitch=_classify(centroid),
                start=float(onset),
                end=float(onset) + _NOTE_DURATION_SEC,
            )
        )

    midi.instruments.append(drum)
    return midi
