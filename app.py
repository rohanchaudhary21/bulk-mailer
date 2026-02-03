from flask import Flask, render_template, request, redirect, session
import os, time, base64, datetime, json, sqlite3
import pandas as pd
from dotenv import load_dotenv
from email.mime.text import MIMEText
from apscheduler.schedulers.background import BackgroundScheduler
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

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
DB_PATH = "stats.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS email_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT,
            status TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS oauth_tokens (
            user_email TEXT PRIMARY KEY,
            token_json TEXT
        )
    """)
    db.commit()

init_db()

# ================= SCHEDULER =================
scheduler = BackgroundScheduler()
scheduler.start()

# ================= HELPERS =================

def read_sheet(sheet_url):
    sheet_id = sheet_url.split("/d/")[1].split("/")[0]
    csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    return pd.read_csv(csv_url)


def get_gmail_service(user_email):
    db = get_db()
    row = db.execute(
        "SELECT token_json FROM oauth_tokens WHERE user_email=?",
        (user_email,)
    ).fetchone()

    if not row:
        raise Exception("User not authenticated")

    creds = Credentials.from_authorized_user_info(json.loads(row[0]), SCOPES)
    return build("gmail", "v1", credentials=creds)


def send_bulk(user_email, recipients, subject, body, delay):
    db = get_db()
    service = get_gmail_service(user_email)

    for email in recipients:
        try:
            msg = MIMEText(body)
            msg["to"] = email
            msg["subject"] = subject

            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            service.users().messages().send(userId="me", body={"raw": raw}).execute()

            db.execute(
                "INSERT INTO email_logs (email, status) VALUES (?, ?)",
                (email, "sent")
            )
        except Exception:
            db.execute(
                "INSERT INTO email_logs (email, status) VALUES (?, ?)",
                (email, "failed")
            )

        db.commit()
        time.sleep(delay)

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
    db.execute(
        "REPLACE INTO oauth_tokens (user_email, token_json) VALUES (?, ?)",
        (user_email, creds.to_json())
    )
    db.commit()

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
    delay = int(request.form.get("delay", 10))

    manual = request.form.get("recipients")
    sheet = request.form.get("sheet")

    recipients = []

    if manual:
        recipients = [e.strip() for e in manual.split(",") if e.strip()]
    elif sheet:
        df = read_sheet(sheet)
        recipients = df["email"].dropna().tolist()
    else:
        return "❌ No recipients provided"

    if send_type == "now":
        send_bulk(
            session["user_email"],
            recipients,
            subject,
            body,
            delay
        )
        return "✅ Emails sent successfully!"

    time_str = request.form.get("time")
    send_time = datetime.datetime.strptime(time_str, "%Y-%m-%dT%H:%M")

    scheduler.add_job(
        send_bulk,
        "date",
        run_date=send_time,
        args=[session["user_email"], recipients, subject, body, delay],
    )

    return "⏰ Emails scheduled successfully!"


@app.route("/api/stats")
def stats_api():
    db = get_db()

    total = db.execute("SELECT COUNT(*) FROM email_logs").fetchone()[0]
    sent = db.execute("SELECT COUNT(*) FROM email_logs WHERE status='sent'").fetchone()[0]
    failed = db.execute("SELECT COUNT(*) FROM email_logs WHERE status='failed'").fetchone()[0]

    daily = db.execute("""
        SELECT DATE(created_at) as date, COUNT(*) as count
        FROM email_logs
        GROUP BY DATE(created_at)
        ORDER BY date
    """).fetchall()

    return {
        "total": total,
        "sent": sent,
        "failed": failed,
        "daily": [dict(row) for row in daily]
    }

@app.route("/stats")
def stats():
    return render_template("stats.html")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
