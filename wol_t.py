#!/usr/bin/env python3
"""
Telegram Bot for Wake-on-LAN
텔레그램 메시지를 수신하여 WOL 패킷을 전송합니다.
실행 시 systemd 서비스로 자동 등록됩니다.
"""

import os
import sys
import shutil
import socket
import stat
import time
import logging
import getpass
import argparse
import subprocess
import textwrap
import requests
from logging.handlers import RotatingFileHandler
from pathlib import Path

SERVICE_NAME  = "wol_t"
SERVICE_FILE  = f"/etc/systemd/system/{SERVICE_NAME}.service"
CONFIG_FILE   = Path(__file__).parent / ".env"
LOG_DIR       = Path(f"/var/log/{SERVICE_NAME}")
LOG_FILE      = LOG_DIR / f"{SERVICE_NAME}.log"
BROADCAST_IP  = "255.255.255.255"
WOL_PORT      = 9

# 런타임에 load_config()로 채워지는 설정값
TELEGRAM_BOT_TOKEN: str = ""
ALLOWED_USER_ID:    int = 0
TARGET_MAC_ADDRESS: str = ""


# ── .env 파싱 ──────────────────────────────────
def _parse_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def load_config() -> None:
    global TELEGRAM_BOT_TOKEN, ALLOWED_USER_ID, TARGET_MAC_ADDRESS

    if not CONFIG_FILE.exists():
        print(f"설정 파일이 없습니다. 먼저 설정을 입력하세요:\n  python3 wol_t.py config")
        sys.exit(1)

    env = _parse_env_file(CONFIG_FILE)

    TELEGRAM_BOT_TOKEN = env.get("TELEGRAM_BOT_TOKEN", "")
    TARGET_MAC_ADDRESS = env.get("TARGET_MAC_ADDRESS", "")
    try:
        ALLOWED_USER_ID = int(env.get("ALLOWED_USER_ID", "0"))
    except ValueError:
        ALLOWED_USER_ID = 0


# ── 로깅 설정 ──────────────────────────────────
def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(funcName)s:%(lineno)d — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    console_handler.setLevel(logging.INFO)

    logger = logging.getLogger(SERVICE_NAME)
    logger.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


logger = setup_logging()


# ── WOL 패킷 전송 ──────────────────────────────
def send_wol_packet(mac: str) -> None:
    mac_clean = mac.replace(":", "").replace("-", "").upper()
    if len(mac_clean) != 12:
        raise ValueError(f"유효하지 않은 MAC 주소: {mac}")

    mac_bytes = bytes.fromhex(mac_clean)
    magic_packet = b"\xff" * 6 + mac_bytes * 16

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(magic_packet, (BROADCAST_IP, WOL_PORT))

    logger.info("WOL 패킷 전송 완료 → MAC: %s", mac)


# ── 설정값 검증 ────────────────────────────────
def validate_config() -> None:
    errors = []

    if not TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN이 설정되지 않았습니다.")
    if ALLOWED_USER_ID <= 0:
        errors.append("ALLOWED_USER_ID가 유효하지 않습니다 (양의 정수여야 합니다).")

    mac_clean = TARGET_MAC_ADDRESS.replace(":", "").replace("-", "")
    if len(mac_clean) != 12 or not all(c in "0123456789ABCDEFabcdef" for c in mac_clean):
        errors.append(f"TARGET_MAC_ADDRESS가 유효하지 않습니다: {TARGET_MAC_ADDRESS}")

    if errors:
        for e in errors:
            logger.critical("설정 오류: %s", e)
        sys.exit(1)

    logger.info("설정값 검증 통과")
    logger.info("  봇 토큰 : %s...%s", TELEGRAM_BOT_TOKEN[:6], TELEGRAM_BOT_TOKEN[-4:])
    logger.info("  허용 UID: %d", ALLOWED_USER_ID)
    logger.info("  MAC 주소: %s", TARGET_MAC_ADDRESS)


