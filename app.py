from datetime import date, datetime, time as datetime_time, timedelta, timezone
import os
import shutil
import sqlite3
import uuid
import math
import re
import hmac
import secrets
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, flash, g, redirect, render_template, request, send_from_directory, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from database.db import get_db, init_app, query_db
from services.itinerary import optimize_day_order, recommend_itinerary
from services.place_search import PlaceSearchError, search_places


def create_app(test_config=None):
    load_dotenv()
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY") or os.urandom(32),
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
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

    init_app(app)
    os.makedirs(app.config["EXPENSE_UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["TRANSPORT_UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["PLACE_PHOTO_UPLOAD_FOLDER"], exist_ok=True)

    @app.before_request
    def require_authenticated_user():
        if request.endpoint in {"login", "register", "static"}:
            return None
        user_id = session.get("user_id")
        if not user_id:
            return redirect(url_for("login"))
        csrf_protected_endpoints = {
            "invite_trip_member", "accept_trip_invitation", "decline_trip_invitation", "remove_trip_member",
        }
        if request.method == "POST" and request.endpoint in csrf_protected_endpoints:
            submitted_token = request.form.get("csrf_token", "")
            session_token = session.get("csrf_token", "")
            if not session_token or not hmac.compare_digest(submitted_token, session_token):
                abort(400)
        trip_id = (request.view_args or {}).get("trip_id")
        if trip_id is not None:
            membership = query_db(
                "SELECT role FROM trip_members WHERE trip_id = ? AND user_id = ?",
                (trip_id, user_id), one=True,
            )
            if membership is None:
                abort(404)
            g.trip_role = membership["role"]
        return None

    @app.context_processor
    def inject_csrf_token():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return {"csrf_token": session["csrf_token"]}

    @app.route("/register", methods=["GET", "POST"])
    def register():
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
            user = query_db("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,), one=True)
            if user and check_password_hash(user["password_hash"], password):
                session.clear()
                session["user_id"] = user["id"]
                session["username"] = user["username"]
                return redirect(url_for("index"))
            flash("아이디 또는 비밀번호를 확인해 주세요.", "error")
        return render_template("auth.html", mode="login")

    @app.post("/logout")
    def logout():
        session.clear()
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

    @app.get("/trips/merge")
    def merge_trips_page():
        trips = query_db(
            """SELECT t.*, COUNT(DISTINCT p.id) AS place_count
               FROM trips t LEFT JOIN places p ON p.trip_id = t.id
               JOIN trip_members tm ON tm.trip_id = t.id
               WHERE tm.user_id = ? GROUP BY t.id ORDER BY t.start_date, t.id""",
            (session["user_id"],),
        )
        preview_trips = []
        for trip in trips:
            itinerary_rows = query_db(
                """SELECT d.date, p.name, p.provider_place_id
                   FROM itinerary_days d
                   LEFT JOIN itinerary_items i ON i.day_id = d.id
                   LEFT JOIN places p ON p.id = i.place_id
                   WHERE d.trip_id = ? ORDER BY d.day_number, i.sequence""",
                (trip["id"],),
            )
            days_by_date = {}
            for row in itinerary_rows:
                day = days_by_date.setdefault(row["date"], {"date": row["date"], "places": []})
                if row["name"]:
                    day["places"].append({"name": row["name"], "provider_id": row["provider_place_id"]})
            stays = query_db(
                "SELECT id, name, check_in_date, check_out_date FROM accommodations WHERE trip_id = ? ORDER BY check_in_date, id",
                (trip["id"],),
            )
            preview_trips.append({
                "id": trip["id"],
                "title": trip["title"],
                "city": trip["city"],
                "start_date": trip["start_date"],
                "end_date": trip["end_date"],
                "days": list(days_by_date.values()),
                "accommodations": [dict(stay) for stay in stays],
            })
        return render_template("merge_trips.html", trips=trips, preview_trips=preview_trips)

    @app.post("/trips/merge")
    def merge_trips():
        try:
            selected_ids = list(dict.fromkeys(int(value) for value in request.form.getlist("trip_ids")))
        except ValueError:
            flash("병합할 여행을 확인해 주세요.", "error")
            return redirect(url_for("merge_trips_page"))
        if len(selected_ids) < 2:
            flash("여행을 두 개 이상 선택해 주세요.", "error")
            return redirect(url_for("merge_trips_page"))

        placeholders = ",".join("?" for _ in selected_ids)
        source_trips = query_db(
            f"""SELECT t.* FROM trips t JOIN trip_members tm ON tm.trip_id = t.id
                WHERE tm.user_id = ? AND t.id IN ({placeholders}) ORDER BY t.start_date, t.id""",
            [session["user_id"], *selected_ids],
        )
        if len(source_trips) != len(selected_ids):
            flash("선택한 여행 중 찾을 수 없는 여행이 있습니다.", "error")
            return redirect(url_for("merge_trips_page"))

        start_date = min(trip["start_date"] for trip in source_trips)
        end_date = max(trip["end_date"] for trip in source_trips)
        city_names = list(dict.fromkeys(trip["city"].strip() for trip in source_trips if trip["city"].strip()))
        merged_city = " · ".join(city_names)[:100] or "여행"
        merged_title = request.form.get("title", "").strip()[:100]
        if not merged_title:
            merged_title = " + ".join(trip["title"] for trip in source_trips)[:92] + " 통합 여행"

        transfer_candidates = {}
        for from_trip in source_trips:
            from_stays = query_db(
                "SELECT id, check_out_date FROM accommodations WHERE trip_id = ? AND check_out_date IS NOT NULL",
                (from_trip["id"],),
            )
            for to_trip in source_trips:
                if from_trip["id"] == to_trip["id"]:
                    continue
                to_stays = query_db(
                    "SELECT id, check_in_date FROM accommodations WHERE trip_id = ? AND check_in_date IS NOT NULL",
                    (to_trip["id"],),
                )
                for from_stay in from_stays:
                    for to_stay in to_stays:
                        if from_stay["check_out_date"] != to_stay["check_in_date"]:
                            continue
                        key = (from_trip["id"], to_trip["id"], from_stay["check_out_date"])
                        transfer_candidates.setdefault(key, {
                            "from_trip_id": from_trip["id"],
                            "to_trip_id": to_trip["id"],
                            "date": from_stay["check_out_date"],
                            "from_stay_id": from_stay["id"],
                            "to_stay_id": to_stay["id"],
                        })

        db = get_db()
        copied_ticket_files = []
        try:
            db.execute("BEGIN")
            cursor = db.execute(
                "INSERT INTO trips (user_id, title, city, start_date, end_date) VALUES (?, ?, ?, ?, ?)",
                (session["user_id"], merged_title, merged_city, start_date, end_date),
            )
            merged_trip_id = cursor.lastrowid
            db.execute(
                "INSERT INTO trip_members (trip_id, user_id, role) VALUES (?, ?, 'owner')",
                (merged_trip_id, session["user_id"]),
            )
            db.executemany(
                "INSERT INTO merged_trip_sources (merged_trip_id, source_trip_id, sort_order) VALUES (?, ?, ?)",
                [(merged_trip_id, source_trip["id"], order) for order, source_trip in enumerate(source_trips)],
            )

            merged_days = {}
            total_days = (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days + 1
            for offset in range(total_days):
                day_date = (date.fromisoformat(start_date) + timedelta(days=offset)).isoformat()
                day_cursor = db.execute(
                    "INSERT INTO itinerary_days (trip_id, day_number, date) VALUES (?, ?, ?)",
                    (merged_trip_id, offset + 1, day_date),
                )
                merged_days[day_date] = day_cursor.lastrowid

            region_ids_by_source = {}
            region_ids_by_label = {}
            region_labels_by_source = {}
            for region_order, source_trip in enumerate(source_trips):
                submitted_label = request.form.get(f"region_label_{source_trip['id']}", "").strip()[:100]
                region_label = submitted_label or source_trip["city"][:100] or source_trip["title"][:100]
                normalized_label = " ".join(region_label.casefold().split())
                region_id = region_ids_by_label.get(normalized_label)
                if region_id is None:
                    region_cursor = db.execute(
                        """INSERT INTO trip_regions (trip_id, label, start_date, end_date, sort_order)
                           VALUES (?, ?, ?, ?, ?)""",
                        (merged_trip_id, region_label, source_trip["start_date"],
                         source_trip["end_date"], region_order),
                    )
                    region_id = region_cursor.lastrowid
                    region_ids_by_label[normalized_label] = region_id
                else:
                    db.execute(
                        """UPDATE trip_regions SET start_date = MIN(start_date, ?),
                           end_date = MAX(end_date, ?) WHERE id = ?""",
                        (source_trip["start_date"], source_trip["end_date"], region_id),
                    )
                region_ids_by_source[source_trip["id"]] = region_id
                region_labels_by_source[source_trip["id"]] = region_label

            merged_places_by_provider = {}
            for source_trip in source_trips:
                source_region_id = region_ids_by_source[source_trip["id"]]
                region_label = region_labels_by_source[source_trip["id"]]
                stays = query_db(
                    "SELECT * FROM accommodations WHERE trip_id = ? ORDER BY check_in_date, id",
                    (source_trip["id"],),
                )
                for stay in stays:
                    db.execute(
                        """INSERT INTO accommodations
                           (trip_id, name, address, check_in_date, check_out_date, latitude, longitude,
                            google_place_id, coordinates_updated_at, region_id)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (merged_trip_id, stay["name"], stay["address"], stay["check_in_date"],
                         stay["check_out_date"], stay["latitude"], stay["longitude"],
                         stay["google_place_id"], stay["coordinates_updated_at"], source_region_id),
                    )
                transport_legs = query_db(
                    "SELECT * FROM trip_transport_legs WHERE trip_id = ? ORDER BY departure_at, id",
                    (source_trip["id"],),
                )
                for leg in transport_legs:
                    copied_file_name = None
                    if leg["stored_file_name"]:
                        safe_file_name = Path(leg["stored_file_name"]).name
                        source_file = os.path.join(app.config["TRANSPORT_UPLOAD_FOLDER"], safe_file_name)
                        if os.path.isfile(source_file):
                            extension = Path(safe_file_name).suffix
                            copied_file_name = f"{uuid.uuid4().hex}{extension}"
                            target_file = os.path.join(app.config["TRANSPORT_UPLOAD_FOLDER"], copied_file_name)
                            shutil.copy2(source_file, target_file)
                            copied_ticket_files.append(copied_file_name)
                    db.execute(
                        """INSERT INTO trip_transport_legs
                           (trip_id, mode, carrier, transport_number, origin, destination,
                            departure_at, arrival_at, original_file_name, stored_file_name)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (merged_trip_id, leg["mode"], leg["carrier"],
                         leg["transport_number"], leg["origin"], leg["destination"],
                         leg["departure_at"], leg["arrival_at"], leg["original_file_name"],
                         copied_file_name),
                    )

                places = query_db("SELECT * FROM places WHERE trip_id = ? ORDER BY id", (source_trip["id"],))
                place_id_map = {}
                for place in places:
                    provider_id = place["provider_place_id"]
                    merged_place_id = merged_places_by_provider.get(provider_id) if provider_id else None
                    if merged_place_id is None:
                        place_cursor = db.execute(
                            """INSERT INTO places
                               (trip_id, provider_place_id, name, address, latitude, longitude, category,
                                stay_duration, coordinates_updated_at, region_id)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (merged_trip_id, provider_id, place["name"], place["address"], place["latitude"],
                             place["longitude"], place["category"], place["stay_duration"],
                             place["coordinates_updated_at"], source_region_id),
                        )
                        merged_place_id = place_cursor.lastrowid
                        if provider_id:
                            merged_places_by_provider[provider_id] = merged_place_id
                    place_id_map[place["id"]] = merged_place_id

                source_days = query_db(
                    "SELECT id, date FROM itinerary_days WHERE trip_id = ? ORDER BY day_number",
                    (source_trip["id"],),
                )
                for source_day in source_days:
                    merged_day_id = merged_days.get(source_day["date"])
                    if merged_day_id is None:
                        continue
                    items = query_db(
                        "SELECT place_id, sequence, start_time, is_locked FROM itinerary_items WHERE day_id = ? ORDER BY sequence",
                        (source_day["id"],),
                    )
                    for item in items:
                        merged_place_id = place_id_map.get(item["place_id"])
                        if merged_place_id is None:
                            continue
                        already_added = db.execute(
                            "SELECT id FROM itinerary_items WHERE day_id = ? AND place_id = ?",
                            (merged_day_id, merged_place_id),
                        ).fetchone()
                        if already_added:
                            continue
                        sequence = db.execute(
                            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM itinerary_items WHERE day_id = ?",
                            (merged_day_id,),
                        ).fetchone()[0]
                        item_cursor = db.execute(
                            """INSERT INTO itinerary_items
                               (day_id, place_id, sequence, start_time, is_locked, region_id)
                               VALUES (?, ?, ?, ?, ?, ?)""",
                            (merged_day_id, merged_place_id, sequence, item["start_time"],
                             item["is_locked"], source_region_id),
                        )
            for (from_trip_id, to_trip_id, default_date), candidate in transfer_candidates.items():
                selected_date = request.form.get(
                    f"transfer_date_{candidate['from_stay_id']}_{candidate['to_stay_id']}", default_date
                ).strip()
                try:
                    transfer_date = date.fromisoformat(selected_date)
                except ValueError:
                    raise ValueError("국가 이동 날짜를 확인해 주세요.")
                if transfer_date < date.fromisoformat(start_date) or transfer_date > date.fromisoformat(end_date):
                    raise ValueError("국가 이동 날짜는 병합한 여행 기간 안에서 선택해 주세요.")
                transfer_date_text = transfer_date.isoformat()
                merged_day_id = merged_days[transfer_date_text]
                from_region_id = region_ids_by_source[from_trip_id]
                to_region_id = region_ids_by_source[to_trip_id]
                if from_region_id == to_region_id:
                    continue
                destination_item = db.execute(
                    "SELECT id FROM itinerary_items WHERE day_id = ? AND region_id = ? ORDER BY sequence LIMIT 1",
                    (merged_day_id, to_region_id),
                ).fetchone()
                origin_trip = next(trip for trip in source_trips if trip["id"] == from_trip_id)
                destination_trip = next(trip for trip in source_trips if trip["id"] == to_trip_id)
                origin_label = request.form.get(f"region_label_{from_trip_id}", "").strip() or origin_trip["city"]
                destination_label = request.form.get(f"region_label_{to_trip_id}", "").strip() or destination_trip["city"]
                db.execute(
                    """INSERT INTO itinerary_transfers
                       (trip_id, day_id, before_item_id, from_region_id, to_region_id, origin, destination)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (merged_trip_id, merged_day_id, destination_item["id"] if destination_item else None,
                     from_region_id, to_region_id, origin_label, destination_label),
                )
            db.commit()
        except (sqlite3.Error, ValueError, OSError):
            db.rollback()
            for file_name in copied_ticket_files:
                try:
                    os.remove(os.path.join(app.config["TRANSPORT_UPLOAD_FOLDER"], file_name))
                except OSError:
                    pass
            flash("여행을 병합하지 못했습니다. 선택한 여행과 날짜를 확인해 주세요.", "error")
            return redirect(url_for("merge_trips_page"))

        flash(f"‘{merged_title}’ 여행에 국가별 일정, 숙소와 교통편을 가져왔습니다. 국가 이동은 일정 사이에 표시했습니다. 원래 여행은 그대로 남아 있습니다.", "success")
        return redirect(url_for("trip_detail", trip_id=merged_trip_id))

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
        regions = query_db("SELECT id, label FROM trip_regions WHERE trip_id = ? ORDER BY sort_order, label", (trip_id,))
        return render_template(
            "accommodation.html", trip=trip, accommodations=accommodations,
            accommodation_search_results=[], accommodation_search_query="",
            editing_accommodation=None, editing_accommodation_id="", regions=regions,
        )

    @app.get("/trips/<int:trip_id>/places")
    def trip_places(trip_id):
        trip = query_db("SELECT * FROM trips WHERE id = ?", (trip_id,), one=True)
        if trip is None:
            abort(404)
        places = _trip_places(trip_id)
        regions = query_db("SELECT id, label FROM trip_regions WHERE trip_id = ? ORDER BY sort_order, label", (trip_id,))
        return render_template(
            "places.html", trip=trip, places=places, place_search_results=[],
            place_search_query="", place_search_category="attraction",
            regions=regions,
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
            "SELECT * FROM trip_regions WHERE trip_id = ? ORDER BY sort_order, label", (trip_id,)
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
            day_transfers_after = []
            for transfer in transfers:
                if selected_region and selected_region["id"] not in (transfer["from_region_id"], transfer["to_region_id"]):
                    continue
                if transfer["before_item_id"] in items_by_id:
                    items_by_id[transfer["before_item_id"]]["transfers_before"].append(transfer)
                elif items and selected_region and transfer["from_region_id"] == selected_region["id"]:
                    items[-1]["transfers_after"].append(transfer)
                elif items and selected_region and transfer["to_region_id"] == selected_region["id"]:
                    items[0]["transfers_before"].append(transfer)
                else:
                    day_transfers_after.append(transfer)
            place_days.update({item["place_id"]: day["day_number"] for item in items})
            place_sequences.update({item["place_id"]: item["sequence"] for item in items})
            days.append({
                "day_number": day["day_number"],
                "date": day["date"],
                "items": items,
                "accommodations_for_date": _accommodations_for_date(accommodations, day["date"]),
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
            flash("병합 여행의 국가별 일정은 기존 계획을 유지합니다.", "error")
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
            if transfer["before_item_id"] in items_by_id:
                items_by_id[transfer["before_item_id"]]["transfers_before"].append(transfer)
            else:
                transfers_after.append(transfer)
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
        accommodations = query_db(
            "SELECT * FROM accommodations WHERE trip_id = ? ORDER BY check_in_date, id", (trip_id,)
        )
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
            transfers_after=transfers_after,
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
        if category not in ("attraction", "restaurant"):
            category = "attraction"
        place_results, accommodation_results = [], []
        if not query:
            flash("검색할 장소 이름을 입력해 주세요.", "error")
        else:
            try:
                results = search_places(
                    app.config["GOOGLE_MAPS_API_KEY"], query, trip["city"]
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
                regions=query_db("SELECT id, label FROM trip_regions WHERE trip_id = ? ORDER BY sort_order, label", (trip_id,)),
            )
        regions = query_db("SELECT id, label FROM trip_regions WHERE trip_id = ? ORDER BY sort_order, label", (trip_id,))
        return render_template(
            "places.html", trip=trip, places=_trip_places(trip_id),
            place_search_results=place_results, place_search_query=query,
            place_search_category=category,
            regions=regions,
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
                   latitude = ?, longitude = ?, google_place_id = ?, coordinates_updated_at = ?, region_id = ?
                   WHERE id = ? AND trip_id = ?""",
                (name, address, check_in_date, check_out_date, *(coordinates or (None, None)), google_place_id,
                 _utc_now() if google_place_id and coordinates else None, region_id, accommodation_id, trip_id),
            )
        else:
            db.execute(
                """INSERT INTO accommodations
                   (trip_id, name, address, check_in_date, check_out_date, latitude, longitude,
                    google_place_id, coordinates_updated_at, region_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (trip_id, name, address, check_in_date, check_out_date, *(coordinates or (None, None)), google_place_id,
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
            db.commit()
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
            db.commit()
        except sqlite3.Error:
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
