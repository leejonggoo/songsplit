"""[7] Guitar Pro 내보내기: 스템별 MIDI를 하나의 Guitar Pro 5(.gp5) 악보로 변환한다.

.gp5는 Guitar Pro 5~8, TuxGuitar, MuseScore 등에서 열린다. 변환 규칙:
  - 마디 그리드는 Analysis에서 확정한 템포/마디당 박 수/첫 마디 시작 시각을 기준으로 만든다.
    첫 마디 시작 앞의 음(못갖춘마디)은 앞마디의 뒷부분에 쉼표와 함께 배치된다.
  - 노트는 16분음표 그리드로 양자화하고, 한 마디를 넘는 음/표준 길이가 아닌 음은 붙임줄(tie)로 나눈다.
  - 기타/베이스는 표준 튜닝 지판 위에서 프렛이 낮은 위치로, 그 외 악기는 넓은 음역의 7현 가상 지판으로 배정한다.
  - 드럼은 퍼커션 트랙(GM 드럼 번호)으로 쓴다.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import guitarpro as gp
import pretty_midi

from songsplit.pipeline.job import Job, Stage, StageStatus

SLOTS_PER_QUARTER = 4  # 16분음표 그리드
MAX_FRET = 24

# 16분음표 개수 -> (guitarpro Duration value, 점음표 여부). 표준으로 표기 가능한 길이만.
_DURATIONS: dict[int, tuple[int, bool]] = {
    1: (16, False),
    2: (8, False),
    3: (8, True),
    4: (4, False),
    6: (4, True),
    8: (2, False),
    12: (2, True),
    16: (1, False),
}

# 튜닝은 높은 현(1번줄)부터 낮은 현 순서
_GUITAR_TUNING = [64, 59, 55, 50, 45, 40]
_BASS_TUNING = [43, 38, 33, 28]
_WIDE_TUNING = [96, 84, 72, 60, 48, 36, 24]

# (GM 프로그램 번호, 튜닝)
_TRACK_PROFILES: dict[str, tuple[int, list[int]]] = {
    "guitar": (25, _GUITAR_TUNING),
    "bass": (33, _BASS_TUNING),
    "piano": (0, _WIDE_TUNING),
    "vocals": (54, _WIDE_TUNING),
    "other": (48, _WIDE_TUNING),
}
_DEFAULT_PROFILE = (0, _WIDE_TUNING)
# (채널, 이펙트 채널) 쌍. 9/10은 드럼 전용
_MELODIC_CHANNELS = [(0, 1), (2, 3), (4, 5), (6, 7), (11, 12), (13, 14)]


@dataclass
class _Slot:
    onset: int  # 마디 안 16분음표 위치
    length: int
    pitches: list[tuple[int, int]]  # (pitch, velocity)


def assign_strings(pitches: list[int], tuning: list[int]) -> dict[int, tuple[int, int]]:
    """동시에 울리는 음들에 (줄 번호(1부터), 프렛)을 배정한다. 연주 불가능한 음은 빠진다.

    높은 음부터 처리해 각 음마다 프렛이 가장 낮은 빈 줄을 고른다."""
    result: dict[int, tuple[int, int]] = {}
    used: set[int] = set()
    for pitch in sorted(set(pitches), reverse=True):
        candidates = [
            (pitch - base, idx)
            for idx, base in enumerate(tuning)
            if idx not in used and 0 <= pitch - base <= MAX_FRET
        ]
        if not candidates:
            continue
        fret, idx = min(candidates)
        used.add(idx)
        result[pitch] = (idx + 1, fret)
    return result


def split_length(length: int) -> list[int]:
    """16분음표 단위 길이를 표준 길이 조각들로 나눈다 (큰 것부터)."""
    parts = []
    while length > 0:
        piece = max(d for d in _DURATIONS if d <= length)
        parts.append(piece)
        length -= piece
    return parts


def _duration(slots: int) -> gp.Duration:
    value, dotted = _DURATIONS[slots]
    return gp.Duration(value=value, isDotted=dotted)


def _collect_slots(
    notes: list[pretty_midi.Note], origin: float, slot_sec: float, slots_per_bar: int, n_bars: int
) -> dict[int, list[_Slot]]:
    """마디 번호 -> 그 마디의 _Slot 목록. 마디를 넘는 음은 다음 마디로 이어 붙인다(tie 표시는 호출부)."""
    by_start: dict[int, dict[str, object]] = {}
    for n in notes:
        start = round((n.start - origin) / slot_sec)
        if start < 0 or start >= slots_per_bar * n_bars:
            continue
        end = max(round((n.end - origin) / slot_sec), start + 1)
        entry = by_start.setdefault(start, {"end": end, "pitches": {}})
        entry["end"] = max(entry["end"], end)
        entry["pitches"][n.pitch] = max(entry["pitches"].get(n.pitch, 0), n.velocity)

    starts = sorted(by_start)
    bars: dict[int, list[_Slot]] = {}
    for i, start in enumerate(starts):
        entry = by_start[start]
        end = entry["end"]
        if i + 1 < len(starts):
            end = min(end, starts[i + 1])  # 다음 타격 전까지로 자른다 (단성 표기)
        bar, onset = divmod(start, slots_per_bar)
        # 마디 경계에서 나눈다
        remaining, pos, cur_bar, cur_onset = end - start, start, bar, onset
        first = True
        while remaining > 0:
            room = slots_per_bar - cur_onset
            take = min(remaining, room)
            bars.setdefault(cur_bar, []).append(
                _Slot(cur_onset, take, [(p, v) for p, v in entry["pitches"].items()] if first else [(p, -1) for p in entry["pitches"]])
            )
            remaining -= take
            pos += take
            cur_bar, cur_onset = divmod(pos, slots_per_bar)
            first = False
    return bars


def _fill_measure(
    measure: gp.Measure,
    slots: list[_Slot],
    slots_per_bar: int,
    tuning: list[int],
    is_drum: bool,
) -> None:
    voice = measure.voices[0]
    voice.beats = []
    cursor = 0

    def add_rest(length: int) -> None:
        for piece in split_length(length):
            beat = gp.Beat(voice, duration=_duration(piece), status=gp.BeatStatus.rest)
            voice.beats.append(beat)

    for slot in sorted(slots, key=lambda s: s.onset):
        if slot.onset < cursor:
            continue  # 겹침 방어
        if slot.onset > cursor:
            add_rest(slot.onset - cursor)
        pitches = [p for p, _ in slot.pitches]
        velocity = {p: v for p, v in slot.pitches}
        if is_drum:
            mapping = {p: (1, p) for p in pitches}
        else:
            mapping = assign_strings(pitches, tuning)
        pieces = split_length(slot.length)
        if not mapping:
            add_rest(slot.length)
        else:
            for k, piece in enumerate(pieces):
                beat = gp.Beat(voice, duration=_duration(piece), status=gp.BeatStatus.normal)
                for pitch, (string, fret) in mapping.items():
                    tied = velocity[pitch] < 0 or k > 0
                    beat.notes.append(
                        gp.Note(
                            beat,
                            value=fret,
                            string=string,
                            velocity=95 if velocity[pitch] < 0 else max(1, min(127, velocity[pitch])),
                            type=gp.NoteType.tie if tied and not is_drum else gp.NoteType.normal,
                        )
                    )
                voice.beats.append(beat)
        cursor = slot.onset + slot.length
    if cursor < slots_per_bar:
        add_rest(slots_per_bar - cursor)


def midi_to_guitarpro(
    stem_midis: Mapping[str, pretty_midi.PrettyMIDI],
    tempo_bpm: float,
    beats_per_bar: int = 4,
    first_downbeat_sec: float = 0.0,
    title: str = "SongSplit",
) -> gp.Song:
    beat_sec = 60.0 / tempo_bpm
    slot_sec = beat_sec / SLOTS_PER_QUARTER
    bar_sec = beat_sec * beats_per_bar
    slots_per_bar = beats_per_bar * SLOTS_PER_QUARTER
    # 첫 마디 시작이 마디선에 오도록, 그 앞의 못갖춘마디가 들어갈 만큼 앞에서 시작한다
    origin = first_downbeat_sec - math.ceil(first_downbeat_sec / bar_sec - 1e-6) * bar_sec

    end_time = max(
        (n.end for m in stem_midis.values() for i in m.instruments for n in i.notes), default=0.0
    )
    n_bars = max(1, math.ceil((end_time - origin) / bar_sec))

    song = gp.Song()
    song.title = title
    song.tempo = int(round(tempo_bpm))
    song.tracks = []
    song.measureHeaders = []

    bar_ticks = gp.Duration.quarterTime * beats_per_bar
    for i in range(n_bars):
        header = gp.MeasureHeader(number=i + 1, start=gp.Duration.quarterTime + i * bar_ticks)
        header.timeSignature = gp.TimeSignature(numerator=beats_per_bar, denominator=gp.Duration(value=4))
        song.measureHeaders.append(header)

    melodic_index = 0
    for number, (stem, midi) in enumerate(stem_midis.items(), start=1):
        is_drum = any(inst.is_drum for inst in midi.instruments)
        program, tuning = _TRACK_PROFILES.get(stem, _DEFAULT_PROFILE)
        track = gp.Track(song, number=number, name=stem[:40])
        track.isPercussionTrack = is_drum
        if is_drum:
            channel, effect_channel = 9, 10
            tuning = _GUITAR_TUNING
        else:
            channel, effect_channel = _MELODIC_CHANNELS[melodic_index % len(_MELODIC_CHANNELS)]
            melodic_index += 1
        track.channel = gp.MidiChannel(channel=channel, effectChannel=effect_channel, instrument=program)
        track.strings = [gp.GuitarString(number=i + 1, value=v) for i, v in enumerate(tuning)]
        track.fretCount = MAX_FRET
        track.measures = [gp.Measure(track, header) for header in song.measureHeaders]

        notes = [n for inst in midi.instruments for n in inst.notes]
        bars = _collect_slots(notes, origin, slot_sec, slots_per_bar, n_bars)
        for idx, measure in enumerate(track.measures):
            _fill_measure(measure, bars.get(idx, []), slots_per_bar, tuning, is_drum)
        song.tracks.append(track)
    return song


def write_guitarpro(song: gp.Song, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    gp.write(song, str(path), version=(5, 1, 0), encoding="utf-8")
    return path


def export_guitarpro(job: Job) -> Path:
    """수정된(04) MIDI가 있으면 그것을, 없으면 03 MIDI를 사용해 Guitar Pro 파일을 만든다."""
    from songsplit.core.midi_io import load_midi
    from songsplit.stages.analyze import load_analysis

    midi_entry = job.get_stage(Stage.MIDI)
    if not midi_entry or midi_entry.get("status") != StageStatus.DONE.value:
        raise RuntimeError("먼저 MIDI 변환을 완료해야 합니다")
    edited = job.get_stage(Stage.EDITED_MIDI)
    edited_outputs = edited["outputs"] if edited and edited.get("status") == StageStatus.DONE.value else {}
    paths = {stem: Path(edited_outputs.get(stem, p)) for stem, p in midi_entry["outputs"].items()}

    analysis = load_analysis(job)
    params = {
        "tempo_bpm": analysis.tempo_bpm if analysis else 120.0,
        "beats_per_bar": analysis.beats_per_bar if analysis else 4,
        "first_downbeat_sec": analysis.first_downbeat_sec if analysis else 0.0,
        "stems": list(paths),
    }
    job.update_stage(Stage.GUITARPRO, StageStatus.RUNNING, params=params)
    try:
        title = Path(job.read_manifest().get("source_filename", "SongSplit")).stem
        song = midi_to_guitarpro(
            {stem: load_midi(p) for stem, p in paths.items()},
            tempo_bpm=params["tempo_bpm"],
            beats_per_bar=params["beats_per_bar"],
            first_downbeat_sec=params["first_downbeat_sec"],
            title=title,
        )
        out = write_guitarpro(song, job.stage_dir(Stage.GUITARPRO) / "song.gp5")
    except Exception as exc:
        job.update_stage(Stage.GUITARPRO, StageStatus.ERROR, error=str(exc))
        raise

    job.update_stage(
        Stage.GUITARPRO, StageStatus.DONE, params=params, outputs={"gp5_path": str(out)}
    )
    return out
