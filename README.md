# Trip Manager 로컬 실행

## 프로젝트 구조

```text
travel_manager/
├── app.py                 # Flask 앱과 페이지 라우트
├── database/              # SQLite 스키마와 연결 관리
├── services/              # 지도 검색, 거리 계산, 일정 추천
├── templates/             # 페이지별 HTML
├── static/css/            # 공통 CSS와 페이지별 CSS
├── instance/              # 로컬 데이터베이스와 업로드 파일
└── TODO.md                # 다음 개발 작업
```

CSS는 `base.css`와 각 페이지의 `index.css`, `trip.css`, `itinerary.css`, `expenses.css`로 나뉩니다.

## 설치 및 실행

```powershell
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

`.env`에 `GOOGLE_MAPS_API_KEY`를 설정한 다음 실행합니다. `GOOGLE_MAPS_BROWSER_API_KEY`는 선택 항목이며, 비워 두면 같은 키로 브라우저 지도를 표시합니다. Maps Demo Key는 Places API (New)와 Maps JavaScript API를 모두 지원하므로 개인 테스트에서는 하나의 키를 쓸 수 있습니다.

여행 개요의 `Markdown으로 내보내기`를 누르면 일정·숙소·교통편을 Markdown 파일로 내려받습니다. 별도 계정이나 API 키가 필요하지 않습니다.

```powershell
python app.py
```

Maps Demo Key는 청구 정보 없이 시제품을 시험하기 위한 키이며 사용량이 제한되고 운영용으로 설계되지는 않았습니다. 일반 Cloud 프로젝트 키를 쓰는 경우 결제 계정과 두 API 활성화가 필요합니다. 보안을 위해 일반 키는 서버 검색용과 브라우저 지도용으로 분리하고, 브라우저 키에는 로컬 HTTP 리퍼러 제한을 설정하세요. `.env`는 Git에 올리지 마세요.

동적 지도는 월 10,000회까지 무료 사용량이 제공되며, 이후 첫 요금 구간은 1,000회당 미화 $7입니다. 사용량과 요금은 Google Maps Platform 가격표에서 확인하세요.

검색 결과를 선택하면 좌표와 Place ID가 여행에 연결됩니다. 장소 이름과 주소는 개인 메모로 직접 입력합니다. Google Place ID는 저장하며, Google에서 받은 좌표는 저장 후 30일이 지나면 자동으로 지웁니다.

## 여러 국가를 포함한 여행 계획

국가마다 별도 여행을 만들고 병합하는 대신, 전체 기간으로 여행 하나를 만든 뒤 **국가 구간** 메뉴에서 국가/도시와 날짜를 추가합니다. 장소와 숙소를 검색할 때 해당 구간을 선택하면 일정과 구간별 보기에서 함께 관리됩니다. 연속 구간의 날짜가 겹치면 겹치는 날에 국가 간 이동 카드가 자동으로 일정에 들어갑니다. 각 국가/지역 구간 일정은 별도로 확인하고 다시 추천할 수 있습니다.

## Oracle Cloud 배포

SQLite 데이터와 업로드 파일을 유지한 채 배포하려면 [Oracle Cloud 배포 안내](docs/deploy-oracle-cloud.md)를 참고하세요. Vercel 함수처럼 읽기 전용 파일시스템에서 실행하는 플랫폼에는 현재 로컬 SQLite/업로드 저장 방식이 적합하지 않습니다.
