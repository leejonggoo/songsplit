"""[5] Post-process: 사용자가 확정한 템포/스케일 기준으로 노트 타이밍을 퀀타이즈하고,
(옵션) 스케일 밖 음을 가장 가까운 스케일 음으로 스냅한다.

이미 생성된 MIDI(03_midi/*)를 무거운 AI 재추론 없이 빠르게 다시 조정하는 것이 목적이며,
결과는 04_edited_midi/*에 저장된다.
"""

from __future__ import annotations

from pathlib import Path

import pretty_midi

from songsplit.core.midi_io import load_midi, save_midi
from songsplit.pipeline.job import Job, Stage, StageStatus
from songsplit.stages.analysis import PITCH_CLASSES

GRID_OPTIONS = ["1/4", "1/8", "1/16", "1/32"]
_GRID_DENOMINATORS = {"1/4": 4, "1/8": 8, "1/16": 16, "1/32": 32}
_MAJOR_SCALE = [0, 2, 4, 5, 7, 9, 11]
_MINOR_SCALE = [0, 2, 3, 5, 7, 8, 10]


def _grid_seconds(tempo_bpm: float, grid: str) -> float:
    try:
        denom = _GRID_DENOMINATORS[grid]
    except KeyError:
        raise ValueError(f"지원하지 않는 grid: {grid}. 사용 가능: {sorted(_GRID_DENOMINATORS)}") from None
    quarter_note_sec = 60.0 / tempo_bpm
    return quarter_note_sec * (4.0 / denom)


def quantize(midi: pretty_midi.PrettyMIDI, tempo_bpm: float, grid: str = "1/16") -> pretty_midi.PrettyMIDI:
    """노트 시작 시각을 템포 그리드에 스냅한다 (노트 길이는 유지)."""
    grid_seconds = _grid_seconds(tempo_bpm, grid)

    out = pretty_midi.PrettyMIDI(initial_tempo=tempo_bpm)
    for inst in midi.instruments:
        new_inst = pretty_midi.Instrument(program=inst.program, is_drum=inst.is_drum, name=inst.name)
        for note in inst.notes:
            duration = max(note.end - note.start, 0.01)
            new_start = round(note.start / grid_seconds) * grid_seconds
            new_inst.notes.append(
                pretty_midi.Note(
                    velocity=note.velocity, pitch=note.pitch, start=new_start, end=new_start + duration
                )
            )
        out.instruments.append(new_inst)
    return out


def _nearest_in_scale_pitch(pitch: int, allowed_pitch_classes: set[int]) -> int:
    if pitch % 12 in allowed_pitch_classes:
        return pitch
    for offset in range(1, 7):
        if (pitch - offset) % 12 in allowed_pitch_classes:
            return pitch - offset
        if (pitch + offset) % 12 in allowed_pitch_classes:
            return pitch + offset
    return pitch  # 7음 스케일이면 offset<=6에서 항상 찾아지므로 이론상 도달하지 않음


def snap_to_scale(midi: pretty_midi.PrettyMIDI, key: str, scale: str) -> pretty_midi.PrettyMIDI:
    """스케일 밖 음을 가장 가까운 스케일 음으로 스냅한다.

    드럼 트랙(is_drum)은 피치가 음높이가 아니라 GM 드럼 맵상의 악기를 의미하므로 건드리지 않는다.
    """
    root = PITCH_CLASSES.index(key)
    intervals = _MAJOR_SCALE if scale == "major" else _MINOR_SCALE
    allowed = {(root + i) % 12 for i in intervals}

    tempi = midi.get_tempo_changes()[1]
    initial_tempo = float(tempi[0]) if tempi.size else 120.0

    out = pretty_midi.PrettyMIDI(initial_tempo=initial_tempo)
    for inst in midi.instruments:
        new_inst = pretty_midi.Instrument(program=inst.program, is_drum=inst.is_drum, name=inst.name)
        for note in inst.notes:
            pitch = note.pitch if inst.is_drum else _nearest_in_scale_pitch(note.pitch, allowed)
            new_inst.notes.append(
                pretty_midi.Note(velocity=note.velocity, pitch=pitch, start=note.start, end=note.end)
            )
        out.instruments.append(new_inst)
    return out


def apply_postprocess(
    job: Job,
    tempo_bpm: float,
    key: str,
    scale: str,
    grid: str = "1/16",
    snap_scale: bool = False,
) -> dict[str, Path]:
    """03_midi/*의 모든 스템에 퀀타이즈(+옵션 스케일 스냅)를 적용해 04_edited_midi/*에
    저장하고 job manifest를 갱신한다."""
    midi_entry = job.get_stage(Stage.MIDI)
    if not midi_entry or midi_entry.get("status") != StageStatus.DONE.value:
        raise RuntimeError("먼저 [4] MIDI 변환 단계를 완료해야 합니다")
    stem_paths = {stem: Path(path) for stem, path in midi_entry["outputs"].items()}

    job.update_stage(
        Stage.EDITED_MIDI,
        StageStatus.RUNNING,
        params={"tempo_bpm": tempo_bpm, "key": key, "scale": scale, "grid": grid, "snap_scale": snap_scale},
    )
    try:
        produced: dict[str, Path] = {}
        for stem, src_path in stem_paths.items():
            midi = load_midi(src_path)
            midi = quantize(midi, tempo_bpm, grid)
            if snap_scale:
                midi = snap_to_scale(midi, key, scale)
            out_path = job.stage_dir(Stage.EDITED_MIDI) / f"{stem}.mid"
            save_midi(midi, out_path)
            produced[stem] = out_path
    except Exception as exc:
        job.update_stage(Stage.EDITED_MIDI, StageStatus.ERROR, error=str(exc))
        raise

    job.update_stage(
        Stage.EDITED_MIDI,
        StageStatus.DONE,
        params={"tempo_bpm": tempo_bpm, "key": key, "scale": scale, "grid": grid, "snap_scale": snap_scale},
        outputs={stem: str(path) for stem, path in produced.items()},
    )
    return produced
