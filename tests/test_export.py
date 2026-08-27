import zipfile
from pathlib import Path

import pretty_midi
import pytest

from songsplit.pipeline.config import AppConfig
from songsplit.pipeline.job import Job, Stage, StageStatus
from songsplit.stages.export import merge_midi, build_export


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return AppConfig(jobs_dir=tmp_path / "jobs")


def _fake_wav(path: Path, content: bytes = b"WAVDATA") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _fake_midi(path: Path, pitch: int, is_drum: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    midi = pretty_midi.PrettyMIDI(initial_tempo=120.0)
    inst = pretty_midi.Instrument(program=0, is_drum=is_drum)
    inst.notes.append(pretty_midi.Note(velocity=100, pitch=pitch, start=0.0, end=0.5))
    midi.instruments.append(inst)
    midi.write(str(path))
    return path


def test_merge_midi_combines_stems_as_separate_named_tracks(tmp_path: Path):
    paths = {
        "vocals": _fake_midi(tmp_path / "vocals.mid", pitch=60),
        "drums": _fake_midi(tmp_path / "drums.mid", pitch=36, is_drum=True),
    }

    merged = merge_midi(paths)

    assert len(merged.instruments) == 2
    by_name = {inst.name: inst for inst in merged.instruments}
    assert by_name["vocals"].notes[0].pitch == 60
    assert by_name["vocals"].is_drum is False
    assert by_name["drums"].notes[0].pitch == 36
    assert by_name["drums"].is_drum is True


def test_merge_midi_raises_on_empty():
    with pytest.raises(RuntimeError, match="합칠"):
        merge_midi({})


def test_build_export_bundles_wav_midi_and_merged(config: AppConfig):
    job = Job.create("song.mp3", config=config)

    sep_dir = job.stage_dir(Stage.SEPARATION)
    vocals_wav = _fake_wav(sep_dir / "vocals.wav")
    job.update_stage(Stage.SEPARATION, StageStatus.DONE, outputs={"vocals": str(vocals_wav)})

    midi_dir = job.stage_dir(Stage.MIDI)
    vocals_mid = _fake_midi(midi_dir / "vocals.mid", pitch=60)
    job.update_stage(Stage.MIDI, StageStatus.DONE, outputs={"vocals": str(vocals_mid)})

    zip_path = build_export(job)

    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        assert names == {"stems/vocals.wav", "midi/vocals.mid", "merged.mid"}
        assert zf.read("stems/vocals.wav") == b"WAVDATA"

    entry = job.get_stage(Stage.EXPORT)
    assert entry["status"] == StageStatus.DONE.value
    merged_path = Path(entry["outputs"]["merged_midi_path"])
    assert merged_path.exists()
    merged = pretty_midi.PrettyMIDI(str(merged_path))
    assert len(merged.instruments) == 1
    assert merged.instruments[0].name == "vocals"
    assert merged.instruments[0].notes[0].pitch == 60


def test_build_export_prefers_edited_midi_in_zip_and_merged(config: AppConfig, tmp_path: Path):
    job = Job.create("song.mp3", config=config)

    sep_dir = job.stage_dir(Stage.SEPARATION)
    vocals_wav = _fake_wav(sep_dir / "vocals.wav")
    job.update_stage(Stage.SEPARATION, StageStatus.DONE, outputs={"vocals": str(vocals_wav)})

    midi_dir = job.stage_dir(Stage.MIDI)
    original_mid = _fake_midi(midi_dir / "vocals.mid", pitch=60)
    job.update_stage(Stage.MIDI, StageStatus.DONE, outputs={"vocals": str(original_mid)})

    edited_dir = job.stage_dir(Stage.EDITED_MIDI)
    edited_mid = _fake_midi(edited_dir / "vocals.mid", pitch=67)
    job.update_stage(Stage.EDITED_MIDI, StageStatus.DONE, outputs={"vocals": str(edited_mid)})

    zip_path = build_export(job)

    extract_dir = tmp_path / "extracted"
    with zipfile.ZipFile(zip_path) as zf:
        zf.extract("midi/vocals.mid", extract_dir)
    extracted = pretty_midi.PrettyMIDI(str(extract_dir / "midi" / "vocals.mid"))
    assert extracted.instruments[0].notes[0].pitch == 67

    entry = job.get_stage(Stage.EXPORT)
    assert entry["outputs"]["used_edited_midi"] == ["vocals"]
    merged = pretty_midi.PrettyMIDI(entry["outputs"]["merged_midi_path"])
    assert merged.instruments[0].notes[0].pitch == 67  # 후처리(edited) 버전이 합쳐짐


def test_build_export_without_any_output_raises(config: AppConfig):
    job = Job.create("song.mp3", config=config)
    with pytest.raises(RuntimeError, match="내보낼"):
        build_export(job)

    entry = job.get_stage(Stage.EXPORT)
    assert entry["status"] == StageStatus.ERROR.value
