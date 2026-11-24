# -*- coding: utf-8 -*-
"""
항목뽑기 — 저장해 둔 원문에서 차 한 대의 항목을 뽑는다 (사이트 접속 없음).

  상세_항목(상세원문 HTML)        → 카매니저 상세 페이지 항목
  기록부_항목(기록부 글, 있음여부) → 성능기록부 항목 (■ 체크 / □ 안 체크 로 표시된 글)

항목마다 {"값": ..., "상태": "값" | "없음" | "오류", "이유": (없음·오류일 때)}
  값   : 정상으로 뽑음
  없음 : 원래 그 차에 없는 것 (빈 칸, 사진 0장, 옵션 체크 0개, 기록부 내역 없음 등)
  오류 : 있어야 할 자리를 못 찾음 (페이지 모양이 다르거나 원문이 없음) → 확인 필요
"""
import re
from bs4 import BeautifulSoup

값, 없음, 오류 = "값", "없음", "오류"


def _칸(v, 이유=None):
    if v is None or v == "" or v == []:
        return {"값": v if v == [] else None, "상태": 없음, "이유": 이유 or "빈 칸"}
    return {"값": v, "상태": 값}


def _오류(이유):
    return {"값": None, "상태": 오류, "이유": 이유}


def _글(el):
    return " ".join(el.get_text(" ").split()) if el else ""


# ════════════════════════════════════════════════════════════
# 카매니저 상세 페이지
# ════════════════════════════════════════════════════════════
상세_항목_이름 = [
    "카매니저번호", "차명", "차량번호", "주행거리km", "미션", "연료", "색상", "연식", "최초등록일",
    "주차위치", "제시번호", "제시일", "조합", "조합전화", "상사", "상사전화", "딜러", "딜러전화", "종사원증번호",
    "추가사항", "한줄광고·사고내역", "압류건수", "저당건수", "세금미납정보", "차량상세설명",
    "옵션", "옵션수", "사진수", "기록부주소",
]


