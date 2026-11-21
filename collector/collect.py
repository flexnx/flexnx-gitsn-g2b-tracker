#!/usr/bin/env python3
"""지슨 나라장터 종합쇼핑몰 납품요구 수집기.

사용법
  python collector/collect.py probe     # 1회: 지슨 계약번호·품명 탐색 + 응답 필드 확인
  python collector/collect.py collect   # 매일: 납품요구 상세 수집 → public/data/*.json

환경변수 DATA_GO_KR_KEY 에 공공데이터포털 일반 인증키(Decoding)를 넣는다.
"""
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG_PATH = ROOT / "config.json"
DATA = ROOT / "public" / "data"
CFG = json.loads(CFG_PATH.read_text(encoding="utf-8"))
CALLS = {"n": 0}

# 응답 필드명은 명세 공개본에 없어 후보를 여러 개 둔다. probe 결과로 확정한다.
FIELDS = {
    "date": ["dlvrReqRcptDate", "dlvrReqDate", "dlvrReqRcptDt"],
    "req_no": ["dlvrReqNo"],
    "seq": ["dlvrReqDtlSeq", "prdctSno", "dlvrReqPrdctSno", "prdctSeq"],
    "chg": ["dlvrReqChgOrd", "dlvrReqChgNo"],
    "org": ["dminsttNm"],
    "org_code": ["dminsttCd"],
    "region": ["dminsttRgnNm", "dminsttRgnCdNm", "dmndInsttRgnNm"],
    "vendor": ["corpNm", "cntrctCorpNm", "corpEntrprsNm"],
    "vendor_bizno": ["cntrctCorpBizno", "corpBizno", "bizno"],
    "category": ["dtilPrdctClsfcNoNm"],
    "class_name": ["prdctClsfcNoNm"],
    "item_name": ["prdctIdntNoNm"],
    "item_no": ["prdctIdntNo"],
    "contract_no": ["cntrctNo", "uCntrctNo"],
    "qty": ["prdctQty", "dlvrReqQty", "qty"],
    "unit_price": ["prdctUprc", "cntrctUprc", "uprc"],
    "amount": ["prdctAmt", "dlvrReqPrdctAmt"],
    "excellent": ["exclcProdctYn", "exclntPrdctYn"],
    "title": ["dlvrReqNm", "reqNm"],
    "org_div": ["dmndInsttDivNm"],
    "final": ["fnlDlvrReqYn"],
}


def pick(item, key):
    for name in FIELDS[key]:
        v = item.get(name)
        if v not in (None, ""):
            return v
    return None


def num(v):
    if v is None:
        return 0.0
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return 0.0


def call(operation, params, timeout=60, tries=1, base=None):
    """한 페이지 호출. (items, totalCount) 반환."""
    key = os.environ.get("DATA_GO_KR_KEY")
    if not key:
        sys.exit("환경변수 DATA_GO_KR_KEY 가 없습니다.")
    if CALLS["n"] >= CFG["max_calls_per_run"]:
        raise RuntimeError("CALL_BUDGET")
    q = {"serviceKey": key, "type": "json", **params}
    root = (base or CFG["base_url"]).replace("https://apis.data.go.kr", "http://apis.data.go.kr")
    url = f'{root}/{operation}?{urllib.parse.urlencode(q)}'
    last = None
    for attempt in range(tries):
        CALLS["n"] += 1
        t0 = time.time()
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                text = r.read().decode("utf-8", "replace")
        except Exception as e:  # 시간 초과, 연결 끊김 등
            last = f"{type(e).__name__}: {str(e)[:80]} ({time.time() - t0:.0f}s)"
            time.sleep(2 * (attempt + 1))
            continue
        CALLS["ok_sec"] = CALLS.get("ok_sec", 0) + (time.time() - t0)
        CALLS["ok"] = CALLS.get("ok", 0) + 1
        if text.lstrip().startswith("<"):
            m = re.search(r"<returnAuthMsg>(.*?)</returnAuthMsg>", text)
            m2 = re.search(r"<returnReasonCode>(.*?)</returnReasonCode>", text)
            raise RuntimeError(
                f"{operation}: 게이트웨이 오류 {m2.group(1) if m2 else '?'} "
                f"{m.group(1) if m else text[:200]}"
            )
        try:
            body = json.loads(text)
        except ValueError:
            raise RuntimeError(f"{operation}: JSON 아님 {text[:120]!r}")
        resp = body.get("response", body)
        header = resp.get("header", {})
        code = str(header.get("resultCode", "00"))
        if code in ("01", "02", "05"):  # 일시 오류
            last = f"resultCode {code} {header.get('resultMsg')}"
            time.sleep(2 * (attempt + 1))
            continue
        if code not in ("00", "0"):
            raise RuntimeError(f"{operation}: {code} {header.get('resultMsg')}")
        b = resp.get("body", {}) or {}
        items = b.get("items") or []
        if isinstance(items, dict):
            items = items.get("item") or []
        if isinstance(items, dict):
            items = [items]
        return items, int(b.get("totalCount") or 0)
    raise RuntimeError(f"{operation}: 실패 {last}")


