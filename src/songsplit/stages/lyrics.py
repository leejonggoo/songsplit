"""[9] Lyrics: 가사 텍스트(또는 LRC)를 읽어 Guitar Pro(.gp)의 보컬 트랙 음표에 맞춰 넣는다.

흐름: parse_lyrics()로 줄 단위 읽기 → syllabify()로 음절 분리(한글은 글자 단위, 영어는 하이픈/간이 규칙) →
보컬 트랙의 음표 박(슬롯)을 구절 단위로 묶어 가사 줄과 순서를 지키며 맞춤(DP) → 박마다 음절 기록.
Guitar Pro는 트랙의 가사 원문(Lyrics/Line/Text, 시작 마디 Offset)과 박별 음절(Beat/Lyrics)을 함께 저장하므로 둘 다 쓴다.
붙임줄로 이어지는 박은 가사를 받지 않는다(Guitar Pro와 같은 규칙).
"""

from __future__ import annotations

import copy
import io
import re
import statistics
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from songsplit.stages.guitarpro import _CD, SCORE_ENTRY, GuitarProError, _is_drum_set, _read_score, serialize_gpif
from songsplit.stages.score_merge import _rhythm_beats

LRC_TIME = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
HANGUL = re.compile(r"[가-힣ㄱ-ㆎ]")
VOWELS = "aeiouy"
DIGRAPHS = ("th", "ch", "sh", "ph", "wh", "ck", "ng", "gh", "qu")
SKIP_LINE_COST = 1.2
LYRIC_LINES_PER_TRACK = 5  # GPIF의 Lyrics는 항상 5줄(Line)


# ---- 가사 읽기 / 음절 분리 ----------------------------------------------------


@dataclass
class LyricLine:
    text: str
    time_sec: float | None = None


@dataclass
class Syllable:
    text: str
    new_word: bool = True  # 단어의 첫 음절
    joins_next: bool = False  # 같은 영어 단어가 다음 음절로 이어짐 (박 표기에 하이픈이 붙는다)
    hangul: bool = False


def parse_lyrics(text: str) -> list[LyricLine]:
    """줄 단위로 읽는다. [mm:ss.xx] 시각(LRC)은 줄 시작 시각으로, [Verse] 같은 구간 표시와 LRC 메타 태그는 버린다."""
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stamps = LRC_TIME.findall(raw)
        body = LRC_TIME.sub("", raw).strip()
        if re.fullmatch(r"\[[^\]]*\]", body):
            continue  # [Verse 1], [ti:제목] 등
        if not body:
            continue
        time_sec = int(stamps[0][0]) * 60 + float(stamps[0][1]) if stamps else None
        lines.append(LyricLine(body, time_sec))
    return lines


def _split_english(word: str) -> list[str]:
    """영어 단어를 음절로 나누는 간이 규칙(정확하지 않다: 필요하면 'ev-ery'처럼 직접 하이픈을 넣는다)."""
    match = re.match(r"^([^A-Za-z]*)([A-Za-z']+)(.*)$", word)
    if not match:
        return [word]
    pre, core, post = match.groups()
    low = core.lower()
    # 모음 덩어리 위치
    groups = [m.span() for m in re.finditer(rf"[{VOWELS}]+", low)]
    if len(groups) <= 1:
        return [word]
    cuts = []
    for (_, end_a), (start_b, _) in zip(groups, groups[1:]):
        cons = low[end_a:start_b]
        if not cons:
            cuts.append(end_a)
        elif len(cons) == 1 or cons in DIGRAPHS:
            cuts.append(end_a)
        else:
            # 자음 둘 이상: 첫 자음 뒤에서 자르되 이중자음(th 등)으로 시작하면 그 앞에서 자른다
            cuts.append(end_a if cons[:2] in DIGRAPHS else end_a + 1)
    pieces = []
    prev = 0
    for cut in cuts:
        pieces.append(core[prev:cut])
        prev = cut
    pieces.append(core[prev:])
    # 묵음 e / -ed / -es 는 앞 음절에 붙인다
    if len(pieces) > 1:
        last = pieces[-1].lower()
        silent_e = re.fullmatch(r"[^aeiouy]+e[sd]?", last) and not re.fullmatch(r"[^aeiouy]*le[sd]?", last)
        if last in ("e", "es") or silent_e or (last == "ed" and pieces[-2][-1:].lower() not in ("t", "d")):
            tail = pieces.pop()
            pieces[-1] += tail
    pieces[0] = pre + pieces[0]
    pieces[-1] += post
    return pieces


