"""
부싼카 매물 수집 프로그램

부싼카(bussancar.com) API로 중고차 매물 정보를 수집한다.
카매니저 Selenium 파싱을 대체하는 경량 버전.

기능:
  1. 부싼카 API로 전체 매물 목록 수집
  2. 로컬 SQLite + Supabase 동시 저장
  3. 사라진 매물 자동 감지
  4. CSV 보고서 생성
  5. 프록시 지원 (일반/주거용)

사용법:
  py bussancar.py              (전체 수집)
  py bussancar.py --test       (100대만 테스트)
  py bussancar.py --proxy      (.env의 PROXY_URL 사용)
"""

import sqlite3
import os
import sys
import time
import csv
import json
import random
from datetime import datetime

import base64
from urllib.parse import quote

import requests

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.chrome.service import Service as ChromeService
    SELENIUM_OK = True
except ImportError:
    SELENIUM_OK = False

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

# ── 로그 자동 저장 (화면 + log.txt 동시 출력) ──
_script_dir = os.path.dirname(os.path.abspath(__file__))
_log_path = os.path.join(_script_dir, "log_bussancar.txt")


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

# ── Supabase 로그 올리기 (1분마다 실시간) ──
import atexit
import threading

_로그_실행ID = datetime.now().strftime("%m%d-%H%M%S")
_로그_PC = os.environ.get("COMPUTERNAME", "unknown")
_로그_위치 = _로그_시작위치
_로그_순번 = 0
_로그_마지막전송 = time.time()
_로그_잠금 = threading.Lock()
_로그_끝냄 = False


def _로그_행_추가(내용, 끝=False):
    global _로그_순번
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        return False
    표시 = f"{_로그_PC}|BSC-{_로그_실행ID}|{_로그_순번:03d}" + ("|끝" if 끝 else "")
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
    except Exception:
        pass
    return False


def _로그_보내기(끝=False):
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
            k = 새것.rfind(b"\n")
            새것 = 새것[:k + 1] if k >= 0 else b""
        if 새것:
            pos = 0
            while pos < len(새것):
                끝위치 = min(pos + 100000, len(새것))
                if 끝위치 < len(새것):
                    k = 새것.rfind(b"\n", pos, 끝위치)
                    if k > pos:
                        끝위치 = k + 1
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
            if _로그_행_추가(f"  (진행 중 — {datetime.now().strftime('%H:%M:%S')})"):
                _로그_마지막전송 = time.time()
        if 끝:
            _로그_끝냄 = True
    finally:
        _로그_잠금.release()


def _로그_주기적_보내기():
    time.sleep(10)
    while not _로그_끝냄:
        _로그_보내기()
        time.sleep(60)


threading.Thread(target=_로그_주기적_보내기, daemon=True).start()
atexit.register(lambda: _로그_보내기(끝=True))

try:
    from supabase import create_client
except ImportError:
    create_client = None


# ════════════════════════════════════════════════════════════
# 설정
# ════════════════════════════════════════════════════════════
API_목록_URL = "https://bussancar.com/proxy/api/v1/vehicle/list"
API_상세_URL = "https://bussancar.com/proxy/api/v1/vehicle/"
DB_파일 = "매물_데이터.db"
보고서_파일 = "매물_보고서.csv"
페이지_크기 = 100
대기_범위 = (1, 8)
긴쉼_주기 = 10
긴쉼_범위 = (15, 30)
세션끊기_주기 = 200
세션끊기_범위 = (60, 120)
상세_대기_범위 = (2, 10)
상세_긴쉼_주기 = 30
상세_긴쉼_범위 = (20, 45)
상세_세션끊기_주기 = 500
상세_세션끊기_범위 = (60, 180)
하루_상세_상한 = 5000
사진_루트 = os.environ.get("PHOTO_DIR", os.path.join("D:\\", "사진_부싼카"))

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
PROXY_URL = os.environ.get("PROXY_URL", "")

def _프록시_목록_로드():
    목록 = []
    env_list = os.environ.get("PROXY_LIST", "")
    if env_list:
        for 줄 in env_list.split(","):
            줄 = 줄.strip()
            if 줄:
                if not 줄.startswith("http"):
                    줄 = "http://" + 줄
                목록.append(줄)
    경로 = os.path.join(_script_dir, "proxies.txt")
    if os.path.exists(경로):
        with open(경로, "r", encoding="utf-8") as f:
            for 줄 in f:
                줄 = 줄.strip()
                if 줄 and not 줄.startswith("#"):
                    if not 줄.startswith("http"):
                        줄 = "http://" + 줄
                    if 줄 not in 목록:
                        목록.append(줄)
    if PROXY_URL and PROXY_URL not in 목록:
        목록.insert(0, PROXY_URL)
    return 목록

