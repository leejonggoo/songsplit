"""[7] Guitar Pro: 업로드한 Guitar Pro 파일(.gp, GP7/8 형식)에 분리된 스템의 MIDI를 새 트랙으로 추가한다.

.gp 파일은 zip이고 악보 본체는 Content/score.gpif(XML)이다. 기존 트랙·마디·음표는 건드리지 않고
새 Track과 마디별 Bar/Voice/Beat/Note를 덧붙인다. MIDI의 초 단위 시간은 (BPM, 오프셋)으로 박 단위로
환산한 뒤 1/16 격자에 맞춰 마디 경계를 따라 쪼개 넣는다(마디를 넘는 음은 붙임줄로 연결).
"""

from __future__ import annotations

import copy
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import pretty_midi

from songsplit.pipeline.job import Job, Stage, StageStatus

SCORE_ENTRY = "Content/score.gpif"
TEMPLATES_PATH = Path(__file__).with_name("gp_templates.xml")

CELLS_PER_BEAT = 4  # 1/16 격자
_RHYTHM_NAMES = {1: "16th", 2: "Eighth", 4: "Quarter", 8: "Half", 16: "Whole"}
# 피치클래스 → (음 이름, 임시표). Guitar Pro가 저장하는 표기("#"/"b")를 그대로 쓴다
_SHARP_SPELLING = [("C", ""), ("C", "#"), ("D", ""), ("D", "#"), ("E", ""), ("F", ""), ("F", "#"), ("G", ""), ("G", "#"), ("A", ""), ("A", "#"), ("B", "")]
_FLAT_SPELLING = [("C", ""), ("D", "b"), ("D", ""), ("E", "b"), ("E", ""), ("F", ""), ("G", "b"), ("G", ""), ("A", "b"), ("A", ""), ("B", "b"), ("B", "")]
MAX_FRET = 24

# 스템 이름 → (템플릿 종류, 트랙 색, 약칭, 보표 음자리표)
_STEM_STYLES: dict[str, tuple[str, str, str, str]] = {
    "vocals": ("vocals", "235 120 160", "Voc.", "G2"),
    "guitar": ("guitar", "235 152 125", "Gtr.", "G2"),
    "acoustic": ("acoustic", "230 190 110", "A.Gtr.", "G2"),
    "bass": ("bass", "125 175 120", "Bs.", "F4"),
    "piano": ("piano", "150 180 235", "Pno.", "G2"),
    "other": ("piano", "180 150 220", "Oth.", "G2"),
    "drums": ("drums", "117 201 227", "Drm.", "Neutral"),
}
_DEFAULT_STYLE = ("piano", "150 180 235", "Trk.", "G2")


class GuitarProError(RuntimeError):
    """지원하지 않는 파일이거나 변환할 수 없을 때 발생."""


@dataclass
class GpInfo:
    track_names: list[str]
    bar_count: int
    tempo_bpm: float
    time_signatures: list[str]
    has_drum_track: bool


@dataclass
class AddStemsResult:
    data: bytes
    added_tracks: list[str]
    notes_written: dict[str, int] = field(default_factory=dict)
    notes_dropped: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


# ---- 읽기 ------------------------------------------------------------------


# Guitar Pro는 제목·가수·트랙 이름·가사 같은 문자열을 CDATA로 저장한다(`<Line><![CDATA[발]]></Line>`).
# ElementTree는 CDATA를 읽는 순간 일반 텍스트로 바꿔 버리므로, 읽을 때 CDATA였던 요소에 _cd 표시를 달고
# 저장할 때 다시 CDATA로 되돌린다. 새 요소도 el.set("_cd", "1")로 CDATA를 요청할 수 있다.
_CD = "_cd"
_CDATA_IN = re.compile(r"<(\w+)([^<>]*)><!\[CDATA\[(.*?)\]\]>", re.S)
_CDATA_WRAPPED_IN = re.compile(r"<(\w+)([^<>]*)>\s+<!\[CDATA\[(.*?)\]\]>\s+</\1>", re.S)  # 줄바꿈으로 감싼 형태(구간 이름 등)
_CDATA_OUT = re.compile(r'<(\w+)([^<>]*?) _cd="([12])"([^<>]*?)(?:/>|>(.*?)</\1>)', re.S)


def _escape_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _unescape_text(text: str) -> str:
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def parse_gpif(data: bytes) -> ET.Element:
    text = data.decode("utf-8")
    text = _CDATA_WRAPPED_IN.sub(
        lambda m: f'<{m.group(1)}{m.group(2)} {_CD}="2">{_escape_text(m.group(3))}</{m.group(1)}>', text
    )
    text = _CDATA_IN.sub(lambda m: f'<{m.group(1)}{m.group(2)} {_CD}="1">{_escape_text(m.group(3))}', text)
    return ET.fromstring(text.encode("utf-8"))


_SCORE_CDATA_FIELDS = (
    "Title", "SubTitle", "Artist", "Album", "Words", "Music", "WordsAndMusic", "Copyright", "Tabber",
    "Instructions", "Notices", "FirstPageHeader", "FirstPageFooter", "PageHeader", "PageFooter",
)


def _mark_cdata(root: ET.Element) -> None:
    """Guitar Pro가 항상 CDATA로 쓰는 항목에 표시한다. 다른 도구나 예전 버전이 CDATA를 풀어 버린 파일도 되돌린다."""
    for tag in _SCORE_CDATA_FIELDS:
        el = root.find(f"Score/{tag}")
        if el is not None and _CD not in el.attrib:
            el.set(_CD, "1")
    for el in root.iterfind("Tracks/Track/Name"):
        el.set(_CD, el.get(_CD, "1"))
    for el in root.iterfind("Tracks/Track/ShortName"):
        el.set(_CD, el.get(_CD, "1"))
    for el in root.iterfind("Tracks/Track/Lyrics/Line/Text"):
        el.set(_CD, "1")
    for el in root.iterfind("Beats/Beat/Lyrics/Line"):
        el.set(_CD, "1")
    for tag in ("Letter", "Text"):
        for el in root.iterfind(f"MasterBars/MasterBar/Section/{tag}"):
            if _CD not in el.attrib:  # CDATA가 풀리면서 줄바꿈이 텍스트에 섞여 들어간 경우를 정리한다
                el.text = (el.text or "").strip()
                el.set(_CD, "2")


