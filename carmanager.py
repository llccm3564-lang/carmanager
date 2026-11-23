"""
카매니저 매물 확인 프로그램

기능:
  1. 카매니저 전체 매물 정보 수집 (차종, 가격, 연식, 주행거리, 연료, 변속기, 사고, 번호판, 지역, 판매자 등)
  2. 차량 사진 다운로드 (상세 페이지에서 여러 장)
  3. 사라진 매물(팔림/삭제) 자동 감지
  4. CSV 보고서 생성

사용법:
  py carmanager.py              (아이디/비번 직접 입력)
  py carmanager.py --headless   (화면 없이 백그라운드 실행, .env에서 계정 읽음)

재실행하면 이전 데이터와 비교하여 사라진 매물을 자동 감지한다.
"""

import sqlite3
import os
import sys
import time
import csv
import json
import re
import getpass
import random
from datetime import datetime, timedelta
from urllib.parse import urljoin, quote, unquote

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    UnexpectedAlertPresentException, TimeoutException, WebDriverException
)
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup
import requests

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

# ── 로그 자동 저장 (화면 + log.txt 동시 출력) ──
_script_dir = os.path.dirname(os.path.abspath(__file__))
_log_path = os.path.join(_script_dir, "log.txt")

class _Tee:
    def __init__(self, *streams):
        self._streams = streams
    def write(self, text):
        for s in self._streams:
            try:
                s.write(text)
                s.flush()
            except Exception:
                pass
    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass

# log.txt 는 이어 쓰기 (쉬고 끝나는 실행이 지난 실행 로그를 덮어쓰지 않게). 5MB 넘으면 log.txt.old 로 넘김.
_로그_시작위치 = 0
try:
    if os.path.exists(_log_path) and os.path.getsize(_log_path) > 5_000_000:
        os.replace(_log_path, _log_path + ".old")
    _로그_시작위치 = os.path.getsize(_log_path) if os.path.exists(_log_path) else 0
except Exception:
    pass

try:
    _log_file = open(_log_path, "a", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, _log_file)
    sys.stderr = _Tee(sys.__stderr__, _log_file)
except Exception:
    pass

# ── 로그를 Supabase에 실시간으로 올리기 ──
# 예전: 끝날 때 한 번만 올림 → 강제 종료·긴 실행이면 아무것도 안 보였음.
# 지금: 1분마다 새로 생긴 줄만 error_logs 에 새 행으로 추가 (표는 추가·읽기만 허용이라 수정 대신 추가).
#   pc_name = "PC이름|실행ID|순번"  (마지막 조각은 "|끝")  → 같은 실행ID 를 순번대로 이어 붙이면 전체 로그
#   새 줄이 5분 넘게 없으면 "(진행 중 …)" 행을 올려 살아 있는지 알림.
#   정상 종료·Ctrl+C·창 닫기 때도 남은 부분을 올림.
import atexit
import threading

_로그_실행ID = datetime.now().strftime("%m%d-%H%M%S")
_로그_PC = os.environ.get("COMPUTERNAME", "unknown")
_로그_위치 = _로그_시작위치  # log.txt 에서 이미 올린 바이트 위치 (이번 실행 시작 지점부터)
_로그_순번 = 0
_로그_마지막전송 = time.time()
_로그_잠금 = threading.Lock()
_로그_끝냄 = False


def _로그_건너뛰기():
    """쉬는 시간 등으로 바로 끝나는 실행은 Supabase 에 로그를 안 올림 (5분마다 빈 로그가 쌓이지 않게)."""
    global _로그_끝냄
    _로그_끝냄 = True


def _로그_행_추가(내용, 끝=False):
    """error_logs 에 한 행 추가. 성공하면 True."""
    global _로그_순번
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        return False
    표시 = f"{_로그_PC}|{_로그_실행ID}|{_로그_순번:03d}" + ("|끝" if 끝 else "")
    try:
        r = requests.post(
            f"{url}/rest/v1/error_logs",
            headers={"apikey": key, "Authorization": f"Bearer {key}",
                     "Content-Type": "application/json", "Prefer": "return=minimal"},
            json={"content": 내용, "pc_name": 표시},
            timeout=15,
        )
        if r.status_code in (200, 201, 204):
            _로그_순번 += 1
            return True
        sys.__stderr__.write(f"  [로그 올리기 실패] 상태={r.status_code} {r.text[:200]}\n")
    except Exception as e:
        try:
            sys.__stderr__.write(f"  [로그 올리기 실패] {str(e)[:200]}\n")
        except Exception:
            pass
    return False


def _로그_보내기(끝=False):
    """log.txt 에서 아직 안 올린 부분을 올린다. 실패하면 위치를 그대로 둬서 다음에 다시 올림."""
    global _로그_위치, _로그_마지막전송, _로그_끝냄
    if not _로그_잠금.acquire(timeout=20):
        return
    try:
        if _로그_끝냄:
            return
        try:
            _log_file.flush()
        except Exception:
            pass
        try:
            with open(_log_path, "rb") as f:
                f.seek(_로그_위치)
                새것 = f.read()
        except Exception:
            새것 = b""
        if not 끝:
            # 줄 중간(한글 글자 중간)에서 자르지 않게 마지막 줄바꿈까지만
            k = 새것.rfind(b"\n")
            새것 = 새것[:k + 1] if k >= 0 else b""
        if 새것:
            # 한 행에 약 100KB 까지, 가능하면 줄바꿈에서 자름 (바이트 단위로 세서 위치가 어긋나지 않게)
            pos = 0
            while pos < len(새것):
                끝위치 = min(pos + 100000, len(새것))
                if 끝위치 < len(새것):
                    k = 새것.rfind(b"\n", pos, 끝위치)
                    if k > pos:
                        끝위치 = k + 1
                    else:
                        # 줄바꿈이 없으면 한글 글자(UTF-8 여러 바이트) 중간이 아닌 곳에서 자름
                        while 끝위치 > pos + 1 and (새것[끝위치] & 0xC0) == 0x80:
                            끝위치 -= 1
                조각 = 새것[pos:끝위치]
                마지막 = 끝 and 끝위치 >= len(새것)
                if not _로그_행_추가(조각.decode("utf-8", errors="replace"), 끝=마지막):
                    return
                _로그_위치 += len(조각)
                pos = 끝위치
            _로그_마지막전송 = time.time()
        elif 끝:
            _로그_행_추가("(실행 끝)", 끝=True)
        elif time.time() - _로그_마지막전송 > 300:
            if _로그_행_추가(f"  (진행 중 — 새 줄 없음 {int((time.time() - _로그_마지막전송) // 60)}분, "
                             f"{datetime.now().strftime('%H:%M:%S')})"):
                _로그_마지막전송 = time.time()
        if 끝:
            _로그_끝냄 = True
    finally:
        _로그_잠금.release()


def _로그_주기적_보내기():
    time.sleep(10)  # 시작 직후 한 번 (실행이 시작됐는지 바로 보이게)
    while not _로그_끝냄:
        _로그_보내기()
        time.sleep(60)


threading.Thread(target=_로그_주기적_보내기, daemon=True).start()
atexit.register(lambda: _로그_보내기(끝=True))

# 창 닫기·로그오프·Ctrl+Break 때도 남은 로그 올리기 (윈도우 콘솔 신호)
if os.name == "nt":
    try:
        import ctypes
        _콘솔처리기형 = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

        def _콘솔신호(신호):
            # 0=Ctrl+C 는 파이썬이 KeyboardInterrupt 로 처리 → 정상 종료 경로(atexit)로 올라감
            if 신호 in (1, 2, 5, 6):  # Ctrl+Break, 창 닫기, 로그오프, 시스템 종료
                try:
                    print(f"\n  [종료 신호 {신호}] 남은 로그 올리는 중...")
                    _로그_보내기(끝=True)
                except Exception:
                    pass
            return False

        _콘솔처리기 = _콘솔처리기형(_콘솔신호)  # 참조 유지 (지우면 처리기가 사라짐)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_콘솔처리기, True)
    except Exception:
        pass

try:
    from supabase import create_client
except ImportError:
    create_client = None


# ════════════════════════════════════════════════════════════
# 설정
# ════════════════════════════════════════════════════════════
사이트 = "https://www.carmanager.co.kr"
DB_파일 = "매물_데이터.db"
사진_폴더 = "사진"
보고서_파일 = "매물_보고서.csv"
최대_페이지 = 0  # 0 = 제한 없음 (실제 한도는 아래 한번_목록_페이지·하루_목록_페이지)
상세_사진_배치 = 20  # (예전 이름) 한 번에 상세 받을 차량 수 = 한번_상세_대수
# 실행 안에서 쿠키 지우고 다시 로그인하는 것은 끔 (11-23: 같은 계정 재로그인이 오히려 튐 → 0 = 안 함)
세션_갱신_페이지 = 0
세션_갱신_상세 = 0

# ── 속도·양 (11-23 gkvlfrl 이용제한 뒤 정함: 목록 59페이지/25분 만에 막힘) ──
목록_수집_켜짐 = False     # 11-23 오찬: 목록은 일단 빼 둠 (이미 받은 차 상세부터). 다시 켤 땐 True
한번_목록_페이지 = 7       # 실행 한 번에 목록 최대 7페이지 (350대)
한번_상세_대수 = 20        # 실행 한 번에 상세 최대 20대
하루_목록_페이지 = 20      # 하루 목록 최대 20페이지 (1,000대)
하루_상세_대수 = 200       # 하루 상세 최대 200대
목록_바퀴_간격_시간 = 48   # 한 지역 목록을 끝까지 다 훑으면 이틀 뒤에 다시
대기_범위 = (20, 40)       # 목록 페이지 사이·상세 차 사이 랜덤 대기(초)
실행_간격_분 = (30, 60)    # 실행 끝나고 다음 실행까지 랜덤(분). bat 은 5분마다 켜지만 이 시간 전엔 바로 쉬고 끝남
쉬는_시간 = (1, 7)         # 01:00~06:59 는 안 돌림
# 손으로 시험할 때만: --detail=100 → 이번 실행만 상세 100대 (하루 상한도 그만큼 허용)
for _a in sys.argv:
    if _a.startswith("--detail="):
        try:
            한번_상세_대수 = max(1, int(_a.split("=", 1)[1]))
            하루_상세_대수 = max(하루_상세_대수, 한번_상세_대수)
        except ValueError:
            pass
제한_문구 = ("사용이 제한", "이용제한", "보안 정책에 위배")
중지_파일 = os.path.join(_script_dir, "중지.txt")          # 있으면 아무것도 안 함 (이용제한 감지 때 생김)
실행상태_파일 = os.path.join(_script_dir, "실행상태.json")  # 다음 실행 가능 시각

# 수집 지역 고정 (계정 기본 단지를 무시하고 이 값으로 검색)
# 103/1038/96502 = 인천 서구 / 엠파크타워 (11-19 옛 계정 페이지에서 확인)
검색_시도 = os.getenv("CM_SIDO", "103")
검색_지역 = os.getenv("CM_AREA", "1038")
검색_단지 = os.getenv("CM_DANJI", "96502")

# 목록을 끝 페이지까지 다 받았을 때만 True → 이때만 「사라짐」 처리 (중간에 끊기면 멀쩡한 차가 사라짐으로 찍힘)
목록_완료 = False
목록_신규 = 0  # 이번 실행 목록에서 새로 등록된 차 수 (페이지마다 저장하면서 셈)
HEADLESS = "--headless" in sys.argv

# Supabase (.env 파일에서 읽음)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
supabase_client = None


# ════════════════════════════════════════════════════════════
# Supabase
# ════════════════════════════════════════════════════════════
def Supabase_초기화():
    global supabase_client
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("  Supabase 미설정 → 로컬 DB만 사용")
        return False
    if not create_client:
        print("  supabase 패키지 없음 → 로컬 DB만 사용")
        return False
    try:
        supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
        supabase_client.table("car_listings").select("count", count="exact").limit(0).execute()
        print("  Supabase 연결 성공!")
        return True
    except Exception as e:
        print(f"  Supabase 연결 실패: {e}")
        print("  → 로컬 DB만 사용")
        supabase_client = None
        return False


