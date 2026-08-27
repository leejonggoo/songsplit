"""[4] Transcription 스테이지 오케스트레이션: 분리된 스템별로 MIDI를 생성해
03_midi/{stem}.mid에 저장하고 job manifest를 갱신한다.

"drums" 스템은 onset 기반 분류를, 그 외 스템은 basic-pitch 기반 폴리포닉
피치 검출을 사용한다. 두 경로 모두 Analysis 단계에서 확정된(사용자 수정 포함)
템포를 결과 MIDI에 반영한다.
"""

from __future__ import annotations

from pathlib import Path

from songsplit.core.midi_io import save_midi
from songsplit.pipeline.job import Job, Stage, StageStatus
from songsplit.stages.transcription.drums import transcribe_drums
from songsplit.stages.transcription.melodic import transcribe_melodic

DRUM_STEM_NAME = "drums"


def transcribe_stem(job: Job, stem: str, wav_path: Path, tempo_bpm: float) -> Path:
    if stem == DRUM_STEM_NAME:
        midi = transcribe_drums(wav_path, tempo_bpm)
    else:
        midi = transcribe_melodic(wav_path, tempo_bpm)

    out_path = job.stage_dir(Stage.MIDI) / f"{stem}.mid"
    save_midi(midi, out_path)
    return out_path


def transcribe_all(job: Job, stem_wav_paths: dict[str, Path], tempo_bpm: float) -> dict[str, Path]:
    job.update_stage(
        Stage.MIDI,
        StageStatus.RUNNING,
        params={"tempo_bpm": tempo_bpm, "stems": list(stem_wav_paths)},
    )
    try:
        produced = {
            stem: transcribe_stem(job, stem, wav_path, tempo_bpm)
            for stem, wav_path in stem_wav_paths.items()
        }
    except Exception as exc:
        job.update_stage(Stage.MIDI, StageStatus.ERROR, error=str(exc))
        raise

    job.update_stage(
        Stage.MIDI,
        StageStatus.DONE,
        params={"tempo_bpm": tempo_bpm, "stems": list(stem_wav_paths)},
        outputs={stem: str(path) for stem, path in produced.items()},
    )
    return produced
