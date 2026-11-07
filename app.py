import hmac
import os
import re
import textwrap
from datetime import date, datetime, time, timedelta
from functools import wraps

import psycopg2
from psycopg2 import errors as pg_errors
from psycopg2.extras import RealDictCursor
from flask import Flask, flash, g, redirect, render_template, request, session, url_for

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "f1-pit-wall-dev-key")

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "password123")

DB_SETTINGS = {
    "dbname": os.environ.get("DB_NAME", "f1_db"),
    "user": os.environ.get("DB_USER", "postgres"),
    "password": os.environ.get("DB_PASSWORD", "postgres"),
    "host": os.environ.get("DB_HOST", "127.0.0.1"),
    "port": os.environ.get("DB_PORT", "5432"),
    "connect_timeout": 5,
}

PENALTY_TYPES = [
    "Time Penalty", "Drive-Through", "Stop-Go",
    "Grid Drop", "Reprimand", "Disqualification",
]
TIRE_COMPOUNDS = ["Soft", "Medium", "Hard", "Intermediate", "Wet"]
RESULT_STATUSES = ["Finished", "DNF", "DSQ"]

# (session type, days before race day, start time UTC)
WEEKEND_LAYOUT = [
    ("FP1", -2, "11:30"), ("FP2", -2, "15:00"), ("FP3", -1, "11:30"),
    ("Qualifying", -1, "15:00"), ("Race", 0, "14:00"),
]

TIRE_COLORS = {
    "Soft": "#E10600", "Medium": "#FFD12E", "Hard": "#FFFFFF",
    "Intermediate": "#43B02A", "Wet": "#2F7DE1",
}

HEX_COLOR = re.compile(r"#[0-9A-Fa-f]{6}")
LAP_PATTERN = re.compile(r"^(\d{1,2}):([0-5]\d)\.(\d{1,3})$")

CONSTRAINT_MESSAGES = {
    "uq_team_name": "A team with that name already exists.",
    "ck_team_color": "Team colour must be a hex value like #E10600.",
    "uq_driver_number": "That driver number is already taken.",
    "uq_driver_code": "That three-letter code is already used by another driver.",
    "ck_driver_number": "Driver number must be between 1 and 99.",
    "ck_driver_code": "Driver code must be exactly three capital letters.",
    "uq_circuit_name": "A circuit with that name already exists.",
    "ck_circuit_length": "Track length must be greater than zero.",
    "ck_circuit_laps": "Number of laps must be greater than zero.",
    "ck_record_lap": "Lap record must be greater than zero.",
    "ck_record_year": "Record year must be between 1950 and 2100.",
    "uq_race_round": "That round number already exists in the calendar.",
    "uq_result_driver": "That driver already has a result in this session.",
    "uq_result_position": "Another driver already holds that position in this session.",
    "uq_pit_lap": "That driver already has a pit stop on that lap.",
    "ck_tire_compound": "Invalid tyre compound.",
    "ck_pit_time": "Pit stop time must be greater than zero.",
    "ck_result_status": "Status must be Finished, DNF or DSQ.",
    "ck_penalty_type": "Invalid penalty type.",
    "ck_penalty_seconds": "Penalty seconds must be between 0 and 300.",
    "ck_license_points": "Licence points must be between 0 and 12.",
}


@app.context_processor
def inject_colors():
    rows = fetch_all("SELECT name, team_color FROM teams", quiet=True)
    return {
        "TEAM_COLORS": {row["name"]: row["team_color"] for row in rows},
        "TIRE_COLORS": TIRE_COLORS,
    }


@app.template_filter("lap")
def format_lap(ms):
    if ms is None:
        return "—"
    minutes, rest = divmod(int(ms), 60000)
    return f"{minutes}:{rest // 1000:02d}.{rest % 1000:03d}"


@app.template_filter("gap")
def format_gap(ms):
    if not ms:
        return "—"
    return f"+{ms // 1000}.{ms % 1000:03d}"


def connect():
    return psycopg2.connect(**DB_SETTINGS)


def explain_error(exc):
    constraint = getattr(getattr(exc, "diag", None), "constraint_name", None)
    if constraint in CONSTRAINT_MESSAGES:
        return CONSTRAINT_MESSAGES[constraint]
    if isinstance(exc, pg_errors.UniqueViolation):
        return "That record already exists."
    if isinstance(exc, pg_errors.ForeignKeyViolation):
        return "The selected team or entry does not exist."
    if isinstance(exc, pg_errors.CheckViolation):
        return "A value breaks a database rule. Please check your input."
    if isinstance(exc, pg_errors.DataError):
        return "One of the values is too long or in the wrong format."
    if isinstance(exc, psycopg2.OperationalError):
        return "Cannot reach the database. Is PostgreSQL running?"
    return "Unexpected database error. Please try again."


def rollback_quietly(conn):
    if conn is not None:
        try:
            conn.rollback()
        except psycopg2.Error:
            pass


def fetch_all(sql, params=None, quiet=False):
    conn = None
    try:
        conn = connect()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    except psycopg2.Error as exc:
        rollback_quietly(conn)
        app.logger.error("Query failed: %s", exc)
        if not quiet and not g.get("db_error_shown", False):
            g.db_error_shown = True
            flash(explain_error(exc), "error")
        return []
    finally:
        if conn is not None:
            conn.close()


def fetch_one(sql, params=None):
    rows = fetch_all(sql, params)
    return rows[0] if rows else None


