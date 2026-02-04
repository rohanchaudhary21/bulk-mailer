from flask import Flask, render_template, request, redirect, session
import os, time, base64, datetime, json, sqlite3
import pandas as pd
from dotenv import load_dotenv
from email.mime.text import MIMEText
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
import pytz

CLIENT_CONFIG = {
    "web": {
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}

# ================= LOAD ENV =================
load_dotenv()

# ================= APP SETUP =================
app = Flask(__name__)

app.secret_key = os.environ.get("FLASK_SECRET_KEY") or "dev-secret-key-change-me"


# 🔥 REQUIRED FOR RENDER (HTTPS)
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

# ================= SCOPES =================
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]

# ================= DB =================
import psycopg2
from psycopg2.extras import RealDictCursor
from urllib.parse import urlparse

DATABASE_URL = os.environ.get("DATABASE_URL")

def get_db():
    """Get database connection - works with both PostgreSQL and SQLite fallback"""
    if DATABASE_URL:
        # PostgreSQL (production)
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        return conn
    else:
        # SQLite fallback (local development)
        import sqlite3
        conn = sqlite3.connect("stats.db")
        conn.row_factory = sqlite3.Row
        return conn

def init_db():
    """Initialize database tables"""
    db = get_db()
    cursor = db.cursor()
    
    if DATABASE_URL:
        # PostgreSQL syntax
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS email_logs (
                id SERIAL PRIMARY KEY,
                email TEXT,
                status TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS oauth_tokens (
                user_email TEXT PRIMARY KEY,
                token_json TEXT
            )
        """)
    else:
        # SQLite syntax
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS email_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT,
                status TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS oauth_tokens (
                user_email TEXT PRIMARY KEY,
                token_json TEXT
            )
        """)
    
    db.commit()
    cursor.close()
    db.close()

def db_execute(query, params=None):
    """Execute query with proper parameter style for PostgreSQL or SQLite"""
    if DATABASE_URL:
        # PostgreSQL uses %s placeholders
        query = query.replace('?', '%s')
    return query, params

init_db()

# ================= SCHEDULER =================
IST = pytz.timezone('Asia/Kolkata')
scheduler = BackgroundScheduler(timezone=IST)

# Add listener to log when jobs execute
def job_executed(event):
    print(f"✅ Job executed: {event.job_id} at {datetime.datetime.now(IST)}")

def job_error(event):
    print(f"❌ Job failed: {event.job_id}, error: {event.exception}")

scheduler.add_listener(job_executed, EVENT_JOB_EXECUTED)
scheduler.add_listener(job_error, EVENT_JOB_ERROR)
scheduler.start()
print(f"🚀 Scheduler started with IST timezone")

# ================= HELPERS =================

def read_sheet(sheet_url):
    sheet_id = sheet_url.split("/d/")[1].split("/")[0]
    csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    return pd.read_csv(csv_url)


def get_gmail_service(user_email):
    db = get_db()
    cursor = db.cursor()
    
    query, params = db_execute(
        "SELECT token_json FROM oauth_tokens WHERE user_email=?",
        (user_email,)
    )
    cursor.execute(query, params)
    row = cursor.fetchone()
    cursor.close()
    db.close()

    if not row:
        raise Exception("User not authenticated")

    token_json = row['token_json'] if DATABASE_URL else row[0]
    creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
    return build("gmail", "v1", credentials=creds)


def send_bulk(user_email, recipients, subject, body, delay):
    print(f"Starting send_bulk for {user_email}, {len(recipients)} recipients")
    db = get_db()
    cursor = db.cursor()
    
    try:
        service = get_gmail_service(user_email)
    except Exception as e:
        print(f"ERROR: Failed to get Gmail service: {e}")
        cursor.close()
        db.close()
        return

    for email in recipients:
        try:
            msg = MIMEText(body)
            msg["to"] = email
            msg["subject"] = subject

            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            service.users().messages().send(userId="me", body={"raw": raw}).execute()

            query, params = db_execute(
                "INSERT INTO email_logs (email, status) VALUES (?, ?)",
                (email, "sent")
            )
            cursor.execute(query, params)
            print(f"✅ Sent to {email}")
        except Exception as e:
            query, params = db_execute(
                "INSERT INTO email_logs (email, status) VALUES (?, ?)",
                (email, "failed")
            )
            cursor.execute(query, params)
            print(f"❌ Failed to send to {email}: {e}")

        db.commit()
        time.sleep(delay)
    
    cursor.close()
    db.close()

# ================= ROUTES =================
import requests

@app.route("/")
def home():
    if session.get("user_email"):
        return redirect("/dashboard")
    return render_template("login.html")

@app.route("/authorize")
def authorize():
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": os.environ["GOOGLE_CLIENT_ID"],
                "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=SCOPES,
        redirect_uri=os.environ["REDIRECT_URI"],
    )

    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
    )
    return redirect(auth_url)


