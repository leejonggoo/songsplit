"""[4] 전사 결과(PrettyMIDI)에서 쓸모없는 음을 걸러내는 후처리.

basic-pitch는 재현율 위주라 배음(옥타브/5도 위 유령음), 아주 약한 음, 짧은 잡음 음,
같은 음이 잘게 끊긴 조각을 많이 만든다. 단선율 악기(보컬/베이스)는 동시에 한 음만 남긴다.
strength(0~1)가 클수록 더 공격적으로 지운다.
"""

from __future__ import annotations

import pretty_midi

# 기준 음 위로 이 간격(반음)에 있는 음은 배음 후보
_HARMONIC_INTERVALS = (12, 19, 24)
MONOPHONIC_STEMS = {"vocals", "bass"}


def _overlap(a: pretty_midi.Note, b: pretty_midi.Note) -> float:
    return max(0.0, min(a.end, b.end) - max(a.start, b.start))


def drop_weak(notes: list[pretty_midi.Note], strength: float) -> list[pretty_midi.Note]:
    floor = 30 + 30 * strength
    return [n for n in notes if n.velocity >= floor]


def drop_harmonic_ghosts(notes: list[pretty_midi.Note], strength: float) -> list[pretty_midi.Note]:
    """같은 시각에 울리는 더 센 음의 옥타브/12도/2옥타브 위 음은 배음으로 보고 제거."""
    keep = []
    ratio = 1.0 - 0.3 * strength  # strength가 클수록 비슷한 세기의 음도 배음으로 취급
    for n in notes:
        dur = max(n.end - n.start, 1e-6)
        is_ghost = any(
            o is not n
            and n.pitch - o.pitch in _HARMONIC_INTERVALS
            and abs(o.start - n.start) < 0.06
            and _overlap(o, n) >= 0.6 * dur
            and n.velocity <= o.velocity * ratio
            for o in notes
        )
        if not is_ghost:
            keep.append(n)
    return keep


def merge_fragments(notes: list[pretty_midi.Note], max_gap: float = 0.08) -> list[pretty_midi.Note]:
    """같은 음이 짧은 틈으로 끊겨 있으면 하나로 잇는다."""
    out: list[pretty_midi.Note] = []
    last_by_pitch: dict[int, pretty_midi.Note] = {}
    for n in sorted(notes, key=lambda x: x.start):
        prev = last_by_pitch.get(n.pitch)
        if prev is not None and n.start - prev.end <= max_gap:
            prev.end = max(prev.end, n.end)
            prev.velocity = max(prev.velocity, n.velocity)
            continue
        last_by_pitch[n.pitch] = n
        out.append(n)
    return out


def make_monophonic(notes: list[pretty_midi.Note]) -> list[pretty_midi.Note]:
    """겹치는 음 중 더 센(velocity*길이) 쪽만 남기고, 앞 음은 겹침 만큼 잘라낸다."""
    out: list[pretty_midi.Note] = []
    for n in sorted(notes, key=lambda x: x.start):
        if out and n.start < out[-1].end:
            prev = out[-1]
            if n.velocity * (n.end - n.start) > prev.velocity * (prev.end - prev.start):
                prev.end = n.start
                if prev.end - prev.start < 0.05:
                    out.pop()
                out.append(n)
            # 약한 쪽(n)은 버린다
            continue
        out.append(n)
    return out


def drop_short(notes: list[pretty_midi.Note], min_len: float) -> list[pretty_midi.Note]:
    return [n for n in notes if n.end - n.start >= min_len]


def _initial_tempo(midi: pretty_midi.PrettyMIDI) -> float:
    tempi = midi.get_tempo_changes()[1]
    return float(tempi[0]) if tempi.size else 120.0


def filter_notes(
    midi: pretty_midi.PrettyMIDI, stem: str, strength: float = 0.5
) -> pretty_midi.PrettyMIDI:
    """모든 (비드럼) 트랙에 필터를 적용한 MIDI를 반환한다. 입력은 수정하지 않는다."""
    strength = min(max(strength, 0.0), 1.0)
    min_len = 0.06 + 0.1 * strength
    out = pretty_midi.PrettyMIDI(initial_tempo=_initial_tempo(midi))
    for inst in midi.instruments:
        new_inst = pretty_midi.Instrument(program=inst.program, is_drum=inst.is_drum, name=inst.name)
        notes = [
            pretty_midi.Note(velocity=n.velocity, pitch=n.pitch, start=n.start, end=n.end)
            for n in inst.notes
        ]
        if not inst.is_drum:
            notes = drop_weak(notes, strength)
            notes = drop_harmonic_ghosts(notes, strength)
            notes = merge_fragments(notes)
            if stem in MONOPHONIC_STEMS:
                notes = make_monophonic(notes)
            notes = drop_short(notes, min_len)
        new_inst.notes = sorted(notes, key=lambda x: (x.start, x.pitch))
        out.instruments.append(new_inst)
    return out