# ── 설정 대화형 입력 ───────────────────────────
def configure() -> None:
    print(f"=== {SERVICE_NAME} 설정 ===")
    print(f"설정 파일 위치: {CONFIG_FILE}\n")

    existing: dict[str, str] = {}
    if CONFIG_FILE.exists():
        existing = _parse_env_file(CONFIG_FILE)
        print("기존 설정이 있습니다. 빈 칸으로 두면 기존 값을 유지합니다.\n")

    def _prompt(label: str, key: str, secret: bool = False) -> str:
        current = existing.get(key, "")
        hint = f"[현재: {'*' * len(current) if secret else current}] " if current else ""
        prompt_fn = getpass.getpass if secret else input
        value = prompt_fn(f"{label} {hint}: ").strip()
        return value if value else current

    token   = _prompt("Telegram Bot Token", "TELEGRAM_BOT_TOKEN", secret=True)
    user_id = _prompt("허용할 Telegram User ID", "ALLOWED_USER_ID")
    mac     = _prompt("WOL 대상 MAC 주소 (예: AA:BB:CC:DD:EE:FF)", "TARGET_MAC_ADDRESS")

    # 간단한 형식 검증
    errors = []
    if not token:
        errors.append("Bot Token을 입력해야 합니다.")
    try:
        uid = int(user_id)
        if uid <= 0:
            raise ValueError
    except ValueError:
        errors.append("User ID는 양의 정수여야 합니다.")
        uid = 0
    mac_clean = mac.replace(":", "").replace("-", "")
    if len(mac_clean) != 12 or not all(c in "0123456789ABCDEFabcdef" for c in mac_clean):
        errors.append(f"MAC 주소 형식이 올바르지 않습니다: {mac}")

    if errors:
        print("\n입력 오류:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)

    content = textwrap.dedent(f"""\
        TELEGRAM_BOT_TOKEN={token}
        ALLOWED_USER_ID={uid}
        TARGET_MAC_ADDRESS={mac}
    """)

    CONFIG_FILE.write_text(content, encoding="utf-8")
    # 소유자만 읽기/쓰기 가능 (rw-------)
    CONFIG_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)

    print(f"\n설정이 저장되었습니다: {CONFIG_FILE}")
    print(f"파일 권한: {oct(CONFIG_FILE.stat().st_mode)[-3:]}")


# ── 로그 tail ──────────────────────────────────
def _tail_log(n: int) -> str:
    if not LOG_FILE.exists():
        return "(로그 파일 없음)"
    with LOG_FILE.open("r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    tail = "".join(lines[-n:])
    # Telegram 메시지 한도 4096자
    if len(tail) > 3800:
        tail = "...(생략)...\n" + tail[-3800:]
    return tail


# ── Telegram API helpers ───────────────────────
_API_BASE = "https://api.telegram.org/bot{token}/{method}"


def _api(method: str, **params) -> dict:
    url = _API_BASE.format(token=TELEGRAM_BOT_TOKEN, method=method)
    resp = requests.post(url, json=params, timeout=35)
    resp.raise_for_status()
    return resp.json()


def _send(chat_id: int, text: str, **kwargs) -> None:
    try:
        _api("sendMessage", chat_id=chat_id, text=text, **kwargs)
    except Exception:
        logger.exception("sendMessage 실패 (chat_id=%d)", chat_id)


# ── 텔레그램 봇 ────────────────────────────────
def run_bot() -> None:
    logger.info("텔레그램 봇 롱폴링 시작")

    offset: int | None = None
    retry_delay = 5

    while True:
        try:
            params: dict = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                params["offset"] = offset

            data = _api("getUpdates", **params)

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                _handle_update(update)

            retry_delay = 5

        except requests.exceptions.ConnectionError:
            logger.warning("네트워크 연결 실패 — %d초 후 재시도", retry_delay)
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)
        except requests.exceptions.HTTPError as exc:
            logger.error("API HTTP 오류: %s — %d초 후 재시도", exc, retry_delay)
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)
        except Exception:
            logger.exception("예상치 못한 오류 — %d초 후 재시도", retry_delay)
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)


