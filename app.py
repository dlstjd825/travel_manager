from datetime import date, datetime, time as datetime_time, timedelta, timezone
import os
import sqlite3
import uuid
import math
import re
import hmac
import secrets
import time
from pathlib import Path

_login_window_seconds = 600
_login_max_attempts = 8

from dotenv import load_dotenv
from flask import Flask, abort, flash, g, redirect, render_template, request, send_from_directory, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix

from database.db import get_db, init_app, query_db
from services.itinerary import optimize_day_order, recommend_itinerary
from services.place_search import PlaceSearchError, search_places


def create_app(test_config=None):
    load_dotenv()
    app_environment = os.environ.get("APP_ENV", "development").strip().lower()
    secret_key = os.environ.get("SECRET_KEY")
    if app_environment == "production" and not secret_key:
        raise RuntimeError("SECRET_KEY must be set when APP_ENV=production")

    app = Flask(
        __name__,
        instance_relative_config=True,
        instance_path=os.environ.get("INSTANCE_PATH") or None,
    )
    app.config.from_mapping(
        SECRET_KEY=secret_key or os.urandom(32),
        ALLOW_REGISTRATION=os.environ.get("ALLOW_REGISTRATION", "true").strip().lower()
        in {"1", "true", "yes", "on"},
        DATABASE=app.instance_path + "/travel.db",
        MAX_CONTENT_LENGTH=32 * 1024 * 1024,
        EXPENSE_UPLOAD_FOLDER=os.path.join(app.instance_path, "expense_uploads"),
        TRANSPORT_UPLOAD_FOLDER=os.path.join(app.instance_path, "transport_tickets"),
        PLACE_PHOTO_UPLOAD_FOLDER=os.path.join(app.instance_path, "place_photos"),
        GOOGLE_MAPS_API_KEY=os.environ.get("GOOGLE_MAPS_API_KEY", ""),
        GOOGLE_MAPS_BROWSER_API_KEY=(
            os.environ.get("GOOGLE_MAPS_BROWSER_API_KEY")
            or os.environ.get("GOOGLE_MAPS_API_KEY", "")
        ),
    )
    if test_config:
        app.config.update(test_config)
    # The production Nginx proxy is the only trusted proxy in front of Gunicorn.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=app_environment == "production",
    )

    init_app(app)
    os.makedirs(app.config["EXPENSE_UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["TRANSPORT_UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["PLACE_PHOTO_UPLOAD_FOLDER"], exist_ok=True)


    @app.before_request
    def require_authenticated_user():
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            submitted_token = request.form.get("csrf_token") or request.headers.get("X-CSRFToken", "")
            session_token = session.get("csrf_token", "")
            if not session_token or not hmac.compare_digest(submitted_token, session_token):
                abort(400, description="요청 보안 토큰이 없거나 만료되었습니다. 페이지를 새로고침해 주세요.")
        if request.endpoint in {"login", "register", "static"}:
            return None
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.headers.get("Origin"):
            from urllib.parse import urlsplit
            if urlsplit(request.headers["Origin"]).netloc != request.host:
                abort(403)
        user_id = session.get("user_id")
        if not user_id:
            return redirect(url_for("login"))
        trip_id = (request.view_args or {}).get("trip_id")
        if trip_id is not None:
            membership = query_db(
                "SELECT role FROM trip_members WHERE trip_id = ? AND user_id = ?",
                (trip_id, user_id), one=True,
            )
            if membership is None:
                abort(404)
            g.trip_role = membership["role"]
            if request.method == "POST":
                submitted_revision = request.form.get("expected_revision")
                if not submitted_revision and request.is_json:
                    submitted_revision = (request.get_json(silent=True) or {}).get("expected_revision")
                if submitted_revision:
                    db = get_db()
                    # Hold a SQLite write lock from revision comparison through the route's commit.
                    # A concurrent writer then checks the revision only after this request commits.
                    db.execute("BEGIN IMMEDIATE")
                    current = query_db("SELECT revision FROM trip_revisions WHERE trip_id = ?", (trip_id,), one=True)
                    if current and str(current["revision"]) != str(submitted_revision):
                        db.rollback()
                        if request.is_json:
                            return {"error": "다른 사용자가 먼저 수정했습니다. 새로고침 후 변경 내용을 확인해 주세요."}, 409
                        abort(409, description="다른 사용자가 먼저 수정했습니다. 새로고침 후 다시 시도해 주세요.")
        return None

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(self)")
        trip_id = (request.view_args or {}).get("trip_id")
        if trip_id is not None:
            revision = query_db(
                "SELECT revision FROM trip_revisions WHERE trip_id = ?", (trip_id,), one=True
            )
            if revision:
                response.headers["X-Trip-Revision"] = str(revision["revision"])
        if app_environment == "production":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    @app.context_processor
    def inject_csrf_token():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return {"csrf_token": session["csrf_token"]}

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if not app.config["ALLOW_REGISTRATION"]:
            flash("현재 회원가입이 닫혀 있습니다. 여행 관리자에게 계정 생성을 요청해 주세요.", "error")
            return redirect(url_for("login"))
        if session.get("user_id"):
            return redirect(url_for("index"))
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            if not re.fullmatch(r"[A-Za-z0-9가-힣_-]{3,24}", username):
                flash("아이디는 한글, 영문, 숫자, _, - 조합으로 3~24자 입력해 주세요.", "error")
            elif len(password) < 10:
                flash("비밀번호는 10자 이상으로 입력해 주세요.", "error")
            else:
                db = get_db()
                try:
                    db.execute("BEGIN")
                    existing_users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                    user_columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
                    if "email" in user_columns:
                        cursor = db.execute(
                            "INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)",
                            (username, f"{username}@local.invalid", generate_password_hash(password)),
                        )
                    else:
                        cursor = db.execute(
                            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                            (username, generate_password_hash(password)),
                        )
                    user_id = cursor.lastrowid
                    if existing_users == 0:
                        db.execute("UPDATE trips SET user_id = ? WHERE user_id IS NULL", (user_id,))
                        db.execute(
                            "INSERT OR IGNORE INTO trip_members (trip_id, user_id, role) SELECT id, ?, 'owner' FROM trips WHERE user_id = ?",
                            (user_id, user_id),
                        )
                    db.commit()
                    session.clear()
                    session["user_id"] = user_id
                    session["username"] = username
                    return redirect(url_for("index"))
                except sqlite3.IntegrityError:
                    db.rollback()
                    flash("이미 사용 중인 아이디입니다.", "error")
        return render_template("auth.html", mode="register")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if session.get("user_id"):
            return redirect(url_for("index"))
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            raw_client_key = request.remote_addr or "unknown"
            client_key = hmac.new(app.config["SECRET_KEY"].encode() if isinstance(app.config["SECRET_KEY"], str) else app.config["SECRET_KEY"], raw_client_key.encode(), "sha256").hexdigest()
            now = int(time.time())
            attempt = query_db("SELECT attempt_count,window_started_at FROM login_attempts WHERE client_key=?", (client_key,), one=True)
            count = attempt["attempt_count"] if attempt and now - attempt["window_started_at"] < _login_window_seconds else 0
            if count >= _login_max_attempts:
                flash("로그인 시도가 너무 많습니다. 10분 후 다시 시도해 주세요.", "error")
                return render_template("auth.html", mode="login"), 429
            user = query_db("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,), one=True)
            if user and check_password_hash(user["password_hash"], password):
                get_db().execute("DELETE FROM login_attempts WHERE client_key=?", (client_key,)); get_db().commit()
                session.clear()
                session["user_id"] = user["id"]
                session["username"] = user["username"]
                return redirect(url_for("index"))
            if not attempt or now - attempt["window_started_at"] >= _login_window_seconds:
                get_db().execute("INSERT INTO login_attempts(client_key,attempt_count,window_started_at) VALUES(?,1,?) ON CONFLICT(client_key) DO UPDATE SET attempt_count=1,window_started_at=excluded.window_started_at", (client_key,now))
            else:
                get_db().execute("UPDATE login_attempts SET attempt_count=attempt_count+1 WHERE client_key=?", (client_key,))
            get_db().commit()
            flash("아이디 또는 비밀번호를 확인해 주세요.", "error")
        return render_template("auth.html", mode="login")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/account")
    def account_settings():
        user_id = session["user_id"]
        owned_trips = query_db(
            """SELECT DISTINCT t.id, t.title, t.start_date, t.end_date FROM trips t
               LEFT JOIN trip_members tm ON tm.trip_id = t.id AND tm.user_id = ?
               WHERE t.user_id = ? OR tm.role = 'owner'
               ORDER BY t.start_date, t.id""",
            (user_id, user_id),
        )
        owned_trips = [
            {
                **dict(trip),
                "editors": query_db(
                    """SELECT u.id, u.username FROM trip_members tm
                       JOIN users u ON u.id = tm.user_id
                       WHERE tm.trip_id = ? AND tm.role = 'editor'
                       ORDER BY u.username COLLATE NOCASE""",
                    (trip["id"],),
                ),
            }
            for trip in owned_trips
        ]
        shared_trip_count = query_db(
            "SELECT COUNT(*) AS count FROM trip_members WHERE user_id = ? AND role = 'editor'",
            (user_id,), one=True,
        )["count"]
        return render_template("account.html", owned_trips=owned_trips, shared_trip_count=shared_trip_count)

    @app.get("/trips/<int:trip_id>/revision")
    def trip_revision(trip_id):
        row = query_db("SELECT revision, updated_at FROM trip_revisions WHERE trip_id = ?", (trip_id,), one=True)
        if row is None:
            abort(404)
        response = app.make_response({"revision": row["revision"], "updated_at": row["updated_at"]})
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return response

    @app.get("/trips/<int:trip_id>/export/markdown")
    def export_trip_markdown(trip_id):
        trip = query_db("SELECT * FROM trips WHERE id = ?", (trip_id,), one=True)
        if trip is None:
            abort(404)
        lines = [
            f"# {trip['title']}", "",
            f"- 여행지: {trip['city']}",
            f"- 기간: {trip['start_date']} — {trip['end_date']}",
            f"- 일정: {(date.fromisoformat(trip['end_date']) - date.fromisoformat(trip['start_date'])).days}박 {(date.fromisoformat(trip['end_date']) - date.fromisoformat(trip['start_date'])).days + 1}일",
        ]
        regions = query_db("SELECT label,start_date,end_date FROM trip_regions WHERE trip_id=? ORDER BY start_date,sort_order", (trip_id,))
        if regions:
            lines.extend(["", "## 국가별 기간", ""])
            lines.extend([f"- **{region['label']}**: {region['start_date']} — {region['end_date']}" for region in regions])
        lines.extend(["", "## 일정", ""])
        days = query_db("SELECT id,day_number,date FROM itinerary_days WHERE trip_id=? ORDER BY day_number", (trip_id,))
        for day in days:
            lines.extend([f"### Day {day['day_number']} · {day['date']}", ""])
            stays = query_db("SELECT name,address,check_in_date,check_out_date,check_in_time,check_out_time FROM accommodations WHERE trip_id=? AND (check_in_date=? OR check_out_date=?) ORDER BY check_in_date", (trip_id, day['date'], day['date']))
            checkout_stays = [stay for stay in stays if stay['check_out_date'] == day['date']]
            checkin_stays = [stay for stay in stays if stay['check_in_date'] == day['date']]
            for stay in checkout_stays:
                lines.append(f"- 🏨 **체크아웃 {stay['name']}** · {stay['check_out_time'] or '시간 미입력'} — {stay['address']}")
            transfers = query_db("SELECT origin,destination FROM itinerary_transfers WHERE day_id=? ORDER BY created_at", (day['id'],))
            for transfer in transfers:
                lines.append(f"- 🚆 국가 간 이동: {transfer['origin']} → {transfer['destination']}")
            for stay in checkin_stays:
                lines.append(f"- 🏨 **체크인 {stay['name']}** · {stay['check_in_time'] or '시간 미입력'} — {stay['address']}")
            places = query_db("SELECT p.name,p.address,i.sequence,i.start_time FROM itinerary_items i JOIN places p ON p.id=i.place_id WHERE i.day_id=? ORDER BY i.sequence", (day['id'],))
            for place in places:
                when = f"{place['start_time']} · " if place['start_time'] else ""
                lines.append(f"{place['sequence']}. {when}**{place['name']}** — {place['address']}")
            if not stays and not transfers and not places:
                lines.append("- 일정 없음")
            lines.append("")
        lines.extend(["## 숙소", ""])
        accommodations = query_db("SELECT name,address,check_in_date,check_out_date,check_in_time,check_out_time FROM accommodations WHERE trip_id=? ORDER BY check_in_date,id", (trip_id,))
        if accommodations:
            lines.extend([f"- **{item['name']}** — {item['address']} (체크인 {item['check_in_date'] or ''} {item['check_in_time'] or ''} · 체크아웃 {item['check_out_date'] or ''} {item['check_out_time'] or ''})" for item in accommodations])
        else:
            lines.append("- 등록된 숙소 없음")
        lines.extend(["", "## 교통편", ""])
        legs = query_db("SELECT mode,carrier,transport_number,origin,destination,departure_at,arrival_at FROM trip_transport_legs WHERE trip_id=? ORDER BY departure_at", (trip_id,))
        if legs:
            for leg in legs:
                label = {"flight":"항공","train":"기차","bus":"버스"}.get(leg['mode'], leg['mode'])
                transport_name = f"{label} {leg['transport_number']}" if leg['transport_number'] else label
                carrier = f" {leg['carrier']}" if leg['carrier'] else ""
                lines.append(f"- **{transport_name}**{carrier}: {leg['origin']} → {leg['destination']} · {leg['departure_at']} ~ {leg['arrival_at']}")
        else:
            lines.append("- 등록된 교통편 없음")
        content = "\n".join(lines).rstrip() + "\n"
        filename = secure_filename(trip['title']) or f"trip-{trip_id}"
        return app.response_class(content, mimetype="text/markdown; charset=utf-8", headers={"Content-Disposition": f"attachment; filename={filename}.md"})

    @app.post("/account/delete")
    def delete_account():
        user_id = session["user_id"]
        password = request.form.get("password", "")
        confirmation = request.form.get("confirmation", "").strip()
        user = query_db("SELECT password_hash FROM users WHERE id = ?", (user_id,), one=True)
        if user is None:
            session.clear()
            return redirect(url_for("login"))
        if not check_password_hash(user["password_hash"], password):
            flash("비밀번호가 일치하지 않습니다.", "error")
            return redirect(url_for("account_settings"))
        if confirmation != "탈퇴":
            flash("확인란에 ‘탈퇴’를 입력해 주세요.", "error")
            return redirect(url_for("account_settings"))

        db = get_db()
        owned_trip_ids = [
            row["id"] for row in query_db(
                """SELECT DISTINCT t.id FROM trips t
                   LEFT JOIN trip_members tm ON tm.trip_id = t.id AND tm.user_id = ?
                   WHERE t.user_id = ? OR tm.role = 'owner'""",
                (user_id, user_id),
            )
        ]
        successor_by_trip = {}
        for trip_id in owned_trip_ids:
            candidates = query_db(
                "SELECT user_id FROM trip_members WHERE trip_id = ? AND role = 'editor'",
                (trip_id,),
            )
            if not candidates:
                continue
            try:
                successor_id = int(request.form.get(f"successor_{trip_id}", ""))
            except ValueError:
                flash("공동 여행의 새 소유자를 각각 선택해 주세요.", "error")
                return redirect(url_for("account_settings"))
            if successor_id not in {candidate["user_id"] for candidate in candidates}:
                flash("선택한 사용자는 해당 여행의 편집자가 아닙니다.", "error")
                return redirect(url_for("account_settings"))
            successor_by_trip[trip_id] = successor_id
        delete_trip_ids = [trip_id for trip_id in owned_trip_ids if trip_id not in successor_by_trip]
        uploaded_files = []
        if delete_trip_ids:
            marks = ",".join("?" for _ in delete_trip_ids)
            file_tables = (
                ("place_photos", "stored_file_name", app.config["PLACE_PHOTO_UPLOAD_FOLDER"]),
                ("trip_expenses", "stored_file_name", app.config["EXPENSE_UPLOAD_FOLDER"]),
                ("trip_transport_legs", "stored_file_name", app.config["TRANSPORT_UPLOAD_FOLDER"]),
            )
            for table, column, folder in file_tables:
                uploaded_files.extend(
                    (folder, row[column])
                    for row in query_db(
                        f"SELECT {column} FROM {table} WHERE trip_id IN ({marks}) AND {column} IS NOT NULL",
                        delete_trip_ids,
                    )
                )

        try:
            db.execute("BEGIN IMMEDIATE")
            for trip_id, successor_id in successor_by_trip.items():
                db.execute(
                    "UPDATE trip_members SET role = 'owner' WHERE trip_id = ? AND user_id = ?",
                    (trip_id, successor_id),
                )
                db.execute("UPDATE trips SET user_id = ? WHERE id = ?", (successor_id, trip_id))
            if owned_trip_ids:
                if delete_trip_ids:
                    marks = ",".join("?" for _ in delete_trip_ids)
                    db.execute(f"DELETE FROM trips WHERE id IN ({marks})", delete_trip_ids)
            # Cascades remove this user's memberships, invitations, and notifications.
            db.execute("DELETE FROM users WHERE id = ?", (user_id,))
            db.commit()
        except sqlite3.Error:
            db.rollback()
            flash("계정을 삭제하지 못했습니다. 잠시 후 다시 시도해 주세요.", "error")
            return redirect(url_for("account_settings"))

        for folder, stored_name in uploaded_files:
            file_path = os.path.join(folder, os.path.basename(stored_name))
            try:
                if os.path.isfile(file_path):
                    os.remove(file_path)
            except OSError:
                app.logger.warning("Could not remove account data file: %s", file_path)

        session.clear()
        flash("계정과 소유한 여행을 삭제했습니다.", "success")
        return redirect(url_for("login"))

    @app.get("/")
    def index():
        trip_rows = query_db(
            """SELECT t.*, tm.role AS member_role, COUNT(DISTINCT p.id) AS place_count
               FROM trips AS t LEFT JOIN places AS p ON p.trip_id = t.id
               JOIN trip_members tm ON tm.trip_id = t.id AND tm.user_id = ?
               GROUP BY t.id, tm.role ORDER BY t.start_date, t.id""",
            (session["user_id"],),
        )
        trips_by_id = {
            trip["id"]: {
                **dict(trip),
                "night_count": (date.fromisoformat(trip["end_date"]) - date.fromisoformat(trip["start_date"])).days,
                "day_count": (date.fromisoformat(trip["end_date"]) - date.fromisoformat(trip["start_date"])).days + 1,
            }
            for trip in trip_rows
        }
        source_relations = query_db(
            """SELECT merged_trip_id, source_trip_id, sort_order
               FROM merged_trip_sources ORDER BY merged_trip_id, sort_order, source_trip_id"""
        )
        children_by_parent = {}
        nested_trip_ids = set()
        for relation in source_relations:
            if relation["merged_trip_id"] not in trips_by_id or relation["source_trip_id"] not in trips_by_id:
                continue
            children_by_parent.setdefault(relation["merged_trip_id"], []).append(relation["source_trip_id"])
            nested_trip_ids.add(relation["source_trip_id"])

        def build_trip_tree(trip_id, ancestors=()):
            trip = dict(trips_by_id[trip_id])
            if trip_id in ancestors:
                trip["source_trips"] = []
                return trip
            next_ancestors = (*ancestors, trip_id)
            trip["source_trips"] = [
                build_trip_tree(child_id, next_ancestors)
                for child_id in children_by_parent.get(trip_id, [])
            ]
            return trip

        root_trip_ids = [trip_id for trip_id in trips_by_id if trip_id not in nested_trip_ids]
        if not root_trip_ids:
            root_trip_ids = list(trips_by_id)
        trips = [build_trip_tree(trip_id) for trip_id in root_trip_ids]
        invitations = query_db(
            """SELECT i.id, i.created_at, t.title, t.city, t.start_date, t.end_date, u.username AS invited_by_name
               FROM trip_invitations i JOIN trips t ON t.id = i.trip_id
               JOIN users u ON u.id = i.invited_by
               WHERE i.invited_user_id = ? AND i.status = 'pending'
               ORDER BY i.created_at DESC, i.id DESC""",
            (session["user_id"],),
        )
        notifications = query_db(
            "SELECT id, message FROM user_notifications WHERE user_id = ? AND is_read = 0 ORDER BY id",
            (session["user_id"],),
        )
        if notifications:
            db = get_db()
            db.execute("UPDATE user_notifications SET is_read = 1 WHERE user_id = ? AND is_read = 0", (session["user_id"],))
            db.commit()
        return render_template(
            "index.html", trips=trips, all_trip_count=len(trips_by_id),
            invitations=invitations, notifications=notifications,
            today=date.today().isoformat(),
        )

    @app.route("/trips/merge", methods=["GET", "POST"])
    def merge_trips_page():
        flash("여행 병합 대신 각 여행 개요에서 국가별 체류 구간을 추가해 주세요.", "info")
        return redirect(url_for("index"))

    def sync_country_transfers(connection, trip_id):
        trip = connection.execute(
            "SELECT start_date, end_date FROM trips WHERE id = ?", (trip_id,)
        ).fetchone()
        if trip is None:
            return
        regions = connection.execute(
            "SELECT id, label, start_date, end_date FROM trip_regions WHERE trip_id = ? "
            "ORDER BY start_date, sort_order, id", (trip_id,),
        ).fetchall()
        desired = {}
        for origin_region, destination_region in zip(regions, regions[1:]):
            overlap_start = max(origin_region["start_date"], destination_region["start_date"])
            overlap_end = min(origin_region["end_date"], destination_region["end_date"])
            if overlap_start <= overlap_end:
                desired[(origin_region["id"], destination_region["id"])] = {
                    "date": overlap_start,
                    "origin": origin_region["label"],
                    "destination": destination_region["label"],
                }
        if not desired:
            connection.execute(
                "DELETE FROM itinerary_transfers WHERE trip_id = ? "
                "AND from_region_id IS NOT NULL AND to_region_id IS NOT NULL", (trip_id,),
            )
            return

        trip_start = date.fromisoformat(trip["start_date"])
        trip_end = date.fromisoformat(trip["end_date"])
        day_rows = connection.execute(
            "SELECT id, day_number, date FROM itinerary_days WHERE trip_id = ?", (trip_id,)
        ).fetchall()
        day_by_date = {row["date"]: row for row in day_rows}
        required_dates = {entry["date"] for entry in desired.values()}
        missing_dates = required_dates - day_by_date.keys()
        if missing_dates:
            if not day_rows:
                dates = [
                    (trip_start + timedelta(days=offset)).isoformat()
                    for offset in range((trip_end - trip_start).days + 1)
                ]
                for day_number, day_date in enumerate(dates, 1):
                    cursor = connection.execute(
                        "INSERT INTO itinerary_days (trip_id, day_number, date) VALUES (?, ?, ?)",
                        (trip_id, day_number, day_date),
                    )
                    day_by_date[day_date] = {"id": cursor.lastrowid, "date": day_date}
            else:
                connection.execute(
                    "UPDATE itinerary_days SET day_number = day_number + 1000000 WHERE trip_id = ?",
                    (trip_id,),
                )
                date_to_id = {row["date"]: row["id"] for row in day_rows}
                total_days = (trip_end - trip_start).days + 1
                for offset in range(total_days):
                    day_date = (trip_start + timedelta(days=offset)).isoformat()
                    day_number = offset + 1
                    if day_date in date_to_id:
                        connection.execute(
                            "UPDATE itinerary_days SET day_number = ? WHERE id = ?",
                            (day_number, date_to_id[day_date]),
                        )
                        day_by_date[day_date] = {"id": date_to_id[day_date], "date": day_date}
                    else:
                        cursor = connection.execute(
                            "INSERT INTO itinerary_days (trip_id, day_number, date) VALUES (?, ?, ?)",
                            (trip_id, day_number, day_date),
                        )
                        day_by_date[day_date] = {"id": cursor.lastrowid, "date": day_date}

        existing_rows = connection.execute(
            """SELECT id, day_id, from_region_id, to_region_id FROM itinerary_transfers
               WHERE trip_id = ? AND from_region_id IS NOT NULL AND to_region_id IS NOT NULL
               ORDER BY id""", (trip_id,),
        ).fetchall()
        retained_pairs = set()
        for row in existing_rows:
            pair = (row["from_region_id"], row["to_region_id"])
            if pair not in desired or pair in retained_pairs:
                connection.execute("DELETE FROM itinerary_transfers WHERE id = ?", (row["id"],))
                continue
            retained_pairs.add(pair)
            entry = desired[pair]
            day_id = day_by_date[entry["date"]]["id"]
            destination_item = connection.execute(
                """SELECT ii.id FROM itinerary_items ii JOIN places p ON p.id = ii.place_id
                   WHERE ii.day_id = ? AND p.region_id = ? ORDER BY ii.sequence LIMIT 1""",
                (day_id, pair[1]),
            ).fetchone()
            connection.execute(
                """UPDATE itinerary_transfers SET day_id = ?, before_item_id = ?, origin = ?, destination = ?
                   WHERE id = ?""",
                (day_id, destination_item["id"] if destination_item else None,
                 entry["origin"], entry["destination"], row["id"]),
            )

        for pair, entry in desired.items():
            if pair in retained_pairs:
                continue
            day_id = day_by_date[entry["date"]]["id"]
            destination_item = connection.execute(
                """SELECT ii.id FROM itinerary_items ii JOIN places p ON p.id = ii.place_id
                   WHERE ii.day_id = ? AND p.region_id = ? ORDER BY ii.sequence LIMIT 1""",
                (day_id, pair[1]),
            ).fetchone()
            connection.execute(
                """INSERT INTO itinerary_transfers
                   (trip_id, day_id, before_item_id, from_region_id, to_region_id, origin, destination)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (trip_id, day_id, destination_item["id"] if destination_item else None,
                 pair[0], pair[1], entry["origin"], entry["destination"]),
            )

    @app.get("/trips/<int:trip_id>")
    def trip_detail(trip_id):
        trip = query_db("SELECT * FROM trips WHERE id = ?", (trip_id,), one=True)
        if trip is None:
            abort(404)
        db = get_db()
        db.execute(
            "UPDATE places SET latitude = NULL, longitude = NULL, coordinates_updated_at = NULL "
            "WHERE trip_id = ? AND provider_place_id IS NOT NULL "
            "AND coordinates_updated_at < datetime('now', '-30 days')", (trip_id,),
        )
        db.execute(
            "UPDATE accommodations SET latitude = NULL, longitude = NULL, coordinates_updated_at = NULL "
            "WHERE trip_id = ? AND google_place_id IS NOT NULL "
            "AND coordinates_updated_at < datetime('now', '-30 days')", (trip_id,),
        )
        db.commit()
        accommodations = query_db(
            "SELECT * FROM accommodations WHERE trip_id = ? ORDER BY check_in_date, id", (trip_id,)
        )
        days = query_db(
            "SELECT id, day_number, date FROM itinerary_days WHERE trip_id = ? ORDER BY day_number",
            (trip_id,),
        )
        days = [
            {
                **dict(day),
                "place_names": " · ".join(
                    place["name"] for place in query_db(
                        """SELECT p.name FROM itinerary_items ii JOIN places p ON p.id = ii.place_id
                           WHERE ii.day_id = ? ORDER BY ii.sequence""",
                        (day["id"],),
                    )
                ),
                "place_count": query_db(
                    "SELECT COUNT(*) AS count FROM itinerary_items WHERE day_id = ?",
                    (day["id"],), one=True,
                )["count"],
            }
            for day in days
        ]
        todos = query_db(
            "SELECT * FROM trip_todos WHERE trip_id = ? ORDER BY is_done, id DESC",
            (trip_id,),
        )
        expense_summary = query_db(
            "SELECT COUNT(*) AS count, COALESCE(SUM(amount), 0) AS total FROM trip_expenses WHERE trip_id = ?",
            (trip_id,), one=True,
        )
        place_count = query_db(
            "SELECT COUNT(*) AS count FROM places WHERE trip_id = ?", (trip_id,), one=True
        )["count"]
        transport_count = query_db(
            "SELECT COUNT(*) AS count FROM trip_transport_legs WHERE trip_id = ?", (trip_id,), one=True
        )["count"]
        journal_place_count = query_db(
            """SELECT COUNT(*) AS count FROM (
                 SELECT p.id FROM places p LEFT JOIN place_journals j ON j.place_id = p.id
                 WHERE p.trip_id = ? AND TRIM(COALESCE(j.information, '')) <> ''
               )""", (trip_id,), one=True,
        )["count"]
        trip_members = query_db(
            """SELECT tm.user_id, tm.role, tm.created_at, u.username
               FROM trip_members tm JOIN users u ON u.id = tm.user_id
               WHERE tm.trip_id = ? ORDER BY CASE tm.role WHEN 'owner' THEN 0 ELSE 1 END, u.username COLLATE NOCASE""",
            (trip_id,),
        )
        start = date.fromisoformat(trip["start_date"])
        end = date.fromisoformat(trip["end_date"])
        return render_template(
            "trip.html", trip=trip, accommodations=accommodations, days=days, todos=todos,
            expense_summary=expense_summary, place_count=place_count, transport_count=transport_count,
            journal_place_count=journal_place_count,
            trip_members=trip_members, trip_role=g.trip_role,
            day_count=(end - start).days + 1, night_count=(end - start).days,
        )

    @app.get("/trips/<int:trip_id>/regions")
    def trip_regions_page(trip_id):
        trip = query_db("SELECT * FROM trips WHERE id = ?", (trip_id,), one=True)
        if trip is None:
            abort(404)
        regions = query_db(
            "SELECT * FROM trip_regions WHERE trip_id = ? ORDER BY start_date, sort_order, id",
            (trip_id,),
        )
        country_transfers = query_db(
            """SELECT it.id, it.origin, it.destination, d.date,
                      fr.label AS from_region_label, tr.label AS to_region_label
               FROM itinerary_transfers it
               JOIN itinerary_days d ON d.id = it.day_id
               LEFT JOIN trip_regions fr ON fr.id = it.from_region_id
               LEFT JOIN trip_regions tr ON tr.id = it.to_region_id
               WHERE it.trip_id = ? AND it.from_region_id IS NOT NULL AND it.to_region_id IS NOT NULL
               ORDER BY d.date, it.id""",
            (trip_id,),
        )
        default_region_start = regions[-1]["end_date"] if regions else trip["start_date"]
        return render_template(
            "trip_regions.html", trip=trip, regions=regions,
            country_transfers=country_transfers,
            new_region_start=default_region_start, new_region_end=trip["end_date"],
        )

    @app.post("/trips/<int:trip_id>/regions")
    def add_trip_region(trip_id):
        trip = query_db("SELECT start_date, end_date FROM trips WHERE id = ?", (trip_id,), one=True)
        if trip is None:
            abort(404)
        label = request.form.get("label", "").strip()[:100]
        try:
            region_start = date.fromisoformat(request.form.get("start_date", ""))
            region_end = date.fromisoformat(request.form.get("end_date", ""))
            trip_start = date.fromisoformat(trip["start_date"])
            trip_end = date.fromisoformat(trip["end_date"])
        except ValueError:
            flash("국가별 구간의 시작일과 종료일을 확인해 주세요.", "error")
            return redirect(url_for("trip_regions_page", trip_id=trip_id))
        if not label or region_end < region_start or region_start < trip_start or region_end > trip_end:
            flash("국가/지역 이름과 여행 기간 안의 날짜를 확인해 주세요.", "error")
            return redirect(url_for("trip_regions_page", trip_id=trip_id))
        db = get_db()
        try:
            sort_order = db.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM trip_regions WHERE trip_id = ?",
                (trip_id,),
            ).fetchone()[0]
            db.execute(
                "INSERT INTO trip_regions (trip_id, label, start_date, end_date, sort_order) VALUES (?, ?, ?, ?, ?)",
                (trip_id, label, region_start.isoformat(), region_end.isoformat(), sort_order),
            )
            sync_country_transfers(db, trip_id)
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            flash("이미 같은 이름의 국가/지역 구간이 있습니다.", "error")
            return redirect(url_for("trip_regions_page", trip_id=trip_id))
        flash(f"{label} 구간을 여행에 추가했습니다. 장소와 숙소를 등록할 때 이 구간을 선택하세요.", "success")
        return redirect(url_for("trip_regions_page", trip_id=trip_id))

    @app.post("/trips/<int:trip_id>/regions/<int:region_id>/update")
    def update_trip_region(trip_id, region_id):
        trip = query_db("SELECT start_date, end_date FROM trips WHERE id = ?", (trip_id,), one=True)
        region = query_db(
            "SELECT id FROM trip_regions WHERE id = ? AND trip_id = ?", (region_id, trip_id), one=True
        )
        if trip is None or region is None:
            abort(404)
        label = request.form.get("label", "").strip()[:100]
        try:
            region_start = date.fromisoformat(request.form.get("start_date", ""))
            region_end = date.fromisoformat(request.form.get("end_date", ""))
            trip_start = date.fromisoformat(trip["start_date"])
            trip_end = date.fromisoformat(trip["end_date"])
        except ValueError:
            flash("국가별 구간의 시작일과 종료일을 확인해 주세요.", "error")
            return redirect(url_for("trip_regions_page", trip_id=trip_id))
        if not label or region_end < region_start or region_start < trip_start or region_end > trip_end:
            flash("국가/지역 이름과 여행 기간 안의 날짜를 확인해 주세요.", "error")
            return redirect(url_for("trip_regions_page", trip_id=trip_id))
        try:
            db = get_db()
            db.execute(
                "UPDATE trip_regions SET label = ?, start_date = ?, end_date = ? WHERE id = ? AND trip_id = ?",
                (label, region_start.isoformat(), region_end.isoformat(), region_id, trip_id),
            )
            sync_country_transfers(db, trip_id)
            db.commit()
        except sqlite3.IntegrityError:
            get_db().rollback()
            flash("이미 같은 이름의 국가/지역 구간이 있습니다.", "error")
            return redirect(url_for("trip_regions_page", trip_id=trip_id))
        flash(f"{label} 구간을 수정했습니다.", "success")
        return redirect(url_for("trip_regions_page", trip_id=trip_id))

    @app.post("/trips/<int:trip_id>/regions/<int:region_id>/delete")
    def delete_trip_region(trip_id, region_id):
        db = get_db()
        db.execute(
            "DELETE FROM itinerary_transfers WHERE trip_id = ? AND (from_region_id = ? OR to_region_id = ?)",
            (trip_id, region_id, region_id),
        )
        cursor = db.execute(
            "DELETE FROM trip_regions WHERE id = ? AND trip_id = ?", (region_id, trip_id)
        )
        if cursor.rowcount:
            sync_country_transfers(db, trip_id)
        db.commit()
        if cursor.rowcount == 0:
            abort(404)
        flash("국가 구간을 삭제했습니다. 연결된 장소와 숙소 정보는 여행에 남아 있습니다.", "success")
        return redirect(url_for("trip_regions_page", trip_id=trip_id))

    @app.post("/trips/<int:trip_id>/invitations")
    def invite_trip_member(trip_id):
        if g.trip_role != "owner":
            abort(403)
        username = request.form.get("username", "").strip()
        invited_user = query_db(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE", (username,), one=True
        )
        if invited_user is None:
            flash("해당 아이디의 계정을 찾을 수 없습니다.", "error")
        elif invited_user["id"] == session["user_id"]:
            flash("자기 자신은 초대할 수 없습니다.", "error")
        elif query_db(
            "SELECT 1 FROM trip_members WHERE trip_id = ? AND user_id = ?",
            (trip_id, invited_user["id"]), one=True,
        ):
            flash("이미 이 여행의 멤버입니다.", "error")
        else:
            get_db().execute(
                """INSERT INTO trip_invitations (trip_id, invited_user_id, invited_by, status)
                   VALUES (?, ?, ?, 'pending')
                   ON CONFLICT(trip_id, invited_user_id) DO UPDATE SET
                     invited_by = excluded.invited_by, status = 'pending', created_at = CURRENT_TIMESTAMP""",
                (trip_id, invited_user["id"], session["user_id"]),
            )
            get_db().commit()
            flash(f"{username} 님에게 여행 초대를 보냈습니다.", "success")
        return redirect(url_for("trip_detail", trip_id=trip_id))

    @app.post("/invitations/<int:invitation_id>/accept")
    def accept_trip_invitation(invitation_id):
        invitation = query_db(
            """SELECT trip_id FROM trip_invitations
               WHERE id = ? AND invited_user_id = ? AND status = 'pending'""",
            (invitation_id, session["user_id"]), one=True,
        )
        if invitation is None:
            abort(404)
        db = get_db()
        try:
            db.execute("BEGIN")
            db.execute(
                "INSERT OR IGNORE INTO trip_members (trip_id, user_id, role) VALUES (?, ?, 'editor')",
                (invitation["trip_id"], session["user_id"]),
            )
            db.execute("UPDATE trip_invitations SET status = 'accepted' WHERE id = ?", (invitation_id,))
            db.commit()
        except sqlite3.Error:
            db.rollback()
            flash("초대를 수락하지 못했습니다. 다시 시도해 주세요.", "error")
            return redirect(url_for("index"))
        flash("여행 초대를 수락했습니다.", "success")
        return redirect(url_for("trip_detail", trip_id=invitation["trip_id"]))

    @app.post("/invitations/<int:invitation_id>/decline")
    def decline_trip_invitation(invitation_id):
        cursor = get_db().execute(
            "UPDATE trip_invitations SET status = 'declined' WHERE id = ? AND invited_user_id = ? AND status = 'pending'",
            (invitation_id, session["user_id"]),
        )
        get_db().commit()
        if cursor.rowcount == 0:
            abort(404)
        flash("여행 초대를 거절했습니다.", "success")
        return redirect(url_for("index"))

    @app.post("/trips/<int:trip_id>/members/<int:member_id>/remove")
    def remove_trip_member(trip_id, member_id):
        if g.trip_role != "owner":
            abort(403)
        member = query_db(
            "SELECT role FROM trip_members WHERE trip_id = ? AND user_id = ?",
            (trip_id, member_id), one=True,
        )
        if member is None:
            abort(404)
        if member["role"] == "owner":
            abort(403)
        trip = query_db("SELECT title FROM trips WHERE id = ?", (trip_id,), one=True)
        db = get_db()
        db.execute(
            "INSERT INTO user_notifications (user_id, message) VALUES (?, ?)",
            (member_id, f"‘{trip['title']}’ 여행의 공동편집에서 제거되었습니다. 이제 이 여행을 볼 수 없습니다."),
        )
        db.execute(
            "DELETE FROM trip_members WHERE trip_id = ? AND user_id = ?", (trip_id, member_id)
        )
        db.execute(
            "UPDATE trip_invitations SET status = 'declined' WHERE trip_id = ? AND invited_user_id = ?",
            (trip_id, member_id),
        )
        db.commit()
        flash("여행 멤버를 제거했습니다.", "success")
        return redirect(url_for("trip_detail", trip_id=trip_id))

    @app.get("/trips/<int:trip_id>/transport")
    def trip_transport(trip_id):
        trip = query_db("SELECT * FROM trips WHERE id = ?", (trip_id,), one=True)
        if trip is None:
            abort(404)
        legs = query_db(
            "SELECT * FROM trip_transport_legs WHERE trip_id = ? ORDER BY departure_at, id",
            (trip_id,),
        )
        return render_template("transport.html", trip=trip, transport_legs=legs)

    @app.get("/trips/<int:trip_id>/accommodation")
    def trip_accommodation(trip_id):
        trip = query_db("SELECT * FROM trips WHERE id = ?", (trip_id,), one=True)
        if trip is None:
            abort(404)
        accommodations = query_db(
            "SELECT * FROM accommodations WHERE trip_id = ? ORDER BY check_in_date, id", (trip_id,)
        )
        regions = query_db("SELECT id, label FROM trip_regions WHERE trip_id = ? ORDER BY start_date, sort_order, id", (trip_id,))
        return render_template(
            "accommodation.html", trip=trip, accommodations=accommodations,
            accommodation_search_results=[], accommodation_search_query="",
            editing_accommodation=None, editing_accommodation_id="", regions=regions,
            selected_region_id=str(regions[0]["id"]) if regions else "",
        )

    @app.get("/trips/<int:trip_id>/places")
    def trip_places(trip_id):
        trip = query_db("SELECT * FROM trips WHERE id = ?", (trip_id,), one=True)
        if trip is None:
            abort(404)
        places = _trip_places(trip_id)
        regions = query_db("SELECT id, label FROM trip_regions WHERE trip_id = ? ORDER BY start_date, sort_order, id", (trip_id,))
        return render_template(
            "places.html", trip=trip, places=places, place_search_results=[],
            place_search_query="", place_search_category="attraction",
            regions=regions, selected_region_id=str(regions[0]["id"]) if regions else "",
        )

    @app.get("/trips/<int:trip_id>/places/<int:place_id>")
    def place_detail(trip_id, place_id):
        trip = query_db("SELECT * FROM trips WHERE id = ?", (trip_id,), one=True)
        place = query_db(
            """SELECT p.*, d.day_number, d.date AS visit_date FROM places p
               LEFT JOIN itinerary_items ii ON ii.place_id = p.id
               LEFT JOIN itinerary_days d ON d.id = ii.day_id
               WHERE p.id = ? AND p.trip_id = ?""",
            (place_id, trip_id), one=True,
        )
        if trip is None or place is None:
            abort(404)
        journal = query_db(
            "SELECT information, thoughts, updated_at FROM place_journals WHERE place_id = ?",
            (place_id,), one=True,
        )
        expenses = query_db(
            "SELECT * FROM trip_expenses WHERE trip_id = ? AND place_id = ? ORDER BY expense_date DESC, id DESC",
            (trip_id, place_id),
        )
        return render_template(
            "place_detail.html", trip=trip, place=place, journal=journal,
            expenses=expenses,
            expense_categories=("숙박", "교통", "식비", "관광·티켓", "쇼핑", "기타"),
            currencies=("EUR", "USD", "KRW", "GBP", "CZK", "HUF", "CHF", "JPY"),
            budget_currency=trip["budget_currency"],
            expense_date=place["visit_date"] or date.today().isoformat(),
        )

    @app.get("/trips/<int:trip_id>/journal")
    def trip_journal(trip_id):
        trip = query_db("SELECT * FROM trips WHERE id = ?", (trip_id,), one=True)
        if trip is None:
            abort(404)
        place_rows = query_db(
            """SELECT p.id, p.name, p.address, j.information
               FROM places p LEFT JOIN place_journals j ON j.place_id = p.id
               WHERE p.trip_id = ? AND TRIM(COALESCE(j.information, '')) <> ''
               ORDER BY p.name COLLATE NOCASE, p.id""",
            (trip_id,),
        )
        entries = []
        for row in place_rows:
            entry = dict(row)
            entries.append(entry)
        return render_template("trip_journal.html", trip=trip, entries=entries)

    @app.post("/trips/<int:trip_id>/places/<int:place_id>/journal")
    def save_place_journal(trip_id, place_id):
        if query_db("SELECT id FROM places WHERE id = ? AND trip_id = ?", (place_id, trip_id), one=True) is None:
            abort(404)
        information = request.form.get("information", "").strip()[:10000]
        thoughts = request.form.get("thoughts", "").strip()[:10000]
        get_db().execute(
            """INSERT INTO place_journals (place_id, trip_id, information, thoughts, updated_at)
               VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(place_id) DO UPDATE SET
                information=excluded.information,
                thoughts=excluded.thoughts,
                updated_at=CURRENT_TIMESTAMP""",
            (place_id, trip_id, information, thoughts),
        )
        get_db().commit()
        flash("장소 기록을 저장했습니다.", "success")
        return redirect(url_for("place_detail", trip_id=trip_id, place_id=place_id))

    @app.post("/trips/<int:trip_id>/places/<int:place_id>/photos")
    def add_place_photos(trip_id, place_id):
        if query_db("SELECT id FROM places WHERE id = ? AND trip_id = ?", (place_id, trip_id), one=True) is None:
            abort(404)
        uploads = [file for file in request.files.getlist("photos") if file and file.filename]
        caption = request.form.get("caption", "").strip()[:240]
        allowed_extensions = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
        if not uploads:
            flash("올릴 사진을 선택해 주세요.", "error")
            return redirect(url_for("place_detail", trip_id=trip_id, place_id=place_id))
        if len(uploads) > 10:
            flash("한 번에 사진은 최대 10장까지 올릴 수 있습니다.", "error")
            return redirect(url_for("place_detail", trip_id=trip_id, place_id=place_id))
        saved_names = []
        db = get_db()
        try:
            for uploaded in uploads:
                extension = Path(uploaded.filename).suffix.lower()
                if extension not in allowed_extensions:
                    raise ValueError("사진은 JPG, PNG, WEBP, HEIC, HEIF 파일만 올릴 수 있습니다.")
                safe_name = secure_filename(uploaded.filename)
                original_name = (
                    safe_name if Path(safe_name).suffix.lower() == extension
                    else f"사진{extension}"
                )
                stored_name = f"{uuid.uuid4().hex}{extension}"
                saved_names.append(stored_name)
                uploaded.save(os.path.join(app.config["PLACE_PHOTO_UPLOAD_FOLDER"], stored_name))
                db.execute(
                    """INSERT INTO place_photos
                       (place_id, trip_id, stored_file_name, original_file_name, caption)
                       VALUES (?, ?, ?, ?, ?)""",
                    (place_id, trip_id, stored_name, original_name, caption),
                )
            db.commit()
        except (ValueError, sqlite3.Error, OSError) as error:
            db.rollback()
            for stored_name in saved_names:
                file_path = os.path.join(app.config["PLACE_PHOTO_UPLOAD_FOLDER"], stored_name)
                if os.path.isfile(file_path):
                    os.remove(file_path)
            message = str(error) if isinstance(error, ValueError) else "사진을 저장하지 못했습니다."
            flash(message, "error")
            return redirect(url_for("place_detail", trip_id=trip_id, place_id=place_id))
        flash(f"사진 {len(saved_names)}장을 추가했습니다.", "success")
        return redirect(url_for("place_detail", trip_id=trip_id, place_id=place_id))

    @app.get("/trips/<int:trip_id>/places/<int:place_id>/photos/<int:photo_id>")
    def place_photo(trip_id, place_id, photo_id):
        photo = query_db(
            """SELECT stored_file_name FROM place_photos
               WHERE id = ? AND trip_id = ? AND place_id = ?""",
            (photo_id, trip_id, place_id), one=True,
        )
        if photo is None:
            abort(404)
        return send_from_directory(
            app.config["PLACE_PHOTO_UPLOAD_FOLDER"], photo["stored_file_name"],
            as_attachment=False,
        )

    @app.post("/trips/<int:trip_id>/places/<int:place_id>/photos/<int:photo_id>/delete")
    def delete_place_photo(trip_id, place_id, photo_id):
        photo = query_db(
            """SELECT stored_file_name FROM place_photos
               WHERE id = ? AND trip_id = ? AND place_id = ?""",
            (photo_id, trip_id, place_id), one=True,
        )
        if photo is None:
            abort(404)
        get_db().execute("DELETE FROM place_photos WHERE id = ?", (photo_id,))
        get_db().commit()
        file_path = os.path.join(app.config["PLACE_PHOTO_UPLOAD_FOLDER"], photo["stored_file_name"])
        if os.path.isfile(file_path):
            os.remove(file_path)
        flash("사진을 삭제했습니다.", "success")
        return redirect(url_for("place_detail", trip_id=trip_id, place_id=place_id))

    @app.post("/trips/<int:trip_id>/todos")
    def add_trip_todo(trip_id):
        if query_db("SELECT id FROM trips WHERE id = ?", (trip_id,), one=True) is None:
            abort(404)
        title = request.form.get("title", "").strip()[:180]
        if not title:
            flash("할 일을 입력해 주세요.", "error")
        else:
            get_db().execute("INSERT INTO trip_todos (trip_id, title) VALUES (?, ?)", (trip_id, title))
            get_db().commit()
            flash("할 일을 추가했습니다.", "success")
        return redirect(url_for("trip_detail", trip_id=trip_id))

    @app.post("/trips/<int:trip_id>/todos/<int:todo_id>/toggle")
    def toggle_trip_todo(trip_id, todo_id):
        cursor = get_db().execute(
            "UPDATE trip_todos SET is_done = 1 - is_done WHERE id = ? AND trip_id = ?",
            (todo_id, trip_id),
        )
        get_db().commit()
        if cursor.rowcount == 0:
            abort(404)
        return redirect(url_for("trip_detail", trip_id=trip_id))

    @app.post("/trips/<int:trip_id>/todos/<int:todo_id>/delete")
    def delete_trip_todo(trip_id, todo_id):
        cursor = get_db().execute(
            "DELETE FROM trip_todos WHERE id = ? AND trip_id = ?", (todo_id, trip_id)
        )
        get_db().commit()
        if cursor.rowcount == 0:
            abort(404)
        return redirect(url_for("trip_detail", trip_id=trip_id))

    @app.get("/trips/<int:trip_id>/expenses")
    def trip_expenses(trip_id):
        trip = query_db("SELECT * FROM trips WHERE id = ?", (trip_id,), one=True)
        if trip is None:
            abort(404)
        expenses = query_db(
            """SELECT e.*, p.name AS place_name FROM trip_expenses e
               LEFT JOIN places p ON p.id = e.place_id
               WHERE e.trip_id = ? ORDER BY e.expense_date DESC, e.id DESC""",
            (trip_id,),
        )
        totals = query_db(
            "SELECT currency, SUM(amount) AS total FROM trip_expenses WHERE trip_id = ? GROUP BY currency ORDER BY currency",
            (trip_id,),
        )
        spent_in_budget_currency = next(
            (row["total"] for row in totals if row["currency"] == trip["budget_currency"]), 0
        )
        return render_template(
            "expenses.html", trip=trip, expenses=expenses, totals=totals,
            spent_in_budget_currency=spent_in_budget_currency,
            remaining_budget=float(trip["budget_amount"]) - spent_in_budget_currency,
            currencies=("EUR", "USD", "KRW", "GBP", "CZK", "HUF", "CHF", "JPY"),
            categories=("숙박", "교통", "식비", "관광·티켓", "쇼핑", "기타"),
            today=date.today().isoformat(),
        )

    @app.post("/trips/<int:trip_id>/expenses/budget")
    def update_trip_budget(trip_id):
        if query_db("SELECT id FROM trips WHERE id = ?", (trip_id,), one=True) is None:
            abort(404)
        try:
            amount = float(request.form.get("budget_amount", "0"))
        except ValueError:
            amount = -1
        currency = request.form.get("budget_currency", "").upper()
        if not math.isfinite(amount) or amount < 0 or currency not in ("EUR", "USD", "KRW", "GBP", "CZK", "HUF", "CHF", "JPY"):
            flash("예산 금액과 통화를 확인해 주세요.", "error")
            return redirect(url_for("trip_expenses", trip_id=trip_id))
        db = get_db()
        db.execute("UPDATE trips SET budget_amount = ?, budget_currency = ? WHERE id = ?", (amount, currency, trip_id))
        db.commit()
        flash("여행 예산을 저장했습니다.", "success")
        return redirect(url_for("trip_expenses", trip_id=trip_id))

    @app.post("/trips/<int:trip_id>/expenses")
    def add_trip_expense(trip_id):
        if query_db("SELECT id FROM trips WHERE id = ?", (trip_id,), one=True) is None:
            abort(404)
        place_id_value = request.form.get("place_id", "").strip()
        place_day = None
        place_id = None
        if place_id_value:
            try:
                place_id = int(place_id_value)
            except ValueError:
                abort(400)
            requested_day_value = request.form.get("day_number", "").strip()
            if requested_day_value:
                try:
                    requested_day = int(requested_day_value)
                except ValueError:
                    abort(400)
                place_day = query_db(
                    """SELECT d.day_number, d.date FROM itinerary_items ii
                       JOIN itinerary_days d ON d.id = ii.day_id
                       JOIN places p ON p.id = ii.place_id
                       WHERE p.id = ? AND p.trip_id = ? AND d.day_number = ?""",
                    (place_id, trip_id, requested_day), one=True,
                )
            else:
                place_day = query_db(
                    "SELECT id FROM places WHERE id = ? AND trip_id = ?",
                    (place_id, trip_id), one=True,
                )
            if place_day is None:
                abort(404)
            place_day = dict(place_day)

        def expense_return():
            if place_day:
                if place_day.get("day_number"):
                    return redirect(url_for(
                        "itinerary_day_detail", trip_id=trip_id,
                        day_number=place_day["day_number"],
                    ))
                return redirect(url_for("place_detail", trip_id=trip_id, place_id=place_id))
            return redirect(url_for("trip_expenses", trip_id=trip_id))

        title = request.form.get("title", "").strip()
        category = request.form.get("category", "기타")
        expense_date = (
            place_day["date"] if place_day and place_day.get("date")
            else request.form.get("expense_date", "")
        )
        currency = request.form.get("currency", "EUR").upper()
        note = request.form.get("note", "").strip()
        allowed_currencies = {"EUR", "USD", "KRW", "GBP", "CZK", "HUF", "CHF", "JPY"}
        allowed_categories = {"숙박", "교통", "식비", "관광·티켓", "쇼핑", "기타"}
        try:
            amount = float(request.form.get("amount", ""))
            date.fromisoformat(expense_date)
        except (ValueError, TypeError):
            flash("날짜와 금액을 올바르게 입력해 주세요.", "error")
            return expense_return()
        if not title or not math.isfinite(amount) or amount < 0 or category not in allowed_categories or currency not in allowed_currencies:
            flash("지출 정보를 확인해 주세요.", "error")
            return expense_return()
        uploaded = request.files.get("attachment")
        original_name = None
        stored_name = None
        if uploaded and uploaded.filename:
            extension = Path(uploaded.filename).suffix.lower()
            if extension not in {".jpg", ".jpeg", ".png", ".webp", ".pdf", ".heic", ".heif"}:
                flash("영수증 또는 티켓은 JPG, PNG, WEBP, HEIC, HEIF, PDF 파일로 첨부해 주세요.", "error")
                return expense_return()
            safe_name = secure_filename(uploaded.filename)
            original_name = (
                safe_name if Path(safe_name).suffix.lower() == extension
                else f"첨부파일{extension}"
            )
            stored_name = f"{uuid.uuid4().hex}{extension}"
            uploaded.save(os.path.join(app.config["EXPENSE_UPLOAD_FOLDER"], stored_name))
        try:
            db = get_db()
            db.execute(
                """INSERT INTO trip_expenses
                   (trip_id, place_id, title, category, expense_date, amount, currency, note, original_file_name, stored_file_name)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (trip_id, place_id, title, category, expense_date, amount, currency, note, original_name, stored_name),
            )
            db.commit()
        except sqlite3.Error:
            get_db().rollback()
            if stored_name:
                os.remove(os.path.join(app.config["EXPENSE_UPLOAD_FOLDER"], stored_name))
            flash("지출 내역을 저장하지 못했습니다.", "error")
            return expense_return()
        flash("지출 내역을 저장했습니다.", "success")
        return expense_return()

    @app.get("/trips/<int:trip_id>/expenses/<int:expense_id>/attachment")
    def trip_expense_attachment(trip_id, expense_id):
        expense = query_db(
            "SELECT stored_file_name, original_file_name FROM trip_expenses WHERE id = ? AND trip_id = ?",
            (expense_id, trip_id), one=True,
        )
        if expense is None or not expense["stored_file_name"]:
            abort(404)
        return send_from_directory(
            app.config["EXPENSE_UPLOAD_FOLDER"], expense["stored_file_name"],
            download_name=expense["original_file_name"], as_attachment=False,
        )

    @app.post("/trips/<int:trip_id>/expenses/<int:expense_id>/delete")
    def delete_trip_expense(trip_id, expense_id):
        expense = query_db(
            """SELECT e.stored_file_name, e.place_id, d.day_number FROM trip_expenses e
               LEFT JOIN itinerary_items ii ON ii.place_id = e.place_id
               LEFT JOIN itinerary_days d ON d.id = ii.day_id
               WHERE e.id = ? AND e.trip_id = ? LIMIT 1""",
            (expense_id, trip_id), one=True,
        )
        if expense is None:
            abort(404)
        db = get_db()
        db.execute("DELETE FROM trip_expenses WHERE id = ? AND trip_id = ?", (expense_id, trip_id))
        db.commit()
        if expense["stored_file_name"]:
            file_path = os.path.join(app.config["EXPENSE_UPLOAD_FOLDER"], expense["stored_file_name"])
            if os.path.isfile(file_path):
                os.remove(file_path)
        flash("지출 내역을 삭제했습니다.", "success")
        if request.form.get("return_to_expenses") == "1":
            return redirect(url_for("trip_expenses", trip_id=trip_id))
        if request.form.get("return_to_place") == "1" and expense["place_id"]:
            return redirect(url_for(
                "place_detail", trip_id=trip_id, place_id=expense["place_id"]
            ))
        if expense["day_number"]:
            return redirect(url_for(
                "itinerary_day_detail", trip_id=trip_id,
                day_number=expense["day_number"],
            ))
        if expense["place_id"]:
            return redirect(url_for(
                "place_detail", trip_id=trip_id, place_id=expense["place_id"]
            ))
        return redirect(url_for("trip_expenses", trip_id=trip_id))

    @app.get("/trips/<int:trip_id>/itinerary")
    def trip_itinerary(trip_id):
        trip = query_db("SELECT * FROM trips WHERE id = ?", (trip_id,), one=True)
        if trip is None:
            abort(404)
        regions = query_db(
            "SELECT * FROM trip_regions WHERE trip_id = ? ORDER BY start_date, sort_order, id", (trip_id,)
        )
        try:
            selected_region_id = int(request.args.get("region_id", ""))
        except ValueError:
            selected_region_id = None
        selected_region = next((region for region in regions if region["id"] == selected_region_id), None)
        day_rows = query_db(
            "SELECT * FROM itinerary_days WHERE trip_id = ? ORDER BY day_number", (trip_id,)
        )
        if selected_region:
            region_day_ids = {
                row["id"] for row in query_db(
                    """SELECT DISTINCT d.id FROM itinerary_days d
                       JOIN itinerary_items ii ON ii.day_id = d.id
                       WHERE d.trip_id = ? AND ii.region_id = ?""",
                    (trip_id, selected_region["id"]),
                )
            }
            region_day_ids.update(
                row["day_id"] for row in query_db(
                    """SELECT DISTINCT day_id FROM itinerary_transfers
                       WHERE trip_id = ? AND (from_region_id = ? OR to_region_id = ?)""",
                    (trip_id, selected_region["id"], selected_region["id"]),
                )
            )
            day_rows = [
                day for day in day_rows
                if selected_region["start_date"] <= day["date"] <= selected_region["end_date"]
                or day["id"] in region_day_ids
            ]
        accommodations = query_db(
            "SELECT * FROM accommodations WHERE trip_id = ? ORDER BY check_in_date, id", (trip_id,)
        )
        if selected_region:
            accommodations = [stay for stay in accommodations if stay["region_id"] == selected_region["id"]]
            places = query_db(
                "SELECT * FROM places WHERE trip_id = ? AND region_id = ? ORDER BY name COLLATE NOCASE",
                (trip_id, selected_region["id"]),
            )
        else:
            places = query_db("SELECT * FROM places WHERE trip_id = ? ORDER BY name COLLATE NOCASE", (trip_id,))
        place_days = {}
        place_sequences = {}
        days = []
        for day in day_rows:
            items = query_db(
                """SELECT ii.id AS itinerary_item_id, ii.sequence, ii.start_time, ii.is_locked, ii.region_id,
                          p.id AS place_id, p.name, p.address,
                          p.latitude, p.longitude, p.category
                   FROM itinerary_items AS ii JOIN places AS p ON p.id = ii.place_id
                   WHERE ii.day_id = ?""" + (" AND ii.region_id = ?" if selected_region else "") + " ORDER BY ii.sequence",
                (day["id"], selected_region["id"]) if selected_region else (day["id"],),
            )
            items = [{**dict(item), "transfers_before": [], "transfers_after": []} for item in items]
            transfers = query_db(
                """SELECT it.*, fr.label AS from_label, tr.label AS to_label
                   FROM itinerary_transfers it
                   LEFT JOIN trip_regions fr ON fr.id = it.from_region_id
                   LEFT JOIN trip_regions tr ON tr.id = it.to_region_id
                   WHERE it.day_id = ? ORDER BY it.id""",
                (day["id"],),
            )
            items_by_id = {item["itinerary_item_id"]: item for item in items}
            checkout_stays = [stay for stay in accommodations if stay["check_out_date"] == day["date"]]
            checkin_stays = [stay for stay in accommodations if stay["check_in_date"] == day["date"]]
            attached_checkout_ids = set()
            attached_checkin_ids = set()
            day_transfers_after = []
            for transfer in transfers:
                if selected_region and selected_region["id"] not in (transfer["from_region_id"], transfer["to_region_id"]):
                    continue
                transfer = {**dict(transfer), "checkout_stays": [], "checkin_stays": []}
                for stay in checkout_stays:
                    matches_origin = (
                        transfer["from_region_id"] is not None
                        and stay["region_id"] == transfer["from_region_id"]
                    ) or (
                        transfer["from_region_id"] is None and len(transfers) == 1
                    )
                    if matches_origin and stay["id"] not in attached_checkout_ids:
                        transfer["checkout_stays"].append(stay)
                        attached_checkout_ids.add(stay["id"])
                for stay in checkin_stays:
                    matches_destination = (
                        transfer["to_region_id"] is not None
                        and stay["region_id"] == transfer["to_region_id"]
                    ) or (
                        transfer["to_region_id"] is None and len(transfers) == 1
                    )
                    if matches_destination and stay["id"] not in attached_checkin_ids:
                        transfer["checkin_stays"].append(stay)
                        attached_checkin_ids.add(stay["id"])
                if transfer["before_item_id"] in items_by_id:
                    items_by_id[transfer["before_item_id"]]["transfers_before"].append(transfer)
                elif items and selected_region and transfer["from_region_id"] == selected_region["id"]:
                    items[-1]["transfers_after"].append(transfer)
                elif items and selected_region and transfer["to_region_id"] == selected_region["id"]:
                    items[0]["transfers_before"].append(transfer)
                else:
                    day_transfers_after.append(transfer)
            unmatched_checkout_stays = [stay for stay in checkout_stays if stay["id"] not in attached_checkout_ids]
            unmatched_checkin_stays = [stay for stay in checkin_stays if stay["id"] not in attached_checkin_ids]
            timeline = []
            for stay in unmatched_checkout_stays:
                timeline.append({
                    "kind": "stay", "key": f"stay-checkout:{stay['id']}", "stay": stay,
                    "stay_event_type": "체크아웃", "stay_event_time": stay["check_out_time"],
                })
            for item in items:
                for transfer in item["transfers_before"]:
                    for stay in transfer["checkout_stays"]:
                        timeline.append({
                            "kind": "stay", "key": f"stay-checkout:{stay['id']}", "stay": stay,
                            "stay_event_type": "체크아웃", "stay_event_time": stay["check_out_time"],
                        })
                    timeline.append({
                        "kind": "transfer", "key": f"transfer:{transfer['id']}", "transfer": transfer,
                    })
                    for stay in transfer["checkin_stays"]:
                        timeline.append({
                            "kind": "stay", "key": f"stay-checkin:{stay['id']}", "stay": stay,
                            "stay_event_type": "체크인", "stay_event_time": stay["check_in_time"],
                        })
                timeline.append({"kind": "place", "key": f"place:{item['place_id']}", "item": item})
                for transfer in item["transfers_after"]:
                    for stay in transfer["checkout_stays"]:
                        timeline.append({
                            "kind": "stay", "key": f"stay-checkout:{stay['id']}", "stay": stay,
                            "stay_event_type": "체크아웃", "stay_event_time": stay["check_out_time"],
                        })
                    timeline.append({
                        "kind": "transfer", "key": f"transfer:{transfer['id']}", "transfer": transfer,
                    })
                    for stay in transfer["checkin_stays"]:
                        timeline.append({
                            "kind": "stay", "key": f"stay-checkin:{stay['id']}", "stay": stay,
                            "stay_event_type": "체크인", "stay_event_time": stay["check_in_time"],
                        })
            for transfer in day_transfers_after:
                for stay in transfer["checkout_stays"]:
                    timeline.append({
                        "kind": "stay", "key": f"stay-checkout:{stay['id']}", "stay": stay,
                        "stay_event_type": "체크아웃", "stay_event_time": stay["check_out_time"],
                    })
                timeline.append({
                    "kind": "transfer", "key": f"transfer:{transfer['id']}", "transfer": transfer,
                })
                for stay in transfer["checkin_stays"]:
                    timeline.append({
                        "kind": "stay", "key": f"stay-checkin:{stay['id']}", "stay": stay,
                        "stay_event_type": "체크인", "stay_event_time": stay["check_in_time"],
                    })
            for stay in unmatched_checkin_stays:
                timeline.append({
                    "kind": "stay", "key": f"stay-checkin:{stay['id']}", "stay": stay,
                    "stay_event_type": "체크인", "stay_event_time": stay["check_in_time"],
                })
            timeline = _order_itinerary_events(
                get_db(), trip_id, day["id"], selected_region["id"] if selected_region else None, timeline
            )
            place_days.update({item["place_id"]: day["day_number"] for item in items})
            place_sequences.update({item["place_id"]: item["sequence"] for item in items})
            days.append({
                "day_number": day["day_number"],
                "date": day["date"],
                "items": items,
                "timeline": timeline,
                "accommodation_status": _accommodation_status(accommodations, day["date"]),
                "transfers_after": day_transfers_after,
            })
        scheduled_place_ids = set(place_days)
        unscheduled_places = [place for place in places if place["id"] not in scheduled_place_ids]
        map_points = _map_points(accommodations, places, place_days, place_sequences)
        for point in map_points:
            if point["kind"] == "accommodation":
                point["stay_day_numbers"] = [
                    day["day_number"] for day in days
                    if point["check_in_date"] <= day["date"] <= point["check_out_date"]
                ] if point["check_in_date"] and point["check_out_date"] else []
        transport_legs = query_db(
            "SELECT * FROM trip_transport_legs WHERE trip_id = ? ORDER BY departure_at, id",
            (trip_id,),
        )
        settings = _get_recommendation_settings(trip_id)
        day_availability = _build_day_availability(trip, transport_legs, settings)
        for day in days:
            day.update(day_availability[day["day_number"]])
        return render_template(
            "itinerary.html", trip=trip, days=days, has_itinerary=bool(day_rows),
            accommodations=accommodations, regions=regions, selected_region=selected_region,
            map_points=map_points,
            map_total=len(places) + sum(1 for stay in accommodations if stay["latitude"] is not None),
            map_days=[
                {"day_number": day["day_number"], "date": day["date"], "place_count": len(day["items"])}
                for day in days
            ],
            maps_enabled=bool(app.config.get("GOOGLE_MAPS_BROWSER_API_KEY")),
            night_count=(date.fromisoformat(trip["end_date"]) - date.fromisoformat(trip["start_date"])).days,
            day_count=(date.fromisoformat(trip["end_date"]) - date.fromisoformat(trip["start_date"])).days + 1,
            unscheduled_places=unscheduled_places,
            recommendation_settings=settings,
        )

    @app.post("/trips/<int:trip_id>/recommend-itinerary")
    def recommend_trip_itinerary(trip_id):
        trip = query_db("SELECT * FROM trips WHERE id = ?", (trip_id,), one=True)
        if trip is None:
            abort(404)
        if query_db("SELECT 1 FROM trip_regions WHERE trip_id = ? LIMIT 1", (trip_id,), one=True):
            flash("국가별 구간이 있는 여행은 해당 국가를 선택해 일정을 추천해 주세요.", "error")
            return redirect(url_for("trip_itinerary", trip_id=trip_id))
        places = query_db(
            "SELECT * FROM places WHERE trip_id = ? ORDER BY id", (trip_id,)
        )
        if not places:
            flash("먼저 가고 싶은 장소를 등록해 주세요.", "error")
            return redirect(url_for("trip_places", trip_id=trip_id))
        accommodations = query_db(
            "SELECT * FROM accommodations WHERE trip_id = ? ORDER BY check_in_date, id", (trip_id,)
        )
        start = date.fromisoformat(trip["start_date"])
        end = date.fromisoformat(trip["end_date"])
        day_count = (end - start).days + 1
        transport_legs = query_db(
            "SELECT * FROM trip_transport_legs WHERE trip_id = ? ORDER BY departure_at, id",
            (trip_id,),
        )
        day_availability = _build_day_availability(
            trip, transport_legs, _get_recommendation_settings(trip_id)
        )
        days = recommend_itinerary(
            places, accommodations, trip["start_date"], day_count, day_availability
        )

        db = get_db()
        try:
            db.execute("DELETE FROM itinerary_days WHERE trip_id = ?", (trip_id,))
            for day in days:
                cursor = db.execute(
                    "INSERT INTO itinerary_days (trip_id, day_number, date) VALUES (?, ?, ?)",
                    (trip_id, day["day_number"], day["date"]),
                )
                day_id = cursor.lastrowid
                for sequence, place in enumerate(day["places"], 1):
                    db.execute(
                        """INSERT INTO itinerary_items (day_id, place_id, sequence, start_time)
                           VALUES (?, ?, ?, NULL)""",
                        (day_id, place["id"], sequence),
                    )
            db.commit()
        except sqlite3.Error:
            db.rollback()
            flash("일정을 저장하지 못했습니다. 다시 시도해 주세요.", "error")
            return redirect(url_for("trip_itinerary", trip_id=trip_id))
        if not any(day.get("is_available", True) for day in days):
            flash("교통편 여유 시간을 고려해 모든 날짜를 비워 두었습니다. 장소는 날짜 미정으로 남았습니다.", "success")
        else:
            flash("여행 일정을 날짜별로 추천하고 저장했습니다.", "success")
        return redirect(url_for("trip_itinerary", trip_id=trip_id))

    @app.post("/trips/<int:trip_id>/regions/<int:region_id>/recommend-itinerary")
    def recommend_region_itinerary(trip_id, region_id):
        trip = query_db("SELECT * FROM trips WHERE id = ?", (trip_id,), one=True)
        region = query_db(
            "SELECT * FROM trip_regions WHERE id = ? AND trip_id = ?",
            (region_id, trip_id), one=True,
        )
        if trip is None or region is None:
            abort(404)
        return_to_region = redirect(url_for("trip_itinerary", trip_id=trip_id, region_id=region_id))
        places = query_db(
            "SELECT * FROM places WHERE trip_id = ? AND region_id = ? ORDER BY id",
            (trip_id, region_id),
        )
        if not places:
            flash(f"{region['label']}에 등록된 장소가 없습니다.", "error")
            return return_to_region

        trip_start = date.fromisoformat(trip["start_date"])
        trip_end = date.fromisoformat(trip["end_date"])
        region_start = max(date.fromisoformat(region["start_date"]), trip_start)
        region_end = min(date.fromisoformat(region["end_date"]), trip_end)
        if region_end < region_start:
            flash(f"{region['label']}의 여행 날짜를 확인해 주세요.", "error")
            return return_to_region

        trip_days = query_db(
            "SELECT id, day_number, date FROM itinerary_days WHERE trip_id = ? ORDER BY day_number",
            (trip_id,),
        )
        if not trip_days:
            db = get_db()
            for offset in range((trip_end - trip_start).days + 1):
                current_date = (trip_start + timedelta(days=offset)).isoformat()
                db.execute(
                    "INSERT INTO itinerary_days (trip_id, day_number, date) VALUES (?, ?, ?)",
                    (trip_id, offset + 1, current_date),
                )
            db.commit()
            trip_days = query_db(
                "SELECT id, day_number, date FROM itinerary_days WHERE trip_id = ? ORDER BY day_number",
                (trip_id,),
            )
        day_by_date = {day["date"]: day for day in trip_days}
        region_day_count = (region_end - region_start).days + 1
        if any((region_start + timedelta(days=offset)).isoformat() not in day_by_date for offset in range(region_day_count)):
            flash("여행 날짜별 일정표가 없어 국가 일정을 다시 추천하지 못했습니다.", "error")
            return return_to_region

        transfer_rows = query_db(
            """SELECT DISTINCT d.date FROM itinerary_transfers t
               JOIN itinerary_days d ON d.id = t.day_id
               WHERE t.trip_id = ?""",
            (trip_id,),
        )
        transfer_dates = {row["date"] for row in transfer_rows}
        existing_region_items = query_db(
            """SELECT ii.place_id, d.date FROM itinerary_items ii
               JOIN places p ON p.id = ii.place_id
               JOIN itinerary_days d ON d.id = ii.day_id
               WHERE d.trip_id = ? AND p.region_id = ?""",
            (trip_id, region_id),
        )
        fixed_place_ids = {
            row["place_id"] for row in existing_region_items
            if row["date"] in transfer_dates
            or row["date"] < region_start.isoformat()
            or row["date"] > region_end.isoformat()
        }
        movable_places = [place for place in places if place["id"] not in fixed_place_ids]
        if not movable_places:
            flash(f"{region['label']}의 장소는 모두 국가 간 이동일에 있어 기존 일정을 유지했습니다.", "success")
            return return_to_region

        transport_legs = query_db(
            "SELECT * FROM trip_transport_legs WHERE trip_id = ? ORDER BY departure_at, id",
            (trip_id,),
        )
        daily_availability = _build_day_availability(
            trip, transport_legs, _get_recommendation_settings(trip_id)
        )
        availability = {}
        for offset in range(region_day_count):
            current_date = (region_start + timedelta(days=offset)).isoformat()
            local_day_number = offset + 1
            global_day_number = day_by_date[current_date]["day_number"]
            if current_date in transfer_dates:
                availability[local_day_number] = {
                    "is_available": False,
                    "blocked_reason": "국가 간 이동이 있는 날은 기존 일정을 유지합니다.",
                    "available_hours": 0,
                }
            else:
                availability[local_day_number] = daily_availability.get(global_day_number, {})
        if not any(day.get("is_available", True) for day in availability.values()):
            flash("이동일과 교통편 시간을 제외하면 추천할 수 있는 날이 없어 기존 일정을 유지했습니다.", "error")
            return return_to_region

        region_stays = query_db(
            "SELECT * FROM accommodations WHERE trip_id = ? AND region_id = ? ORDER BY check_in_date, id",
            (trip_id, region_id),
        )
        midpoint = region_start + timedelta(days=(region_end - region_start).days // 2)
        active_stay = next((
            stay for stay in region_stays
            if stay["check_in_date"] and stay["check_out_date"]
            and stay["check_in_date"] <= midpoint.isoformat() < stay["check_out_date"]
            and stay["latitude"] is not None and stay["longitude"] is not None
        ), None)
        recommendation = recommend_itinerary(
            movable_places, active_stay, region_start.isoformat(), region_day_count, availability
        )

        replace_day_ids = [
            day_by_date[(region_start + timedelta(days=offset)).isoformat()]["id"]
            for offset in range(region_day_count)
            if (region_start + timedelta(days=offset)).isoformat() not in transfer_dates
        ]
        movable_place_ids = [place["id"] for place in movable_places]
        db = get_db()
        try:
            if replace_day_ids and movable_place_ids:
                day_placeholders = ",".join("?" for _ in replace_day_ids)
                place_placeholders = ",".join("?" for _ in movable_place_ids)
                db.execute(
                    f"DELETE FROM itinerary_items WHERE day_id IN ({day_placeholders}) AND place_id IN ({place_placeholders})",
                    (*replace_day_ids, *movable_place_ids),
                )
            if replace_day_ids:
                placeholders = ",".join("?" for _ in replace_day_ids)
                db.execute(
                    f"DELETE FROM itinerary_event_order WHERE trip_id = ? AND day_id IN ({placeholders})",
                    (trip_id, *replace_day_ids),
                )
            for day in recommendation:
                day_row = day_by_date[day["date"]]
                next_sequence = db.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM itinerary_items WHERE day_id = ?",
                    (day_row["id"],),
                ).fetchone()[0]
                for sequence, place in enumerate(day["places"], 1):
                    db.execute(
                        """INSERT INTO itinerary_items (day_id, place_id, sequence, start_time, region_id)
                           VALUES (?, ?, ?, NULL, ?)""",
                        (day_row["id"], place["id"], next_sequence + sequence - 1, region_id),
                    )
            db.commit()
        except sqlite3.Error:
            db.rollback()
            flash(f"{region['label']} 일정을 다시 추천하지 못했습니다. 기존 일정은 유지됩니다.", "error")
            return return_to_region

        flash(f"{region['label']} 일정만 다시 추천했습니다. 다른 국가와 국가 간 이동일은 유지했습니다.", "success")
        return return_to_region

    @app.post("/trips/<int:trip_id>/recommendation-settings")
    def save_recommendation_settings(trip_id):
        if query_db("SELECT id FROM trips WHERE id = ?", (trip_id,), one=True) is None:
            abort(404)
        try:
            flight_buffer = float(request.form.get("flight_buffer_hours", "4"))
            train_buffer = float(request.form.get("train_buffer_hours", "2"))
            bus_buffer = float(request.form.get("bus_buffer_hours", "2"))
            if not (0 <= flight_buffer <= 12 and 0 <= train_buffer <= 12 and 0 <= bus_buffer <= 12):
                raise ValueError
            day_start = datetime.strptime(request.form.get("day_start_time", ""), "%H:%M").time()
            day_end = datetime.strptime(request.form.get("day_end_time", ""), "%H:%M").time()
            arrival_limit = datetime.strptime(request.form.get("arrival_latest_start", ""), "%H:%M").time()
            departure_limit = datetime.strptime(request.form.get("departure_earliest_cutoff", ""), "%H:%M").time()
            if day_start >= day_end or not (day_start <= arrival_limit <= day_end) or not (day_start <= departure_limit <= day_end):
                raise ValueError
        except (TypeError, ValueError):
            flash("시간을 확인해 주세요. 하루 추천 시작은 종료보다 앞서야 하며 도착·출발 기준 시각은 추천 시간 안에 있어야 합니다.", "error")
            return redirect(url_for("trip_itinerary", trip_id=trip_id))
        get_db().execute(
            """INSERT INTO trip_recommendation_settings
               (trip_id, flight_buffer_hours, train_buffer_hours, bus_buffer_hours, day_start_time, day_end_time,
                arrival_latest_start, departure_earliest_cutoff)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(trip_id) DO UPDATE SET
                flight_buffer_hours=excluded.flight_buffer_hours,
                train_buffer_hours=excluded.train_buffer_hours,
                bus_buffer_hours=excluded.bus_buffer_hours,
                day_start_time=excluded.day_start_time,
                day_end_time=excluded.day_end_time,
                arrival_latest_start=excluded.arrival_latest_start,
                departure_earliest_cutoff=excluded.departure_earliest_cutoff""",
            (trip_id, flight_buffer, train_buffer, bus_buffer, day_start.strftime("%H:%M"),
             day_end.strftime("%H:%M"), arrival_limit.strftime("%H:%M"),
             departure_limit.strftime("%H:%M")),
        )
        get_db().commit()
        flash("일정 추천 설정을 저장했습니다. '일정 다시 추천'을 누르면 적용됩니다.", "success")
        return redirect(url_for("trip_itinerary", trip_id=trip_id))

    @app.get("/trips/<int:trip_id>/itinerary/day/<int:day_number>")
    def itinerary_day_detail(trip_id, day_number):
        trip = query_db("SELECT * FROM trips WHERE id = ?", (trip_id,), one=True)
        if trip is None:
            abort(404)
        day = query_db(
            "SELECT * FROM itinerary_days WHERE trip_id = ? AND day_number = ?",
            (trip_id, day_number), one=True,
        )
        if day is None:
            abort(404)
        items = query_db(
            """SELECT p.*, ii.id AS itinerary_item_id, ii.sequence, ii.is_locked, ii.region_id
               FROM itinerary_items AS ii JOIN places AS p ON p.id = ii.place_id
               WHERE ii.day_id = ? ORDER BY ii.sequence""",
            (day["id"],),
        )
        items = [{**dict(item), "transfers_before": [], "transfers_after": []} for item in items]
        accommodations = query_db(
            "SELECT * FROM accommodations WHERE trip_id = ? ORDER BY check_in_date, id", (trip_id,)
        )
        checkout_stays = [stay for stay in accommodations if stay["check_out_date"] == day["date"]]
        checkin_stays = [stay for stay in accommodations if stay["check_in_date"] == day["date"]]
        attached_checkout_ids = set()
        attached_checkin_ids = set()
        transfers = query_db(
            """SELECT it.*, fr.label AS from_label, tr.label AS to_label
               FROM itinerary_transfers it
               LEFT JOIN trip_regions fr ON fr.id = it.from_region_id
               LEFT JOIN trip_regions tr ON tr.id = it.to_region_id
               WHERE it.day_id = ? ORDER BY it.id""",
            (day["id"],),
        )
        items_by_id = {item["itinerary_item_id"]: item for item in items}
        transfers_after = []
        for transfer in transfers:
            transfer = {**dict(transfer), "checkout_stays": [], "checkin_stays": []}
            for stay in checkout_stays:
                matches_origin = (
                    transfer["from_region_id"] is not None
                    and stay["region_id"] == transfer["from_region_id"]
                ) or (transfer["from_region_id"] is None and len(transfers) == 1)
                if matches_origin and stay["id"] not in attached_checkout_ids:
                    transfer["checkout_stays"].append(stay)
                    attached_checkout_ids.add(stay["id"])
            for stay in checkin_stays:
                matches_destination = (
                    transfer["to_region_id"] is not None
                    and stay["region_id"] == transfer["to_region_id"]
                ) or (transfer["to_region_id"] is None and len(transfers) == 1)
                if matches_destination and stay["id"] not in attached_checkin_ids:
                    transfer["checkin_stays"].append(stay)
                    attached_checkin_ids.add(stay["id"])
            if transfer["before_item_id"] in items_by_id:
                items_by_id[transfer["before_item_id"]]["transfers_before"].append(transfer)
            else:
                transfers_after.append(transfer)
        timeline = []
        unmatched_checkout_stays = [stay for stay in checkout_stays if stay["id"] not in attached_checkout_ids]
        unmatched_checkin_stays = [stay for stay in checkin_stays if stay["id"] not in attached_checkin_ids]
        for stay in unmatched_checkout_stays:
            timeline.append({
                "kind": "stay", "key": f"stay-checkout:{stay['id']}", "stay": stay,
                "stay_event_type": "체크아웃", "stay_event_time": stay["check_out_time"],
            })

        def add_transfer_events(transfer):
            for stay in transfer["checkout_stays"]:
                timeline.append({
                    "kind": "stay", "key": f"stay-checkout:{stay['id']}", "stay": stay,
                    "stay_event_type": "체크아웃", "stay_event_time": stay["check_out_time"],
                })
            timeline.append({"kind": "transfer", "key": f"transfer:{transfer['id']}", "transfer": transfer})
            for stay in transfer["checkin_stays"]:
                timeline.append({
                    "kind": "stay", "key": f"stay-checkin:{stay['id']}", "stay": stay,
                    "stay_event_type": "체크인", "stay_event_time": stay["check_in_time"],
                })

        for item in items:
            for transfer in item["transfers_before"]:
                add_transfer_events(transfer)
            timeline.append({"kind": "place", "key": f"place:{item['id']}", "item": item})
            for transfer in item["transfers_after"]:
                add_transfer_events(transfer)
        for transfer in transfers_after:
            add_transfer_events(transfer)
        for stay in unmatched_checkin_stays:
            timeline.append({
                "kind": "stay", "key": f"stay-checkin:{stay['id']}", "stay": stay,
                "stay_event_type": "체크인", "stay_event_time": stay["check_in_time"],
            })
        timeline = _order_itinerary_events(get_db(), trip_id, day["id"], None, timeline)
        place_expenses = query_db(
            """SELECT e.* FROM trip_expenses e
               JOIN itinerary_items ii ON ii.place_id = e.place_id
               WHERE e.trip_id = ? AND ii.day_id = ?
               ORDER BY e.expense_date, e.id""",
            (trip_id, day["id"]),
        )
        expenses_by_place = {}
        for expense in place_expenses:
            expenses_by_place.setdefault(expense["place_id"], []).append(expense)
        has_regions = query_db(
            "SELECT 1 FROM trip_regions WHERE trip_id = ? LIMIT 1", (trip_id,), one=True
        ) is not None
        transport_legs = query_db(
            "SELECT * FROM trip_transport_legs WHERE trip_id = ? ORDER BY departure_at, id",
            (trip_id,),
        )
        availability = _build_day_availability(
            trip, transport_legs, _get_recommendation_settings(trip_id)
        )[day_number]
        place_days = {item["id"]: day_number for item in items}
        place_sequences = {item["id"]: index for index, item in enumerate(items, 1)}
        accommodation_status = _accommodation_status(accommodations, day["date"])
        map_accommodations = [
            stay for stay in accommodations
            if _accommodation_status(stay, day["date"])
        ]
        return render_template(
            "day_detail.html", trip=trip, day=day, day_number=day_number,
            items=items, accommodations=accommodations, accommodation_status=accommodation_status,
            transfers_after=transfers_after, timeline=timeline,
            has_regions=has_regions,
            expenses_by_place=expenses_by_place,
            expense_categories=("숙박", "교통", "식비", "관광·티켓", "쇼핑", "기타"),
            currencies=("EUR", "USD", "KRW", "GBP", "CZK", "HUF", "CHF", "JPY"),
            budget_currency=trip["budget_currency"],
            day_availability=availability,
            map_points=_map_points(map_accommodations, items, place_days, place_sequences),
            map_total=len(items) + sum(1 for stay in map_accommodations if stay["latitude"] is not None),
            maps_enabled=bool(app.config.get("GOOGLE_MAPS_BROWSER_API_KEY")),
        )

    @app.post("/trips/<int:trip_id>/itinerary/day/<int:day_number>/optimize")
    def optimize_itinerary_day(trip_id, day_number):
        day = query_db(
            "SELECT id, date FROM itinerary_days WHERE trip_id = ? AND day_number = ?",
            (trip_id, day_number), one=True,
        )
        if day is None:
            abort(404)
        places = query_db(
            """SELECT p.*, ii.is_locked, ii.sequence FROM itinerary_items AS ii
               JOIN places AS p ON p.id = ii.place_id
               WHERE ii.day_id = ? ORDER BY ii.sequence""",
            (day["id"],),
        )
        if len(places) < 2:
            flash("순서를 추천할 장소가 충분하지 않습니다.", "error")
            return _itinerary_day_redirect(trip_id, day_number, request.form.get("return_to_day") == "1")
        accommodations = query_db(
            "SELECT * FROM accommodations WHERE trip_id = ? ORDER BY check_in_date, id", (trip_id,)
        )
        accommodation = _accommodation_for_date(accommodations, day["date"])
        locked_place_ids = {place["id"] for place in places if place["is_locked"]}
        ordered_places = optimize_day_order(places, accommodation, locked_place_ids)
        db = get_db()
        try:
            db.execute(
                "UPDATE itinerary_items SET sequence = sequence + 100000 WHERE day_id = ?",
                (day["id"],),
            )
            for sequence, place in enumerate(ordered_places, 1):
                db.execute(
                    "UPDATE itinerary_items SET sequence = ? WHERE day_id = ? AND place_id = ?",
                    (sequence, day["id"], place["id"]),
                )
            db.execute(
                "DELETE FROM itinerary_event_order WHERE trip_id = ? AND day_id = ?",
                (trip_id, day["id"]),
            )
            db.commit()
        except sqlite3.Error:
            db.rollback()
            flash("세부 일정 순서를 저장하지 못했습니다.", "error")
            return _itinerary_day_redirect(trip_id, day_number, request.form.get("return_to_day") == "1")
        flash(f"Day {day_number} 방문 순서를 추천해 적용했습니다.", "success")
        return _itinerary_day_redirect(trip_id, day_number, request.form.get("return_to_day") == "1")

    @app.post("/trips/<int:trip_id>/itinerary/places/<int:place_id>/toggle-lock")
    def toggle_itinerary_place_lock(trip_id, place_id):
        item = query_db(
            """SELECT ii.id, ii.is_locked, d.day_number FROM itinerary_items AS ii
               JOIN itinerary_days AS d ON d.id = ii.day_id
               WHERE d.trip_id = ? AND ii.place_id = ?""",
            (trip_id, place_id), one=True,
        )
        if item is None:
            abort(404)
        locked = 0 if item["is_locked"] else 1
        get_db().execute(
            "UPDATE itinerary_items SET is_locked = ? WHERE id = ?",
            (locked, item["id"]),
        )
        get_db().commit()
        flash("방문 순서를 고정했습니다." if locked else "방문 순서 고정을 해제했습니다.", "success")
        if request.form.get("return_to_day") == "1":
            return redirect(url_for("itinerary_day_detail", trip_id=trip_id, day_number=item["day_number"]))
        return redirect(url_for("trip_itinerary", trip_id=trip_id))

    @app.post("/trips/<int:trip_id>/place-search")
    def search_trip_places(trip_id):
        trip = query_db("SELECT * FROM trips WHERE id = ?", (trip_id,), one=True)
        if trip is None:
            abort(404)
        query = request.form.get("query", "").strip()
        target = request.form.get("target", "places")
        category = request.form.get("category", "attraction")
        selected_region_id = request.form.get("region_id", "").strip()
        selected_region = None
        if selected_region_id:
            try:
                selected_region = query_db(
                    "SELECT id, label FROM trip_regions WHERE id = ? AND trip_id = ?",
                    (int(selected_region_id), trip_id), one=True,
                )
            except ValueError:
                selected_region = None
            if selected_region is None:
                selected_region_id = ""
        if category not in ("attraction", "restaurant"):
            category = "attraction"
        place_results, accommodation_results = [], []
        if not query:
            flash("검색할 장소 이름을 입력해 주세요.", "error")
        else:
            try:
                results = search_places(
                    app.config["GOOGLE_MAPS_API_KEY"], query,
                    selected_region["label"] if selected_region else trip["city"],
                )
                if target == "accommodation":
                    accommodation_results = results
                else:
                    place_results = results
                if not results:
                    flash("검색 결과가 없습니다. 다른 검색어를 입력해 주세요.", "error")
            except PlaceSearchError as error:
                flash(str(error), "error")
        if target == "accommodation":
            accommodations = query_db(
                "SELECT * FROM accommodations WHERE trip_id = ? ORDER BY check_in_date, id", (trip_id,)
            )
            accommodation_id = request.form.get("accommodation_id", "")
            editing_accommodation = next(
                (stay for stay in accommodations if str(stay["id"]) == accommodation_id), None
            )
            return render_template(
                "accommodation.html", trip=trip, accommodations=accommodations,
                accommodation_search_results=accommodation_results,
                accommodation_search_query=query,
                editing_accommodation=editing_accommodation,
                editing_accommodation_id=accommodation_id,
                regions=query_db("SELECT id, label FROM trip_regions WHERE trip_id = ? ORDER BY start_date, sort_order, id", (trip_id,)),
                selected_region_id=selected_region_id,
            )
        regions = query_db("SELECT id, label FROM trip_regions WHERE trip_id = ? ORDER BY start_date, sort_order, id", (trip_id,))
        return render_template(
            "places.html", trip=trip, places=_trip_places(trip_id),
            place_search_results=place_results, place_search_query=query,
            place_search_category=category,
            regions=regions, selected_region_id=selected_region_id,
        )

    @app.post("/trips")
    def create_trip():
        title = request.form.get("title", "").strip()
        city = request.form.get("city", "").strip()
        start_date = request.form.get("start_date", "")
        end_date = request.form.get("end_date", "")
        if not title or not city or not start_date or not end_date:
            flash("여행 이름, 도시, 시작일과 종료일을 모두 입력해 주세요.", "error")
            return redirect(url_for("index"))
        try:
            start = date.fromisoformat(start_date)
            end = date.fromisoformat(end_date)
        except ValueError:
            flash("날짜 형식을 확인해 주세요.", "error")
            return redirect(url_for("index"))
        if end < start:
            flash("종료일은 시작일보다 빠를 수 없습니다.", "error")
            return redirect(url_for("index"))

        connection = get_db()
        connection.execute(
            "INSERT INTO trips (user_id, title, city, start_date, end_date) VALUES (?, ?, ?, ?, ?)",
            (session["user_id"], title, city, start_date, end_date),
        )
        trip_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute(
            "INSERT INTO trip_members (trip_id, user_id, role) VALUES (?, ?, 'owner')",
            (trip_id, session["user_id"]),
        )
        connection.commit()
        flash(f"‘{title}’ 여행을 만들었습니다.", "success")
        return redirect(url_for("index"))

    @app.post("/trips/<int:trip_id>/delete")
    def delete_trip(trip_id):
        if g.trip_role != "owner":
            abort(403)
        photos = query_db(
            "SELECT stored_file_name FROM place_photos WHERE trip_id = ?", (trip_id,)
        )
        connection = get_db()
        cursor = connection.execute("DELETE FROM trips WHERE id = ?", (trip_id,))
        connection.commit()
        if cursor.rowcount == 0:
            abort(404)
        for photo in photos:
            file_path = os.path.join(app.config["PLACE_PHOTO_UPLOAD_FOLDER"], photo["stored_file_name"])
            if os.path.isfile(file_path):
                os.remove(file_path)
        flash("여행을 삭제했습니다.", "success")
        return redirect(url_for("index"))

    @app.post("/trips/<int:trip_id>/dates")
    def update_trip_dates(trip_id):
        trip = query_db("SELECT * FROM trips WHERE id = ?", (trip_id,), one=True)
        if trip is None:
            abort(404)
        old_trip_start = date.fromisoformat(trip["start_date"])
        try:
            start = date.fromisoformat(request.form.get("start_date", ""))
            end = date.fromisoformat(request.form.get("end_date", ""))
        except ValueError:
            flash("여행 시작일과 종료일을 확인해 주세요.", "error")
            return redirect(url_for("trip_detail", trip_id=trip_id))
        if end < start:
            flash("종료일은 시작일보다 빠를 수 없습니다.", "error")
            return redirect(url_for("trip_detail", trip_id=trip_id))

        accommodations = query_db(
            "SELECT name, check_in_date, check_out_date FROM accommodations WHERE trip_id = ?",
            (trip_id,),
        )
        for accommodation in accommodations:
            if not accommodation["check_in_date"] or not accommodation["check_out_date"]:
                continue
            check_in = date.fromisoformat(accommodation["check_in_date"])
            check_out = date.fromisoformat(accommodation["check_out_date"])
            if check_in < start or check_out > end:
                flash(f"{accommodation['name']} 숙소의 체크인·체크아웃 날짜가 여행 기간 밖입니다. 숙소 날짜를 먼저 수정해 주세요.", "error")
                return redirect(url_for("trip_detail", trip_id=trip_id))
        transport_legs = query_db(
            "SELECT departure_at, arrival_at FROM trip_transport_legs WHERE trip_id = ?",
            (trip_id,),
        )
        for leg in transport_legs:
            departure_date = date.fromisoformat(leg["departure_at"][:10])
            arrival_date = date.fromisoformat(leg["arrival_at"][:10])
            if not (start <= departure_date <= end or start <= arrival_date <= end):
                flash("교통편의 출발일과 도착일이 모두 여행 기간 밖입니다. 여행 기간이나 교통편을 먼저 수정해 주세요.", "error")
                return redirect(url_for("trip_detail", trip_id=trip_id))

        day_count = (end - start).days + 1
        db = get_db()
        try:
            day_rows = db.execute(
                "SELECT id, day_number FROM itinerary_days WHERE trip_id = ? ORDER BY day_number",
                (trip_id,),
            ).fetchall()
            if day_rows:
                items_by_day = {row["id"]: [] for row in day_rows}
                for item in db.execute(
                    """SELECT ii.id, ii.place_id, ii.day_id FROM itinerary_items AS ii
                       JOIN itinerary_days AS d ON d.id = ii.day_id
                       WHERE d.trip_id = ? ORDER BY d.day_number, ii.sequence""",
                    (trip_id,),
                ):
                    items_by_day[item["day_id"]].append(item["id"])

                kept_days = day_rows[:day_count]
                removed_days = day_rows[day_count:]
                if removed_days:
                    last_day_id = kept_days[-1]["id"]
                    combined = list(items_by_day[last_day_id])
                    for removed_day in removed_days:
                        combined.extend(items_by_day[removed_day["id"]])
                    items_by_day[last_day_id] = combined
                    db.execute(
                        """UPDATE itinerary_items SET sequence = sequence + 100000
                           WHERE day_id IN (SELECT id FROM itinerary_days WHERE trip_id = ?)""",
                        (trip_id,),
                    )
                    for day_row in kept_days:
                        item_ids = items_by_day[day_row["id"]]
                        for sequence, item_id in enumerate(item_ids, 1):
                            db.execute(
                                "UPDATE itinerary_items SET day_id = ?, sequence = ? WHERE id = ?",
                                (day_row["id"], sequence, item_id),
                            )
                    db.executemany(
                        "DELETE FROM itinerary_days WHERE id = ?",
                        [(row["id"],) for row in removed_days],
                    )
                    day_rows = kept_days

                for row in day_rows:
                    day_date = (start + timedelta(days=row["day_number"] - 1)).isoformat()
                    db.execute(
                        "UPDATE itinerary_days SET date = ? WHERE id = ?",
                        (day_date, row["id"]),
                    )
                existing_numbers = {row["day_number"] for row in day_rows}
                for day_number in range(1, day_count + 1):
                    if day_number not in existing_numbers:
                        day_date = (start + timedelta(days=day_number - 1)).isoformat()
                        db.execute(
                            "INSERT INTO itinerary_days (trip_id, day_number, date) VALUES (?, ?, ?)",
                            (trip_id, day_number, day_date),
                        )

            db.execute(
                "UPDATE trips SET start_date = ?, end_date = ? WHERE id = ?",
                (start.isoformat(), end.isoformat(), trip_id),
            )
            max_day_offset = day_count - 1
            regions = db.execute(
                "SELECT id, start_date, end_date FROM trip_regions WHERE trip_id = ?", (trip_id,)
            ).fetchall()
            for region in regions:
                region_start_offset = (date.fromisoformat(region["start_date"]) - old_trip_start).days
                region_end_offset = (date.fromisoformat(region["end_date"]) - old_trip_start).days
                region_start_offset = max(0, min(region_start_offset, max_day_offset))
                region_end_offset = max(region_start_offset, min(region_end_offset, max_day_offset))
                db.execute(
                    "UPDATE trip_regions SET start_date = ?, end_date = ? WHERE id = ?",
                    ((start + timedelta(days=region_start_offset)).isoformat(),
                     (start + timedelta(days=region_end_offset)).isoformat(), region["id"]),
                )
            sync_country_transfers(db, trip_id)
            db.commit()
        except sqlite3.Error:
            db.rollback()
            flash("여행 기간을 저장하지 못했습니다.", "error")
            return redirect(url_for("trip_detail", trip_id=trip_id))
        flash("여행 기간을 수정했습니다. 저장된 일정은 유지했습니다.", "success")
        return redirect(url_for("trip_detail", trip_id=trip_id))

    @app.post("/trips/<int:trip_id>/accommodation")
    def save_accommodation(trip_id):
        trip = query_db("SELECT * FROM trips WHERE id = ?", (trip_id,), one=True)
        if trip is None:
            abort(404)
        name = request.form.get("name", "").strip()
        address = request.form.get("address", "").strip()
        latitude = request.form.get("latitude", "").strip()
        longitude = request.form.get("longitude", "").strip()
        google_place_id = request.form.get("google_place_id", "").strip() or None
        check_in_date = request.form.get("check_in_date", "")
        check_out_date = request.form.get("check_out_date", "")
        check_in_time = request.form.get("check_in_time", "15:00") or "15:00"
        check_out_time = request.form.get("check_out_time", "11:00") or "11:00"
        accommodation_id = request.form.get("accommodation_id", "").strip()
        region_value = request.form.get("region_id", "").strip()
        if not name or not address:
            flash("숙소 이름과 주소를 입력해 주세요.", "error")
            return redirect(url_for("trip_accommodation", trip_id=trip_id))
        try:
            check_in = date.fromisoformat(check_in_date)
            check_out = date.fromisoformat(check_out_date)
            trip_start = date.fromisoformat(trip["start_date"])
            trip_end = date.fromisoformat(trip["end_date"])
        except ValueError:
            flash("숙소 체크인과 체크아웃 날짜를 입력해 주세요.", "error")
            return redirect(url_for("trip_accommodation", trip_id=trip_id))
        if check_in < trip_start or check_out > trip_end or check_out <= check_in:
            flash("숙소 날짜는 여행 기간 안에서 체크아웃이 체크인보다 뒤가 되도록 선택해 주세요.", "error")
            return redirect(url_for("trip_accommodation", trip_id=trip_id))
        try:
            datetime_time.fromisoformat(check_in_time)
            datetime_time.fromisoformat(check_out_time)
        except ValueError:
            flash("체크인과 체크아웃 시간을 확인해 주세요.", "error")
            return redirect(url_for("trip_accommodation", trip_id=trip_id))
        coordinates = _parse_coordinates(latitude, longitude)
        if coordinates is None and (latitude or longitude):
            flash("위도와 경도를 올바르게 함께 입력해 주세요.", "error")
            return redirect(url_for("trip_accommodation", trip_id=trip_id))
        try:
            region_id = int(region_value) if region_value else None
        except ValueError:
            flash("국가 선택을 확인해 주세요.", "error")
            return redirect(url_for("trip_accommodation", trip_id=trip_id))
        if region_id and not query_db(
            "SELECT 1 FROM trip_regions WHERE id = ? AND trip_id = ?", (region_id, trip_id), one=True
        ):
            flash("국가 선택을 확인해 주세요.", "error")
            return redirect(url_for("trip_accommodation", trip_id=trip_id))
        if region_id:
            region = query_db(
                "SELECT start_date, end_date FROM trip_regions WHERE id = ? AND trip_id = ?",
                (region_id, trip_id), one=True,
            )
            if check_in < date.fromisoformat(region["start_date"]) or check_out > date.fromisoformat(region["end_date"]):
                flash("숙소 체크인·체크아웃 날짜가 선택한 국가/지역 구간 안에 있어야 합니다.", "error")
                return redirect(url_for("trip_accommodation", trip_id=trip_id))
        db = get_db()
        if accommodation_id:
            try:
                accommodation_id = int(accommodation_id)
            except ValueError:
                abort(404)
            existing = db.execute(
                "SELECT id FROM accommodations WHERE id = ? AND trip_id = ?", (accommodation_id, trip_id)
            ).fetchone()
            if existing is None:
                abort(404)
            db.execute(
                """UPDATE accommodations SET name = ?, address = ?, check_in_date = ?, check_out_date = ?,
                   check_in_time = ?, check_out_time = ?,
                   latitude = ?, longitude = ?, google_place_id = ?, coordinates_updated_at = ?, region_id = ?
                   WHERE id = ? AND trip_id = ?""",
                (name, address, check_in_date, check_out_date, check_in_time, check_out_time,
                 *(coordinates or (None, None)), google_place_id,
                 _utc_now() if google_place_id and coordinates else None, region_id, accommodation_id, trip_id),
            )
        else:
            db.execute(
                """INSERT INTO accommodations
                   (trip_id, name, address, check_in_date, check_out_date, check_in_time, check_out_time, latitude, longitude,
                    google_place_id, coordinates_updated_at, region_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (trip_id, name, address, check_in_date, check_out_date, check_in_time, check_out_time,
                 *(coordinates or (None, None)), google_place_id,
                 _utc_now() if google_place_id and coordinates else None, region_id),
            )
        db.commit()
        flash("숙소 정보를 수정했습니다." if accommodation_id else "숙소를 추가했습니다.", "success")
        return redirect(url_for("trip_accommodation", trip_id=trip_id))

    @app.post("/trips/<int:trip_id>/transport")
    def add_transport_leg(trip_id):
        trip = query_db("SELECT * FROM trips WHERE id = ?", (trip_id,), one=True)
        if trip is None:
            abort(404)
        mode = request.form.get("mode", "")
        carrier = request.form.get("carrier", "").strip()[:120]
        transport_number = request.form.get("transport_number", "").strip()[:80]
        origin = request.form.get("origin", "").strip()[:180]
        destination = request.form.get("destination", "").strip()[:180]
        departure_value = request.form.get("departure_at", "")
        arrival_value = request.form.get("arrival_at", "")
        if mode not in {"flight", "train", "bus"}:
            flash("교통수단을 선택해 주세요.", "error")
            return redirect(url_for("trip_transport", trip_id=trip_id))
        try:
            departure_at = datetime.fromisoformat(departure_value)
            arrival_at = datetime.fromisoformat(arrival_value)
            trip_start = date.fromisoformat(trip["start_date"])
            trip_end = date.fromisoformat(trip["end_date"])
        except ValueError:
            flash("출발·도착 날짜와 시간을 입력해 주세요.", "error")
            return redirect(url_for("trip_transport", trip_id=trip_id))
        if not origin or not destination:
            flash("출발지와 도착지를 입력해 주세요.", "error")
            return redirect(url_for("trip_transport", trip_id=trip_id))
        if not (
            trip_start <= departure_at.date() <= trip_end
            or trip_start <= arrival_at.date() <= trip_end
        ):
            flash("출발일과 도착일 중 하나는 여행 기간 안에 있어야 합니다.", "error")
            return redirect(url_for("trip_transport", trip_id=trip_id))

        uploaded = request.files.get("ticket")
        original_name = None
        stored_name = None
        if uploaded and uploaded.filename:
            extension = Path(uploaded.filename).suffix.lower()
            if extension not in {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".pdf"}:
                flash("티켓은 JPG, PNG, WEBP, HEIC, HEIF, PDF 파일로 첨부해 주세요.", "error")
                return redirect(url_for("trip_transport", trip_id=trip_id))
            safe_name = secure_filename(uploaded.filename)
            original_name = safe_name if Path(safe_name).suffix.lower() == extension else f"티켓{extension}"
            stored_name = f"{uuid.uuid4().hex}{extension}"
            uploaded.save(os.path.join(app.config["TRANSPORT_UPLOAD_FOLDER"], stored_name))
        db = get_db()
        try:
            db.execute(
                """INSERT INTO trip_transport_legs
                   (trip_id, mode, carrier, transport_number, origin, destination,
                    departure_at, arrival_at, original_file_name, stored_file_name)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (trip_id, mode, carrier, transport_number, origin, destination,
                 departure_at.isoformat(timespec="minutes"), arrival_at.isoformat(timespec="minutes"),
                 original_name, stored_name),
            )
            db.commit()
        except sqlite3.Error:
            db.rollback()
            if stored_name:
                os.remove(os.path.join(app.config["TRANSPORT_UPLOAD_FOLDER"], stored_name))
            flash("교통편을 저장하지 못했습니다.", "error")
            return redirect(url_for("trip_transport", trip_id=trip_id))
        flash("교통편 정보를 저장했습니다. 다음에 일정을 다시 추천할 때 반영됩니다.", "success")
        return redirect(url_for("trip_transport", trip_id=trip_id))

    @app.get("/trips/<int:trip_id>/transport/<int:leg_id>/ticket")
    def transport_ticket(trip_id, leg_id):
        leg = query_db(
            "SELECT stored_file_name, original_file_name FROM trip_transport_legs WHERE id = ? AND trip_id = ?",
            (leg_id, trip_id), one=True,
        )
        if leg is None or not leg["stored_file_name"]:
            abort(404)
        return send_from_directory(
            app.config["TRANSPORT_UPLOAD_FOLDER"], leg["stored_file_name"],
            download_name=leg["original_file_name"], as_attachment=True,
        )

    @app.post("/trips/<int:trip_id>/transport/<int:leg_id>/delete")
    def delete_transport_leg(trip_id, leg_id):
        leg = query_db(
            "SELECT stored_file_name FROM trip_transport_legs WHERE id = ? AND trip_id = ?",
            (leg_id, trip_id), one=True,
        )
        if leg is None:
            abort(404)
        db = get_db()
        db.execute("DELETE FROM trip_transport_legs WHERE id = ? AND trip_id = ?", (leg_id, trip_id))
        db.commit()
        if leg["stored_file_name"]:
            file_path = os.path.join(app.config["TRANSPORT_UPLOAD_FOLDER"], leg["stored_file_name"])
            if os.path.isfile(file_path):
                os.remove(file_path)
        flash("교통편을 삭제했습니다.", "success")
        return redirect(url_for("trip_transport", trip_id=trip_id))

    @app.post("/trips/<int:trip_id>/places")
    def add_place(trip_id):
        if query_db("SELECT id FROM trips WHERE id = ?", (trip_id,), one=True) is None:
            abort(404)
        name = request.form.get("name", "").strip()
        address = request.form.get("address", "").strip()
        provider_place_id = request.form.get("provider_place_id", "").strip() or None
        category = request.form.get("category", "attraction")
        latitude = request.form.get("latitude", "").strip()
        longitude = request.form.get("longitude", "").strip()
        region_value = request.form.get("region_id", "").strip()
        if not name:
            flash("장소 이름을 입력해 주세요.", "error")
            return redirect(url_for("trip_places", trip_id=trip_id))
        if category not in ("attraction", "restaurant"):
            flash("장소 유형을 선택해 주세요.", "error")
            return redirect(url_for("trip_places", trip_id=trip_id))
        coordinates = _parse_coordinates(latitude, longitude)
        if coordinates is None and (latitude or longitude):
            flash("위도와 경도를 올바르게 함께 입력해 주세요.", "error")
            return redirect(url_for("trip_places", trip_id=trip_id))
        try:
            region_id = int(region_value) if region_value else None
        except ValueError:
            abort(400)
        if region_id and not query_db(
            "SELECT 1 FROM trip_regions WHERE id = ? AND trip_id = ?", (region_id, trip_id), one=True
        ):
            abort(400)
        duplicate = query_db(
            """SELECT id FROM places WHERE trip_id = ? AND (
                 (? IS NOT NULL AND provider_place_id = ?) OR
                 (name = ? COLLATE NOCASE AND address = ? COLLATE NOCASE))""",
            (trip_id, provider_place_id, provider_place_id, name, address), one=True,
        )
        if duplicate:
            if provider_place_id and coordinates:
                try:
                    get_db().execute(
                        """UPDATE places SET provider_place_id = ?, latitude = ?, longitude = ?,
                           coordinates_updated_at = ?, region_id = COALESCE(?, region_id)
                           WHERE id = ? AND trip_id = ?""",
                        (provider_place_id, *coordinates, _utc_now(), region_id, duplicate["id"], trip_id),
                    )
                    get_db().commit()
                    flash("이미 등록된 장소의 지도 위치를 갱신했습니다.", "success")
                    return redirect(url_for("trip_places", trip_id=trip_id))
                except sqlite3.IntegrityError:
                    get_db().rollback()
            flash("이미 등록된 장소입니다.", "error")
            return redirect(url_for("trip_places", trip_id=trip_id))
        try:
            get_db().execute(
                """INSERT INTO places
                   (trip_id, provider_place_id, name, address, latitude, longitude,
                    category, coordinates_updated_at, region_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (trip_id, provider_place_id, name, address,
                 *(coordinates or (None, None)), category,
                 _utc_now() if provider_place_id and coordinates else None, region_id),
            )
            get_db().commit()
        except sqlite3.IntegrityError:
            get_db().rollback()
            flash("장소를 저장하지 못했습니다. 중복 여부를 확인해 주세요.", "error")
            return redirect(url_for("trip_places", trip_id=trip_id))
        flash(f"‘{name}’ 장소를 추가했습니다.", "success")
        return redirect(url_for("trip_places", trip_id=trip_id))

    @app.post("/trips/<int:trip_id>/itinerary/add")
    @app.post("/trips/<int:trip_id>/itinerary/day/<int:day_number>/add")
    def add_itinerary_place(trip_id, day_number=None):
        if day_number is None:
            try:
                day_number = int(request.form.get("day_number", ""))
            except ValueError:
                abort(400)
        day = query_db(
            "SELECT id FROM itinerary_days WHERE trip_id = ? AND day_number = ?",
            (trip_id, day_number), one=True,
        )
        if day is None:
            abort(404)
        try:
            place_id = int(request.form.get("place_id", ""))
        except ValueError:
            abort(400)
        place = query_db(
            "SELECT id, name, region_id FROM places WHERE id = ? AND trip_id = ?",
            (place_id, trip_id), one=True,
        )
        if place is None:
            abort(404)
        existing = query_db(
            """SELECT 1 FROM itinerary_items AS ii JOIN itinerary_days AS d ON d.id = ii.day_id
               WHERE d.trip_id = ? AND ii.place_id = ?""",
            (trip_id, place_id), one=True,
        )
        if existing:
            flash("이미 일정에 등록된 장소입니다.", "error")
            return redirect(url_for("trip_itinerary", trip_id=trip_id))
        sequence = query_db(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM itinerary_items WHERE day_id = ?",
            (day["id"],), one=True,
        )["next_sequence"]
        db = get_db()
        db.execute(
            "INSERT INTO itinerary_items (day_id, place_id, sequence, start_time, region_id) VALUES (?, ?, ?, NULL, ?)",
            (day["id"], place_id, sequence, place["region_id"]),
        )
        db.commit()
        flash(f"‘{place['name']}’ 장소를 Day {day_number}에 추가했습니다.", "success")
        return redirect(url_for("trip_itinerary", trip_id=trip_id))

    @app.post("/trips/<int:trip_id>/places/<int:place_id>/delete")
    def delete_place(trip_id, place_id):
        photos = query_db(
            "SELECT stored_file_name FROM place_photos WHERE place_id = ? AND trip_id = ?",
            (place_id, trip_id),
        )
        cursor = get_db().execute(
            "DELETE FROM places WHERE id = ? AND trip_id = ?", (place_id, trip_id)
        )
        get_db().commit()
        if cursor.rowcount == 0:
            abort(404)
        for photo in photos:
            file_path = os.path.join(app.config["PLACE_PHOTO_UPLOAD_FOLDER"], photo["stored_file_name"])
            if os.path.isfile(file_path):
                os.remove(file_path)
        flash("장소를 삭제했습니다.", "success")
        return redirect(url_for("trip_places", trip_id=trip_id))

    @app.post("/trips/<int:trip_id>/itinerary/places/<int:place_id>/delete")
    def remove_itinerary_place(trip_id, place_id):
        day = query_db(
            """SELECT d.day_number, d.id AS day_id FROM itinerary_items ii
               JOIN itinerary_days d ON d.id = ii.day_id
               WHERE d.trip_id = ? AND ii.place_id = ?""",
            (trip_id, place_id), one=True,
        )
        cursor = get_db().execute(
            """DELETE FROM itinerary_items WHERE place_id = ? AND day_id IN
               (SELECT id FROM itinerary_days WHERE trip_id = ?)""",
            (place_id, trip_id),
        )
        get_db().commit()
        if cursor.rowcount == 0:
            abort(404)
        if day:
            remaining = query_db(
                "SELECT id FROM itinerary_items WHERE day_id = ? ORDER BY sequence", (day["day_id"],)
            )
            db = get_db()
            db.execute("UPDATE itinerary_items SET sequence = sequence + 100000 WHERE day_id = ?", (day["day_id"],))
            for sequence, item in enumerate(remaining, 1):
                db.execute("UPDATE itinerary_items SET sequence = ? WHERE id = ?", (sequence, item["id"]))
            db.commit()
        flash("장소를 일정에서 제외했습니다.", "success")
        if request.form.get("return_to_day") == "1" and day:
            return redirect(url_for("itinerary_day_detail", trip_id=trip_id, day_number=day["day_number"]))
        return redirect(url_for("trip_itinerary", trip_id=trip_id))

    @app.post("/trips/<int:trip_id>/itinerary/day/<int:day_number>/reorder")
    def reorder_itinerary_day(trip_id, day_number):
        day = query_db(
            "SELECT id FROM itinerary_days WHERE trip_id = ? AND day_number = ?",
            (trip_id, day_number), one=True,
        )
        if day is None:
            abort(404)
        payload = request.get_json(silent=True) or {}
        try:
            submitted_ids = [int(place_id) for place_id in payload["place_ids"]]
        except (KeyError, TypeError, ValueError):
            return {"error": "방문 장소 목록을 확인해 주세요."}, 400
        submitted_event_keys = payload.get("event_keys")
        if submitted_event_keys is not None and (
            not isinstance(submitted_event_keys, list)
            or not all(isinstance(key, str) for key in submitted_event_keys)
        ):
            return {"error": "일정 순서 정보가 올바르지 않습니다."}, 400
        rows = query_db(
            "SELECT place_id, is_locked FROM itinerary_items WHERE day_id = ? ORDER BY sequence",
            (day["id"],),
        )
        expected_ids = [row["place_id"] for row in rows]
        if len(submitted_ids) != len(expected_ids) or set(submitted_ids) != set(expected_ids):
            return {"error": "방문 장소 목록이 변경되었습니다. 새로고침 후 다시 시도해 주세요."}, 400
        locked_ids = {row["place_id"] for row in rows if row["is_locked"]}
        if any(
            (expected_ids[index] in locked_ids) != (submitted_ids[index] in locked_ids)
            or (expected_ids[index] in locked_ids and expected_ids[index] != submitted_ids[index])
            for index in range(len(expected_ids))
        ):
            return {"error": "순서 고정한 장소는 이동할 수 없습니다."}, 400
        db = get_db()
        try:
            db.execute("UPDATE itinerary_items SET sequence = sequence + 100000 WHERE day_id = ?", (day["id"],))
            for sequence, place_id in enumerate(submitted_ids, 1):
                db.execute(
                    "UPDATE itinerary_items SET sequence = ? WHERE day_id = ? AND place_id = ?",
                    (sequence, day["id"], place_id),
                )
            if submitted_event_keys is not None:
                _save_itinerary_event_order(db, trip_id, day["id"], None, submitted_event_keys)
            db.commit()
        except ValueError:
            db.rollback()
            return {"error": "일정이 바뀌어 순서를 저장하지 못했습니다. 새로고침해 주세요."}, 409
        except sqlite3.Error:
            db.rollback()
            return {"error": "방문 순서를 저장하지 못했습니다."}, 500
        return {"ok": True}

    @app.post("/trips/<int:trip_id>/accommodation/<int:accommodation_id>/delete")
    def delete_accommodation(trip_id, accommodation_id):
        cursor = get_db().execute(
            "DELETE FROM accommodations WHERE id = ? AND trip_id = ?", (accommodation_id, trip_id)
        )
        get_db().commit()
        if cursor.rowcount == 0:
            abort(404)
        flash("숙소를 삭제했습니다.", "success")
        return redirect(url_for("trip_accommodation", trip_id=trip_id))

    @app.post("/trips/<int:trip_id>/itinerary/reorder")
    def reorder_trip_itinerary(trip_id):
        trip = query_db("SELECT id FROM trips WHERE id = ?", (trip_id,), one=True)
        if trip is None:
            abort(404)
        payload = request.get_json(silent=True) or {}
        submitted_days = payload.get("days")
        submitted_timeline = payload.get("timeline")
        if not isinstance(submitted_days, list):
            return {"error": "일정 데이터 형식이 올바르지 않습니다."}, 400

        day_rows = query_db(
            "SELECT id, day_number FROM itinerary_days WHERE trip_id = ? ORDER BY day_number",
            (trip_id,),
        )
        day_ids = {row["day_number"]: row["id"] for row in day_rows}
        expected_rows = query_db(
            """SELECT ii.place_id FROM itinerary_items AS ii
               JOIN itinerary_days AS d ON d.id = ii.day_id WHERE d.trip_id = ?""",
            (trip_id,),
        )
        expected_ids = {row["place_id"] for row in expected_rows}
        received_ids = []
        normalized_days = {}
        try:
            for entry in submitted_days:
                day_number = int(entry["day_number"])
                place_ids = [int(place_id) for place_id in entry["place_ids"]]
                if day_number not in day_ids or day_number in normalized_days:
                    raise ValueError
                normalized_days[day_number] = place_ids
                received_ids.extend(place_ids)
        except (KeyError, TypeError, ValueError):
            return {"error": "일정 항목을 확인해 주세요."}, 400
        if set(normalized_days) != set(day_ids) or set(received_ids) != expected_ids or len(received_ids) != len(expected_ids):
            return {"error": "장소를 빠뜨리거나 중복 이동했습니다. 새로고침 후 다시 시도해 주세요."}, 400

        db = get_db()
        try:
            db.execute(
                """UPDATE itinerary_items SET sequence = sequence + 100000
                   WHERE day_id IN (SELECT id FROM itinerary_days WHERE trip_id = ?)""",
                (trip_id,),
            )
            for day_number, place_ids in normalized_days.items():
                for sequence, place_id in enumerate(place_ids, 1):
                    cursor = db.execute(
                        """UPDATE itinerary_items SET day_id = ?, sequence = ?
                           WHERE place_id = ? AND day_id IN
                             (SELECT id FROM itinerary_days WHERE trip_id = ?)""",
                        (day_ids[day_number], sequence, place_id, trip_id),
                    )
                    if cursor.rowcount != 1:
                        raise sqlite3.IntegrityError("itinerary item was not found")
            db.execute(
                """UPDATE itinerary_transfers SET day_id =
                   (SELECT day_id FROM itinerary_items WHERE id = itinerary_transfers.before_item_id)
                   WHERE trip_id = ? AND before_item_id IS NOT NULL""",
                (trip_id,),
            )
            if isinstance(submitted_timeline, list):
                timeline_by_day = {}
                for entry in submitted_timeline:
                    day_number = int(entry["day_number"])
                    event_keys = entry["event_keys"]
                    if day_number not in day_ids or day_number in timeline_by_day or not isinstance(event_keys, list):
                        raise ValueError("invalid itinerary timeline")
                    timeline_by_day[day_number] = event_keys
                if set(timeline_by_day) != set(day_ids):
                    raise ValueError("incomplete itinerary timeline")
                try:
                    for day_number, event_keys in timeline_by_day.items():
                        _save_itinerary_event_order(db, trip_id, day_ids[day_number], None, event_keys)
                except ValueError:
                    # A transfer card follows its destination visit if that visit moves to another day.
                    # In that case, discard stale event order and rebuild the display from the new schedule.
                    db.execute("DELETE FROM itinerary_event_order WHERE trip_id = ?", (trip_id,))
            else:
                db.execute("DELETE FROM itinerary_event_order WHERE trip_id = ?", (trip_id,))
            db.commit()
        except (sqlite3.Error, KeyError, TypeError, ValueError):
            db.rollback()
            return {"error": "일정을 저장하지 못했습니다."}, 500
        return {"ok": True}

    return app


def _parse_coordinates(latitude, longitude):
    if not latitude and not longitude:
        return None
    if not latitude or not longitude:
        return None
    try:
        lat, lng = float(latitude), float(longitude)
    except ValueError:
        return None
    if not -90 <= lat <= 90 or not -180 <= lng <= 180:
        return None
    return lat, lng


def _itinerary_day_redirect(trip_id, day_number, return_to_day):
    if return_to_day:
        return redirect(url_for("itinerary_day_detail", trip_id=trip_id, day_number=day_number))
    return redirect(url_for("trip_itinerary", trip_id=trip_id))


def _utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _trip_places(trip_id):
    return query_db(
        """SELECT p.*, MIN(d.day_number) AS visit_day FROM places p
           LEFT JOIN itinerary_items ii ON ii.place_id = p.id
           LEFT JOIN itinerary_days d ON d.id = ii.day_id
           WHERE p.trip_id = ? GROUP BY p.id
           ORDER BY CASE WHEN MIN(d.day_number) IS NULL THEN 1 ELSE 0 END,
                    MIN(d.day_number), p.name COLLATE NOCASE""",
        (trip_id,),
    )


def _accommodation_status(accommodation, day_date):
    if not accommodation:
        return None
    stays = accommodation if isinstance(accommodation, (list, tuple)) else [accommodation]
    statuses = []
    for stay in stays:
        if not stay["check_in_date"] or not stay["check_out_date"]:
            continue
        if day_date < stay["check_in_date"] or day_date > stay["check_out_date"]:
            continue
        if day_date == stay["check_in_date"]:
            status = "체크인"
        elif day_date == stay["check_out_date"]:
            status = "체크아웃"
        else:
            status = "숙박"
        statuses.append(f"{stay['name']} {status}")
    return " · ".join(statuses) or None


def _accommodations_for_date(accommodations, day_date):
    return [stay for stay in accommodations if _accommodation_status(stay, day_date)]


def _itinerary_event_keys_for_day(connection, trip_id, day_id, region_id=None):
    day = connection.execute(
        "SELECT date FROM itinerary_days WHERE id = ? AND trip_id = ?", (day_id, trip_id)
    ).fetchone()
    if day is None:
        return set()
    event_keys = set()
    place_rows = connection.execute(
        """SELECT p.id FROM itinerary_items ii JOIN places p ON p.id = ii.place_id
           WHERE ii.day_id = ? AND p.trip_id = ?""" + (" AND p.region_id = ?" if region_id is not None else ""),
        (day_id, trip_id, region_id) if region_id is not None else (day_id, trip_id),
    )
    event_keys.update(f"place:{row['id']}" for row in place_rows)
    transfer_rows = connection.execute(
        "SELECT id FROM itinerary_transfers WHERE trip_id = ? AND day_id = ?"
        + (" AND (from_region_id = ? OR to_region_id = ?)" if region_id is not None else ""),
        (trip_id, day_id, region_id, region_id) if region_id is not None else (trip_id, day_id),
    )
    event_keys.update(f"transfer:{row['id']}" for row in transfer_rows)
    stays = connection.execute(
        "SELECT id, check_in_date, check_out_date FROM accommodations WHERE trip_id = ?"
        + (" AND region_id = ?" if region_id is not None else "")
        + " AND (check_in_date = ? OR check_out_date = ?)",
        (trip_id, region_id, day["date"], day["date"])
        if region_id is not None else (trip_id, day["date"], day["date"]),
    )
    for stay in stays:
        if stay["check_in_date"] == day["date"]:
            event_keys.add(f"stay-checkin:{stay['id']}")
        if stay["check_out_date"] == day["date"]:
            event_keys.add(f"stay-checkout:{stay['id']}")
    return event_keys


def _save_itinerary_event_order(connection, trip_id, day_id, region_id, event_keys):
    if len(event_keys) != len(set(event_keys)):
        raise ValueError("duplicate itinerary event")
    expected_keys = _itinerary_event_keys_for_day(connection, trip_id, day_id, region_id)
    if set(event_keys) != expected_keys:
        raise ValueError("itinerary events changed")
    if region_id is None:
        connection.execute(
            "DELETE FROM itinerary_event_order WHERE trip_id = ? AND day_id = ? AND region_id IS NULL",
            (trip_id, day_id),
        )
    else:
        connection.execute(
            "DELETE FROM itinerary_event_order WHERE trip_id = ? AND day_id = ? AND region_id = ?",
            (trip_id, day_id, region_id),
        )
    connection.executemany(
        """INSERT INTO itinerary_event_order (trip_id, day_id, region_id, event_key, sequence)
           VALUES (?, ?, ?, ?, ?)""",
        [(trip_id, day_id, region_id, event_key, sequence)
         for sequence, event_key in enumerate(event_keys, 1)],
    )


def _order_itinerary_events(connection, trip_id, day_id, region_id, events):
    regional_order = connection.execute(
        "SELECT event_key, sequence FROM itinerary_event_order WHERE trip_id = ? AND day_id = ? AND region_id IS ? ORDER BY sequence",
        (trip_id, day_id, region_id),
    ).fetchall()
    global_order = []
    if region_id is not None and not regional_order:
        global_order = connection.execute(
            "SELECT event_key, sequence FROM itinerary_event_order WHERE trip_id = ? AND day_id = ? AND region_id IS NULL ORDER BY sequence",
            (trip_id, day_id),
        ).fetchall()
    regional_rank = {row["event_key"]: row["sequence"] for row in regional_order}
    global_rank = {row["event_key"]: row["sequence"] for row in global_order}
    default_rank = {event["key"]: index for index, event in enumerate(events)}
    return sorted(
        events,
        key=lambda event: (
            (0, regional_rank[event["key"]]) if event["key"] in regional_rank
            else (1, global_rank[event["key"]]) if event["key"] in global_rank
            else (2, default_rank[event["key"]])
        ),
    )


def _accommodation_for_date(accommodations, day_date):
    active = [
        stay for stay in accommodations
        if stay["check_in_date"] and stay["check_out_date"]
        and stay["check_in_date"] <= day_date < stay["check_out_date"]
    ]
    if not active:
        active = [
            stay for stay in accommodations
            if stay["check_in_date"] == day_date or stay["check_out_date"] == day_date
        ]
    return active[-1] if active else None


def _get_recommendation_settings(trip_id):
    row = query_db(
        "SELECT * FROM trip_recommendation_settings WHERE trip_id = ?",
        (trip_id,), one=True,
    )
    return dict(row) if row else {
        "flight_buffer_hours": 4.0,
        "train_buffer_hours": 2.0,
        "bus_buffer_hours": 2.0,
        "day_start_time": "08:00",
        "day_end_time": "20:00",
        "arrival_latest_start": "18:00",
        "departure_earliest_cutoff": "12:00",
    }


def _build_day_availability(trip, transport_legs, settings=None):
    settings = settings or {}
    start_date = date.fromisoformat(trip["start_date"])
    day_count = (date.fromisoformat(trip["end_date"]) - start_date).days + 1
    day_start = datetime_time.fromisoformat(settings.get("day_start_time", "08:00"))
    day_end = datetime_time.fromisoformat(settings.get("day_end_time", "20:00"))
    arrival_latest_start = datetime_time.fromisoformat(settings.get("arrival_latest_start", "18:00"))
    departure_earliest_cutoff = datetime_time.fromisoformat(settings.get("departure_earliest_cutoff", "12:00"))
    days = {
        number: {
            "is_available": True,
            "blocked_reason": "",
            "available_from": "",
            "available_until": "",
            "available_hours": (datetime.combine(start_date, day_end) - datetime.combine(start_date, day_start)).total_seconds() / 3600,
            "availability_note": "",
        }
        for number in range(1, day_count + 1)
    }
    start_bounds = {}
    end_bounds = {}
    blocked_reasons = {number: [] for number in days}

    for leg in transport_legs:
        mode = leg["mode"]
        buffer_hours = float(settings.get(
            {"flight": "flight_buffer_hours", "train": "train_buffer_hours", "bus": "bus_buffer_hours"}.get(mode, "train_buffer_hours"),
            {"flight": 4, "train": 2, "bus": 2}.get(mode, 2),
        ))
        label = {"flight": "항공", "train": "기차", "bus": "버스"}.get(mode, "교통편")
        departure = datetime.fromisoformat(leg["departure_at"])
        arrival = datetime.fromisoformat(leg["arrival_at"])
        departure_day = (departure.date() - start_date).days + 1
        arrival_day = (arrival.date() - start_date).days + 1
        departure_is_in_trip = departure_day in days
        arrival_is_in_trip = arrival_day in days

        if departure_is_in_trip and arrival_is_in_trip and departure_day == arrival_day:
            departure_limit = departure - timedelta(hours=buffer_hours)
            arrival_limit = arrival + timedelta(hours=buffer_hours)
            before_hours = max(0.0, (datetime.combine(start_date, departure_limit.time()) - datetime.combine(start_date, day_start)).total_seconds() / 3600) if departure_limit.date() == departure.date() else 0.0
            after_hours = max(0.0, (datetime.combine(start_date, day_end) - datetime.combine(start_date, arrival_limit.time())).total_seconds() / 3600) if arrival_limit.date() == arrival.date() else 0.0
            if max(before_hours, after_hours) < 2:
                blocked_reasons[departure_day].append(
                    f"{label} 이동 시간을 고려해 이날은 비워 두었습니다."
                )
            elif before_hours >= after_hours:
                if departure_limit.time() <= departure_earliest_cutoff or departure_limit.time() <= day_start:
                    blocked_reasons[departure_day].append(
                        f"{label} 출발 전 준비 시간을 고려해 그날은 비워 두었습니다."
                    )
                else:
                    end_bounds[departure_day] = min(end_bounds.get(departure_day, day_end), departure_limit.time())
            else:
                if arrival_limit.date() != arrival.date() or arrival_limit.time() >= arrival_latest_start or arrival_limit.time() >= day_end:
                    blocked_reasons[arrival_day].append(
                        f"{label} 도착 후 여유 시간이 늦어 이날은 비워 두었습니다."
                    )
                else:
                    start_bounds[arrival_day] = max(start_bounds.get(arrival_day, day_start), arrival_limit.time())
            continue

        if departure_is_in_trip:
            available = departure - timedelta(hours=buffer_hours)
            if available.date() != departure.date() or available.time() <= departure_earliest_cutoff or available.time() <= day_start:
                blocked_reasons[departure_day].append(
                    f"{label} 출발 전 준비 시간을 고려해 그날은 비워 두었습니다."
                )
            else:
                end_bounds[departure_day] = min(end_bounds.get(departure_day, day_end), available.time())

        if arrival_is_in_trip:
            available = arrival + timedelta(hours=buffer_hours)
            if available.date() != arrival.date() or available.time() >= arrival_latest_start or available.time() >= day_end:
                blocked_reasons[arrival_day].append(
                    f"{label} 도착 후 여유 시간이 늦어 이날은 비워 두었습니다."
                )
            else:
                start_bounds[arrival_day] = max(start_bounds.get(arrival_day, day_start), available.time())

    for day_number, result in days.items():
        start_bound = start_bounds.get(day_number, day_start)
        end_bound = end_bounds.get(day_number, day_end)
        available_hours = (
            datetime.combine(start_date, end_bound) - datetime.combine(start_date, start_bound)
        ).total_seconds() / 3600
        if available_hours < 2 and not blocked_reasons[day_number]:
            blocked_reasons[day_number].append("이동 준비 시간을 제외하면 일정 가능 시간이 짧아 그날은 비워 두었습니다.")
        result["available_hours"] = max(0.0, available_hours)
        if blocked_reasons[day_number]:
            result["is_available"] = False
            result["blocked_reason"] = " ".join(blocked_reasons[day_number])
        else:
            result["available_from"] = start_bound.strftime("%H:%M") if day_number in start_bounds else ""
            result["available_until"] = end_bound.strftime("%H:%M") if day_number in end_bounds else ""
            if result["available_from"] and result["available_until"]:
                result["availability_note"] = f"{result['available_from']} 이후 · {result['available_until']}까지 추천"
            elif result["available_from"]:
                result["availability_note"] = f"{result['available_from']} 이후 추천"
            elif result["available_until"]:
                result["availability_note"] = f"{result['available_until']}까지 추천"
    return days


def _map_points(accommodation, places, place_days=None, place_sequences=None):
    place_days = place_days or {}
    place_sequences = place_sequences or {}
    points = []
    accommodations = accommodation if isinstance(accommodation, (list, tuple)) else ([accommodation] if accommodation else [])
    for stay in accommodations:
        if stay["latitude"] is None or stay["longitude"] is None:
            continue
        points.append({
            "id": f"accommodation-{stay['id']}",
            "name": stay["name"],
            "address": stay["address"],
            "latitude": stay["latitude"],
            "longitude": stay["longitude"],
            "kind": "accommodation",
            "check_in_date": stay["check_in_date"],
            "check_out_date": stay["check_out_date"],
            "region_id": stay["region_id"],
        })
    for place in places:
        if place["latitude"] is None or place["longitude"] is None:
            continue
        points.append({
            "id": f"place-{place['id']}",
            "name": place["name"],
            "address": place["address"],
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "kind": "place",
            "category": place["category"],
            "day_number": place_days.get(place["id"]),
            "sequence_number": place_sequences.get(place["id"]),
        })
    return points


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
