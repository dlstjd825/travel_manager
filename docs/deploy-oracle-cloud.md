# Oracle Cloud 배포

이 안내는 현재 SQLite 데이터베이스와 `instance/` 안의 업로드 파일을 그대로 쓰는 구성입니다. Oracle VM의 디스크에 저장되므로 Vercel처럼 함수가 다시 시작될 때 데이터가 초기화되지 않습니다.

## 1. Oracle VM 만들기

Oracle Cloud Free Tier에서 **Always Free Eligible**로 표시된 Ubuntu 이미지와 `VM.Standard.A1.Flex`를 선택합니다. 이 앱에는 우선 1 OCPU와 6 GB 메모리를 할당하면 충분합니다. A1 용량이 없는 리전이면 잠시 뒤 다시 시도하거나 Always Free Eligible로 표시되는 다른 구성을 확인하세요. Always Free 컴퓨트는 리전별 제공 여부와 계정 한도가 적용됩니다. [Oracle Always Free 리소스](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)

인스턴스 생성 시 SSH 공개 키를 등록하고 공인 IPv4 주소를 할당합니다. VCN 보안 목록과 인스턴스 방화벽에는 다음 인바운드 규칙만 둡니다.

- TCP 22 (SSH): 본인 공인 IP만 허용
- TCP 80, 443 (웹): 인터넷에서 허용
- TCP 8000은 외부에 열지 않습니다. Gunicorn은 VM 내부에서만 받습니다.

Oracle 계정 생성 과정에서 전화번호와 결제 카드 확인을 요구할 수 있습니다. 미성년 사용자는 보호자와 함께 계정을 만들고 결제 정보를 처리하세요. Always Free Eligible 리소스만 만들고, 콘솔에서 리소스가 무료 한도에 포함되는지 확인합니다.

Oracle은 7일 동안 CPU·네트워크·메모리 사용량이 모두 낮은 Always Free 인스턴스를 회수할 수 있습니다. 드물게 접속하는 개인 앱은 이 조건에 걸릴 수 있으므로, Oracle VM만을 유일한 데이터 사본으로 사용하지 말고 `/var/lib/trip-manager/`를 주기적으로 별도 위치에 백업하세요. [Oracle 유휴 인스턴스 정책](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)

## 2. 도메인 연결

HTTPS 인증서를 받으려면 도메인 또는 DDNS 주소가 필요합니다. DNS의 A 레코드를 VM 공인 IP에 연결하고, 아래의 `YOUR_DOMAIN`을 실제 주소로 바꿉니다.

## 3. 앱 설치

PowerShell에서 SSH로 접속합니다. 키 파일과 IP를 본인 값으로 바꿉니다.

```powershell
ssh -i "$env:USERPROFILE\.ssh\oracle_key" ubuntu@PUBLIC_IP
```

Ubuntu에서 다음 패키지를 설치합니다.

```bash
sudo apt update
sudo apt install -y git python3-venv python3-pip python3-dev build-essential nginx certbot python3-certbot-nginx
sudo apt install -y ufw
sudo mkdir -p /opt/trip-manager
sudo adduser --system --group --home /var/lib/trip-manager tripmanager
sudo chown ubuntu:ubuntu /opt/trip-manager
```

