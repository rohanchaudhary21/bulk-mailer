from flask import Flask, render_template, request, redirect, session
import os, time, base64, datetime
import pandas as pd
import os
from dotenv import load_dotenv
load_dotenv()

\
from email.mime.text import MIMEText
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from apscheduler.schedulers.background import BackgroundScheduler

os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

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


@app.route("/authorize")
def authorize():
    flow = Flow.from_client_secrets_file(
        "credentials.json",
        scopes=SCOPES,
        redirect_uri=os.getenv("REDIRECT_URI"),
    )
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
    return redirect(auth_url)


@app.route("/callback")
def callback():
    flow = Flow.from_client_secrets_file(
        "credentials.json",
        scopes=SCOPES,
        redirect_uri=os.getenv("REDIRECT_URI"),
    )
    flow.fetch_token(code=request.args.get("code"))

    with open("token.json", "w") as f:
        f.write(flow.credentials.to_json())

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
    for email in recipients:
        send_email(email.strip(), subject, body)
        time.sleep(delay)


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