def Supabase_차량_저장(차량_목록):
    if not supabase_client:
        return
    지금 = datetime.now().isoformat()
    테이블 = supabase_client.table("car_listings")

    for 차 in 차량_목록:
        번호 = 차.get("차량번호", "")
        if not 번호:
            continue

        사진들 = 차.get("사진URL들", [])

        row = {
            "car_id": 번호,
            "model": 차.get("차종", ""),
            "price": 차.get("가격", ""),
            "year": 차.get("연식", ""),
            "mileage": 차.get("주행거리", ""),
            "fuel": 차.get("연료", ""),
            "transmission": 차.get("변속기", ""),
            "color": 차.get("색상", ""),
            "accident": 차.get("사고여부", ""),
            "plate": 차.get("번호판", ""),
            "region": 차.get("지역", ""),
            "seller": 차.get("판매자", ""),
            "phone": 차.get("연락처", ""),
            "photo_url": 차.get("사진URL", ""),
            "detail_url": 차.get("상세링크", ""),
            "status": "active",
            "last_seen": 지금,
            "removed_at": None,
        }

        if 사진들:
            row["photo_urls"] = json.dumps(사진들, ensure_ascii=False)

        try:
            기존 = 테이블.select("car_id").eq("car_id", 번호).execute()
            if 기존.data:
                테이블.update(row).eq("car_id", 번호).execute()
            else:
                row["first_seen"] = 지금
                테이블.insert(row).execute()
        except Exception:
            pass


def Supabase_사라진_처리(현재_차량번호들):
    if not supabase_client:
        return
    지금 = datetime.now().isoformat()
    테이블 = supabase_client.table("car_listings")
    현재 = set(현재_차량번호들)

    try:
        등록중 = 테이블.select("car_id").eq("status", "active").execute()
        for row in 등록중.data:
            if row["car_id"] not in 현재:
                테이블.update({
                    "status": "removed",
                    "removed_at": 지금
                }).eq("car_id", row["car_id"]).execute()
    except Exception:
        pass


# ════════════════════════════════════════════════════════════
# 로컬 데이터베이스 (SQLite)
# ════════════════════════════════════════════════════════════
def DB_연결():
    db = sqlite3.connect(DB_파일)
    db.row_factory = sqlite3.Row
    return db


