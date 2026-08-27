"""[6] Export: 선택한 스템의 wav + MIDI를 zip 하나로 묶고, 모든 스템의 MIDI를
트랙(Instrument)별로 유지한 채 하나로 합친 merged.mid도 함께 생성한다.

MIDI는 후처리 결과(04_edited_midi)가 있으면 그것을, 없으면 원본 변환 결과(03_midi)를 사용한다.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pretty_midi

from songsplit.core.midi_io import save_midi
from songsplit.pipeline.job import Job, Stage, StageStatus

ZIP_FILENAME = "export.zip"
MERGED_MIDI_FILENAME = "merged.mid"


def _resolve_outputs(job: Job) -> tuple[dict, dict, dict]:
    sep_entry = job.get_stage(Stage.SEPARATION)
    midi_entry = job.get_stage(Stage.MIDI)
    edited_entry = job.get_stage(Stage.EDITED_MIDI)

    wav_outputs = (
        sep_entry["outputs"] if sep_entry and sep_entry["status"] == StageStatus.DONE.value else {}
    )
    midi_outputs = (
        midi_entry["outputs"] if midi_entry and midi_entry["status"] == StageStatus.DONE.value else {}
    )
    edited_outputs = (
        edited_entry["outputs"] if edited_entry and edited_entry["status"] == StageStatus.DONE.value else {}
    )
    return wav_outputs, midi_outputs, edited_outputs


def merge_midi(stem_midi_paths: dict[str, Path]) -> pretty_midi.PrettyMIDI:
    """여러 스템의 MIDI를 스템별 트랙(Instrument)으로 유지한 채 하나로 합친다.

    각 트랙 이름을 스템 이름으로 지정해 DAW 등에서 구분할 수 있게 한다.
    """
    if not stem_midi_paths:
        raise RuntimeError("합칠 MIDI가 없습니다. 먼저 [4] MIDI 변환을 완료하세요")

    initial_tempo = 120.0
    for path in stem_midi_paths.values():
        tempi = pretty_midi.PrettyMIDI(str(path)).get_tempo_changes()[1]
        if tempi.size:
            initial_tempo = float(tempi[0])
            break

    merged = pretty_midi.PrettyMIDI(initial_tempo=initial_tempo)
    for stem, path in stem_midi_paths.items():
        source = pretty_midi.PrettyMIDI(str(path))
        for inst in source.instruments:
            new_inst = pretty_midi.Instrument(program=inst.program, is_drum=inst.is_drum, name=stem)
            new_inst.notes = list(inst.notes)
            merged.instruments.append(new_inst)
    return merged


def build_export(job: Job) -> Path:
    job.update_stage(Stage.EXPORT, StageStatus.RUNNING)
    try:
        wav_outputs, midi_outputs, edited_outputs = _resolve_outputs(job)

        if not wav_outputs and not midi_outputs:
            raise RuntimeError("내보낼 결과가 없습니다. 먼저 분리 또는 MIDI 변환을 완료하세요")

        final_midi_paths = {
            stem: Path(edited_outputs.get(stem, path)) for stem, path in midi_outputs.items()
        }

        merged_path: Path | None = None
        if final_midi_paths:
            merged = merge_midi(final_midi_paths)
            merged_path = job.stage_dir(Stage.EXPORT) / MERGED_MIDI_FILENAME
            save_midi(merged, merged_path)

        out_path = job.stage_dir(Stage.EXPORT) / ZIP_FILENAME
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for stem, path in wav_outputs.items():
                zf.write(path, arcname=f"stems/{stem}.wav")
            for stem, path in final_midi_paths.items():
                zf.write(path, arcname=f"midi/{stem}.mid")
            if merged_path is not None:
                zf.write(merged_path, arcname=MERGED_MIDI_FILENAME)
    except Exception as exc:
        job.update_stage(Stage.EXPORT, StageStatus.ERROR, error=str(exc))
        raise

    outputs = {
        "zip_path": str(out_path),
        "stems": sorted(set(wav_outputs) | set(midi_outputs)),
        "used_edited_midi": sorted(set(midi_outputs) & set(edited_outputs)),
    }
    if merged_path is not None:
        outputs["merged_midi_path"] = str(merged_path)

    job.update_stage(Stage.EXPORT, StageStatus.DONE, outputs=outputs)
    return out_path