프록시_목록 = _프록시_목록_로드()
_프록시_순번 = 0

def _다음_프록시():
    global _프록시_순번
    if not 프록시_목록:
        return ""
    p = 프록시_목록[_프록시_순번 % len(프록시_목록)]
    _프록시_순번 += 1
    return p

테스트모드 = "--test" in sys.argv
목록만 = "--list-only" in sys.argv
프록시_사용 = "--proxy" in sys.argv or bool(PROXY_URL) or bool(프록시_목록)
supabase_client = None


# ════════════════════════════════════════════════════════════
# Supabase
# ════════════════════════════════════════════════════════════
def Supabase_초기화():
    global supabase_client
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("  Supabase 미설정 -> 로컬 DB만 사용")
        return False
    if not create_client:
        print("  supabase 패키지 없음 -> 로컬 DB만 사용")
        return False
    try:
        supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
        supabase_client.table("car_listings").select("count", count="exact").limit(0).execute()
        print("  Supabase 연결 성공!")
        return True
    except Exception as e:
        print(f"  Supabase 연결 실패: {e}")
        supabase_client = None
        return False


def Supabase_차량_저장(차량_목록):
    if not supabase_client:
        return
    지금 = datetime.now().isoformat()
    테이블 = supabase_client.table("car_listings")
    성공 = 0
    실패 = 0

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
            성공 += 1
        except Exception:
            실패 += 1

    print(f"  Supabase: {성공}대 저장" + (f" (실패 {실패}대)" if 실패 else ""))


def Supabase_사라진_처리(현재_차량번호들):
    if not supabase_client:
        return 0
    지금 = datetime.now().isoformat()
    테이블 = supabase_client.table("car_listings")
    현재 = set(str(x) for x in 현재_차량번호들)
    사라진 = 0

    try:
        오프셋 = 0
        while True:
            결과 = 테이블.select("car_id").eq("status", "active").range(오프셋, 오프셋 + 999).execute()
            if not 결과.data:
                break
            for row in 결과.data:
                if row["car_id"] not in 현재:
                    try:
                        테이블.update({"status": "removed", "removed_at": 지금}).eq("car_id", row["car_id"]).execute()
                        사라진 += 1
                    except Exception:
                        pass
            if len(결과.data) < 1000:
                break
            오프셋 += 1000
    except Exception as e:
        print(f"  Supabase 사라짐 처리 실패: {str(e)[:80]}")

    return 사라진