def DB_초기화():
    db = DB_연결()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS 차량 (
            차량번호 TEXT PRIMARY KEY,
            차종 TEXT,
            가격 TEXT,
            연식 TEXT,
            주행거리 TEXT,
            연료 TEXT,
            변속기 TEXT,
            색상 TEXT,
            사고여부 TEXT,
            번호판 TEXT,
            지역 TEXT,
            판매자 TEXT,
            연락처 TEXT,
            사진URL TEXT,
            사진URL들 TEXT,
            성능기록부 TEXT,
            상세링크 TEXT,
            원본데이터 TEXT,
            최초발견 TEXT,
            최근확인 TEXT,
            상태 TEXT DEFAULT '등록중',
            사라진날 TEXT
        );

        CREATE TABLE IF NOT EXISTS 수집기록 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            시각 TEXT,
            수집_건수 INTEGER,
            신규_건수 INTEGER,
            사라진_건수 INTEGER
        );

        -- 하루에 사이트에 보낸 양 (하루 상한 지키기용)
        CREATE TABLE IF NOT EXISTS 하루사용 (
            날짜 TEXT PRIMARY KEY,
            상세 INTEGER DEFAULT 0,
            목록페이지 INTEGER DEFAULT 0,
            로그인 INTEGER DEFAULT 0
        );

        -- 지역별 목록 진행 (몇 페이지까지 훑었는지, 마지막으로 끝까지 훑은 때)
        CREATE TABLE IF NOT EXISTS 목록진행 (
            지역키 TEXT PRIMARY KEY,
            다음페이지 INTEGER,
            바퀴시작 TEXT,
            마지막완료 TEXT,
            전체건수 INTEGER
        );
    """)
    # 기존 DB에 없는 컬럼 추가
    for col in ["사진URL들 TEXT", "성능기록부 TEXT", "검색지역 TEXT", "기록부링크 TEXT"]:
        try:
            db.execute(f"ALTER TABLE 차량 ADD COLUMN {col}")
        except Exception:
            pass
    db.commit()
    db.close()


def 차량_저장(차량_목록):
    db = DB_연결()
    지금 = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    신규 = 0

    for 차 in 차량_목록:
        번호 = 차.get("차량번호", "")
        if not 번호:
            continue

        기존 = db.execute("SELECT 1 FROM 차량 WHERE 차량번호=?", (번호,)).fetchone()
        원본 = json.dumps(차, ensure_ascii=False)
        사진들 = 차.get("사진URL들", [])
        사진JSON = json.dumps(사진들, ensure_ascii=False) if 사진들 else None

        if 기존:
            if 사진JSON:
                db.execute("""
                    UPDATE 차량
                    SET 최근확인=?, 상태='등록중', 사라진날=NULL,
                        차종=?, 가격=?, 연식=?, 주행거리=?, 연료=?, 변속기=?,
                        색상=?, 사고여부=?, 번호판=?, 지역=?, 판매자=?, 연락처=?,
                        사진URL=?, 사진URL들=?, 상세링크=?, 원본데이터=?
                    WHERE 차량번호=?
                """, (
                    지금,
                    차.get("차종", ""), 차.get("가격", ""), 차.get("연식", ""),
                    차.get("주행거리", ""), 차.get("연료", ""), 차.get("변속기", ""),
                    차.get("색상", ""), 차.get("사고여부", ""), 차.get("번호판", ""),
                    차.get("지역", ""), 차.get("판매자", ""), 차.get("연락처", ""),
                    차.get("사진URL", ""), 사진JSON, 차.get("상세링크", ""), 원본,
                    번호
                ))
            else:
                db.execute("""
                    UPDATE 차량
                    SET 최근확인=?, 상태='등록중', 사라진날=NULL,
                        차종=?, 가격=?, 연식=?, 주행거리=?, 연료=?, 변속기=?,
                        색상=?, 사고여부=?, 번호판=?, 지역=?, 판매자=?, 연락처=?,
                        사진URL=?, 상세링크=?, 원본데이터=?
                    WHERE 차량번호=?
                """, (
                    지금,
                    차.get("차종", ""), 차.get("가격", ""), 차.get("연식", ""),
                    차.get("주행거리", ""), 차.get("연료", ""), 차.get("변속기", ""),
                    차.get("색상", ""), 차.get("사고여부", ""), 차.get("번호판", ""),
                    차.get("지역", ""), 차.get("판매자", ""), 차.get("연락처", ""),
                    차.get("사진URL", ""), 차.get("상세링크", ""), 원본,
                    번호
                ))
        else:
            db.execute("""
                INSERT INTO 차량
                (차량번호,차종,가격,연식,주행거리,연료,변속기,색상,사고여부,
                 번호판,지역,판매자,연락처,사진URL,사진URL들,상세링크,원본데이터,
                 최초발견,최근확인,상태)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'등록중')
            """, (
                번호,
                차.get("차종", ""), 차.get("가격", ""), 차.get("연식", ""),
                차.get("주행거리", ""), 차.get("연료", ""), 차.get("변속기", ""),
                차.get("색상", ""), 차.get("사고여부", ""), 차.get("번호판", ""),
                차.get("지역", ""), 차.get("판매자", ""), 차.get("연락처", ""),
                차.get("사진URL", ""), 사진JSON, 차.get("상세링크", ""), 원본,
                지금, 지금
            ))
            신규 += 1

    db.commit()
    db.close()
    return 신규


def 사라진_매물_처리(현재_차량번호들):
    db = DB_연결()
    지금 = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    현재 = set(현재_차량번호들)

    등록중 = db.execute(
        "SELECT 차량번호 FROM 차량 WHERE 상태='등록중'"
    ).fetchall()

    사라진 = 0
    for row in 등록중:
        if row["차량번호"] not in 현재:
            db.execute(
                "UPDATE 차량 SET 상태='사라짐', 사라진날=? WHERE 차량번호=?",
                (지금, row["차량번호"])
            )
            사라진 += 1

    db.commit()
    db.close()
    return 사라진


# ════════════════════════════════════════════════════════════
# 브라우저
# ════════════════════════════════════════════════════════════
def 브라우저_열기():
    옵션 = webdriver.ChromeOptions()
    옵션.add_argument("--lang=ko")
    옵션.add_argument("--disable-blink-features=AutomationControlled")
    옵션.add_argument("--no-sandbox")
    옵션.add_argument("--disable-dev-shm-usage")
    옵션.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
    if HEADLESS:
        옵션.add_argument("--headless=new")
        옵션.add_argument("--window-size=1920,1080")
    else:
        옵션.add_argument("--start-maximized")
    옵션.add_experimental_option("excludeSwitches", ["enable-automation"])
    옵션.add_experimental_option("useAutomationExtension", False)
    옵션.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    서비스 = Service(ChromeDriverManager().install())
    드라이버 = webdriver.Chrome(service=서비스, options=옵션)

    드라이버.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": """
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        window.chrome = {runtime: {}};
        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
        Object.defineProperty(navigator, 'languages', {get: () => ['ko-KR', 'ko', 'en-US', 'en']});

        window.__captured = [];

        (function() {
            var _open = XMLHttpRequest.prototype.open;
            var _send = XMLHttpRequest.prototype.send;
            XMLHttpRequest.prototype.open = function(method, url) {
                this.__method = method;
                this.__url = url;
                _open.apply(this, arguments);
            };
            XMLHttpRequest.prototype.send = function(body) {
                var xhr = this;
                xhr.addEventListener('load', function() {
                    try {
                        window.__captured.push({
                            type: 'xhr',
                            method: xhr.__method,
                            url: xhr.__url,
                            body: body || '',
                            status: xhr.status,
                            response: xhr.responseText.substring(0, 500000)
                        });
                    } catch(e) {}
                });
                _send.apply(this, arguments);
            };
        })();

        (function() {
            var _fetch = window.fetch;
            if (!_fetch) return;
            window.fetch = function(url, opts) {
                return _fetch.apply(this, arguments).then(function(resp) {
                    resp.clone().text().then(function(text) {
                        try {
                            window.__captured.push({
                                type: 'fetch',
                                method: (opts && opts.method) || 'GET',
                                url: typeof url === 'string' ? url : url.url,
                                body: (opts && opts.body) || '',
                                status: resp.status,
                                response: text.substring(0, 500000)
                            });
                        } catch(e) {}
                    });
                    return resp;
                });
            };
        })();
    """})

    return 드라이버


def 가로챈_응답_가져오기(드라이버):
    try:
        return 드라이버.execute_script("return window.__captured || [];") or []
    except Exception:
        return []


def 가로챈_응답_비우기(드라이버):
    try:
        드라이버.execute_script("window.__captured = [];")
    except Exception:
        pass


def alert_처리(드라이버):
    try:
        alert = 드라이버.switch_to.alert
        텍스트 = alert.text
        alert.accept()
        return 텍스트
    except Exception:
        return None


def 로그인(드라이버, 아이디, 비밀번호):
    하루사용_더하기("로그인")
    드라이버.get(f"{사이트}/User/Login")
    time.sleep(random.uniform(3, 6))

    msg = alert_처리(드라이버)
    if msg:
        print(f"  알림: {msg}")
        _제한_확인(msg, "로그인 페이지 알림")
    _제한_확인(드라이버.page_source, "로그인 페이지")

    try:
        id_input = 드라이버.find_element(By.CSS_SELECTOR, 'input[placeholder="아이디"]')
        pw_input = 드라이버.find_element(By.CSS_SELECTOR, 'input[placeholder="비밀번호"]')
    except Exception:
        try:
            id_input = 드라이버.find_element(By.CSS_SELECTOR, 'input[name="UserId"], input[name="userid"], input[type="text"]')
            pw_input = 드라이버.find_element(By.CSS_SELECTOR, 'input[name="UserPw"], input[name="userpw"], input[type="password"]')
        except Exception:
            print("  로그인 폼을 찾지 못했습니다.")
            return False

    id_input.clear()
    id_input.send_keys(아이디)
    pw_input.clear()
    pw_input.send_keys(비밀번호)

    try:
        for btn in 드라이버.find_elements(By.TAG_NAME, "button"):
            if "로그인" in btn.text:
                btn.click()
                break
        else:
            btn = 드라이버.find_element(By.CSS_SELECTOR, 'button[type="submit"], input[type="submit"]')
            btn.click()
    except Exception:
        pass

    time.sleep(random.uniform(4, 7))

    msg = alert_처리(드라이버)
    if msg:
        print(f"  알림: {msg}")
        _제한_확인(msg, "로그인 뒤 알림")
    _제한_확인(드라이버.page_source, "로그인 뒤 화면")

    if "/User/Login" in 드라이버.current_url:
        print("  로그인 실패 — 아이디/비밀번호를 확인하세요.")
        return False

    print("  로그인 성공!")
    return True


# ════════════════════════════════════════════════════════════
# 데이터 추출 — 전략 1: 가로챈 API 응답에서 추출
# ════════════════════════════════════════════════════════════
def API_응답에서_차량_추출(응답들):
    차량들 = []

    for resp in 응답들:
        url = resp.get("url", "")
        body = resp.get("response", "")
        if not body:
            continue

        try:
            data = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            if "<table" in body.lower() or "<tr" in body.lower():
                차량들.extend(HTML에서_차량_추출(body))
            continue

        found = _JSON에서_차량_찾기(data)
        차량들.extend(found)

    return 차량들


def _JSON에서_차량_찾기(data, depth=0):
    if depth > 5:
        return []

    차량들 = []

    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                keys = set(item.keys())
                차량_필드 = {"EnCarNo", "CarModelName", "CarAmountSale", "CarRegYear",
                           "CarUseKm", "CarFuel", "CarMission", "CarPlateNumber"}
                if keys & 차량_필드:
                    차량들.append(_JSON_차량_변환(item))
                else:
                    차량들.extend(_JSON에서_차량_찾기(item, depth + 1))

    elif isinstance(data, dict):
        keys = set(data.keys())
        차량_필드 = {"EnCarNo", "CarModelName", "CarAmountSale"}
        if keys & 차량_필드:
            차량들.append(_JSON_차량_변환(data))
        else:
            for v in data.values():
                if isinstance(v, (dict, list)):
                    차량들.extend(_JSON에서_차량_찾기(v, depth + 1))

    return 차량들


def _JSON_차량_변환(item):
    사진 = item.get("CarThumb", "") or item.get("CarPhoto", "") or ""
    if 사진 and not 사진.startswith("http"):
        사진 = urljoin(사이트, 사진)

    상세 = ""
    encar = item.get("EnCarNo", "")
    if encar:
        상세 = f"{사이트}/Car/Detail/{encar}"

    return {
        "차량번호": encar or item.get("CarNo", "") or str(item.get("Seq", "")),
        "차종": item.get("CarModelName", "") or item.get("CarName", ""),
        "가격": str(item.get("CarAmountSale", "")) + "만원" if item.get("CarAmountSale") else "",
        "연식": str(item.get("CarRegYear", "")),
        "주행거리": str(item.get("CarUseKm", "")) + "km" if item.get("CarUseKm") else "",
        "연료": item.get("CarFuel", "") or item.get("CarFuelName", ""),
        "변속기": item.get("CarMission", "") or item.get("CarMissionName", ""),
        "색상": item.get("CarColor", "") or item.get("CarColorName", ""),
        "사고여부": item.get("CarSagoYn", "") or item.get("CarSago", ""),
        "번호판": item.get("CarPlateNumber", "") or item.get("CarNo", ""),
        "지역": item.get("CarSiDo", "") or item.get("CarArea", ""),
        "판매자": item.get("UserName", "") or item.get("DealerName", ""),
        "연락처": item.get("UserHP", "") or item.get("DealerHP", ""),
        "사진URL": 사진,
        "상세링크": 상세,
    }


# ════════════════════════════════════════════════════════════
# 데이터 추출 — HTML 파싱 (카매니저 전용)
# ════════════════════════════════════════════════════════════
def HTML에서_차량_추출(html):
    soup = BeautifulSoup(html, "html.parser")
    차량들 = []

    for row in soup.select("tr.uc_carcss"):
        차 = {}
        tds = row.select("td")

        cb = row.select_one("input[type='checkbox']")
        if cb:
            차["차량번호"] = cb.get("value", "")
        if not 차.get("차량번호"):
            opt = row.select_one("[data-carno]")
            if opt:
                차["차량번호"] = opt.get("data-carno", "")
        if not 차.get("차량번호"):
            continue

        img = row.select_one("img.thumbnail")
        if img:
            src = img.get("src", "")
            차["사진URL"] = "" if "noimage" in src else src
        else:
            차["사진URL"] = ""

        maker = row.select_one("label.uc_carmaker")
        model_a = row.select_one("td.uc_textleft a")
        제조사 = maker.get_text(strip=True) if maker else ""
        모델명 = model_a.get_text(strip=True) if model_a else ""
        차["차종"] = f"{제조사} {모델명}".strip()

        if model_a:
            # 실제 형태: href="javascript:carmangerDetailWindowPopUp_CHECK('Bb654c...==')"
            # → 상세는 /PopupFrame/CarDetailEnc/ 에 encarno + EncCheck 를 POST (default.js)
            링크원문 = (model_a.get("href", "") or "") + " " + (model_a.get("onclick", "") or "")
            m = re.search(r"['\"]([A-Za-z0-9+/=]{10,})['\"]", 링크원문)
            if m:
                차["encarno"] = m.group(1)
                차["상세링크"] = _상세링크(m.group(1))

        reg = row.select_one("span.uc_carreg")
        if reg:
            차["연식"] = reg.get_text(strip=True)

        km = row.select_one(".uc_carusekm")
        if km:
            차["주행거리"] = km.get_text(strip=True)

        color = row.select_one(".uc_carcolor")
        if color:
            차["색상"] = color.get_text(strip=True)

        amount = row.select_one(".uc_caramount")
        if amount:
            차["가격"] = amount.get_text(strip=True) + "만원"

        area = row.select_one(".uc_cararea")
        if area:
            차["지역"] = " ".join(area.get_text(strip=True).split())

        dealer = row.select_one("span.uc_cardealer")
        if dealer:
            차["판매자"] = dealer.get_text(strip=True)
        phone = row.select_one("span.uc_cardealer_phone")
        if phone:
            차["연락처"] = phone.get_text(strip=True)

        plates = row.select("span.uc_carreg_year")
        if plates:
            차["번호판"] = plates[0].get_text(strip=True)

        if len(tds) >= 5:
            mission_p = tds[4].select_one("p")
            if mission_p:
                차["변속기"] = mission_p.get_text(strip=True)

        if len(tds) >= 7:
            차["연료"] = tds[6].get_text(strip=True)

        차량들.append(차)

    return 차량들


# ════════════════════════════════════════════════════════════
# 수집 엔진
# ════════════════════════════════════════════════════════════
def _지역_고정(드라이버):
    """검색 지역을 설정값으로 고정 (계정 기본 단지 무시). 제출 직전에 부른다.

    사이트 검색 함수(sCarData.js reLoadJsonCarDataMulti)는 hidden 칸이 아니라
    여러 개 고르기 상자 #searchSiDo_multi / #searchSiDoArea_multi 값으로 시도·지역을 정하고,
    단지만 hidden #cbxSearchDanji 를 읽는다. 상자 플러그인은 jq351(jQuery 3.5.1)에 붙어 있다.
    """
    상자 = 드라이버.execute_script("""
        var want = {'searchSiDo_multi': arguments[0], 'searchSiDoArea_multi': arguments[1],
                    'searchDanji_multi': arguments[2]};
        var out = {};
        var jq = window.jq351 || window.jQuery;
        Object.keys(want).forEach(function(id) {
            var sel = document.getElementById(id);
            if (!sel) { out[id] = '상자없음'; return; }
            var v = want[id];
            var has = false;
            for (var i = 0; i < sel.options.length; i++) {
                var o = sel.options[i];
                o.selected = (o.value === v);
                if (o.value === v) has = true;
            }
            if (!has) {
                var op = document.createElement('option');
                op.value = v; op.text = v; op.selected = true;
                sel.appendChild(op);
            }
            try {
                if (jq && jq(sel).multipleSelect) {
                    jq(sel).multipleSelect('refresh');
                    jq(sel).multipleSelect('setSelects', [v]);
                }
            } catch (e) { out[id + '_오류'] = String(e).substring(0, 80); }
            var now = jq ? jq(sel).val() : null;
            out[id] = (has ? '' : '(옵션추가)') + JSON.stringify(now);
        });
        return out;
    """, 검색_시도, 검색_지역, 검색_단지)
    print(f"  지역 상자: {상자}")

    결과 = 드라이버.execute_script("""
        var want = arguments[0];
        var out = {};
        ['hdfDefaultSido','hdfDefaultCity','hdfDefaultDanji'].forEach(function(n){
            var el = document.querySelector('input[name="'+n+'"]');
            if (el) el.value = want[n];
        });
        delete want.hdfDefaultSido; delete want.hdfDefaultCity; delete want.hdfDefaultDanji;
        Object.keys(want).forEach(function(name) {
            var els = document.querySelectorAll('input[name="'+name+'"]');
            if (els.length === 0) {
                var f = document.querySelector('form');
                if (f) {
                    var el = document.createElement('input');
                    el.type = 'hidden'; el.name = name; f.appendChild(el);
                    els = [el];
                }
            }
            els.forEach(function(el){ el.value = want[name]; });
            out[name] = els.length;
        });
        return out;
    """, {
        "cbxSearchSiDo": 검색_시도,
        "cbxSearchSiDoArea": 검색_지역,
        "cbxSearchDanji": 검색_단지,
        "hdfDefaultSido": 검색_시도,
        "hdfDefaultCity": 검색_지역,
        "hdfDefaultDanji": 검색_단지,
    })
    print(f"  지역 고정: 시도={검색_시도} 지역={검색_지역} 단지={검색_단지} (칸 {결과})")


def _검색_초기화(드라이버, 아이디, 비밀번호):
    """검색 필터 초기화 + 페이지당 50건 설정"""
    print("  검색 조건 초기화 중...")
    print(f"  현재 URL: {드라이버.current_url}")

    페이지_정보 = 드라이버.execute_script("""
        return {
            url: location.href,
            bodyLen: document.body ? document.body.innerText.length : 0,
            formCount: document.querySelectorAll('form').length,
            selectCount: document.querySelectorAll('select').length,
            tableCount: document.querySelectorAll('table').length,
            inputCount: document.querySelectorAll('input').length,
            title: document.title,
            readyState: document.readyState
        };
    """)
    print(f"  [페이지] 제목={페이지_정보.get('title','?')}, 본문={페이지_정보.get('bodyLen',0)}자, "
          f"form={페이지_정보.get('formCount',0)}, select={페이지_정보.get('selectCount',0)}, "
          f"input={페이지_정보.get('inputCount',0)}, table={페이지_정보.get('tableCount',0)}")

    if 페이지_정보.get('selectCount', 0) == 0:
        print("  select 없음 → 10초 추가 대기...")
        time.sleep(10)
        재확인 = 드라이버.execute_script("""
            return {
                selectCount: document.querySelectorAll('select').length,
                bodyLen: document.body ? document.body.innerText.length : 0
            };
        """)
        print(f"  재확인: select={재확인.get('selectCount',0)}, 본문={재확인.get('bodyLen',0)}자")

        if 재확인.get('selectCount', 0) == 0:
            print("  여전히 select 없음 → 페이지 새로고침 시도...")
            드라이버.refresh()
            time.sleep(8)
            for _ in range(3):
                alert_처리(드라이버)
                time.sleep(1)
            새로고침_확인 = 드라이버.execute_script("return document.querySelectorAll('select').length;")
            print(f"  새로고침 후: select={새로고침_확인}개")

    상태 = 드라이버.execute_script("""
        var r = {};
        r.PTC = (typeof PageTCount !== 'undefined') ? PageTCount : 'N/A';
        r.PR = (typeof PageRow !== 'undefined') ? PageRow : 'N/A';
        var h = {};
        document.querySelectorAll('input[type="hidden"]').forEach(function(i) {
            if (i.name && i.value) h[i.name] = i.value;
        });
        r.h = h;
        var t = {};
        document.querySelectorAll('input[type="text"]').forEach(function(i) {
            if (i.value) t[i.name || i.id || '?'] = i.value;
        });
        r.t = t;
        return r;
    """)
    print(f"  [전] PageTCount={상태.get('PTC')}, PageRow={상태.get('PR')}")
    if 상태.get('h'):
        print(f"  [전] hidden: {상태['h']}")
    if 상태.get('t'):
        print(f"  [전] text: {상태['t']}")

    셀렉트_정보 = 드라이버.execute_script("""
        var info = [];
        document.querySelectorAll('select').forEach(function(s){
            if (!s.name) return;
            var opts = [];
            for (var i=0; i<Math.min(s.options.length, 5); i++) {
                opts.push(i + ':' + s.options[i].value + '=' + s.options[i].text.trim());
            }
            info.push(s.name + ' [' + s.selectedIndex + '] → ' + opts.join(' | '));
        });
        return info;
    """)
    for s in (셀렉트_정보 or []):
        print(f"  [select] {s}")

    드라이버.execute_script("""
        document.querySelectorAll('input[type="text"]').forEach(function(i){ i.value=''; });

        document.querySelectorAll('select').forEach(function(s){
            if (!s.name || s.name==='sbxPageRowCount') return;
            var found = false;
            for (var i=0; i<s.options.length; i++) {
                var t = s.options[i].text.trim();
                var v = s.options[i].value;
                if (t==='전체' || t==='전체선택' || t==='-- 전체 --' || t==='선택' || v==='' || v==='0' || v==='-1') {
                    s.selectedIndex = i;
                    found = true;
                    break;
                }
            }
            if (!found) s.selectedIndex = 0;
            s.dispatchEvent(new Event('change', {bubbles:true}));
        });

        var rs = document.querySelector('select[name="sbxPageRowCount"]');
        if (rs) {
            for (var i=0; i<rs.options.length; i++) {
                if (rs.options[i].value === '50') { rs.selectedIndex = i; break; }
            }
            rs.dispatchEvent(new Event('change', {bubbles:true}));
        }

        var pr = document.querySelector('input[name="PageRow"]');
        if (pr) pr.value = '50';
    """)
    time.sleep(1)
    _지역_고정(드라이버)

    try:
        method = 드라이버.execute_script("""
            if (typeof goFormSubmitType === 'function') { goFormSubmitType(); return 'goFormSubmitType'; }
            if (typeof goSearchSubmit === 'function') { goSearchSubmit(); return 'goSearchSubmit'; }
            if (typeof goPageSubmit === 'function') { goPageSubmit(1); return 'goPageSubmit(1)'; }
            var btn = document.querySelector('.btn_search, [onclick*="goForm"], [onclick*="Submit"], button[type="submit"]');
            if (btn) { btn.click(); return 'button_click'; }
            var form = document.querySelector('form');
            if (form) { form.submit(); return 'form.submit'; }
            return 'none';
        """)
        print(f"  검색 방식: {method}")
    except Exception:
        pass
    time.sleep(5)
    alert_처리(드라이버)

    상태2 = 드라이버.execute_script("""
        var r = {};
        r.PTC = (typeof PageTCount !== 'undefined') ? PageTCount : 'N/A';
        r.PR = (typeof PageRow !== 'undefined') ? PageRow : 'N/A';
        r.rows = document.querySelectorAll('tr.uc_carcss').length;
        r.url = location.href;
        var jq = window.jq351 || window.jQuery;
        r.multi = {};
        ['searchSiDo_multi','searchSiDoArea_multi','searchDanji_multi'].forEach(function(id){
            var el = document.getElementById(id);
            r.multi[id] = el && jq ? JSON.stringify(jq(el).val()) : '없음';
        });
        var areas = [];
        document.querySelectorAll('tr.uc_carcss .uc_cararea').forEach(function(td){
            var t = td.innerText.split('\\n')[0].trim();
            if (areas.indexOf(t) < 0) areas.push(t);
        });
        r.areas = areas.slice(0, 5);
        var h = {};
        document.querySelectorAll('input[type="hidden"]').forEach(function(i){
            if (i.name && i.value) h[i.name] = i.value;
        });
        r.h = h;
        return r;
    """)
    rows = 상태2.get('rows', 0)
    print(f"  [후] PageTCount={상태2.get('PTC')}, PageRow={상태2.get('PR')}, 행수={rows}")
    if 상태2.get('h'):
        print(f"  [후] hidden: {상태2['h']}")
    print(f"  [후] 지역 상자: {상태2.get('multi')} | 목록 단지: {상태2.get('areas')}")

    if rows == 0:
        print("  0건 → 쿠키 삭제 후 재로그인...")
        드라이버.delete_all_cookies()

        if not 로그인(드라이버, 아이디, 비밀번호):
            print("  재로그인 실패!")
            return

        드라이버.get(f"{사이트}/Car/Data")
        time.sleep(5)
        for _ in range(3):
            alert_처리(드라이버)
            time.sleep(1)

        드라이버.execute_script("""
            document.querySelectorAll('input[type="text"]').forEach(function(i){ i.value=''; });
            document.querySelectorAll('select').forEach(function(s){
                if (!s.name || s.name==='sbxPageRowCount') return;
                s.selectedIndex = 0;
                s.dispatchEvent(new Event('change', {bubbles:true}));
            });
            var rs = document.querySelector('select[name="sbxPageRowCount"]');
            if (rs) {
                for (var i=0; i<rs.options.length; i++) {
                    if (rs.options[i].value === '50') { rs.selectedIndex = i; break; }
                }
                rs.dispatchEvent(new Event('change', {bubbles:true}));
            }
            var pr = document.querySelector('input[name="PageRow"]');
            if (pr) pr.value = '50';
        """)
        time.sleep(1)
        _지역_고정(드라이버)
        try:
            드라이버.execute_script("""
                if (typeof goFormSubmitType === 'function') goFormSubmitType();
                else if (typeof goPageSubmit === 'function') goPageSubmit(1);
            """)
        except Exception:
            pass
        time.sleep(5)
        alert_처리(드라이버)

        상태3 = 드라이버.execute_script("""
            return {
                PTC: (typeof PageTCount !== 'undefined') ? PageTCount : 'N/A',
                PR: (typeof PageRow !== 'undefined') ? PageRow : 'N/A',
                rows: document.querySelectorAll('tr.uc_carcss').length
            };
        """)
        rows = 상태3.get('rows', 0)
        print(f"  [재시도] PageTCount={상태3.get('PTC')}, PageRow={상태3.get('PR')}, 행수={rows}")

    print(f"  검색 초기화 완료 (현재 {rows}건 표시)")


# ════════════════════════════════════════════════════════════
# 이용제한 방지: 하루 상한 · 실행 간격 · 제한 화면 감지
# ════════════════════════════════════════════════════════════
class _이용제한감지(BaseException):
    """제한 화면을 보면 던짐. Exception 이 아니라서 중간의 except Exception 에 안 잡히고 끝까지 올라감."""


def _지금글():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _오늘():
    return datetime.now().strftime("%Y-%m-%d")


def 하루사용_읽기():
    db = DB_연결()
    try:
        r = db.execute("SELECT 상세, 목록페이지, 로그인 FROM 하루사용 WHERE 날짜=?", (_오늘(),)).fetchone()
    finally:
        db.close()
    return {"상세": r["상세"] if r else 0, "목록페이지": r["목록페이지"] if r else 0, "로그인": r["로그인"] if r else 0}


def 하루사용_더하기(항목, n=1):
    if 항목 not in ("상세", "목록페이지", "로그인"):
        return
    db = DB_연결()
    try:
        db.execute("INSERT OR IGNORE INTO 하루사용 (날짜) VALUES (?)", (_오늘(),))
        db.execute(f"UPDATE 하루사용 SET {항목} = {항목} + ? WHERE 날짜=?", (n, _오늘()))
        db.commit()
    finally:
        db.close()


def _실행상태_읽기():
    try:
        with open(실행상태_파일, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _다음실행_정하기():
    분 = random.uniform(*실행_간격_분)
    다음 = datetime.now() + timedelta(minutes=분)
    try:
        상태 = _실행상태_읽기()
        상태["다음실행"] = 다음.strftime("%Y-%m-%d %H:%M:%S")
        상태["마지막실행끝"] = _지금글()
        with open(실행상태_파일, "w", encoding="utf-8") as f:
            json.dump(상태, f, ensure_ascii=False)
        print(f"  다음 실행: {다음.strftime('%H:%M')} 이후 ({분:.0f}분 쉼)")
    except Exception as e:
        print(f"  다음 실행 시각 저장 실패: {e}")


def _제한_확인(글, 어디):
    """페이지·응답에 이용제한 문구가 있으면 중지.txt 를 만들고 _이용제한감지 를 던진다."""
    if not 글:
        return
    걸린 = [k for k in 제한_문구 if k in 글]
    if not 걸린:
        return
    # 「보안 정책에 위배」는 제한 화면에만 있는 문구 → 어디서든 멈춤.
    # 「이용제한」「사용이 제한」은 차 광고 글에도 나올 수 있어서 짧은 화면(제한 화면은 404자였음)일 때만.
    if "보안 정책에 위배" not in 글 and len(글) >= 5000:
        return
    i = 글.find(걸린[0])
    조각 = " ".join(re.sub(r"<[^>]+>", " ", 글[max(0, i - 150): i + 400]).split())
    print("\n  " + "!" * 60)
    print(f"  [이용제한 감지] {어디}")
    print(f"  화면 내용: {조각}")
    print("  → 지금 바로 모두 멈춥니다. 휴대폰으로 온 주소로 이용제한을 푼 뒤")
    print(f"    {중지_파일} 파일을 지우면 다시 돌아갑니다.")
    print("  " + "!" * 60)
    try:
        with open(중지_파일, "w", encoding="utf-8") as f:
            f.write(f"{_지금글()} 이용제한 감지 ({어디})\n{조각}\n\n"
                    "이용제한을 푼 뒤 이 파일을 지우면 다시 돌아갑니다.\n")
    except Exception:
        pass
    raise _이용제한감지(어디)


def _지역키():
    return f"{검색_시도}/{검색_지역}/{검색_단지}"


def _목록진행_읽기(키=None):
    """지역 목록 진행 상태. 처음이면 지금 DB 에 이미 받아 둔 차 수로 어디까지 했는지 추정해서 만든다."""
    키 = 키 or _지역키()
    db = DB_연결()
    try:
        r = db.execute("SELECT * FROM 목록진행 WHERE 지역키=?", (키,)).fetchone()
        if r:
            return dict(r)
        # 처음: 새 형식(CarDetailEnc) 상세링크로 받아 둔 차 = 이 지역 목록에서 받은 차로 봄 (11-23 2,950대)
        n, 최초 = db.execute(
            "SELECT COUNT(*), MIN(최근확인) FROM 차량 WHERE 상세링크 LIKE '%CarDetailEnc%' AND 검색지역 IS NULL"
        ).fetchone()
        진행 = {"지역키": 키, "다음페이지": 1, "바퀴시작": None, "마지막완료": None, "전체건수": None}
        if n and n >= 50:
            db.execute("UPDATE 차량 SET 검색지역=? WHERE 상세링크 LIKE '%CarDetailEnc%' AND 검색지역 IS NULL", (키,))
            진행["다음페이지"] = n // 50 + 1
            진행["바퀴시작"] = 최초
            print(f"  목록 진행 기록 처음 만듦: 이미 받은 {n}대 → {진행['다음페이지']}페이지부터 이어서")
        db.execute("INSERT INTO 목록진행 (지역키, 다음페이지, 바퀴시작, 마지막완료, 전체건수) VALUES (?,?,?,?,?)",
                   (키, 진행["다음페이지"], 진행["바퀴시작"], 진행["마지막완료"], 진행["전체건수"]))
        db.commit()
        return 진행
    finally:
        db.close()


def _목록진행_쓰기(진행):
    db = DB_연결()
    try:
        db.execute("UPDATE 목록진행 SET 다음페이지=?, 바퀴시작=?, 마지막완료=?, 전체건수=? WHERE 지역키=?",
                   (진행.get("다음페이지"), 진행.get("바퀴시작"), 진행.get("마지막완료"),
                    진행.get("전체건수"), 진행["지역키"]))
        db.commit()
    finally:
        db.close()


def 목록_할_차례():
    """(할 차례인지, 이유). 한 바퀴 도는 중이면 이어서, 다 돌았으면 이틀 뒤에."""
    진행 = _목록진행_읽기()
    if 진행.get("바퀴시작"):
        return True, f"{진행.get('다음페이지')}페이지부터 이어서"
    마지막 = 진행.get("마지막완료")
    if not 마지막:
        return True, "처음"
    다음 = datetime.strptime(마지막, "%Y-%m-%d %H:%M:%S") + timedelta(hours=목록_바퀴_간격_시간)
    if datetime.now() >= 다음:
        return True, "새 바퀴"
    return False, f"마지막으로 끝까지 훑은 때 {마지막[:16]} → 다음 바퀴 {다음.strftime('%m-%d %H:%M')}"


def DB_상세_대상(최대):
    """상세(사진·내용·성능기록부)를 아직 안 받은 등록중 차. 최근에 목록에서 본 차부터."""
    db = DB_연결()
    try:
        rows = db.execute("""
            SELECT 차량번호, 차종, 상세링크, 원본데이터 FROM 차량
            WHERE 상태='등록중' AND 상세링크 LIKE '%CarDetailEnc%'
              AND (성능기록부 IS NULL OR 성능기록부 NOT LIKE ?)
            ORDER BY 최근확인 DESC LIMIT ?
        """, (f'%"detail_v": {상세_버전}%', max(1, 최대) * 3)).fetchall()
    finally:
        db.close()
    대상 = []
    for r in rows:
        try:
            차 = json.loads(r["원본데이터"] or "{}")
        except Exception:
            차 = {}
        차["차량번호"] = r["차량번호"]
        if not 차.get("encarno"):
            m = re.search(r"encarno=([^&]+)", r["상세링크"] or "")
            if m:
                차["encarno"] = unquote(m.group(1))
        차.setdefault("차종", r["차종"] or "?")
        차.setdefault("상세링크", r["상세링크"])
        if 차.get("encarno"):
            대상.append(차)
        if len(대상) >= 최대:
            break
    return 대상


def 지역_사라짐_처리(키, 바퀴시작):
    """한 바퀴를 끝까지 돌았을 때: 이 지역 차 중 이번 바퀴에서 한 번도 못 본 차 = 사라짐."""
    지금 = _지금글()
    db = DB_연결()
    try:
        ids = [r["차량번호"] for r in db.execute(
            "SELECT 차량번호 FROM 차량 WHERE 상태='등록중' AND 검색지역=? AND (최근확인 IS NULL OR 최근확인 < ?)",
            (키, 바퀴시작)).fetchall()]
        for i in ids:
            db.execute("UPDATE 차량 SET 상태='사라짐', 사라진날=? WHERE 차량번호=?", (지금, i))
        db.commit()
    finally:
        db.close()
    if supabase_client and ids:
        try:
            테이블 = supabase_client.table("car_listings")
            for k in range(0, len(ids), 100):
                테이블.update({"status": "removed", "removed_at": datetime.now().isoformat()}) \
                    .in_("car_id", ids[k:k + 100]).execute()
        except Exception as e:
            print(f"  Supabase 사라짐 반영 실패: {str(e)[:80]}")
    return len(ids)


def _실행_해도_되나():
    """(돌려도 되는지, 이유). 브라우저 열기 전에 판단 → 할 일 없으면 로그인도 안 함."""
    if os.path.exists(중지_파일):
        return False, f"중지.txt 있음 — 이용제한을 푼 뒤 {중지_파일} 를 지우면 다시 돌아요"
    if HEADLESS:  # 자동 실행일 때만 시간 규칙 (손으로 켠 실행은 바로 돎)
        시 = datetime.now().hour
        if 쉬는_시간[0] <= 시 < 쉬는_시간[1]:
            return False, f"밤 쉬는 시간 ({쉬는_시간[0]}~{쉬는_시간[1]}시)"
        다음 = _실행상태_읽기().get("다음실행")
        if 다음:
            try:
                if datetime.now() < datetime.strptime(다음, "%Y-%m-%d %H:%M:%S"):
                    return False, f"다음 실행 {다음[11:16]} 이후"
            except Exception:
                pass
    사용 = 하루사용_읽기()
    상세_가능 = 사용["상세"] < 하루_상세_대수 and bool(DB_상세_대상(1))
    목록_가능 = 목록_수집_켜짐 and 사용["목록페이지"] < 하루_목록_페이지 and 목록_할_차례()[0]
    if not 상세_가능 and not 목록_가능:
        목록이유 = 목록_할_차례()[1] if 목록_수집_켜짐 else "꺼 둠"
        return False, (f"오늘 할 일 없음 (상세 {사용['상세']}/{하루_상세_대수}대, "
                       f"목록 {사용['목록페이지']}/{하루_목록_페이지}페이지, 목록: {목록이유})")
    return True, ""


def _목록_요청(드라이버, 페이지):
    """사이트 검색 버튼과 같은 요청을 직접 보낸다.
    (sCarData.js reLoadJsonCarDataMulti → POST /Car/DataPart, JSON {"para": {...}})
    화면 상자·계정 기본 단지와 상관없이 설정한 지역으로 목록 HTML 을 받는다.
    받은 HTML 은 사이트처럼 #ui_context 에 넣어, 안의 스크립트(EncCheck·PrePage·건수 갱신)가 돌게 한다."""
    return 드라이버.execute_async_script("""
        var page = arguments[0], sido = arguments[1], area = arguments[2], danji = arguments[3];
        var done = arguments[arguments.length - 1];
        var val = function(id, d) { var el = document.getElementById(id); return (el && el.value) ? el.value.trim() : d; };
        var para = {
            "EncCheck": val('EncCheck', ''), "PrePage": val('PrePage', '1'),
            "PageNow": page, "PageSize": "50",
            // 정렬 고정: 1=광고등록순, 0=오름차순(오래된 것부터). 새 매물은 뒤에 붙어서
            // 여러 실행에 나눠 훑어도 이미 본 페이지가 덜 밀림 (매물수정순은 수정할 때마다 앞으로 튐)
            "PageSort": "1", "PageAscDesc": "0",
            "CarMode": "0",
            "CarSiDoNo": sido, "CarSiDoAreaNo": area, "CarDanjiNo": danji,
            "CarMakerNo": null, "CarModelNo": null, "CarModelDetailNo": "", "CarGradeNo": "", "CarGradeDetailNo": "",
            "CarMakeSDate": "", "CarMakeEDate": "", "CarDriveSKm": null, "CarDriveEKm": null,
            "CarMission": "", "CarFuel": "", "CarColor": "", "CarSMoney": null, "CarEMoney": null,
            "CarIsLPG": "False", "CarIsSago": "False", "CarIsPhoto": "False", "CarIsSaleAmount": "False",
            "CarIsCarCheck": "False", "CarIsLeaseCheck": "False",
            "CarName": "", "CarDealerName": "", "CarShopName": "", "CarDealerHP": "", "CarNumber": "", "CarOption": "",
            "CarTruckTonS": "", "CarTruckTonE": ""
        };
        fetch('/Car/DataPart', {
            method: 'POST', credentials: 'include', cache: 'no-store',
            headers: {'Content-Type': 'application/json; charset=utf-8',
                      'X-Requested-With': 'XMLHttpRequest', 'Accept': 'text/html, */*; q=0.01'},
            body: JSON.stringify({para: para})
        }).then(function(r) {
            return r.text().then(function(t) {
                var out = {status: r.status, html: t, sentEnc: para.EncCheck, sentPre: para.PrePage};
                try {
                    var jq = window.jQuery || window.jq351;
                    var box = document.getElementById('ui_context');
                    if (jq && box) { jq(box).html(t); } else { out.applyError = 'ui_context 없음'; }
                } catch (e) { out.applyError = String(e).substring(0, 120); }
                out.total = val('hdfCarRowCount', '');
                out.enc = val('EncCheck', '');
                out.pre = val('PrePage', '');
                done(out);
            });
        }).catch(function(e) { done({error: String(e)}); });
    """, 페이지, 검색_시도, 검색_지역, 검색_단지) or {}