def paged(operation, params):
    page = 1
    rows = CFG["rows_per_page"]
    while True:
        items, total = call(operation, {**params, "pageNo": page, "numOfRows": rows})
        yield from items
        if page * rows >= total or not items:
            return
        page += 1


def windows(start, end, days):
    cur = start
    while cur <= end:
        nxt = min(cur + dt.timedelta(days=days - 1), end)
        yield cur, nxt
        cur = nxt + dt.timedelta(days=1)


def ymd(d):
    return d.strftime("%Y%m%d")


# ---------- 분류 ----------
ORG_RULES = [
    ("교육기관", r"교육청|교육지원청|학교|대학|유치원|교육원|교육연수원|학원|도서관"),
    ("경찰·사법", r"경찰|검찰|법원|교도소|구치소|법무부|해양경찰"),
    ("지자체", r"특별시|광역시|특별자치|^.{2,4}도$|도청|시청|군청|구청|[가-힣]+(시|군|구)($|\s)|읍|면사무소|주민센터|행정복지센터|시설관리공단|도시공사"),
    ("공공기관", r"공사|공단|진흥원|연구원|재단|병원|의료원|센터|협회|은행|조합"),
    ("중앙부처", r"부$|처$|청$|위원회|국회|대통령|국무|감사원|본부|사령부|군$|부대|사단"),
]


def org_type(name, div=""):
    """기관명에 학교·교육청·경찰이 보이면 그쪽으로, 아니면 API의 수요기관구분을 따른다."""
    n, d = name or "", div or ""
    for label, pat in ORG_RULES[:2]:
        if re.search(pat, n):
            return label
    if "교육" in d:
        return "교육기관"
    if "지방" in d or "지자체" in d:
        return "지자체"
    if "국가" in d or "중앙" in d:
        return "중앙부처"
    if re.search("공기업|준정부|공공|출연|출자", d):
        return "공공기관"
    for label, pat in ORG_RULES:
        if re.search(pat, n):
            return label
    return "기타"


def product_family(text):
    t = text or ""
    if re.search(r"불법\s*촬영|카메라|몰래", t):
        return "불법촬영(Alpha-C)"
    if re.search(r"도청", t):
        return "도청(Alpha-I)"
    if re.search(r"백도어|해킹|무선침입|스파이", t):
        return "무선백도어(Alpha-H)"
    return "기타"


def is_target(vendor):
    v = (vendor or "").replace("(주)", "").replace("주식회사", "").strip()
    return any(v == c or v.startswith(c) for c in CFG["company_names"])


