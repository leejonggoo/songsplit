"""[8] Score merge: 같은 곡의 MIDI / MuseScore / MusicXML / Guitar Pro 파일에서 트랙을 읽어 분석한 뒤,
기준 Guitar Pro(.gp) 악보에 새 트랙으로 추가한다.

흐름: load_*()로 파일을 SourceTrack(박 단위 음표 목록)으로 읽기 → analyze_sources()로 악기 종류,
기준 악보와의 위치 정렬(오프셋·배속), 이미 있는 트랙과의 중복 여부를 분석 → apply_plan()으로 .gp에 추가.
모든 위치는 곡 시작부터 센 4분음표 박 수라서 템포가 서로 달라도 같은 마디에 놓인다.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

import numpy as np
import pretty_midi

from songsplit.stages.guitarpro import (
    CELLS_PER_BEAT,
    AddStemsResult,
    GuitarProError,
    TrackSpec,
    _PC_NAMES,
    _bar_cells,
    gp_key,
    _read_score,
    add_tracks_to_gp,
)

MIDI_EXTS = {".mid", ".midi"}
MUSICXML_EXTS = {".musicxml", ".xml", ".mxl"}
MUSESCORE_EXTS = {".mscz", ".mscx"}
GP_EXTS = {".gp"}
SUPPORTED_EXTS = MIDI_EXTS | MUSICXML_EXTS | MUSESCORE_EXTS | GP_EXTS

_MSCORE_CANDIDATES = [
    "/Applications/MuseScore 4.app/Contents/MacOS/mscore",
    "/Applications/MuseScore 3.app/Contents/MacOS/mscore",
    r"C:\Program Files\MuseScore 4\bin\MuseScore4.exe",
]

_NOTE_VALUES = {
    "Whole": Fraction(4),
    "Half": Fraction(2),
    "Quarter": Fraction(1),
    "Eighth": Fraction(1, 2),
    "16th": Fraction(1, 4),
    "32nd": Fraction(1, 8),
    "64th": Fraction(1, 16),
    "128th": Fraction(1, 32),
    "256th": Fraction(1, 64),
}

# 정렬 탐색 범위: 기준 악보 앞뒤로 이 박 수만큼 밀어 본다
MAX_SHIFT_BEATS = 32
SCALE_CANDIDATES = (1.0, 2.0, 0.5)
MIN_ALIGN_SCORE = 0.35
DUPLICATE_THRESHOLD = 0.8


@dataclass
class SourceTrack:
    origin: str  # 원본 파일 이름
    name: str
    is_drum: bool
    notes: list[tuple[int, float, float]]  # (pitch, 시작 박, 끝 박)
    program: int | None = None  # GM 프로그램 번호
    hint: str = ""  # 악기 종류 힌트 (GP의 InstrumentSet 타입 등)
    # GP 소스일 때만: 트랙 XML(튜닝·악기·사운드 포함)과 음별 (줄, 프렛, 드럼 아티큘레이션). 키는 (pitch, 시작 박)
    track_xml: bytes | None = None
    extras: dict[tuple[int, float], dict] | None = None

    @property
    def role(self) -> str:
        return classify_role(self)

    @property
    def style(self) -> str:
        """GP 트랙 템플릿 종류. 어쿠스틱 기타는 따로 구분한다."""
        role = self.role
        if role == "guitar":
            text = f"{self.name} {self.hint}".lower()
            if self.program in (24, 25) or any(k in text for k in ("acoustic", "steel", "nylon", "어쿠스틱", "클래식")):
                return "acoustic"
        return role

    @property
    def label(self) -> str:
        return f"{self.name} ({Path(self.origin).stem})"


@dataclass
class Alignment:
    scale: float = 1.0
    shift_beats: float = 0.0
    score: float = 0.0
    confident: bool = False
    note: str = ""
    transpose: int = 0  # 소스 음에 더할 반음 수 (기준 악보의 조에 맞추기 위한 조옮김)


@dataclass
class PlanItem:
    track: SourceTrack
    alignment: Alignment
    duplicate_of: str | None = None
    duplicate_score: float = 0.0
    include: bool = True


@dataclass
class MergePlan:
    items: list[PlanItem]
    base_tracks: list[str]
    warnings: list[str] = field(default_factory=list)
    base_key: str = ""
    source_keys: dict[str, str] = field(default_factory=dict)  # 파일 이름 → 추정한 조


# ---- 악기 종류 분류 ----------------------------------------------------------


def classify_role(track: SourceTrack) -> str:
    """vocals / guitar / bass / piano / other / drums 중 하나."""
    if track.is_drum:
        return "drums"
    text = f"{track.name} {track.hint}".lower()
    if any(k in text for k in ("vocal", "voice", "vox", "sing", "choir", "보컬", "노래")):
        return "vocals"
    if "bass" in text or "베이스" in text:
        return "bass"
    if "guitar" in text or "기타" in text:
        return "guitar"
    if "piano" in text or "keys" in text or "피아노" in text:
        return "piano"
    p = track.program
    if p is not None:
        if 32 <= p <= 39:
            return "bass"
        if 24 <= p <= 31:
            return "guitar"
        if 0 <= p <= 7:
            return "piano"
        if p in (52, 53, 54, 85):
            return "vocals"
    return "other"


# ---- 로더 -------------------------------------------------------------------


def load_midi(data: bytes, filename: str) -> list[SourceTrack]:
    midi = pretty_midi.PrettyMIDI(io.BytesIO(data))
    to_beat = lambda t: midi.time_to_tick(t) / midi.resolution  # noqa: E731
    tracks = []
    for i, inst in enumerate(midi.instruments):
        if not inst.notes:
            continue
        name = inst.name.strip() or (
            "Drums" if inst.is_drum else pretty_midi.program_to_instrument_name(inst.program)
        )
        tracks.append(
            SourceTrack(
                origin=filename,
                name=name,
                is_drum=inst.is_drum,
                program=None if inst.is_drum else inst.program,
                notes=[(n.pitch, to_beat(n.start), to_beat(n.end)) for n in inst.notes],
            )
        )
    return tracks


def _find_mscore() -> str | None:
    env = os.environ.get("SONGSPLIT_MSCORE")
    if env and Path(env).exists():
        return env
    for name in ("mscore", "musescore", "mscore4portable"):
        found = shutil.which(name)
        if found:
            return found
    return next((c for c in _MSCORE_CANDIDATES if Path(c).exists()), None)


def load_musicxml(data: bytes, filename: str, suffix: str | None = None) -> list[SourceTrack]:
    from music21 import chord, converter, note

    suffix = suffix or Path(filename).suffix or ".musicxml"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"score{suffix}"
        path.write_bytes(data)
        score = converter.parse(str(path))

    tracks = []
    for i, part in enumerate(score.parts):
        try:
            part = part.stripTies(inPlace=False)
        except Exception:  # noqa: BLE001 - 붙임줄 정리 실패 시 원본 그대로 사용
            pass
        instrument = part.getInstrument(returnDefault=True)
        program = instrument.midiProgram
        flat = part.flatten()
        is_drum = "percussion" in (instrument.instrumentName or "").lower() or program is None and instrument.midiChannel == 9
        notes: list[tuple[int, float, float]] = []
        for el in flat.getElementsByClass([note.Note, chord.Chord, note.Unpitched]):
            length = float(el.duration.quarterLength)
            if length <= 0:  # 꾸밈음
                continue
            start = float(el.offset)
            if isinstance(el, note.Unpitched):
                perc = getattr(el.storedInstrument, "percMapPitch", None)
                if perc is not None:
                    notes.append((int(perc) + 0, start, start + length))
                continue
            pitches = el.pitches if isinstance(el, chord.Chord) else [el.pitch]
            notes.extend((int(p.midi), start, start + length) for p in pitches)
        if notes:
            tracks.append(
                SourceTrack(
                    origin=filename,
                    name=(part.partName or instrument.instrumentName or f"Part {i + 1}").strip(),
                    is_drum=is_drum,
                    program=None if is_drum else program,
                    notes=notes,
                )
            )
    return tracks


def load_musescore(data: bytes, filename: str) -> list[SourceTrack]:
    mscore = _find_mscore()
    if mscore is None:
        raise GuitarProError(
            "MuseScore가 설치되어 있지 않아 .mscz/.mscx를 읽을 수 없습니다. MuseScore에서 MusicXML(.musicxml)로 "
            "내보내 올리거나, 환경변수 SONGSPLIT_MSCORE에 MuseScore 실행 파일 경로를 지정하세요"
        )
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"score{Path(filename).suffix}"
        out = Path(tmp) / "score.musicxml"
        src.write_bytes(data)
        env = dict(os.environ)
        if sys.platform != "darwin":  # macOS 번들에는 offscreen 플랫폼 플러그인이 없다
            env["QT_QPA_PLATFORM"] = "offscreen"
        try:
            proc = subprocess.run(
                [mscore, "-o", str(out), str(src)], capture_output=True, timeout=180, env=env, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise GuitarProError("MuseScore 변환이 시간 초과되었습니다") from exc
        if not out.exists():
            raise GuitarProError(
                f"MuseScore로 {filename}을 변환하지 못했습니다: {proc.stderr.decode(errors='replace')[-300:]}"
            )
        return load_musicxml(out.read_bytes(), filename, suffix=".musicxml")


def _rhythm_beats(rhythm: ET.Element) -> Fraction:
    value = _NOTE_VALUES.get(rhythm.findtext("NoteValue", "Quarter"), Fraction(1))
    dots = rhythm.find("AugmentationDot")
    if dots is not None:
        count = int(dots.get("count", "1"))
        value = value * (2 - Fraction(1, 2**count))
    tuplet = rhythm.find("PrimaryTuplet")
    if tuplet is not None:
        value = value * Fraction(int(tuplet.get("den", "1")), int(tuplet.get("num", "1")))
    return value


def load_gp(data: bytes, filename: str) -> list[SourceTrack]:
    _, root = _read_score(data)
    lookup = {tag: {x.get("id"): x for x in root.find(tag)} for tag in ("Bars", "Voices", "Beats", "Notes", "Rhythms")}
    rhythm_beats = {rid: _rhythm_beats(r) for rid, r in lookup["Rhythms"].items()}

    tracks = list(root.find("Tracks"))
    staff_index: list[list[int]] = []  # 트랙별 보표의 전역 인덱스
    n = 0
    for track in tracks:
        count = max(1, len(track.findall("Staves/Staff")))
        staff_index.append(list(range(n, n + count)))
        n += count

    master_bars = list(root.find("MasterBars"))
    bar_starts: list[Fraction] = []
    pos = Fraction(0)
    for master in master_bars:
        num, den = (int(x) for x in master.findtext("Time", "4/4").split("/"))
        bar_starts.append(pos)
        pos += Fraction(4 * num, den)

    out = []
    for track, staves in zip(tracks, staff_index):
        is_drum = track.findtext("InstrumentSet/Type") == "drumKit"
        notes: list[tuple[int, float, float]] = []
        extras: dict[tuple[int, float], dict] = {}
        open_ties: dict[tuple[int, int, int], list] = {}  # (보표, 성부 칸, 음높이) → 이어지는 음. 마디를 넘어 유지
        for master, bar_start in zip(master_bars, bar_starts):
            bar_ids = master.findtext("Bars", "").split()
            for s in staves:
                if s >= len(bar_ids):
                    continue
                bar = lookup["Bars"].get(bar_ids[s])
                if bar is None:
                    continue
                for slot, vid in enumerate(bar.findtext("Voices", "").split()):
                    voice = lookup["Voices"].get(vid)
                    if voice is None:
                        continue
                    t = bar_start
                    for bid in voice.findtext("Beats", "").split():
                        beat = lookup["Beats"][bid]
                        dur = rhythm_beats[beat.find("Rhythm").get("ref")]
                        if beat.find("GraceNotes") is not None:
                            continue
                        for nid in beat.findtext("Notes", "").split():
                            note = lookup["Notes"][nid]
                            num_el = note.find("Properties/Property[@name='Midi']/Number")
                            if num_el is None:
                                continue
                            pitch = int(num_el.text)
                            tie = note.find("Tie")
                            key = (s, slot, pitch)
                            if tie is not None and tie.get("destination") == "true" and key in open_ties:
                                open_ties[key][2] = float(t + dur)
                                continue
                            entry = [pitch, float(t), float(t + dur)]
                            notes.append(entry)  # type: ignore[arg-type]
                            open_ties[key] = entry
                            string_el = note.find("Properties/Property[@name='String']/String")
                            fret_el = note.find("Properties/Property[@name='Fret']/Fret")
                            art_el = note.find("InstrumentArticulation")
                            extras[(pitch, float(t))] = {
                                "string": int(string_el.text) if string_el is not None else None,
                                "fret": int(fret_el.text) if fret_el is not None else None,
                                "art": int(art_el.text) if art_el is not None and art_el.text else None,
                            }
                        t += dur
        if notes:
            midi_program = track.findtext("Sounds/Sound/MIDI/Program")
            out.append(
                SourceTrack(
                    origin=filename,
                    name=track.findtext("Name", "").strip() or "Track",
                    is_drum=is_drum,
                    program=None if is_drum or midi_program is None else int(midi_program),
                    hint=track.findtext("InstrumentSet/Type", ""),
                    track_xml=ET.tostring(track),
                    extras=extras,
                    notes=[tuple(x) for x in notes],  # type: ignore[misc]
                )
            )
    return out


def load_tracks(data: bytes, filename: str) -> list[SourceTrack]:
    ext = Path(filename).suffix.lower()
    if ext in MIDI_EXTS:
        return load_midi(data, filename)
    if ext in GP_EXTS:
        return load_gp(data, filename)
    if ext in MUSESCORE_EXTS:
        return load_musescore(data, filename)
    if ext in MUSICXML_EXTS:
        return load_musicxml(data, filename)
    raise GuitarProError(
        f"지원하지 않는 형식입니다: {ext or filename}. 지원: {', '.join(sorted(SUPPORTED_EXTS))} "
        "(.gp3~.gp5/.gpx는 Guitar Pro에서 .gp로 저장하세요)"
    )


# ---- 분석 -------------------------------------------------------------------


def _cells(track_notes: list[tuple[int, float, float]], scale: float, shift_cells: int = 0):
    for pitch, start, end in track_notes:
        a = round(start * scale * CELLS_PER_BEAT) + shift_cells
        b = max(a + 1, round(end * scale * CELLS_PER_BEAT) + shift_cells)
        yield pitch, a, b


def _chroma(notes: list[tuple[int, float, float]], scale: float, length: int) -> np.ndarray:
    """칸별 피치클래스 지속 행렬 (length × 12), 행은 L2 정규화."""
    m = np.zeros((length, 12))
    for pitch, a, b in _cells(notes, scale):
        if a >= length:
            continue
        m[a : min(b, length), pitch % 12] = 1.0
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    return np.divide(m, norms, out=np.zeros_like(m), where=norms > 0)


def find_alignment(
    base_notes: list[tuple[int, float, float]], source_notes: list[tuple[int, float, float]], total_cells: int
) -> Alignment:
    """기준 악보의 음과 가장 잘 겹치도록 source의 (배속, 오프셋)을 찾는다."""
    if not base_notes or not source_notes:
        return Alignment(note="비교할 음이 없어 정렬을 건드리지 않았습니다")

    max_shift = MAX_SHIFT_BEATS * CELLS_PER_BEAT
    pad = total_cells + 2 * max_shift
    base = _chroma(base_notes, 1.0, total_cells)
    best = Alignment(score=-1.0)
    best_key = -1.0
    for scale in SCALE_CANDIDATES:
        src = _chroma(source_notes, scale, pad)
        src_rows = int((np.abs(src).sum(axis=1) > 0).sum()) or 1
        for shift in range(-max_shift, max_shift + 1):
            lo = max(0, shift)
            hi = min(total_cells, src.shape[0] + shift)
            if hi <= lo:
                continue
            score = float((base[lo:hi] * src[lo - shift : hi - shift]).sum()) / src_rows
            key = score - 1e-4 * abs(shift) - (0.01 if scale != 1.0 else 0.0)
            if key > best_key:
                best_key = key
                best = Alignment(scale=scale, shift_beats=shift / CELLS_PER_BEAT, score=score)
    best.confident = best.score >= MIN_ALIGN_SCORE
    if not best.confident:
        best = Alignment(score=best.score, note="기준 악보와 비슷한 구간을 찾지 못해 정렬하지 않았습니다")
    return best


# Krumhansl-Kessler 조성 프로파일
_KK_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_KK_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


def _pitch_class_profile(notes: list[tuple[int, float, float]]) -> np.ndarray:
    profile = np.zeros(12)
    for pitch, start, end in notes:
        profile[pitch % 12] += max(end - start, 0.25)
    return profile


def estimate_key(notes: list[tuple[int, float, float]]) -> tuple[str, int, float] | None:
    """음 길이 가중 피치클래스 분포로 조를 추정. (이름, 으뜸음, 상관계수) 또는 None."""
    profile = _pitch_class_profile(notes)
    if not profile.any():
        return None
    best = None
    for tonic in range(12):
        for ref, mode in ((_KK_MAJOR, "major"), (_KK_MINOR, "minor")):
            corr = float(np.corrcoef(profile, np.roll(ref, tonic))[0, 1])
            if best is None or corr > best[2]:
                best = (f"{_PC_NAMES[tonic]} {mode}", tonic, corr)
    return best


def best_transposition(
    base_notes: list[tuple[int, float, float]], source_notes: list[tuple[int, float, float]]
) -> tuple[int, float]:
    """source 음에 더하면 base와 피치클래스 분포가 가장 비슷해지는 반음 수(-6~+5)와 그때의 상관계수."""
    base, src = _pitch_class_profile(base_notes), _pitch_class_profile(source_notes)
    if not base.any() or not src.any():
        return 0, 0.0
    best_k, best_corr = 0, -2.0
    for k in range(12):
        corr = float(np.corrcoef(base, np.roll(src, k))[0, 1])
        if corr > best_corr + 1e-9:
            best_k, best_corr = k, corr
    return (best_k if best_k < 6 else best_k - 12), best_corr


def _transposed(notes, semitones: int):
    return [(p + semitones, s, e) for p, s, e in notes] if semitones else notes


def _onsets(track_notes, scale: float, shift_beats: float, transpose: int = 0) -> set[tuple[int, int]]:
    shift_cells = round(shift_beats * CELLS_PER_BEAT)
    return {(a, pitch + transpose) for pitch, a, _ in _cells(track_notes, scale, shift_cells)}


def analyze_sources(base_gp: bytes, sources: list[SourceTrack]) -> MergePlan:
    """각 소스 트랙의 정렬을 찾고 기준 악보의 기존 트랙과의 중복 여부를 판정해 병합 계획을 만든다."""
    base_tracks = load_gp(base_gp, "base.gp")
    total_cells = sum(_bar_cells(_read_score(base_gp)[1]))
    base_pitched = [n for t in base_tracks if not t.is_drum for n in t.notes]
    all_base_names = [t.name for t in base_tracks]

    plan = MergePlan(items=[], base_tracks=all_base_names, base_key=gp_key(base_gp)[0])
    alignments: dict[str, Alignment] = {}
    for origin in dict.fromkeys(t.origin for t in sources):
        pitched = [n for t in sources if t.origin == origin and not t.is_drum for n in t.notes]
        alignment = find_alignment(base_pitched, pitched, total_cells)
        if pitched and base_pitched and not alignment.confident:
            # 같은 위치에서 안 맞으면 다른 조로 옮겨 쓴 사본일 수 있다: 조옮김한 뒤 다시 맞춰 본다
            semitones, _ = best_transposition(base_pitched, pitched)
            if semitones:
                moved = find_alignment(base_pitched, _transposed(pitched, semitones), total_cells)
                if moved.confident and moved.score >= alignment.score + 0.1:
                    moved.transpose = semitones
                    moved.note = f"기준 악보의 조에 맞춰 {semitones:+d}반음 조옮김"
                    alignment = moved
        alignments[origin] = alignment
        key = estimate_key(pitched)
        if key:
            plan.source_keys[origin] = key[0]
        if not pitched:
            alignment.note = "음정 트랙이 없는 파일"

    # 드럼만 있는 파일은 음정으로 정렬할 수 없으므로 같은 묶음에서 가장 확실하게 정렬된 파일의 값을 따른다
    confident = [a for a in alignments.values() if a.confident]
    if confident:
        reference = max(confident, key=lambda a: a.score)
        for origin, alignment in alignments.items():
            if alignment.note == "음정 트랙이 없는 파일":
                alignments[origin] = Alignment(
                    reference.scale, reference.shift_beats, 0.0, False, "다른 파일의 정렬 값을 따름"
                )
    for origin, alignment in alignments.items():
        if alignment.note:
            plan.warnings.append(f"{origin}: {alignment.note}")

    for track in sources:
        al = alignments[track.origin]
        onsets = _onsets(track.notes, al.scale, al.shift_beats, 0 if track.is_drum else al.transpose)
        best_name, best_sim = None, 0.0
        for base in base_tracks:
            if base.is_drum != track.is_drum or not onsets:
                continue
            sim = len(onsets & _onsets(base.notes, 1.0, 0.0)) / len(onsets)
            if sim > best_sim:
                best_name, best_sim = base.name, sim
        duplicate = best_sim >= DUPLICATE_THRESHOLD
        plan.items.append(
            PlanItem(
                track=track,
                alignment=al,
                duplicate_of=best_name if duplicate else None,
                duplicate_score=best_sim,
                include=not duplicate,
            )
        )
    return plan


# ---- 적용 -------------------------------------------------------------------


def apply_plan(base_gp: bytes, plan: MergePlan) -> AddStemsResult:
    """plan에서 include가 켜진 항목을 기준 악보에 새 트랙으로 추가한다."""
    specs = []
    for item in plan.items:
        if not item.include:
            continue
        t = item.track
        shift_cells = round(item.alignment.shift_beats * CELLS_PER_BEAT)
        cell_notes = list(_cells(t.notes, item.alignment.scale, shift_cells))
        semitones = 0 if t.is_drum else item.alignment.transpose
        if semitones:
            cell_notes = [(p + semitones, a, b) for p, a, b in cell_notes]
        extras = None
        if t.extras and not semitones:  # 조옮김하면 원래 줄·프렛이 맞지 않으므로 튜닝에 맞춰 다시 계산한다
            extras = {
                (pitch, a): t.extras[(src_pitch, src_start)]
                for (src_pitch, src_start, _), (pitch, a, _b) in zip(t.notes, cell_notes)
                if (src_pitch, src_start) in t.extras
            }
        specs.append(
            TrackSpec(
                key=t.label,
                name=t.label,
                style=t.style,
                notes=cell_notes,
                is_drum=t.is_drum,
                track_xml=t.track_xml,
                extras=extras,
            )
        )
    if not specs:
        raise GuitarProError("추가할 트랙이 선택되지 않았습니다")
    return add_tracks_to_gp(base_gp, specs)