def syllabify(line: str, *, split_english: bool = True) -> list[Syllable]:
    out: list[Syllable] = []
    for word in line.split():
        if HANGUL.search(word):
            # 한글: 글자 하나가 음절 하나, 뒤따르는 문장부호는 앞 글자에 붙인다. 한글이 아닌 덩어리는 따로 둔다
            first = True
            for chunk in re.findall(rf"{HANGUL.pattern}[^\s가-힣ㄱ-ㆎ]*|[^\s가-힣ㄱ-ㆎ]+", word):
                out.append(Syllable(chunk, new_word=first, hangul=bool(HANGUL.match(chunk))))
                first = False
            continue
        if "-" in word.strip("-") and word.strip("-"):
            parts = [p for p in word.split("-") if p]  # 직접 지정한 음절 구분을 그대로 따른다
        elif split_english:
            parts = _split_english(word)
        else:
            parts = [word]
        for i, part in enumerate(parts):
            out.append(Syllable(part, new_word=i == 0, joins_next=i < len(parts) - 1))
    return out


# ---- 보컬 트랙의 음표 박 ------------------------------------------------------


@dataclass
class Slot:
    beat: ET.Element
    bar: int
    start: float  # 곡 시작부터의 박
    duration: float
    eligible: bool  # 음절을 실을 수 있는 박: 음이 있고 붙임줄로 이어지는 박이 아님 (줄 맞춤은 이 박들만 쓴다)
    has_notes: bool = True  # 음이 있는 박. Guitar Pro는 붙임줄 도착 박까지 포함해 모든 음표 박에 순서대로 토큰을 배정한다


def _lookup(root: ET.Element) -> dict[str, dict[str, ET.Element]]:
    return {tag: {x.get("id"): x for x in root.find(tag)} for tag in ("Bars", "Voices", "Beats", "Notes", "Rhythms")}


def _staff_of_track(root: ET.Element, track_index: int) -> int:
    n = 0
    for i, track in enumerate(root.find("Tracks")):
        if i == track_index:
            return n
        n += max(1, len(track.findall("Staves/Staff")))
    raise GuitarProError(f"트랙 번호가 범위를 벗어났습니다: {track_index}")


def track_slots(root: ET.Element, track_index: int) -> list[Slot]:
    lk = _lookup(root)
    rhythm = {rid: _rhythm_beats(r) for rid, r in lk["Rhythms"].items()}
    staff = _staff_of_track(root, track_index)
    slots: list[Slot] = []
    pos = 0.0
    for bar_index, master in enumerate(root.find("MasterBars")):
        num, den = (int(x) for x in master.findtext("Time", "4/4").split("/"))
        bar_len = 4.0 * num / den
        ids = master.findtext("Bars", "").split()
        bar = lk["Bars"].get(ids[staff]) if staff < len(ids) else None
        voice_ids = [v for v in (bar.findtext("Voices", "").split() if bar is not None else []) if v != "-1"]
        if voice_ids:
            t = pos
            for bid in lk["Voices"][voice_ids[0]].findtext("Beats", "").split():
                beat = lk["Beats"][bid]
                dur = float(rhythm[beat.find("Rhythm").get("ref")])
                note_ids = beat.findtext("Notes", "").split()
                tied = bool(note_ids) and all(
                    lk["Notes"][n].find("Tie") is not None and lk["Notes"][n].find("Tie").get("destination") == "true"
                    for n in note_ids
                )
                slots.append(Slot(beat, bar_index, t, dur, bool(note_ids) and not tied, bool(note_ids)))
                t += dur
        pos += bar_len
    return slots


