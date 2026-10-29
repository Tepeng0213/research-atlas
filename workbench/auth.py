"""Local accounts, persistent opaque sessions, workspace authorization.

All authorization is derived from current database membership, never client roles.
"""
import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from .store import now, uid

ROLES = {
    'admin': {'read', 'write', 'review', 'members'},
    'researcher': {'read', 'write'},
    'reviewer': {'read', 'review'},
    'viewer': {'read'},
}
SESSION_TTL = 7 * 24 * 60 * 60


class AuthError(Exception):
    def __init__(self, message, status=401):
        super().__init__(message)
        self.status = status


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def password_hash(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 128:
        raise ValueError('Password must be 12–128 characters long')
    salt = secrets.token_hex(16)
    encoded = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600000).hex()
    return 'pbkdf2_sha256$600000$' + salt + '$' + encoded


def verify_password(password, stored):
    if not isinstance(password, str) or len(password) > 128:
        return False
    _, rounds, salt, expected = stored.split('$')
    actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(rounds)).hex()
    return hmac.compare_digest(actual, expected)


class Accounts:
    def __init__(self, store):
        self.store = store
        self.dummy_hash = password_hash(secrets.token_urlsafe(24))

    @staticmethod
    def username(value):
        if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{2,63}', value):
            raise ValueError('Username must be 3–64 characters and use only ASCII letters, digits, periods, hyphens, or underscores')
        return value.lower()

    @staticmethod
    def email(value):
        if not isinstance(value, str):
            raise ValueError('Enter a valid email address')
        value = value.strip().lower()
        if len(value) > 254 or value.count('@') != 1:
            raise ValueError('Enter a valid email address')
        local, domain = value.split('@')
        if not 1 <= len(local) <= 64 or not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+", local) or local.startswith('.') or local.endswith('.') or '..' in local:
            raise ValueError('Enter a valid email address')
        labels = domain.split('.')
        if len(labels) < 2 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels) or not re.fullmatch(r'[a-z]{2,63}', labels[-1]):
            raise ValueError('Enter a valid email address')
        return value

    def limited(self, bucket, maximum=10):
        """Persistent per-account/IP counters; no passwords or raw names in log."""
        stamp = time.time()
        blocked = False
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM auth_limits WHERE reset_at<?', (stamp,))
            row = db.execute('SELECT * FROM auth_limits WHERE bucket=?', (bucket,)).fetchone()
            if row and row['attempts'] >= maximum:
                blocked = True
            else:
                db.execute('INSERT INTO auth_limits VALUES (?,?,?) ON CONFLICT(bucket) DO UPDATE SET attempts=attempts+1',
                           (bucket, 1, stamp + 900))
        if blocked:
            raise AuthError('Too many attempts. Try again in 15 minutes.', 429)

    @staticmethod
    def audit(db, workspace, actor, action, subject=None):
        db.execute('INSERT INTO audit(workspace_id,actor_id,action,subject_id,created_at) VALUES (?,?,?,?,?)',
                   (workspace, actor, action, subject, now()))

    def register(self, data):
        email = self.email(data['email']) if 'email' in data else None
        # Existing username-only accounts remain valid; new UI uses email.
        username = ('u_' + uid()) if email else self.username(data.get('username'))
        display = data.get('display_name', '')
        if not isinstance(display, str) or not 1 <= len(display.strip()) <= 80:
            raise ValueError('Display name must be 1–80 characters long')
        hashed = password_hash(data.get('password'))
        user_id, workspace_id = uid(), uid()
        try:
            with self.store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                first = not db.execute('SELECT 1 FROM users LIMIT 1').fetchone()
                db.execute('INSERT INTO users(id,username,display_name,password_hash,created_at,email) VALUES (?,?,?,?,?,?)',
                           (user_id, username, display.strip(), hashed, now(), email))
                db.execute('INSERT INTO workspaces VALUES (?,?,?)', (workspace_id, display.strip() + "'s workspace", now()))
                db.execute('INSERT INTO memberships VALUES (?,?,?)', (workspace_id, user_id, 'admin'))
                if first:
                    # Only the very first account claims legacy single-user projects.
                    db.execute('UPDATE projects SET workspace_id=? WHERE workspace_id IS NULL', (workspace_id,))
                if not db.execute('SELECT 1 FROM projects WHERE workspace_id=?', (workspace_id,)).fetchone():
                    db.execute('INSERT INTO projects(id,name,created_at,workspace_id) VALUES (?,?,?,?)',
                               (uid(), 'Research project', now(), workspace_id))
                self.audit(db, workspace_id, user_id, 'account.register', user_id)
        except sqlite3.IntegrityError:
            raise ValueError('This email address is already in use' if email else 'This username is unavailable')
        return self.new_session(user_id)

    def login(self, data, ip):
        raw = data.get('identifier', data.get('email', data.get('username', '')))
        username = raw.strip().lower() if isinstance(raw, str) else ''
        self.limited('login-ip:' + digest(ip), 50)
        bucket = 'login-user:' + digest(username)
        self.limited(bucket)
        with self.store.db() as db:
            user = db.execute('SELECT * FROM users WHERE email=?' if '@' in username else 'SELECT * FROM users WHERE username=?', (username,)).fetchone()
        valid = verify_password(data.get('password'), user['password_hash'] if user else self.dummy_hash)
        if not user or not valid:
            raise AuthError('Email, username, or password is incorrect')
        with self.store.db() as db:
            db.execute('DELETE FROM auth_limits WHERE bucket=?', (bucket,))
            self.audit(db, None, user['id'], 'account.login')
        return self.new_session(user['id'])

    def new_session(self, user_id):
        token = secrets.token_urlsafe(32)
        with self.store.db() as db:
            db.execute('DELETE FROM sessions WHERE expires_at<=?', (time.time(),))
            db.execute('INSERT INTO sessions VALUES (?,?,?,?)', (digest(token), user_id, secrets.token_urlsafe(32), time.time() + SESSION_TTL))
        return token

    def session(self, token):
        if not token:
            return None
        with self.store.db() as db:
            row = db.execute('SELECT u.id,u.username,u.display_name,u.email,u.email_verified,s.csrf,s.token_hash FROM sessions s JOIN users u ON u.id=s.user_id WHERE token_hash=? AND expires_at>?',
                             (digest(token), time.time())).fetchone()
            return dict(row) if row else None

    @staticmethod
    def public(user):
        return {key: user[key] for key in ('id', 'username', 'display_name', 'email', 'email_verified')}

    def workspaces(self, user):
        with self.store.db() as db:
            return [dict(r) for r in db.execute('SELECT w.*,m.role FROM workspaces w JOIN memberships m ON m.workspace_id=w.id WHERE m.user_id=? ORDER BY w.created_at,w.id', (user['id'],))]

    def require_workspace(self, db, user, workspace_id, permission='read'):
        row = db.execute('SELECT role FROM memberships WHERE workspace_id=? AND user_id=?', (workspace_id, user['id'])).fetchone()
        if not row:
            raise AuthError('Resource not found or access denied', 404)
        if permission not in ROLES.get(row['role'], set()):
            raise AuthError('Your current role does not allow this action', 403)
        return row['role']

    def require_project(self, db, user, project_id, permission='read'):
        row = db.execute('SELECT workspace_id FROM projects WHERE id=?', (project_id,)).fetchone()
        if not row or not row['workspace_id']:
            raise AuthError('Resource not found or access denied', 404)
        self.require_workspace(db, user, row['workspace_id'], permission)
        return row['workspace_id']

    def require_run(self, db, user, run_id, permission='read'):
        row = db.execute('SELECT project_id FROM runs WHERE id=?', (run_id,)).fetchone()
        if not row:
            raise AuthError('Resource not found or access denied', 404)
        return self.require_project(db, user, row['project_id'], permission)

    def change_password(self, user, data):
        hashed = password_hash(data.get('new_password'))
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT password_hash FROM users WHERE id=?', (user['id'],)).fetchone()
            if not verify_password(data.get('current_password'), row['password_hash']):
                raise AuthError('Current password is incorrect', 403)
            db.execute('UPDATE users SET password_hash=?,credential_version=credential_version+1 WHERE id=?', (hashed, user['id']))
            db.execute('DELETE FROM sessions WHERE user_id=?', (user['id'],))
            db.execute('DELETE FROM email_actions WHERE user_id=?', (user['id'],))
            self.audit(db, None, user['id'], 'account.password_changed')

    def change_email(self, user, data):
        email = self.email(data.get('email'))
        try:
            with self.store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT password_hash FROM users WHERE id=?', (user['id'],)).fetchone()
                if not verify_password(data.get('current_password'), row['password_hash']):
                    raise AuthError('Current password is incorrect', 403)
                db.execute('UPDATE users SET email=?,email_verified=0,credential_version=credential_version+1 WHERE id=?', (email,user['id']))
                db.execute('DELETE FROM sessions WHERE user_id=?', (user['id'],))
                db.execute('DELETE FROM email_actions WHERE user_id=?', (user['id'],))
                self.audit(db,None,user['id'],'account.email_changed')
        except sqlite3.IntegrityError:
            raise ValueError('This email address is already in use')

    def issue_email_token(self, user_id, email, kind, credential_version=None):
        if kind not in ('verify','reset'):
            raise ValueError('Invalid email action')
        token = secrets.token_urlsafe(32)
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            user = db.execute('SELECT email,credential_version FROM users WHERE id=?',(user_id,)).fetchone()
            if not user or user['email'] != email or (credential_version is not None and credential_version!=user['credential_version']):
                raise ValueError('The email address has changed')
            db.execute('DELETE FROM email_actions WHERE user_id=? AND kind=?',(user_id,kind))
            db.execute('INSERT INTO email_actions VALUES (?,?,?,?,?,NULL,?)',
                       (digest(token),user_id,email,kind,time.time()+(1800 if kind=='reset' else 3600),now()))
        return token

    def consume_email_token(self, token, kind, user=None, password=None):
        if not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{40,100}',token):
            raise ValueError('This link is invalid, already used, or expired. Request a new one.')
        hashed = password_hash(password) if kind=='reset' else None
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            action=db.execute('SELECT a.*,u.email AS current_email,u.password_hash FROM email_actions a JOIN users u ON a.user_id=u.id WHERE a.token_hash=? AND a.kind=? AND a.used_at IS NULL AND a.expires_at>?',
                              (digest(token),kind,time.time())).fetchone()
            if not action or action['email']!=action['current_email']:
                raise ValueError('This link is invalid, already used, or expired. Request a new one.')
            if kind=='verify':
                if not user or user['id']!=action['user_id']:
                    raise AuthError('Sign in to the account that received the verification email first',403)
                if not verify_password(password,action['password_hash']):
                    raise AuthError('Current password is incorrect',403)
                db.execute('UPDATE users SET email_verified=1,credential_version=credential_version+1 WHERE id=?',(user['id'],))
                db.execute('DELETE FROM email_actions WHERE user_id=?',(user['id'],))
                self.audit(db,None,user['id'],'account.email_verified')
            elif kind=='reset':
                # Mailbox possession plus a new password establishes ownership even
                # for an old, unverified email account. Never restore old sessions.
                db.execute('UPDATE users SET password_hash=?,email_verified=1,credential_version=credential_version+1 WHERE id=?',(hashed,action['user_id']))
                db.execute('DELETE FROM sessions WHERE user_id=?',(action['user_id'],))
                db.execute('DELETE FROM email_actions WHERE user_id=?',(action['user_id'],))
                db.execute('UPDATE mail_jobs SET status=? WHERE user_id=? AND status=?',('cancelled',action['user_id'],'queued'))
                self.audit(db,None,action['user_id'],'account.password_reset')
            else:
                raise ValueError('Invalid email action')

    def members(self, user, workspace_id):
        with self.store.db() as db:
            self.require_workspace(db, user, workspace_id)
            return [dict(r) for r in db.execute('SELECT u.id,u.username,u.display_name,m.role FROM memberships m JOIN users u ON m.user_id=u.id WHERE m.workspace_id=? ORDER BY u.created_at', (workspace_id,))]

    def invite(self, user, workspace_id, role):
        if role not in ROLES:
            raise ValueError('Invalid member role')
        code, expires = secrets.token_urlsafe(24), time.time() + 7 * 86400
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.require_workspace(db, user, workspace_id, 'members')
            db.execute('INSERT INTO invitations VALUES (?,?,?,?,?,NULL)', (digest(code), workspace_id, role, user['id'], expires))
            self.audit(db, workspace_id, user['id'], 'invitation.created')
        return {'code': code, 'expires_at': expires, 'role': role}

    def join(self, user, code):
        if not isinstance(code, str) or not 20 <= len(code) <= 100:
            raise ValueError('Invitation code is invalid or expired')
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            invitation = db.execute('SELECT * FROM invitations WHERE token_hash=? AND used_at IS NULL AND expires_at>?', (digest(code), time.time())).fetchone()
            if not invitation:
                raise ValueError('Invitation code is invalid or expired')
            workspace = invitation['workspace_id']
            if db.execute('SELECT 1 FROM memberships WHERE workspace_id=? AND user_id=?', (workspace, user['id'])).fetchone():
                raise ValueError('You are already a member of this workspace')
            db.execute('INSERT INTO memberships VALUES (?,?,?)', (workspace, user['id'], invitation['role']))
            db.execute('UPDATE invitations SET used_at=? WHERE token_hash=?', (now(), invitation['token_hash']))
            self.audit(db, workspace, user['id'], 'membership.joined', user['id'])
        return {'workspace_id': workspace}

    def update_member(self, user, workspace, target, role):
        if role is not None and role not in ROLES:
            raise ValueError('Invalid member role')
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.require_workspace(db, user, workspace, 'members')
            old = db.execute('SELECT role FROM memberships WHERE workspace_id=? AND user_id=?', (workspace, target)).fetchone()
            if not old:
                raise AuthError('Member not found', 404)
            count = db.execute("SELECT COUNT(*) FROM memberships WHERE workspace_id=? AND role='admin'", (workspace,)).fetchone()[0]
            if old['role'] == 'admin' and role != 'admin' and count <= 1:
                raise ValueError('A workspace must retain at least one admin')
            if role is None:
                db.execute('DELETE FROM memberships WHERE workspace_id=? AND user_id=?', (workspace, target))
            else:
                db.execute('UPDATE memberships SET role=? WHERE workspace_id=? AND user_id=?', (role, workspace, target))
            if old['role'] == 'admin' and role != 'admin':
                db.execute('UPDATE invitations SET expires_at=? WHERE workspace_id=? AND created_by=? AND used_at IS NULL',
                           (time.time(), workspace, target))
            self.audit(db, workspace, user['id'], 'membership.removed' if role is None else 'membership.role_changed', target)
