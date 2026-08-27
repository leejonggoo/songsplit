# SongSplit 작업계획

## 1. 목표

MP3 등 음원 파일을 입력받아
1) 트랙(보컬/드럼/베이스/기타 등)으로 분리하고,
2) 분리된 각 트랙을 MIDI로 변환하며,
3) MIDI 변환 시 템포/스케일(조성)을 자동 추정해 보여주고 사용자가 수정할 수 있게 한다.

각 단계는 독립 모듈로 분리하고, 단계별 산출물을 파일로 저장 + 웹 UI에서 즉시 확인할 수 있어야 한다.
악기 분리는 전체(보컬/드럼/베이스/기타/피아노/기타악기)를 다 뽑는 게 아니라, 사용자가 필요한 악기만 선택적으로 분리할 수 있어야 한다.

## 2. 기술 스택 (확정)

| 영역 | 선택 |
|---|---|
| 언어 | Python 3.11+ |
| 음원 분리 | 플러그형 엔진: Demucs(기본, 고품질, `htdemucs`/`htdemucs_6s`) / Spleeter(경량, 대안, 2/4/5stems) / (Phase 7, 실험적) MoisesDB 기반 모델(관악기 등 세분화 분리) — 악기 선택적 분리 지원 |
| 오디오→MIDI | basic-pitch (멜로디/화성 악기), 드럼은 onset 기반 별도 처리 |
| 템포/키 분석 | librosa (템포), librosa chroma + Krumhansl-Schmuckler 또는 music21 (키/스케일) |
| MIDI 후처리 | pretty_midi, mido |
| UI | Streamlit (단계별 wizard 형태, 오디오 플레이어 + 피아노롤 시각화 + 수정 폼) |
| 패키지 관리 | uv (또는 poetry) |
| 오디오 I/O | pydub + ffmpeg |

## 3. 파이프라인 & 모듈 구조

```
audio 입력
   │
   ▼
[1] Ingestion       — 포맷 검증, 리샘플링/정규화, 메타데이터 추출
   │
   ▼
[2] Separation      — 사용자가 선택한 악기만 스템 분리 (엔진/모델 교체 가능)
   │
   ▼
[3] Analysis        — 스템별/전체 템포·키(스케일) 추정
   │  (웹 UI에서 결과 확인 + 수동 수정)
   ▼
[4] Transcription   — 스템별 오디오→MIDI 변환 (수정된 템포/스케일 반영)
   │
   ▼
[5] Post-process    — 퀀타이즈(템포 그리드 스냅), 스케일 스냅(옵션), 미리듣기
   │
   ▼
[6] Export          — 최종 stems(wav) + MIDI 파일 묶음 다운로드
```

### 디렉터리 구조 (제안)

```
songsplit/
├── src/songsplit/
│   ├── stages/
│   │   ├── ingest.py          # [1]
│   │   ├── separation/
│   │   │   ├── base.py        # SeparationEngine 추상 클래스
│   │   │   ├── demucs_engine.py
│   │   │   └── spleeter_engine.py
│   │   ├── analysis.py        # [3] 템포/키 추정
│   │   ├── transcription/
│   │   │   ├── melodic.py     # basic-pitch 기반
│   │   │   └── drums.py       # onset+분류 기반
│   │   └── postprocess.py     # [5] 퀀타이즈/스케일 스냅
│   ├── pipeline/
│   │   ├── job.py             # Job 단위 실행 관리, manifest.json 기록
│   │   └── config.py
│   └── core/
│       ├── audio_io.py
│       └── midi_io.py
├── ui/
│   └── app.py                 # Streamlit 앱 (단계별 페이지)
├── jobs/                      # 런타임 산출물 (job_id별 폴더)
│   └── <job_id>/
│       ├── 00_input/
│       ├── 01_separation/{선택한 악기}.wav   # 예: vocals.wav, guitar.wav (선택 안 한 악기는 생성 안 함)
│       ├── 02_analysis/analysis.json
│       ├── 03_midi/{선택한 악기}.mid
│       ├── 04_edited_midi/
│       └── manifest.json      # 단계별 상태/파라미터/타임스탬프
├── tests/
│   └── fixtures/               # 짧은 테스트용 오디오
└── pyproject.toml
```

### Job/Manifest 규약
- 처리 1회 = 1 job (`jobs/<job_id>/`)
- 각 스테이지 완료 시 `manifest.json`에 `status`, 사용한 파라미터(엔진 종류, 추정 템포/키, 사용자 수정 여부), 산출물 경로, 소요시간 기록
- UI는 manifest를 읽어 이미 완료된 단계는 재실행 없이 결과만 표시, "다시 실행" 버튼으로 재처리 가능

## 4. 단계별 상세

**[1] Ingestion**: mp3/wav/m4a 등 입력 → ffmpeg으로 44.1kHz/stereo wav로 통일, 길이/샘플레이트 등 메타데이터 저장.

