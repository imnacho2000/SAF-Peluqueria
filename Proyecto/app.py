import csv
import os
import random
import sqlite3
from datetime import datetime, timedelta

from flask import Flask, abort, current_app, flash, g, has_app_context, redirect, render_template, request, send_file, session, url_for

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
except ImportError:  # pragma: no cover - optional dependency
    service_account = None
    build = None

DEFAULT_DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "appointments.db"))


def create_app(db_path=None):
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "dev-secret")
    app.config["DATABASE_PATH"] = db_path or os.environ.get("DATABASE_PATH", DEFAULT_DB_PATH)
    app.config["AGENDA_PASSWORD"] = os.environ.get("AGENDA_PASSWORD", "123").strip()

    app.teardown_appcontext(close_db)
    init_db(app.config["DATABASE_PATH"])

    @app.route("/")
    def index():
        selected = None
        appointments = []
        access_code = request.args.get("access_code", "").strip()
        if access_code:
            appointment = get_appointment_by_code(access_code)
            if appointment is not None:
                appointments = [appointment]
                selected = appointment
        return render_template("index.html", appointments=appointments, selected=selected)

    def agenda_access_granted():
        return session.get("agenda_access") is True

    @app.route("/agenda", methods=["GET", "POST"])
    def agenda():
        if not agenda_access_granted():
            error_message = None
            if request.method == "POST":
                provided_password = request.form.get("agenda_password", "").strip()
                if provided_password == app.config.get("AGENDA_PASSWORD", "").strip():
                    session["agenda_access"] = True
                    return redirect(url_for("agenda"))
                error_message = "Contraseña incorrecta"
            return render_template("agenda_login.html", error_message=error_message)

        appointments = get_appointments()
        search_date = request.args.get("search_date", "").strip()
        if not search_date:
            search_date = datetime.now().strftime("%Y-%m-%d")

        try:
            parsed_search_date = datetime.strptime(search_date, "%Y-%m-%d")
            search_date_display = parsed_search_date.strftime("%d/%m/%Y")
        except ValueError:
            search_date_display = search_date

        appointments = [a for a in appointments if a["appointment_date"] == search_date_display]
        appointments.sort(key=lambda item: (item["appointment_date"], item["appointment_time"]))

        page = request.args.get("page", 1, type=int)
        per_page = 10
        total_pages = max(1, (len(appointments) + per_page - 1) // per_page)
        if page < 1:
            page = 1
        if page > total_pages:
            page = total_pages
        start = (page - 1) * per_page
        end = start + per_page
        paginated_appointments = appointments[start:end]

        today_date = datetime.now().strftime("%Y-%m-%d")
        return render_template(
            "agenda.html",
            appointments=paginated_appointments,
            search_date=search_date or today_date,
            today_date=today_date,
            page=page,
            total_pages=total_pages,
            total_results=len(appointments),
        )

    @app.route("/agenda/change-password", methods=["GET", "POST"])
    def change_agenda_password():
        if request.method == "POST":
            current_password = request.form.get("current_password", "").strip()
            new_password = request.form.get("new_password", "").strip()
            confirm_password = request.form.get("confirm_password", "").strip()

            stored_password = app.config.get("AGENDA_PASSWORD", "123").strip()
            if current_password != stored_password:
                flash("La contraseña actual no es correcta", "danger")
                return redirect(url_for("change_agenda_password"))

            if not new_password or len(new_password) < 4:
                flash("La nueva contraseña debe tener al menos 4 caracteres", "danger")
                return redirect(url_for("change_agenda_password"))

            if new_password != confirm_password:
                flash("La nueva contraseña y la confirmación no coinciden", "danger")
                return redirect(url_for("change_agenda_password"))

            app.config["AGENDA_PASSWORD"] = new_password
            session.pop("agenda_access", None)
            flash("Contraseña de agenda actualizada", "success")
            return redirect(url_for("agenda"))

        return render_template("change_password.html")

    @app.route("/export/csv")
    def export_csv():
        if not agenda_access_granted():
            return redirect(url_for("agenda"))
        appointments = get_appointments()
        appointments.sort(key=lambda item: (item["appointment_date"], item["appointment_time"]))

        temp_path = os.path.join(app.root_path, "appointments_export.csv")
        with open(temp_path, "w", newline="", encoding="utf-8-sig") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=["date", "time", "client", "service", "phone", "access_code"])
            writer.writeheader()
            for appointment in appointments:
                writer.writerow({
                    "date": appointment["appointment_date"],
                    "time": appointment["appointment_time"],
                    "client": f"{appointment['client_name']} {appointment['client_last_name']}",
                    "service": appointment["service"],
                    "phone": appointment["phone"],
                    "access_code": appointment["access_code"],
                })

        return send_file(temp_path, as_attachment=True, download_name="turnos.csv")

    @app.route("/availability")
    def availability():
        appointment_date = request.args.get("appointment_date", "").strip()
        if not appointment_date:
            return {"available_times": []}

        parsed_date = None
        if len(appointment_date) == 10 and appointment_date[4] == "-":
            try:
                parsed_date = datetime.strptime(appointment_date, "%Y-%m-%d")
            except ValueError:
                parsed_date = None
        else:
            try:
                parsed_date = datetime.strptime(appointment_date, "%d/%m/%Y")
            except ValueError:
                parsed_date = None

        if parsed_date is None:
            return {"available_times": []}

        if parsed_date.weekday() not in {1, 2, 3, 4, 5} or is_argentina_public_holiday(parsed_date):
            return {"available_times": []}

        slots = []
        for hour in ["10:00", "10:30", "11:00", "11:30", "12:00", "12:30", "13:00", "14:30", "15:00", "15:30", "16:00", "16:30", "17:00", "17:30", "18:00", "18:30", "19:00", "19:30", "20:00"]:
            if is_valid_business_datetime(datetime.strptime(f"{parsed_date.strftime('%Y-%m-%d')} {hour}", "%Y-%m-%d %H:%M")):
                if not appointment_exists(parsed_date.strftime("%d/%m/%Y"), hour):
                    slots.append(hour)

        return {"available_times": slots}

    @app.route("/appointments", methods=["POST"])
    def create_appointment():
        client_name = request.form.get("client_name", "").strip()
        client_last_name = request.form.get("client_last_name", "").strip()
        phone = request.form.get("phone", "").strip()
        service = request.form.get("service", "").strip()
        appointment_date = request.form.get("appointment_date", "").strip()
        appointment_time = request.form.get("appointment_time", "").strip()

        errors = []
        if not client_name:
            errors.append("El nombre es obligatorio")
        if not client_last_name:
            errors.append("El apellido es obligatorio")
        if not phone:
            errors.append("El teléfono es obligatorio")
        if not service:
            errors.append("El servicio es obligatorio")
        if not appointment_date or not appointment_time:
            errors.append("La fecha y la hora son obligatorias")

        parsed_datetime = None
        if not errors:
            parsed_datetime = parse_appointment_datetime(appointment_date, appointment_time)
            if parsed_datetime is None:
                errors.append("La fecha o la hora tienen un formato inválido")

        if not errors and not is_valid_business_datetime(parsed_datetime):
            errors.append("Fuera del horario de atención (martes a sábado, 10:00 a 13:00 y 14:30 a 20:00)")

        if not errors and not is_future_datetime(parsed_datetime):
            errors.append("Ese turno ya no está disponible")

        if not errors and appointment_exists(parsed_datetime.strftime("%d/%m/%Y"), parsed_datetime.strftime("%H:%M")):
            errors.append("Ese turno ya está ocupado")

        if errors:
            for message in errors:
                flash(message, "danger")
            return redirect(url_for("index"))

        access_code = generate_access_code()
        conn = get_db()
        cursor = conn.execute(
            """
            INSERT INTO appointments (client_name, client_last_name, phone, service, appointment_date, appointment_time, access_code, google_event_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (client_name, client_last_name, phone, service, parsed_datetime.strftime("%d/%m/%Y"), parsed_datetime.strftime("%H:%M"), access_code, None),
        )
        appointment_id = cursor.lastrowid
        conn.commit()

        appointment = {
            "id": appointment_id,
            "client_name": client_name,
            "client_last_name": client_last_name,
            "phone": phone,
            "service": service,
            "appointment_date": parsed_datetime.strftime("%d/%m/%Y"),
            "appointment_time": parsed_datetime.strftime("%H:%M"),
            "access_code": access_code,
            "google_event_id": None,
        }
        sync_to_google_calendar("create", appointment)
        flash(f"Turno creado correctamente. Tu código de turno es {access_code}", "success")
        return redirect(url_for("index", access_code=access_code))

    @app.route("/appointments/lookup", methods=["POST"])
    def lookup_appointment():
        access_code = request.form.get("access_code", "").strip()
        if not access_code:
            flash("Ingresá un código de turno", "danger")
            return redirect(url_for("index"))

        appointment = get_appointment_by_code(access_code)
        if appointment is None:
            flash("No se encontró un turno con ese código", "danger")
            return redirect(url_for("index"))

        return redirect(url_for("index", access_code=access_code))

    @app.route("/appointments/<int:appointment_id>/edit")
    def edit_appointment(appointment_id):
        appointment = get_appointment(appointment_id)
        if appointment is None:
            abort(404)
        return render_template("index.html", appointments=[appointment], selected=appointment)

    @app.route("/appointments/<int:appointment_id>/edit", methods=["POST"])
    def update_appointment(appointment_id):
        appointment = get_appointment(appointment_id)
        if appointment is None:
            abort(404)

        provided_code = request.form.get("access_code", "").strip()
        if provided_code != appointment.get("access_code"):
            flash("Código de turno incorrecto", "danger")
            return redirect(url_for("edit_appointment", appointment_id=appointment_id))

        client_name = request.form.get("client_name", "").strip()
        client_last_name = request.form.get("client_last_name", "").strip()
        phone = request.form.get("phone", "").strip()
        service = request.form.get("service", "").strip()
        appointment_date = request.form.get("appointment_date", "").strip()
        appointment_time = request.form.get("appointment_time", "").strip()

        parsed_datetime = parse_appointment_datetime(appointment_date, appointment_time)
        if parsed_datetime is None:
            flash("La fecha o la hora tienen un formato inválido", "danger")
            return redirect(url_for("edit_appointment", appointment_id=appointment_id))

        if not is_valid_business_datetime(parsed_datetime):
            flash("Fuera del horario de atención", "danger")
            return redirect(url_for("edit_appointment", appointment_id=appointment_id))

        if not is_future_datetime(parsed_datetime):
            flash("Ese turno ya no está disponible", "danger")
            return redirect(url_for("edit_appointment", appointment_id=appointment_id))

        if appointment_exists(parsed_datetime.strftime("%d/%m/%Y"), parsed_datetime.strftime("%H:%M"), appointment_id):
            flash("Ese turno ya está ocupado", "danger")
            return redirect(url_for("edit_appointment", appointment_id=appointment_id))

        if not client_name or not client_last_name or not phone or not service or not appointment_date or not appointment_time:
            flash("Todos los campos son obligatorios", "danger")
            return redirect(url_for("edit_appointment", appointment_id=appointment_id))

        conn = get_db()
        conn.execute(
            """
            UPDATE appointments
            SET client_name = ?, client_last_name = ?, phone = ?, service = ?, appointment_date = ?, appointment_time = ?
            WHERE id = ?
            """,
            (client_name, client_last_name, phone, service, parsed_datetime.strftime("%d/%m/%Y"), parsed_datetime.strftime("%H:%M"), appointment_id),
        )
        conn.commit()

        updated_appointment = {
            "id": appointment_id,
            "client_name": client_name,
            "client_last_name": client_last_name,
            "phone": phone,
            "service": service,
            "appointment_date": parsed_datetime.strftime("%d/%m/%Y"),
            "appointment_time": parsed_datetime.strftime("%H:%M"),
            "access_code": appointment["access_code"],
            "google_event_id": appointment["google_event_id"],
        }
        sync_to_google_calendar("update", updated_appointment)
        flash("Turno actualizado correctamente", "success")
        return redirect(url_for("index", access_code=appointment["access_code"]))

    @app.route("/appointments/<int:appointment_id>/delete", methods=["POST"])
    def delete_appointment(appointment_id):
        appointment = get_appointment(appointment_id)
        if appointment is None:
            abort(404)

        provided_code = request.form.get("access_code", "").strip()
        if provided_code != appointment.get("access_code"):
            flash("Código de turno incorrecto", "danger")
            return redirect(url_for("index"))

        sync_to_google_calendar("delete", appointment)
        conn = get_db()
        conn.execute("DELETE FROM appointments WHERE id = ?", (appointment_id,))
        conn.commit()
        flash("Turno eliminado", "success")
        return redirect(url_for("index"))

    return app


def init_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_name TEXT NOT NULL,
            client_last_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            service TEXT NOT NULL,
            appointment_date TEXT NOT NULL,
            appointment_time TEXT NOT NULL,
            access_code TEXT NOT NULL,
            google_event_id TEXT
        )
        """
    )

    columns = {row[1] for row in conn.execute("PRAGMA table_info(appointments)")}
    if "client_last_name" not in columns:
        conn.execute("ALTER TABLE appointments ADD COLUMN client_last_name TEXT")
    if "access_code" not in columns:
        conn.execute("ALTER TABLE appointments ADD COLUMN access_code TEXT")

    conn.commit()
    conn.close()