def execute_query(sql, params=None):
    """Returns (rows_affected, None) on success or (None, message) on failure."""
    conn = None
    try:
        conn = connect()
        with conn.cursor() as cur:
            cur.execute(sql, params)
            affected = cur.rowcount
        conn.commit()
        return affected, None
    except psycopg2.Error as exc:
        rollback_quietly(conn)
        app.logger.error("Statement failed: %s", exc)
        return None, explain_error(exc)
    finally:
        if conn is not None:
            conn.close()


def clean_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def clean_float(value):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_lap(text):
    """'1:19.234' -> (79234, True). Blank -> (None, True). Bad input -> (None, False)."""
    text = (text or "").strip()
    if not text:
        return None, True
    match = LAP_PATTERN.match(text)
    if not match:
        return None, False
    minutes, seconds, fraction = match.groups()
    return int(minutes) * 60000 + int(seconds) * 1000 + int(fraction.ljust(3, "0")), True


def same_text(a, b):
    return hmac.compare_digest((a or "").encode(), (b or "").encode())


def flash_all(messages):
    for msg in messages:
        flash(msg, "error")


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            flash("Restricted access: please log in.", "error")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------
# Gateway and login
# ---------------------------------------------------------------
@app.route("/")
def hub():
    return render_template("hub.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if (same_text(request.form.get("username"), ADMIN_USERNAME)
                and same_text(request.form.get("password"), ADMIN_PASSWORD)):
            session["logged_in"] = True
            flash("Authentication successful. Welcome to the Pit Wall.", "success")
            return redirect(url_for("manage_teams"))
        flash("Invalid credentials.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("logged_in", None)
    flash("Logged out successfully.", "success")
    return redirect(url_for("hub"))


# ---------------------------------------------------------------
# Fan portal (read only)
# ---------------------------------------------------------------
@app.route("/fan/standings")
def standings():
    drivers = fetch_all(
        "SELECT * FROM View_Driver_Standings ORDER BY standing_pos, wins DESC, driver_name"
    )
    constructors = fetch_all(
        "SELECT * FROM View_Constructor_Standings ORDER BY standing_pos, team_name"
    )
    pit_stats = fetch_all(
        """
        SELECT t.name AS team_name,
               COUNT(*)                       AS stops,
               ROUND(AVG(p.stop_time_sec), 2) AS avg_stop,
               MIN(p.stop_time_sec)           AS fastest_stop
        FROM pitstops p
        JOIN sessionresults sr ON sr.result_id = p.result_id
        JOIN drivers d         ON d.driver_id  = sr.driver_id
        JOIN teams t           ON t.team_id    = d.team_id
        GROUP BY t.team_id, t.name
        HAVING COUNT(*) >= 2
        ORDER BY avg_stop
        """
    )
    return render_template("standings.html", drivers=drivers, constructors=constructors,
                           pit_stats=pit_stats, view_type="fan")


@app.route("/fan/schedule")
def schedule():
    races = fetch_all(
        """
        SELECT c.race_id, c.round, c.name, c.circuit, c.country, c.race_date,
               CASE WHEN c.race_date < CURRENT_DATE THEN 'Completed' ELSE 'Upcoming' END AS race_status,
               w.winner_name
        FROM View_Race_Calendar c
        LEFT JOIN (
            SELECT s.race_id, d.first_name || ' ' || d.last_name AS winner_name
            FROM sessions s
            JOIN sessionresults sr ON sr.session_id = s.session_id
            JOIN drivers d         ON d.driver_id   = sr.driver_id
            WHERE s.session_type = 'Race' AND sr.finish_position = 1
        ) w ON w.race_id = c.race_id
        ORDER BY c.round
        """
    )
    weekend_sessions = fetch_all(
        "SELECT race_id, session_type, session_start FROM sessions ORDER BY session_start"
    )
    sessions_by_race = {}
    for row in weekend_sessions:
        sessions_by_race.setdefault(row["race_id"], []).append(row)
    return render_template("schedule.html", races=races, sessions_by_race=sessions_by_race,
                           view_type="fan")


@app.route("/fan/circuits")
def circuits():
    rows = fetch_all(
        """
        SELECT c.*, ROUND(c.length_km * c.race_laps, 1) AS race_distance_km,
               COALESCE(string_agg(r.name, ', ' ORDER BY r.round), '') AS hosts
        FROM circuits c
        LEFT JOIN races r ON r.circuit_id = c.circuit_id
        GROUP BY c.circuit_id
        ORDER BY c.name
        """
    )
    return render_template("circuits.html", circuits=rows, view_type="fan")


def weekend_highlights(by_type):
    race = by_type.get("Race", [])
    quali = by_type.get("Qualifying", [])
    fastest = next((r for r in race if r["is_fastest"]), None)
    return {
        "winner": next((r["code"] for r in race if r["finish_position"] == 1), None),
        "pole": next((r["code"] for r in quali if r["finish_position"] == 1), None),
        "fastest": f'{fastest["code"]} {format_lap(fastest["best_lap_ms"])}' if fastest else None,
    }


@app.route("/fan/results")
def fan_results():
    races = fetch_all("SELECT * FROM View_Race_Calendar ORDER BY round DESC")
    rows = fetch_all(
        """
        SELECT s.race_id, s.session_type, d.code,
               d.first_name || ' ' || d.last_name AS driver_name,
               t.name AS team_name,
               sr.finish_position, sr.grid_position, sr.points, sr.status, sr.best_lap_ms,
               sr.best_lap_ms - MIN(sr.best_lap_ms) OVER (PARTITION BY sr.session_id) AS gap_ms,
               (sr.best_lap_ms = MIN(sr.best_lap_ms) OVER (PARTITION BY sr.session_id)) AS is_fastest
        FROM sessionresults sr
        JOIN sessions s ON s.session_id = sr.session_id
        JOIN drivers d  ON d.driver_id  = sr.driver_id
        JOIN teams t    ON t.team_id    = d.team_id
        ORDER BY s.session_start, sr.finish_position
        """
    )
    stops = fetch_all(
        """
        SELECT s.race_id, d.code, d.first_name || ' ' || d.last_name AS driver_name,
               t.name AS team_name, p.lap_number, p.stop_time_sec, p.tire_compound,
               (p.stop_time_sec = MIN(p.stop_time_sec) OVER (PARTITION BY s.race_id)) AS is_fastest
        FROM pitstops p
        JOIN sessionresults sr ON sr.result_id = p.result_id
        JOIN sessions s        ON s.session_id = sr.session_id
        JOIN drivers d         ON d.driver_id  = sr.driver_id
        JOIN teams t           ON t.team_id    = d.team_id
        ORDER BY s.race_id, p.stop_time_sec
        """
    )

    by_race = {}
    for row in rows:
        by_race.setdefault(row["race_id"], {}).setdefault(row["session_type"], []).append(row)
    stops_by_race = {}
    for stop in stops:
        stops_by_race.setdefault(stop["race_id"], []).append(stop)
    highlights = {race_id: weekend_highlights(by_type) for race_id, by_type in by_race.items()}

    return render_template("fan_results.html", races=races, by_race=by_race,
                           stops_by_race=stops_by_race, highlights=highlights, view_type="fan")


REPORTS = [
    {
        "title": "Fastest lap of each Grand Prix",
        "concept": "Correlated subquery",
        "sql": """
            SELECT r.round, r.name AS grand_prix, d.code AS driver,
                   t.name AS team, sr.best_lap_ms
            FROM sessionresults sr
            JOIN sessions s ON s.session_id = sr.session_id
            JOIN races r    ON r.race_id    = s.race_id
            JOIN drivers d  ON d.driver_id  = sr.driver_id
            JOIN teams t    ON t.team_id    = d.team_id
            WHERE s.session_type = 'Race'
              AND sr.best_lap_ms = (SELECT MIN(x.best_lap_ms)
                                    FROM sessionresults x
                                    WHERE x.session_id = sr.session_id)
            ORDER BY r.round
        """,
    },
    {
        "title": "Season fastest lap vs circuit lap record",
        "concept": "4-table JOIN + MIN + GROUP BY",
        "sql": """
            SELECT c.name AS circuit, c.lap_record_ms AS record_ms,
                   MIN(sr.best_lap_ms) AS season_best_ms,
                   ROUND((MIN(sr.best_lap_ms) - c.lap_record_ms) / 1000.0, 3) AS gap_sec
            FROM circuits c
            JOIN races r           ON r.circuit_id = c.circuit_id
            JOIN sessions s        ON s.race_id = r.race_id AND s.session_type = 'Race'
            JOIN sessionresults sr ON sr.session_id = s.session_id
            GROUP BY c.circuit_id, c.name, c.lap_record_ms
            ORDER BY gap_sec
        """,
    },
    {
        "title": "Circuits ranked by race distance",
        "concept": "Computed column + ORDER BY",
        "sql": """
            SELECT name AS circuit, country, length_km, race_laps,
                   ROUND(length_km * race_laps, 1) AS race_distance_km
            FROM circuits
            ORDER BY race_distance_km DESC, name
        """,
    },
    {
        "title": "Teams with no drivers",
        "concept": "LEFT JOIN anti-join (IS NULL)",
        "sql": """
            SELECT t.name AS team, t.team_principal AS principal
            FROM teams t
            LEFT JOIN drivers d ON d.team_id = t.team_id
            WHERE d.driver_id IS NULL
            ORDER BY t.name
        """,
    },
    {
        "title": "Pole positions per driver",
        "concept": "JOIN + GROUP BY + COUNT",
        "sql": """
            SELECT d.code AS driver, d.last_name AS surname, COUNT(*) AS poles
            FROM sessionresults sr
            JOIN sessions s ON s.session_id = sr.session_id
            JOIN drivers d  ON d.driver_id  = sr.driver_id
            WHERE s.session_type = 'Qualifying' AND sr.finish_position = 1
            GROUP BY d.driver_id, d.code, d.last_name
            ORDER BY poles DESC, driver
        """,
    },
    {
        "title": "Drivers scoring above the average",
        "concept": "Subquery on a view",
        "sql": """
            SELECT driver_name, team_name, total_points
            FROM View_Driver_Standings
            WHERE total_points > (SELECT AVG(total_points) FROM View_Driver_Standings)
            ORDER BY total_points DESC
        """,
    },
    {
        "title": "Drivers who won a race or took a pole",
        "concept": "UNION",
        "sql": """
            SELECT d.code AS driver
            FROM sessionresults sr
            JOIN sessions s ON s.session_id = sr.session_id
            JOIN drivers d  ON d.driver_id  = sr.driver_id
            WHERE s.session_type = 'Qualifying' AND sr.finish_position = 1
            UNION
            SELECT d.code
            FROM sessionresults sr
            JOIN sessions s ON s.session_id = sr.session_id
            JOIN drivers d  ON d.driver_id  = sr.driver_id
            WHERE s.session_type = 'Race' AND sr.finish_position = 1
            ORDER BY driver
        """,
    },
    {
        "title": "Podium finishers who were also penalised",
        "concept": "INTERSECT",
        "sql": """
            SELECT d.code AS driver
            FROM sessionresults sr
            JOIN sessions s ON s.session_id = sr.session_id
            JOIN drivers d  ON d.driver_id  = sr.driver_id
            WHERE s.session_type = 'Race' AND sr.finish_position <= 3
            INTERSECT
            SELECT d.code
            FROM penalties p
            JOIN sessionresults sr ON sr.result_id = p.result_id
            JOIN drivers d         ON d.driver_id  = sr.driver_id
            ORDER BY driver
        """,
    },
    {
        "title": "Podium finishers with a clean record",
        "concept": "EXCEPT",
        "sql": """
            SELECT d.code AS driver
            FROM sessionresults sr
            JOIN sessions s ON s.session_id = sr.session_id
            JOIN drivers d  ON d.driver_id  = sr.driver_id
            WHERE s.session_type = 'Race' AND sr.finish_position <= 3
            EXCEPT
            SELECT d.code
            FROM penalties p
            JOIN sessionresults sr ON sr.result_id = p.result_id
            JOIN drivers d         ON d.driver_id  = sr.driver_id
            ORDER BY driver
        """,
    },
    {
        "title": "Race entries with no lap time",
        "concept": "NULL / three-valued logic",
        "sql": """
            SELECT r.name AS grand_prix, d.code AS driver, sr.status
            FROM sessionresults sr
            JOIN sessions s ON s.session_id = sr.session_id
            JOIN races r    ON r.race_id    = s.race_id
            JOIN drivers d  ON d.driver_id  = sr.driver_id
            WHERE s.session_type = 'Race' AND sr.best_lap_ms IS NULL
            ORDER BY r.round
        """,
    },
    {
        "title": "Pit stop times by tyre compound",
        "concept": "AVG, MIN, COUNT + GROUP BY",
        "sql": """
            SELECT tire_compound AS tyre, COUNT(*) AS stops,
                   ROUND(AVG(stop_time_sec), 2) AS avg_stop_sec,
                   MIN(stop_time_sec) AS fastest_sec
            FROM pitstops
            GROUP BY tire_compound
            ORDER BY avg_stop_sec
        """,
    },
]


@app.route("/fan/reports")
def reports():
    data = []
    for item in REPORTS:
        sql = textwrap.dedent(item["sql"]).strip()
        rows = fetch_all(sql)
        data.append({
            "title": item["title"],
            "concept": item["concept"],
            "sql": sql,
            "rows": rows,
            "columns": list(rows[0].keys()) if rows else [],
        })
    return render_template("reports.html", reports=data, view_type="fan")


# ---------------------------------------------------------------
# Admin: teams
# ---------------------------------------------------------------
@app.route("/admin/teams", methods=["GET", "POST"])
@login_required
def manage_teams():
    form = {}
    if request.method == "POST":
        form = request.form.to_dict()
        errors = []
        name = form.get("name", "").strip()
        country = form.get("base_country", "").strip()
        principal = form.get("team_principal", "").strip()
        color = form.get("team_color", "").strip()
        for label, value, limit in (("Team name", name, 60), ("Base country", country, 60),
                                    ("Team principal", principal, 80)):
            if not value or len(value) > limit:
                errors.append(f"{label} is required (max {limit} characters).")
        if not HEX_COLOR.fullmatch(color):
            errors.append("Pick a valid team colour.")

        if errors:
            flash_all(errors)
        else:
            _, err = execute_query(
                "INSERT INTO teams (name, base_country, team_principal, team_color) "
                "VALUES (%s, %s, %s, %s)",
                (name, country, principal, color),
            )
            if err:
                flash(err, "error")
            else:
                flash(f"Team {name} added. Register its drivers on the Drivers page.", "success")
                return redirect(url_for("manage_teams"))

    teams = fetch_all(
        """
        SELECT t.team_id, t.name, t.base_country, t.team_principal, t.team_color,
               COUNT(d.driver_id) AS driver_count
        FROM teams t
        LEFT JOIN drivers d ON d.team_id = t.team_id
        GROUP BY t.team_id
        ORDER BY t.name
        """
    )
    return render_template("teams.html", form=form, teams=teams, view_type="admin")


@app.route("/admin/teams/<int:team_id>/delete", methods=["POST"])
@login_required
def delete_team(team_id):
    # Cascades to drivers, then to their results, pit stops and penalties
    affected, err = execute_query("DELETE FROM teams WHERE team_id = %s", (team_id,))
    if err:
        flash(err, "error")
    elif affected == 0:
        flash("Team not found.", "error")
    else:
        flash("Team removed along with its drivers and their results.", "success")
    return redirect(url_for("manage_teams"))


# ---------------------------------------------------------------
# Admin: circuits
# ---------------------------------------------------------------
def read_circuit_form(form):
    """Returns (values_tuple, errors). Values are only valid when errors is empty."""
    errors = []
    name = form.get("name", "").strip()
    city = form.get("city", "").strip()
    country = form.get("country", "").strip()
    for label, value, limit in (("Circuit name", name, 80), ("City", city, 60),
                                ("Country", country, 60)):
        if not value or len(value) > limit:
            errors.append(f"{label} is required (max {limit} characters).")

    length = clean_float(form.get("length_km"))
    if length is None or not 0 < length <= 25:
        errors.append("Track length must be between 0 and 25 km.")
    laps = clean_int(form.get("race_laps"))
    if laps is None or not 1 <= laps <= 100:
        errors.append("Race laps must be 1-100.")

    record_ms, lap_ok = parse_lap(form.get("lap_record"))
    if not lap_ok:
        errors.append("Lap record must look like 1:19.813 (leave blank if unknown).")
    holder = form.get("record_holder", "").strip()
    if len(holder) > 60:
        errors.append("Record holder is too long (max 60 characters).")
    year_text = form.get("record_year", "").strip()
    year = clean_int(year_text) if year_text else None
    if year_text and (year is None or not 1950 <= year <= 2100):
        errors.append("Record year must be between 1950 and 2100.")

    values = (name, city, country,
              round(length, 3) if length is not None else None,
              laps, record_ms, holder or None, year)
    return values, errors


@app.route("/admin/circuits", methods=["GET", "POST"])
@login_required
def manage_circuits():
    form = {}
    if request.method == "POST":
        form = request.form.to_dict()
        values, errors = read_circuit_form(form)
        if errors:
            flash_all(errors)
        else:
            _, err = execute_query(
                "INSERT INTO circuits (name, city, country, length_km, race_laps, "
                "lap_record_ms, record_holder, record_year) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                values,
            )
            if err:
                flash(err, "error")
            else:
                flash(f"Circuit {values[0]} added.", "success")
                return redirect(url_for("manage_circuits"))

    rows = fetch_all(
        """
        SELECT c.circuit_id, c.name, c.city, c.country, c.length_km, c.race_laps,
               c.lap_record_ms, c.record_holder, c.record_year,
               COUNT(r.race_id) AS race_count
        FROM circuits c
        LEFT JOIN races r ON r.circuit_id = c.circuit_id
        GROUP BY c.circuit_id
        ORDER BY c.name
        """
    )
    return render_template("admin_circuits.html", form=form, circuits=rows, view_type="admin")


@app.route("/admin/circuits/<int:circuit_id>/edit", methods=["GET", "POST"])
@login_required
def edit_circuit(circuit_id):
    entry = fetch_one("SELECT * FROM circuits WHERE circuit_id = %s", (circuit_id,))
    if entry is None:
        flash("Circuit not found.", "error")
        return redirect(url_for("manage_circuits"))

    form = {
        "name": entry["name"],
        "city": entry["city"],
        "country": entry["country"],
        "length_km": str(entry["length_km"]),
        "race_laps": entry["race_laps"],
        "lap_record": format_lap(entry["lap_record_ms"]) if entry["lap_record_ms"] else "",
        "record_holder": entry["record_holder"] or "",
        "record_year": entry["record_year"] or "",
    }

    if request.method == "POST":
        form = request.form.to_dict()
        values, errors = read_circuit_form(form)
        if errors:
            flash_all(errors)
        else:
            _, err = execute_query(
                "UPDATE circuits SET name = %s, city = %s, country = %s, length_km = %s, "
                "race_laps = %s, lap_record_ms = %s, record_holder = %s, record_year = %s "
                "WHERE circuit_id = %s",
                values + (circuit_id,),
            )
            if err:
                flash(err, "error")
            else:
                flash("Circuit updated.", "success")
                return redirect(url_for("manage_circuits"))

    return render_template("edit_circuit.html", entry=entry, form=form, view_type="admin")


# ---------------------------------------------------------------
# Admin: FIA penalties
# ---------------------------------------------------------------
def show_fia(prefill=None):
    results = fetch_all(
        """
        SELECT sr.result_id, r.name AS race_name, s.session_type, d.code,
               d.first_name || ' ' || d.last_name AS driver_name
        FROM sessionresults sr
        JOIN sessions s ON s.session_id = sr.session_id
        JOIN races r    ON r.race_id    = s.race_id
        JOIN drivers d  ON d.driver_id  = sr.driver_id
        WHERE s.session_type IN ('Race', 'Qualifying')
        ORDER BY r.round DESC, s.session_start DESC, sr.finish_position
        """
    )
    history = fetch_all(
        """
        SELECT p.penalty_id, p.penalty_type, p.penalty_seconds, p.license_points,
               p.reason, p.issued_at, d.code,
               d.first_name || ' ' || d.last_name AS driver_name,
               t.name AS team_name, r.name AS race_name, s.session_type
        FROM penalties p
        JOIN sessionresults sr ON sr.result_id = p.result_id
        JOIN sessions s        ON s.session_id = sr.session_id
        JOIN races r           ON r.race_id    = s.race_id
        JOIN drivers d         ON d.driver_id  = sr.driver_id
        JOIN teams t           ON t.team_id    = d.team_id
        ORDER BY p.issued_at DESC, p.penalty_id DESC
        """
    )
    watch = fetch_all(
        """
        SELECT d.code,
               d.first_name || ' ' || d.last_name AS driver_name,
               t.name AS team_name,
               COUNT(*)               AS penalty_count,
               SUM(p.penalty_seconds) AS total_seconds,
               SUM(p.license_points)  AS total_points
        FROM penalties p
        JOIN sessionresults sr ON sr.result_id = p.result_id
        JOIN drivers d         ON d.driver_id  = sr.driver_id
        JOIN teams t           ON t.team_id    = d.team_id
        GROUP BY d.driver_id, d.code, d.first_name, d.last_name, t.name
        HAVING SUM(p.license_points) > 0
        ORDER BY total_points DESC, driver_name
        """
    )
    return render_template("fia.html", results=results, history=history, watch=watch,
                           penalty_types=PENALTY_TYPES, form=prefill or {}, view_type="admin")


@app.route("/admin/fia", methods=["GET", "POST"])
@login_required
def fia():
    if request.method == "GET":
        return show_fia()

    submitted = request.form.to_dict()
    errors = []

    result_id = clean_int(submitted.get("result_id"))
    if result_id is None:
        errors.append("Select the driver and session.")
    penalty_type = submitted.get("penalty_type", "").strip()
    if penalty_type not in PENALTY_TYPES:
        errors.append("Select a valid penalty type.")
    seconds = clean_int(submitted.get("penalty_seconds") or "0")
    if seconds is None or not 0 <= seconds <= 300:
        errors.append("Seconds must be 0-300.")
    points = clean_int(submitted.get("license_points") or "0")
    if points is None or not 0 <= points <= 12:
        errors.append("Licence points must be 0-12.")
    reason = submitted.get("reason", "").strip()
    if not reason or len(reason) > 300:
        errors.append("Reason is required (max 300 characters).")

    if errors:
        flash_all(errors)
        return show_fia(submitted)

    _, err = execute_query(
        "INSERT INTO penalties (result_id, penalty_type, penalty_seconds, license_points, reason) "
        "VALUES (%s, %s, %s, %s, %s)",
        (result_id, penalty_type, seconds, points, reason),
    )
    if err:
        flash(err, "error")
        return show_fia(submitted)

    flash("Penalty logged.", "success")
    return redirect(url_for("fia"))


# ---------------------------------------------------------------
# Admin: drivers
# ---------------------------------------------------------------
def show_add_driver(prefill=None):
    teams = fetch_all("SELECT team_id, name FROM teams ORDER BY name")
    drivers = fetch_all(
        """
        SELECT d.driver_id, d.driver_number, d.code, d.first_name, d.last_name,
               d.nationality, d.date_of_birth, t.name AS team_name
        FROM drivers d
        JOIN teams t ON t.team_id = d.team_id
        ORDER BY t.name, d.driver_number
        """
    )
    return render_template("add_driver.html", teams=teams, drivers=drivers,
                           form=prefill or {}, view_type="admin")


@app.route("/admin/drivers/add", methods=["GET", "POST"])
@login_required
def add_driver():
    if request.method == "GET":
        return show_add_driver()

    submitted = request.form.to_dict()
    errors = []

    team_id = clean_int(submitted.get("team_id"))
    if team_id is None:
        errors.append("Select a team.")
    number = clean_int(submitted.get("driver_number"))
    if number is None or not 1 <= number <= 99:
        errors.append("Number must be 1-99.")
    code = submitted.get("code", "").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", code):
        errors.append("Code must be exactly 3 letters.")

    first_name = submitted.get("first_name", "").strip()
    last_name = submitted.get("last_name", "").strip()
    nationality = submitted.get("nationality", "").strip()
    for label, value in (("First name", first_name), ("Last name", last_name),
                         ("Nationality", nationality)):
        if not value or len(value) > 50:
            errors.append(f"{label} is required (max 50 characters).")

    birth_date = None
    try:
        birth_date = date.fromisoformat(submitted.get("date_of_birth", "").strip())
        if birth_date > date.today() or birth_date.year < 1940:
            errors.append("Date of birth is not realistic.")
    except ValueError:
        errors.append("Enter a valid date of birth.")

    if errors:
        flash_all(errors)
        return show_add_driver(submitted)

    _, err = execute_query(
        "INSERT INTO drivers (team_id, driver_number, code, first_name, last_name, "
        "nationality, date_of_birth) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (team_id, number, code, first_name, last_name, nationality, birth_date),
    )
    if err:
        flash(err, "error")
        return show_add_driver(submitted)

    flash(f"Driver {code} registered.", "success")
    return redirect(url_for("add_driver"))


@app.route("/admin/drivers/<int:driver_id>/delete", methods=["POST"])
@login_required
def delete_driver(driver_id):
    # Cascades to the driver's results, pit stops and penalties
    affected, err = execute_query("DELETE FROM drivers WHERE driver_id = %s", (driver_id,))
    if err:
        flash(err, "error")
    elif affected == 0:
        flash("Driver not found.", "error")
    else:
        flash("Driver removed.", "success")
    return redirect(url_for("add_driver"))


# ---------------------------------------------------------------
# Admin: race calendar
# ---------------------------------------------------------------
def create_weekend(round_no, name, circuit_id, race_date):
    """Insert a race and its five sessions as one transaction. Returns an error message or None."""
    conn = None
    try:
        conn = connect()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO races (round, name, circuit_id, race_date) "
                "VALUES (%s, %s, %s, %s) RETURNING race_id",
                (round_no, name, circuit_id, race_date),
            )
            race_id = cur.fetchone()[0]
            for session_type, day_offset, clock in WEEKEND_LAYOUT:
                start = datetime.combine(race_date + timedelta(days=day_offset),
                                         time.fromisoformat(clock))
                cur.execute(
                    "INSERT INTO sessions (race_id, session_type, session_start) "
                    "VALUES (%s, %s, %s)",
                    (race_id, session_type, start),
                )
        conn.commit()
        return None
    except psycopg2.Error as exc:
        rollback_quietly(conn)
        app.logger.error("Weekend creation failed: %s", exc)
        return explain_error(exc)
    finally:
        if conn is not None:
            conn.close()


