# 지슨 공공조달 트래커

조달청 나라장터쇼핑몰 납품요구 상세 데이터를 매일 모아 지슨(GITSN)의 공공 부문 주문을 추적한다.
GitHub Actions가 수집해 `public/data/*.json` 으로 커밋하고, Cloudflare Pages가 `public/` 을 그대로 서빙한다.

## 1. 인증키 발급 (5분, 무료)

1. https://www.data.go.kr 회원가입·로그인
2. 검색창에 `나라장터쇼핑몰 품목정보 서비스` → 오픈API 탭 → **조달청_나라장터쇼핑몰 품목정보 서비스**
   (https://www.data.go.kr/data/15129471/openapi.do)
3. **활용신청** 클릭 → 활용목적 "연구(개인)" 등으로 작성 → 신청. 개발계정은 자동승인.
4. 마이페이지 → 데이터 활용 → Open API → 활용신청 현황 → 해당 API 클릭
   → **일반 인증키(Decoding)** 복사

개발계정 트래픽은 하루 1,000건. 수집기는 한 번에 800건까지만 쓰고, 백필이 남으면 다음 날 이어서 한다.

## 2. 배포

1. 이 폴더를 GitHub 저장소(예: `flexnx/gitsn-g2b-tracker`)로 push
2. 저장소 Settings → Secrets and variables → Actions → `DATA_GO_KR_KEY` 에 인증키 등록
3. Actions → collect → Run workflow → mode 에 `probe` 입력 후 실행
   - 지슨의 계약번호·세부품명을 찾아 `config.json` 에 채우고, 응답 필드명을 `public/data/probe.json` 에 남긴다.
4. 다시 Run workflow → mode `collect` (이후 매일 07:17 KST 자동 실행)
5. Cloudflare Pages → 저장소 연결 → Build command 없음, Output directory `public`

로컬 실행:

```
export DATA_GO_KR_KEY=...
python collector/collect.py probe
python collector/collect.py collect
cd public && python -m http.server 8000
```

## 3. probe 후 확인할 것

이 코드는 실제 API에 붙여 보지 못한 상태다. 아래는 첫 실행에서 확인이 필요하다.

- **오퍼레이션 이름**: `config.json` 의 `operations`. 포털 명세에는 한글 이름만 나와 있어 영문 경로는 관례대로 넣었다.
  probe 결과에 `NO_OPENAPI_SERVICE_ERROR` 가 뜨면 포털의 활용명세에서 실제 경로를 확인해 고친다.
- **응답 필드명**: `collect.py` 의 `FIELDS` 후보. `probe.json` 의 `unmapped` 에 `date/org/vendor/qty/unit_price` 가 있으면
  `sample_keys` 를 보고 후보를 추가한다. 대시보드 상단에도 경고가 뜬다.
- **세부품명**: `category_names` 에 채워진 품명으로 경쟁사까지 수집한다. 품명이 너무 넓으면(예: 일반 "보안장비") 무관한 업체가 섞이므로 조정한다.
- **수요기관 분류**: `ORG_RULES` 정규식. 기관명 규칙 기반이라 오분류가 있을 수 있다.

## 4. 한계

- 납품요구 접수일 기준. 매출 인식(설치·검수)과 시차가 있다.
- 종합쇼핑몰 밖 계약, 학교장터(S2B) 소액구매, 민간(B2B), 수출, 비공개 계약은 안 잡힌다.
- `collector/make_sample.py` 는 화면 확인용 가짜 데이터 생성기다. 실제 수집이 한 번 돌면 덮어써진다.
