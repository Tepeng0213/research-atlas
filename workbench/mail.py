"""SMTP transactional email. Never logs credentials, message bodies or tokens."""
import os
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from urllib.parse import urlsplit
from .store import uid, now
from .auth import AuthError, digest

MAIL_KEYS = {'SMTP_HOST','SMTP_PORT','SMTP_SECURITY','SMTP_USERNAME','SMTP_PASSWORD','MAIL_FROM','PUBLIC_BASE_URL'}


def load_mail_config(root):
    """Literal key/value file; no shell execution or interpolation."""
    path = Path(root) / 'mail.local.env'
    values = {}
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            key, separator, value = line.partition('=')
            if separator and key.strip() in MAIL_KEYS:
                values[key.strip()] = value.strip()
    for key in MAIL_KEYS:
        if key in os.environ:
            values[key] = os.environ[key]
    return values


class Mailer:
    def __init__(self, root):
        config = load_mail_config(root)
        self.host = config.get('SMTP_HOST','')
        self.username = config.get('SMTP_USERNAME','')
        self.password = config.get('SMTP_PASSWORD','')
        self.sender = config.get('MAIL_FROM','')
        self.security = config.get('SMTP_SECURITY','ssl')
        self.port = int(config.get('SMTP_PORT','465') or '465')
        self.base = config.get('PUBLIC_BASE_URL','http://127.0.0.1:8765').rstrip('/')
        address = urlsplit(self.base)
        if address.scheme not in ('http','https') or not address.hostname or address.username or address.password or address.query or address.fragment or address.path:
            raise ValueError('PUBLIC_BASE_URL must be a valid site origin')
        if address.scheme == 'http' and address.hostname not in ('localhost','127.0.0.1','::1'):
            raise ValueError('Email links for public sites must use HTTPS')
        if self.security not in ('ssl','starttls'):
            raise ValueError('SMTP_SECURITY must be ssl or starttls')
        if any(c in self.sender for c in '\r\n'):
            raise ValueError('Invalid sender address')

    @property
    def configured(self):
        return bool(self.host and self.username and self.password and self.sender)

    def send(self, recipient, kind, token):
        if not self.configured:
            raise RuntimeError('SMTP is not configured')
        reset = kind == 'reset'
        title = 'Reset your Research Atlas password' if reset else 'Verify your Research Atlas email'
        route = 'reset-password' if reset else 'verify-email'
        link = self.base + '/#' + route + '/' + token
        minutes = 30 if reset else 60
        message = EmailMessage()
        message['Subject'] = title
        message['From'] = self.sender
        message['To'] = recipient
        message['Date'] = formatdate(localtime=False)
        message['Message-ID'] = make_msgid()
        message.set_content(title + '\n\nOpen this link:\n' + link + '\n\nThe link expires in ' + str(minutes) + ' minutes and can only be used once.'
                            + ('\nEmail verification requires signing in to the original account. The link does not sign you in.' if not reset else '\nPassword reset revokes all existing sessions.')
                            + '\n\nIf you did not request this, you can ignore this email.')
        context = ssl.create_default_context()
        client = smtplib.SMTP_SSL(self.host,self.port,timeout=15,context=context) if self.security == 'ssl' else smtplib.SMTP(self.host,self.port,timeout=15)
        with client as smtp:
            if self.security == 'starttls':
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
            smtp.login(self.username,self.password)
            smtp.send_message(message)


class MailWorker:
    def __init__(self,store,accounts,mailer):
        self.store,self.accounts,self.mailer=store,accounts,mailer
        self.stopping=threading.Event()
        self.thread=None

    def queue(self,email,kind,user=None,ip='local'):
        if not self.mailer.configured:
            raise AuthError('Email delivery is not configured. Contact the platform administrator.',503)
        self.accounts.limited('mail-ip:'+digest(ip),20)
        self.accounts.limited('mail-email:'+digest(email)+':'+kind,3)
        with self.store.db() as db:
            row=db.execute('SELECT id,email_verified,credential_version FROM users WHERE email=?',(email,)).fetchone()
            if row and (kind!='verify' or (user and user['id']==row['id'] and not row['email_verified'])):
                db.execute('INSERT INTO mail_jobs(id,user_id,email,kind,status,created_at,updated_at,credential_version) VALUES (?,?,?,?,?,?,?,?)',
                           (uid(),row['id'],email,kind,'queued',now(),now(),row['credential_version']))
        # Same acknowledgement regardless of account existence; SMTP is async.
        return {'ok':True,'message':'If the email address is eligible, a message will be sent. Check your inbox and spam folder.'}

    def start(self):
        with self.store.db() as db:
            db.execute("UPDATE mail_jobs SET status='failed',updated_at=? WHERE status='sending'",(now(),))
        self.thread=threading.Thread(target=self.loop,daemon=True,name='transactional-mail')
        self.thread.start()

    def stop(self):
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=2)

    def loop(self):
        while not self.stopping.is_set():
            try:
                if self.deliver_once():
                    continue
            except Exception:
                pass  # No secrets or SMTP payloads in application logs.
            self.stopping.wait(.5)

    def deliver_once(self):
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            job=db.execute("SELECT * FROM mail_jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not job:
                return False
            job=dict(job)
            db.execute("UPDATE mail_jobs SET status='sending',updated_at=? WHERE id=?",(now(),job['id']))
        token=None
        try:
            token=self.accounts.issue_email_token(job['user_id'],job['email'],job['kind'],job['credential_version'])
            self.mailer.send(job['email'],job['kind'],token)
            status='sent'
        except Exception:
            status='failed'
            if token:
                with self.store.db() as db:
                    db.execute('DELETE FROM email_actions WHERE token_hash=?',(digest(token),))
        with self.store.db() as db:
            db.execute('UPDATE mail_jobs SET status=?,updated_at=? WHERE id=?',(status,now(),job['id']))
        return True