def send_menu(chat_id: int) -> None:
    text = "제어 방식을 선택하세요 (WOL 부팅):"
    reply_markup = {
        "inline_keyboard": [
            [{"text": "🚀 매직 패킷 전송 (WOL)", "callback_data": "send_wol"}],
            [{"text": "📋 상태 확인", "callback_data": "check_status"},
             {"text": "📄 로그 보기", "callback_data": "show_log"}]
        ]
    }
    _send(chat_id, text, reply_markup=reply_markup)


def send_log_menu(chat_id: int) -> None:
    text = "출력할 로그 줄수를 선택하세요:"
    reply_markup = {
        "inline_keyboard": [
            [{"text": "30줄", "callback_data": "show_log:30"},
             {"text": "50줄", "callback_data": "show_log:50"},
             {"text": "100줄", "callback_data": "show_log:100"}],
            [{"text": "◀ 뒤로", "callback_data": "back_to_menu"}]
        ]
    }
    _send(chat_id, text, reply_markup=reply_markup)


def _handle_update(update: dict) -> None:
    if "callback_query" in update:
        cq = update["callback_query"]
        chat_id = cq["message"]["chat"]["id"]
        uid = cq["from"]["id"]
        data = cq.get("data", "")

        logger.debug("콜백 수신 — user_id=%d, data=%r", uid, data)

        if uid != ALLOWED_USER_ID:
            logger.warning("허가되지 않은 접근 차단 (콜백) — user_id=%d", uid)
            try:
                _api("answerCallbackQuery", callback_query_id=cq["id"])
            except Exception:
                pass
            return

        try:
            if data == "send_wol":
                logger.info("콜백: send_wol — user_id=%d", uid)
                try:
                    send_wol_packet(TARGET_MAC_ADDRESS)
                    _api("answerCallbackQuery", callback_query_id=cq["id"], text="WOL 패킷 전송 완료")
                    _send(chat_id, f"✅ WOL 패킷을 전송했습니다.\nMAC: {TARGET_MAC_ADDRESS}")
                except Exception as exc:
                    logger.exception("WOL 패킷 전송 실패")
                    _api("answerCallbackQuery", callback_query_id=cq["id"], text="전송 실패")
                    _send(chat_id, f"❌ 전송 실패: {exc}")
                send_menu(chat_id)

            elif data == "check_status":
                logger.info("콜백: check_status — user_id=%d", uid)
                _api("answerCallbackQuery", callback_query_id=cq["id"])
                _send(chat_id,
                      f"ℹ️ 봇 정상 동작 중\n"
                      f"대상 MAC: {TARGET_MAC_ADDRESS}\n"
                      f"로그 파일: {LOG_FILE}")
                send_menu(chat_id)

            elif data == "show_log":
                logger.info("콜백: show_log — user_id=%d", uid)
                _api("answerCallbackQuery", callback_query_id=cq["id"])
                send_log_menu(chat_id)

            elif data.startswith("show_log:"):
                n = max(1, min(int(data.split(":")[1]), 100))
                logger.info("콜백: show_log:%d — user_id=%d", n, uid)
                _api("answerCallbackQuery", callback_query_id=cq["id"])
                log_text = _tail_log(n)
                _send(chat_id, f"📄 최근 로그 {n}줄:\n\n{log_text}")
                send_menu(chat_id)

            elif data == "back_to_menu":
                _api("answerCallbackQuery", callback_query_id=cq["id"])
                send_menu(chat_id)

        except Exception:
            logger.exception("콜백 처리 중 오류")

        return

    message = update.get("message")
    if not message:
        return

    chat_id = message["chat"]["id"]
    uid     = message["from"]["id"]
    text    = message.get("text", "")

    logger.debug("메시지 수신 — user_id=%d, text=%r", uid, text)

    if uid != ALLOWED_USER_ID:
        logger.warning("허가되지 않은 접근 차단 — user_id=%d", uid)
        return

    if text.startswith("/wol"):
        logger.info("/wol 수신 — user_id=%d", uid)
        try:
            send_wol_packet(TARGET_MAC_ADDRESS)
            _send(chat_id, f"✅ WOL 패킷을 전송했습니다.\nMAC: {TARGET_MAC_ADDRESS}")
        except Exception as exc:
            logger.exception("WOL 패킷 전송 실패")
            _send(chat_id, f"❌ 전송 실패: {exc}")
        send_menu(chat_id)

    elif text.startswith("/status"):
        _send(chat_id,
              f"ℹ️ 봇 정상 동작 중\n"
              f"대상 MAC: {TARGET_MAC_ADDRESS}\n"
              f"로그 파일: {LOG_FILE}")
        send_menu(chat_id)

    elif text.startswith("/log"):
        parts = text.split()
        try:
            lines = max(1, min(int(parts[1]), 100)) if len(parts) > 1 else 30
            log_text = _tail_log(lines)
            _send(chat_id, f"📄 최근 로그 {lines}줄:\n\n{log_text}")
        except ValueError:
            _send(chat_id, "사용법: /log [줄수]  (예: /log 50, 최대 100)")
        send_menu(chat_id)

    else:
        # /start 이거나 그 외 일반 텍스트일 때는 인라인 키보드 메뉴 제공
        if text.startswith("/start"):
            logger.info("/start 수신 — user_id=%d", uid)
        send_menu(chat_id)