def 상세_항목(원문):
    if not 원문:
        return {k: _오류("상세 원문 없음 (원문 저장 전에 받은 차)") for k in 상세_항목_이름}
    s = BeautifulSoup(원문, "html.parser")
    out = {}

    def th칸(이름):
        th = s.find("th", string=lambda x: x is not None and x.strip() == 이름)
        if not th:
            return None
        return th.find_next_sibling("td")

    # 카매니저 고유번호 (숫자)
    e = s.find("input", id="cmCarNo")
    out["카매니저번호"] = _칸(e.get("value", "").strip()) if e else _오류("cmCarNo 칸 없음")

    td = th칸("차명")
    out["차명"] = _칸(_글(td)) if td is not None else _오류("차명 칸 없음")

    e = s.find("input", id="carplatenoCopy")
    out["차량번호"] = _칸((e.get("value") or "").strip()) if e else _오류("차량번호 칸 없음")

    td = th칸("주행거리")
    if td is not None:
        v = (td.get("value") or "").strip() or re.sub(r"[^\d]", "", _글(td))
        out["주행거리km"] = _칸(int(v) if v.isdigit() else None)
    else:
        out["주행거리km"] = _오류("주행거리 칸 없음")

    for 이름 in ["미션", "연료", "색상"]:
        td = th칸(이름)
        out[이름] = _칸(_글(td)) if td is not None else _오류(f"{이름} 칸 없음")

    td = th칸("연형")
    if td is not None:
        t = _글(td)
        m1 = re.search(r"(\d{4})\s*년", t)
        m2 = re.search(r"\((\d{4}-\d{2}-\d{2})\)", t)
        out["연식"] = _칸(m1.group(1) if m1 else None)
        out["최초등록일"] = _칸(m2.group(1) if m2 else None)
    else:
        out["연식"] = out["최초등록일"] = _오류("연형 칸 없음")

    e = s.find(id="span_CarParkingPosition")
    out["주차위치"] = _칸(_글(e)) if e is not None else _오류("주차위치 칸 없음")

    td = th칸("제시번호")
    if td is not None:
        t = _글(td)
        m = re.match(r"(\S+)\s*(?:\((\d{4}-\d{2}-\d{2})\))?", t)
        out["제시번호"] = _칸(m.group(1) if m else None)
        out["제시일"] = _칸(m.group(2) if m and m.group(2) else None)
    else:
        out["제시번호"] = out["제시일"] = _오류("제시번호 칸 없음")

    td = th칸("담당사원")
    if td is not None:
        t = _글(td)
        # 모양: 조합이름 ( 조합전화) / 상사이름 (상사전화)/ 딜러이름 ( 딜러휴대폰 ) 증번호 : 종사원증번호
        조각 = [x.strip() for x in t.split("/")]
        def 이름전화(x):
            m = re.match(r"(.*?)\s*\(\s*([\d\-]+)\s*\)", x or "")
            return (m.group(1).strip(), m.group(2)) if m else ((x or "").strip(), None)
        조합, 조합전화 = 이름전화(조각[0] if 조각 else "")
        상사, 상사전화 = 이름전화(조각[1] if len(조각) > 1 else "")
        나머지 = " / ".join(조각[2:]) if len(조각) > 2 else ""
        증 = re.search(r"증번호\s*:\s*(\S+)", 나머지)
        딜러, 딜러전화 = 이름전화(re.sub(r"증번호\s*:\s*\S+", "", 나머지))
        out.update({"조합": _칸(조합), "조합전화": _칸(조합전화), "상사": _칸(상사), "상사전화": _칸(상사전화),
                    "딜러": _칸(딜러), "딜러전화": _칸(딜러전화), "종사원증번호": _칸(증.group(1) if 증 else None)})
    else:
        for k in ["조합", "조합전화", "상사", "상사전화", "딜러", "딜러전화", "종사원증번호"]:
            out[k] = _오류("담당사원 칸 없음")

    def dt칸(이름):
        dt = s.find("dt", string=lambda x: x is not None and x.replace(" ", "").replace(":", "") == 이름)
        return dt.find_next_sibling("dd") if dt else None

    for 키, 이름 in [("추가사항", "추가사항"), ("한줄광고·사고내역", "한줄광고·사고내역"), ("압류건수", "압류건수"),
                   ("저당건수", "저당건수"), ("세금미납정보", "세금미납정보"), ("차량상세설명", "차량정보상세내역")]:
        dd = dt칸(이름)
        if dd is None:
            out[키] = _오류(f"{이름} 칸 없음")
            continue
        t = _글(dd)
        if 키 in ("압류건수", "저당건수"):
            out[키] = _칸(int(t) if t.isdigit() else None)
        else:
            out[키] = _칸(t)

    # 옵션: 그룹별 체크된 것만 (주요옵션·편의/안전·영상/음향·내/외장 …)
    그룹들 = []
    for ul in s.select("div.num01sub ul"):
        if not ul.select("input[type=checkbox]"):
            continue
        h4 = ul.find_previous_sibling("h4")
        그룹 = _글(h4) or ul.get("id", "")
        for li in ul.select("li"):
            inp = li.find("input")
            if inp is not None and inp.has_attr("checked"):
                그룹들.append(f"{그룹}:{_글(li)}")
    if not s.select("div.num01sub ul input[type=checkbox]"):
        out["옵션"] = _오류("옵션 목록 칸 없음")
        out["옵션수"] = _오류("옵션 목록 칸 없음")
    else:
        out["옵션"] = _칸(그룹들, "딜러가 옵션을 하나도 체크 안 함")
        out["옵션수"] = {"값": len(그룹들), "상태": 값 if 그룹들 else 없음, **({} if 그룹들 else {"이유": "옵션 체크 0개"})}

    사진 = set(re.sub(r"_TH(\.\w+)$", r"\1", u) for u in
             re.findall(r"https?://img\.carmanager\.co\.kr/[^\"'\s<>]+?\.(?:jpe?g|png|webp)", 원문, flags=re.I))
    out["사진수"] = {"값": len(사진), "상태": 값 if 사진 else 없음, **({} if 사진 else {"이유": "딜러가 사진을 안 올림"})}

    m = re.search(r"carcheckoutUrl\s*=\s*'([^']*)'", 원문)
    ifr = s.find("iframe", id="carcheckout")
    주소 = (m.group(1) if m and m.group(1) else "") or (ifr.get("src") if ifr else "")
    out["기록부주소"] = _칸(주소, "기록부 주소 없음")
    return out


