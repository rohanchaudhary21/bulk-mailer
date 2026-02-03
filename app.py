from flask import Flask, render_template, request, redirect, session
import os, time, base64, datetime
import pandas as pd
import os
from dotenv import load_dotenv
import sqlite3
load_dotenv()

\
from email.mime.text import MIMEText
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.oauth2.credentials import Credentials
from db import get_db
import json

def get_gmail_service(user_email):
    db = get_db()
    cursor = db.cursor()

    cursor.execute(
        "SELECT token_json FROM oauth_tokens WHERE user_email=?",
        (user_email,)
    )
    row = cursor.fetchone()

    if not row:
        raise Exception("User not authenticated")

    creds = Credentials.from_authorized_user_info(json.loads(row[0]))
    return build("gmail", "v1", credentials=creds)

from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET")


SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]

scheduler = BackgroundScheduler()
scheduler.start()

# ---------- AUTH ----------

@app.route("/")
def home():
    if os.path.exists("token.json"):
        return redirect("/dashboard")
    return render_template("login.html")


from google_auth_oauthlib.flow import Flow
import os

@app.route("/authorize")
def authorize():
    client_config = json.loads(os.environ["GOOGLE_CLIENT_CONFIG"])

    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        redirect_uri=os.environ["REDIRECT_URI"]
    )

    auth_url, _ = flow.authorization_url(
        prompt="consent",
        access_type="offline"
    )

    return redirect(auth_url)

@app.route("/callback")
def callback():
    client_config = json.loads(os.environ["GOOGLE_CLIENT_CONFIG"])

    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        redirect_uri=os.environ["REDIRECT_URI"]
    )

    flow.fetch_token(code=request.args.get("code"))
    creds = flow.credentials

    user_email = creds.id_token.get("email")

    db = get_db()
    db.execute(
        "REPLACE INTO oauth_tokens (user_email, token_json) VALUES (?, ?)",
        (user_email, creds.to_json())
    )
    db.commit()

    session["user_email"] = user_email
    return redirect("/dashboard")


# ---------- DASHBOARD ----------

@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")


# ---------- HELPERS ----------

def read_sheet(sheet_url):
    sheet_id = sheet_url.split("/d/")[1].split("/")[0]
    csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    return pd.read_csv(csv_url)

def send_email(to, subject, body):
    creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    service = build("gmail", "v1", credentials=creds)

    msg = MIMEText(body)
    msg["to"] = to
    msg["subject"] = subject

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()

def send_bulk(recipients, subject, body, delay):
    db = get_db()

    for email in recipients:
        try:
            send_email(email, subject, body)
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


# ---------- SEND ----------

@app.route("/send", methods=["POST"])
def send():
    send_type = request.form.get("send_type")
    subject = request.form.get("subject")
    body = request.form.get("body")
    delay = int(request.form.get("delay", 10))

    manual = request.form.get("recipients")
    sheet = request.form.get("sheet")

    recipients = []

    if manual:
        recipients = manual.split(",")

    elif sheet:
        df = read_sheet(sheet)
        recipients = df["email"].tolist()

    else:
        return "❌ No recipients provided"

    if send_type == "now":
        send_bulk(recipients, subject, body, delay)
        return "✅ Emails sent successfully!"

    # schedule
    time_str = request.form.get("time")
    send_time = datetime.datetime.strptime(time_str, "%Y-%m-%dT%H:%M")

    scheduler.add_job(
        send_bulk,
        "date",
        run_date=send_time,
        args=[recipients, subject, body, delay]
    )

    return "⏰ Emails scheduled successfully!"


if __name__ == "__main__":
    app.run(debug=True)
