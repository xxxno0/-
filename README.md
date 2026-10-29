# GrowAI (Android APK)

폰 하나로 혼자 게임을 하며 자라는 AI. PC 없이 동작.

## 구조
| 역할 | 구현 | 파일 |
|---|---|---|
| 눈 | MediaProjection 화면 녹화 (가로 게임 자동 회전 대응) | BrainService.kt |
| 손 | 접근성 서비스 탭/스와이프/길게 누르기/뒤로 | HandsService.kt |
| 판단 | TypeSafe **Jev** (행동 선택·진전 판정) + 비전 LLM | Ai.kt |
| 기억 | 폰 안 SQLite (경험·교훈·조작법) | Memory.kt |
| 생명 | 레벨·호기심·콤보 기술·잠재우기 / 조작법 자동 발견 | Life.kt |
| 생명 루프 | 보기→기억→판단→터치→평가→저장→성장, 오류 나도 회복 | Brain.kt |
| 화면 | API 키 📋붙여넣기, 시작/정지, 성장 기록 | MainActivity.kt |

## APK 받기 (GitHub 자동 빌드)
이 폴더를 GitHub 저장소(비공개 추천)에 올리면 Actions가 자동으로 빌드합니다.
완료되면 저장소의 **Releases → latest → GrowAI.apk** 를 폰에서 눌러 설치.

## 폰에서 처음 설정
1. GrowAI.apk 설치 ("출처를 알 수 없는 앱" 허용)
2. 앱 실행 → API 키들 복사 후 📋 붙여넣기 → 💾 저장
3. **1. 손 켜기** → 접근성 → 설치된 앱 → GrowAI → 사용 켜기
4. **2. 깨우기** → 화면 녹화 허용 (**전체 화면** 선택) → 게임 열기
5. 정지: 알림창의 **■ 정지** 버튼 또는 앱의 재우기

## 메모
- LLM 주소에 PC에서 돌리는 FreeLLMAPI를 쓰려면 `http://PC의IP:포트/v1` (같은 와이파이)
- 서명키(app/growai.keystore)가 고정이라 새 버전을 덮어 설치해도 기억이 유지됨
- 화면 캡처를 막아둔 게임은 검은 화면으로 보여서 플레이 불가