@app.route("/admin/races/add", methods=["GET", "POST"])
@login_required
def add_race():
    form = {}
    if request.method == "POST":
        form = request.form.to_dict()
        errors = []

        round_no = clean_int(form.get("round"))
        if round_no is None or not 1 <= round_no <= 30:
            errors.append("Round must be a number from 1 to 30.")
        name = form.get("name", "").strip()
        if not name or len(name) > 80:
            errors.append("Grand Prix name is required (max 80 characters).")
        circuit_id = clean_int(form.get("circuit_id"))
        if circuit_id is None:
            errors.append("Select a circuit.")
        try:
            race_date = date.fromisoformat(form.get("race_date", "").strip())
        except ValueError:
            race_date = None
            errors.append("Enter a valid race date.")

        if errors:
            flash_all(errors)
        else:
            err = create_weekend(round_no, name, circuit_id, race_date)
            if err:
                flash(err, "error")
            else:
                flash(f"{name} created with FP1, FP2, FP3, Qualifying and Race.", "success")
                return redirect(url_for("add_race"))

    circuit_list = fetch_all("SELECT circuit_id, name, country FROM circuits ORDER BY name")
    races = fetch_all(
        "SELECT round, name, circuit, country, race_date FROM View_Race_Calendar ORDER BY round DESC"
    )
    return render_template("add_race.html", form=form, circuits=circuit_list, races=races,
                           view_type="admin")


