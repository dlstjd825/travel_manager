from datetime import date, datetime, time as datetime_time, timedelta, timezone
import os
import sqlite3
import uuid
import math
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, flash, redirect, render_template, request, send_from_directory, url_for
from werkzeug.utils import secure_filename

from database.db import get_db, init_app, query_db
from services.itinerary import optimize_day_order, recommend_itinerary
from services.place_search import PlaceSearchError, search_places


def create_app(test_config=None):
    load_dotenv()
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY="local-travel-planner-session",
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

    init_app(app)
    os.makedirs(app.config["EXPENSE_UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["TRANSPORT_UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["PLACE_PHOTO_UPLOAD_FOLDER"], exist_ok=True)

    @app.get("/")
    def index():
        trips = query_db(
            """SELECT t.*, COUNT(DISTINCT p.id) AS place_count
               FROM trips AS t LEFT JOIN places AS p ON p.trip_id = t.id
               GROUP BY t.id ORDER BY t.start_date, t.id"""
        )
        trips = [
            {
                **dict(trip),
                "night_count": (date.fromisoformat(trip["end_date"]) - date.fromisoformat(trip["start_date"])).days,
                "day_count": (date.fromisoformat(trip["end_date"]) - date.fromisoformat(trip["start_date"])).days + 1,
            }
            for trip in trips
        ]
        return render_template("index.html", trips=trips, today=date.today().isoformat())

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
        accommodation = query_db(
            "SELECT * FROM accommodations WHERE trip_id = ?", (trip_id,), one=True
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
                 WHERE p.trip_id = ? AND (
                   TRIM(COALESCE(j.information, '')) <> '' OR TRIM(COALESCE(j.thoughts, '')) <> ''
                   OR EXISTS (SELECT 1 FROM place_photos ph WHERE ph.place_id = p.id)
                 )
               )""", (trip_id,), one=True,
        )["count"]
        trip_photo_count = query_db(
            "SELECT COUNT(*) AS count FROM place_photos WHERE trip_id = ?", (trip_id,), one=True
        )["count"]
        start = date.fromisoformat(trip["start_date"])
        end = date.fromisoformat(trip["end_date"])
        return render_template(
            "trip.html", trip=trip, accommodation=accommodation, days=days, todos=todos,
            expense_summary=expense_summary, place_count=place_count, transport_count=transport_count,
            journal_place_count=journal_place_count, trip_photo_count=trip_photo_count,
            day_count=(end - start).days + 1, night_count=(end - start).days,
        )

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
        accommodation = query_db(
            "SELECT * FROM accommodations WHERE trip_id = ?", (trip_id,), one=True
        )
        return render_template(
            "accommodation.html", trip=trip, accommodation=accommodation,
            accommodation_search_results=[], accommodation_search_query="",
        )

    @app.get("/trips/<int:trip_id>/places")
    def trip_places(trip_id):
        trip = query_db("SELECT * FROM trips WHERE id = ?", (trip_id,), one=True)
        if trip is None:
            abort(404)
        places = _trip_places(trip_id)
        return render_template(
            "places.html", trip=trip, places=places, place_search_results=[],
            place_search_query="", place_search_category="attraction",
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
        photos = query_db(
            "SELECT * FROM place_photos WHERE place_id = ? ORDER BY id DESC",
            (place_id,),
        )
        expenses = query_db(
            "SELECT * FROM trip_expenses WHERE trip_id = ? AND place_id = ? ORDER BY expense_date DESC, id DESC",
            (trip_id, place_id),
        )
        return render_template(
            "place_detail.html", trip=trip, place=place, journal=journal,
            photos=photos, expenses=expenses,
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
            """SELECT p.id, p.name, p.address, p.category, j.information, j.thoughts
               FROM places p LEFT JOIN place_journals j ON j.place_id = p.id
               WHERE p.trip_id = ? AND (
                 TRIM(COALESCE(j.information, '')) <> '' OR TRIM(COALESCE(j.thoughts, '')) <> ''
                 OR EXISTS (SELECT 1 FROM place_photos ph WHERE ph.place_id = p.id)
               ) ORDER BY p.name COLLATE NOCASE, p.id""",
            (trip_id,),
        )
        entries = []
        for row in place_rows:
            entry = dict(row)
            entry["photos"] = query_db(
                "SELECT id, original_file_name, caption FROM place_photos WHERE place_id = ? ORDER BY id DESC",
                (row["id"],),
            )
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
        day_rows = query_db(
            "SELECT * FROM itinerary_days WHERE trip_id = ? ORDER BY day_number", (trip_id,)
        )
        accommodation = query_db(
            "SELECT * FROM accommodations WHERE trip_id = ?", (trip_id,), one=True
        )
        places = query_db("SELECT * FROM places WHERE trip_id = ? ORDER BY name COLLATE NOCASE", (trip_id,))
        place_days = {}
        place_sequences = {}
        days = []
        for day in day_rows:
            items = query_db(
                """SELECT ii.sequence, ii.start_time, ii.is_locked,
                          p.id AS place_id, p.name, p.address,
                          p.latitude, p.longitude, p.category
                   FROM itinerary_items AS ii JOIN places AS p ON p.id = ii.place_id
                   WHERE ii.day_id = ? ORDER BY ii.sequence""",
                (day["id"],),
            )
            place_days.update({item["place_id"]: day["day_number"] for item in items})
            place_sequences.update({item["place_id"]: item["sequence"] for item in items})
            days.append({
                "day_number": day["day_number"],
                "date": day["date"],
                "items": items,
                "accommodation_status": _accommodation_status(accommodation, day["date"]),
            })
        scheduled_place_ids = set(place_days)
        unscheduled_places = [place for place in places if place["id"] not in scheduled_place_ids]
        map_points = _map_points(accommodation, places, place_days, place_sequences)
        stay_day_numbers = [day["day_number"] for day in days if day["accommodation_status"]]
        for point in map_points:
            if point["kind"] == "accommodation":
                point["stay_day_numbers"] = stay_day_numbers
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
            accommodation=accommodation,
            map_points=map_points,
            map_total=len(places) + (1 if accommodation else 0),
            map_days=list(range(1, (date.fromisoformat(trip["end_date"]) - date.fromisoformat(trip["start_date"])).days + 2)),
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
        places = query_db(
            "SELECT * FROM places WHERE trip_id = ? ORDER BY id", (trip_id,)
        )
        if not places:
            flash("먼저 가고 싶은 장소를 등록해 주세요.", "error")
            return redirect(url_for("trip_places", trip_id=trip_id))
        accommodation = query_db(
            "SELECT * FROM accommodations WHERE trip_id = ?", (trip_id,), one=True
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
            places, accommodation, trip["start_date"], day_count, day_availability
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
            if not (0 <= flight_buffer <= 12 and 0 <= train_buffer <= 12):
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
               (trip_id, flight_buffer_hours, train_buffer_hours, day_start_time, day_end_time,
                arrival_latest_start, departure_earliest_cutoff)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(trip_id) DO UPDATE SET
                flight_buffer_hours=excluded.flight_buffer_hours,
                train_buffer_hours=excluded.train_buffer_hours,
                day_start_time=excluded.day_start_time,
                day_end_time=excluded.day_end_time,
                arrival_latest_start=excluded.arrival_latest_start,
                departure_earliest_cutoff=excluded.departure_earliest_cutoff""",
            (trip_id, flight_buffer, train_buffer, day_start.strftime("%H:%M"),
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
            """SELECT p.*, ii.sequence, ii.is_locked
               FROM itinerary_items AS ii JOIN places AS p ON p.id = ii.place_id
               WHERE ii.day_id = ? ORDER BY ii.sequence""",
            (day["id"],),
        )
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
        accommodation = query_db(
            "SELECT * FROM accommodations WHERE trip_id = ?", (trip_id,), one=True
        )
        transport_legs = query_db(
            "SELECT * FROM trip_transport_legs WHERE trip_id = ? ORDER BY departure_at, id",
            (trip_id,),
        )
        availability = _build_day_availability(
            trip, transport_legs, _get_recommendation_settings(trip_id)
        )[day_number]
        place_days = {item["id"]: day_number for item in items}
        place_sequences = {item["id"]: index for index, item in enumerate(items, 1)}
        accommodation_status = _accommodation_status(accommodation, day["date"])
        map_accommodation = accommodation
        if accommodation and accommodation["check_in_date"] and not accommodation_status:
            map_accommodation = None
        return render_template(
            "day_detail.html", trip=trip, day=day, day_number=day_number,
            items=items, accommodation=accommodation, accommodation_status=accommodation_status,
            expenses_by_place=expenses_by_place,
            expense_categories=("숙박", "교통", "식비", "관광·티켓", "쇼핑", "기타"),
            currencies=("EUR", "USD", "KRW", "GBP", "CZK", "HUF", "CHF", "JPY"),
            budget_currency=trip["budget_currency"],
            day_availability=availability,
            map_points=_map_points(map_accommodation, items, place_days, place_sequences),
            map_total=len(items) + (1 if map_accommodation and map_accommodation["latitude"] is not None else 0),
            maps_enabled=bool(app.config.get("GOOGLE_MAPS_BROWSER_API_KEY")),
        )

    @app.post("/trips/<int:trip_id>/itinerary/day/<int:day_number>/optimize")
    def optimize_itinerary_day(trip_id, day_number):
        day = query_db(
            "SELECT id FROM itinerary_days WHERE trip_id = ? AND day_number = ?",
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
        accommodation = query_db(
            "SELECT * FROM accommodations WHERE trip_id = ?", (trip_id,), one=True
        )
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
            return render_template(
                "accommodation.html", trip=trip,
                accommodation=query_db("SELECT * FROM accommodations WHERE trip_id = ?", (trip_id,), one=True),
                accommodation_search_results=accommodation_results,
                accommodation_search_query=query,
            )
        return render_template(
            "places.html", trip=trip, places=_trip_places(trip_id),
            place_search_results=place_results, place_search_query=query,
            place_search_category=category,
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
            "INSERT INTO trips (title, city, start_date, end_date) VALUES (?, ?, ?, ?)",
            (title, city, start_date, end_date),
        )
        connection.commit()
        flash(f"‘{title}’ 여행을 만들었습니다.", "success")
        return redirect(url_for("index"))

    @app.post("/trips/<int:trip_id>/delete")
    def delete_trip(trip_id):
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
        try:
            start = date.fromisoformat(request.form.get("start_date", ""))
            end = date.fromisoformat(request.form.get("end_date", ""))
        except ValueError:
            flash("여행 시작일과 종료일을 확인해 주세요.", "error")
            return redirect(url_for("trip_detail", trip_id=trip_id))
        if end < start:
            flash("종료일은 시작일보다 빠를 수 없습니다.", "error")
            return redirect(url_for("trip_detail", trip_id=trip_id))

        accommodation = query_db(
            "SELECT check_in_date, check_out_date FROM accommodations WHERE trip_id = ?",
            (trip_id,), one=True,
        )
        if accommodation and accommodation["check_in_date"] and accommodation["check_out_date"]:
            check_in = date.fromisoformat(accommodation["check_in_date"])
            check_out = date.fromisoformat(accommodation["check_out_date"])
            if check_in < start or check_out > end:
                flash("여행 기간 안에 숙소 체크인·체크아웃 날짜가 포함되어야 합니다. 숙소 날짜를 먼저 수정해 주세요.", "error")
                return redirect(url_for("trip_detail", trip_id=trip_id))
        transport_legs = query_db(
            "SELECT leg_type, departure_at, arrival_at FROM trip_transport_legs WHERE trip_id = ?",
            (trip_id,),
        )
        for leg in transport_legs:
            event_date = date.fromisoformat(
                leg["arrival_at"][:10] if leg["leg_type"] == "arrival" else leg["departure_at"][:10]
            )
            if event_date < start or event_date > end:
                flash("등록된 항공·기차 도착일 또는 출발일이 여행 기간 밖으로 나갑니다. 여행 기간이나 교통편을 먼저 수정해 주세요.", "error")
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
        db = get_db()
        existing = db.execute(
            "SELECT id FROM accommodations WHERE trip_id = ?", (trip_id,)
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE accommodations SET name = ?, address = ?, check_in_date = ?, check_out_date = ?, latitude = ?, longitude = ?, google_place_id = ?, coordinates_updated_at = ? WHERE trip_id = ?",
                (name, address, check_in_date, check_out_date, *(coordinates or (None, None)), google_place_id,
                 _utc_now() if google_place_id and coordinates else None, trip_id),
            )
        else:
            db.execute(
                "INSERT INTO accommodations (trip_id, name, address, check_in_date, check_out_date, latitude, longitude, google_place_id, coordinates_updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (trip_id, name, address, check_in_date, check_out_date, *(coordinates or (None, None)), google_place_id,
                 _utc_now() if google_place_id and coordinates else None),
            )
        db.commit()
        flash("숙소 정보를 저장했습니다.", "success")
        return redirect(url_for("trip_accommodation", trip_id=trip_id))

    @app.post("/trips/<int:trip_id>/transport")
    def add_transport_leg(trip_id):
        trip = query_db("SELECT * FROM trips WHERE id = ?", (trip_id,), one=True)
        if trip is None:
            abort(404)
        mode = request.form.get("mode", "")
        leg_type = request.form.get("leg_type", "")
        carrier = request.form.get("carrier", "").strip()[:120]
        transport_number = request.form.get("transport_number", "").strip()[:80]
        origin = request.form.get("origin", "").strip()[:180]
        destination = request.form.get("destination", "").strip()[:180]
        departure_value = request.form.get("departure_at", "")
        arrival_value = request.form.get("arrival_at", "")
        if mode not in {"flight", "train"} or leg_type not in {"arrival", "departure"}:
            flash("교통수단과 도착·출발 유형을 선택해 주세요.", "error")
            return redirect(url_for("trip_transport", trip_id=trip_id))
        try:
            departure_at = datetime.fromisoformat(departure_value)
            arrival_at = datetime.fromisoformat(arrival_value)
            trip_start = date.fromisoformat(trip["start_date"])
            trip_end = date.fromisoformat(trip["end_date"])
        except ValueError:
            flash("출발·도착 날짜와 시간을 입력해 주세요.", "error")
            return redirect(url_for("trip_transport", trip_id=trip_id))
        event_date = arrival_at.date() if leg_type == "arrival" else departure_at.date()
        if not origin or not destination:
            flash("출발지와 도착지를 입력해 주세요.", "error")
            return redirect(url_for("trip_transport", trip_id=trip_id))
        if not trip_start <= event_date <= trip_end:
            flash("도착편은 도착일, 출발편은 출발일이 여행 기간 안에 있어야 합니다.", "error")
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
                   (trip_id, mode, leg_type, carrier, transport_number, origin, destination,
                    departure_at, arrival_at, original_file_name, stored_file_name)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (trip_id, mode, leg_type, carrier, transport_number, origin, destination,
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
        flash("항공·기차 정보를 저장했습니다. 다음에 일정을 다시 추천할 때 반영됩니다.", "success")
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
                           coordinates_updated_at = ? WHERE id = ? AND trip_id = ?""",
                        (provider_place_id, *coordinates, _utc_now(), duplicate["id"], trip_id),
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
                    category, coordinates_updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (trip_id, provider_place_id, name, address,
                 *(coordinates or (None, None)), category,
                 _utc_now() if provider_place_id and coordinates else None),
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
            "SELECT id, name FROM places WHERE id = ? AND trip_id = ?",
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
            "INSERT INTO itinerary_items (day_id, place_id, sequence, start_time) VALUES (?, ?, ?, NULL)",
            (day["id"], place_id, sequence),
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

    @app.post("/trips/<int:trip_id>/accommodation/delete")
    def delete_accommodation(trip_id):
        cursor = get_db().execute(
            "DELETE FROM accommodations WHERE trip_id = ?", (trip_id,)
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
    if not accommodation or not accommodation["check_in_date"] or not accommodation["check_out_date"]:
        return None
    if day_date < accommodation["check_in_date"] or day_date > accommodation["check_out_date"]:
        return None
    if day_date == accommodation["check_in_date"]:
        return "체크인"
    if day_date == accommodation["check_out_date"]:
        return "체크아웃"
    return "숙박"


def _get_recommendation_settings(trip_id):
    row = query_db(
        "SELECT * FROM trip_recommendation_settings WHERE trip_id = ?",
        (trip_id,), one=True,
    )
    return dict(row) if row else {
        "flight_buffer_hours": 4.0,
        "train_buffer_hours": 2.0,
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
            "flight_buffer_hours" if mode == "flight" else "train_buffer_hours",
            4 if mode == "flight" else 2,
        ))
        label = "항공" if mode == "flight" else "기차"
        if leg["leg_type"] == "arrival":
            event = datetime.fromisoformat(leg["arrival_at"])
            day_number = (event.date() - start_date).days + 1
            if day_number not in days:
                continue
            available = event + timedelta(hours=buffer_hours)
            if available.date() != event.date() or available.time() >= arrival_latest_start or available.time() >= day_end:
                blocked_reasons[day_number].append(
                    f"{label} 도착 후 여유 시간이 늦어 그날은 비워 두었습니다."
                )
            else:
                start_bounds[day_number] = max(
                    start_bounds.get(day_number, day_start), available.time()
                )
        else:
            event = datetime.fromisoformat(leg["departure_at"])
            day_number = (event.date() - start_date).days + 1
            if day_number not in days:
                continue
            available = event - timedelta(hours=buffer_hours)
            if available.date() != event.date() or available.time() <= departure_earliest_cutoff or available.time() <= day_start:
                blocked_reasons[day_number].append(
                    f"{label} 출발 전 준비 시간을 고려해 그날은 비워 두었습니다."
                )
            else:
                end_bounds[day_number] = min(
                    end_bounds.get(day_number, day_end), available.time()
                )

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
    if accommodation and accommodation["latitude"] is not None and accommodation["longitude"] is not None:
        points.append({
            "id": "accommodation",
            "name": accommodation["name"],
            "address": accommodation["address"],
            "latitude": accommodation["latitude"],
            "longitude": accommodation["longitude"],
            "kind": "accommodation",
            "check_in_date": accommodation["check_in_date"],
            "check_out_date": accommodation["check_out_date"],
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