# ── systemd 서비스 등록/제거 ───────────────────
def install_service() -> None:
    if os.geteuid() != 0:
        logger.error("서비스 설치는 root 권한이 필요합니다. sudo 로 실행하세요.")
        sys.exit(1)

    script_path = Path(os.path.abspath(__file__))
    python_path = sys.executable

    service_content = textwrap.dedent(f"""\
        [Unit]
        Description={SERVICE_NAME} Telegram WOL Bot
        After=network-online.target
        Wants=network-online.target

        [Service]
        Type=simple
        ExecStart={python_path} {script_path} run
        Restart=always
        RestartSec=10
        StandardOutput=journal
        StandardError=journal
        SyslogIdentifier={SERVICE_NAME}

        [Install]
        WantedBy=multi-user.target
    """)

    Path(SERVICE_FILE).write_text(service_content, encoding="utf-8")
    logger.info("서비스 파일 작성: %s", SERVICE_FILE)

    for cmd in (
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", SERVICE_NAME],
        ["systemctl", "start",  SERVICE_NAME],
    ):
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.error("명령 실패 %s: %s", " ".join(cmd), result.stderr.strip())
            sys.exit(1)
        logger.info("실행: %s", " ".join(cmd))

    status = subprocess.run(
        ["systemctl", "is-active", SERVICE_NAME], capture_output=True, text=True
    )
    logger.info("서비스 상태: %s", status.stdout.strip())
    print(f"\n서비스 '{SERVICE_NAME}' 설치 및 시작 완료.")
    print(f"로그 확인: journalctl -u {SERVICE_NAME} -f")
    print(f"또는:      tail -f {LOG_FILE}")


def uninstall_service(remove_logs: bool = True) -> None:
    if os.geteuid() != 0:
        logger.error("서비스 제거는 root 권한이 필요합니다.")
        sys.exit(1)

    for cmd in (
        ["systemctl", "stop",    SERVICE_NAME],
        ["systemctl", "disable", SERVICE_NAME],
    ):
        result = subprocess.run(cmd, capture_output=True, text=True)
        logger.info("실행: %s → %s", " ".join(cmd),
                    "완료" if result.returncode == 0 else result.stderr.strip())

    service_path = Path(SERVICE_FILE)
    if service_path.exists():
        service_path.unlink()
        logger.info("서비스 파일 삭제: %s", SERVICE_FILE)
        print(f"서비스 파일 삭제: {SERVICE_FILE}")
    else:
        logger.warning("서비스 파일 없음 (이미 삭제됨): %s", SERVICE_FILE)

    subprocess.run(["systemctl", "daemon-reload"], capture_output=True)

    if remove_logs:
        if LOG_DIR.exists():
            shutil.rmtree(LOG_DIR)
            print(f"로그 디렉토리 삭제: {LOG_DIR}")
        else:
            print(f"로그 디렉토리 없음 (이미 삭제됨): {LOG_DIR}")
    else:
        print(f"로그 파일 유지: {LOG_DIR}")

    print(f"\n서비스 '{SERVICE_NAME}' 제거 완료.")