def serialize_gpif(root: ET.Element) -> bytes:
    _mark_cdata(root)
    text = ET.tostring(root, encoding="unicode")

    def to_cdata(m: re.Match) -> str:
        attrs = (m.group(2) + m.group(4)).rstrip()
        inner = _unescape_text(m.group(5) or "")
        pad = "\n" if m.group(3) == "2" else ""
        return f"<{m.group(1)}{attrs}>{pad}<![CDATA[{inner}]]>{pad}</{m.group(1)}>"

    text = _CDATA_OUT.sub(to_cdata, text)
    return ('<?xml version="1.0" encoding="utf-8"?>\n' + text).encode("utf-8")


def _read_score(gp_bytes: bytes) -> tuple[zipfile.ZipFile, ET.Element]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(gp_bytes))
    except zipfile.BadZipFile as exc:
        raise GuitarProError(
            "Guitar Pro 7/8 형식(.gp) 파일만 지원합니다. .gp3/.gp4/.gp5/.gpx 파일은 "
            "Guitar Pro에서 .gp로 다시 저장한 뒤 업로드하세요"
        ) from exc
    if SCORE_ENTRY not in zf.namelist():
        raise GuitarProError(f"{SCORE_ENTRY}가 없습니다. 올바른 .gp 파일이 아닙니다")
    return zf, parse_gpif(zf.read(SCORE_ENTRY))


def _initial_tempo(root: ET.Element) -> float:
    for auto in root.findall("MasterTrack/Automations/Automation"):
        if auto.findtext("Type") == "Tempo":
            return float(auto.findtext("Value", "120").split()[0])
    return 120.0


def _bar_cells(root: ET.Element) -> list[int]:
    """마디별 16분음표 칸 수 (4/4 → 16)."""
    cells = []
    for master in root.find("MasterBars"):
        num, den = (int(x) for x in master.findtext("Time", "4/4").split("/"))
        if (num * 16) % den:
            raise GuitarProError(f"지원하지 않는 박자표입니다: {num}/{den}")
        cells.append(num * 16 // den)
    return cells


def _is_drum_track(track: ET.Element) -> bool:
    return track.findtext("InstrumentSet/Type") == "drumKit"


_MAJOR_SHARPS = ["C", "G", "D", "A", "E", "B", "F#", "C#"]
_MAJOR_FLATS = ["C", "F", "Bb", "Eb", "Ab", "Db", "Gb", "Cb"]
_NOTE_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def key_name_and_tonic(count: int, mode: str) -> tuple[str, int]:
    """조표(올림/내림 개수, Major/Minor) → ('G major' 같은 이름, 으뜸음 피치클래스)."""
    major = (_MAJOR_SHARPS[count] if count >= 0 else _MAJOR_FLATS[-count]) if abs(count) < 8 else "C"
    tonic = (_NOTE_PC[major[0]] + (1 if "#" in major else -1 if "b" in major else 0)) % 12
    if mode.lower() == "minor":
        tonic = (tonic - 3) % 12
        return f"{_PC_NAMES[tonic]} minor", tonic
    return f"{major} major", tonic


_PC_NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]


def gp_key(gp_bytes: bytes) -> tuple[str, int, bool]:
    """첫 마디의 조표: (이름, 으뜸음 피치클래스, 장조 여부)."""
    root = _read_score(gp_bytes)[1]
    key = next(iter(root.find("MasterBars"))).find("Key")
    count = int(key.findtext("AccidentalCount", "0") or 0) if key is not None else 0
    mode = key.findtext("Mode", "Major") if key is not None else "Major"
    name, tonic = key_name_and_tonic(count, mode)
    return name, tonic, mode.lower() != "minor"


def inspect_gp(gp_bytes: bytes) -> GpInfo:
    _, root = _read_score(gp_bytes)
    tracks = root.find("Tracks")
    return GpInfo(
        track_names=[t.findtext("Name", "") for t in tracks],
        bar_count=len(root.find("MasterBars")),
        tempo_bpm=_initial_tempo(root),
        time_signatures=sorted({m.findtext("Time", "4/4") for m in root.find("MasterBars")}),
        has_drum_track=any(_is_drum_track(t) for t in tracks),
    )


# ---- MIDI → 격자 ------------------------------------------------------------


def _to_grid(
    cell_notes: list[tuple[int, int, int]],
    *,
    total_cells: int,
    one_cell_notes: bool,
) -> tuple[list[set[int]], list[set[int]], int, int]:
    """(pitch, 시작 칸, 끝 칸) 목록을 칸별 (지속 음 집합, 시작 음 집합)으로 변환.

    (…, 쓴 음 수, 버린 음 수)를 함께 반환한다.
    """
    sustain: list[set[int]] = [set() for _ in range(total_cells)]
    onset: list[set[int]] = [set() for _ in range(total_cells)]
    written = dropped = 0
    for pitch, start, end in cell_notes:
        if start < 0 or start >= total_cells:
            dropped += 1
            continue
        end = start + 1 if one_cell_notes else min(max(end, start + 1), total_cells)
        for cell in range(start, end):
            sustain[cell].add(pitch)
        onset[start].add(pitch)
        written += 1
    return sustain, onset, written, dropped