# ---------------------------------------------------------------
# Admin: session results (FP, Qualifying, Race, fastest laps)
# ---------------------------------------------------------------
def check_result_fields(grid, finish, status, lap_ok):
    errors = []
    if grid is None or not 0 <= grid <= 22:
        errors.append("Grid must be 0-22 (use 0 for practice and qualifying).")
    if finish is None or not 1 <= finish <= 22:
        errors.append("Finish position must be 1-22.")
    if status not in RESULT_STATUSES:
        errors.append("Select a valid status.")
    if not lap_ok:
        errors.append("Lap time must look like 1:19.234 (leave blank if none).")
    return errors


@app.route("/admin/results/add", methods=["GET", "POST"])
@login_required
def add_result():
    form = {}
    if request.method == "POST":
        form = request.form.to_dict()
        session_id = clean_int(form.get("session_id"))
        driver_id = clean_int(form.get("driver_id"))
        grid = clean_int(form.get("grid_position") or "0")
        finish = clean_int(form.get("finish_position"))
        status = form.get("status", "Finished")
        lap_ms, lap_ok = parse_lap(form.get("best_lap"))

        errors = check_result_fields(grid, finish, status, lap_ok)
        if session_id is None:
            errors.append("Select a session.")
        if driver_id is None:
            errors.append("Select a driver.")

        if errors:
            flash_all(errors)
        else:
            affected, err = execute_query(
                """
                INSERT INTO sessionresults
                    (session_id, driver_id, grid_position, finish_position, points, best_lap_ms, status)
                SELECT s.session_id, %s, %s, %s,
                       CASE WHEN %s = 'Finished' THEN f1_points(s.session_type, %s) ELSE 0 END,
                       %s, %s
                FROM sessions s
                WHERE s.session_id = %s
                """,
                (driver_id, grid, finish, status, finish, lap_ms, status, session_id),
            )
            if err:
                flash(err, "error")
            elif affected == 0:
                flash("Session not found.", "error")
            else:
                flash("Result logged. Points were calculated automatically.", "success")
                return redirect(url_for("add_result", session_id=session_id))

    filter_id = clean_int(request.args.get("session_id"))
    sessions_list = fetch_all(
        """
        SELECT s.session_id, r.round, r.name AS race_name, s.session_type
        FROM sessions s
        JOIN races r ON r.race_id = s.race_id
        ORDER BY r.round DESC, s.session_start
        """
    )
    drivers_list = fetch_all(
        "SELECT driver_id, first_name || ' ' || last_name AS driver_name, code FROM drivers ORDER BY code"
    )
    results = fetch_all(
        """
        SELECT sr.result_id, r.round, r.name AS race_name, s.session_type, d.code,
               sr.grid_position, sr.finish_position, sr.points, sr.best_lap_ms,
               sr.status, t.name AS team_name
        FROM sessionresults sr
        JOIN sessions s ON s.session_id = sr.session_id
        JOIN races r    ON r.race_id    = s.race_id
        JOIN drivers d  ON d.driver_id  = sr.driver_id
        JOIN teams t    ON t.team_id    = d.team_id
        WHERE (%s IS NULL OR sr.session_id = %s)
        ORDER BY r.round DESC, s.session_start DESC, sr.finish_position
        LIMIT 60
        """,
        (filter_id, filter_id),
    )
    selected = form.get("session_id") or (str(filter_id) if filter_id else "")
    return render_template("add_result.html", form=form, sessions=sessions_list,
                           drivers=drivers_list, results=results, selected=selected,
                           filter_id=filter_id, view_type="admin")