# ════════════════════════════════════════════════════════════
# 로컬 DB
# ════════════════════════════════════════════════════════════
def DB_연결():
    db_path = os.path.join(_script_dir, DB_파일)
    db = sqlite3.connect(db_path)
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
            사라진날 TEXT,
            상세완료 INTEGER DEFAULT 0,
            옵션 TEXT,
            차대번호 TEXT
        );

        CREATE TABLE IF NOT EXISTS 수집기록 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            시각 TEXT,
            수집_건수 INTEGER,
            신규_건수 INTEGER,
            사라진_건수 INTEGER
        );
    """)
    try:
        db.execute("ALTER TABLE 차량 ADD COLUMN 상세완료 INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        db.execute("ALTER TABLE 차량 ADD COLUMN 옵션 TEXT")
    except Exception:
        pass
    try:
        db.execute("ALTER TABLE 차량 ADD COLUMN 차대번호 TEXT")
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
    현재 = set(str(x) for x in 현재_차량번호들)

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
# API 호출
# ════════════════════════════════════════════════════════════
def _세션_만들기():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Referer": "https://bussancar.com/buy-car",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ko-KR,ko;q=0.9",
    })
    if 프록시_사용 and 프록시_목록:
        p = _다음_프록시()
        s.proxies = {"http": p, "https": p}
        print(f"  프록시: {p[:40]}...")
    return s


def API_매물_가져오기(세션, 페이지, 크기=100):
    try:
        r = 세션.get(API_목록_URL, params={"page": 페이지, "size": 크기}, timeout=30)
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            print(f"  API 오류: {data.get('message', '?')}")
            return None, 0
        return data["data"]["contents"], data["data"]["total"]
    except requests.exceptions.RequestException as e:
        print(f"  API 요청 실패: {str(e)[:100]}")
        return None, 0
    except (KeyError, json.JSONDecodeError) as e:
        print(f"  API 응답 파싱 실패: {str(e)[:100]}")
        return None, 0


def API_상세_가져오기(세션, 차량번호):
    try:
        r = 세션.get(f"{API_상세_URL}{차량번호}", timeout=30)
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            return None
        return data["data"]
    except Exception:
        return None


_사진_브라우저 = None


def _사진_브라우저_시작():
    global _사진_브라우저
    if _사진_브라우저:
        try:
            _ = _사진_브라우저.title
            return _사진_브라우저
        except Exception:
            _사진_브라우저 = None

    if not SELENIUM_OK:
        print("  selenium 없음 — 사진 다운로드 건너뜀")
        return None

    opts = ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--window-size=1280,720")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    try:
        _사진_브라우저 = webdriver.Chrome(options=opts)
        _사진_브라우저.set_page_load_timeout(30)
        print("  사진 브라우저 시작됨 (headless Chrome)")
        return _사진_브라우저
    except Exception as e:
        print(f"  사진 브라우저 시작 실패: {str(e)[:80]}")
        return None


def _사진_브라우저_종료():
    global _사진_브라우저
    if _사진_브라우저:
        try:
            _사진_브라우저.quit()
        except Exception:
            pass
        _사진_브라우저 = None


def 사진_다운로드(이미지목록, 차량번호):
    사진폴더 = os.path.join(사진_루트, str(차량번호))
    os.makedirs(사진폴더, exist_ok=True)

    저장된 = []
    건너뜀 = 0

    for img in 이미지목록:
        url = img.get("imageUrl", "")
        순번 = img.get("sortNo", 0)
        if not url:
            continue

        확장자 = ".png" if ".png" in url.lower() else ".jpg"
        파일명 = f"{순번:02d}{확장자}"
        경로 = os.path.join(사진폴더, 파일명)

        if os.path.exists(경로):
            저장된.append(경로)
            건너뜀 += 1
            continue

        driver = _사진_브라우저_시작()
        if not driver:
            break

        try:
            driver.get(url)
            time.sleep(0.3)
            데이터 = driver.execute_script("""
                var img = document.querySelector('img');
                if (!img || !img.complete || !img.naturalWidth) return null;
                var c = document.createElement('canvas');
                c.width = img.naturalWidth;
                c.height = img.naturalHeight;
                c.getContext('2d').drawImage(img, 0, 0);
                var fmt = arguments[0] === '.png' ? 'image/png' : 'image/jpeg';
                return c.toDataURL(fmt, 1.0).split(',')[1];
            """, 확장자)
            if 데이터:
                with open(경로, "wb") as f:
                    f.write(base64.b64decode(데이터))
                저장된.append(경로)
        except Exception:
            pass

    return 저장된


def API_차량_변환(item):
    v = item.get("vehicleInventory", {})

    부품 = [v.get("carMakerName", ""), v.get("carModelName", ""), v.get("carGradeName", "")]
    차종 = " ".join(x for x in 부품 if x).strip()

    가격원 = v.get("carAmountSale", "")
    try:
        가격 = f"{int(가격원):,}만원" if 가격원 else ""
    except (ValueError, TypeError):
        가격 = f"{가격원}만원" if 가격원 else ""

    km원 = v.get("carUseKm", "")
    try:
        주행거리 = f"{int(km원):,}km" if km원 else ""
    except (ValueError, TypeError):
        주행거리 = f"{km원}km" if km원 else ""

    지역 = f"{v.get('sido', '')} {v.get('city', '')}".strip()

    return {
        "차량번호": str(v.get("carNo", "")),
        "차종": 차종,
        "가격": 가격,
        "연식": v.get("carRegYear", ""),
        "주행거리": 주행거리,
        "연료": v.get("carFuel", ""),
        "변속기": v.get("carMission", ""),
        "색상": v.get("carColor", ""),
        "사고여부": "",
        "번호판": v.get("carPlateNumber", ""),
        "지역": 지역,
        "판매자": v.get("shopName", ""),
        "연락처": "",
        "사진URL": v.get("thumbnail", ""),
        "상세링크": f"https://bussancar.com/buy-car/{v.get('carNo', '')}",
    }


def 상세_파싱(세션):
    db = DB_연결()
    미완료 = db.execute(
        "SELECT 차량번호 FROM 차량 WHERE 상태='등록중' AND (상세완료 IS NULL OR 상세완료=0)"
    ).fetchall()
    db.close()

    전체대상 = [row["차량번호"] for row in 미완료]
    대상 = [n for n in 전체대상 if str(n).isdigit()]
    건너뜀 = len(전체대상) - len(대상)
    if 건너뜀 > 0:
        print(f"  상세 파싱: 숫자 아닌 옛 carNo {건너뜀}건 건너뜀 (카매니저 데이터)")
    if not 대상:
        print("  상세 파싱: 모든 차량 완료됨")
        return 0

    상한 = min(len(대상), 하루_상세_상한)
    if 테스트모드:
        상한 = min(len(대상), 5)
    print(f"  상세 파싱: 미완료 {len(대상)}대 중 {상한}대 진행")

    완료 = 0
    실패 = 0

    for i, 번호 in enumerate(대상[:상한]):
        if i > 0:
            if i % 상세_세션끊기_주기 == 0:
                쉼 = random.uniform(*상세_세션끊기_범위)
                print(f"  --- 상세 {i}건: 세션 쉼 {쉼:.0f}초 ---")
                time.sleep(쉼)
                세션 = _세션_만들기()
            elif i % 상세_긴쉼_주기 == 0:
                쉼 = random.uniform(*상세_긴쉼_범위)
                print(f"  --- 상세 {i}건: 잠깐 쉼 {쉼:.0f}초 ---")
                time.sleep(쉼)
            else:
                대기 = random.uniform(*상세_대기_범위)
                time.sleep(대기)

        상세 = API_상세_가져오기(세션, 번호)
        if not 상세:
            실패 += 1
            if i % 100 == 0:
                print(f"  상세 {i+1}/{상한}: {번호} 실패")
            continue

        vi = 상세.get("vehicleInventory", {})
        이미지들 = vi.get("images", []) or []
        사진URL들 = ",".join(img.get("imageUrl", "") for img in 이미지들 if img.get("imageUrl"))
        옵션 = vi.get("carOption", "")
        차대번호 = vi.get("carFrameNo", "")
        성능기록부 = vi.get("checkoutUrl", "")
        원본 = json.dumps(상세, ensure_ascii=False)

        저장된사진 = 사진_다운로드(이미지들, 번호)

        db = DB_연결()
        db.execute("""
            UPDATE 차량
            SET 사진URL들=?, 성능기록부=?, 옵션=?, 차대번호=?, 원본데이터=?, 상세완료=1
            WHERE 차량번호=?
        """, (사진URL들, 성능기록부, 옵션, 차대번호, 원본, 번호))
        db.commit()
        db.close()

        if supabase_client:
            try:
                supabase_client.table("car_listings").update({
                    "photo_urls": 사진URL들,
                }).eq("car_id", 번호).execute()
            except Exception:
                pass

        완료 += 1
        if (i + 1) % 100 == 0:
            print(f"  상세 {i+1}/{상한}: 완료 {완료}대 / 실패 {실패}대 / 사진 {len(저장된사진)}장")

    print(f"  상세 파싱 완료: {완료}대 성공, {실패}대 실패")
    남은 = len(대상) - 상한
    if 남은 > 0:
        print(f"  남은 미완료: {남은}대 (다음 실행에서 계속)")
    return 완료


# ════════════════════════════════════════════════════════════
# 보고서
# ════════════════════════════════════════════════════════════
def 보고서_생성():
    db = DB_연결()
    rows = db.execute(
        "SELECT * FROM 차량 WHERE 상태='등록중' ORDER BY 최근확인 DESC"
    ).fetchall()
    db.close()

    if not rows:
        print("  보고서: 등록중인 차량 없음")
        return

    경로 = os.path.join(_script_dir, 보고서_파일)
    with open(경로, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["차량번호", "차종", "가격", "연식", "주행거리", "연료", "변속기",
                     "색상", "번호판", "지역", "판매자", "사진URL", "상세링크",
                     "최초발견", "최근확인"])
        for r in rows:
            w.writerow([
                r["차량번호"], r["차종"], r["가격"], r["연식"], r["주행거리"],
                r["연료"], r["변속기"], r["색상"], r["번호판"], r["지역"],
                r["판매자"], r["사진URL"], r["상세링크"], r["최초발견"], r["최근확인"]
            ])

    print(f"  보고서: {len(rows)}대 -> {보고서_파일}")


# ════════════════════════════════════════════════════════════
# 메인
# ════════════════════════════════════════════════════════════
def 메인():
    DB_초기화()

    print()
    print(f"  ===== 부싼카 매물 수집 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} =====")
    print("  ┌────────────────────────────────────┐")
    print("  │      부싼카 API 매물 수집 프로그램    │")
    print("  ├────────────────────────────────────┤")
    if 테스트모드:
        print("  │  [테스트] 100대만 수집              │")
    else:
        print("  │  전체 매물 수집                     │")
    프록시표시 = "켜짐" if 프록시_사용 and PROXY_URL else "꺼짐"
    print(f"  │  프록시: {프록시표시}                          │")
    print("  └────────────────────────────────────┘")
    print()

    Supabase_초기화()

    세션 = _세션_만들기()

    print("  매물 목록 가져오는 중...")
    items, 전체 = API_매물_가져오기(세션, 1, 페이지_크기)
    if items is None:
        print("  첫 페이지 가져오기 실패. 종료.")
        return

    print(f"  전체 매물: {전체:,}대")

    최대 = 100 if 테스트모드 else 전체
    전체_차량 = []

    for item in items:
        차 = API_차량_변환(item)
        if 차["차량번호"]:
            전체_차량.append(차)
        if len(전체_차량) >= 최대:
            break

    총페이지 = (min(최대, 전체) + 페이지_크기 - 1) // 페이지_크기
    print(f"  수집 목표: {min(최대, 전체):,}대 ({총페이지}페이지)")
    print(f"  1페이지: {len(전체_차량)}대 수집")

    for p in range(2, 총페이지 + 1):
        if len(전체_차량) >= 최대:
            break

        if p % 세션끊기_주기 == 0:
            쉼 = random.uniform(*세션끊기_범위)
            print(f"  --- {p}페이지: 세션 쉼 {쉼:.0f}초 (새 탭 열듯) ---")
            time.sleep(쉼)
            세션 = _세션_만들기()
        elif p % 긴쉼_주기 == 0:
            쉼 = random.uniform(*긴쉼_범위)
            print(f"  --- {p}페이지: 잠깐 쉼 {쉼:.0f}초 ---")
            time.sleep(쉼)
        else:
            대기 = random.uniform(*대기_범위)
            time.sleep(대기)

        items, _ = API_매물_가져오기(세션, p, 페이지_크기)
        if items is None:
            print(f"  {p}페이지 실패 — 중단")
            break

        for item in items:
            차 = API_차량_변환(item)
            if 차["차량번호"]:
                전체_차량.append(차)
            if len(전체_차량) >= 최대:
                break

        print(f"  {p}/{총페이지}페이지: 누적 {len(전체_차량)}대")

    if not 전체_차량:
        print("  수집된 차량 없음. 종료.")
        return

    print(f"\n  DB 저장 중... ({len(전체_차량)}대)")
    신규 = 차량_저장(전체_차량)
    print(f"  로컬 DB: {len(전체_차량)}대 저장 (신규 {신규}대)")

    if supabase_client:
        print("  Supabase 저장 중...")
        Supabase_차량_저장(전체_차량)

    if not 테스트모드:
        현재번호들 = [차["차량번호"] for 차 in 전체_차량]
        사라진 = 사라진_매물_처리(현재번호들)
        sb사라진 = Supabase_사라진_처리(현재번호들)
        print(f"  사라진 매물: 로컬 {사라진}대" + (f" / Supabase {sb사라진}대" if supabase_client else ""))

    db = DB_연결()
    db.execute(
        "INSERT INTO 수집기록 (시각,수집_건수,신규_건수,사라진_건수) VALUES (?,?,?,?)",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), len(전체_차량), 신규, 0)
    )
    db.commit()
    db.close()

    보고서_생성()

    if not 목록만:
        print(f"\n  ===== 상세 파싱 시작 =====")
        상세완료 = 상세_파싱(세션)
    else:
        상세완료 = 0
        print("  --list-only: 상세 파싱 건너뜀")

    db = DB_연결()
    총등록 = db.execute("SELECT COUNT(*) FROM 차량 WHERE 상태='등록중'").fetchone()[0]
    총사라짐 = db.execute("SELECT COUNT(*) FROM 차량 WHERE 상태='사라짐'").fetchone()[0]
    총상세 = db.execute("SELECT COUNT(*) FROM 차량 WHERE 상세완료=1").fetchone()[0]
    미상세 = db.execute("SELECT COUNT(*) FROM 차량 WHERE 상태='등록중' AND (상세완료 IS NULL OR 상세완료=0)").fetchone()[0]
    db.close()

    print(f"\n  ===== 완료 =====")
    print(f"  이번 수집: {len(전체_차량)}대 (신규 {신규}대)")
    if 상세완료 > 0:
        print(f"  이번 상세: {상세완료}대")
    print(f"  DB 현황: 등록중 {총등록:,}대 / 사라짐 {총사라짐:,}대")
    print(f"  상세 현황: 완료 {총상세:,}대 / 미완료 {미상세:,}대")

    if 전체_차량:
        예시 = 전체_차량[0]
        print(f"\n  [예시] {예시['차종']} | {예시['가격']} | {예시['연식']}년 | "
              f"{예시['주행거리']} | {예시['번호판']} | {예시['지역']}")

    _사진_브라우저_종료()


if __name__ == "__main__":
    메인()
