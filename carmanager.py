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

# ── 종료 시 로그를 Supabase에 자동 업로드 ──
import atexit

def _upload_log_to_supabase():
    try:
        _log_file.flush()
        log_content = open(_log_path, "r", encoding="utf-8").read()
        if not log_content.strip():
            return
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_KEY", "")
        if not url or not key:
            return
        requests.post(
            f"{url}/rest/v1/error_logs",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal"
            },
            json={
                "content": log_content[-50000:],
                "pc_name": os.environ.get("COMPUTERNAME", "unknown")
            },
            timeout=10
        )
    except Exception:
        pass

atexit.register(_upload_log_to_supabase)

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
    # 기존 DB에 사진URL들 컬럼이 없으면 추가
    try:
        db.execute("ALTER TABLE 차량 ADD COLUMN 사진URL들 TEXT")
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
            onclick = model_a.get("onclick", "") or ""
            m = re.search(r"['\"]([A-Za-z0-9+/=]{10,})['\"]", onclick)
            if m:
                차["상세링크"] = f"{사이트}/Car/Detail/{m.group(1)}"

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
def _검색_초기화(드라이버, 아이디, 비밀번호):
    """검색 필터 초기화 + 페이지당 50건 설정"""
    print("  검색 조건 초기화 중...")

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