@app.route("/callback")
def callback():
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": os.environ["GOOGLE_CLIENT_ID"],
                "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=SCOPES,
        redirect_uri=os.environ["REDIRECT_URI"],
    )

    flow.fetch_token(code=request.args.get("code"))
    creds = flow.credentials

    # ✅ Get user email safely
    userinfo = requests.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {creds.token}"}
    ).json()

    user_email = userinfo.get("email")
    if not user_email:
        return "Failed to fetch user email", 400

    # ✅ Store token in DB
    db = get_db()
    cursor = db.cursor()
    
    if DATABASE_URL:
        # PostgreSQL: Use INSERT ... ON CONFLICT
        cursor.execute(
            "INSERT INTO oauth_tokens (user_email, token_json) VALUES (%s, %s) ON CONFLICT (user_email) DO UPDATE SET token_json = EXCLUDED.token_json",
            (user_email, creds.to_json())
        )
    else:
        # SQLite: Use REPLACE
        cursor.execute(
            "REPLACE INTO oauth_tokens (user_email, token_json) VALUES (?, ?)",
            (user_email, creds.to_json())
        )
    
    db.commit()
    cursor.close()
    db.close()

    # ✅ Set session
    session.clear()
    session["logged_in"] = True
    session["user_email"] = user_email

    return redirect("/dashboard")


@app.route("/dashboard")
def dashboard():
    if not session.get("logged_in"):
        return redirect("/")
    return render_template("dashboard.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.route("/send", methods=["POST"])
def send():
    if not session.get("logged_in"):
        return redirect("/")

    send_type = request.form.get("send_type")
    subject = request.form.get("subject")
    body = request.form.get("body")
    delay = int(request.form.get("delay", 2))  # Reduced default to 2 seconds

    manual = request.form.get("recipients")
    sheet = request.form.get("sheet")

    recipients = []

    if manual:
        recipients = [e.strip() for e in manual.split(",") if e.strip()]
        print(f"Manual recipients: {recipients}")
    elif sheet:
        print(f"Reading sheet: {sheet}")
        try:
            df = read_sheet(sheet)
            print(f"Sheet columns: {df.columns.tolist()}")
            recipients = df["email"].dropna().tolist()
            print(f"Found {len(recipients)} recipients from sheet")
        except Exception as e:
            print(f"ERROR reading sheet: {e}")
            return f"❌ Error reading sheet: {e}"
    else:
        return "❌ No recipients provided"

    if not recipients:
        return "❌ No valid email addresses found"

    print(f"Total recipients: {len(recipients)}")
    print(f"Subject: {subject}")
    print(f"Send type: {send_type}")

    if send_type == "now":
        # Run in background to avoid timeout
        import threading
        thread = threading.Thread(
            target=send_bulk,
            args=(session["user_email"], recipients, subject, body, delay)
        )
        thread.daemon = True  # Allow thread to be killed when main process exits
        thread.start()
        return f"✅ Sending {len(recipients)} emails in background!"

    time_str = request.form.get("time")
    # Parse the time and make it timezone-aware (IST)
    naive_time = datetime.datetime.strptime(time_str, "%Y-%m-%dT%H:%M")
    send_time = IST.localize(naive_time)
    
    current_time = datetime.datetime.now(IST)
    print(f"📅 Current time IST: {current_time}")
    print(f"⏰ Scheduled time IST: {send_time}")
    print(f"⏳ Time until execution: {send_time - current_time}")

    if send_time <= current_time:
        return "❌ Schedule time must be in the future!"

    job = scheduler.add_job(
        send_bulk,
        "date",
        run_date=send_time,
        args=[session["user_email"], recipients, subject, body, delay],
    )
    
    print(f"✅ Job scheduled with ID: {job.id}, will run at {job.next_run_time}")

    return f"⏰ Emails scheduled for {send_time.strftime('%Y-%m-%d %I:%M %p')} IST! (Job ID: {job.id})"


@app.route("/api/stats")
def stats_api():
    db = get_db()
    cursor = db.cursor()

    cursor.execute("SELECT COUNT(*) as count FROM email_logs")
    total = cursor.fetchone()['count'] if DATABASE_URL else cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) as count FROM email_logs WHERE status='sent'")
    sent = cursor.fetchone()['count'] if DATABASE_URL else cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) as count FROM email_logs WHERE status='failed'")
    failed = cursor.fetchone()['count'] if DATABASE_URL else cursor.fetchone()[0]

    cursor.execute("""
        SELECT DATE(created_at) as date, COUNT(*) as count
        FROM email_logs
        GROUP BY DATE(created_at)
        ORDER BY date
    """)
    daily = cursor.fetchall()
    
    cursor.close()
    db.close()

    return {
        "total": total,
        "sent": sent,
        "failed": failed,
        "daily": [dict(row) for row in daily]
    }

@app.route("/stats")
def stats():
    return render_template("stats.html")

@app.route("/debug/jobs")
def debug_jobs():
    """Debug endpoint to see scheduled jobs"""
    jobs = scheduler.get_jobs()
    job_info = []
    for job in jobs:
        job_info.append({
            "id": job.id,
            "next_run": str(job.next_run_time),
            "func": job.func.__name__
        })
    return {
        "scheduled_jobs": job_info,
        "scheduler_running": scheduler.running,
        "current_time_utc": str(datetime.datetime.now(pytz.UTC)),
        "current_time_ist": str(datetime.datetime.now(IST))
    }

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
