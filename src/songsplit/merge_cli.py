"""같은 곡의 MIDI/MuseScore/MusicXML/Guitar Pro 파일들을 분석해 기준 .gp 악보에 트랙으로 합치는 명령행 도구.

    python -m songsplit.merge_cli --base song.gp bass.mid piano.mscz other.gp -o merged.gp
    python -m songsplit.merge_cli --base song.gp bass.mid --analyze-only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from songsplit.stages.guitarpro import GuitarProError, inspect_gp, repair_playable
from songsplit.stages.lyrics import add_lyrics_to_gp, list_tracks
from songsplit.stages.score_merge import analyze_sources, apply_plan, load_tracks


def _write_lyrics(args, gp_bytes: bytes, output: Path) -> None:
    tracks = list_tracks(gp_bytes)
    if args.lyrics_track:
        wanted = args.lyrics_track.lower()
        matches = [t for t in tracks if wanted in t["name"].lower() and not t["is_drum"]]
    else:
        matches = [t for t in tracks if t["looks_vocal"] and not t["is_drum"]]
    if not matches:
        names = ", ".join(t["name"] for t in tracks)
        raise GuitarProError(f"가사를 넣을 트랙을 찾지 못했습니다. --lyrics-track으로 지정하세요 (트랙: {names})")
    target = matches[0]
    audio_guide = None
    if args.audio:
        from songsplit.stages.audio_sync import AudioGuide, VocalActivity, compute_time_map

        vocals = args.vocals
        if vocals is None:
            from songsplit.stages.separation.demucs_engine import DemucsEngine

            print("보컬 분리 중 (Demucs)...")
            vocals = DemucsEngine().separate(args.audio, ["vocals"], args.audio.parent / "sep")["vocals"]
        time_map = compute_time_map(gp_bytes, args.audio)
        print(f"악보↔음원 정렬 품질: {time_map.quality:.2f} (1에 가까울수록 잘 맞음)")
        audio_guide = AudioGuide(time_map, VocalActivity(vocals))
    result = add_lyrics_to_gp(
        gp_bytes,
        target["index"],
        args.lyrics.read_text(encoding="utf-8"),
        mode=args.lyrics_mode,
        start_bar=args.lyrics_start_bar,
        audio=audio_guide,
    )
    output.write_bytes(result.data)
    print(f"가사: '{target['name']}' 트랙에 음절 {result.placed}개 배치 (시작 마디 {result.offset_bar + 1})")
    for line in result.lines:
        where = f"{line.first_bar}~{line.last_bar}마디" if line.first_bar else "맞춤 실패"
        when = f" {int(line.start_sec // 60)}:{line.start_sec % 60:04.1f}" if line.start_sec is not None else ""
        print(f"  [{where}{when}] 음절 {line.syllables} / 음표 {line.slots} — {line.text}")
    if args.export_lrc:
        rows = [ln for ln in result.lines if ln.start_sec is not None]
        args.export_lrc.write_text(
            "\n".join(f"[{int(ln.start_sec // 60):02d}:{ln.start_sec % 60:05.2f}]{ln.text}" for ln in rows) + "\n",
            encoding="utf-8",
        )
        print(f"LRC 저장: {args.export_lrc} ({len(rows)}줄)")
    for warning in result.warnings:
        print(f"경고: {warning}", file=sys.stderr)
    print(f"저장: {output}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True, type=Path, help="트랙을 추가할 기준 Guitar Pro(.gp) 파일")
    parser.add_argument("sources", nargs="*", type=Path, help="합칠 MIDI/MuseScore/MusicXML/.gp 파일들")
    parser.add_argument("--lyrics", type=Path, help="보컬 트랙에 맞춰 넣을 가사 파일(.txt 또는 시각이 있는 .lrc)")
    parser.add_argument("--lyrics-track", help="가사를 넣을 트랙 이름(일부만 써도 됨). 기본: 보컬로 보이는 트랙")
    parser.add_argument("--lyrics-mode", choices=["smart", "sequential"], default="smart", help="smart: 가사 줄을 멜로디 구간에 맞춤(기본), sequential: 첫 음표부터 순서대로")
    parser.add_argument("--audio", type=Path, help="가사 싱크 기준이 될 실제 음원(mp3/wav). 악보를 이 음원에 맞춰 줄 위치를 정합니다")
    parser.add_argument("--vocals", type=Path, help="--audio에서 이미 분리한 보컬 wav. 없으면 Demucs로 분리합니다")
    parser.add_argument("--export-lrc", type=Path, help="맞춘 결과를 줄별 시각이 있는 LRC 파일로도 저장 (음원 사용 시)")
    parser.add_argument("--lyrics-start-bar", type=int, help="이 마디(1부터) 이후의 음표부터 가사를 넣음")
    parser.add_argument("-o", "--output", type=Path, help="결과 .gp 경로 (기본: <base>_merged.gp)")
    parser.add_argument("--repair", action="store_true", help="조표를 바꾸면 Guitar Pro가 죽는 파일 고치기: 비기타 트랙의 줄·프렛을 다시 배정(합치기·가사 없이 이것만 실행)")
    parser.add_argument("--analyze-only", action="store_true", help="분석 결과만 출력하고 파일은 만들지 않음")
    parser.add_argument("--include-duplicates", action="store_true", help="기준 악보와 겹치는 트랙도 추가")
    args = parser.parse_args(argv)

    try:
        base = args.base.read_bytes()
        if args.repair:
            fixed, report = repair_playable(base)
            output = args.output or args.base.with_name(f"{args.base.stem}_repaired.gp")
            output.write_bytes(fixed)
            print("\n".join(report) or "고칠 트랙이 없습니다")
            print(f"저장: {output}")
            return 0
        if not args.sources and not args.lyrics:
            raise GuitarProError("합칠 파일이나 --lyrics 중 하나는 필요합니다")
        if not args.sources:
            _write_lyrics(args, base, args.output or args.base.with_name(f"{args.base.stem}_merged.gp"))
            return 0
        tracks = [t for path in args.sources for t in load_tracks(path.read_bytes(), path.name)]
        if not tracks:
            raise GuitarProError("읽을 수 있는 음표가 있는 트랙이 없습니다")
        plan = analyze_sources(base, tracks)
        if args.include_duplicates:
            for item in plan.items:
                item.include = True

        print(f"기준 악보 트랙: {', '.join(plan.base_tracks)}")
        print(f"기준 악보 조: {plan.base_key}")
        for item in plan.items:
            al = item.alignment
            dup = f" · 중복: {item.duplicate_of} ({item.duplicate_score:.0%})" if item.duplicate_of else ""
            print(
                f"[{'+' if item.include else ' '}] {item.track.label} — {item.track.role}, 음 {len(item.track.notes)}개, "
                f"x{al.scale:g} 오프셋 {al.shift_beats:+g}박 조옮김 {al.transpose:+d}반음 (일치도 {al.score:.2f}){dup}"
            )
        for warning in plan.warnings:
            print(f"경고: {warning}", file=sys.stderr)
        if args.analyze_only:
            return 0

        result = apply_plan(base, plan)
        output = args.output or args.base.with_name(f"{args.base.stem}_merged.gp")
        output.write_bytes(result.data)
        for warning in result.warnings:
            print(f"경고: {warning}", file=sys.stderr)
        print(f"저장: {output} (추가된 트랙 {len(result.added_tracks)}개)")
        if args.lyrics:
            _write_lyrics(args, result.data, output)
    except (GuitarProError, OSError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