def _slot_pitches(lk: dict[str, dict[str, ET.Element]], slot: Slot) -> list[int]:
    pitches = []
    for note_id in slot.beat.findtext("Notes", "").split():
        num = lk["Notes"][note_id].find("Properties/Property[@name='Midi']/Number")
        if num is not None and num.text:
            pitches.append(int(num.text))
    return pitches


def list_tracks(gp_bytes: bytes) -> list[dict]:
    """가사를 넣을 트랙 후보: 이름, 드럼 여부, 보컬로 보이는지."""
    _, root = _read_score(gp_bytes)
    out = []
    for i, track in enumerate(root.find("Tracks")):
        name = track.findtext("Name", "")
        low = name.lower()
        vocal = any(k in low for k in ("vocal", "voice", "vox", "sing", "보컬", "노래", "lead v")) or bool(
            track.findtext("InstrumentSet/Type") in ("voice", "vocals")
        )
        is_drum = _is_drum_set(track)
        notes = 0 if is_drum else sum(1 for slot in track_slots(root, i) if slot.eligible)
        out.append({"index": i, "name": name, "is_drum": is_drum, "looks_vocal": vocal, "notes": notes})
    return out


def gp_tokens(text: str) -> list[str]:
    """Guitar Pro가 가사 텍스트를 박 하나에 하나씩 나눠 넣는 토큰으로 쪼갠다.

    공백으로 나누고, 하이픈으로 끝나는 음절 뒤의 음절은 다음 박(예: 'to-o' → 'to-', 'o'), '+'로 묶인 것은 한 박에 공백으로 표시,
    '_'는 글자 그대로 한 박을 차지하는 토큰이다.
    """
    out: list[str] = []
    for tk in text.split():
        if "+" in tk:
            out.append(tk.replace("+", " "))
        elif "-" in tk and tk != "-":
            out.extend(re.findall(r"[^-]+-?|-", tk))
        else:
            out.append(tk)
    return out


def simulate_gp_dispatch(root: ET.Element, track_index: int, text: str, offset_bar: int) -> list[tuple[Slot, str | None]]:
    """사용자가 Guitar Pro에서 가사를 고쳤을 때 Guitar Pro가 하는 일을 재현한다.

    시작 마디(offset_bar, 0부터) 이후의 음표 박 — 붙임줄 도착 박 포함 — 에 토큰을 처음부터 차례로 하나씩 배정한다.
    실제 Guitar Pro 파일 여러 개로 확인한 규칙이다. 반환: [(박, 배정된 글자 또는 None)].
    """
    note_slots = [s for s in track_slots(root, track_index) if s.has_notes and s.bar >= offset_bar]
    tokens = gp_tokens(text)
    return [(slot, tokens[k] if k < len(tokens) else None) for k, slot in enumerate(note_slots)]


# ---- 맞춤 --------------------------------------------------------------------


def _match_cost(m: int, n: int) -> float:
    return abs(m - n) / max(m, n)


def _eligible_with_gaps(slots: list[Slot]) -> tuple[list[Slot], list[float]]:
    """가사를 받을 수 있는 슬롯과, 각 슬롯 직전의 쉬는 길이(박). 첫 슬롯은 아주 큰 값."""
    eligible = [s for s in slots if s.eligible]
    gaps = []
    prev_end = None
    for s in eligible:
        gaps.append(1e9 if prev_end is None else s.start - prev_end)
        prev_end = s.start + s.duration
    return eligible, gaps


def _boundary_cost(gap: float) -> float:
    """줄 경계가 숨 쉬는 자리(쉼) 근처일수록 싸다."""
    if gap >= 1.5:
        return 0.0
    if gap >= 0.5:
        return 0.2
    return 0.5


