"""[4] Transcription (멜로디/화성 계열): basic-pitch로 note onset/pitch/velocity를
추출해 MIDI로 변환한다.

vocals/bass/other/guitar/piano 등 음정이 있는 스템에 사용. Analysis 단계에서
확정된 템포(사용자 수정값 포함)를 결과 MIDI의 tempo track에 반영한다.

쓸모없는 음을 줄이기 위해 (1) 스템별 음역/임계값으로 basic-pitch를 돌리고
(2) note_filter로 약한 음/배음/조각/동시발음을 후처리한다. strength(0~1)로 강도를 조절한다.
"""

from __future__ import annotations

from pathlib import Path

import pretty_midi
from basic_pitch.inference import predict

from songsplit.stages.transcription.note_filter import filter_notes

# 스템별 허용 음역(Hz). 이 밖의 음은 basic-pitch가 아예 후보로 만들지 않는다.
STEM_FREQ_RANGE_HZ: dict[str, tuple[float, float]] = {
    "vocals": (80.0, 1200.0),
    "bass": (30.0, 400.0),
    "guitar": (80.0, 1400.0),
    "piano": (27.0, 4200.0),
    "other": (60.0, 2000.0),
}


def transcribe_melodic(
    wav_path: Path, tempo_bpm: float, stem: str | None = None, strength: float = 0.5
) -> pretty_midi.PrettyMIDI:
    strength = min(max(strength, 0.0), 1.0)
    fmin, fmax = STEM_FREQ_RANGE_HZ.get(stem or "", (None, None))
    _, midi_data, _ = predict(
        str(wav_path),
        midi_tempo=tempo_bpm,
        onset_threshold=0.4 + 0.3 * strength,
        frame_threshold=0.2 + 0.2 * strength,
        minimum_note_length=100.0 + 120.0 * strength,
        minimum_frequency=fmin,
        maximum_frequency=fmax,
    )
    return filter_notes(midi_data, stem or "", strength)
