"""SQLite state and transaction boundary. No model context is a source of truth."""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid.uuid4().hex


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "workbench.sqlite3"
        if self.path.exists():
            with sqlite3.connect(str(self.path)) as old:
                columns = {r[1] for r in old.execute('PRAGMA table_info(projects)')}
                old_user_columns = {r[1] for r in old.execute('PRAGMA table_info(users)')}
                needs_accounts = columns and 'workspace_id' not in columns
                needs_email = old_user_columns and 'email' not in old_user_columns
                if needs_accounts or needs_email:
                    backup_dir = self.root / 'backups'
                    backup_dir.mkdir(exist_ok=True)
                    prefix = 'pre-accounts-' if needs_accounts else 'pre-email-'
                    with sqlite3.connect(str(backup_dir / (prefix + uid() + '.sqlite3'))) as backup:
                        old.backup(backup)
        with self.db() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS projects (
                  id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                  status TEXT NOT NULL, step INTEGER NOT NULL DEFAULT 0,
                  document TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id),
                  created_at TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS files (
                  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                  name TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
                  created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS users (
                  id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                  display_name TEXT NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS workspaces (
                  id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS memberships (
                  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                  user_id TEXT NOT NULL REFERENCES users(id), role TEXT NOT NULL,
                  PRIMARY KEY(workspace_id,user_id));
                CREATE TABLE IF NOT EXISTS sessions (
                  token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                  csrf TEXT NOT NULL, expires_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS invitations (
                  token_hash TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                  role TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(id),
                  expires_at REAL NOT NULL, used_at TEXT);
                CREATE TABLE IF NOT EXISTS audit (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id TEXT,
                  actor_id TEXT, action TEXT NOT NULL, subject_id TEXT, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS auth_limits (
                  bucket TEXT PRIMARY KEY, attempts INTEGER NOT NULL, reset_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS email_actions (
                  token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                  email TEXT NOT NULL, kind TEXT NOT NULL, expires_at REAL NOT NULL,
                  used_at TEXT, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS mail_jobs (
                  id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                  email TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            ''')
            columns = {r['name'] for r in db.execute('PRAGMA table_info(projects)')}
            if 'workspace_id' not in columns:
                db.execute('ALTER TABLE projects ADD COLUMN workspace_id TEXT REFERENCES workspaces(id)')
            user_columns = {r['name'] for r in db.execute('PRAGMA table_info(users)')}
            if 'email' not in user_columns:
                db.execute('ALTER TABLE users ADD COLUMN email TEXT')
            if 'email_verified' not in user_columns:
                db.execute('ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0')
            if 'credential_version' not in user_columns:
                db.execute('ALTER TABLE users ADD COLUMN credential_version INTEGER NOT NULL DEFAULT 0')
            mail_columns = {r['name'] for r in db.execute('PRAGMA table_info(mail_jobs)')}
            if 'credential_version' not in mail_columns:
                db.execute('ALTER TABLE mail_jobs ADD COLUMN credential_version INTEGER NOT NULL DEFAULT 0')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email) WHERE email IS NOT NULL')
            db.execute('CREATE INDEX IF NOT EXISTS idx_projects_workspace ON projects(workspace_id)')
            db.execute('CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id)')
            if not db.execute("SELECT 1 FROM projects LIMIT 1").fetchone():
                db.execute("INSERT INTO projects(id,name,created_at) VALUES (?,?,?)", (uid(), "Research project", now()))

    @contextmanager
    def db(self):
        db = sqlite3.connect(str(self.path), timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def projects(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM projects ORDER BY created_at")]

    def project(self, name, workspace_id=None, authorize=None):
        item = {"id": uid(), "name": name, "created_at": now(), "workspace_id": workspace_id}
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if authorize:
                authorize(db)
            db.execute("INSERT INTO projects(id,name,created_at,workspace_id) VALUES (:id,:name,:created_at,:workspace_id)", item)
        return item

    def create(self, document, authorize=None):
        stamp, run_id = now(), uid()
        document.update(id=run_id, outputs={}, review=None, model_calls=0, model_attempts=0)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if authorize:
                authorize(db)
            db.execute("INSERT INTO runs VALUES (?,?,?,0,?,?,?)",
                       (run_id, document["project_id"], "draft", json.dumps(document), stamp, stamp))
            self.event(db, run_id, "created", "Research run created. Add research files before starting the workflow.")
        return self.get(run_id)

    @staticmethod
    def event(db, run_id, kind, message):
        db.execute("INSERT INTO events(run_id,created_at,kind,message) VALUES (?,?,?,?)",
                   (run_id, now(), kind, message))

    @staticmethod
    def unpack(row):
        if row is None:
            raise KeyError("Run not found")
        result = json.loads(row["document"])
        result.update(status=row["status"], step=row["step"], created_at=row["created_at"], updated_at=row["updated_at"])
        return result

    def get(self, run_id):
        with self.db() as db:
            item = self.unpack(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            item["events"] = [dict(r) for r in db.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,))]
            item["files"] = [dict(r) for r in db.execute("SELECT * FROM files WHERE run_id=? ORDER BY created_at", (run_id,))]
            return item

    def runs(self):
        with self.db() as db:
            return [self.unpack(r) for r in db.execute("SELECT * FROM runs ORDER BY created_at DESC")]

    def mutate(self, run_id, fn):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            item = self.unpack(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            fn(item, db)
            status, step = item.pop("status"), item.pop("step")
            item.pop("created_at", None)
            item.pop("updated_at", None)
            db.execute("UPDATE runs SET status=?,step=?,document=?,updated_at=? WHERE id=?",
                       (status, step, json.dumps(item, ensure_ascii=False), now(), run_id))
        return self.get(run_id)

    def recover(self):
        with self.db() as db:
            for row in db.execute("SELECT id FROM runs WHERE status='running'").fetchall():
                db.execute("UPDATE runs SET status='queued',updated_at=? WHERE id=?", (now(), row["id"]))
                self.event(db, row["id"], "recovered", "Service restarted. Resuming from the last committed stage; interrupted external requests may need to be retried.")