def _align_lines(
    counts: list[int],
    eligible: list[Slot],
    gaps: list[float],
    line_times: list[float | None] | None,
    bpm: float,
    offset_sec: float,
    *,
    activity: list[float] | None = None,
    audio_gaps: list[float] | None = None,
    slot_secs: list[float] | None = None,
) -> list[tuple[int, int, int]]:
    """가사 줄을 음표 슬롯의 연속 구간에 순서대로 대응시키는 최소 비용 경로. [(줄, 시작 슬롯, 슬롯 수)] 반환.

    비용 = 음절 수와 슬롯 수의 차이 + 줄이 쉼 없는 곳에서 시작/끝나는 벌점(+ LRC 시각과의 거리).
    줄 앞의 슬롯 몇 개(잡음·간주)는 건너뛸 수 있고, 멜로디를 못 찾은 줄은 비싼 값으로 건너뛴다.

    음원 정보가 있으면(activity·audio_gaps) 실제로 노래가 불리는 음표는 건너뛰기 어렵게, 무음 구간 음표는
    쉽게 건너뛰게 하고, 줄 경계는 실제로 숨을 쉬는 자리에 놓이게 한다.
    """
    L, N = len(counts), len(eligible)
    INF = float("inf")
    audio = activity is not None and audio_gaps is not None
    MAX_SKIP = 40 if audio else 10
    f = [[INF] * (N + 1) for _ in range(L + 1)]
    back: list[list[tuple | None]] = [[None] * (N + 1) for _ in range(L + 1)]
    f[0][0] = 0.0
    # 건너뛴 슬롯 비용과 줄 안의 무음 슬롯 비용을 구간합으로 계산한다
    skip_prefix = [0.0]
    silent_prefix = [0.0]
    for k in range(N):
        skip_prefix.append(skip_prefix[-1] + (0.04 + 0.5 * activity[k] if audio else 0.12))
        silent_prefix.append(silent_prefix[-1] + (1.0 - activity[k] if audio else 0.0))
    boundary = (lambda k: _boundary_cost(audio_gaps[k])) if audio else (lambda k: _boundary_cost(gaps[k]))

    for i in range(L + 1):
        for p in range(N + 1):
            here = f[i][p]
            if here == INF or i == L:
                continue
            m = counts[i]
            if here + SKIP_LINE_COST < f[i + 1][p]:
                f[i + 1][p], back[i + 1][p] = here + SKIP_LINE_COST, ("skip_line", i, p)
            lo, hi = max(1, int(m * 0.6)), max(2, int(m * 1.6) + 1)
            for q in range(p, min(N, p + MAX_SKIP + 1)):
                if audio and activity[q] < 0.2:
                    continue  # 줄은 실제로 노래가 불리는 음표에서 시작한다
                skip_cost = skip_prefix[q] - skip_prefix[p]
                start_cost = boundary(q)
                time_cost = 0.0
                if line_times and line_times[i] is not None:
                    sec = slot_secs[q] if slot_secs else eligible[q].start * 60.0 / bpm
                    time_cost = 0.6 * min(1.0, abs(line_times[i] - (sec + offset_sec)) / 6.0)
                for n in range(lo, hi + 1):
                    end = q + n
                    if end > N:
                        break
                    end_cost = boundary(end) if end < N else 0.0
                    silent_cost = 0.6 * (silent_prefix[end] - silent_prefix[q]) / n if audio else 0.0
                    c = here + skip_cost + start_cost + end_cost + time_cost + silent_cost + 1.5 * _match_cost(m, n)
                    if c < f[i + 1][end]:
                        f[i + 1][end], back[i + 1][end] = c, ("match", i, p, q, n)
    best_p = min(range(N + 1), key=lambda p: f[L][p])
    pairs: list[tuple[int, int, int]] = []
    i, p = L, best_p
    while (i, p) != (0, 0):
        step = back[i][p]
        if step is None:
            break
        if step[0] == "match":
            _, li, prev_p, q, n = step
            pairs.append((li, q, n))
            i, p = li, prev_p
        else:
            i, p = step[1], step[2]
    return pairs[::-1]