def normalize(item):
    vendor = pick(item, "vendor") or ""
    text = " ".join(
        str(pick(item, k) or "") for k in ("category", "class_name", "item_name")
    )
    qty = num(pick(item, "qty"))
    unit = num(pick(item, "unit_price"))
    amount = num(pick(item, "amount")) or qty * unit
    date = str(pick(item, "date") or "")[:10].replace("-", "")
    date = f"{date[:4]}-{date[4:6]}-{date[6:8]}" if len(date) >= 8 else ""
    org = pick(item, "org") or ""
    return {
        "date": date,
        "req_no": str(pick(item, "req_no") or ""),
        "seq": str(pick(item, "seq") or "1"),
        "chg": int(num(pick(item, "chg"))),
        "org": org,
        "org_type": org_type(org, pick(item, "org_div")),
        "org_div": pick(item, "org_div") or "",
        "title": pick(item, "title") or "",
        "region": pick(item, "region") or "",
        "vendor": vendor,
        "is_target": is_target(vendor),
        "category": pick(item, "category") or pick(item, "class_name") or "",
        "item_name": pick(item, "item_name") or "",
        "family": product_family(text),
        "contract_no": str(pick(item, "contract_no") or ""),
        "qty": qty,
        "unit_price": unit,
        "amount": amount,
    }


def field_report(items):
    """후보 필드 중 실제로 잡힌 것 / 못 잡은 것."""
    if not items:
        return {"sample_keys": [], "unmapped": list(FIELDS)}
    keys = sorted({k for it in items[:50] for k in it})
    unmapped = [k for k in FIELDS if all(pick(it, k) is None for it in items[:50])]
    return {"sample_keys": keys, "unmapped": unmapped}


# ---------- probe ----------
def _summ(items, total):
    mine = [it for it in items if is_target(pick(it, "vendor"))]
    combos = {}
    for it in items:
        k = (pick(it, "vendor"), pick(it, "class_name"), pick(it, "category"))
        combos[k] = combos.get(k, 0) + 1
    top = sorted(combos.items(), key=lambda x: -x[1])[:15]
    return {"totalCount": total, "fetched": len(items), "target_rows": len(mine),
            "top": [{"vendor": k[0], "class": k[1], "category": k[2], "n": n} for k, n in top],
            "target_samples": [
                {x: it.get(x) for x in ("corpNm", "prdctClsfcNoNm", "dtilPrdctClsfcNoNm",
                                         "prdctIdntNoNm", "cntrctNo", "prdctUprc", "dminsttNm",
                                         "dmndInsttDivNm", "dlvrReqRcptDate")}
                for it in mine[:8]]}