@app.route("/admin/results/<int:result_id>/edit", methods=["GET", "POST"])
@login_required
def edit_result(result_id):
    entry = fetch_one(
        """
        SELECT sr.result_id, sr.session_id, sr.grid_position, sr.finish_position,
               sr.best_lap_ms, sr.status, r.name AS race_name, s.session_type, d.code,
               d.first_name || ' ' || d.last_name AS driver_name
        FROM sessionresults sr
        JOIN sessions s ON s.session_id = sr.session_id
        JOIN races r    ON r.race_id    = s.race_id
        JOIN drivers d  ON d.driver_id  = sr.driver_id
        WHERE sr.result_id = %s
        """,
        (result_id,),
    )
    if entry is None:
        flash("Result not found.", "error")
        return redirect(url_for("add_result"))

    form = {
        "grid_position": entry["grid_position"],
        "finish_position": entry["finish_position"],
        "best_lap": format_lap(entry["best_lap_ms"]) if entry["best_lap_ms"] else "",
        "status": entry["status"],
    }

    if request.method == "POST":
        form = request.form.to_dict()
        grid = clean_int(form.get("grid_position") or "0")
        finish = clean_int(form.get("finish_position"))
        status = form.get("status", "Finished")
        lap_ms, lap_ok = parse_lap(form.get("best_lap"))

        errors = check_result_fields(grid, finish, status, lap_ok)
        if errors:
            flash_all(errors)
        else:
            _, err = execute_query(
                """
                UPDATE sessionresults
                SET grid_position = %s, finish_position = %s, best_lap_ms = %s, status = %s,
                    points = CASE WHEN %s = 'Finished' THEN f1_points(s.session_type, %s) ELSE 0 END
                FROM sessions s
                WHERE s.session_id = sessionresults.session_id
                  AND sessionresults.result_id = %s
                """,
                (grid, finish, lap_ms, status, status, finish, result_id),
            )
            if err:
                flash(err, "error")
            else:
                flash("Result updated. Points recalculated.", "success")
                return redirect(url_for("add_result", session_id=entry["session_id"]))

    return render_template("edit_result.html", entry=entry, form=form,
                           statuses=RESULT_STATUSES, view_type="admin")


