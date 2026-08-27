"""[4] Transcription (멜로디/화성 계열): basic-pitch로 note onset/pitch/velocity를
추출해 MIDI로 변환한다.

vocals/bass/other/guitar/piano 등 음정이 있는 스템에 사용. Analysis 단계에서
확정된 템포(사용자 수정값 포함)를 결과 MIDI의 tempo track에 반영한다.
"""

from __future__ import annotations

from pathlib import Path

import pretty_midi
from basic_pitch.inference import predict


def transcribe_melodic(wav_path: Path, tempo_bpm: float) -> pretty_midi.PrettyMIDI:
    _, midi_data, _ = predict(str(wav_path), midi_tempo=tempo_bpm)
    return midi_data
