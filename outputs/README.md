# Home Sweet Home 작업 결과

## 최종 파일

- `home_sweet_home_merged/Home_Sweet_Home_80bpm_Piano_repaired.gp`: 원본 160 BPM 악보에 한 마디를 먼저 추가하고 80 BPM으로 재구성한 뒤 MusicXML 피아노를 추가한 최종본. 68마디, 7트랙.
- `home_sweet_home_chords/Home_Sweet_Home_full_score.musicxml`: 공식 음원을 자동 채보한 G장조·80 BPM 합주 악보와 마디별 추정 코드.
- 각 결과 폴더의 악기별 MusicXML, MIDI, 필터 통계 및 코드 CSV.

## 작업 흐름

1. `home_sweet_home`: TJ 노래방 음원의 6스템 분리와 최초 자동 채보.
2. `home_sweet_home_official`: 공식 음원으로 재처리. G장조·80 BPM, onset 0.65, frame 0.40, 최소 음표 150 ms. 낮은 RMS와 velocity를 필터링.
3. `home_sweet_home_aligned`: 한 박의 못갖춘마디로 마디 경계 조정.
4. `home_sweet_home_chords`: 반주와 베이스를 이용한 마디별 코드 추정.
5. `home_sweet_home_gp80`: 원본 Guitar Pro 악보의 리듬 길이를 절반으로 줄이고 두 마디를 하나로 병합. 원본에 먼저 한 마디를 추가한 변환본이 최종 병합의 입력이다.
6. `home_sweet_home_merged`: 기존 6트랙을 보존하고 `Piano — MusicXML`을 별도 추가. 번호가 같은 마디끼리 정렬하고 못갖춘마디 음은 도입부에 보존.

## 실행 및 검증

프로젝트 루트에서 `.venv/bin/python outputs/<단계>/<스크립트>.py`로 실행한다. 음원 스크립트는 로컬 Downloads의 해당 원본 음원을 요구하며, `jobs/`와 단계별 `job_id.txt`를 사용한다. 새 환경에서는 기존 job_id.txt를 제거하거나 해당 작업 폴더를 복원해야 한다.

Guitar Pro 변환은 원본 경로를 인자로 전달한다:

```sh
.venv/bin/python outputs/home_sweet_home_gp80/convert_intro_first.py '/path/to/original.gp'
.venv/bin/python outputs/home_sweet_home_merged/merge_piano.py
.venv/bin/python outputs/home_sweet_home_merged/repair_audio.py
```

`merge_piano.py`의 중간 GP 파일은 오디오 설정이 불완전하므로 직접 열지 말고 반드시 `repair_audio.py`를 실행한 최종 `Piano_repaired.gp`를 사용한다. 복구 스크립트는 설치된 피아노 사운드뱅크의 `acousticPiano` 식별자와 `German-APiano` 패치를 사용한다.

검증: MusicXML 재파싱, MIDI 트랙/템포 확인, 마디 재구성 전후 음표 시각·길이 비교, 병합 전후 기존 트랙 보존, 새 피아노의 음높이/시간 격자 비교, ZIP 무결성 검사. 최종 복구본은 Guitar Pro 8.1에서 열고 프로세스 유지 및 새 충돌 보고서 부재를 확인했다. 화면·재생 전체를 검수한 것은 아니다.

이 악보와 코드는 자동 추정 초안이며 청음 보정이 필요하다. 박자는 4/4 기준이다. 음원 분리 모델과 가상환경, 로컬 작업 데이터, 100MB를 넘는 음원 포함 ZIP, 로그, 충돌했던 중간 GP 파일은 Git에서 제외한다.