@app.route("/admin/results/<int:result_id>/delete", methods=["POST"])
@login_required
def delete_result(result_id):
    affected, err = execute_query("DELETE FROM sessionresults WHERE result_id = %s", (result_id,))
    if err:
        flash(err, "error")
    elif affected == 0:
        flash("Result not found.", "error")
    else:
        flash("Result deleted (its pit stops and penalties were removed too).", "success")
    return redirect(url_for("add_result"))


# ---------------------------------------------------------------
# Admin: pit stops
# ---------------------------------------------------------------
def check_pit_fields(lap, seconds, tyre):
    errors = []
    if lap is not None and not 1 <= lap <= 100:
        errors.append("Lap must be 1-100.")
    if seconds is None or not 0 < seconds <= 120:
        errors.append("Stop time must be between 0 and 120 seconds.")
    if tyre not in TIRE_COMPOUNDS:
        errors.append("Select a valid tyre compound.")
    return errors


@app.route("/admin/pitstops", methods=["GET", "POST"])
@login_required
def pitstops():
    form = {}
    if request.method == "POST":
        form = request.form.to_dict()
        result_id = clean_int(form.get("result_id"))
        lap = clean_int(form.get("lap_number"))
        seconds = clean_float(form.get("stop_time_sec"))
        tyre = form.get("tire_compound", "")

        errors = check_pit_fields(lap, seconds, tyre)
        if result_id is None:
            errors.append("Select a driver.")
        if lap is None:
            errors.append("Enter the lap number.")

        if errors:
            flash_all(errors)
        else:
            _, err = execute_query(
                "INSERT INTO pitstops (result_id, lap_number, stop_time_sec, tire_compound) "
                "VALUES (%s, %s, %s, %s)",
                (result_id, lap, round(seconds, 2), tyre),
            )
            if err:
                flash(err, "error")
            else:
                flash("Pit stop logged.", "success")
                return redirect(url_for("pitstops"))

    entries = fetch_all(
        """
        SELECT sr.result_id, r.round, r.name AS race_name, d.code,
               d.first_name || ' ' || d.last_name AS driver_name
        FROM sessionresults sr
        JOIN sessions s ON s.session_id = sr.session_id
        JOIN races r    ON r.race_id    = s.race_id
        JOIN drivers d  ON d.driver_id  = sr.driver_id
        WHERE s.session_type = 'Race'
        ORDER BY r.round DESC, sr.finish_position
        """
    )
    stops = fetch_all(
        """
        SELECT p.pit_id, r.round, r.name AS race_name, d.code, p.lap_number,
               p.stop_time_sec, p.tire_compound
        FROM pitstops p
        JOIN sessionresults sr ON sr.result_id = p.result_id
        JOIN sessions s        ON s.session_id = sr.session_id
        JOIN races r           ON r.race_id    = s.race_id
        JOIN drivers d         ON d.driver_id  = sr.driver_id
        ORDER BY r.round DESC, p.stop_time_sec
        """
    )
    return render_template("pitstops.html", form=form, entries=entries, stops=stops,
                           tyres=TIRE_COMPOUNDS, view_type="admin")