def _group_syllables(syllables: list[Syllable], n: int) -> list[list[Syllable]]:
    """음절 m개를 슬롯 n개에 연속 묶음으로 나눈다 (m ≥ n). 단어 경계에서 자르는 쪽을 선호한다."""
    m = len(syllables)
    cuts = [round(m * g / n) for g in range(1, n)]
    for idx, cut in enumerate(cuts):
        lo = cuts[idx - 1] + 1 if idx else 1
        hi = cuts[idx + 1] - 1 if idx + 1 < len(cuts) else m - 1
        for cand in (cut, cut - 1, cut + 1):
            if lo <= cand <= hi and syllables[cand].new_word:
                cuts[idx] = cand
                break
    bounds = [0, *cuts, m]
    return [syllables[a:b] for a, b in zip(bounds, bounds[1:])]


def _assign(syllables: list[Syllable], slots: list[Slot]) -> list[tuple[Slot, list[Syllable]]]:
    m, n = len(syllables), len(slots)
    if m == 0:
        return []
    if m >= n:
        return list(zip(slots, _group_syllables(syllables, n)))
    # 음절이 더 적으면 긴 음표(와 구절 첫 음표)를 우선해 음절을 받을 슬롯을 고른다
    order = sorted(range(1, n), key=lambda i: (-slots[i].duration, i))[: m - 1]
    chosen = sorted([0, *order])
    return [(slots[i], [syllables[k]]) for k, i in enumerate(chosen)]


def _group_text(group: list[Syllable]) -> tuple[str, bool]:
    """묶음 하나의 표시 문자열과, 다음 박으로 같은 단어가 이어지는지."""
    text = ""
    for k, syl in enumerate(group):
        if k and syl.new_word:
            text += " "
        text += syl.text
    return text, group[-1].joins_next


# ---- 결과 ---------------------------------------------------------------------


@dataclass
class LineReport:
    text: str
    first_bar: int | None  # 1부터 세는 마디 번호. None이면 맞추지 못함
    last_bar: int | None
    syllables: int
    slots: int
    start_sec: float | None = None  # 음원 기준 줄 시작 시각(초). 음원을 쓴 경우에만


@dataclass
class LyricsResult:
    data: bytes
    lines: list[LineReport] = field(default_factory=list)
    placed: int = 0
    unplaced_lines: list[str] = field(default_factory=list)
    skipped_slots: int = 0  # 어느 가사 줄에도 쓰이지 않은 음표 박 수 (잡음·간주·가사 없는 구간)
    warnings: list[str] = field(default_factory=list)
    offset_bar: int = 0
    filler_beats: int = 0  # 음절 없이 '_'로 채운 박 (붙임줄로 이어진 박, 건너뛴 박)


