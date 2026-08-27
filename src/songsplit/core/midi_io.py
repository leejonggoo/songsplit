"""pretty_midi 기반 MIDI 읽기/쓰기 래퍼."""

from __future__ import annotations

from pathlib import Path

import pretty_midi


def new_midi(initial_tempo: float = 120.0) -> pretty_midi.PrettyMIDI:
    return pretty_midi.PrettyMIDI(initial_tempo=initial_tempo)


def save_midi(midi: pretty_midi.PrettyMIDI, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    midi.write(str(path))


def load_midi(path: Path) -> pretty_midi.PrettyMIDI:
    return pretty_midi.PrettyMIDI(str(path))
