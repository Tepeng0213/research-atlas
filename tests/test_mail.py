import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import test_workbench as helpers
import test_accounts as account_helpers
from workbench.mail import Mailer


class FakeMailer:
    configured=True

    def __init__(self):
        self.messages=[]
        self.fail=False

    def send(self,email,kind,token):
        if self.fail:
            raise RuntimeError('SMTP error containing sensitive details')
        self.messages.append((email,kind,token))


class MailFlowTests(unittest.TestCase):
    req=account_helpers.AccountTests.req
    denied=account_helpers.AccountTests.denied

    def setUp(self):
        helpers.APITests.setUp(self)
        self.session_token=self.app.accounts.register({'email':'mail-user@example.com','display_name':'Mail test','password':'mail-password-12345'})
        self.session=self.app.accounts.session(self.session_token)
        self.fake=FakeMailer()
        self.app.mailer=self.fake
        self.app.mail_worker.mailer=self.fake

    tearDown=helpers.APITests.tearDown

    def send(self,kind):
        if kind=='verify':
            self.req('/api/auth/request-verification',{})
        else:
            self.req('/api/auth/forgot-password',{'email':'mail-user@example.com'},token='')
        self.assertTrue(self.app.mail_worker.deliver_once())
        return self.fake.messages[-1][2]

    def test_verification_reauth_single_use_and_purpose(self):
        token=self.send('verify')
        self.denied('/api/auth/verify-email',{'token':token,'current_password':'wrong'},code=403)
        self.denied('/api/auth/reset-password',{'token':token,'new_password':'new-password-12345'},token='',code=400)
        self.req('/api/auth/verify-email',{'token':token,'current_password':'mail-password-12345'})
        self.assertEqual(self.req('/api/bootstrap')['user']['email_verified'],1)
        self.denied('/api/auth/verify-email',{'token':token,'current_password':'mail-password-12345'},code=400)

    def test_unknown_and_known_recovery_have_identical_response(self):
        unknown=self.req('/api/auth/forgot-password',{'email':'unknown@example.com'},token='')
        known=self.req('/api/auth/forgot-password',{'email':'mail-user@example.com'},token='')
        self.assertEqual(unknown,known)
        self.assertNotIn('token',known)
        with self.app.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM mail_jobs').fetchone()[0],1)

    def test_reset_revokes_sessions_and_confirms_mailbox(self):
        token=self.send('reset')
        self.req('/api/auth/reset-password',{'token':token,'new_password':'recovered-password-12345'},token='')
        self.assertIsNone(self.app.accounts.session(self.session_token))
        new=self.app.accounts.login({'identifier':'mail-user@example.com','password':'recovered-password-12345'},'test-reset')
        self.assertEqual(self.app.accounts.session(new)['email_verified'],1)
        self.denied('/api/auth/reset-password',{'token':token,'new_password':'another-password-12345'},token='',code=400)

    def test_expired_and_superseded_links_rejected(self):
        old=self.send('reset')
        new=self.send('reset')
        self.denied('/api/auth/reset-password',{'token':old,'new_password':'recovered-password-12345'},token='',code=400)
        with self.app.store.db() as db:
            db.execute('UPDATE email_actions SET expires_at=?',(time.time()-1,))
        self.denied('/api/auth/reset-password',{'token':new,'new_password':'recovered-password-12345'},token='',code=400)

    def test_other_account_cannot_confirm_verification(self):
        token=self.send('verify')
        other=self.app.accounts.register({'email':'different@example.com','display_name':'Another person','password':'other-password-12345'})
        self.denied('/api/auth/verify-email',{'token':token,'current_password':'other-password-12345'},token=other,code=403)
        self.req('/api/auth/verify-email',{'token':token,'current_password':'mail-password-12345'})

    def test_send_failure_does_not_expose_or_leave_token(self):
        self.fake.fail=True
        self.req('/api/auth/request-verification',{})
        self.app.mail_worker.deliver_once()
        boot=self.req('/api/bootstrap')
        self.assertEqual(boot['mail']['last']['status'],'failed')
        self.assertNotIn('SMTP error',json.dumps(boot))
        with self.app.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM email_actions').fetchone()[0],0)

    def test_tokens_are_hashed_and_never_in_jobs(self):
        token=self.send('reset')
        with self.app.store.db() as db:
            records=[dict(r) for r in db.execute('SELECT * FROM email_actions')]
            jobs=[dict(r) for r in db.execute('SELECT * FROM mail_jobs')]
        self.assertNotIn(token,json.dumps(records+jobs))

    def test_queued_mail_invalidated_by_password_change(self):
        self.req('/api/auth/forgot-password',{'email':'mail-user@example.com'},token='')
        self.req('/api/auth/password',{'current_password':'mail-password-12345','new_password':'changed-password-12345'})
        self.app.mail_worker.deliver_once()
        self.assertEqual(self.fake.messages,[])

    def test_email_change_invalidates_old_link(self):
        token=self.send('reset')
        self.req('/api/auth/email',{'email':'new-address@example.com','current_password':'mail-password-12345'})
        self.denied('/api/auth/reset-password',{'token':token,'new_password':'recovered-password-12345'},token='',code=400)

    def test_unconfigured_smtp_returns_explicit_error(self):
        self.fake.configured=False
        self.denied('/api/auth/forgot-password',{'email':'mail-user@example.com'},token='',code=503)


class SMTPTests(unittest.TestCase):
    def test_gmail_ssl_config_and_message(self):
        with tempfile.TemporaryDirectory() as root, patch.dict('os.environ',{},clear=True):
            Path(root,'mail.local.env').write_text('SMTP_HOST=smtp.gmail.com\nSMTP_USERNAME=sender@example.com\nSMTP_PASSWORD=fixture-password\nMAIL_FROM=sender@example.com\nPUBLIC_BASE_URL=http://127.0.0.1:8765\n')
            mailer=Mailer(root)
            self.assertTrue(mailer.configured)
            with patch('workbench.mail.smtplib.SMTP_SSL') as client:
                mailer.send('recipient@example.com','reset','fixture-token')
                client.assert_called_once()
                smtp=client.return_value.__enter__.return_value
                smtp.login.assert_called_once_with('sender@example.com','fixture-password')
                message=smtp.send_message.call_args[0][0]
                self.assertIn('/#reset-password/fixture-token',message.get_content())
                self.assertEqual(message['To'],'recipient@example.com')

    def test_untrusted_host_cannot_control_email_url(self):
        with tempfile.TemporaryDirectory() as root,patch.dict('os.environ',{'PUBLIC_BASE_URL':'http://evil.example'},clear=True):
            with self.assertRaises(ValueError):
                Mailer(root)


if __name__=='__main__':
    unittest.main()
