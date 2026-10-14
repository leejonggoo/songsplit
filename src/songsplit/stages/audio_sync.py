"""[10] Audio sync: 실제 음원(예: 영상에서 받은 오디오)을 기준으로 악보의 박 위치를 시간으로 바꾸고,
보컬 음표 자리에서 정말 노래가 불리는지·어디서 숨을 쉬는지 알아낸다. 가사 맞춤(lyrics.py)이 이 정보를 쓴다.

- compute_time_map: 악보를 크로마로 만들어 음원 크로마와 DTW로 맞춰 '박 → 초' 대응표를 만든다.
- VocalActivity: 분리한 보컬 음원의 에너지로 구간별 발성 비율과 직전 무음 길이를 계산한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import librosa
import numpy as np

from songsplit.stages.guitarpro import _read_score
from songsplit.stages.score_merge import load_gp

SR = 22050
HOP = 2048  # 크로마 프레임: 약 93ms
ACTIVITY_HOP = 512  # 에너지 프레임: 약 23ms
SILENCE_REL_DB = -32.0  # 보컬 최대 에너지 대비 이 값 아래면 무음으로 본다


@dataclass
class BeatTimeMap:
    """곡 시작부터의 박 위치 ↔ 음원 시각(초)."""

    beats: np.ndarray
    secs: np.ndarray
    quality: float = 0.0  # 1에 가까울수록 악보와 음원이 잘 맞음

    def to_sec(self, beat: float) -> float:
        return float(np.interp(beat, self.beats, self.secs))

    def to_beat(self, sec: float) -> float:
        return float(np.interp(sec, self.secs, self.beats))


def _score_beats(gp_bytes: bytes) -> tuple[float, float]:
    """(총 박 수, 악보 첫 템포 BPM)."""
    root = _read_score(gp_bytes)[1]
    total = 0.0
    for master in root.find("MasterBars"):
        num, den = (int(x) for x in master.findtext("Time", "4/4").split("/"))
        total += 4.0 * num / den
    bpm = 120.0
    for auto in root.findall("MasterTrack/Automations/Automation"):
        if auto.findtext("Type") == "Tempo":
            bpm = float(auto.findtext("Value", "120").split()[0])
            break
    return total, bpm


def compute_time_map(gp_bytes: bytes, audio_path: Path) -> BeatTimeMap:
    """악보 전체(드럼 제외)의 음정 분포를 음원 크로마에 DTW로 맞춰 박→초 대응표를 만든다."""
    total_beats, bpm = _score_beats(gp_bytes)
    sec_per_beat = 60.0 / bpm
    fps = SR / HOP

    frames = int(total_beats * sec_per_beat * fps) + 1
    score = np.zeros((12, frames))
    for track in load_gp(gp_bytes, "score.gp"):
        if track.is_drum:
            continue
        for pitch, start, end in track.notes:
            a = int(start * sec_per_beat * fps)
            b = max(a + 1, int(end * sec_per_beat * fps))
            score[pitch % 12, a:b] += 1.0
    score /= np.linalg.norm(score, axis=0, keepdims=True) + 1e-9

    y, _ = librosa.load(str(audio_path), sr=SR, mono=True)
    chroma = librosa.feature.chroma_cens(y=y, sr=SR, hop_length=HOP)
    chroma /= np.linalg.norm(chroma, axis=0, keepdims=True) + 1e-9

    cost = 1.0 - score.T @ chroma
    _, path = librosa.sequence.dtw(
        C=cost, step_sizes_sigma=np.array([[1, 1], [1, 2], [2, 1]]), weights_mul=np.array([1.0, 1.3, 1.3])
    )
    path = path[::-1]
    secs = np.full(frames, np.nan)
    for k in range(frames):
        hit = path[path[:, 0] == k, 1]
        if hit.size:
            secs[k] = np.median(hit) / fps
    # 빈 프레임은 보간하고 단조 증가를 보장한다
    ok = ~np.isnan(secs)
    secs = np.interp(np.arange(frames), np.flatnonzero(ok), secs[ok])
    secs = np.maximum.accumulate(secs)
    beats = np.arange(frames) / (sec_per_beat * fps)
    quality = float(np.clip(1.0 - np.mean([cost[a, b] for a, b in path]) / max(float(cost.mean()), 1e-9), 0.0, 1.0))
    return BeatTimeMap(beats=beats, secs=secs, quality=quality)


class VocalActivity:
    """분리한 보컬 음원의 에너지로 발성 여부를 판단한다."""

    def __init__(self, vocals_path: Path):
        y, _ = librosa.load(str(vocals_path), sr=SR, mono=True)
        self.y = y
        rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=ACTIVITY_HOP)[0]
        db = librosa.amplitude_to_db(rms, ref=max(float(rms.max()), 1e-9))
        self._path = Path(vocals_path)
        self._f0: np.ndarray | None = None
        self.active = db > SILENCE_REL_DB
        self.dt = ACTIVITY_HOP / SR
        self.duration = len(y) / SR
        # 무음→발성 전환점과, 그 직전에 이어진 무음 길이
        self._onsets: list[tuple[float, float]] = []
        run = 0
        for i, on in enumerate(self.active):
            if on:
                if i > 0 and not self.active[i - 1]:
                    self._onsets.append((i * self.dt, run * self.dt))
                run = 0
            else:
                run += 1

    def _index(self, sec: float) -> int:
        return int(np.clip(sec / self.dt, 0, len(self.active) - 1))

    def fraction_active(self, start: float, end: float) -> float:
        a, b = self._index(start), max(self._index(end), self._index(start) + 1)
        return float(self.active[a:b].mean())

    F0_HOP = 512

    @property
    def f0_midi(self) -> np.ndarray:
        """프레임(약 23ms)별 가창 음높이(MIDI 번호, 무성 구간은 NaN). 계산이 오래 걸려 보컬 파일 옆에 캐시한다."""
        if self._f0 is None:
            cache = self._path.with_suffix(".f0.npy")
            if cache.exists():
                self._f0 = np.load(cache)
            else:
                f0, _, _ = librosa.pyin(
                    self.y, fmin=librosa.note_to_hz("C2"), fmax=librosa.note_to_hz("C6"),
                    sr=SR, frame_length=2048, hop_length=self.F0_HOP, fill_na=np.nan,
                )
                self._f0 = librosa.hz_to_midi(f0)
                np.save(cache, self._f0)
        return self._f0

    def gap_at(self, sec: float, tol: float = 0.4) -> float:
        """sec 근처(±tol)에서 발성이 시작되는 지점이 있으면 그 직전 무음 길이(초), 없으면 0.

        악보와 음원의 시각 오차를 감안해 가장 가까운 발성 시작점을 쓴다.
        """
        best = None
        for onset, silence in self._onsets:
            if onset > sec + tol:
                break
            if onset >= sec - tol and (best is None or abs(onset - sec) < abs(best[0] - sec)):
                best = (onset, silence)
        return best[1] if best else 0.0


REFINE_BAND_SEC = 1.5  # 거친 대응표에서 이만큼 안에서만 다시 맞춘다
UNVOICED_COST = 0.55  # 한쪽이 쉼표/무성일 때의 중립 비용


def _banded_dtw(cost: np.ndarray, half: int) -> np.ndarray:
    """밴드 DTW. cost[i, c]는 (i, j=i-half+c)의 비용. 악보 프레임 i마다 대응하는 음원 프레임 j를 반환한다.

    이동은 (1,1), (1,2), (2,1)이고 대각선이 아닌 이동에는 가중 1.3을 곱한다. 시작과 끝은 대각선 양 끝으로 고정한다.
    """
    n, width = cost.shape
    INF = 1e18
    D = np.full((n, width), INF)
    step = np.zeros((n, width), dtype=np.uint8)
    D[0, half] = cost[0, half]
    cs = np.arange(width)
    for i in range(1, n):
        cand = np.full((3, width), INF)
        cand[0] = D[i - 1] + cost[i]  # (i-1, j-1): 같은 offset
        cand[1, 1:] = D[i - 1, :-1] + 1.3 * cost[i, 1:]  # (i-1, j-2): 앞 행의 offset c-1
        if i >= 2:
            cand[2, :-1] = D[i - 2, 1:] + 1.3 * cost[i, :-1]  # (i-2, j-1): 두 행 앞의 offset c+1
        best = cand.argmin(axis=0)
        D[i] = cand[best, cs]
        step[i] = best
    c = half
    j_of_i = np.zeros(n)
    i = n - 1
    while i > 0:
        j_of_i[i] = i - half + c
        s = step[i, c]
        if s == 0:
            i -= 1
        elif s == 1:
            i -= 1
            c -= 1
        else:
            j_of_i[i - 1] = (i - 1) - half + (c + 1)  # 건너뛴 행은 같은 쪽으로
            i -= 2
            c += 1
    j_of_i[0] = 0 - half + c
    return np.clip(j_of_i, 0, n - 1)


def refine_time_map(
    coarse: BeatTimeMap,
    vocals: "VocalActivity",
    notes: list[tuple[float, float, list[int]]],
) -> BeatTimeMap:
    """보컬 멜로디의 음높이를 실제 가창 음높이(f0)에 DTW로 다시 맞춰 박→초 대응표를 정밀하게 고친다.

    notes는 (시작 박, 길이 박, [MIDI 음높이들]). 거친 대응표(전체 믹스 기준)가 만든 시간축 위에서
    ±REFINE_BAND_SEC 이내로만 풀기 때문에 엉뚱한 곳으로 튀지 않는다. 음높이는 음이름(옥타브 무시)으로 비교한다.
    """
    fps = SR / VocalActivity.F0_HOP
    f0 = vocals.f0_midi
    n = len(f0)

    # 거친 대응표 시간축(프레임 k = k/fps 초) 위에 악보 멜로디의 음높이를 올린다 (쉼표 프레임은 NaN)
    expected = np.full((n, 3), np.nan)  # 한 박에 최대 3음까지
    for start, dur, pitches in notes:
        a = int(np.clip(coarse.to_sec(start) * fps, 0, n - 1))
        b = int(np.clip(max(coarse.to_sec(start + dur) * fps, a + 1), a + 1, n))
        for k, p in enumerate(pitches[:3]):
            expected[a:b, k] = p

    # 비용(악보 프레임 i, 음원 프레임 j)을 밴드 j ∈ [i-half, i+half] 안에서만 계산한다 (전체 행렬은 메모리가 너무 크다)
    half = int(REFINE_BAND_SEC * fps)
    width = 2 * half + 1
    f0_ok = ~np.isnan(f0)
    cost = np.full((n, width), 5.0)
    for i in range(n):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        row = np.full(hi - lo, UNVOICED_COST)
        exp = expected[i][~np.isnan(expected[i])]
        voiced = f0_ok[lo:hi]
        if exp.size and voiced.any():
            diff = np.abs(((f0[lo:hi][voiced][:, None] - exp[None, :]) + 6) % 12 - 6).min(axis=1)  # 0~6 반음
            row[voiced] = np.clip(diff / 3.0, 0.0, 1.0)
        cost[i, lo - (i - half) : hi - (i - half)] = row
    path_j = _banded_dtw(cost, half)
    refined = path_j / fps
    idx = np.arange(n)
    refined = np.maximum.accumulate(refined)
    beats = np.array([coarse.to_beat(t) for t in idx / fps])
    keep = np.concatenate([[True], np.diff(beats) > 1e-6])
    return BeatTimeMap(beats=beats[keep], secs=refined[keep], quality=coarse.quality)


@dataclass
class AudioGuide:
    """가사 맞춤이 참고할 음원 정보: 박→초 대응표와 보컬 발성."""

    time_map: BeatTimeMap
    vocals: VocalActivity

    def refine(self, notes: list[tuple[float, float, list[int]]]) -> None:
        """보컬 멜로디를 보컬 음원에 다시 맞춰 시간 대응을 정밀화한다."""
        self.time_map = refine_time_map(self.time_map, self.vocals, notes)

    def slot_features(self, starts: list[float], durations: list[float]) -> tuple[list[float], list[float], list[float]]:
        """슬롯마다 (발성 비율, 시작 직전 무음 길이, 시작 시각 초)."""
        activity, gaps, times = [], [], []
        for start, dur in zip(starts, durations):
            t0 = self.time_map.to_sec(start)
            t1 = max(self.time_map.to_sec(start + dur), t0 + 0.05)
            activity.append(self.vocals.fraction_active(t0, t1))
            gaps.append(self.vocals.gap_at(t0))
            times.append(t0)
        return activity, gaps, times
