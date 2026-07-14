import os
import time
import logging

import psycopg2
from flask import Flask, request, render_template, redirect, url_for, flash
from prometheus_flask_exporter import PrometheusMetrics

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")

# Exposes /metrics automatically for Prometheus scraping
metrics = PrometheusMetrics(app)
metrics.info("app_info", "Registration application", version="1.0.0")

DB_HOST = os.environ.get("DB_HOST", "postgres")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "appdb")
DB_USER = os.environ.get("DB_USER", "appuser")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


def get_connection(retries=5, delay=3):
    """Connect to PostgreSQL with basic retry logic (useful during pod startup)."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            return psycopg2.connect(
                host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
                user=DB_USER, password=DB_PASSWORD, connect_timeout=5,
            )
        except psycopg2.OperationalError as e:
            last_err = e
            log.warning("DB connection attempt %s/%s failed: %s", attempt, retries, e)
            time.sleep(delay)
    raise last_err


def init_db():
    conn = get_connection()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS registrations (
                    id SERIAL PRIMARY KEY,
                    full_name VARCHAR(120) NOT NULL,
                    email VARCHAR(160) UNIQUE NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                );
            """)
    finally:
        conn.close()


@app.route("/", methods=["GET"])
def index():
    return redirect(url_for("register"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip()

        if not full_name or not email:
            flash("Both name and email are required.")
            return redirect(url_for("register"))

        try:
            conn = get_connection()
            with conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO registrations (full_name, email) VALUES (%s, %s)",
                    (full_name, email),
                )
            conn.close()
            flash("Registration successful!")
        except psycopg2.IntegrityError:
            flash("That email is already registered.")
        except Exception as e:
            log.exception("Registration failed")
            flash(f"Registration failed: {e}")

        return redirect(url_for("register"))

    return render_template("register.html")


@app.route("/healthz", methods=["GET"])
def healthz():
    """Liveness probe - process is up."""
    return {"status": "ok"}, 200


@app.route("/readyz", methods=["GET"])
def readyz():
    """Readiness probe - can reach the database."""
    try:
        conn = get_connection(retries=1)
        conn.close()
        return {"status": "ready"}, 200
    except Exception as e:
        return {"status": "not-ready", "error": str(e)}, 503


# Run once at import time so this also works under gunicorn/production WSGI
# servers, not just `python app.py`. Retries handle the app container
# starting slightly before the database is ready to accept connections.
try:
    init_db()
    log.info("Database initialized successfully")
except Exception:
    log.exception("Database initialization failed at startup - will retry on first request")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
