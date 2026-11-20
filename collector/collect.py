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


def call(operation, params):
    """한 페이지 호출. (items, totalCount) 반환."""
    key = os.environ.get("DATA_GO_KR_KEY")
    if not key:
        sys.exit("환경변수 DATA_GO_KR_KEY 가 없습니다.")
    if CALLS["n"] >= CFG["max_calls_per_run"]:
        raise RuntimeError("CALL_BUDGET")
    q = {"serviceKey": key, "type": "json", **params}
    url = f'{CFG["base_url"]}/{operation}?{urllib.parse.urlencode(q)}'
    last = None
    for attempt in range(4):
        CALLS["n"] += 1
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                text = r.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError) as e:
            last = repr(e)
            time.sleep(2 * (attempt + 1))
            continue
        if text.lstrip().startswith("<"):
            m = re.search(r"<returnAuthMsg>(.*?)</returnAuthMsg>", text)
            m2 = re.search(r"<returnReasonCode>(.*?)</returnReasonCode>", text)
            raise RuntimeError(
                f"{operation}: 게이트웨이 오류 {m2.group(1) if m2 else '?'} "
                f"{m.group(1) if m else text[:200]}"
            )
        body = json.loads(text)
        resp = body.get("response", body)
        header = resp.get("header", {})
        code = str(header.get("resultCode", "00"))
        if code in ("01", "02", "05"):  # 일시 오류
            last = header.get("resultMsg")
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
    raise RuntimeError(f"{operation}: 재시도 실패 {last}")


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


def org_type(name):
    n = name or ""
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
        "org_type": org_type(org),
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
def probe():
    found_contracts, found_categories, report = set(), set(), {}
    for op in CFG["operations"]["contract_items"]:
        for name in CFG["company_names"]:
            try:
                items = list(paged(op, {"inqryDiv": 1, "cntrctCorpNm": name}))
            except RuntimeError as e:
                report[op] = f"실패: {e}"
                continue
            report[op] = {"rows": len(items), **field_report(items)}
            for it in items:
                if not is_target(pick(it, "vendor") or name):
                    continue
                if pick(it, "contract_no"):
                    found_contracts.add(str(pick(it, "contract_no")))
                if pick(it, "category"):
                    found_categories.add(pick(it, "category"))
    # 납품요구 상세 응답 필드 확인(최근 7일 아무 데이터)
    end = dt.date.today() - dt.timedelta(days=1)
    op = CFG["operations"]["delivery_detail"]
    try:
        items, total = call(op, {
            "inqryDiv": 1, "inqryBgnDate": ymd(end - dt.timedelta(days=2)),
            "inqryEndDate": ymd(end), "pageNo": 1, "numOfRows": 20})
        report[op] = {"totalCount_3days": total, **field_report(items),
                      "first_row": items[0] if items else None}
    except RuntimeError as e:
        report[op] = f"실패: {e}"
    CFG["contract_numbers"] = sorted(set(CFG["contract_numbers"]) | found_contracts)
    CFG["category_names"] = sorted(set(CFG["category_names"]) | found_categories)
    CFG_PATH.write_text(json.dumps(CFG, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "probe.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("\n계약번호:", CFG["contract_numbers"])
    print("세부품명:", CFG["category_names"])
    print(f"API 호출 {CALLS['n']}회. config.json 을 갱신했습니다.")


# ---------- collect ----------
def collect():
    DATA.mkdir(parents=True, exist_ok=True)
    out = DATA / "deliveries.json"
    state_path = DATA / "state.json"
    rows = {}
    if out.exists():
        for r in json.loads(out.read_text(encoding="utf-8")):
            rows[(r["req_no"], r["seq"], r["contract_no"], r["item_name"])] = r
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    end = dt.date.today() - dt.timedelta(days=1)  # API는 D-1까지 제공
    start0 = dt.date.fromisoformat(CFG["start_date"])
    refresh_from = end - dt.timedelta(days=CFG["refresh_days"])
    op = CFG["operations"]["delivery_detail"]
    queries = [("cntrctNo", c) for c in CFG["contract_numbers"]] + \
              [("dtilPrdctClsfcNoNm", c) for c in CFG["category_names"]]
    if not queries:
        sys.exit("config.json 에 contract_numbers / category_names 가 없습니다. 먼저 probe 를 실행하세요.")
    raw_seen, budget_hit, misses = 0, False, set()
    for pname, pval in queries:
        qkey = f"{pname}={pval}"
        done_until = state.get(qkey)  # 백필이 끝난 마지막 날짜
        start = start0 if not done_until else min(
            dt.date.fromisoformat(done_until) + dt.timedelta(days=1), refresh_from)
        try:
            for a, b in windows(start, end, CFG["window_days"]):
                items = list(paged(op, {
                    "inqryDiv": 1, "inqryBgnDate": ymd(a), "inqryEndDate": ymd(b),
                    pname: pval}))
                raw_seen += len(items)
                if items:
                    misses |= set(field_report(items)["unmapped"])
                for it in items:
                    r = normalize(it)
                    k = (r["req_no"], r["seq"], r["contract_no"], r["item_name"])
                    if k not in rows or r["chg"] >= rows[k]["chg"]:
                        rows[k] = r
                state[qkey] = b.isoformat()
        except RuntimeError as e:
            if str(e) == "CALL_BUDGET":
                budget_hit = True
                break
            raise
    data = sorted((r for r in rows.values() if r["date"]),
                  key=lambda r: (r["date"], r["req_no"], r["seq"]))
    out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    meta = {
        "sample": False,
        "updated_at": dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat(timespec="minutes"),
        "data_through": end.isoformat(),
        "rows": len(data),
        "target_rows": sum(1 for r in data if r["is_target"]),
        "company_names": CFG["company_names"],
        "categories": CFG["category_names"],
        "backfill_incomplete": budget_hit,
        "unmapped_fields": sorted(misses),
        "api_calls": CALLS["n"],
    }
    (DATA / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    print(f"이번 실행에서 받은 원본 행: {raw_seen}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "collect"
    {"probe": probe, "collect": collect}[mode]()
