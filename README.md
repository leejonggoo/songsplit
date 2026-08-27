# SongSplit

음원 파일(mp3/wav/m4a)을 악기별 트랙으로 분리하고, 각 트랙을 MIDI로 변환하는 파이프라인.
전체 설계와 단계별 계획은 [PLAN.md](PLAN.md) 참고.

## 파이프라인

```
업로드 → Ingestion → Separation(악기 선택적 분리) → Analysis(템포/키 추정+수정)
       → Transcription(MIDI 변환) → Post-process(퀀타이즈/스케일 스냅) → Export(zip)
```

각 단계는 독립 모듈이며, 처리 결과는 `jobs/<job_id>/` 폴더에 단계별로 저장되고
`manifest.json`에 상태·파라미터·산출물 경로가 기록된다. 웹 UI(Streamlit)는 이
manifest만 읽어 이미 끝난 단계는 재실행 없이 결과를 보여준다.

## 요구 사항

- Python 3.11+
- [ffmpeg](https://ffmpeg.org/)가 설치되어 PATH에 잡혀 있어야 함 (pydub/librosa의 오디오 디코딩에 필요)

## 설치

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"   # Windows
# .venv/bin/pip install -e ".[dev]"     # macOS/Linux
```

최초 실행 시 Demucs·basic-pitch 사전학습 모델 가중치를 자동 다운로드하므로
인터넷 연결과 약간의 시간이 필요하다.

## 실행

```bash
.venv/Scripts/streamlit run ui/app.py
```

브라우저에서 `http://localhost:8501` 접속 → 오디오 업로드 → 악기 선택 →
분리 → 템포/키 확인·수정 → MIDI 변환 → (선택) 후처리 → zip 다운로드
(스템별 wav/MIDI + 전체 스템을 트랙별로 합친 `merged.mid` 포함).

## 테스트

```bash
.venv/Scripts/pytest -q
```

## 프로젝트 구조

```
src/songsplit/
├── pipeline/
│   ├── config.py         # AppConfig (jobs 폴더 위치 등)
│   └── job.py             # Job/manifest.json 읽기·쓰기, 스테이지 상태 관리
├── core/
│   ├── audio_io.py        # 포맷 통일(wav 변환), wav 읽기/쓰기
│   └── midi_io.py         # pretty_midi 저장/로드 래퍼
└── stages/
    ├── ingest.py                    # [1] 입력 → 표준 wav
    ├── separate.py                  # [2] 오케스트레이션 (엔진 호출 + manifest)
    ├── separation/
    │   ├── base.py                  # SeparationEngine 추상 인터페이스
    │   ├── demucs_engine.py         # Demucs 구현 (기본, 고품질)
    │   └── spleeter_engine.py       # Spleeter 구현 (경량 대안, 아래 참고)
    ├── analysis.py / analyze.py     # [3] 템포·키 추정 (algorithm / orchestration)
    ├── transcription/
    │   ├── melodic.py               # basic-pitch 기반 (보컬/베이스/기타/피아노/그 외)
    │   └── drums.py                 # onset+스펙트럼 휴리스틱 (드럼)
    ├── transcribe.py                # [4] 오케스트레이션
    ├── postprocess.py               # [5] 퀀타이즈 / 스케일 스냅 (+오케스트레이션)
    └── export.py                    # [6] wav+MIDI를 zip으로 묶기 + 스템별 MIDI를 merged.mid로 병합

ui/app.py       # Streamlit 웹 UI
jobs/           # 런타임 산출물 (job_id별 폴더, git 추적 안 함)
tests/          # pytest
```

## 악기 분리 엔진

`SeparationEngine` 인터페이스(`available_stems()` / `separate()`)를 통해 엔진을
교체 가능하게 설계했다.

| 엔진 | 특징 |
|---|---|
| **Demucs** (기본) | 고품질. `htdemucs`(4종: vocals/drums/bass/other) 또는 `htdemucs_6s`(6종: +guitar/piano) |
| **Spleeter** (대안) | 더 가볍고 빠름. `2stems`/`4stems`/`5stems` 설정 지원 |

악기를 하나만 선택하면 Demucs는 `--two-stems` 모드로 연산량을 줄인다.

### ⚠️ Spleeter 플랫폼 제약

Spleeter 2.4.2는 `tensorflow==2.12.1`을 강하게 고정하는데, 이 버전이 요구하는
`tensorflow-io-gcs-filesystem==0.32.0`은 **Windows용 wheel이 PyPI에 없다**
(2026-08 기준, Windows는 0.31.0까지만 존재). 따라서 이 프로젝트의 기본
Windows 개발 환경에는 Spleeter를 설치할 수 없다.

Spleeter를 쓰려면 Linux/WSL/macOS 환경에서 별도로 설치한다:

```bash
pip install -e ".[spleeter]"
```

Windows에서 UI의 Spleeter 옵션을 실행하면 위 사유를 설명하는 에러 메시지가
표시되고 실패한다 (`SpleeterEngine.separate()`가 친절한 안내와 함께 예외 발생).
이 경우 Demucs 엔진을 사용하면 된다.

## 알려진 한계

- **드럼 MIDI 변환**은 정교한 채보가 아니라 onset detection + 스펙트럴 센트로이드
  기반의 단순 휴리스틱으로 킥/스네어/클로즈 하이햇 3종만 분류한다.
- **관악기(wind/brass) 분리**는 지원하지 않는다. Demucs/Spleeter 모두 관악기를
  별도 스템으로 분리하지 못해 "other"에 뭉쳐서 나온다. MoisesDB 기반 연구용
  모델(Bandit 등)은 성숙한 pip 패키지가 없어 채택하지 않았다.
- 배치(여러 파일 일괄) 처리 CLI는 아직 없음 — 현재는 웹 UI로 파일 하나씩 처리.
