from pathlib import Path

import pretty_midi
import pytest

from songsplit.pipeline.config import AppConfig
from songsplit.pipeline.job import Job, Stage, StageStatus
from songsplit.stages.postprocess import (
    apply_postprocess,
    quantize,
    snap_to_scale,
)


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return AppConfig(jobs_dir=tmp_path / "jobs")


def _midi_with_notes(notes: list[tuple[float, float, int]], is_drum: bool = False) -> pretty_midi.PrettyMIDI:
    midi = pretty_midi.PrettyMIDI(initial_tempo=120.0)
    inst = pretty_midi.Instrument(program=0, is_drum=is_drum)
    for start, end, pitch in notes:
        inst.notes.append(pretty_midi.Note(velocity=100, pitch=pitch, start=start, end=end))
    midi.instruments.append(inst)
    return midi


def test_quantize_snaps_start_to_grid():
    # 120 BPM, 1/16 그리드 = 0.125초 간격
    midi = _midi_with_notes([(0.11, 0.4, 60), (0.61, 0.9, 62)])

    result = quantize(midi, tempo_bpm=120.0, grid="1/16")

    starts = sorted(n.start for n in result.instruments[0].notes)
    assert starts == pytest.approx([0.125, 0.625], abs=1e-9)
    # 노트 길이는 유지
    durations = sorted(n.end - n.start for n in result.instruments[0].notes)
    assert durations == pytest.approx([0.29, 0.29], abs=1e-6)


def test_quantize_rejects_unknown_grid():
    midi = _midi_with_notes([(0.0, 0.1, 60)])
    with pytest.raises(ValueError, match="grid"):
        quantize(midi, tempo_bpm=120.0, grid="1/3")


def test_snap_to_scale_shifts_out_of_scale_note():
    # C major = {0,2,4,5,7,9,11}; pitch 61 (C#) 은 스케일 밖 -> 가장 가까운 60(C) 또는 62(D)
    midi = _midi_with_notes([(0.0, 0.5, 61)])

    result = snap_to_scale(midi, key="C", scale="major")

    new_pitch = result.instruments[0].notes[0].pitch
    assert new_pitch % 12 in {0, 2, 4, 5, 7, 9, 11}
    assert abs(new_pitch - 61) == 1


def test_snap_to_scale_leaves_in_scale_note_untouched():
    midi = _midi_with_notes([(0.0, 0.5, 60)])  # C, C major 스케일 안
    result = snap_to_scale(midi, key="C", scale="major")
    assert result.instruments[0].notes[0].pitch == 60


def test_snap_to_scale_ignores_drum_track():
    midi = _midi_with_notes([(0.0, 0.1, 61)], is_drum=True)
    result = snap_to_scale(midi, key="C", scale="major")
    assert result.instruments[0].notes[0].pitch == 61


def test_apply_postprocess_updates_manifest_and_files(config: AppConfig, tmp_path: Path):
    job = Job.create("song.mp3", config=config)

    midi_dir = job.stage_dir(Stage.MIDI)
    src_midi = _midi_with_notes([(0.11, 0.4, 61)])
    src_path = midi_dir / "vocals.mid"
    src_midi.write(str(src_path))
    job.update_stage(
        Stage.MIDI,
        StageStatus.DONE,
        outputs={"vocals": str(src_path)},
    )

    produced = apply_postprocess(job, tempo_bpm=120.0, key="C", scale="major", grid="1/16", snap_scale=True)

    assert set(produced) == {"vocals"}
    out_path = produced["vocals"]
    assert out_path.exists()
    assert out_path.parent.name == Stage.EDITED_MIDI.value

    result_midi = pretty_midi.PrettyMIDI(str(out_path))
    note = result_midi.instruments[0].notes[0]
    assert note.start == pytest.approx(0.125, abs=1e-9)  # 퀀타이즈 적용
    assert note.pitch % 12 in {0, 2, 4, 5, 7, 9, 11}  # 스케일 스냅 적용

    entry = job.get_stage(Stage.EDITED_MIDI)
    assert entry["status"] == StageStatus.DONE.value
    assert entry["params"]["snap_scale"] is True


def test_apply_postprocess_without_midi_stage_raises(config: AppConfig):
    job = Job.create("song.mp3", config=config)
    with pytest.raises(RuntimeError, match="MIDI"):
        apply_postprocess(job, tempo_bpm=120.0, key="C", scale="major")
