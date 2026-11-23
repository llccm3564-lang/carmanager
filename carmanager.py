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
from datetime import datetime
from urllib.parse import urljoin

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

try:
    _log_file = open(_log_path, "w", encoding="utf-8")
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
_로그_위치 = 0          # log.txt 에서 이미 올린 바이트 위치
_로그_순번 = 0
_로그_마지막전송 = time.time()
_로그_잠금 = threading.Lock()
_로그_끝냄 = False


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
최대_페이지 = 0  # 0 = 전체, 숫자 = 해당 페이지까지만
상세_사진_배치 = 100  # 한 번에 상세 사진 수집할 차량 수
세션_갱신_페이지 = 20  # N페이지마다 쿠키 날리고 재로그인 (봇 감지 방지)
세션_갱신_상세 = 20  # 상세 N대마다 쿠키 날리고 재로그인

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
    """)
    # 기존 DB에 없는 컬럼 추가
    for col in ["사진URL들 TEXT", "성능기록부 TEXT"]:
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
    드라이버.get(f"{사이트}/User/Login")
    time.sleep(3)

    alert_처리(드라이버)

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

    time.sleep(3)

    msg = alert_처리(드라이버)
    if msg:
        print(f"  알림: {msg}")

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
                차["상세링크"] = f"{사이트}/PopupFrame/CarDetailEnc/?encarno={m.group(1)}"

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
            "PageSort": val('sbxPageSort', '0'), "PageAscDesc": val('sbxPageAscDesc', '1'),
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
    time.sleep(3)
    try:
        드라이버.set_script_timeout(60)
    except Exception:
        pass


def 전체_수집(드라이버, 아이디, 비밀번호):
    global 목록_완료, 목록_신규
    목록_완료 = False
    목록_신규 = 0
    전체 = []
    본_번호 = set()

    print("  매물조회 페이지 접속 중...")
    가로챈_응답_비우기(드라이버)
    _목록_페이지_열기(드라이버)
    print(f"  수집 지역: 시도={검색_시도} 지역={검색_지역} 단지={검색_단지} (목록 요청 /Car/DataPart 직접)")

    총_페이지 = None   # 첫 응답의 전체 건수로 정함. 못 찾으면 50대 미만 페이지가 나올 때까지
    p = 1
    while True:
        if 총_페이지 is not None and p > 총_페이지:
            목록_완료 = True
            break
        if 최대_페이지 > 0 and p > 최대_페이지:
            break
        if p > 1000:
            print("  1000페이지 넘음 — 안전장치로 중단")
            break

        # 세션 갱신: N페이지마다 쿠키 날리고 재로그인 (봇 감지 방지)
        if p > 1 and 세션_갱신_페이지 > 0 and (p - 1) % 세션_갱신_페이지 == 0:
            갱신대기 = random.uniform(15, 30)
            print(f"\n  [세션 갱신] 쿠키 초기화 + 재로그인 ({갱신대기:.0f}초 대기 후)...")
            time.sleep(갱신대기)
            드라이버.delete_all_cookies()
            time.sleep(2)
            if not 로그인(드라이버, 아이디, 비밀번호):
                print("  재로그인 실패! 여기까지 모은 것만 저장")
                break
            _목록_페이지_열기(드라이버)

        print(f"  [{p:>4}/{총_페이지 or '?'}] ", end="", flush=True)

        if p > 1:
            대기 = random.uniform(8, 15)
            if p % 10 == 0:
                대기 = random.uniform(30, 60)
                print(f"(쉬는 중 {대기:.0f}초) ", end="", flush=True)
            time.sleep(대기)

        try:
            응답 = _목록_요청(드라이버, p)
        except Exception as e:
            alert_처리(드라이버)
            print(f"요청 실패 ({str(e)[:120]})")
            break
        time.sleep(random.uniform(3, 6))  # 예전 화면 로딩 대기와 같은 간격 유지
        alert_처리(드라이버)

        html = 응답.get("html", "") or ""
        if 응답.get("error") or 응답.get("status") != 200:
            앞부분 = " ".join(html.split())[:800]
            print(f"요청 실패: 상태={응답.get('status')} 오류={응답.get('error')}")
            print(f"  응답 앞부분: {앞부분}")
            break

        # 응답 안 스크립트가 갱신하는 값: 전체 건수 / EncCheck
        건수 = (re.search(r"hdfCarRowCount[\"']?\)\.val\(\s*['\"](\d+)", html)
              or re.search(r"ui_totalcount[\"']?\)\.html\(\s*remakeNumber\(\s*['\"](\d+)", html)
              or re.search(r"reLoadPage\(\s*['\"](\d+)['\"]", html))
        새_enc = re.search(r"EncCheck[\"']?\)\.val\(\s*['\"]([^'\"]+)", html)
        if 새_enc and 새_enc.group(1) != 응답.get("enc"):
            드라이버.execute_script(
                "var e=document.getElementById('EncCheck'); if(e) e.value=arguments[0];", 새_enc.group(1))

        차량들 = HTML에서_차량_추출(html)

        if p == 1:
            with open("페이지_소스.html", "w", encoding="utf-8") as f:
                f.write(html)
            전체건수 = int(건수.group(1)) if 건수 else None
            단지들 = sorted({(차.get("지역") or "")[:25] for 차 in 차량들})[:5]
            print(f"\n  [첫 응답] 상태={응답.get('status')} 길이={len(html)} 전체건수={전체건수} "
                  f"화면칸건수={응답.get('total')} 행={len(차량들)} 단지={단지들}")
            print(f"  [첫 응답] 보낸 EncCheck={응답.get('sentEnc')} PrePage={응답.get('sentPre')} → "
                  f"받은 뒤 EncCheck={응답.get('enc')} PrePage={응답.get('pre')} 적용오류={응답.get('applyError')}")
            if 전체건수:
                총_페이지 = max(1, -(-전체건수 // 50))
            if not 차량들:
                print(f"  [첫 응답] 목록 없음 — 응답 앞부분: {' '.join(html.split())[:1500]}")
            print(f"  수집 대상: {총_페이지 if 총_페이지 else '끝날 때까지'} 페이지")
            print(f"  [{p:>4}/{총_페이지 or '?'}] ", end="", flush=True)

        if not 차량들:
            print("데이터 없음")
            if p > 1 and 총_페이지 is None:
                목록_완료 = True
            break

        새것 = [차 for 차 in 차량들 if 차.get("차량번호") not in 본_번호]
        if not 새것:
            print("앞 페이지와 같은 목록 — 페이지 넘김이 안 됨, 중단")
            break
        for 차 in 새것:
            본_번호.add(차.get("차량번호"))
        전체.extend(새것)

        # 페이지마다 바로 저장 (중간에 꺼져도 여기까지 훑은 차는 남음)
        저장메모 = ""
        try:
            이번신규 = 차량_저장(새것)
            목록_신규 += 이번신규
            Supabase_차량_저장(새것)
            if 이번신규:
                저장메모 = f" (신규 {이번신규})"
        except Exception as e:
            저장메모 = f" (저장 오류: {str(e)[:60]})"
        print(f"{len(새것):>3}대 추출{저장메모}")

        if 총_페이지 is None and len(차량들) < 50:
            목록_완료 = True
            break
        p += 1

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
    try:
        r = requests.get(주소, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
            "Referer": 상세주소 or f"{사이트}/",
        })
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


def 상세_사진_수집(드라이버, 차량_목록):
    """각 차량 상세 페이지에서 사진 여러 장 수집 (배치 단위)"""

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

    # 상세 요청에는 목록 페이지의 EncCheck 값이 필요하다 → 목록 페이지에 있어야 함
    try:
        드라이버.set_script_timeout(40)
        if "/Car/Data" not in (드라이버.current_url or ""):
            드라이버.get(f"{사이트}/Car/Data")
            time.sleep(5)
            for _ in range(3):
                alert_처리(드라이버)
                time.sleep(1)
    except Exception:
        pass

    if not 대상:
        print("\n  상세 사진: 모든 차량 수집 완료 (건너뜀)")
        return

    배치 = 대상[:상세_사진_배치]
    남은 = len(대상) - len(배치)
    print(f"\n  상세 사진 수집: {len(배치)}대 (미수집 {len(대상)}대 중, 나머지 {남은}대는 다음 실행)")

    수집_성공 = 0
    총_장수 = 0

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

        print(f"  [{i+1:>4}/{len(배치)}] {차종:20s} ", end="", flush=True)

        if i > 0:
            대기 = random.uniform(5, 10)
            if (i + 1) % 20 == 0:
                대기 = random.uniform(30, 60)
                print(f"(쉬는 중 {대기:.0f}초) ", end="", flush=True)
            time.sleep(대기)

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
            time.sleep(random.uniform(3, 6))  # 예전 화면 로딩 대기와 같은 간격 유지
            alert_처리(드라이버)

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

            사진들 = 상세.get("photos", []) or []
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
                    부분출력.append("기록부 실패")
                else:
                    부분출력.append("기록부O" if 기록.get("exists") else "기록부 없음")

            # 상세 응답을 정상으로 받았고 기록부 요청도 실패하지 않았으면 「완료표시」 → 다음 실행부터 건너뜀
            # (사진이 원래 없는 차도 여기서 끝. 실패한 차는 표시 안 해서 다음 실행에 다시 시도)
            정상응답 = 상세.get("status") == 200 and (상세.get("len") or 0) > 1000
            if 정상응답 and not 기록.get("error"):
                성능["detail_v"] = 상세_버전
                성능["fetched_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            if 정상응답:
                수집_성공 += 1
                총_장수 += len(사진들)
                print(" | ".join(부분출력) if 부분출력 else "내용 없음")

                성능JSON = json.dumps(성능, ensure_ascii=False)

                db = DB_연결()
                사진JSON = json.dumps(사진들, ensure_ascii=False) if 사진들 else None
                db.execute(
                    "UPDATE 차량 SET 사진URL들=COALESCE(?,사진URL들), 성능기록부=COALESCE(?,성능기록부) WHERE 차량번호=?",
                    (사진JSON, 성능JSON, 번호)
                )
                db.commit()
                db.close()

                if supabase_client:
                    try:
                        업데이트 = {}
                        if 사진들:
                            업데이트["photo_urls"] = json.dumps(사진들, ensure_ascii=False)
                        if 성능JSON:
                            업데이트["performance_record"] = 성능JSON
                        if 업데이트:
                            supabase_client.table("car_listings").update(업데이트).eq("car_id", 번호).execute()
                    except Exception:
                        pass
            else:
                print(f"상세 응답 이상 (상태={상세.get('status')} 길이={상세.get('len')}) — 다음 실행에 다시")

        except Exception as e:
            차["사진URL들"] = []
            차["성능기록부"] = {}
            print(f"실패 ({str(e)[:50]})")

    print(f"\n  상세 사진 완료: {수집_성공}대에서 총 {총_장수}장 수집")
    if 남은 > 0:
        print(f"  남은 {남은}대는 다음 실행에서 계속")


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
    print()
    print("  ┌────────────────────────────────────┐")
    print("  │     카매니저 매물 확인 프로그램      │")
    print("  ├────────────────────────────────────┤")
    print("  │  - 전체 매물 정보 수집              │")
    print("  │  - 차량 사진 다운로드 (여러 장)      │")
    print("  │  - 사라진 매물 자동 감지            │")
    print("  └────────────────────────────────────┘")
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

    DB_초기화()
    Supabase_초기화()

    print("\n  브라우저 여는 중..." + (" (헤드리스)" if HEADLESS else ""))
    드라이버 = 브라우저_열기()

    try:
        print("  카매니저 로그인 중...")
        if not 로그인(드라이버, 아이디, 비밀번호):
            return

        차량_목록 = 전체_수집(드라이버, 아이디, 비밀번호)

        if not 차량_목록:
            print("\n  차량 데이터를 추출하지 못했습니다.")
            print("  '페이지_소스.html' 파일을 확인해주세요.")
            if not HEADLESS:
                input("\n  엔터를 누르면 종료...")
            return

        print(f"\n  === 수집 완료: {len(차량_목록)}대 ===")

        # 저장은 목록 페이지마다 이미 함 → 여기선 사라진 매물 처리만 (로컬 + Supabase)
        신규 = 목록_신규
        번호들 = [차["차량번호"] for 차 in 차량_목록 if 차.get("차량번호")]
        if 목록_완료:
            사라진 = 사라진_매물_처리(번호들)
            Supabase_사라진_처리(번호들)
        else:
            사라진 = 0
            print("  목록을 끝까지 못 받아서 이번엔 「사라짐」 처리를 건너뜀")

        print(f"  신규 등록: {신규}대")
        if 사라진 > 0:
            print(f"  사라진 매물: {사라진}대 (팔렸거나 내려감)")

        # 상세 사진 수집 (차량별 여러 장, 배치 단위)
        상세_사진_수집(드라이버, 차량_목록)

        # 사진 다운로드
        print("\n  사진 다운로드 중...")
        다운, 실패 = 사진_다운로드(차량_목록)
        print(f"  다운로드: {다운}장" + (f" (실패 {실패}건)" if 실패 else ""))

        # 수집 기록
        db = DB_연결()
        db.execute(
            "INSERT INTO 수집기록 (시각,수집_건수,신규_건수,사라진_건수) VALUES (?,?,?,?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), len(차량_목록), 신규, 사라진)
        )
        db.commit()
        db.close()

        # 보고서
        보고서_생성()

        # 샘플 출력
        print("\n  --- 최근 수집 샘플 (최대 5대) ---")
        for 차 in 차량_목록[:5]:
            사진수 = len(차.get("사진URL들", []))
            print(f"  {차.get('차종','?'):20s}  {차.get('가격','?'):>10s}"
                  f"  {차.get('연식','?'):>6s}  {차.get('주행거리','?'):>10s}"
                  f"  {차.get('지역','?')}  사진:{사진수}장")

    finally:
        드라이버.quit()

    print("\n  완료!")
    if not HEADLESS:
        input("  엔터를 누르면 종료...")


if __name__ == "__main__":
    메인()
