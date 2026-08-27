from pathlib import Path

import pytest

from songsplit.stages.separation import get_engine
from songsplit.stages.separation.spleeter_engine import STEM_CONFIGS, SpleeterEngine


def test_available_stems_per_config():
    assert SpleeterEngine(config="2stems").available_stems() == ["vocals", "accompaniment"]
    assert SpleeterEngine(config="4stems").available_stems() == ["vocals", "drums", "bass", "other"]
    assert SpleeterEngine(config="5stems").available_stems() == ["vocals", "drums", "bass", "piano", "other"]
    assert set(STEM_CONFIGS) == {"2stems", "4stems", "5stems"}


def test_rejects_unknown_config():
    with pytest.raises(ValueError, match="config"):
        SpleeterEngine(config="7stems")


def test_rejects_unsupported_stem(tmp_path: Path):
    engine = SpleeterEngine(config="2stems")
    with pytest.raises(ValueError, match="drums"):
        engine.separate(tmp_path / "in.wav", ["drums"], tmp_path / "out")


def test_registry_returns_spleeter_engine():
    engine = get_engine("spleeter", config="4stems")
    assert isinstance(engine, SpleeterEngine)
    assert engine.available_stems() == ["vocals", "drums", "bass", "other"]


def test_separate_raises_helpful_error_when_not_installed(tmp_path: Path):
    # 이 프로젝트의 기본(Windows) venv에는 spleeter가 실제로 설치되어 있지 않으므로
    # 친절한 안내 메시지로 실패하는지를 실제 서브프로세스 호출로 검증한다.
    engine = SpleeterEngine(config="2stems")
    wav_path = tmp_path / "in.wav"
    wav_path.write_bytes(b"fake")

    with pytest.raises(RuntimeError, match="spleeter"):
        engine.separate(wav_path, ["vocals"], tmp_path / "out")