def add_lyrics_to_gp(
    gp_bytes: bytes,
    track_index: int,
    lyrics: str,
    *,
    mode: str = "smart",  # "smart": 줄을 음표 구절에 맞춤, "sequential": 첫 음표부터 순서대로
    start_bar: int | None = None,  # 1부터 세는 마디. 이 마디 이후의 음표부터 사용
    tempo_bpm: float | None = None,
    time_offset_sec: float | None = None,  # LRC 시각 - 악보 시각 (None이면 자동 추정)
    split_english: bool = True,
    audio=None,  # audio_sync.AudioGuide — 실제 음원 기준으로 줄 위치를 맞춘다 (smart 모드)
) -> LyricsResult:
    zf, root = _read_score(gp_bytes)
    tracks = root.find("Tracks")
    if not 0 <= track_index < len(tracks):
        raise GuitarProError(f"트랙 번호가 범위를 벗어났습니다: {track_index}")
    if _is_drum_set(tracks[track_index]):
        raise GuitarProError("드럼 트랙에는 가사를 넣을 수 없습니다")

    parsed = parse_lyrics(lyrics)
    lines = [(ln, syllabify(ln.text, split_english=split_english)) for ln in parsed]
    lines = [(ln, syl) for ln, syl in lines if syl]
    if not lines:
        raise GuitarProError("가사가 비어 있습니다")

    slots = track_slots(root, track_index)
    if start_bar:
        slots = [s for s in slots if s.bar >= start_bar - 1]
    if not any(s.eligible for s in slots):
        raise GuitarProError("이 트랙에는 가사를 넣을 음표가 없습니다")

    result = LyricsResult(data=b"")
    placements: list[tuple[Slot, list[Syllable], int]] = []  # (슬롯, 음절 묶음, 가사 줄 번호)
    reports: dict[int, LineReport] = {}

    if mode == "sequential":
        flat = [s for s in slots if s.has_notes]  # Guitar Pro와 같게: 붙임줄 도착 박도 음절을 하나 받는다
        all_syl = [(i, syl) for i, (_, sy) in enumerate(lines) for syl in sy]
        used = min(len(flat), len(all_syl))
        if len(all_syl) > len(flat):
            result.warnings.append(f"가사 음절 {len(all_syl)}개가 음표 {len(flat)}개보다 많아 뒤쪽 {len(all_syl) - len(flat)}개는 넣지 못함")
        per_line: dict[int, list[Slot]] = {}
        for k in range(used):
            line_idx, syl = all_syl[k]
            placements.append((flat[k], [syl], line_idx))
            per_line.setdefault(line_idx, []).append(flat[k])
        for i, (ln, sy) in enumerate(lines):
            got = per_line.get(i, [])
            if got:
                reports[i] = LineReport(ln.text, got[0].bar + 1, got[-1].bar + 1, len(sy), len(got))
            else:
                reports[i] = LineReport(ln.text, None, None, len(sy), 0)
                result.unplaced_lines.append(ln.text)
    else:
        eligible, gaps = _eligible_with_gaps(slots)
        counts = [len(sy) for _, sy in lines]
        times = [ln.time_sec for ln, _ in lines]
        bpm = tempo_bpm or _first_tempo(root)
        extra: dict = {}
        if audio is not None:
            lk = _lookup(root)
            audio.refine(  # 보컬 멜로디를 실제 가창 음높이에 다시 맞춰 시간 대응을 정밀화
                [(s.start, s.duration, _slot_pitches(lk, s)) for s in eligible]
            )
            activity, audio_gaps, slot_secs = audio.slot_features(
                [s.start for s in eligible], [s.duration for s in eligible]
            )
            extra = {"activity": activity, "audio_gaps": audio_gaps, "slot_secs": slot_secs}
        pairs = _align_lines(counts, eligible, gaps, None, bpm, 0.0, **extra)
        if any(t is not None for t in times):
            offset = time_offset_sec
            if offset is None:
                secs_of = (lambda q: extra["slot_secs"][q]) if extra else (lambda q: eligible[q].start * 60.0 / bpm)
                diffs = [times[i] - secs_of(q) for i, q, _ in pairs if times[i] is not None]
                offset = statistics.median(diffs) if diffs else 0.0
            pairs = _align_lines(counts, eligible, gaps, times, bpm, offset, **extra)
        used_slots = 0
        done_lines = set()
        for i, q, n in pairs:
            group_slots = eligible[q : q + n]
            assigned = _assign(lines[i][1], group_slots)
            placements.extend((slot, group, i) for slot, group in assigned)
            used_slots += n
            done_lines.add(i)
            ln, sy = lines[i]
            reports[i] = LineReport(
                ln.text, group_slots[0].bar + 1, group_slots[-1].bar + 1, len(sy), n,
                start_sec=extra["slot_secs"][q] if extra else None,
            )
        for i, (ln, sy) in enumerate(lines):
            if i not in done_lines:
                reports[i] = LineReport(ln.text, None, None, len(sy), 0)
                result.unplaced_lines.append(ln.text)
        result.skipped_slots = len(eligible) - used_slots
        if result.unplaced_lines:
            result.warnings.append(f"맞는 멜로디 구간을 찾지 못한 가사 줄 {len(result.unplaced_lines)}개를 넣지 못함")

    if not placements:
        raise GuitarProError("가사를 넣을 수 있는 위치를 찾지 못했습니다")
    placements.sort(key=lambda p: p[0].start)
    result.placed = len(placements)
    result.lines = [reports[i] for i in sorted(reports)]

    # Guitar Pro는 가사 텍스트를 첫 음표 박부터 빈틈없이 한 박에 하나씩 배정한다(붙임줄 도착 박 포함).
    # 그래서 박별 가사가 이 순차 배정과 똑같도록, 첫 음절부터 마지막 음절까지의 모든 음표 박에 토큰을 만든다:
    # 음절을 받은 박은 그 글자, 이어진 박·건너뛴 박은 '_'. 이렇게 해야 사용자가 가사를 고쳐도 위치가 밀리지 않는다.
    lk = _lookup(root)
    staff = _staff_of_track(root, track_index)
    note_slots = [s for s in track_slots(root, track_index) if s.has_notes]
    first_bar = placements[0][0].bar
    placed = {id(slot.beat): (group, line_idx) for slot, group, line_idx in placements}
    emit = [s for s in note_slots if s.bar >= first_bar]
    last_idx = max(k for k, s in enumerate(emit) if id(s.beat) in placed)
    emit = emit[: last_idx + 1]
    result.filler_beats = sum(1 for s in emit if id(s.beat) not in placed)

    for master in root.find("MasterBars"):
        ids = master.findtext("Bars", "").split()
        bar = lk["Bars"].get(ids[staff]) if staff < len(ids) else None
        for vid in (bar.findtext("Voices", "").split() if bar is not None else []):
            if vid == "-1":
                continue
            for bid in lk["Voices"][vid].findtext("Beats", "").split():
                for old in lk["Beats"][bid].findall("Lyrics"):
                    lk["Beats"][bid].remove(old)

    text_parts: list[str] = []
    prev_line = None
    prev_token = ""
    for slot in emit:
        entry = placed.get(id(slot.beat))
        if entry is None:
            label, token, first_hangul, line_idx = "_", "_", False, None
        else:
            group, line_idx = entry
            shown, joins = _group_text(group)
            label = shown + ("-" if joins else "")
            token = label.replace(" ", "+")  # 한 박에 여러 단어는 '+'로 묶는다
            first_hangul = group[0].hangul and group[0].new_word
        lyrics_el = ET.SubElement(slot.beat, "Lyrics")
        for k in range(LYRIC_LINES_PER_TRACK):
            ET.SubElement(lyrics_el, "Line", {_CD: "1"}).text = label if k == 0 else ""
        # 줄은 개행, 하이픈으로 이어진 음절은 붙여 쓰고, 한글은 글자마다 한 칸·단어 사이는 두 칸
        if not text_parts:
            sep = ""
        elif prev_token.endswith("-"):
            sep = ""
        elif line_idx is not None and prev_line is not None and line_idx != prev_line:
            sep = "\n"
        elif first_hangul:
            sep = "  "
        else:
            sep = " "
        text_parts.append(sep + token)
        prev_token = token
        if line_idx is not None:
            prev_line = line_idx
    text = "".join(text_parts)

    result.offset_bar = placements[0][0].bar
    track = tracks[track_index]
    lyrics_track = track.find("Lyrics")
    if lyrics_track is None:
        lyrics_track = ET.SubElement(track, "Lyrics")
        # Lyrics는 Staves 앞에 와야 한다 (Guitar Pro가 저장하는 순서)
        staves = track.find("Staves")
        if staves is not None:
            track.remove(lyrics_track)
            track.insert(list(track).index(staves), lyrics_track)
    lyrics_track.set("dispatched", "true")
    for line in list(lyrics_track):
        lyrics_track.remove(line)
    for k in range(LYRIC_LINES_PER_TRACK):
        line = ET.SubElement(lyrics_track, "Line")
        ET.SubElement(line, "Text", {_CD: "1"}).text = text if k == 0 else ""
        ET.SubElement(line, "Offset").text = str(result.offset_bar if k == 0 else 0)

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dest:
        for info in zf.infolist():
            data = zf.read(info.filename)
            if info.filename == SCORE_ENTRY:
                data = serialize_gpif(root)
            dest.writestr(info, data)
    result.data = out.getvalue()
    return result


def _first_tempo(root: ET.Element) -> float:
    for auto in root.findall("MasterTrack/Automations/Automation"):
        if auto.findtext("Type") == "Tempo":
            return float(auto.findtext("Value", "120").split()[0])
    return 120.0
