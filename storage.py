import sqlite3
import threading
from datetime import datetime, timezone


class Store:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        with self.connect() as conn:
            conn.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL DEFAULT '',
                full_name TEXT NOT NULL DEFAULT '',
                requested_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                suspicious INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);
            CREATE INDEX IF NOT EXISTS idx_requests_user ON requests(user_id);
            CREATE TABLE IF NOT EXISTS blacklist (
                user_id INTEGER PRIMARY KEY,
                added_by INTEGER NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS action_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                action TEXT NOT NULL,
                user_id INTEGER,
                admin_id INTEGER,
                details TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS request_throttle (
                user_id INTEGER PRIMARY KEY,
                last_requested_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL DEFAULT '',
                full_name TEXT NOT NULL DEFAULT '',
                photo_file_id TEXT NOT NULL,
                character_name TEXT NOT NULL DEFAULT '',
                nickname TEXT NOT NULL,
                telegram_username TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                rejection_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                reviewed_by INTEGER,
                reviewed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_applications_user_status ON applications(user_id,status);
            CREATE TABLE IF NOT EXISTS application_review_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                caption TEXT NOT NULL DEFAULT '',
                UNIQUE(application_id, chat_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_application_review_messages_app ON application_review_messages(application_id);
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_created ON requests(requested_at)")

    def create_application(self, user_id, username, full_name, photo_file_id, character_name, nickname, telegram_username):
        with self.lock, self.connect() as conn:
            conn.execute("UPDATE applications SET status='superseded' WHERE user_id=? AND status='pending'", (user_id,))
            cur = conn.execute(
                "INSERT INTO applications(user_id,username,full_name,photo_file_id,character_name,nickname,telegram_username,status,created_at) VALUES(?,?,?,?,?,?,?,'draft',?)",
                (user_id, username, full_name, photo_file_id, character_name, nickname, telegram_username, datetime.now(timezone.utc).isoformat()),
            )
            return cur.lastrowid

    def get_application(self, application_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM applications WHERE id=?", (application_id,)).fetchone()
            return dict(row) if row else None

    def latest_application(self, user_id, statuses=None):
        with self.connect() as conn:
            if statuses:
                marks = ','.join('?' for _ in statuses)
                row = conn.execute(f"SELECT * FROM applications WHERE user_id=? AND status IN ({marks}) ORDER BY id DESC LIMIT 1", (user_id, *statuses)).fetchone()
            else:
                row = conn.execute("SELECT * FROM applications WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
            return dict(row) if row else None

    def set_application_status(self, application_id, status, admin_id=None, reason=''):
        with self.lock, self.connect() as conn:
            conn.execute("UPDATE applications SET status=?, reviewed_by=?, reviewed_at=?, rejection_reason=? WHERE id=?",
                         (status, admin_id, datetime.now(timezone.utc).isoformat(), reason[:1000], application_id))

    def add_application_review_message(self, application_id, chat_id, message_id, caption):
        with self.lock, self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO application_review_messages(application_id,chat_id,message_id,caption) VALUES(?,?,?,?)",
                (application_id, chat_id, message_id, caption or ""),
            )

    def application_review_messages(self, application_id):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT chat_id,message_id,caption FROM application_review_messages WHERE application_id=? ORDER BY id",
                (application_id,),
            )]

    def pending_applications(self, limit=20):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM applications WHERE status='pending' ORDER BY id DESC LIMIT ?", (limit,))]

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def add_request(self, chat_id, user_id, username, full_name, date, suspicious):
        with self.lock, self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO requests(chat_id,user_id,username,full_name,requested_at,status,suspicious) VALUES(?,?,?,?,?,'pending',?)",
                (chat_id, user_id, username, full_name, date, int(suspicious)),
            )
            return cur.lastrowid

    def get_request(self, request_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            return dict(row) if row else None

    def set_request_status(self, request_id, status):
        with self.lock, self.connect() as conn:
            conn.execute("UPDATE requests SET status=? WHERE id=?", (status, request_id))

    def last_request_time(self, user_id):
        with self.connect() as conn:
            row = conn.execute("SELECT last_requested_at FROM request_throttle WHERE user_id=?", (user_id,)).fetchone()
            return row["last_requested_at"] if row else None

    def set_last_request_time(self, user_id, value):
        with self.lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO request_throttle(user_id,last_requested_at) VALUES(?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET last_requested_at=excluded.last_requested_at",
                (user_id, value),
            )

    def is_banned(self, user_id):
        with self.connect() as conn:
            return conn.execute("SELECT 1 FROM blacklist WHERE user_id=?", (user_id,)).fetchone() is not None

    def ban_user(self, user_id, admin_id, reason):
        with self.lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO blacklist(user_id,added_by,reason,created_at) VALUES(?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET added_by=excluded.added_by,reason=excluded.reason,created_at=excluded.created_at",
                (user_id, admin_id, reason[:500], datetime.now(timezone.utc).isoformat()),
            )

    def unban_user(self, user_id):
        with self.lock, self.connect() as conn:
            conn.execute("DELETE FROM blacklist WHERE user_id=?", (user_id,))

    def blacklist(self, limit=30):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM blacklist ORDER BY created_at DESC LIMIT ?", (limit,))]

    def stats(self):
        with self.connect() as conn:
            request_count = lambda status: conn.execute(
                "SELECT COUNT(*) FROM requests WHERE status=?", (status,)
            ).fetchone()[0]
            app_count = lambda statuses: conn.execute(
                "SELECT COUNT(*) FROM applications WHERE status IN (" +
                ",".join("?" for _ in statuses) + ")", tuple(statuses)
            ).fetchone()[0]
            return {
                "total": conn.execute("SELECT COUNT(*) FROM applications WHERE status != 'draft'").fetchone()[0],
                "pending": app_count(["pending"]),
                "approved": app_count(["approved", "joined"]),
                "rejected": app_count(["rejected"]),
                "banned": conn.execute("SELECT COUNT(*) FROM blacklist").fetchone()[0],
                "suspicious": conn.execute("SELECT COUNT(*) FROM requests WHERE suspicious=1").fetchone()[0],
                "join_total": conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0],
                "join_pending": request_count("pending"),
                "join_approved": request_count("approved"),
                "join_rejected": request_count("rejected"),
            }

    def pending_requests(self, limit=10):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM requests WHERE status='pending' ORDER BY requested_at DESC LIMIT ?", (limit,)
            )]

    def log_action(self, action, user_id=None, admin_id=None, details=""):
        with self.lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO action_log(created_at,action,user_id,admin_id,details) VALUES(?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), action, user_id, admin_id, details[:1000]),
            )

    def logs(self, limit=15):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM action_log ORDER BY id DESC LIMIT ?", (limit,)
            )]