# ---- GPIF 작성 --------------------------------------------------------------


class _Ids:
    """Bars/Voices/Beats/Notes/Rhythms 컬렉션의 다음 id 발급기."""

    def __init__(self, root: ET.Element):
        self.root = root
        self.next = {
            tag: max((int(x.get("id")) for x in root.find(tag)), default=-1) + 1
            for tag in ("Bars", "Voices", "Beats", "Notes", "Rhythms")
        }

    def new(self, tag: str, element: str) -> tuple[ET.Element, str]:
        ident = str(self.next[tag])
        self.next[tag] += 1
        return ET.SubElement(self.root.find(tag), element, id=ident), ident


def _load_template(kind: str) -> ET.Element:
    for tpl in ET.parse(TEMPLATES_PATH).getroot():
        if tpl.get("kind") == kind:
            return tpl
    raise GuitarProError(f"트랙 템플릿을 찾을 수 없습니다: {kind}")


def _drum_articulations(track: ET.Element) -> dict[int, int]:
    """드럼 트랙의 MIDI 번호 → InstrumentArticulation 인덱스 (요소·아티큘레이션 평탄화 순서)."""
    mapping: dict[int, int] = {}
    index = 0
    for articulation in track.iterfind("InstrumentSet/Elements/Element/Articulations/Articulation"):
        for num in articulation.findtext("InputMidiNumbers", "").split():
            mapping.setdefault(int(num), index)
        index += 1
    return mapping


def _is_drum_set(track: ET.Element) -> bool:
    return track.findtext("InstrumentSet/Type") == "drumKit"


def _is_fretted(track: ET.Element) -> bool:
    """기타·베이스 계열이면 True. 건반·관악·보컬은 줄 번호를 붙이되 음높이를 바꾸지는 않는다."""
    kind = track.findtext("InstrumentSet/Type", "").lower()
    return "guitar" in kind or "bass" in kind


def _staff_tuning(track: ET.Element) -> list[int] | None:
    """줄 악기의 개방현 음높이(낮은 줄부터). 튜닝이 없는 악기(건반·드럼 등)는 None."""
    if _is_drum_set(track):
        return None
    text = track.findtext("Staves/Staff/Properties/Property[@name='Tuning']/Pitches")
    values = [int(x) for x in text.split()] if text else []
    return values if any(values) else None


def _set_tuning(track: ET.Element, tuning: list[int]) -> None:
    track.find("Staves/Staff/Properties/Property[@name='Tuning']/Pitches").text = " ".join(map(str, tuning))


def _base_tuning(base_tracks: list[ET.Element], like: ET.Element, strings: int) -> list[int] | None:
    """기준 악보에서 같은 계열(베이스/기타)·같은 줄 수 악기의 튜닝 — 드롭 D나 반음 내림 곡에 맞추기 위해 따른다."""
    is_bass = "bass" in like.findtext("InstrumentSet/Type", "").lower()
    for other in base_tracks:
        tuning = _staff_tuning(other)
        if tuning and len(tuning) == strings and ("bass" in other.findtext("InstrumentSet/Type", "").lower()) == is_bass:
            return tuning
    return None


def _pick_string(midi: int, tuning: list[int], taken: set[int]) -> tuple[int, int] | None:
    """midi를 칠 (줄, 프렛). 12프렛 이내에서 가장 낮은 프렛을 우선하고 이미 쓰인 줄은 피한다."""
    best = None
    for string, open_pitch in enumerate(tuning):
        fret = midi - open_pitch
        if 0 <= fret <= MAX_FRET and string not in taken:
            key = (fret > 12, fret)
            if best is None or key < best[0]:
                best = (key, string, fret)
    return None if best is None else (best[1], best[2])


TRANSPOSE_HEADROOM = 12  # 조표를 바꿔(조옮김) 음이 이만큼 움직여도 줄·프렛이 유효하도록 음역에 여유를 둔다
DEFAULT_STRINGS = 6


def _wide_tuning(low: int, high: int, strings: int = DEFAULT_STRINGS) -> list[int]:
    """건반·관악·보컬처럼 줄 악기가 아닌 트랙용 튜닝: [low-여유, high+여유]를 24프렛 안에서 모두 덮는다.

    줄 악기가 아니어도 Guitar Pro는 음마다 (줄, 프렛)을 요구한다. 기본 기타 튜닝(40~88)으로는 범위 밖 음의 프렛이
    -2147483648이 되고, 오디오 엔진이 이 값으로 악기 샘플을 찾다가 죽는다. 줄 간격을 고르게 나눠 한 음에 여러 줄이 겹치게 하면
    화음도 서로 다른 줄에 배정할 수 있다.
    """
    lowest = max(0, min(40, low - TRANSPOSE_HEADROOM))
    top = max(64, high + TRANSPOSE_HEADROOM - MAX_FRET)
    top = min(top, 127 - MAX_FRET)
    top = max(top, lowest + strings - 1)
    return [round(lowest + (top - lowest) * k / (strings - 1)) for k in range(strings)]


