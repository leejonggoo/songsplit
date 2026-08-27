from pathlib import Path

import pretty_midi
import pytest

from songsplit.pipeline.config import AppConfig
from songsplit.pipeline.job import Job, Stage, StageStatus
from songsplit.stages.transcribe import transcribe_all
from songsplit.stages.transcription.drums import transcribe_drums
from songsplit.stages.transcription.melodic import transcribe_melodic

FIXTURE = Path(__file__).parent / "fixtures" / "tone.wav"


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return AppConfig(jobs_dir=tmp_path / "jobs")


def test_transcribe_melodic_returns_midi():
    midi = transcribe_melodic(FIXTURE, tempo_bpm=120.0)
    assert isinstance(midi, pretty_midi.PrettyMIDI)


def test_transcribe_drums_returns_drum_track():
    midi = transcribe_drums(FIXTURE, tempo_bpm=120.0)
    assert isinstance(midi, pretty_midi.PrettyMIDI)
    assert len(midi.instruments) == 1
    assert midi.instruments[0].is_drum is True


def test_transcribe_all_updates_manifest(config: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # 실제 basic-pitch/librosa 추론은 느리므로 오케스트레이션 로직만 빠른 가짜로 검증
    def fake_melodic(wav_path, tempo_bpm):
        m = pretty_midi.PrettyMIDI(initial_tempo=tempo_bpm)
        m.instruments.append(pretty_midi.Instrument(program=0))
        return m

    def fake_drums(wav_path, tempo_bpm):
        m = pretty_midi.PrettyMIDI(initial_tempo=tempo_bpm)
        m.instruments.append(pretty_midi.Instrument(program=0, is_drum=True))
        return m

    monkeypatch.setattr("songsplit.stages.transcribe.transcribe_melodic", fake_melodic)
    monkeypatch.setattr("songsplit.stages.transcribe.transcribe_drums", fake_drums)

    job = Job.create("song.mp3", config=config)
    vocals_wav = tmp_path / "vocals.wav"
    drums_wav = tmp_path / "drums.wav"
    vocals_wav.write_bytes(b"fake")
    drums_wav.write_bytes(b"fake")

    result = transcribe_all(job, {"vocals": vocals_wav, "drums": drums_wav}, tempo_bpm=128.0)

    assert set(result) == {"vocals", "drums"}
    for path in result.values():
        assert path.exists()

    entry = job.get_stage(Stage.MIDI)
    assert entry["status"] == StageStatus.DONE.value
    assert entry["params"]["tempo_bpm"] == 128.0
    assert Path(entry["outputs"]["drums"]).name == "drums.mid"