def _목록_페이지_열기(드라이버):
    드라이버.get(f"{사이트}/Car/Data")
    for _ in range(3):
        time.sleep(2)
        msg = alert_처리(드라이버)
        if msg:
            print(f"  알림: {msg}")
            _제한_확인(msg, "매물조회 페이지 알림")
    time.sleep(random.uniform(3, 6))
    _제한_확인(드라이버.page_source, "매물조회 페이지")
    try:
        드라이버.set_script_timeout(60)
    except Exception:
        pass


def 목록_이어서(드라이버, 아이디, 비밀번호, 최대페이지):
    """지역 목록을 지난번 멈춘 페이지부터 최대페이지 만큼만 훑는다 (한 페이지 겹쳐서 밀림 대비).
    끝 페이지까지 다 돌면 그 바퀴에서 못 본 차를 사라짐 처리하고, 다음 바퀴는 목록_바퀴_간격_시간 뒤."""
    global 목록_완료, 목록_신규
    목록_완료 = False
    목록_신규 = 0
    전체 = []
    본_번호 = set()
    키 = _지역키()

    진행 = _목록진행_읽기(키)
    if not 진행.get("바퀴시작"):
        진행.update({"다음페이지": 1, "바퀴시작": _지금글()})
        _목록진행_쓰기(진행)
        print("  목록: 새 바퀴 시작 (1페이지부터)")
    시작 = max(1, int(진행.get("다음페이지") or 1) - 1)

    if "/Car/Data" not in (드라이버.current_url or ""):
        _목록_페이지_열기(드라이버)
    print(f"  목록: 지역 {키} | {시작}페이지부터 최대 {최대페이지}페이지 "
          f"(정렬: 광고등록 오래된 순, 대기 {대기_범위[0]}~{대기_범위[1]}초)")

    총_페이지 = None
    p = 시작
    받은 = 0
    while 받은 < 최대페이지:
        if 총_페이지 is not None and p > 총_페이지:
            break
        if 받은 > 0:
            time.sleep(random.uniform(*대기_범위))

        print(f"  [{p:>4}/{총_페이지 or '?'}] ", end="", flush=True)
        try:
            응답 = _목록_요청(드라이버, p)
        except Exception as e:
            alert_처리(드라이버)
            print(f"요청 실패 ({str(e)[:120]})")
            break
        하루사용_더하기("목록페이지")
        time.sleep(random.uniform(3, 6))
        msg = alert_처리(드라이버)
        if msg:
            _제한_확인(msg, f"목록 {p}페이지 알림")

        html = 응답.get("html", "") or ""
        _제한_확인(html, f"목록 {p}페이지")
        if 응답.get("error") or 응답.get("status") != 200:
            print(f"요청 실패: 상태={응답.get('status')} 오류={응답.get('error')}")
            print(f"  응답 앞부분: {' '.join(html.split())[:800]}")
            break

        건수 = (re.search(r"hdfCarRowCount[\"']?\)\.val\(\s*['\"](\d+)", html)
              or re.search(r"ui_totalcount[\"']?\)\.html\(\s*remakeNumber\(\s*['\"](\d+)", html)
              or re.search(r"reLoadPage\(\s*['\"](\d+)['\"]", html))
        새_enc = re.search(r"EncCheck[\"']?\)\.val\(\s*['\"]([^'\"]+)", html)
        if 새_enc and 새_enc.group(1) != 응답.get("enc"):
            드라이버.execute_script(
                "var e=document.getElementById('EncCheck'); if(e) e.value=arguments[0];", 새_enc.group(1))
        if 건수:
            진행["전체건수"] = int(건수.group(1))
            총_페이지 = max(1, -(-진행["전체건수"] // 50))

        차량들 = HTML에서_차량_추출(html)
        if 받은 == 0:
            단지들 = sorted({(차.get("지역") or "")[:25] for 차 in 차량들})[:5]
            print(f"\n  [첫 응답] 상태={응답.get('status')} 길이={len(html)} 전체건수={진행.get('전체건수')} "
                  f"총페이지={총_페이지} 행={len(차량들)} 단지={단지들}")
            if not 차량들:
                print(f"  [첫 응답] 목록 없음 — 응답 앞부분: {' '.join(html.split())[:1500]}")
            print(f"  [{p:>4}/{총_페이지 or '?'}] ", end="", flush=True)

        if not 차량들:
            print("데이터 없음")
            if p > 1:
                목록_완료 = True  # 끝 페이지를 넘음
            break

        새것 = [차 for 차 in 차량들 if 차.get("차량번호") not in 본_번호]
        for 차 in 새것:
            본_번호.add(차.get("차량번호"))
        전체.extend(새것)

        # 페이지마다 바로 저장 + 이 지역 표시 + 진행 기록
        저장메모 = ""
        try:
            이번신규 = 차량_저장(새것)
            목록_신규 += 이번신규
            db = DB_연결()
            db.executemany("UPDATE 차량 SET 검색지역=? WHERE 차량번호=?",
                           [(키, 차["차량번호"]) for 차 in 새것 if 차.get("차량번호")])
            db.commit()
            db.close()
            Supabase_차량_저장(새것)
            if 이번신규:
                저장메모 = f" (신규 {이번신규})"
        except Exception as e:
            저장메모 = f" (저장 오류: {str(e)[:60]})"
        진행["다음페이지"] = p + 1
        _목록진행_쓰기(진행)
        print(f"{len(새것):>3}대{저장메모}")

        if len(차량들) < 50:
            목록_완료 = True
            break
        p += 1
        받은 += 1

    if 총_페이지 is not None and p > 총_페이지:
        목록_완료 = True

    if 목록_완료:
        사라진 = 지역_사라짐_처리(키, 진행["바퀴시작"])
        print(f"\n  목록 한 바퀴 끝 (전체 {진행.get('전체건수')}대). 이번 바퀴에서 못 본 차 {사라진}대 → 사라짐")
        진행.update({"다음페이지": 1, "바퀴시작": None, "마지막완료": _지금글()})
        _목록진행_쓰기(진행)
        print(f"  다음 바퀴는 {목록_바퀴_간격_시간}시간 뒤")
    else:
        print(f"\n  목록: 이번엔 여기까지. 다음 실행에 {진행.get('다음페이지')}페이지부터 이어서"
              + (f" (전체 {총_페이지}페이지)" if 총_페이지 else ""))
    return 전체


def _총_페이지_수(드라이버):
    try:
        result = 드라이버.execute_script("""
            // 방법 1: 전역 변수
            if (typeof PageTCount !== 'undefined' && typeof PageRow !== 'undefined') {
                var total = parseInt(PageTCount);
                var row = parseInt(PageRow);
                if (total > 0 && row > 0) return Math.ceil(total / row);
            }
            // 방법 2: hidden input
            var tc = document.querySelector('input[name="PageTCount"]');
            var pr = document.querySelector('input[name="PageRow"]');
            if (tc && pr) {
                var t = parseInt(tc.value);
                var r = parseInt(pr.value);
                if (t > 0 && r > 0) return Math.ceil(t / r);
            }
            // 방법 3: hdfCarRowCount (총 차량 수)
            var hdf = document.querySelector('input[name="hdfCarRowCount"]');
            if (hdf) {
                var t = parseInt(hdf.value);
                var rowCount = 50;
                if (pr) { var rv = parseInt(pr.value); if (rv > 0) rowCount = rv; }
                if (t > 0) return Math.ceil(t / rowCount);
            }
            // 방법 4: 페이지 링크에서 최대값
            var max = 1;
            document.querySelectorAll('a').forEach(function(a) {
                var oc = a.getAttribute('onclick') || '';
                var m = oc.match(/goPageSubmit\\((\\d+)\\)/);
                if (m) {
                    var n = parseInt(m[1]);
                    if (n > max) max = n;
                }
                var href = a.getAttribute('href') || '';
                m = href.match(/[Pp]age[=:](\\d+)/);
                if (m) {
                    var n = parseInt(m[1]);
                    if (n > max) max = n;
                }
            });
            return max;
        """)
        return max(1, int(result or 1))
    except Exception:
        return 1


# ════════════════════════════════════════════════════════════
# 상세 사진 수집 (차량별 여러 장)
# ════════════════════════════════════════════════════════════
_기록부_구조_출력함 = False
상세_버전 = 2  # 상세 수집 방식이 바뀌면 올림 → 예전 방식으로 받은 차는 다시 받음


def _성능기록부_가져오기(주소, 상세주소=""):
    """상세 페이지 iframe 이 불러오는 성능점검기록부(예: autocafe.co.kr/ASSO/CarCheck_Form.asp?OnCarNo=...)를 직접 열어 내용 해석.
    로그인 없이 열리는 페이지라 requests 로 받는다."""
    global _기록부_구조_출력함
    머리 = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
        "Referer": 상세주소 or f"{사이트}/",
    }
    try:
        try:
            r = requests.get(주소, timeout=20, headers=머리)
        except requests.exceptions.SSLError:
            # autocafe 는 인증서 일부를 빠뜨려 파이썬이 거절함 → 사이트 자체 코드(protocacheck)처럼 http 로
            if not 주소.startswith("https://"):
                raise
            r = requests.get("http://" + 주소[len("https://"):], timeout=20, headers=머리)
        최종 = r.url or 주소

        # 엠파크 단지 차: autocafe → m-park.co.kr/popup/performance/<번호> 로 넘어감.
        # 이 페이지는 화면에서 그려지는 방식이라 요청만으로는 내용이 비어 있음 → 주소·번호만 저장,
        # 내용은 따로 기록부_채우기() 에서 브라우저로 그려서 받음 (카매니저 접속 없음)
        if "m-park.co.kr/popup/performance" in 최종:
            return {"url": 주소, "final_url": 최종, "provider": "m-park",
                    "checkNo": 최종.rstrip("/").rsplit("/", 1)[-1], "status": r.status_code,
                    "exists": None, "rendered": False}
        try:
            html = r.content.decode("utf-8")
        except UnicodeDecodeError:
            html = r.content.decode("cp949", errors="replace")
        soup = BeautifulSoup(html, "html.parser")
        for t in soup(["script", "style"]):
            t.decompose()
        글 = " ".join(soup.get_text(" ").split())
        없음 = ("내역이 없습니다" in 글) or len(글) < 40
        표 = []
        for tr in soup.select("tr"):
            칸 = [" ".join(td.get_text(" ").split()) for td in tr.find_all(["th", "td"], recursive=False)]
            칸 = [c for c in 칸 if c]
            if 칸 and 칸 not in 표[-1:]:
                표.append(칸)
        체크 = []
        for inp in soup.select("input[type=checkbox], input[type=radio]"):
            if inp.has_attr("checked"):
                체크.append({"name": inp.get("name", ""), "value": inp.get("value", "")})
        이미지 = [urljoin(주소, img.get("src")) for img in soup.select("img[src]")][:60]
        결과 = {"url": 주소, "status": r.status_code, "exists": not 없음,
                "text": 글[:6000], "rows": 표[:250], "checked": 체크[:300], "images": 이미지}
        if not 없음 and not _기록부_구조_출력함:
            _기록부_구조_출력함 = True
            print(f"\n  [기록부 구조] {주소} 상태={r.status_code} 글자={len(글)} 표줄={len(표)} 체크={len(체크)} 이미지={len(이미지)}")
            print(f"  [기록부 구조] 표 앞부분: {json.dumps(표[:25], ensure_ascii=False)[:2500]}")
            print(f"  [기록부 구조] 체크 예: {체크[:15]} | 이미지 예: {이미지[:5]}")
            print(f"  [기록부 구조] 본문 앞부분: {글[:1500]}")
        return 결과
    except Exception as e:
        return {"url": 주소, "error": str(e)[:200]}


def _상세링크(encarno):
    """카매니저 상세 링크 (로그인한 브라우저에서 열림). 사이트의 「최근 본 차량」과 같은 형식,
    차 번호의 + = / 기호는 주소용으로 바꿔 넣음 (안 바꾸면 + 가 공백으로 읽혀 안 열림)."""
    return f"{사이트}/PopupFrame/CarDetailEnc?encarno={quote(encarno or '', safe='')}"


_수파베이스_새칸_없음 = False  # performance_url / performance_summary 칸이 아직 없으면 True (SQL 실행 전)


def _상세_저장(번호, 성능, 사진들=None):
    """상세 결과를 로컬 DB + Supabase 에 저장. 기록부 링크(누구나 열림)·요약은 따로 칸에도."""
    global _수파베이스_새칸_없음
    성능JSON = json.dumps(성능, ensure_ascii=False)
    기록 = 성능.get("record") or {}
    링크 = None
    if 기록 and not 기록.get("error") and (기록.get("provider") or 기록.get("exists")):
        링크 = 기록.get("final_url") or 기록.get("url")
    요약 = 기록.get("summary") or {}
    if 기록.get("rendered") is not None or 기록.get("exists") is not None:
        요약 = dict(요약, 기록부=("있음" if 기록.get("exists") else ("확인 전" if 기록.get("exists") is None else "없음")))
    요약JSON = json.dumps(요약, ensure_ascii=False) if 요약 else None
    사진JSON = json.dumps(사진들, ensure_ascii=False) if 사진들 else None

    db = DB_연결()
    try:
        db.execute("UPDATE 차량 SET 사진URL들=COALESCE(?,사진URL들), 성능기록부=?, 기록부링크=COALESCE(?,기록부링크) "
                   "WHERE 차량번호=?", (사진JSON, 성능JSON, 링크, 번호))
        db.commit()
    finally:
        db.close()

    if not supabase_client:
        return
    업데이트 = {"performance_record": 성능JSON}
    if 사진JSON:
        업데이트["photo_urls"] = 사진JSON
    새칸 = {}
    if 링크:
        새칸["performance_url"] = 링크
    if 요약JSON:
        새칸["performance_summary"] = 요약JSON
    테이블 = supabase_client.table("car_listings")
    if 새칸 and not _수파베이스_새칸_없음:
        try:
            테이블.update(dict(업데이트, **새칸)).eq("car_id", 번호).execute()
            return
        except Exception as e:
            if "performance_url" in str(e) or "performance_summary" in str(e) or "column" in str(e).lower():
                _수파베이스_새칸_없음 = True
                print("\n  (Supabase 에 기록부 링크·요약 칸이 아직 없어서 그 둘은 빼고 저장. SQL 실행하면 다음부터 들어감)")
    try:
        테이블.update(업데이트).eq("car_id", 번호).execute()
    except Exception:
        pass


def 상세링크_고치기():
    """예전 형식(…CarDetailEnc/?encarno=기호 그대로) 상세링크를 새 형식으로 (DB + Supabase). 사이트 접속 없음."""
    db = DB_연결()
    try:
        rows = db.execute("SELECT 차량번호, 상세링크 FROM 차량 WHERE 상세링크 LIKE '%CarDetailEnc/?encarno=%'").fetchall()
        고친 = []
        for r in rows:
            m = re.search(r"encarno=([^&]+)", r["상세링크"] or "")
            if not m:
                continue
            새 = _상세링크(unquote(m.group(1)))
            db.execute("UPDATE 차량 SET 상세링크=? WHERE 차량번호=?", (새, r["차량번호"]))
            고친.append((r["차량번호"], 새))
        db.commit()
    finally:
        db.close()
    if 고친:
        print(f"  상세링크 새 형식으로 고침: {len(고친)}대" + (" (Supabase 반영 중…)" if supabase_client else ""))
    if supabase_client:
        실패 = 0
        for 번호, 새 in 고친:
            try:
                supabase_client.table("car_listings").update({"detail_url": 새}).eq("car_id", 번호).execute()
            except Exception:
                실패 += 1
        if 실패:
            print(f"  Supabase 상세링크 반영 실패 {실패}대")
    return len(고친)


def _사진_원본주소(사진들):
    """상세 사진 주소 끝의 _TH(300×225 미리보기)를 빼서 원본(1024×768) 주소로. 같은 사진은 하나만."""
    결과, 본 = [], set()
    for u in 사진들 or []:
        원본 = re.sub(r"_TH(\.(?:jpe?g|png|webp|gif))(\?.*)?$", r"\1\2", u or "", flags=re.I).replace("None", "")
        if 원본 and 원본 not in 본:
            본.add(원본)
            결과.append(원본)
    return 결과


def 사진주소_원본으로_고치기():
    """이미 미리보기(_TH) 주소로 저장된 차의 사진 주소를 원본으로 (DB + Supabase). 사이트 접속 없음."""
    db = DB_연결()
    try:
        # 'TH.' 가 들어간 행만 후보로 (실제로 바뀌는 행만 아래에서 고침)
        rows = db.execute("SELECT 차량번호, 사진URL들 FROM 차량 WHERE 사진URL들 LIKE '%TH.%'").fetchall()
        고친 = []
        for r in rows:
            try:
                원래 = json.loads(r["사진URL들"] or "[]")
            except Exception:
                continue
            새것 = _사진_원본주소(원래)
            if 새것 != 원래:
                db.execute("UPDATE 차량 SET 사진URL들=? WHERE 차량번호=?", (json.dumps(새것, ensure_ascii=False), r["차량번호"]))
                고친.append((r["차량번호"], 새것))
        db.commit()
    finally:
        db.close()
    if supabase_client:
        for 번호, 새것 in 고친:
            try:
                supabase_client.table("car_listings").update(
                    {"photo_urls": json.dumps(새것, ensure_ascii=False)}).eq("car_id", 번호).execute()
            except Exception:
                pass
    if 고친:
        print(f"  사진 주소 원본으로 고침: {len(고친)}대")
    return len(고친)


def _엠파크_기록부_그리기(드라이버, 기록):
    """엠파크 성능기록부(m-park.co.kr/popup/performance/<번호>)를 브라우저로 그려서 내용 읽기.
    체크된 칸은 ■, 안 된 칸은 □ 로 글에 표시 (예: 「7.변속기 종류 ■자동 □수동」). 카매니저 접속 없음."""
    주소 = 기록.get("final_url") or 기록.get("url")
    try:
        드라이버.get(주소)
        for _ in range(20):
            time.sleep(1)
            n = len((드라이버.execute_script("return document.body ? document.body.innerText : ''") or "").strip())
            if n > 300:
                break
        r = 드라이버.execute_script("""
            var checked = [];
            document.querySelectorAll('input[type=checkbox],input[type=radio]').forEach(function(x){
                var lab = x.id ? document.querySelector('label[for="' + x.id + '"]') : null;
                if (!lab) return;
                var t = (lab.textContent || '').trim();
                if (x.checked) checked.push(t);
                lab.textContent = (x.checked ? '■' : '□') + t;
            });
            var text = (document.body ? document.body.innerText : '')
                .replace(/[ \\t]+/g, ' ').replace(/\\n\\s*\\n+/g, '\\n').trim();
            return {text: text, checked: checked};
        """) or {}
        글 = r.get("text") or ""
        요약 = {}
        for 키, 식 in [("등록번호", r"자동차등록번호\s*\n\s*(\S+)"), ("차대번호", r"차대번호\s*\n\s*([A-Z0-9]{11,17})"),
                      ("최초등록일", r"최초등록일\s*\n\s*(\d{8})"), ("주행거리", r"현재 주행거리\s*:\s*\[([\d,]+)\s*km\]"),
                      ("사고이력", r"사고이력[^\n]*\n((?:[■□][^\n]*\n?){2})"), ("단순수리", r"단순수리\s*\n((?:[■□][^\n]*\n?){2})")]:
            m = re.search(식, 글)
            if m:
                요약[키] = " ".join(m.group(1).split())
        기록.update({"rendered": True, "exists": ("발행번호" in 글 and len(글) > 300),
                   "text": 글[:12000], "checked": (r.get("checked") or [])[:400], "summary": 요약})
    except Exception as e:
        기록.update({"rendered": False, "render_error": str(e)[:200]})
    return 기록


def 기록부_채우기(드라이버, 최대=200):
    """상세는 받았는데 성능기록부 내용이 빠진 차를 채운다 (엠파크 그리기·예전 인증서 실패분).
    기록부 사이트(autocafe·엠파크)만 열고 카매니저에는 접속하지 않는다."""
    db = DB_연결()
    try:
        rows = db.execute("""
            SELECT 차량번호, 성능기록부 FROM 차량
            WHERE 성능기록부 IS NOT NULL
              AND (성능기록부 NOT LIKE ? OR (성능기록부 LIKE '%"provider": "m-park"%' AND 성능기록부 LIKE '%"rendered": false%'))
        """, (f'%"detail_v": {상세_버전}%',)).fetchall()
    finally:
        db.close()
    대상 = []
    for r in rows:
        try:
            p = json.loads(r["성능기록부"])
        except Exception:
            continue
        if not (p.get("info") or p.get("links")):
            continue  # 상세 자체를 정상으로 못 받은 차는 상세부터 다시
        기록 = p.get("record") or {}
        주소들 = [u for u in (p.get("frames") or [])
                if u and u.startswith("http") and re.search(r"CarCheck|Check_Form|[Pp]erform|[Ii]nspect", u)]
        주소 = 기록.get("url") or (주소들[0] if 주소들 else None)
        대상.append((r["차량번호"], p, 기록, 주소))
        if len(대상) >= 최대:
            break
    if not 대상:
        print("\n  기록부 채우기: 할 차 없음")
        return 0
    print(f"\n  기록부 채우기: {len(대상)}대 (카매니저 접속 없음)")
    채움 = 0
    for k, (번호, p, 기록, 주소) in enumerate(대상):
        if k > 0:
            time.sleep(random.uniform(2, 4))
        if 주소 and (not 기록 or 기록.get("error") or not 기록.get("provider")):
            기록 = _성능기록부_가져오기(주소)
        elif not 주소:
            기록 = {}
        if 기록.get("provider") == "m-park" and not 기록.get("rendered"):
            기록 = _엠파크_기록부_그리기(드라이버, 기록)
        표시 = ("실패 " + 기록.get("error", 기록.get("render_error", ""))[:60]) if (기록.get("error") or 기록.get("render_error")) \
            else ("없음(기록부 링크 없음)" if not 기록 else ("O " + json.dumps(기록.get("summary", {}), ensure_ascii=False)[:120]
                                               if 기록.get("exists") else "내역 없음"))
        print(f"  [{k+1:>4}/{len(대상)}] {번호[:12]} 기록부 {표시}")
        if 기록.get("error") or 기록.get("render_error"):
            continue
        if 기록:
            p["record"] = 기록
        p["detail_v"] = 상세_버전
        p["record_filled_at"] = _지금글()
        _상세_저장(번호, p)
        채움 += 1
    print(f"  기록부 채우기 끝: {채움}/{len(대상)}대")
    return 채움


def 상세_사진_수집(드라이버, 차량_목록, 최대=None):
    """각 차량 상세 페이지에서 사진 전체·내용·성능기록부 수집 (최대 대수만큼)"""
    최대 = 한번_상세_대수 if 최대 is None else 최대

    # 이미 상세를 끝낸 차량 제외
    # 기준 = 지금 방식으로 상세(사진+내용+성능기록부)를 받아 「완료표시」가 저장된 차.
    # 사진만 있는 옛 기록은 성능기록부가 없으므로 다시 받고, 사진이 원래 없는 차는 한 번 받으면 끝.
    수집완료 = set()
    db = DB_연결()
    try:
        rows = db.execute(
            "SELECT 차량번호 FROM 차량 WHERE 성능기록부 LIKE ?", (f'%"detail_v": {상세_버전}%',)
        ).fetchall()
        수집완료 = {r["차량번호"] for r in rows}
    except Exception:
        pass
    db.close()

    대상 = [차 for 차 in 차량_목록 if 차.get("encarno") and 차.get("차량번호") not in 수집완료]

    if not 대상 or 최대 <= 0:
        print("\n  상세: 할 차 없음 (건너뜀)")
        return

    # 상세 요청에는 목록 페이지의 EncCheck 값이 필요하다 → 목록 페이지에 있어야 함
    try:
        드라이버.set_script_timeout(40)
    except Exception:
        pass
    if "/Car/Data" not in (드라이버.current_url or ""):
        _목록_페이지_열기(드라이버)  # 이용제한 화면이면 여기서 멈춤

    배치 = 대상[:최대]
    print(f"\n  상세 수집: {len(배치)}대 (차 사이 {대기_범위[0]}~{대기_범위[1]}초)")

    수집_성공 = 0
    총_장수 = 0
    연속_이상 = 0

    for i, 차 in enumerate(배치):
        링크 = 차.get("상세링크", "")
        번호 = 차.get("차량번호", "")
        차종 = 차.get("차종", "?")
        if len(차종) > 20:
            차종 = 차종[:20]

        # 세션 갱신: N대마다 쿠키 날리고 재로그인 (봇 감지 방지)
        if i > 0 and 세션_갱신_상세 > 0 and i % 세션_갱신_상세 == 0:
            갱신대기 = random.uniform(15, 30)
            print(f"\n  [세션 갱신] 쿠키 초기화 + 재로그인 ({갱신대기:.0f}초 대기 후)...")
            time.sleep(갱신대기)
            드라이버.delete_all_cookies()
            time.sleep(2)
            아이디 = os.environ.get("CM_ID", "").strip()
            비밀번호 = os.environ.get("CM_PW", "").strip()
            if 아이디 and 비밀번호:
                if not 로그인(드라이버, 아이디, 비밀번호):
                    print("  재로그인 실패! 상세 수집 중단...")
                    break
                # 새 세션의 EncCheck 를 받으려고 목록 페이지를 다시 연다
                드라이버.get(f"{사이트}/Car/Data")
                time.sleep(5)
                for _ in range(3):
                    alert_처리(드라이버)
                    time.sleep(1)
            else:
                print("  .env에 계정 없음 — 상세 수집 중단...")
                break

        if i > 0:
            time.sleep(random.uniform(*대기_범위))
        print(f"  [{i+1:>4}/{len(배치)}] {차종:20s} ", end="", flush=True)

        try:
            # 상세 = /PopupFrame/CarDetailEnc/ 에 encarno + EncCheck POST (사이트 default.js 와 같은 방식)
            # 팝업 창을 띄우지 않고 목록 페이지 안에서 요청만 보내 HTML 을 받아 해석한다.
            상세 = 드라이버.execute_async_script("""
                var encarno = arguments[0], dump = arguments[1], done = arguments[arguments.length - 1];
                var encEl = document.getElementById('EncCheck') || document.querySelector('input[name="EncCheck"]');
                var body = 'encarno=' + encodeURIComponent(encarno) +
                           '&EncCheck=' + encodeURIComponent(encEl ? encEl.value.trim() : '');
                fetch('/PopupFrame/CarDetailEnc/', {
                    method: 'POST', credentials: 'include',
                    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                    body: body
                }).then(function(r){ return r.text().then(function(t){ return {status: r.status, url: r.url, html: t}; }); })
                .then(function(res) {
                    var doc = new DOMParser().parseFromString(res.html, 'text/html');
                    var abs = function(u){ try { return new URL(u, location.origin + '/PopupFrame/CarDetailEnc/').href; } catch(e){ return u; } };
                    var photos = [], seen = {};
                    var add = function(u) {
                        if (!u) return;
                        u = abs(u);
                        if (u.indexOf('noimage') !== -1 || seen[u]) return;
                        if (!/\\.(jpe?g|png|webp|gif)(\\?|$)/i.test(u)) return;
                        if (!/\\/(photo|Photo|Upload|CarPhoto)\\//.test(u) && u.indexOf('img.carmanager.co.kr') === -1) return;
                        if (/logo|icon|btn_|bg_|banner|footer/i.test(u)) return;
                        seen[u] = true; photos.push(u);
                    };
                    doc.querySelectorAll('img').forEach(function(img){
                        add(img.getAttribute('src')); add(img.getAttribute('data-src'));
                        add(img.getAttribute('data-original')); add(img.getAttribute('data-large'));
                    });
                    doc.querySelectorAll('a[href]').forEach(function(a){ add(a.getAttribute('href')); });
                    doc.querySelectorAll('[style*="url("]').forEach(function(el){
                        var m = (el.getAttribute('style') || '').match(/url\\(['"]?(.*?)['"]?\\)/);
                        if (m) add(m[1]);
                    });
                    // 스크립트 안에 사진 주소가 들어 있는 경우(슬라이드 등)
                    var raw = res.html.match(/https?:[^"'\\s<>]+?\\.(?:jpe?g|png|webp)/gi) || [];
                    raw.forEach(add);

                    // 성능기록부: 성능/점검/진단 링크·iframe·버튼 onclick
                    var perf = {links: [], info: {}, perfSection: '', frames: []};
                    doc.querySelectorAll('a, button, input[type="button"], area').forEach(function(el){
                        var text = (el.textContent || el.value || '').trim().replace(/\\s+/g, ' ');
                        var href = el.getAttribute('href') || '';
                        var onclick = el.getAttribute('onclick') || '';
                        var all = text + ' ' + href + ' ' + onclick;
                        if (/성능|점검|진단|기록부|보험이력|이력/.test(text) || /erform|nspect|heck|Record/i.test(href + onclick)) {
                            perf.links.push({text: text.substring(0, 50), href: href.substring(0, 300), onclick: onclick.substring(0, 300)});
                        }
                    });
                    doc.querySelectorAll('iframe, frame').forEach(function(f){ perf.frames.push(abs(f.getAttribute('src') || '')); });
                    doc.querySelectorAll('tr').forEach(function(tr){
                        var cells = tr.querySelectorAll('th, td');
                        for (var i = 0; i < cells.length - 1; i++) {
                            var k = cells[i].textContent.trim().replace(/\\s+/g, ' ');
                            var v = cells[i+1].textContent.trim().replace(/\\s+/g, ' ');
                            if (k && v && k.length < 30 && v.length < 300) perf.info[k] = v;
                        }
                    });
                    doc.querySelectorAll('dt').forEach(function(dt){
                        var dd = dt.nextElementSibling;
                        if (dd && dd.tagName === 'DD') {
                            var k = dt.textContent.trim(), v = dd.textContent.trim().replace(/\\s+/g, ' ');
                            if (k && v && k.length < 30) perf.info[k] = v.substring(0, 300);
                        }
                    });
                    var best = '';
                    doc.querySelectorAll('div, section, table').forEach(function(el){
                        var t = (el.textContent || '').replace(/\\s+/g, ' ').trim();
                        if (t.length > 20 && t.length < 3000 && /성능|점검|사고|침수|교환|판금|도색/.test(t)) {
                            if (!best || t.length < best.length) best = t;
                        }
                    });
                    perf.perfSection = best.substring(0, 2000);

                    var out = {status: res.status, len: res.html.length, photos: photos, perf: perf};
                    // 이용제한 확인용: 짧은 응답은 본문을, 긴 응답은 제한 문구가 있을 때 그 앞뒤만
                    var bodyText = (doc.body ? doc.body.textContent : res.html).replace(/\\s+/g, ' ').trim();
                    var pk = res.html.indexOf('보안 정책에 위배');
                    if (res.html.length < 5000) out.head = bodyText.substring(0, 600);
                    else if (pk >= 0) out.head = res.html.substring(Math.max(0, pk - 200), pk + 400);
                    if (dump) {
                        out.title = doc.title;
                        out.text = (doc.body ? doc.body.textContent : '').replace(/\\s+/g, ' ').substring(0, 1500);
                        out.scripts = [];
                        doc.querySelectorAll('script[src]').forEach(function(s){ out.scripts.push(s.getAttribute('src')); });
                        var fns = res.html.match(/function\\s+\\w*(?:[Pp]erform|[Cc]heck|[Ii]nspect|[Rr]ecord)\\w*\\s*\\([^)]*\\)\\s*\\{[\\s\\S]{0,400}/g) || [];
                        out.fns = fns.slice(0, 5);
                    }
                    done(out);
                }).catch(function(e){ done({error: String(e)}); });
            """, 차.get("encarno", ""), i == 0) or {}
            하루사용_더하기("상세")
            time.sleep(random.uniform(3, 6))  # 예전 화면 로딩 대기와 같은 간격 유지
            msg = alert_처리(드라이버)
            if msg:
                _제한_확인(msg, f"상세 {i+1}번째 알림")
            _제한_확인(상세.get("head") or "", f"상세 {i+1}번째 ({차종})")

            if 상세.get("error"):
                raise Exception(상세["error"])
            if i == 0:
                # 첫 차 1대는 상세 페이지 구조를 로그에 남긴다 (성능기록부 위치 확인용)
                print(f"\n  [상세 구조] 응답={상세.get('status')} 길이={상세.get('len')} 제목={상세.get('title')}")
                print(f"  [상세 구조] 사진 {len(상세.get('photos', []))}장 예: {상세.get('photos', [])[:3]}")
                print(f"  [상세 구조] 성능 링크: {json.dumps(상세.get('perf', {}).get('links', [])[:10], ensure_ascii=False)}")
                print(f"  [상세 구조] iframe: {상세.get('perf', {}).get('frames', [])}")
                print(f"  [상세 구조] 표 항목: {list(상세.get('perf', {}).get('info', {}).keys())[:40]}")
                print(f"  [상세 구조] 스크립트: {상세.get('scripts')}")
                print(f"  [상세 구조] 관련 함수: {json.dumps(상세.get('fns'), ensure_ascii=False)[:1500]}")
                print(f"  [상세 구조] 본문: {상세.get('text')}")
                print(f"  [{i+1:>4}/{len(배치)}] {차종:20s} ", end="", flush=True)

            사진들 = _사진_원본주소(상세.get("photos", []) or [])
            성능 = 상세.get("perf", {}) or {}

            # 성능점검기록부 본문: 상세 페이지 iframe 이 외부 기록부 페이지를 불러옴 → 그 주소를 직접 연다
            기록부주소 = [u for u in (성능.get("frames") or [])
                         if u and u.startswith("http") and re.search(r"CarCheck|Check_Form|[Pp]erform|[Ii]nspect", u)]
            if 기록부주소:
                time.sleep(random.uniform(1, 2))
                성능["record"] = _성능기록부_가져오기(기록부주소[0], 링크)

            차["사진URL들"] = 사진들
            차["성능기록부"] = 성능

            부분출력 = []
            if 사진들:
                부분출력.append(f"사진{len(사진들)}장")
            if 성능.get("links"):
                부분출력.append(f"성능링크{len(성능['links'])}")
            if 성능.get("info"):
                부분출력.append(f"상세{len(성능['info'])}항목")
            if 성능.get("perfSection"):
                부분출력.append("성능섹션O")
            기록 = 성능.get("record") or {}
            if 기록:
                if 기록.get("error"):
                    부분출력.append(f"기록부 실패({기록['error'][:60]})")
                elif 기록.get("provider") == "m-park":
                    부분출력.append(f"기록부 엠파크 {기록.get('checkNo')}(내용은 따로)")
                else:
                    부분출력.append("기록부O" if 기록.get("exists") else "기록부 없음")

            # 상세 응답을 정상으로 받았고 기록부 요청도 실패하지 않았으면 「완료표시」 → 다음 실행부터 건너뜀
            # (사진이 원래 없는 차도 여기서 끝. 실패한 차는 표시 안 해서 다음 실행에 다시 시도)
            정상응답 = 상세.get("status") == 200 and (상세.get("len") or 0) > 1000
            연속_이상 = 0 if 정상응답 else 연속_이상 + 1
            if 정상응답 and not 기록.get("error"):
                성능["detail_v"] = 상세_버전
                성능["fetched_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            if 정상응답:
                수집_성공 += 1
                총_장수 += len(사진들)
                print(" | ".join(부분출력) if 부분출력 else "내용 없음")
                _상세_저장(번호, 성능, 사진들)
            else:
                print(f"상세 응답 이상 (상태={상세.get('status')} 길이={상세.get('len')}) — 다음 실행에 다시")
                if 상세.get("head"):
                    print(f"    응답 내용: {상세['head'][:300]}")

        except Exception as e:
            차["사진URL들"] = []
            차["성능기록부"] = {}
            연속_이상 += 1
            print(f"실패 ({str(e)[:50]})")

        if 연속_이상 >= 3:
            print("  상세가 3번 연속 이상해서 이번 실행의 상세는 여기서 멈춤")
            break

    print(f"\n  상세 완료: {수집_성공}대에서 사진 총 {총_장수}장")


# ════════════════════════════════════════════════════════════
# 사진 다운로드
# ════════════════════════════════════════════════════════════
def 사진_다운로드(차량_목록):
    os.makedirs(사진_폴더, exist_ok=True)
    세션 = requests.Session()
    세션.headers["User-Agent"] = "Mozilla/5.0"
    다운 = 0
    실패 = 0

    for 차 in 차량_목록:
        안전_이름 = re.sub(r'[\\/:*?"<>|]', "_", 차.get("차량번호", "unknown"))

        # 상세 사진 여러 장 우선, 없으면 썸네일 1장
        urls = 차.get("사진URL들", [])
        if not urls:
            url = 차.get("사진URL", "")
            if url and url.startswith("http"):
                urls = [url]

        for idx, url in enumerate(urls):
            if not url or not url.startswith("http"):
                continue

            확장자 = ".jpg"
            if ".png" in url.lower():
                확장자 = ".png"
            elif ".webp" in url.lower():
                확장자 = ".webp"

            if len(urls) > 1:
                경로 = os.path.join(사진_폴더, f"{안전_이름}_{idx+1}{확장자}")
            else:
                경로 = os.path.join(사진_폴더, f"{안전_이름}{확장자}")

            if os.path.exists(경로):
                continue

            try:
                r = 세션.get(url, timeout=15)
                if r.status_code == 200 and len(r.content) > 500:
                    with open(경로, "wb") as f:
                        f.write(r.content)
                    다운 += 1
            except Exception:
                실패 += 1
            time.sleep(random.uniform(0.3, 0.8))  # 사진 서버에 한꺼번에 몰리지 않게

    return 다운, 실패


# ════════════════════════════════════════════════════════════
# CSV 보고서
# ════════════════════════════════════════════════════════════
def 보고서_생성():
    db = DB_연결()
    rows = db.execute("""
        SELECT * FROM 차량
        ORDER BY
            CASE 상태 WHEN '등록중' THEN 0 ELSE 1 END,
            최근확인 DESC
    """).fetchall()

    if not rows:
        db.close()
        return

    컬럼 = [
        "차량번호", "차종", "가격", "연식", "주행거리", "연료", "변속기",
        "색상", "사고여부", "번호판", "지역", "판매자", "연락처",
        "상태", "최초발견", "최근확인", "사라진날", "성능기록부", "상세링크",
    ]

    with open(보고서_파일, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=컬럼, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in 컬럼 if k in r.keys()})

    등록중 = sum(1 for r in rows if r["상태"] == "등록중")
    사라짐 = sum(1 for r in rows if r["상태"] == "사라짐")

    print(f"\n  보고서: {보고서_파일}")
    print(f"  등록중 {등록중}대 | 사라짐 {사라짐}대 | 합계 {len(rows)}대")

    db.close()


# ════════════════════════════════════════════════════════════
# 메인
# ════════════════════════════════════════════════════════════
def 메인():
    # 기록부만 채우기: --records (카매니저 로그인 없이 기록부 사이트만 엶)
    if "--records" in sys.argv:
        DB_초기화()
        Supabase_초기화()
        print(f"\n  ===== 기록부만 채우기 {_지금글()} (카매니저 접속 없음) =====")
        상세링크_고치기()
        사진주소_원본으로_고치기()
        드라이버 = 브라우저_열기()
        try:
            기록부_채우기(드라이버, 최대=1000)
        finally:
            try:
                드라이버.quit()
            except Exception:
                pass
        return

    # 0) 돌려도 되는지 먼저 (브라우저·로그인 전에). 안 되면 로그도 안 올리고 바로 끝.
    DB_초기화()
    됨, 이유 = _실행_해도_되나()
    if not 됨:
        print(f"  [{datetime.now().strftime('%m-%d %H:%M')} 쉼] {이유}")
        _로그_건너뛰기()
        return

    print()
    print(f"  ===== 실행 시작 {_지금글()} =====")
    print("  ┌────────────────────────────────────┐")
    print("  │     카매니저 매물 확인 프로그램      │")
    print("  ├────────────────────────────────────┤")
    print("  │  - 상세(사진·내용·성능기록부) 먼저   │")
    print("  │  - 목록은 이어서 조금씩, 이틀에 한 바퀴│")
    print("  │  - 이용제한 화면 보이면 바로 멈춤    │")
    print("  └────────────────────────────────────┘")
    사용 = 하루사용_읽기()
    print(f"  오늘 쓴 양: 상세 {사용['상세']}/{하루_상세_대수}대 · 목록 {사용['목록페이지']}/{하루_목록_페이지}페이지 · "
          f"로그인 {사용['로그인']}번")
    print(f"  이번 한도: 상세 {한번_상세_대수}대 · 목록 {한번_목록_페이지}페이지 · 대기 {대기_범위[0]}~{대기_범위[1]}초")
    print()

    # 아이디/비번 — 환경변수 우선, 없으면 직접 입력
    아이디 = os.environ.get("CM_ID", "").strip()
    비밀번호 = os.environ.get("CM_PW", "").strip()
    if 아이디 and 비밀번호:
        print(f"  계정: {아이디} (환경변수)")
    else:
        if HEADLESS:
            print("  .env에 CM_ID, CM_PW가 없습니다. 자동 실행 불가.")
            return
        아이디 = input("  카매니저 아이디: ").strip()
        비밀번호 = getpass.getpass("  카매니저 비밀번호: ").strip()

    if not 아이디 or not 비밀번호:
        print("  아이디와 비밀번호를 입력해주세요.")
        return

    Supabase_초기화()
    # 예전 형식으로 저장된 링크·사진 주소 고치기 (처음 한 번만 실제로 일함, 사이트 접속 없음)
    상세링크_고치기()
    사진주소_원본으로_고치기()

    print("\n  브라우저 여는 중..." + (" (헤드리스)" if HEADLESS else ""))
    드라이버 = 브라우저_열기()

    차량_목록 = []
    상세대상 = []
    try:
        print("  카매니저 로그인 중...")
        if not 로그인(드라이버, 아이디, 비밀번호):
            return

        # 1) 상세 먼저 — DB 에 있는 등록중 차 중 아직 안 받은 차
        사용 = 하루사용_읽기()
        남은상세 = min(한번_상세_대수, 하루_상세_대수 - 사용["상세"])
        if 남은상세 > 0:
            상세대상 = DB_상세_대상(남은상세)
            if 상세대상:
                상세_사진_수집(드라이버, 상세대상, 남은상세)
                # 상세에서 주소만 받아 둔 엠파크 기록부 내용 채우기 (기록부 사이트만, 카매니저 요청 없음)
                기록부_채우기(드라이버, 최대=len(상세대상) + 10)
            else:
                print("\n  상세: 남은 차 없음")
        else:
            print(f"\n  상세: 오늘 상한 {하루_상세_대수}대 다 씀")

        # 2) 목록 — 한 바퀴 도는 중이면 이어서, 다 돌았으면 이틀 뒤
        사용 = 하루사용_읽기()
        남은목록 = min(한번_목록_페이지, 하루_목록_페이지 - 사용["목록페이지"])
        차례, 차례이유 = 목록_할_차례()
        if not 목록_수집_켜짐:
            print(f"\n  목록: 꺼 둠 (다음에 켜면 {_목록진행_읽기().get('다음페이지')}페이지부터 이어서)")
        elif 남은목록 > 0 and 차례:
            if 상세대상:
                time.sleep(random.uniform(*대기_범위))  # 상세와 목록 사이도 사람처럼 쉼
            print(f"\n  목록: {차례이유}")
            차량_목록 = 목록_이어서(드라이버, 아이디, 비밀번호, 남은목록)
            print(f"  목록: 이번에 {len(차량_목록)}대 확인, 신규 {목록_신규}대")
        elif not 차례:
            print(f"\n  목록: 쉼 — {차례이유}")
        else:
            print(f"\n  목록: 오늘 상한 {하루_목록_페이지}페이지 다 씀")

        # 3) 사진 다운로드 (상세에서 받은 사진 여러 장 + 목록 대표 사진)
        받을것 = 상세대상 + 차량_목록
        if 받을것:
            print("\n  사진 다운로드 중...")
            다운, 실패 = 사진_다운로드(받을것)
            print(f"  다운로드: {다운}장" + (f" (실패 {실패}건)" if 실패 else ""))

        # 수집 기록
        db = DB_연결()
        db.execute(
            "INSERT INTO 수집기록 (시각,수집_건수,신규_건수,사라진_건수) VALUES (?,?,?,?)",
            (_지금글(), len(차량_목록), 목록_신규, 0)
        )
        db.commit()
        db.close()

        보고서_생성()

    except _이용제한감지 as e:
        print(f"\n  이용제한 감지로 멈춤 ({e}). 중지.txt 가 생겨서 다음 실행부터는 아무것도 안 합니다.")
    finally:
        try:
            드라이버.quit()
        except Exception:
            pass
        _다음실행_정하기()
        사용 = 하루사용_읽기()
        print(f"  오늘 누적: 상세 {사용['상세']}대 · 목록 {사용['목록페이지']}페이지 · 로그인 {사용['로그인']}번")

    print("\n  완료!")
    if not HEADLESS:
        input("  엔터를 누르면 종료...")


if __name__ == "__main__":
    메인()