def probe():
    """어떤 조회 조건으로 지슨 납품요구를 뽑을 수 있는지 시험한다."""
    op = CFG["operations"]["delivery_detail"]
    end = dt.date.today() - dt.timedelta(days=1)
    wins = {"last30": (end - dt.timedelta(days=29), end),
            "dec2025": (dt.date(2025, 12, 1), dt.date(2025, 12, 31))}
    tests = [("prdctIdntNoNm", v) for v in ("지슨", "Alpha-C", "Alpha", "알파")] + \
            [("prdctClsfcNoNm", v) for v in ("불법촬영", "도청", "탐지")] + \
            [("dtilPrdctClsfcNoNm", v) for v in ("불법촬영", "도청", "탐지")]
    report, contracts, cats, keywords = {"filters": {}, "contract_ops": {}}, set(), set(), set()
    for pname, pval in tests:
        for wname, (a, b) in wins.items():
            key = f"{pname}={pval}|{wname}"
            try:
                items, total = call(op, {
                    "inqryDiv": 1, "inqryBgnDate": ymd(a), "inqryEndDate": ymd(b),
                    pname: pval, "pageNo": 1, "numOfRows": 300}, timeout=60, tries=1)
            except RuntimeError as e:
                report["filters"][key] = f"실패: {e}"
                continue
            report["filters"][key] = _summ(items, total)
            for it in items:
                if is_target(pick(it, "vendor")):
                    contracts.add(str(pick(it, "contract_no")))
                    cats.add(pick(it, "category"))
                    if pname == "prdctIdntNoNm" and total < 5000:
                        keywords.add(pval)
    # 페이지 크기 상한
    try:
        items, total = call(op, {"inqryDiv": 1, "inqryBgnDate": ymd(end), "inqryEndDate": ymd(end),
                                 "pageNo": 1, "numOfRows": 999}, timeout=60, tries=1)
        report["page_size_test"] = {"asked": 999, "got": len(items), "totalCount_1day": total}
    except RuntimeError as e:
        report["page_size_test"] = f"실패: {e}"
    # 계약 품목 조회(업체명) — 날짜 조건 후보를 바꿔가며 시험
    date_variants = [{}, {"rgstDtBgnDt": "202001010000", "rgstDtEndDt": ymd(end) + "2359"},
                     {"chgDtBgnDt": "202001010000", "chgDtEndDt": ymd(end) + "2359"}]
    for cop in CFG["operations"]["contract_items"]:
        for name in ("지슨",):
            for i, dv in enumerate(date_variants):
                key = f"{cop}|{name}|v{i}"
                try:
                    items, total = call(cop, {"inqryDiv": 2 if i == 2 else 1, "cntrctCorpNm": name,
                                              "pageNo": 1, "numOfRows": 100, **dv},
                                        timeout=60, tries=1)
                except RuntimeError as e:
                    report["contract_ops"][key] = f"실패: {str(e)[:160]}"
                    continue
                report["contract_ops"][key] = {
                    "totalCount": total, "keys": sorted({k for it in items[:20] for k in it}),
                    "first": items[0] if items else None}
                for it in items:
                    if pick(it, "contract_no"):
                        contracts.add(str(pick(it, "contract_no")))
                    if pick(it, "category"):
                        cats.add(pick(it, "category"))
    contracts.discard("None"); cats.discard(None)
    CFG["contract_numbers"] = sorted(set(CFG["contract_numbers"]) | contracts)
    CFG["category_names"] = sorted(set(CFG["category_names"]) | cats)
    CFG["item_keywords"] = sorted(set(CFG.get("item_keywords", [])) | keywords)
    report["found"] = {"contract_numbers": CFG["contract_numbers"],
                       "category_names": CFG["category_names"],
                       "item_keywords": CFG["item_keywords"], "api_calls": CALLS["n"]}
    CFG_PATH.write_text(json.dumps(CFG, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "probe.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["found"], ensure_ascii=False, indent=2))


# ---------- collect ----------
def month_windows(start, end):
    """달력 월 단위 창. 최신 달부터."""
    out, y, m = [], end.year, end.month
    while (y, m) >= (start.year, start.month):
        a = dt.date(y, m, 1)
        nxt = dt.date(y + (m == 12), m % 12 + 1, 1)
        out.append((max(a, start), min(nxt - dt.timedelta(days=1), end)))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out


def collect():
    t_start = time.time()
    max_minutes = CFG.get("max_minutes", 40)
    rows_per_page = 300
    DATA.mkdir(parents=True, exist_ok=True)
    out = DATA / "deliveries.json"
    state_path = DATA / "state.json"
    rows = {}
    if out.exists():
        for r in json.loads(out.read_text(encoding="utf-8")):
            if r.get("contract_no") == "SAMPLE":
                continue
            rows[(r["req_no"], r["seq"], r["contract_no"], r["item_name"])] = r
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    if state.get("_v") != 2:
        state = {"_v": 2, "done": {}}
    end = dt.date.today() - dt.timedelta(days=1)  # API는 D-1까지 제공
    start0 = dt.date.fromisoformat(CFG["start_date"])
    refresh_from = end - dt.timedelta(days=CFG["refresh_days"])
    op = CFG["operations"]["delivery_detail"]
    # 규격명 키워드(지슨) 먼저, 그다음 품명(경쟁사 포함)
    queries = [("prdctIdntNoNm", c) for c in CFG.get("item_keywords", [])] + \
              [("prdctClsfcNoNm", c) for c in CFG["category_names"]]
    if not queries:
        sys.exit("config.json 에 조회 조건이 없습니다. 먼저 probe 를 실행하세요.")
    stats = {"raw": 0, "failed": 0, "ok": 0, "stopped": "", "misses": set(), "errors": []}

    def fetch(pname, pval, a, b):
        page = 1
        while True:
            items, total = call(op, {"inqryDiv": 1, "inqryBgnDate": ymd(a), "inqryEndDate": ymd(b),
                                     pname: pval, "pageNo": page, "numOfRows": rows_per_page})
            stats["raw"] += len(items)
            if items:
                stats["misses"] |= set(field_report(items)["unmapped"])
            for it in items:
                r = normalize(it)
                k = (r["req_no"], r["seq"], r["contract_no"], r["item_name"])
                if k not in rows or r["chg"] >= rows[k]["chg"]:
                    rows[k] = r
            if page * rows_per_page >= total or not items:
                return
            page += 1

    wins = month_windows(start0, end)
    todo = []
    for a, b in wins:  # 최신 달부터, 달마다 모든 조회 조건
        for pname, pval in queries:
            qkey = f"{pname}={pval}"
            wkey = a.isoformat()[:7]
            if wkey in state["done"].get(qkey, []) and b < refresh_from:
                continue
            todo.append((pname, pval, a, b, qkey, wkey))

    def save():
        data = sorted((r for r in rows.values() if r["date"]),
                      key=lambda r: (r["date"], r["req_no"], r["seq"]))
        out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        total_w = len(wins) * len(queries)
        done_w = sum(len(v) for v in state["done"].values())
        meta = {
            "sample": False,
            "updated_at": dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat(timespec="minutes"),
            "data_through": end.isoformat(),
            "rows": len(data),
            "target_rows": sum(1 for r in data if r["is_target"]),
            "earliest_date": data[0]["date"] if data else None,
            "company_names": CFG["company_names"],
            "categories": CFG["category_names"],
            "backfill_incomplete": done_w < total_w,
            "windows_done": done_w, "windows_total": total_w,
            "this_run": {"ok": stats["ok"], "failed": stats["failed"], "stopped": stats["stopped"],
                         "minutes": round((time.time() - t_start) / 60, 1),
                         "avg_ok_sec": round(CALLS.get("ok_sec", 0) / max(CALLS.get("ok", 0), 1), 1)},
            "errors": stats["errors"][:25],
            "unmapped_fields": sorted(stats["misses"]),
            "api_calls": CALLS["n"],
        }
        (DATA / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return meta

    streak = 0
    for i, (pname, pval, a, b, qkey, wkey) in enumerate(todo):
        if (time.time() - t_start) / 60 > max_minutes:
            stats["stopped"] = f"시간 한도 {max_minutes}분"
            break
        if streak >= 12:
            stats["stopped"] = "연속 12회 실패 — API 응답 없음"
            break
        try:
            fetch(pname, pval, a, b)
        except RuntimeError as e:
            if str(e) == "CALL_BUDGET":
                stats["stopped"] = "호출 한도"
                break
            stats["failed"] += 1
            streak += 1
            stats["errors"].append(f"{qkey} {wkey}: {str(e)[:140]}")
            print(f"건너뜀 {qkey} {wkey}: {str(e)[:140]}", flush=True)
            continue
        streak = 0
        stats["ok"] += 1
        done = state["done"].setdefault(qkey, [])
        if wkey not in done:
            done.append(wkey)
        if stats["ok"] % 8 == 0:
            save()
            print(f"진행 {i + 1}/{len(todo)} · 행 {len(rows)} · 호출 {CALLS['n']}", flush=True)
    meta = save()
    print(json.dumps(meta, ensure_ascii=False, indent=2))


# ---------- 입찰공고·사전규격·계약·발주계획 ----------
G2B = "http://apis.data.go.kr/1230000"
KEYWORDS = ["불법촬영", "불법카메라", "몰래카메라", "무선도청", "도청탐지", "도청방지",
            "전파탐지", "부정행위", "백도어"]
# kw: 키워드 조회 조건 후보(앞에서부터 시험), dates: 날짜 조건 형식
FEEDS = [
    {"id": "bid_thng", "kind": "입찰공고", "sub": "물품", "base": G2B + "/ad/BidPublicInfoService",
     "op": "getBidPblancListInfoThngPPSSrch", "kw": ["bidNtceNm"], "dates": "dt"},
    {"id": "bid_servc", "kind": "입찰공고", "sub": "용역", "base": G2B + "/ad/BidPublicInfoService",
     "op": "getBidPblancListInfoServcPPSSrch", "kw": ["bidNtceNm"], "dates": "dt"},
    {"id": "spec_thng", "kind": "사전규격", "sub": "물품", "base": G2B + "/ao/HrcspSsstndrdInfoService",
     "op": "getPublicPrcureThngInfoThngPPSSrch", "kw": ["prdctClsfcNoNm", "bizNm", "prcureNm"], "dates": "dt"},
    {"id": "spec_servc", "kind": "사전규격", "sub": "용역", "base": G2B + "/ao/HrcspSsstndrdInfoService",
     "op": "getPublicPrcureThngInfoServcPPSSrch", "kw": ["prdctClsfcNoNm", "bizNm", "prcureNm"], "dates": "dt"},
    {"id": "cntrct_thng", "kind": "계약", "sub": "물품", "base": G2B + "/ao/CntrctInfoService",
     "op": "getCntrctInfoListThngPPSSrch", "kw": ["cntrctNm", "prdctClsfcNoNm"], "dates": "date"},
    {"id": "plan_thng", "kind": "발주계획", "sub": "물품", "base": G2B + "/ao/OrderPlanSttusService",
     "op": "getOrderPlanSttusListThngPPSSrch", "kw": ["bizNm", "prdctClsfcNoNm"], "dates": "ym"},
]
BF = {
    "id": ["bidNtceNo", "bfSpecRgstNo", "untyCntrctNo", "dcsnCntrctNo", "cntrctNo", "orderPlanUntyNo", "orderPlanSno"],
    "ord": ["bidNtceOrd", "cntrctChgOrd"],
    "title": ["bidNtceNm", "cntrctNm", "bizNm", "prdctClsfcNoNm", "prcureNm"],
    "date": ["bidNtceDt", "rcptDt", "rgstDt", "cntrctCnclsDate", "cntrctDate", "nticeDt", "orderYear"],
    "org": ["dminsttNm", "rlDminsttNm", "dmndInsttNm", "orderInsttNm", "cntrctInsttNm"],
    "notice_org": ["ntceInsttNm", "orderInsttNm", "cntrctInsttNm"],
    "org_div": ["dmndInsttDivNm", "cntrctInsttJrsdctnDivNm", "jrsdctnDivNm"],
    "amount": ["presmptPrce", "asignBdgtAmt", "totCntrctAmt", "thtmCntrctAmt", "cntrctAmt", "sumOrderAmt", "orderContrctAmt"],
    "deadline": ["bidClseDt", "opninRgstClseDt"],
    "method": ["cntrctCnclsMthdNm", "cntrctMthdNm", "bidMethdNm"],
    "url": ["bidNtceDtlUrl", "bidNtceUrl", "cntrctDtlInfoUrl", "cntrctInfoUrl", "bfSpecDtlUrl", "specDocFileUrl1"],
    "vendor": ["corpList", "cntrctCorpNm", "corpNm", "rprsntCorpNm"],
    "dminstt_list": ["dminsttList"],
    "status": ["ntceKindNm", "bidNtceSttusNm"],
}


def bpick(item, key):
    for name in BF[key]:
        v = item.get(name)
        if v not in (None, ""):
            return v
    return None


def edu_tier(org):
    """발주 주체 구분. '○○교육청 △△고등학교'처럼 학교명이 붙으면 개별 학교로 본다."""
    n = org or ""
    if re.search(r"대학", n):
        return "대학"
    if re.search(r"(초등|중|고등|특수|여자고등|공업고등|상업고등)학교|유치원|학교$", n):
        return "개별 학교"
    if re.search(r"교육지원청|교육청", n):
        return "시도교육청·지원청"
    if re.search(r"^교육부|교육과정평가원|국립특수교육원|국립국제교육원|교육학술정보원", n):
        return "교육부·소속기관"
    if re.search(r"인사혁신처|산업인력공단|국가고시|시험원|평가원", n):
        return "시험 주관기관"
    return org_type(n)


def bnorm(feed, kw, item):
    org = str(bpick(item, "org") or "")
    dl = str(bpick(item, "dminstt_list") or "")
    if not org and dl:  # 계약: [순번^기관코드^기관명^...] 형식
        parts = dl.strip("[]").split("^")
        org = parts[2] if len(parts) > 2 else dl[:40]
    vendor = str(bpick(item, "vendor") or "")
    if vendor.startswith("["):
        vp = vendor.strip("[]").split("^")
        vendor = vp[3] if len(vp) > 3 else vendor[:60]
    date = re.sub(r"[^0-9]", "", str(bpick(item, "date") or ""))[:8]
    date = f"{date[:4]}-{date[4:6]}-{date[6:8]}" if len(date) >= 8 else (date[:4] if date else "")
    return {
        "feed": feed["id"], "kind": feed["kind"], "sub": feed["sub"], "keyword": kw,
        "id": f'{bpick(item, "id") or ""}-{bpick(item, "ord") or ""}'.strip("-"),
        "title": str(bpick(item, "title") or ""), "date": date,
        "org": org, "notice_org": str(bpick(item, "notice_org") or ""),
        "tier": edu_tier(org or str(bpick(item, "notice_org") or "")),
        "amount": num(bpick(item, "amount")), "deadline": str(bpick(item, "deadline") or ""),
        "method": str(bpick(item, "method") or ""), "status": str(bpick(item, "status") or ""),
        "vendor": vendor, "is_target": any(c in vendor for c in CFG["company_names"]),
        "url": str(bpick(item, "url") or ""),
        "bid_no": str(item.get("bidNtceNoList") or ""),
    }


def bids(max_minutes=20, months=6):
    t_start = time.time()
    DATA.mkdir(parents=True, exist_ok=True)
    out, sp = DATA / "bids.json", DATA / "bids_state.json"
    recs = {(r["feed"], r["id"], r["title"]): r for r in json.loads(out.read_text(encoding="utf-8"))} if out.exists() else {}
    state = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else {}
    state.setdefault("done", {}); state.setdefault("kw_param", {}); state.setdefault("dead", {})
    if state.get("_v") != 2:  # 조회 조건 재시험(계약·발주계획)
        for f in ("cntrct_thng", "plan_thng"):
            state["kw_param"].pop(f, None)
            state["done"] = {k: v for k, v in state["done"].items() if not k.startswith(f + "|")}
        state["_v"] = 2
    end = dt.date.today()
    start = dt.date(end.year - (1 if months >= 12 else 0), end.month, 1) if months >= 12 else end - dt.timedelta(days=31 * months)
    wins = month_windows(start, end)
    info = {"feeds": {}, "errors": [], "stopped": ""}

    VARIANTS = {"dt": ["dt"], "date": ["date", "dt", "date2", "both"], "ym": ["ym", "ymdt", "dt"]}
    state.setdefault("variant", {})

    def dparams(feed, a, b, v=None):
        v = v or state["variant"].get(feed["id"]) or VARIANTS[feed["dates"]][0]
        dtp = {"inqryBgnDt": ymd(a) + "0000", "inqryEndDt": ymd(b) + "2359"}
        dp = {"inqryBgnDate": ymd(a), "inqryEndDate": ymd(b)}
        if v == "dt":
            return {"inqryDiv": 1, **dtp}
        if v == "date":
            return {"inqryDiv": 1, **dp}
        if v == "date2":
            return {"inqryDiv": 2, **dp}
        if v == "both":
            return {"inqryDiv": 1, **dp, **dtp}
        if v == "ym":
            return {"orderBgnYm": a.strftime("%Y%m"), "orderEndYm": b.strftime("%Y%m")}
        return {"orderBgnYm": a.strftime("%Y%m"), "orderEndYm": b.strftime("%Y%m"), **dtp}

    def fetch(feed, kwp, kw, a, b):
        page, got = 1, []
        while True:
            items, total = call(feed["op"], {**dparams(feed, a, b), kwp: kw, "pageNo": page, "numOfRows": 100},
                                base=feed["base"])
            got += items
            if page * 100 >= total or not items or page >= 5:
                return got, total
            page += 1

    for feed in FEEDS:
        fi = info["feeds"].setdefault(feed["id"], {"ok": 0, "failed": 0, "rows": 0, "kw_param": None, "keys": []})
        if state["dead"].get(feed["id"]) == end.isoformat():
            fi["note"] = "오늘은 건너뜀(앞선 실행에서 응답 없음)"
            continue
        streak = 0
        kwp = state["kw_param"].get(feed["id"])
        # 조회 조건 후보 시험: 키워드를 넣었을 때 결과 수가 넣지 않았을 때보다 줄어야 유효
        if not kwp:
            a, b = wins[1] if len(wins) > 1 else wins[0]  # 지난달(자료 지연 영향 없음)
            try:
                base_total, tried = 0, {}
                for v in VARIANTS[feed["dates"]]:
                    try:
                        _, t = call(feed["op"], {**dparams(feed, a, b, v), "pageNo": 1, "numOfRows": 1}, base=feed["base"])
                    except RuntimeError as e:
                        if "429" in str(e) or "403" in str(e):
                            raise
                        tried[v] = str(e)[:80]
                        continue
                    tried[v] = t
                    if t > 0:
                        base_total = t
                        state["variant"][feed["id"]] = v
                        break
                fi["variant_totals"] = tried
                if base_total == 0:
                    info["errors"].append(f'{feed["id"]}: 날짜 조건으로 조회되는 자료가 없음 {tried}')
                    continue
                for cand in feed["kw"]:
                    _, t = call(feed["op"], {**dparams(feed, a, b), cand: "불법촬영", "pageNo": 1, "numOfRows": 1},
                                base=feed["base"])
                    if t < base_total:
                        kwp = cand
                        break
                fi["base_total"] = base_total
            except RuntimeError as e:
                info["errors"].append(f'{feed["id"]} 시험: {str(e)[:160]}')
                fi["failed"] += 1
                state["dead"][feed["id"]] = end.isoformat() if "429" in str(e) else ""
                continue
            if not kwp:
                info["errors"].append(f'{feed["id"]}: 키워드 조회 조건을 찾지 못함 {feed["kw"]}')
                continue
            state["kw_param"][feed["id"]] = kwp
        fi["kw_param"] = kwp
        for a, b in wins:
            for kw in KEYWORDS:
                wkey = f'{feed["id"]}|{kw}|{a.isoformat()[:7]}'
                if wkey in state["done"] and b < end - dt.timedelta(days=45):
                    continue
                if (time.time() - t_start) / 60 > max_minutes:
                    info["stopped"] = f"시간 한도 {max_minutes}분"
                    break
                if streak >= 6:
                    break
                try:
                    items, total = fetch(feed, kwp, kw, a, b)
                except RuntimeError as e:
                    if str(e) == "CALL_BUDGET":
                        info["stopped"] = "호출 한도"
                        break
                    streak += 1
                    fi["failed"] += 1
                    info["errors"].append(f"{wkey}: {str(e)[:140]}")
                    continue
                streak = 0
                fi["ok"] += 1
                fi["rows"] += len(items)
                if items and not fi["keys"]:
                    fi["keys"] = sorted(items[0].keys())
                    fi["first"] = items[0]
                for it in items:
                    r = bnorm(feed, kw, it)
                    recs[(r["feed"], r["id"], r["title"])] = r
                state["done"][wkey] = 1
            if info["stopped"] or streak >= 6:
                break
        if streak >= 6:
            state["dead"][feed["id"]] = end.isoformat()
            fi["note"] = "연속 6회 실패로 중단"
        if info["stopped"]:
            break
    for r in recs.values():
        r["tier"] = edu_tier(r["org"] or r["notice_org"])
    data = sorted(recs.values(), key=lambda r: (r["date"], r["id"]), reverse=True)
    out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    sp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    info.update(rows=len(data), errors=info["errors"][:30], api_calls=CALLS["n"],
                minutes=round((time.time() - t_start) / 60, 1), keywords=KEYWORDS,
                updated_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat(timespec="minutes"))
    (DATA / "bids_meta.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in info.items() if k != "feeds"}, ensure_ascii=False, indent=1))
    for k, v in info["feeds"].items():
        print(k, {x: v[x] for x in v if x not in ("keys", "first")})


def run_all():
    try:
        collect()
    except SystemExit as e:
        print(e)
    CALLS["n"] = 0
    bids()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "collect"
    {"probe": probe, "collect": run_all, "bids": bids, "orders": collect}[mode]()