@app.route("/admin/pitstops/<int:pit_id>/update", methods=["POST"])
@login_required
def update_pitstop(pit_id):
    seconds = clean_float(request.form.get("stop_time_sec"))
    tyre = request.form.get("tire_compound", "")
    errors = check_pit_fields(None, seconds, tyre)
    if errors:
        flash_all(errors)
        return redirect(url_for("pitstops"))

    affected, err = execute_query(
        "UPDATE pitstops SET stop_time_sec = %s, tire_compound = %s WHERE pit_id = %s",
        (round(seconds, 2), tyre, pit_id),
    )
    if err:
        flash(err, "error")
    elif affected == 0:
        flash("Pit stop not found.", "error")
    else:
        flash("Pit stop updated.", "success")
    return redirect(url_for("pitstops"))


@app.route("/admin/pitstops/<int:pit_id>/delete", methods=["POST"])
@login_required
def delete_pitstop(pit_id):
    affected, err = execute_query("DELETE FROM pitstops WHERE pit_id = %s", (pit_id,))
    if err:
        flash(err, "error")
    elif affected == 0:
        flash("Pit stop not found.", "error")
    else:
        flash("Pit stop deleted.", "success")
    return redirect(url_for("pitstops"))


@app.errorhandler(500)
def server_error(_):
    return "Internal server error. Check the Flask terminal for details.", 500


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)