def _match_strings(pitches: list[int], tuning: list[int], taken: set[int]) -> dict[int, tuple[int, int]]:
    """한 박의 음들을 서로 다른 줄에 최대한 많이 배정한다(이분 매칭). 반환: {음 번호(pitches 인덱스): (줄, 프렛)}.

    낮은 프렛과 12프렛 이내를 우선하되, 화음이 빽빽해도 줄이 모자라지 않도록 필요하면 다른 음을 다른 줄로 옮긴다.
    """
    options: list[list[tuple[int, int]]] = []
    for midi in pitches:
        cand = [
            (string, midi - open_pitch)
            for string, open_pitch in enumerate(tuning)
            if 0 <= midi - open_pitch <= MAX_FRET and string not in taken
        ]
        cand.sort(key=lambda sf: (sf[1] > 12, sf[1]))
        options.append(cand)
    owner: dict[int, int] = {}  # 줄 → 음 인덱스

    def place(i: int, seen: set[int]) -> bool:
        for string, _ in options[i]:
            if string in seen:
                continue
            seen.add(string)
            if string not in owner or place(owner[string], seen):
                owner[string] = i
                return True
        return False

    # 후보가 적은 음부터 놓으면 더 잘 맞는다
    for i in sorted(range(len(pitches)), key=lambda k: len(options[k])):
        place(i, set())
    result = {}
    for string, i in owner.items():
        result[i] = (string, pitches[i] - tuning[string])
    return result


def _fold_pitch(pitch: int, tuning: list[int]) -> int:
    low, high = min(tuning), max(tuning) + MAX_FRET
    while pitch < low:
        pitch += 12
    while pitch > high:
        pitch -= 12
    return pitch


def _make_track(style: str, track_id: str, name: str, channel: int, track_xml: bytes | None = None) -> ET.Element:
    kind, color, short, _ = _STEM_STYLES.get(style, _DEFAULT_STYLE)
    if track_xml is not None:
        # 다른 GP 파일의 트랙을 통째로 가져온다: 튜닝·악기·사운드·이펙트가 그대로 유지된다
        track = ET.fromstring(track_xml)
        staves = track.find("Staves")
        if staves is not None:
            for extra in list(staves)[1:]:  # 마디는 보표 하나만 만들므로 첫 보표만 남긴다
                staves.remove(extra)
        layout = track.find("SystemsLayout")
        if layout is not None:
            track.remove(layout)
    else:
        track = copy.deepcopy(_load_template(kind))
        track.find("ShortName").text = short
        track.find("Color").text = color
    track.attrib.clear()
    track.set("id", track_id)
    track.find("Name").text = name
    for tag in ("Name", "ShortName"):
        track.find(tag).set(_CD, "1")
    if not _is_drum_set(track):
        conn = track.find("MidiConnection")
        conn.find("PrimaryChannel").text = str(channel)
        conn.find("SecondaryChannel").text = str(channel + 1)
    return track


def _free_channel(root: ET.Element) -> int:
    used = {
        int(c.text)
        for tag in ("PrimaryChannel", "SecondaryChannel")
        for c in root.iterfind(f"Tracks/Track/MidiConnection/{tag}")
        if c.text and c.text.isdigit()
    }
    channel = 0
    while channel in used or channel + 1 in used or 9 in (channel, channel + 1):
        channel += 1
    return channel


def _uses_flats(master: ET.Element, previous: bool) -> bool:
    """마디의 조표가 내림표 계열이면 True (조표가 없으면 앞 마디 것을 따른다)."""
    key = master.find("Key")
    if key is None:
        return previous
    count = int(key.findtext("AccidentalCount", "0") or 0)
    return count < 0 or (count == 0 and key.findtext("TransposeAs") == "Flats")