def restart_service() -> None:
    if os.geteuid() != 0:
        logger.error("서비스 재시작은 root 권한이 필요합니다. sudo 로 실행하세요.")
        sys.exit(1)

    result = subprocess.run(
        ["systemctl", "restart", SERVICE_NAME], capture_output=True, text=True
    )
    if result.returncode != 0:
        logger.error("재시작 실패: %s", result.stderr.strip())
        sys.exit(1)

    status = subprocess.run(
        ["systemctl", "is-active", SERVICE_NAME], capture_output=True, text=True
    )
    print(f"서비스 재시작 완료 — 상태: {status.stdout.strip()}")


def show_log_cli(n: int) -> None:
    n = max(1, min(n, 100))
    print(_tail_log(n))


def show_status() -> None:
    result = subprocess.run(
        ["systemctl", "status", SERVICE_NAME], capture_output=True, text=True
    )
    print(result.stdout or result.stderr)


# ── 도움말 ─────────────────────────────────────
HELP_TEXT = textwrap.dedent(f"""\
    {SERVICE_NAME} — Telegram WOL Bot

    사용법:
      python3 wol_t.py <command> [options]

    Commands:
      config             설정값 입력 및 저장 (.env)
      install            systemd 서비스로 등록하고 시작합니다  (sudo 필요)
      restart            서비스를 재시작합니다  (sudo 필요)
      uninstall          서비스를 중지·삭제하고 로그를 제거합니다  (sudo 필요)
      status             서비스 동작 상태를 확인합니다
      log [N]            로그 파일 마지막 N줄 출력 (기본 30, 최대 100)
      run                봇을 포그라운드에서 직접 실행합니다

    Bot Commands (텔레그램):
      /wol               WOL 패킷 전송
      /status            봇 상태 및 MAC 확인
      /log               최근 로그 30줄 출력
      /log N             최근 로그 N줄 출력 (최대 100)

    Options (uninstall 전용):
      --keep-logs        로그 파일을 삭제하지 않고 유지합니다

    시작 순서:
      python3 wol_t.py config            # 1. 설정 입력
      sudo python3 wol_t.py install      # 2. 서비스 등록

    로그 위치:
      {LOG_FILE}
      journalctl -u {SERVICE_NAME} -f
""")


# ── 진입점 ─────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        prog="wol_t.py",
        description=f"{SERVICE_NAME} — Telegram WOL Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=HELP_TEXT,
        add_help=True,
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("config",  help="설정값 입력 및 저장 (.env)")
    sub.add_parser("run",     help="봇 직접 실행 (서비스에서 호출)")
    sub.add_parser("install",  help="systemd 서비스로 등록 (sudo 필요)")
    sub.add_parser("restart",  help="서비스 재시작 (sudo 필요)")
    sub.add_parser("status",   help="서비스 상태 확인")

    p_log = sub.add_parser("log", help="로그 파일 출력 (기본 30줄, 최대 100)")
    p_log.add_argument("n", nargs="?", type=int, default=30,
                       help="출력할 줄수 (기본값: 30, 최대: 100)")

    p_uninstall = sub.add_parser("uninstall", help="서비스 + 로그 삭제 (sudo 필요)")
    p_uninstall.add_argument(
        "--keep-logs",
        action="store_true",
        help="로그 파일을 삭제하지 않고 유지합니다.",
    )

    args = parser.parse_args()

    if args.command == "config":
        configure()
    elif args.command == "install":
        load_config()
        validate_config()
        install_service()
    elif args.command == "restart":
        restart_service()
    elif args.command == "uninstall":
        uninstall_service(remove_logs=not args.keep_logs)
    elif args.command == "status":
        show_status()
    elif args.command == "log":
        show_log_cli(args.n)
    elif args.command == "run":
        load_config()
        validate_config()
        run_bot()
    else:
        print(HELP_TEXT)
        parser.exit(0)


if __name__ == "__main__":
    main()
