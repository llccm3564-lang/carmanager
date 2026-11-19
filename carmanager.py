"""
카매니저 매물 확인 프로그램

기능:
  1. 카매니저 전체 매물 정보 수집 (차종, 가격, 연식, 주행거리, 연료, 변속기, 사고, 번호판, 지역, 판매자 등)
  2. 차량 사진 다운로드
  3. 사라진 매물(팔림/삭제) 자동 감지
  4. CSV 보고서 생성

사용법:
  py 매물확인.py              (아이디/비번 직접 입력)
  py 매물확인.py --headless   (화면 없이 백그라운드 실행)

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

        if 기존:
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
                 번호판,지역,판매자,연락처,사진URL,상세링크,원본데이터,
                 최초발견,최근확인,상태)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'등록중')
            """, (
                번호,
                차.get("차종", ""), 차.get("가격", ""), 차.get("연식", ""),
                차.get("주행거리", ""), 차.get("연료", ""), 차.get("변속기", ""),
                차.get("색상", ""), 차.get("사고여부", ""), 차.get("번호판", ""),
                차.get("지역", ""), 차.get("판매자", ""), 차.get("연락처", ""),
                차.get("사진URL", ""), 차.get("상세링크", ""), 원본,
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

    # 모든 새 페이지에 XHR 가로채기 스크립트 주입
    드라이버.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": """
        window.__captured = [];

        // XMLHttpRequest 가로채기
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

        // fetch 가로채기
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

    # alert 처리 (혹시 있으면)
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

    # 로그인 버튼 클릭
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

    # alert 처리
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
    """가로챈 XHR/fetch 응답에서 차량 데이터 추출"""
    차량들 = []

    for resp in 응답들:
        url = resp.get("url", "")
        body = resp.get("response", "")
        if not body:
            continue

        # JSON 응답 파싱 시도
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            # HTML 응답일 수도 있음 — 나중에 HTML 파싱으로 처리
            if "<table" in body.lower() or "<tr" in body.lower():
                차량들.extend(HTML에서_차량_추출(body))
            continue

        # JSON 구조에서 차량 목록 찾기
        found = _JSON에서_차량_찾기(data)
        차량들.extend(found)

    return 차량들


def _JSON에서_차량_찾기(data, depth=0):
    """JSON 데이터에서 차량 목록 재귀 탐색"""
    if depth > 5:
        return []

    차량들 = []

    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                # 차량 데이터인지 확인 (EnCarNo 또는 차량 관련 필드 존재)
                keys = set(item.keys())
                차량_필드 = {"EnCarNo", "CarModelName", "CarAmountSale", "CarRegYear",
                           "CarUseKm", "CarFuel", "CarMission", "CarPlateNumber"}
                if keys & 차량_필드:
                    차량들.append(_JSON_차량_변환(item))
                else:
                    차량들.extend(_JSON에서_차량_찾기(item, depth + 1))

    elif isinstance(data, dict):
        # 차량 데이터 직접 확인
        keys = set(data.keys())
        차량_필드 = {"EnCarNo", "CarModelName", "CarAmountSale"}
        if keys & 차량_필드:
            차량들.append(_JSON_차량_변환(data))
        else:
            # 하위 필드 탐색
            for v in data.values():
                if isinstance(v, (dict, list)):
                    차량들.extend(_JSON에서_차량_찾기(v, depth + 1))

    return 차량들


def _JSON_차량_변환(item):
    """카매니저 JSON 필드 → 한글 필드 매핑"""
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
    """카매니저 HTML 테이블에서 차량 목록 추출 (tr.uc_carcss 기반)"""
    soup = BeautifulSoup(html, "html.parser")
    차량들 = []

    for row in soup.select("tr.uc_carcss"):
        차 = {}
        tds = row.select("td")

        # 차량번호 (checkbox value = 암호화 ID)
        cb = row.select_one("input[type='checkbox']")
        if cb:
            차["차량번호"] = cb.get("value", "")
        if not 차.get("차량번호"):
            opt = row.select_one("[data-carno]")
            if opt:
                차["차량번호"] = opt.get("data-carno", "")
        if not 차.get("차량번호"):
            continue

        # 사진 URL
        img = row.select_one("img.thumbnail")
        if img:
            src = img.get("src", "")
            차["사진URL"] = "" if "noimage" in src else src
        else:
            차["사진URL"] = ""

        # 차종 = [제조사] 모델명
        maker = row.select_one("label.uc_carmaker")
        model_a = row.select_one("td.uc_textleft a")
        제조사 = maker.get_text(strip=True) if maker else ""
        모델명 = model_a.get_text(strip=True) if model_a else ""
        차["차종"] = f"{제조사} {모델명}".strip()

        # 상세링크
        if model_a:
            onclick = model_a.get("onclick", "") or ""
            m = re.search(r"['\"]([A-Za-z0-9+/=]{10,})['\"]", onclick)
            if m:
                차["상세링크"] = f"{사이트}/Car/Detail/{m.group(1)}"

        # 연식 (span.uc_carreg)
        reg = row.select_one("span.uc_carreg")
        if reg:
            차["연식"] = reg.get_text(strip=True)

        # 주행거리 (td.uc_carusekm)
        km = row.select_one(".uc_carusekm")
        if km:
            차["주행거리"] = km.get_text(strip=True)

        # 색상 (td.uc_carcolor)
        color = row.select_one(".uc_carcolor")
        if color:
            차["색상"] = color.get_text(strip=True)

        # 가격 (b.uc_caramount)
        amount = row.select_one(".uc_caramount")
        if amount:
            차["가격"] = amount.get_text(strip=True) + "만원"

        # 지역 (td.uc_cararea)
        area = row.select_one(".uc_cararea")
        if area:
            차["지역"] = " ".join(area.get_text(strip=True).split())

        # 판매자 / 연락처
        dealer = row.select_one("span.uc_cardealer")
        if dealer:
            차["판매자"] = dealer.get_text(strip=True)
        phone = row.select_one("span.uc_cardealer_phone")
        if phone:
            차["연락처"] = phone.get_text(strip=True)

        # 번호판 (span.uc_carreg_year — 첫 번째가 번호판 4자리)
        plates = row.select("span.uc_carreg_year")
        if plates:
            차["번호판"] = plates[0].get_text(strip=True)

        # 변속기 (5번째 td의 첫 p)
        if len(tds) >= 5:
            mission_p = tds[4].select_one("p")
            if mission_p:
                차["변속기"] = mission_p.get_text(strip=True)

        # 연료 (7번째 td)
        if len(tds) >= 7:
            차["연료"] = tds[6].get_text(strip=True)

        차량들.append(차)

    return 차량들


# ════════════════════════════════════════════════════════════
# 수집 엔진
# ════════════════════════════════════════════════════════════
def _검색_초기화(드라이버):
    """이전 검색 조건(북마크) 제거 + 페이지당 표시 수 100으로"""
    print("  검색 조건 초기화 중...")
    드라이버.execute_script("""
        // 모든 텍스트 input 비우기
        document.querySelectorAll('input[type="text"]').forEach(function(inp) {
            inp.value = '';
        });
        // 모든 hidden input 중 검색 관련 비우기
        document.querySelectorAll('input[type="hidden"]').forEach(function(inp) {
            if (inp.name && (inp.name.indexOf('Search') > -1 || inp.name.indexOf('Maker') > -1
                || inp.name.indexOf('Model') > -1 || inp.name.indexOf('Grade') > -1)) {
                inp.value = '';
            }
        });
        // select 초기화
        document.querySelectorAll('select').forEach(function(sel) {
            if (sel.name && sel.name !== 'sbxPageRowCount') {
                sel.selectedIndex = 0;
            }
        });
        // 페이지당 100건 표시
        var rowSel = document.querySelector('select[name="sbxPageRowCount"]');
        if (rowSel) {
            for (var i = 0; i < rowSel.options.length; i++) {
                if (parseInt(rowSel.options[i].value) >= 100) {
                    rowSel.value = rowSel.options[i].value;
                    break;
                }
            }
            if (parseInt(rowSel.value) < 50) rowSel.value = '50';
        }
    """)
    # form 직접 submit (goFormSubmitType이 없을 수 있으므로)
    try:
        드라이버.execute_script("""
            var form = document.querySelector('form');
            if (form) form.submit();
        """)
    except Exception:
        pass
    time.sleep(5)
    alert_처리(드라이버)
    print("  검색 초기화 완료")


def 전체_수집(드라이버):
    전체 = []

    print("  매물조회 페이지 접속 중...")
    가로챈_응답_비우기(드라이버)
    드라이버.get(f"{사이트}/Car/Data")

    # alert 처리
    for _ in range(3):
        time.sleep(2)
        msg = alert_처리(드라이버)
        if msg:
            print(f"  알림: {msg}")

    time.sleep(3)

    # 이전 검색 조건 초기화 (번호판 필터 등 제거)
    _검색_초기화(드라이버)

    # 첫 페이지 소스 저장
    with open("페이지_소스.html", "w", encoding="utf-8") as f:
        f.write(드라이버.page_source)
    print("  페이지 소스 저장 완료 (페이지_소스.html)")

    # 총 페이지 수 확인
    총_페이지 = _총_페이지_수(드라이버)
    if 최대_페이지 > 0:
        총_페이지 = min(총_페이지, 최대_페이지)
    print(f"\n  수집 대상: {총_페이지} 페이지\n")

    # 페이지별 수집
    for p in range(1, 총_페이지 + 1):
        print(f"  [{p:>4}/{총_페이지}] ", end="", flush=True)

        if p > 1:
            # 사람처럼 랜덤 딜레이 (봇 감지 방지)
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

        # HTML 파싱으로 추출 (카매니저는 서버 렌더링)
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
    """페이지네이션 정보에서 총 페이지 수 추출"""
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
            // 방법 3: 페이지 링크에서 최대값
            var max = 1;
            document.querySelectorAll('a').forEach(function(a) {
                var oc = a.getAttribute('onclick') || '';
                var m = oc.match(/goPageSubmit\\((\\d+)\\)/);
                if (m) {
                    var n = parseInt(m[1]);
                    if (n > max) max = n;
                }
                // href에 page 파라미터
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
# 사진 다운로드
# ════════════════════════════════════════════════════════════
def 사진_다운로드(차량_목록):
    os.makedirs(사진_폴더, exist_ok=True)
    세션 = requests.Session()
    세션.headers["User-Agent"] = "Mozilla/5.0"
    다운 = 0
    실패 = 0

    for 차 in 차량_목록:
        url = 차.get("사진URL", "")
        if not url or not url.startswith("http"):
            continue

        확장자 = ".jpg"
        if ".png" in url.lower():
            확장자 = ".png"
        elif ".webp" in url.lower():
            확장자 = ".webp"

        안전_이름 = re.sub(r'[\\/:*?"<>|]', "_", 차.get("차량번호", "unknown"))
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
    print("  │  - 차량 사진 다운로드               │")
    print("  │  - 사라진 매물 자동 감지            │")
    print("  └────────────────────────────────────┘")
    print()

    # 아이디/비번 입력
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
        # 로그인
        print("  카매니저 로그인 중...")
        if not 로그인(드라이버, 아이디, 비밀번호):
            return

        차량_목록 = 전체_수집(드라이버)

        if not 차량_목록:
            print("\n  차량 데이터를 추출하지 못했습니다.")
            print("  '페이지_소스.html' 파일을 확인해주세요.")
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
            print(f"  {차.get('차종','?'):20s}  {차.get('가격','?'):>10s}"
                  f"  {차.get('연식','?'):>6s}  {차.get('주행거리','?'):>10s}"
                  f"  {차.get('지역','?')}")

    finally:
        드라이버.quit()

    print("\n  완료!")
    input("  엔터를 누르면 종료...")


if __name__ == "__main__":
    메인()