Oracle VCN 보안 목록에서 먼저 SSH(22)는 본인 IP만, HTTP(80)와 HTTPS(443)는 허용합니다. 그다음 Ubuntu 방화벽을 켭니다. SSH 허용 규칙을 먼저 추가한 뒤 방화벽을 활성화하세요.

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status
```

비공개 GitHub 저장소를 VM에 내려받습니다. `YOUR_GITHUB_REPO`를 저장소 주소로 바꾸세요. 저장소가 비공개면 읽기 전용 배포 키를 쓰고, 토큰을 명령어나 저장소 파일에 직접 적지 않습니다.

```bash
git clone YOUR_GITHUB_REPO /opt/trip-manager
cd /opt/trip-manager
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
sudo mkdir -p /var/lib/trip-manager
sudo chown -R tripmanager:tripmanager /var/lib/trip-manager
```

## 4. 비밀 설정

VM에서 `/opt/trip-manager/.env`를 만들고 다음 항목을 채웁니다. `SECRET_KEY`는 아래 명령으로 생성할 수 있습니다.

```bash
python3 -c 'import secrets; print(secrets.token_hex(32))'
sudo nano /opt/trip-manager/.env
```

```env
APP_ENV=production
SECRET_KEY=생성한_긴_문자열
ALLOW_REGISTRATION=true
INSTANCE_PATH=/var/lib/trip-manager
GOOGLE_MAPS_API_KEY=서버용_키
GOOGLE_MAPS_BROWSER_API_KEY=브라우저용_키
```

Google 지도 키는 가능하면 서버 검색용과 브라우저 지도용으로 분리합니다. 브라우저 키는 웹사이트 HTTP 리퍼러로 제한하고, 서버 키는 필요한 API만 허용합니다. 환경파일 권한을 제한합니다.

```bash
sudo chown ubuntu:tripmanager /opt/trip-manager/.env
sudo chmod 640 /opt/trip-manager/.env
```

`ALLOW_REGISTRATION=true` 상태에서 세 명이 계정을 만든 뒤, 이 값을 `false`로 바꿉니다. 이후 회원가입은 닫히고 로그인만 가능합니다.

## 5. Gunicorn 서비스 시작

```bash
sudo cp /opt/trip-manager/deploy/oracle/trip-manager.service /etc/systemd/system/trip-manager.service
sudo systemctl daemon-reload
sudo systemctl enable --now trip-manager
sudo systemctl status trip-manager
```

서비스 로그 확인:

```bash
sudo journalctl -u trip-manager -n 100 --no-pager
```

## 6. Nginx와 HTTPS

Nginx 설정 파일을 복사하고 도메인을 입력합니다.

```bash
sudo cp /opt/trip-manager/deploy/oracle/nginx-site.conf.example /etc/nginx/sites-available/trip-manager
sudo nano /etc/nginx/sites-available/trip-manager
sudo ln -s /etc/nginx/sites-available/trip-manager /etc/nginx/sites-enabled/trip-manager
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d YOUR_DOMAIN
```

이제 `https://YOUR_DOMAIN`에서 접속합니다. HTTPS가 동작하는지 확인한 뒤 `.env`의 `ALLOW_REGISTRATION=false`로 바꾸고 서비스를 재시작합니다.

```bash
sudo systemctl restart trip-manager
```

## 기존 여행 데이터 옮기기

Git에는 로컬 `instance/` 데이터베이스와 업로드 파일이 포함되지 않습니다. 기존 여행을 옮기려면 PowerShell에서 앱을 종료한 뒤 로컬 `instance/` 폴더를 VM의 임시 위치로 복사합니다.

```powershell
scp -i "$env:USERPROFILE\.ssh\oracle_key" -r .\instance ubuntu@PUBLIC_IP:/tmp/travel-instance
```

VM에서 데이터를 영구 저장 경로로 옮기고 소유자를 맞춥니다.

```bash
sudo mkdir -p /var/lib/trip-manager
sudo cp -a /tmp/travel-instance/. /var/lib/trip-manager/
sudo chown -R tripmanager:tripmanager /var/lib/trip-manager
```

새로 시작해도 된다면 이 단계를 건너뜁니다.

## 코드 업데이트

```bash
cd /opt/trip-manager
sudo systemctl stop trip-manager
sudo -u ubuntu git pull
sudo /opt/trip-manager/.venv/bin/pip install -r /opt/trip-manager/requirements.txt
sudo systemctl start trip-manager
```

## 백업

`/var/lib/trip-manager/` 폴더에는 SQLite DB와 영수증·티켓·장소 사진이 있습니다. 정기적으로 폴더 전체를 다른 저장 위치로 복사하세요. 서버 디스크에만 둔 사본은 VM이나 계정 문제 때 함께 잃을 수 있습니다.