def 전체_수집(드라이버, 아이디, 비밀번호):
    전체 = []

    print("  매물조회 페이지 접속 중...")
    가로챈_응답_비우기(드라이버)
    드라이버.get(f"{사이트}/Car/Data")

    for _ in range(3):
        time.sleep(2)
        msg = alert_처리(드라이버)
        if msg:
            print(f"  알림: {msg}")

    time.sleep(3)

    _검색_초기화(드라이버, 아이디, 비밀번호)

    with open("페이지_소스.html", "w", encoding="utf-8") as f:
        f.write(드라이버.page_source)
    print("  페이지 소스 저장 완료 (페이지_소스.html)")

    총_페이지 = _총_페이지_수(드라이버)
    if 최대_페이지 > 0:
        총_페이지 = min(총_페이지, 최대_페이지)
    print(f"\n  수집 대상: {총_페이지} 페이지\n")

    for p in range(1, 총_페이지 + 1):
        print(f"  [{p:>4}/{총_페이지}] ", end="", flush=True)

        if p > 1:
            대기 = random.uniform(3, 7)
            if p % 10 == 0:
                대기 = random.uniform(10, 20)
                print(f"(쉬는 중 {대기:.0f}초) ", end="", flush=True)
            time.sleep(대기)

            try:
                드라이버.execute_script(f"goPageSubmit({p})")
            except Exception:
                try:
                    드라이버.execute_script(
                        f"document.querySelector('input[name=\"PageNumber\"]').value={p};"
                        f"document.querySelector('form').submit();"
                    )
                except Exception:
                    print("페이지 이동 실패 — 중단")
                    break
            time.sleep(random.uniform(2, 4))
            alert_처리(드라이버)

        차량들 = HTML에서_차량_추출(드라이버.page_source)

        if 차량들:
            전체.extend(차량들)
            print(f"{len(차량들):>3}대 추출")
        else:
            print("데이터 없음")
            if p == 1:
                print("\n  첫 페이지에서 데이터를 못 찾았습니다.")
                print("  '페이지_소스.html'을 확인해주세요.")

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
def 상세_사진_수집(드라이버, 차량_목록):
    """각 차량 상세 페이지에서 사진 여러 장 수집 (배치 단위)"""

    # 이미 사진을 수집한 차량 제외
    수집완료 = set()
    db = DB_연결()
    try:
        rows = db.execute(
            "SELECT 차량번호 FROM 차량 WHERE 사진URL들 IS NOT NULL AND 사진URL들 != '' AND 사진URL들 != '[]'"
        ).fetchall()
        수집완료 = {r["차량번호"] for r in rows}
    except Exception:
        pass
    db.close()

    대상 = [차 for 차 in 차량_목록 if 차.get("상세링크") and 차.get("차량번호") not in 수집완료]

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
        print(f"  [{i+1:>4}/{len(배치)}] {차종:20s} ", end="", flush=True)

        if i > 0:
            대기 = random.uniform(2, 5)
            if (i + 1) % 20 == 0:
                대기 = random.uniform(8, 15)
                print(f"(쉬는 중 {대기:.0f}초) ", end="", flush=True)
            time.sleep(대기)

        try:
            드라이버.get(링크)
            time.sleep(random.uniform(2, 4))
            alert_처리(드라이버)

            사진들 = 드라이버.execute_script("""
                var photos = [];
                var seen = {};

                document.querySelectorAll('img').forEach(function(img) {
                    var src = img.src || img.getAttribute('data-src') || img.getAttribute('data-original') || '';
                    if (!src || src.indexOf('noimage') !== -1) return;
                    if (src.indexOf('logo') !== -1 || src.indexOf('icon') !== -1) return;
                    if (src.indexOf('btn_') !== -1 || src.indexOf('bg_') !== -1) return;
                    if (src.indexOf('banner') !== -1 || src.indexOf('footer') !== -1) return;
                    if (src.indexOf('/Upload/') !== -1 || src.indexOf('/CarPhoto/') !== -1 ||
                        src.indexOf('/photo/') !== -1 || src.indexOf('/Photo/') !== -1 ||
                        src.indexOf('/img/car') !== -1 || src.indexOf('/images/car') !== -1) {
                        if (!seen[src]) { seen[src] = true; photos.push(src); }
                    }
                    else if (img.naturalWidth > 150 && img.naturalHeight > 100) {
                        if (!seen[src]) { seen[src] = true; photos.push(src); }
                    }
                });

                document.querySelectorAll('a[href]').forEach(function(a) {
                    var href = a.href || '';
                    if (href.match(/\\.(jpg|jpeg|png|webp|gif)/i) &&
                        (href.indexOf('/Upload/') !== -1 || href.indexOf('/CarPhoto/') !== -1 ||
                         href.indexOf('/photo/') !== -1 || href.indexOf('/Photo/') !== -1)) {
                        if (!seen[href]) { seen[href] = true; photos.push(href); }
                    }
                });

                document.querySelectorAll('[style*="background"]').forEach(function(el) {
                    var style = window.getComputedStyle(el).backgroundImage || el.style.backgroundImage || '';
                    var m = style.match(/url\\(['"]?(.*?)['"]?\\)/);
                    if (m && m[1]) {
                        var src = m[1];
                        if (src.indexOf('noimage') === -1 && !seen[src] &&
                            (src.indexOf('/Upload/') !== -1 || src.indexOf('/CarPhoto/') !== -1 ||
                             src.indexOf('/photo/') !== -1 || src.indexOf('/Photo/') !== -1)) {
                            seen[src] = true;
                            photos.push(src);
                        }
                    }
                });

                return photos;
            """) or []

            차["사진URL들"] = 사진들

            if 사진들:
                수집_성공 += 1
                총_장수 += len(사진들)
                print(f"사진 {len(사진들)}장")

                # 즉시 DB에 저장 (다음 실행에서 건너뛰도록)
                db = DB_연결()
                db.execute(
                    "UPDATE 차량 SET 사진URL들=? WHERE 차량번호=?",
                    (json.dumps(사진들, ensure_ascii=False), 번호)
                )
                db.commit()
                db.close()

                # Supabase에도 즉시 저장
                if supabase_client:
                    try:
                        supabase_client.table("car_listings").update({
                            "photo_urls": json.dumps(사진들, ensure_ascii=False)
                        }).eq("car_id", 번호).execute()
                    except Exception:
                        pass
            else:
                print("사진 없음")

        except Exception as e:
            차["사진URL들"] = []
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
        "상태", "최초발견", "최근확인", "사라진날", "상세링크",
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

        # DB 저장 + 사라진 매물 처리 (로컬 + Supabase)
        신규 = 차량_저장(차량_목록)
        번호들 = [차["차량번호"] for 차 in 차량_목록 if 차.get("차량번호")]
        사라진 = 사라진_매물_처리(번호들)

        Supabase_차량_저장(차량_목록)
        Supabase_사라진_처리(번호들)

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
