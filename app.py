from datetime import date, datetime, timedelta, timezone
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
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,
        EXPENSE_UPLOAD_FOLDER=os.path.join(app.instance_path, "expense_uploads"),
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
        places = query_db(
            """SELECT p.*, MIN(d.day_number) AS visit_day FROM places AS p
               LEFT JOIN itinerary_items AS ii ON ii.place_id = p.id
               LEFT JOIN itinerary_days AS d ON d.id = ii.day_id
               WHERE p.trip_id = ? GROUP BY p.id
               ORDER BY CASE WHEN MIN(d.day_number) IS NULL THEN 1 ELSE 0 END,
                        MIN(d.day_number), p.name COLLATE NOCASE""",
            (trip_id,),
        )
        scheduled_rows = query_db(
            """SELECT ii.place_id, d.day_number FROM itinerary_items AS ii
               JOIN itinerary_days AS d ON d.id = ii.day_id WHERE d.trip_id = ?""",
            (trip_id,),
        )
        place_days = {row["place_id"]: row["day_number"] for row in scheduled_rows}
        start = date.fromisoformat(trip["start_date"])
        end = date.fromisoformat(trip["end_date"])
        return render_template(
            "trip.html", trip=trip, accommodation=accommodation, places=places,
            day_count=(end - start).days + 1, night_count=(end - start).days,
            place_search_results=[], accommodation_search_results=[],
            place_search_query="", accommodation_search_query="",
            map_points=_map_points(accommodation, places),
            map_total=len(places) + (1 if accommodation else 0),
            place_search_category="attraction",
            accommodation_edit_open=request.args.get("edit_accommodation") == "1",
            place_days=place_days,
        )

    @app.get("/trips/<int:trip_id>/expenses")
    def trip_expenses(trip_id):
        trip = query_db("SELECT * FROM trips WHERE id = ?", (trip_id,), one=True)
        if trip is None:
            abort(404)
        expenses = query_db(
            "SELECT * FROM trip_expenses WHERE trip_id = ? ORDER BY expense_date DESC, id DESC",
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
        title = request.form.get("title", "").strip()
        category = request.form.get("category", "기타")
        expense_date = request.form.get("expense_date", "")
        currency = request.form.get("currency", "EUR").upper()
        note = request.form.get("note", "").strip()
        allowed_currencies = {"EUR", "USD", "KRW", "GBP", "CZK", "HUF", "CHF", "JPY"}
        allowed_categories = {"숙박", "교통", "식비", "관광·티켓", "쇼핑", "기타"}
        try:
            amount = float(request.form.get("amount", ""))
            date.fromisoformat(expense_date)
        except (ValueError, TypeError):
            flash("날짜와 금액을 올바르게 입력해 주세요.", "error")
            return redirect(url_for("trip_expenses", trip_id=trip_id))
        if not title or not math.isfinite(amount) or amount < 0 or category not in allowed_categories or currency not in allowed_currencies:
            flash("지출 정보를 확인해 주세요.", "error")
            return redirect(url_for("trip_expenses", trip_id=trip_id))
        uploaded = request.files.get("attachment")
        original_name = None
        stored_name = None
        if uploaded and uploaded.filename:
            original_name = secure_filename(uploaded.filename) or "첨부파일"
            extension = Path(original_name).suffix.lower()
            if extension not in {".jpg", ".jpeg", ".png", ".webp", ".pdf"}:
                flash("영수증 또는 티켓은 JPG, PNG, WEBP, PDF 파일로 첨부해 주세요.", "error")
                return redirect(url_for("trip_expenses", trip_id=trip_id))
            stored_name = f"{uuid.uuid4().hex}{extension}"
            uploaded.save(os.path.join(app.config["EXPENSE_UPLOAD_FOLDER"], stored_name))
        try:
            db = get_db()
            db.execute(
                """INSERT INTO trip_expenses
                   (trip_id, title, category, expense_date, amount, currency, note, original_file_name, stored_file_name)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (trip_id, title, category, expense_date, amount, currency, note, original_name, stored_name),
            )
            db.commit()
        except sqlite3.Error:
            get_db().rollback()
            if stored_name:
                os.remove(os.path.join(app.config["EXPENSE_UPLOAD_FOLDER"], stored_name))
            flash("지출 내역을 저장하지 못했습니다.", "error")
            return redirect(url_for("trip_expenses", trip_id=trip_id))
        flash("지출 내역을 저장했습니다.", "success")
        return redirect(url_for("trip_expenses", trip_id=trip_id))

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
            "SELECT stored_file_name FROM trip_expenses WHERE id = ? AND trip_id = ?",
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
            })
        scheduled_place_ids = set(place_days)
        unscheduled_places = [place for place in places if place["id"] not in scheduled_place_ids]
        return render_template(
            "itinerary.html", trip=trip, days=days, has_itinerary=bool(day_rows),
            accommodation=accommodation,
            map_points=_map_points(accommodation, places, place_days, place_sequences),
            map_total=len(places) + (1 if accommodation else 0),
            map_days=list(range(1, (date.fromisoformat(trip["end_date"]) - date.fromisoformat(trip["start_date"])).days + 2)),
            maps_enabled=bool(app.config.get("GOOGLE_MAPS_BROWSER_API_KEY")),
            night_count=(date.fromisoformat(trip["end_date"]) - date.fromisoformat(trip["start_date"])).days,
            day_count=(date.fromisoformat(trip["end_date"]) - date.fromisoformat(trip["start_date"])).days + 1,
            unscheduled_places=unscheduled_places,
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
            return redirect(url_for("trip_detail", trip_id=trip_id))
        accommodation = query_db(
            "SELECT * FROM accommodations WHERE trip_id = ?", (trip_id,), one=True
        )
        start = date.fromisoformat(trip["start_date"])
        end = date.fromisoformat(trip["end_date"])
        day_count = (end - start).days + 1
        days = recommend_itinerary(places, accommodation, trip["start_date"], day_count)

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
            return redirect(url_for("trip_detail", trip_id=trip_id))
        flash("여행 일정을 날짜별로 추천하고 저장했습니다.", "success")
        return redirect(url_for("trip_itinerary", trip_id=trip_id))

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
            return redirect(url_for("trip_itinerary", trip_id=trip_id))
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
            return redirect(url_for("trip_itinerary", trip_id=trip_id))
        flash(f"Day {day_number} 방문 순서를 추천해 적용했습니다.", "success")
        return redirect(url_for("trip_itinerary", trip_id=trip_id))

    @app.post("/trips/<int:trip_id>/itinerary/places/<int:place_id>/toggle-lock")
    def toggle_itinerary_place_lock(trip_id, place_id):
        item = query_db(
            """SELECT ii.id, ii.is_locked FROM itinerary_items AS ii
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
        accommodation = query_db(
            "SELECT * FROM accommodations WHERE trip_id = ?", (trip_id,), one=True
        )
        places = query_db(
            """SELECT p.*, MIN(d.day_number) AS visit_day FROM places AS p
               LEFT JOIN itinerary_items AS ii ON ii.place_id = p.id
               LEFT JOIN itinerary_days AS d ON d.id = ii.day_id
               WHERE p.trip_id = ? GROUP BY p.id
               ORDER BY CASE WHEN MIN(d.day_number) IS NULL THEN 1 ELSE 0 END,
                        MIN(d.day_number), p.name COLLATE NOCASE""",
            (trip_id,),
        )
        start = date.fromisoformat(trip["start_date"])
        end = date.fromisoformat(trip["end_date"])
        return render_template(
            "trip.html", trip=trip, accommodation=accommodation, places=places,
            day_count=(end - start).days + 1, night_count=(end - start).days,
            place_search_results=place_results,
            accommodation_search_results=accommodation_results,
            place_search_query=query if target != "accommodation" else "",
            accommodation_search_query=query if target == "accommodation" else "",
            map_points=_map_points(accommodation, places),
            map_total=len(places) + (1 if accommodation else 0),
            place_search_category=category,
            accommodation_edit_open=(target == "accommodation"),
            place_days={row["place_id"]: row["day_number"] for row in query_db(
                """SELECT ii.place_id, d.day_number FROM itinerary_items ii
                   JOIN itinerary_days d ON d.id = ii.day_id WHERE d.trip_id = ?""",
                (trip_id,),
            )},
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
        connection = get_db()
        cursor = connection.execute("DELETE FROM trips WHERE id = ?", (trip_id,))
        connection.commit()
        if cursor.rowcount == 0:
            abort(404)
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
        if query_db("SELECT id FROM trips WHERE id = ?", (trip_id,), one=True) is None:
            abort(404)
        name = request.form.get("name", "").strip()
        address = request.form.get("address", "").strip()
        latitude = request.form.get("latitude", "").strip()
        longitude = request.form.get("longitude", "").strip()
        google_place_id = request.form.get("google_place_id", "").strip() or None
        if not name or not address:
            flash("숙소 이름과 주소를 입력해 주세요.", "error")
            return redirect(url_for("trip_detail", trip_id=trip_id))
        coordinates = _parse_coordinates(latitude, longitude)
        if coordinates is None and (latitude or longitude):
            flash("위도와 경도를 올바르게 함께 입력해 주세요.", "error")
            return redirect(url_for("trip_detail", trip_id=trip_id))
        db = get_db()
        existing = db.execute(
            "SELECT id FROM accommodations WHERE trip_id = ?", (trip_id,)
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE accommodations SET name = ?, address = ?, latitude = ?, longitude = ?, google_place_id = ?, coordinates_updated_at = ? WHERE trip_id = ?",
                (name, address, *(coordinates or (None, None)), google_place_id,
                 _utc_now() if google_place_id and coordinates else None, trip_id),
            )
        else:
            db.execute(
                "INSERT INTO accommodations (trip_id, name, address, latitude, longitude, google_place_id, coordinates_updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (trip_id, name, address, *(coordinates or (None, None)), google_place_id,
                 _utc_now() if google_place_id and coordinates else None),
            )
        db.commit()
        flash("숙소 정보를 저장했습니다.", "success")
        return redirect(url_for("trip_detail", trip_id=trip_id))

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
            return redirect(url_for("trip_detail", trip_id=trip_id))
        if category not in ("attraction", "restaurant"):
            flash("장소 유형을 선택해 주세요.", "error")
            return redirect(url_for("trip_detail", trip_id=trip_id))
        coordinates = _parse_coordinates(latitude, longitude)
        if coordinates is None and (latitude or longitude):
            flash("위도와 경도를 올바르게 함께 입력해 주세요.", "error")
            return redirect(url_for("trip_detail", trip_id=trip_id))
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
                    return redirect(url_for("trip_detail", trip_id=trip_id))
                except sqlite3.IntegrityError:
                    get_db().rollback()
            flash("이미 등록된 장소입니다.", "error")
            return redirect(url_for("trip_detail", trip_id=trip_id))
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
            return redirect(url_for("trip_detail", trip_id=trip_id))
        flash(f"‘{name}’ 장소를 추가했습니다.", "success")
        return redirect(url_for("trip_detail", trip_id=trip_id))

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
        cursor = get_db().execute(
            "DELETE FROM places WHERE id = ? AND trip_id = ?", (place_id, trip_id)
        )
        get_db().commit()
        if cursor.rowcount == 0:
            abort(404)
        flash("장소를 삭제했습니다.", "success")
        return redirect(url_for("trip_detail", trip_id=trip_id))

    @app.post("/trips/<int:trip_id>/itinerary/places/<int:place_id>/delete")
    def remove_itinerary_place(trip_id, place_id):
        cursor = get_db().execute(
            """DELETE FROM itinerary_items WHERE place_id = ? AND day_id IN
               (SELECT id FROM itinerary_days WHERE trip_id = ?)""",
            (place_id, trip_id),
        )
        get_db().commit()
        if cursor.rowcount == 0:
            abort(404)
        flash("장소를 일정에서 제외했습니다.", "success")
        return redirect(url_for("trip_itinerary", trip_id=trip_id))

    @app.post("/trips/<int:trip_id>/accommodation/delete")
    def delete_accommodation(trip_id):
        cursor = get_db().execute(
            "DELETE FROM accommodations WHERE trip_id = ?", (trip_id,)
        )
        get_db().commit()
        if cursor.rowcount == 0:
            abort(404)
        flash("숙소를 삭제했습니다.", "success")
        return redirect(url_for("trip_detail", trip_id=trip_id))

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


def _utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


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