# ════════════════════════════════════════════════════════════
# 성능기록부 (브라우저로 그려서 ■/□ 로 표시한 글)
# ════════════════════════════════════════════════════════════
기록부_항목_이름 = [
    "발행번호", "차명", "등록번호", "연식", "검사유효기간", "최초등록일", "차대번호", "변속기", "사용연료", "원동기형식",
    "보증유형", "계기상태", "주행량", "기록부주행거리km", "차대번호표기", "배출가스", "튜닝", "특별이력", "용도변경",
    "색상", "주요옵션", "리콜", "사고이력", "단순수리", "외판1랭크", "외판2랭크", "주요골격", "교환판금부위",
    "주요장치이상", "수리필요", "기본품목", "특기사항",
]
_잡음 = {"가격조사ㆍ산정액", "비고", "만원", "원", "가격산정 평가액", "가격산정평가액"}


def _토막(글):
    return [x.strip() for x in (글 or "").split("\n") if x.strip()]


def 기록부_항목(글, 있음=True, 이유없음=None):
    if not 있음:
        return {k: {"값": None, "상태": 없음, "이유": 이유없음 or "성능기록부 내역 없음"} for k in 기록부_항목_이름}
    t = _토막(글)
    if len(t) < 20:
        return {k: _오류("기록부 글 없음") for k in 기록부_항목_이름}
    out = {}

    def 찾기(라벨, 부터=0):
        for i in range(부터, len(t)):
            if t[i] == 라벨 or t[i].startswith(라벨):
                return i
        return -1

    def 다음값(라벨):
        i = 찾기(라벨)
        if i < 0 or i + 1 >= len(t):
            return _오류(f"「{라벨}」 못 찾음")
        v = t[i + 1]
        return _칸(None if v.startswith(("■", "□")) else v)

    def 체크(라벨, 부터=0):
        i = 찾기(라벨, 부터)
        if i < 0:
            return _오류(f"「{라벨}」 못 찾음"), -1
        고른 = []
        본박스 = False
        j = i + 1
        while j < len(t):
            x = t[j]
            if x.startswith(("■", "□")):
                본박스 = True
                if x.startswith("■"):
                    고른.append(x[1:].strip())
            elif x in _잡음:
                pass
            elif 본박스 or j > i + 4:
                break
            j += 1
        if not 본박스:
            return _오류(f"「{라벨}」 다음에 체크 칸 없음"), j
        return _칸(" ".join(고른), "체크 안 됨(미기재)"), j

    m = re.search(r"발행번호\s*제\s*(\S+?)호", 글 or "")
    out["발행번호"] = _칸(m.group(1)) if m else _오류("발행번호 못 찾음")
    out["차명"] = 다음값("1.차명")
    out["등록번호"] = 다음값("2.자동차등록번호")
    out["연식"] = 다음값("3.연식")
    out["검사유효기간"] = 다음값("4.검사유효기간")
    out["최초등록일"] = 다음값("5.최초등록일")
    out["차대번호"] = 다음값("6.차대번호")
    out["변속기"] = 체크("7.변속기")[0]
    out["사용연료"] = 체크("8.사용연료")[0]
    out["원동기형식"] = 다음값("9.원동기형식")
    out["보증유형"] = 체크("10.보증유형")[0]

    계기, _ = 체크("주행거리 및 계기 상태")
    if 계기["상태"] == 값:
        조각 = 계기["값"].split()
        out["계기상태"] = _칸(" ".join(x for x in 조각 if x in ("양호", "불량")))
        out["주행량"] = _칸(" ".join(x for x in 조각 if x in ("많음", "보통", "적음")))
    else:
        out["계기상태"] = out["주행량"] = 계기
    m = re.search(r"현재 주행거리\s*:\s*\[([\d,]+)\s*km\]", 글 or "")
    out["기록부주행거리km"] = _칸(int(m.group(1).replace(",", ""))) if m else _오류("현재 주행거리 못 찾음")
    out["차대번호표기"] = 체크("차대번호 표기")[0]
    i = 찾기("배출가스")
    배출 = [x for x in t[i + 1:i + 6] if x not in _잡음 and not x.startswith(("■", "□"))][:1] if i >= 0 else []
    out["배출가스"] = _칸(배출[0] if 배출 else None) if i >= 0 else _오류("배출가스 못 찾음")
    for 키, 라벨 in [("튜닝", "튜닝"), ("특별이력", "특별이력"), ("용도변경", "용도변경"), ("색상", "색상"),
                   ("주요옵션", "주요옵션"), ("리콜", "리콜대상")]:
        out[키] = 체크(라벨, 찾기("11.사용이력"))[0]
    사고, j = 체크("12.사고이력")
    out["사고이력"] = 사고
    out["단순수리"] = 체크("단순수리", max(0, j - 1))[0]
    k13 = 찾기("13.부위별")
    out["외판1랭크"] = 체크("외판부위 1랭크", k13)[0]
    out["외판2랭크"] = 체크("외판부위 2랭크", k13)[0]
    out["주요골격"] = 체크("주요골격", k13)[0]

    k14 = 찾기("14.교환")
    k15 = 찾기("15.") if 찾기("15.") > 0 else 찾기("15. 주요장치")
    if k14 >= 0:
        끝 = next((n for n in range(k14 + 1, len(t)) if t[n].startswith("가격산정")), len(t))
        부위 = [x[1:].strip() for x in t[k14 + 1:끝] if x.startswith("■")]
        out["교환판금부위"] = _칸(부위, "교환·판금 부위 없음")
    else:
        out["교환판금부위"] = _오류("14.교환·판금 부위 못 찾음")

    k기타 = 찾기("자동차 기타정보")
    if k15 >= 0:
        좋음 = {"양호", "없음", "적정"}
        # 상위 묶음 이름 (그 아래 세부 항목이 이어짐). 「실린더 헤드 / 가스켓」처럼 같은 이름이 오일누유·냉각수누수 양쪽에 있어서 묶음을 같이 적음
        묶음이름 = {"자기진단", "원동기", "변속기", "오일누유", "냉각수누수", "자동변속기(A/T)", "수동변속기(M/T)",
                "동력전달", "조향", "작동상태", "제동", "전기", "고전원전기장치", "기타"}
        이상 = []
        묶음, 항목 = "", ""
        for x in t[k15 + 1:(k기타 if k기타 > k15 else len(t))]:
            if x.startswith("■"):
                v = x[1:].strip()
                if v not in 좋음:
                    이상.append(" > ".join(a for a in (묶음, 항목) if a) + f": {v}")
            elif x.startswith("□") or x in _잡음:
                continue
            elif x in 묶음이름:
                묶음, 항목 = x, ""
            else:
                항목 = x
        out["주요장치이상"] = _칸(이상, "주요장치 이상 없음")
    else:
        out["주요장치이상"] = _오류("15.주요장치 못 찾음")

    if k기타 >= 0:
        끝 = next((n for n in range(k기타 + 1, len(t)) if t[n].startswith("기본품목")), len(t))
        수리 = []
        앞 = ""
        for x in t[k기타 + 1:끝]:
            if x.startswith("■"):
                v = x[1:].strip()
                if v not in ("없음", "양호"):
                    수리.append(f"{앞}: {v}")
            elif not x.startswith("□"):
                앞 = x
        out["수리필요"] = _칸(수리, "수리 필요 표시 없음")
        k품 = 찾기("기본품목", k기타)
        품 = [x[1:].strip() for x in t[k품 + 1:k품 + 9] if x.startswith("■")] if k품 >= 0 else []
        out["기본품목"] = _칸(품, "보유 표시 없음") if k품 >= 0 else _오류("기본품목 못 찾음")
    else:
        out["수리필요"] = out["기본품목"] = _오류("자동차 기타정보 못 찾음")

    k16 = 찾기("16.특기사항")
    if k16 >= 0:
        k점 = 찾기("성능ㆍ상태점검자", k16)
        k가 = 찾기("가격ㆍ조사산정자", k16)
        의견 = " ".join(t[k점 + 1:k가]) if (k점 >= 0 and k가 > k점) else ""
        out["특기사항"] = _칸(의견, "점검자 의견 없음")
    else:
        out["특기사항"] = _오류("16.특기사항 못 찾음")
    return out


def 평평하게(항목, 앞=""):
    """{이름: {값, 상태}} → 표 한 줄용 {이름: 값(목록은 ' | ' 로)}, {이름+'_상태': 상태}"""
    줄 = {}
    for k, v in 항목.items():
        x = v.get("값")
        if isinstance(x, list):
            x = " | ".join(str(a) for a in x)
        줄[앞 + k] = "" if x is None else x
        줄[앞 + k + "_상태"] = v["상태"] + (f"({v['이유']})" if v.get("이유") and v["상태"] != 값 else "")
    return 줄
