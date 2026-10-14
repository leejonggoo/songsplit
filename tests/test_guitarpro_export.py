from pathlib import Path

import guitarpro as gp
import pretty_midi
import pytest

from songsplit.core.midi_io import save_midi
from songsplit.pipeline.config import AppConfig
from songsplit.pipeline.job import Job, Stage, StageStatus
from songsplit.stages.guitarpro_export import (
    assign_strings,
    export_guitarpro,
    midi_to_guitarpro,
    split_length,
    write_guitarpro,
)


def _midi(notes, drum=False):
    m = pretty_midi.PrettyMIDI(initial_tempo=120)
    inst = pretty_midi.Instrument(program=0, is_drum=drum)
    inst.notes = [pretty_midi.Note(velocity=90, pitch=p, start=s, end=e) for p, s, e in notes]
    m.instruments.append(inst)
    return m


def test_split_length_uses_standard_durations():
    assert split_length(16) == [16]
    assert split_length(5) == [4, 1]
    assert split_length(7) == [6, 1]
    assert sum(split_length(11)) == 11


def test_assign_strings_prefers_low_frets_and_distinct_strings():
    result = assign_strings([40, 64, 52], [64, 59, 55, 50, 45, 40])
    assert result[64] == (1, 0) and result[40] == (6, 0)
    assert len({string for string, _ in result.values()}) == 3
    assert assign_strings([10], [64, 59, 55, 50, 45, 40]) == {}  # 연주 불가


def test_roundtrip_notes_and_bars(tmp_path: Path):
    # 120 BPM: 한 박 0.5초, 한 마디 2초. 두 번째 마디 첫 박에 E4(64), 첫 마디에 E2(40)
    guitar = _midi([(40, 0.0, 0.5), (64, 2.0, 3.0)])
    drums = _midi([(36, 0.0, 0.1), (38, 0.5, 0.6)], drum=True)
    song = midi_to_guitarpro({"guitar": guitar, "drums": drums}, tempo_bpm=120, title="t")
    path = write_guitarpro(song, tmp_path / "t.gp5")

    loaded = gp.parse(str(path))
    assert loaded.tempo == 120
    assert len(loaded.tracks) == 2 and loaded.tracks[1].isPercussionTrack
    g = loaded.tracks[0]
    assert len(g.measures) >= 2
    first = [b for b in g.measures[0].voices[0].beats if b.notes]
    assert (first[0].notes[0].string, first[0].notes[0].value) == (6, 0)
    second = [b for b in g.measures[1].voices[0].beats if b.notes]
    assert (second[0].notes[0].string, second[0].notes[0].value) == (1, 0)
    # 각 마디가 정확히 4/4을 채운다
    for measure in g.measures:
        assert sum(b.duration.time for b in measure.voices[0].beats) == gp.Duration.quarterTime * 4


def test_first_downbeat_places_note_on_bar_line(tmp_path: Path):
    # 첫 마디 시작이 1.5초 -> 그 시각의 음은 마디 첫 박이어야 한다 (앞 못갖춘마디가 생김)
    midi = _midi([(64, 1.5, 2.0)])
    song = midi_to_guitarpro({"guitar": midi}, tempo_bpm=120, first_downbeat_sec=1.5)
    loaded = gp.parse(str(write_guitarpro(song, tmp_path / "a.gp5")))
    measures = loaded.tracks[0].measures
    idx, measure = next((i, m) for i, m in enumerate(measures) if any(b.notes for b in m.voices[0].beats))
    assert idx == 1
    assert measure.voices[0].beats[0].notes


def test_export_guitarpro_job(tmp_path: Path):
    job = Job.create("노래.mp3", AppConfig(jobs_dir=tmp_path / "jobs"))
    mid = job.stage_dir(Stage.MIDI) / "guitar.mid"
    save_midi(_midi([(64, 0.0, 1.0)]), mid)
    job.update_stage(Stage.MIDI, StageStatus.DONE, outputs={"guitar": str(mid)})

    out = export_guitarpro(job)

    assert out.exists() and job.is_stage_done(Stage.GUITARPRO)
    assert len(gp.parse(str(out)).tracks) == 1
