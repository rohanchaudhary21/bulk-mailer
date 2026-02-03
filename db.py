import sqlite3

def get_db():
    conn = sqlite3.connect("app.db", check_same_thread=False)
    return conn

def init_db():
    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS oauth_tokens (
        user_email TEXT PRIMARY KEY,
        token_json TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS email_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT,
        status TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    db.commit()