def get_db():
    if not has_app_context():
        conn = sqlite3.connect(DEFAULT_DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE_PATH"])
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def get_appointments():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM appointments ORDER BY appointment_date, appointment_time"
    ).fetchall()
    if not has_app_context():
        conn.close()
    return [dict(row) for row in rows]


def get_appointment(appointment_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM appointments WHERE id = ?", (appointment_id,)).fetchone()
    if not has_app_context():
        conn.close()
    return dict(row) if row is not None else None


def get_appointment_by_code(access_code):
    conn = get_db()
    row = conn.execute("SELECT * FROM appointments WHERE access_code = ?", (access_code,)).fetchone()
    if not has_app_context():
        conn.close()
    return dict(row) if row is not None else None


def appointment_exists(appointment_date, appointment_time, exclude_id=None):
    conn = get_db()
    if exclude_id is None:
        row = conn.execute(
            "SELECT id FROM appointments WHERE appointment_date = ? AND appointment_time = ?",
            (appointment_date, appointment_time),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM appointments WHERE appointment_date = ? AND appointment_time = ? AND id != ?",
            (appointment_date, appointment_time, exclude_id),
        ).fetchone()
    if not has_app_context():
        conn.close()
    return row is not None


def parse_appointment_datetime(appointment_date, appointment_time):
    candidates = []
    if len(appointment_date) == 10 and appointment_date[4] == "-":
        candidates.append(("%Y-%m-%d", appointment_date))
    else:
        candidates.append(("%d/%m/%Y", appointment_date))

    if len(appointment_time) == 5 and ":" in appointment_time:
        candidates.append(("%H:%M", appointment_time))

    for date_format, date_value in candidates[:1]:
        for time_format, time_value in [("%H:%M", appointment_time)] if len(appointment_time) == 5 else []:
            try:
                return datetime.strptime(f"{date_value} {time_value}", f"{date_format} {time_format}")
            except ValueError:
                continue

    for date_format, date_value in [("%d/%m/%Y", appointment_date)] if len(appointment_date) != 10 else []:
        for time_format, time_value in [("%H:%M", appointment_time)] if len(appointment_time) == 5 else []:
            try:
                return datetime.strptime(f"{date_value} {time_value}", f"{date_format} {time_format}")
            except ValueError:
                continue

    return None


def generate_access_code():
    while True:
        code = f"{random.randint(100000, 999999)}"
        if get_appointment_by_code(code) is None:
            return code


def is_argentina_public_holiday(dt):
    if dt is None:
        return True

    day = dt.date()
    year = day.year

    holidays = {
        (1, 1),
        (3, 24),
        (4, 2),
        (5, 1),
        (5, 25),
        (6, 20),
        (7, 9),
        (8, 17),
        (10, 12),
        (11, 20),
        (12, 8),
        (12, 25),
    }

    if (day.month, day.day) in holidays:
        return True

    easter_sunday = calculate_easter_sunday(year)
    if day in {easter_sunday - timedelta(days=2), easter_sunday + timedelta(days=1)}:
        return True

    return False


def calculate_easter_sunday(year):
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return datetime(year, month, day).date()


def is_future_datetime(dt):
    if dt is None:
        return False
    now = datetime.now()
    return dt > now


def is_valid_business_datetime(dt):
    if dt is None:
        return False
    if dt.weekday() not in {1, 2, 3, 4, 5}:
        return False
    if is_argentina_public_holiday(dt):
        return False

    t = dt.time()
    morning_start = datetime.strptime("10:00", "%H:%M").time()
    morning_end = datetime.strptime("13:00", "%H:%M").time()
    afternoon_start = datetime.strptime("14:30", "%H:%M").time()
    afternoon_end = datetime.strptime("20:00", "%H:%M").time()

    if morning_start <= t <= morning_end:
        return True
    if afternoon_start <= t <= afternoon_end:
        return True
    return False


def build_google_event_body(appointment):
    parsed_datetime = parse_appointment_datetime(appointment["appointment_date"], appointment["appointment_time"])
    if parsed_datetime is None:
        return None

    start_dt = parsed_datetime
    end_dt = start_dt + timedelta(minutes=30)

    return {
        "summary": f"{appointment['service']} - {appointment['client_name']}",
        "description": f"Teléfono: {appointment['phone']}\nServicio: {appointment['service']}\nCódigo de turno: {appointment.get('access_code', '')}",
        "start": {"dateTime": start_dt.strftime("%Y-%m-%dT%H:%M:%S-03:00"), "timeZone": "America/Argentina/Buenos_Aires"},
        "end": {"dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S-03:00"), "timeZone": "America/Argentina/Buenos_Aires"},
    }


def sync_to_google_calendar(action, appointment):
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID")
    credentials_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not calendar_id or not credentials_path:
        return None
    if service_account is None or build is None:
        return None

    try:
        credentials = service_account.Credentials.from_service_account_file(
            credentials_path,
            scopes=["https://www.googleapis.com/auth/calendar"],
        )
        service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
    except Exception:
        return None

    event_body = build_google_event_body(appointment)
    if event_body is None:
        return None

    if action == "delete" and appointment.get("google_event_id"):
        service.events().delete(calendarId=calendar_id, eventId=appointment["google_event_id"]).execute()
        return None

    if action == "create":
        created_event = service.events().insert(calendarId=calendar_id, body=event_body).execute()
        event_id = created_event.get("id")
        conn = get_db()
        conn.execute("UPDATE appointments SET google_event_id = ? WHERE id = ?", (event_id, appointment["id"]))
        conn.commit()
        if not has_app_context():
            conn.close()
        return event_id

    if action == "update" and appointment.get("google_event_id"):
        service.events().update(
            calendarId=calendar_id,
            eventId=appointment["google_event_id"],
            body=event_body,
        ).execute()

    return None


app = create_app()


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
