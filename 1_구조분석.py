"""
카매니저 매물조회 페이지 구조 분석
- 기존 크롬 프로필을 사용하여 로그인 없이 접속
- 매물조회 페이지 소스 + 네트워크 요청 저장

[실행 전] 크롬 브라우저를 모두 닫아주세요 (기존 프로필 사용을 위해)
"""

import json
import os
import time
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

사이트 = "https://www.carmanager.co.kr"
크롬_프로필 = os.path.expanduser("~") + "/AppData/Local/Google/Chrome/User Data"


def 브라우저_열기():
    옵션 = webdriver.ChromeOptions()
    옵션.add_argument("--lang=ko")
    옵션.add_argument("--start-maximized")
    옵션.add_argument(f"--user-data-dir={크롬_프로필}")
    옵션.add_argument("--profile-directory=Default")
    옵션.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    서비스 = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=서비스, options=옵션)


def 네트워크_요청_수집(드라이버):
    로그 = 드라이버.get_log("performance")
    요청들 = []
    for 항목 in 로그:
        try:
            메시지 = json.loads(항목["message"])["message"]
            if 메시지["method"] == "Network.requestWillBeSent":
                url = 메시지["params"]["request"]["url"]
                method = 메시지["params"]["request"]["method"]
                body = 메시지["params"]["request"].get("postData", "")
                if "carmanager.co.kr" in url and not any(
                    ext in url
                    for ext in [".css", ".js", ".png", ".jpg", ".gif", ".woff", ".ttf", ".ico"]
                ):
                    요청들.append({"method": method, "url": url, "body": body})
        except (KeyError, json.JSONDecodeError):
            pass
    return 요청들


def 메인():
    print("=" * 50)
    print(" 카매니저 매물조회 구조 분석")
    print("=" * 50)
    print()
    print("[중요] 크롬 브라우저를 모두 닫았는지 확인하세요!")
    print("(기존 로그인 상태를 그대로 사용합니다)")
    print()
    input("준비되면 엔터를 누르세요...")

    print("\n크롬을 여는 중...")
    드라이버 = 브라우저_열기()

    try:
        print("매물조회 페이지로 이동...")
        드라이버.get(f"{사이트}/Car/Data")
        time.sleep(5)

        현재_주소 = 드라이버.current_url
        print(f"현재 페이지: {현재_주소}")

        if "/User/Login" in 현재_주소:
            print("\n로그인이 필요합니다!")
            print("열린 브라우저에서 직접 로그인해주세요.")
            print("로그인 후 엔터를 누르세요...")
            input()
            드라이버.get(f"{사이트}/Car/Data")
            time.sleep(5)

        # 페이지 소스 저장
        with open("페이지_소스.html", "w", encoding="utf-8") as f:
            f.write(드라이버.page_source)
        print("\n페이지 소스 저장 완료 → 페이지_소스.html")

        # 네트워크 요청 수집
        요청들 = 네트워크_요청_수집(드라이버)
        with open("네트워크_요청.json", "w", encoding="utf-8") as f:
            json.dump(요청들, f, ensure_ascii=False, indent=2)
        print(f"네트워크 요청 {len(요청들)}건 저장 → 네트워크_요청.json\n")

        if 요청들:
            print("--- 주요 네트워크 요청 ---")
            for r in 요청들:
                print(f"  {r['method']:4s} {r['url'][:100]}")
                if r["body"]:
                    print(f"       본문: {r['body'][:200]}")

        print("\n\n브라우저가 열려 있습니다.")
        print("매물 목록이 보이면 스크롤해서 데이터를 확인하세요.")
        print("확인 후 엔터를 누르면 종료됩니다.")
        input()

    finally:
        드라이버.quit()


if __name__ == "__main__":
    메인()