def _append_bars(
    root: ET.Element,
    ids: _Ids,
    rhythm_refs: dict[int, str],
    sustain: list[set[int]],
    onset: list[set[int]],
    bar_cells: list[int],
    clef: str,
    drum_map: dict[int, int] | None,
    tuning: list[int] | None = None,
    extras: dict[tuple[int, int], dict] | None = None,
    stats: dict[str, int] | None = None,
) -> None:
    stats = stats if stats is not None else {}
    total = len(sustain)
    bar_start = 0
    # 지금 울리는 음 → (줄, 프렛, 드럼 아티큘레이션). 붙임줄로 이어지는 음이 같은 줄을 쓰도록 유지한다
    active: dict[int, tuple[int | None, int | None, int | None]] = {}
    blocked: set[int] = set()
    flats = False
    for master, cells in zip(root.find("MasterBars"), bar_cells):
        flats = _uses_flats(master, flats)
        spelling = _FLAT_SPELLING if flats else _SHARP_SPELLING
        bar, bar_id = ids.new("Bars", "Bar")
        ET.SubElement(bar, "Clef").text = clef
        voice, voice_id = ids.new("Voices", "Voice")
        ET.SubElement(bar, "Voices").text = f"{voice_id} -1 -1 -1"
        bar_end = bar_start + cells
        beat_ids: list[str] = []

        t = bar_start
        while t < bar_end:
            end = t + 1
            while end < bar_end and sustain[end] == sustain[t] and not onset[end]:
                end += 1
            # 2의 거듭제곱 길이만 사용하고 박 위치에 정렬; 남는 부분은 붙임줄로 이어 붙인다
            size = max(s for s in _RHYTHM_NAMES if s <= end - t and (t - bar_start) % s == 0)
            finish = t + size

            beat, beat_id = ids.new("Beats", "Beat")
            beat_ids.append(beat_id)
            ET.SubElement(beat, "Dynamic").text = "MF"
            ET.SubElement(beat, "Rhythm", ref=rhythm_refs[size])

            # 이어지는 음은 이전 배정을 유지하고, 새로 치는 음은 남은 줄에서 고른다
            assigned: dict[int, tuple[int | None, int | None, int | None]] = {}
            taken: set[int] = set()
            pitches = sorted(sustain[t], reverse=True)
            for midi in pitches:
                if midi not in onset[t] and midi in active:
                    assigned[midi] = active[midi]
                    if active[midi][0] is not None:
                        taken.add(active[midi][0])
            still_blocked: set[int] = set()
            for midi in pitches:
                if midi in assigned:
                    continue
                if midi not in onset[t] and midi in blocked:
                    still_blocked.add(midi)
                    continue
                extra = (extras or {}).get((midi, t)) or {}
                string, fret, art = extra.get("string"), extra.get("fret"), extra.get("art")
                if string is None and tuning:
                    pick = _pick_string(midi, tuning, taken)
                    if pick is None:
                        # 줄·프렛이 없는 음은 Guitar Pro가 프렛을 -2147483648로 채워 오디오 엔진을 죽인다: 쓰지 않는다
                        stats["no_string"] = stats.get("no_string", 0) + 1
                        still_blocked.add(midi)
                        continue
                    string, fret = pick
                if drum_map is not None:
                    art = art if art is not None else drum_map.get(midi)
                    if art is None:
                        continue
                if string is not None:
                    taken.add(string)
                assigned[midi] = (string, fret, art)
            active, blocked = assigned, still_blocked

            note_ids: list[str] = []
            for midi in sorted(assigned):
                string, fret, art = assigned[midi]
                note, note_id = ids.new("Notes", "Note")
                note_ids.append(note_id)
                before = t > 0 and midi in sustain[t - 1] and midi not in onset[t]
                after = finish < total and midi in sustain[finish] and midi not in onset[finish]
                if before or after:
                    ET.SubElement(note, "Tie", origin=str(after).lower(), destination=str(before).lower())
                ET.SubElement(note, "InstrumentArticulation").text = str(art if drum_map is not None else 0)
                props = ET.SubElement(note, "Properties")

                def pitch_prop(key: str) -> None:
                    pitch = ET.SubElement(ET.SubElement(props, "Property", name=key), "Pitch")
                    step, accidental = spelling[midi % 12]
                    ET.SubElement(pitch, "Step").text = step
                    ET.SubElement(pitch, "Accidental").text = accidental
                    ET.SubElement(pitch, "Octave").text = str(midi // 12)

                # Guitar Pro가 저장하는 속성 순서(이름 알파벳 순)를 따른다
                pitch_prop("ConcertPitch")
                if fret is not None:
                    ET.SubElement(ET.SubElement(props, "Property", name="Fret"), "Fret").text = str(fret)
                ET.SubElement(ET.SubElement(props, "Property", name="Midi"), "Number").text = str(midi)
                if string is not None:
                    ET.SubElement(ET.SubElement(props, "Property", name="String"), "String").text = str(string)
                pitch_prop("TransposedPitch")
            if note_ids:
                ET.SubElement(beat, "Notes").text = " ".join(note_ids)
            t = finish

        ET.SubElement(voice, "Beats").text = " ".join(beat_ids)
        master.find("Bars").text += f" {bar_id}"
        bar_start = bar_end


def _verify_base_untouched(old: ET.Element, new: ET.Element) -> None:
    """기준 악보의 문서 정보·설정과 기존 내용이 그대로인지 확인한다 (새로 추가한 것은 항상 뒤쪽에만 붙는다)."""
    for tag in ("GPVersion", "GPRevision", "Encoding", "Score", "ScoreViews"):
        before, after = old.find(tag), new.find(tag)
        if before is not None and (after is None or ET.tostring(before) != ET.tostring(after)):
            raise GuitarProError(f"기준 악보의 {tag}가 변경되었습니다 (내부 오류)")
    for tag in ("Tracks", "Bars", "Voices", "Beats", "Notes", "Rhythms"):
        for a, b in zip(old.find(tag), new.find(tag)):
            if ET.tostring(a) != ET.tostring(b):
                raise GuitarProError(f"기존 {tag} 내용이 변경되었습니다 (내부 오류)")
    # 마스터 트랙(템포·이펙트)은 트랙 목록만 늘어난다
    old_master, new_master = copy.deepcopy(old.find("MasterTrack")), copy.deepcopy(new.find("MasterTrack"))
    for m in (old_master, new_master):
        m.find("Tracks").text = ""
    if ET.tostring(old_master) != ET.tostring(new_master):
        raise GuitarProError("기준 악보의 MasterTrack이 변경되었습니다 (내부 오류)")
    # 마디(박자·조표·섹션 등)는 트랙별 Bar id만 늘어난다
    for a, b in zip(old.find("MasterBars"), new.find("MasterBars")):
        ids_old = a.findtext("Bars", "").split()
        if b.findtext("Bars", "").split()[: len(ids_old)] != ids_old:
            raise GuitarProError("기준 악보의 마디가 변경되었습니다 (내부 오류)")


# ---- 복구 ------------------------------------------------------------------

INT_MIN = -2147483648  # Guitar Pro가 줄·프렛을 정하지 못한 음에 채우는 프렛 값


def _staff_index(root: ET.Element, track_index: int) -> int:
    n = 0
    for i, track in enumerate(root.find("Tracks")):
        if i == track_index:
            return n
        n += max(1, len(track.findall("Staves/Staff")))
    raise GuitarProError(f"트랙 번호가 범위를 벗어났습니다: {track_index}")


def _set_note_string_fret(note: ET.Element, string: int, fret: int) -> None:
    props = note.find("Properties")
    for prop in list(props):
        if prop.get("name") in ("String", "Fret"):
            props.remove(prop)
    midi_at = next(i for i, p in enumerate(props) if p.get("name") == "Midi")
    fret_prop = ET.Element("Property", name="Fret")
    ET.SubElement(fret_prop, "Fret").text = str(fret)
    string_prop = ET.Element("Property", name="String")
    ET.SubElement(string_prop, "String").text = str(string)
    props.insert(midi_at, fret_prop)  # Guitar Pro의 속성 순서: ConcertPitch, Fret, Midi, String, TransposedPitch
    props.insert(midi_at + 2, string_prop)


def _referenced_ids(root: ET.Element) -> dict[str, set[str]]:
    """마디에서 출발해 실제로 도달하는 Bars/Voices/Beats/Notes id (어디에서도 쓰이지 않는 고아 요소는 제외)."""
    elements = {tag: {x.get("id"): x for x in root.find(tag)} for tag in ("Bars", "Voices", "Beats", "Notes")}
    refs: dict[str, set[str]] = {tag: set() for tag in elements}
    for master in root.find("MasterBars"):
        refs["Bars"].update(master.findtext("Bars", "").split())
    for bar_id in refs["Bars"]:
        bar = elements["Bars"].get(bar_id)
        if bar is not None:
            refs["Voices"].update(v for v in bar.findtext("Voices", "").split() if v != "-1")
    for voice_id in refs["Voices"]:
        voice = elements["Voices"].get(voice_id)
        if voice is not None:
            refs["Beats"].update(voice.findtext("Beats", "").split())
    for beat_id in refs["Beats"]:
        beat = elements["Beats"].get(beat_id)
        if beat is not None:
            refs["Notes"].update(beat.findtext("Notes", "").split())
    return refs


def repair_playable(gp_bytes: bytes, *, only_broken: bool = False) -> tuple[bytes, list[str]]:
    """건반·관악·보컬 같은 비기타 트랙의 음마다 유효한 줄·프렛을 다시 배정하고 튜닝을 음역에 맞춰 넓힌다.

    줄·프렛이 없거나 프렛이 -2147483648인 음이 있으면, 조표를 바꾸거나(조옮김) 재생할 때 Guitar Pro가 비정상 종료된다.
    기타·베이스·드럼 트랙과 악보의 나머지 내용은 건드리지 않는다. only_broken이면 문제가 있는 트랙만 고친다.

    Guitar Pro는 같은 음이 여러 트랙에 있으면 하나의 Note 요소를 공유해 저장한다. 그래서 고칠 트랙의 마디·성부·박·음을
    먼저 복제해 독립시킨 뒤 수정한다(그러지 않으면 다른 트랙의 줄·프렛이 덮어써진다).
    """
    zf, root = _read_score(gp_bytes)
    before = _referenced_ids(root)
    lookup = {tag: {x.get("id"): x for x in root.find(tag)} for tag in ("Bars", "Voices", "Beats", "Notes")}
    ids = _Ids(root)

    def clone(tag: str, element: ET.Element) -> ET.Element:
        new = copy.deepcopy(element)
        new.set("id", str(ids.next[tag]))
        ids.next[tag] += 1
        root.find(tag).append(new)
        return new

    def midi_of(note: ET.Element) -> int:
        return int(note.find("Properties/Property[@name='Midi']/Number").text)

    report: list[str] = []
    for ti, track in enumerate(root.find("Tracks")):
        if _is_drum_set(track) or _is_fretted(track) or _staff_tuning(track) is None:
            continue
        staff = _staff_index(root, ti)

        # 이 트랙이 쓰는 음들을 먼저 읽어 문제가 있는지 본다
        old_notes: list[ET.Element] = []
        for master in root.find("MasterBars"):
            ids_list = master.findtext("Bars", "").split()
            bar = lookup["Bars"].get(ids_list[staff]) if staff < len(ids_list) else None
            for vid in (bar.findtext("Voices", "").split() if bar is not None else []):
                if vid == "-1":
                    continue
                for bid in lookup["Voices"][vid].findtext("Beats", "").split():
                    old_notes += [lookup["Notes"][n] for n in lookup["Beats"][bid].findtext("Notes", "").split()]
        if not old_notes:
            continue
        pitches = [midi_of(n) for n in old_notes]
        tuning = _staff_tuning(track)
        low, high = min(tuning), max(tuning) + MAX_FRET
        broken = 0
        for n in old_notes:
            props = {p.get("name"): p for p in n.findall("Properties/Property")}
            fret = props.get("Fret")
            if "String" not in props or fret is None or int(fret.findtext("Fret")) == INT_MIN or not low <= midi_of(n) <= high:
                broken += 1
        if only_broken and not broken:
            continue

        new_tuning = _wide_tuning(min(pitches), max(pitches))
        _set_tuning(track, new_tuning)

        carried: dict[int, tuple[int, int]] = {}  # 붙임줄로 이어지는 음은 앞 음과 같은 줄·프렛
        shared = 0
        count = 0
        for master in root.find("MasterBars"):
            ids_list = master.findtext("Bars", "").split()
            if staff >= len(ids_list):
                continue
            old_bar = lookup["Bars"][ids_list[staff]]
            new_bar = clone("Bars", old_bar)
            ids_list[staff] = new_bar.get("id")
            master.find("Bars").text = " ".join(ids_list)
            new_voice_ids = []
            for vid in old_bar.findtext("Voices", "").split():
                if vid == "-1":
                    new_voice_ids.append(vid)
                    continue
                new_voice = clone("Voices", lookup["Voices"][vid])
                new_voice_ids.append(new_voice.get("id"))
                new_beat_ids = []
                for bid in lookup["Voices"][vid].findtext("Beats", "").split():
                    new_beat = clone("Beats", lookup["Beats"][bid])
                    new_beat_ids.append(new_beat.get("id"))
                    notes_el = new_beat.find("Notes")
                    old_ids = notes_el.text.split() if notes_el is not None and notes_el.text else []
                    notes = [clone("Notes", lookup["Notes"][n]) for n in old_ids]
                    if not notes:
                        continue
                    notes_el.text = " ".join(n.get("id") for n in notes)

                    taken: set[int] = set()
                    assigned: dict[int, tuple[int, int]] = {}
                    for n in notes:
                        tie = n.find("Tie")
                        m = midi_of(n)
                        if tie is not None and tie.get("destination") == "true" and m in carried and carried[m][0] not in taken:
                            assigned[id(n)] = carried[m]
                            taken.add(carried[m][0])
                    free = [n for n in notes if id(n) not in assigned]
                    matched = _match_strings([midi_of(n) for n in free], new_tuning, taken)
                    for k, n in enumerate(free):
                        if k in matched:
                            assigned[id(n)] = matched[k]
                        else:  # 줄이 정말 모자라는 드문 경우: 줄을 공유한다(음은 지우지 않는다)
                            assigned[id(n)] = _pick_string(midi_of(n), new_tuning, set())
                            shared += 1
                    for n in notes:
                        string, fret = assigned[id(n)]
                        _set_note_string_fret(n, string, fret)
                        carried[midi_of(n)] = (string, fret)
                        count += 1
                new_voice.find("Beats").text = " ".join(new_beat_ids)
            new_bar.find("Voices").text = " ".join(new_voice_ids)
        report.append(
            f"{ti + 1}번 '{track.findtext('Name', '')}': 음 {count}개 중 줄·프렛 문제 {broken}개 → "
            f"튜닝 {' '.join(map(str, new_tuning))}로 넓히고 전부 다시 배정" + (f" (줄 공유 {shared})" if shared else "")
        )

    # 복제하면서 아무도 쓰지 않게 된 옛 마디·성부·박·음은 지운다 (줄·프렛이 잘못된 음이 남지 않게)
    after = _referenced_ids(root)
    for tag, old_ids in before.items():
        for element in list(root.find(tag)):
            eid = element.get("id")
            if eid in old_ids and eid not in after[tag]:
                root.find(tag).remove(element)

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dest:
        for info in zf.infolist():
            data = zf.read(info.filename)
            if info.filename == SCORE_ENTRY:
                data = serialize_gpif(root)
            dest.writestr(info, data)
    return out.getvalue(), report


# ---- 공개 API ---------------------------------------------------------------


@dataclass
class TrackSpec:
    """GP에 추가할 트랙 하나. notes는 (pitch, 시작 칸, 끝 칸)이며 칸 0은 악보 첫 마디 첫 박(1/16 격자)."""

    key: str  # 결과 딕셔너리 키 (스템 이름 등)
    name: str  # GP에 표시할 트랙 이름
    style: str  # _STEM_STYLES 키 (vocals/guitar/bass/piano/other/drums)
    notes: list[tuple[int, int, int]]
    is_drum: bool = False
    # 다른 GP 파일에서 온 트랙이면 그 트랙의 XML과 음별 (줄, 프렛, 드럼 아티큘레이션) — 튜닝·악기를 그대로 유지한다
    track_xml: bytes | None = None
    extras: dict[tuple[int, int], dict] | None = None


def gp_bar_cells(gp_bytes: bytes) -> list[int]:
    """마디별 16분음표 칸 수 (4/4 → 16)."""
    return _bar_cells(_read_score(gp_bytes)[1])


def add_tracks_to_gp(gp_bytes: bytes, specs: list[TrackSpec]) -> AddStemsResult:
    """specs 각각을 gp_bytes 악보의 새 트랙으로 추가한 .gp 바이트를 반환. 기존 내용은 건드리지 않는다."""
    if not specs:
        raise GuitarProError("추가할 트랙이 없습니다")

    zf, root = _read_score(gp_bytes)
    old_root = copy.deepcopy(root)
    tracks = root.find("Tracks")
    bar_cells = _bar_cells(root)
    total_cells = sum(bar_cells)

    ids = _Ids(root)
    rhythm_refs: dict[int, str] = {}
    for size, name in _RHYTHM_NAMES.items():
        rhythm, rhythm_id = ids.new("Rhythms", "Rhythm")
        ET.SubElement(rhythm, "NoteValue").text = name
        rhythm_refs[size] = rhythm_id

    result = AddStemsResult(data=b"", added_tracks=[])
    next_track_id = max((int(t.get("id")) for t in tracks), default=-1) + 1

    base_tracks = list(tracks)
    for spec in specs:
        style = "drums" if spec.is_drum else spec.style
        track = _make_track(style, str(next_track_id), spec.name, _free_channel(root), spec.track_xml)
        next_track_id += 1

        notes = spec.notes
        tuning = _staff_tuning(track)
        if tuning and spec.track_xml is None:
            # 템플릿 악기: 기준 악보의 같은 계열 악기가 다른 튜닝(드롭 D 등)이면 그 튜닝을 따른다
            base = _base_tuning(base_tracks, track, len(tuning))
            if base and base != tuning:
                _set_tuning(track, base)
                tuning = base
        fretted = _is_fretted(track)
        if tuning and not spec.extras and not fretted and notes and not spec.is_drum:
            # 음을 옥타브 옮기지 않고, 대신 튜닝을 음역에 맞춰 넓힌다
            tuning = _wide_tuning(min(p for p, _, _ in notes), max(p for p, _, _ in notes))
            _set_tuning(track, tuning)
        if tuning and not spec.extras and fretted:
            folded = [(_fold_pitch(p, tuning), a, b) for p, a, b in notes]
            moved = sum(1 for x, y in zip(folded, notes) if x[0] != y[0])
            if moved:
                result.warnings.append(f"{spec.key}: 악기 음역 밖의 음 {moved}개를 옥타브 이동해 맞춤")
            notes = folded

        sustain, onset, written, dropped = _to_grid(notes, total_cells=total_cells, one_cell_notes=spec.is_drum)

        drum_map: dict[int, int] | None = None
        if spec.is_drum:
            drum_map = _drum_articulations(track)
            known = set(drum_map) | {p for (p, _a) in (spec.extras or {})}
            unmapped = {pitch for pitch, _, _ in spec.notes} - known
            if unmapped:
                result.warnings.append(f"{spec.key}: 드럼 키트에 없는 MIDI 번호 {sorted(unmapped)}는 건너뜀")
        tracks.append(track)
        root.find("MasterTrack/Tracks").text += f" {track.get('id')}"

        stats: dict[str, int] = {}
        clef = _STEM_STYLES.get(style, _DEFAULT_STYLE)[3]
        _append_bars(
            root, ids, rhythm_refs, sustain, onset, bar_cells, clef, drum_map, tuning, spec.extras, stats
        )
        if stats.get("no_string"):
            result.warnings.append(f"{spec.key}: 줄·프렛을 배정할 수 없는 음(한 박에 동시에 너무 많은 음) {stats['no_string']}개를 건너뜀")
        result.added_tracks.append(spec.name)
        result.notes_written[spec.key] = written
        result.notes_dropped[spec.key] = dropped
        if dropped:
            result.warnings.append(f"{spec.key}: 악보 범위(오프셋 이전/마지막 마디 이후)를 벗어난 음 {dropped}개를 버림")
        if not written:
            result.warnings.append(f"{spec.key}: 악보 범위 안에 들어온 음이 없습니다 (BPM/오프셋을 확인하세요)")

    _verify_base_untouched(old_root, root)

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dest:
        for info in zf.infolist():
            # 제목·가수·페이지 설정·템포·마디 배치(ScoreViews)·스타일시트 등 문서 설정은 모두 기준 .gp 그대로 둔다.
            # 트랙만 추가하고 마디 수는 그대로라서 이 항목들을 다시 만들 필요가 없다.
            data = zf.read(info.filename)
            if info.filename == SCORE_ENTRY:
                data = serialize_gpif(root)
            dest.writestr(info, data)
    result.data = out.getvalue()
    return result


def add_stems_to_gp(
    gp_bytes: bytes,
    stem_midis: dict[str, Path],
    *,
    tempo_bpm: float | None = None,
    offset_sec: float = 0.0,
) -> AddStemsResult:
    """gp_bytes의 악보에 stem_midis(스템 이름 → MIDI 경로) 각각을 새 트랙으로 추가한 .gp 바이트를 반환.

    tempo_bpm이 없으면 악보의 첫 템포를 쓴다. offset_sec는 MIDI 0초가 악보 첫 박보다 얼마나 늦은지(초)이다.
    """
    if not stem_midis:
        raise GuitarProError("추가할 스템이 선택되지 않았습니다")
    tempo = tempo_bpm if tempo_bpm else _initial_tempo(_read_score(gp_bytes)[1])
    if tempo <= 0:
        raise GuitarProError("BPM은 0보다 커야 합니다")

    cells_per_sec = tempo / 60.0 * CELLS_PER_BEAT
    specs = []
    for stem, midi_path in stem_midis.items():
        midi = pretty_midi.PrettyMIDI(str(midi_path))
        notes = [
            (n.pitch, round((n.start - offset_sec) * cells_per_sec), round((n.end - offset_sec) * cells_per_sec))
            for inst in midi.instruments
            for n in inst.notes
        ]
        specs.append(
            TrackSpec(
                key=stem,
                name=f"{stem} — SongSplit",
                style=stem,
                notes=notes,
                is_drum=any(inst.is_drum for inst in midi.instruments),
            )
        )
    return add_tracks_to_gp(gp_bytes, specs)


def resolve_stem_midis(job: Job) -> dict[str, Path]:
    """Guitar Pro에 넣을 수 있는 스템별 MIDI. 후처리 결과(04)가 있으면 그것을, 없으면 원본 변환(03)을 쓴다."""
    midi_entry = job.get_stage(Stage.MIDI)
    if not midi_entry or midi_entry["status"] != StageStatus.DONE.value:
        return {}
    edited_entry = job.get_stage(Stage.EDITED_MIDI)
    edited = (
        edited_entry["outputs"] if edited_entry and edited_entry["status"] == StageStatus.DONE.value else {}
    )
    return {stem: Path(edited.get(stem, path)) for stem, path in midi_entry["outputs"].items()}


def build_guitarpro(
    job: Job,
    gp_bytes: bytes,
    source_filename: str,
    stems: list[str],
    *,
    tempo_bpm: float | None = None,
    offset_sec: float = 0.0,
) -> Path:
    """선택한 스템을 업로드한 Guitar Pro 악보에 트랙으로 추가해 06_guitarpro/에 저장하고 manifest에 기록."""
    params = {
        "source_filename": source_filename,
        "stems": stems,
        "tempo_bpm": tempo_bpm,
        "offset_sec": offset_sec,
    }
    job.update_stage(Stage.GUITARPRO, StageStatus.RUNNING, params=params)
    try:
        available = resolve_stem_midis(job)
        missing = [s for s in stems if s not in available]
        if missing:
            raise GuitarProError(f"MIDI가 없는 스템입니다: {', '.join(missing)}. 먼저 MIDI 변환을 완료하세요")
        result = add_stems_to_gp(
            gp_bytes, {s: available[s] for s in stems}, tempo_bpm=tempo_bpm, offset_sec=offset_sec
        )
        out_dir = job.stage_dir(Stage.GUITARPRO)
        out_path = out_dir / f"{Path(source_filename).stem}_songsplit.gp"
        out_path.write_bytes(result.data)
    except Exception as exc:
        job.update_stage(Stage.GUITARPRO, StageStatus.ERROR, params=params, error=str(exc))
        raise

    job.update_stage(
        Stage.GUITARPRO,
        StageStatus.DONE,
        params=params,
        outputs={
            "gp_path": str(out_path),
            "added_tracks": result.added_tracks,
            "notes_written": result.notes_written,
            "notes_dropped": result.notes_dropped,
            "warnings": result.warnings,
        },
    )
    return out_path
