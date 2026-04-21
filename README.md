# wol_t

텔레그램 봇으로 Wake-on-LAN 매직 패킷을 전송하는 단일 파일 Python 스크립트.  
systemd 서비스로 자동 등록되며, 재부팅 후에도 자동 실행됩니다.

---

## 기능

- 텔레그램 메시지 및 인라인 키보드로 원격 WOL 패킷 전송
- 지정된 1명의 사용자만 허용 (미인증 접근 시 무응답 처리 및 로깅)
- systemd 서비스 자동 등록 / 재시작 / 제거 (스크립트 자체가 관리)
- 서비스 장애 및 재부팅 시 자동 재시작
- 텔레그램으로 실시간 로그 조회
- 네트워크 오류 시 지수 백오프 자동 재시도
- 로테이팅 로그 파일 (5 MB × 최대 3개)

### 텔레그램 봇 명령어

| 명령어 | 설명 |
|--------|------|
| `/start` | 인라인 키보드 메뉴 출력 |
| `/wol` | WOL 매직 패킷 전송 |
| `/status` | 봇 상태 및 대상 MAC 주소 확인 |
| `/log` | 최근 로그 30줄 출력 |
| `/log N` | 최근 로그 N줄 출력 (최대 100) |

명령어 입력 없이 `/start` 또는 아무 메시지를 보내면 버튼 메뉴가 표시됩니다.

---

## 요구사항

- Python 3.10 이상
- `requests` 라이브러리
- Linux + systemd (서비스 등록 기능 사용 시)
- 대상 PC의 BIOS/UEFI에서 Wake-on-LAN 활성화 필요

```bash
pip install requests
```

---

## 설정

`config` 명령으로 대화형 프롬프트에서 입력합니다. 입력값은 스크립트와 같은 디렉토리의 `.env`에 저장됩니다.

```bash
python3 wol_t.py config
```

```
=== wol_t 설정 ===
Telegram Bot Token : 
허용할 Telegram User ID : 
WOL 대상 MAC 주소 (예: AA:BB:CC:DD:EE:FF) : 
```

- Bot Token은 입력 시 화면에 표시되지 않습니다 (getpass)
- 기존 설정이 있으면 빈 칸으로 두어 유지할 수 있습니다
- 저장된 `.env`는 파일 권한 `600`(소유자만 읽기/쓰기)으로 설정됩니다

**토큰 발급:** 텔레그램에서 [@BotFather](https://t.me/BotFather) → `/newbot`

**유저 ID 확인:** 텔레그램에서 [@userinfobot](https://t.me/userinfobot) → `/start`

---

## 사용법

```
python3 wol_t.py <command> [options]
```

| command | 설명 | 권한 |
|---------|------|------|
| `config` | 설정값 입력 및 `.env` 저장 | - |
| `install` | systemd 서비스 등록 및 시작 | sudo |
| `restart` | 서비스 재시작 (코드 변경 후 적용) | sudo |
| `uninstall` | 서비스 중지·삭제 + 로그 삭제 | sudo |
| `status` | 서비스 동작 상태 확인 | - |
| `run` | 포그라운드에서 직접 실행 | - |

### 시작 순서

```bash
python3 wol_t.py config        # 1. 설정 입력
sudo python3 wol_t.py install  # 2. 서비스 등록
```

### 서비스 등록

```bash
sudo python3 wol_t.py install
```

등록하면 다음이 자동으로 처리됩니다.

- `/etc/systemd/system/wol_t.service` 파일 생성
- `systemctl enable` — 부팅 시 자동 시작 등록
- `systemctl start` — 즉시 서비스 시작

### 서비스 제거

```bash
# 서비스 + 로그 디렉토리 모두 삭제
sudo python3 wol_t.py uninstall

# 로그는 남기고 서비스만 제거
sudo python3 wol_t.py uninstall --keep-logs
```

### 상태 확인

```bash
python3 wol_t.py status
```

### 직접 실행 (테스트 용도)

```bash
python3 wol_t.py run
```

---

## 로그

| 경로 | 설명 |
|------|------|
| `/var/log/wol_t/wol_t.log` | 파일 로그 (DEBUG 레벨 이상) |
| `journalctl -u wol_t -f` | systemd 저널 (INFO 레벨 이상) |

로그는 5 MB 초과 시 자동 롤링되며 최대 3개 파일을 유지합니다.

```bash
# 실시간 파일 로그 확인
tail -f /var/log/wol_t/wol_t.log

# systemd 저널 실시간 확인
journalctl -u wol_t -f
```

---

## 구현 상세

### 파일 구성

```
wol_t/
├── wol_t.py          # 단일 실행 스크립트
├── .env              # 설정값 (git 제외)
├── .env.example      # 설정 형식 예시
├── .gitignore        # .env 제외 설정
└── requirements.txt  # requests>=2.28
```

### 설정 파일 (.env)

외부 라이브러리(`python-dotenv` 등) 없이 직접 파싱합니다.  
`key=value` 형식이며 `#` 주석을 지원합니다.  
저장 시 `chmod 600`으로 소유자 외 접근을 차단합니다.

```bash
TELEGRAM_BOT_TOKEN=your_token
ALLOWED_USER_ID=123456789
TARGET_MAC_ADDRESS=AA:BB:CC:DD:EE:FF
```

### WOL 매직 패킷

`0xFF × 6` + `MAC 주소 × 16` 으로 구성된 102바이트 매직 패킷을  
UDP 소켓으로 브로드캐스트 주소(`255.255.255.255:9`)에 전송합니다.

```python
magic_packet = b"\xff" * 6 + mac_bytes * 16
sock.sendto(magic_packet, ("255.255.255.255", 9))
```

### Telegram 롱폴링

`python-telegram-bot` 같은 프레임워크 없이 `requests`로 Bot API를 직접 호출합니다.

- `getUpdates?timeout=30` — 서버가 최대 30초 동안 연결을 유지하다 새 메시지가 오면 즉시 반환
- `offset` 파라미터로 이미 처리한 업데이트를 건너뜀
- 네트워크 오류 / HTTP 오류 / 예외 발생 시 지수 백오프(5초 → 10초 → … 최대 60초) 후 재시도

### 접근 제어

모든 수신 메시지와 콜백의 `from.id`를 `ALLOWED_USER_ID`와 비교합니다.  
일치하지 않으면 아무 응답 없이 무시하고 경고 로그만 남깁니다.

### systemd 서비스

`install` 실행 시 스크립트가 직접 서비스 유닛 파일을 생성합니다.

```ini
[Service]
Restart=always
RestartSec=10
```

프로세스가 비정상 종료되면 10초 후 자동 재시작되며, 부팅 시에도 네트워크가 준비된 후 자동으로 시작됩니다(`After=network-online.target`).

### 로깅

- `RotatingFileHandler` — 5 MB 초과 시 롤링, 최대 3개 보관
- 파일: DEBUG 레벨 이상 전체 기록
- 콘솔(journald): INFO 레벨 이상 출력
- 포맷: `날짜 시간 [레벨] 함수명:줄번호 — 메시지`
