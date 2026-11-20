#!/usr/bin/env python3
"""대시보드 미리보기용 가짜 데이터. 실제 수치가 아니다. meta.sample=true 로 표시된다."""
import datetime as dt, json, random
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent))
from collect import org_type, DATA

random.seed(7)
ORGS = ["서울특별시 강남구", "부산광역시 해운대구", "경기도 수원시", "서울특별시교육청", "경기도교육청",
        "대구광역시교육청 남부교육지원청", "한빛고등학교", "새솔중학교", "국립가람대학교", "서울강북경찰서",
        "인천국제공항공사", "한국철도공사", "서울교통공사", "행정안전부", "국세청", "강원특별자치도 춘천시",
        "전라남도 순천시", "충청북도교육청", "제주특별자치도", "대전광역시 유성구", "국민건강보험공단"]
VENDORS = [("주식회사 지슨", 0.6), ("가상경쟁사A", 0.25), ("가상경쟁사B", 0.15)]
FAM = [("불법촬영(Alpha-C)", "상시형 불법촬영 탐지 시스템", 1800000, (4, 40)),
       ("도청(Alpha-I)", "무선도청 상시 탐지 시스템", 15000000, (1, 6)),
       ("무선백도어(Alpha-H)", "무선백도어 해킹 탐지 시스템", 12000000, (1, 8))]
rows, n = [], 0
d, end = dt.date(2023, 1, 1), dt.date.today() - dt.timedelta(days=1)
while d <= end:
    season = 5.0 if d.month == 12 else 2.0 if d.month in (6, 11) else 1.0
    growth = 1 + (d.year - 2023) * 0.45
    if random.random() < 0.035 * season * growth:
        n += 1
        fam, item, price, (lo, hi) = random.choices(FAM, weights=[6, 3, 2])[0]
        vendor = "주식회사 지슨" if fam != "불법촬영(Alpha-C)" else random.choices(
            [v for v, _ in VENDORS], weights=[w for _, w in VENDORS])[0]
        org = random.choice(ORGS)
        qty = random.randint(lo, hi)
        rows.append({"date": d.isoformat(), "req_no": f"S{n:07d}", "seq": "1", "chg": 0,
                     "org": org, "org_type": org_type(org), "region": org.split()[0],
                     "vendor": vendor, "is_target": vendor.endswith("지슨"),
                     "category": item, "item_name": item, "family": fam,
                     "contract_no": "SAMPLE", "qty": qty, "unit_price": price, "amount": qty * price})
    d += dt.timedelta(days=1)
DATA.mkdir(parents=True, exist_ok=True)
(DATA / "deliveries.json").write_text(json.dumps(rows, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
(DATA / "meta.json").write_text(json.dumps({
    "sample": True, "updated_at": dt.datetime.now().isoformat(timespec="minutes"),
    "data_through": end.isoformat(), "rows": len(rows),
    "target_rows": sum(r["is_target"] for r in rows), "company_names": ["지슨"],
    "categories": [], "backfill_incomplete": False, "unmapped_fields": [], "api_calls": 0},
    ensure_ascii=False, indent=2), encoding="utf-8")
print(len(rows), "sample rows")