**[2] Separation**: `SeparationEngine` 인터페이스를 두고 Demucs/Spleeter 구현체를 교체 가능하게 구성.
  - `available_stems() -> list[str]`: 현재 엔진/모델이 지원하는 악기 목록 반환 (Demucs `htdemucs`: vocals/drums/bass/other, `htdemucs_6s`: +guitar/piano, Spleeter: 설정에 따라 vocals/drums/bass/piano/accompaniment, Phase 7 실험 엔진: +wind(관악기) 등 MoisesDB 세분화 카테고리)
  - `separate(path, targets: list[str]) -> dict[str, wav_path]`: 사용자가 선택한 악기만 반환
  - 선택한 악기가 모델이 지원하는 최소 단위보다 세분화되어 있지 않으면(예: Demucs 4-stem 모델에서 기타만 선택) UI에서 "이 모델은 기타를 단독 분리할 수 없음, 6-stem 모델 필요" 등으로 안내하고 상위 호환 모델로 자동 전환 제안
  - 단일 악기만 필요한 경우 Demucs의 `two-stems` 모드(해당 악기 vs 나머지)로 처리해 연산량 절감
  - 선택하지 않은 악기는 분리/후속 단계 모두 스킵 (연산 시간 절약)
  - 결과 스템별 wav + 원본 대비 파형 비교 이미지 저장

**[3] Analysis**: 스템(주로 보컬/베이스/other 믹스)에서 템포(BPM), 키/스케일(예: C major, A minor) 추정 후 confidence와 함께 `analysis.json`에 저장. UI에서 슬라이더/드롭다운으로 값 수정 가능, 수정값은 다음 단계에 사용.

**[4] Transcription**: 스템 종류별 분기
  - 멜로디/화성 계열(vocals, bass, other) → basic-pitch로 note onset/pitch/velocity 추출 → MIDI
  - drums → onset detection 후 에너지/스펙트럼 특성으로 킥/스네어/하이햇 등 분류 → GM 드럼 맵 MIDI
  - 이 단계에서 [3]에서 확정된 템포를 MIDI의 tempo track에 반영

**[5] Post-process**: 사용자가 수정한 템포/스케일 기준으로 노트 타이밍 퀀타이즈, (옵션) 스케일 밖 음을 가장 가까운 스케일 음으로 스냅. 적용 전/후 피아노롤 비교 제공.

**[6] Export**: 스템 wav + 최종 MIDI를 zip으로 묶어 다운로드. 스템별 MIDI를 트랙(Instrument)별로 유지한 채 하나로 합친 `merged.mid`도 함께 생성해 zip에 포함하고 별도 다운로드도 제공.

## 5. 웹 UI (Streamlit) 페이지 흐름

1. **업로드**: 파일 업로드 → job 생성
2. **악기 선택**: 엔진/모델 선택 → 해당 모델이 지원하는 악기 체크박스 목록 표시 → 원하는 악기만 선택 후 분리 실행
3. **분리 결과**: 선택한 스템만 오디오 플레이어/파형 표시, 재실행(엔진·악기 변경) 옵션
4. **템포/키 분석**: 추정값 표시 + 수정 폼(BPM 숫자입력, 키 드롭다운) → 저장 시 [5] 무효화 표시
5. **MIDI 변환 결과**: 선택한 스템별 피아노롤 시각화(plotly), MIDI 재생/다운로드
6. **내보내기**: 선택한 스템 wav + MIDI만 zip 다운로드

## 6. 개발 마일스톤

| Phase | 내용 |
|---|---|
| 0 | 프로젝트 셋업 (pyproject, ffmpeg/Demucs/basic-pitch 의존성, 폴더 구조, Job/manifest 뼈대) |
| 1 | Ingestion 모듈 + 단위 테스트 |
| 2 | Separation 모듈 (Demucs 우선, `available_stems`/선택적 분리 포함) + Streamlit 악기 선택 UI + 결과 재생 확인 |
| 3 | Analysis 모듈 (템포/키 추정) + UI 수정 폼 |
| 4 | Transcription 모듈 (basic-pitch 멜로디 + 드럼 처리) + 피아노롤 미리보기 |
| 5 | Post-process (퀀타이즈/스케일 스냅) + 재적용 UI |
| 6 | Export (zip 다운로드) |
| 7 | Spleeter 엔진 추가(플러그형 검증), **관악기 분리 실험적 엔진(MoisesDB 기반 모델, 예: Bandit/BS-Roformer 계열) 조사·통합**, 배치 처리, 테스트 보강, README/문서화 |

## 7. 리스크/검토 필요 사항
- Demucs/basic-pitch는 모델 다운로드 및 연산량이 커서 최초 실행 시간이 김 → 캐시 및 진행률 표시 필요
- 드럼 MIDI 변환은 상용 수준 정확도가 어려움 → 우선 onset 기반 단순 분류로 시작, 필요시 개선
- GPU 유무에 따라 Demucs 처리 속도 차이 큼 → CPU 환경 기준 처리 시간 사전 안내 필요
- 악기별 선택적 분리는 모델의 최소 분리 단위에 제약됨 (예: 4-stem 모델은 기타/피아노를 단독 추출 불가) → UI에서 선택 가능한 악기를 모델 능력에 맞춰 동적으로 제한하고, 더 세분화된 분리가 필요하면 6-stem 모델 등으로 안내
- 관악기(wind/brass) 분리는 Demucs/Spleeter가 지원하지 않아 Phase 7에서 MoisesDB 기반 실험적 엔진을 별도로 조사·통합 예정. pip 설치형 성숙한 패키지가 아니라 연구 코드/체크포인트 수준일 가능성이 높아 정확도·안정성이 기본 엔진보다 낮을 수 있음 → UI에 "실험적" 표시, 실패 시 기본 엔진으로 폴백 처리 필요
