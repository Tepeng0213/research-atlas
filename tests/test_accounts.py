import base64
import json
import sqlite3
import time
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import test_workbench as helpers
from workbench.auth import Accounts, AuthError, digest, password_hash, verify_password
from workbench.store import Store


class AccountTests(unittest.TestCase):
    setUp = helpers.APITests.setUp
    tearDown = helpers.APITests.tearDown

    def req(self, path, data=None, token=None, workspace=None):
        token = self.session_token if token is None else token
        session = self.app.accounts.session(token)
        headers = {'Content-Type':'application/json', 'X-Workbench-Token':session['csrf'] if session else self.app.token}
        if token:
            headers['Cookie'] = 'atlas_session='+token
        if workspace:
            headers['X-Workspace-ID']=workspace
        response = urlopen(Request(self.base+path,None if data is None else json.dumps(data).encode(),headers),timeout=10)
        with response:
            raw=response.read()
            if response.headers.get_content_type()=='application/json':
                return json.loads(raw)
            return raw

    def other(self, username='other_user'):
        return self.app.accounts.register({'username':username,'display_name':'Another Researcher','password':'other-password-12345'})

    def make_run(self):
        boot=self.req('/api/bootstrap')
        return self.req('/api/runs',{'project_id':boot['projects'][0]['id'],'pack_id':'drug_discovery','title':'Private research','objective':'Confidential target'})

    def denied(self, path, data=None, token=None, workspace=None, code=404):
        with self.assertRaises(HTTPError) as caught:
            self.req(path,data,token,workspace)
        self.assertEqual(caught.exception.code,code)

    def test_anonymous_has_no_research_access(self):
        run=self.make_run()
        boot=self.req('/api/bootstrap',token='')
        self.assertIsNone(boot['user'])
        self.assertNotIn('projects',boot)
        for path in ['/api/runs','/api/runs/'+run['id'],'/api/runs/'+run['id']+'/export','/api/files/unknown','/api/members']:
            self.denied(path,token='',code=401)
        self.denied('/api/projects',{'name':'unauthorized'},token='',code=401)

    def test_private_workspaces_isolate_all_run_and_file_routes(self):
        owner=self.req('/api/bootstrap')
        run=self.make_run()
        uploaded=self.req('/api/runs/'+run['id']+'/files',{'name':'private.csv','content':base64.b64encode(b'private-data').decode()})
        file_id=uploaded['files'][0]['id']
        other=self.other()
        boot=self.req('/api/bootstrap',token=other)
        self.assertNotEqual(owner['workspace']['id'],boot['workspace']['id'])
        self.assertEqual(self.req('/api/runs',token=other),[])
        for suffix in ['', '/report','/export']:
            self.denied('/api/runs/'+run['id']+suffix,token=other)
        self.denied('/api/files/'+file_id,token=other)
        for action,payload in [('start',{}),('cancel',{}),('approve',{}),('files',{'name':'x.txt','content':'YQ=='})]:
            self.denied('/api/runs/'+run['id']+'/'+action,payload,token=other)
        self.denied('/api/runs',{'project_id':owner['projects'][0]['id'],'pack_id':'drug_discovery','title':'spoof','objective':'spoof'},token=other)
        self.denied('/api/runs',token=other,workspace=owner['workspace']['id'])
        self.assertEqual(self.req('/api/files/'+file_id),b'private-data')

    def test_viewer_invite_permissions_and_single_use(self):
        run=self.make_run()
        space=self.req('/api/bootstrap')['workspace']['id']
        invite=self.req('/api/invitations',{'role':'viewer'})
        other=self.other()
        self.req('/api/invitations/accept',{'code':invite['code']},token=other)
        self.assertEqual(len(self.req('/api/runs',token=other,workspace=space)),1)
        self.assertEqual(self.req('/api/runs/'+run['id'],token=other)['title'],'Private research')
        for path,payload in [('/api/projects',{'name':'forbidden'}),('/api/runs/'+run['id']+'/start',{}),('/api/runs/'+run['id']+'/approve',{}),('/api/invitations',{'role':'admin'})]:
            self.denied(path,payload,token=other,workspace=space,code=403)
        third=self.other('third_user')
        self.denied('/api/invitations/accept',{'code':invite['code']},token=third,code=400)
        with self.app.store.db() as db:
            record=db.execute('SELECT * FROM invitations').fetchone()
            self.assertNotEqual(record['token_hash'],invite['code'])

    def test_researcher_cannot_review_and_reviewer_cannot_write(self):
        run=self.make_run()
        space=self.req('/api/bootstrap')['workspace']['id']
        other=self.other()
        invite=self.req('/api/invitations',{'role':'researcher'})
        self.req('/api/invitations/accept',{'code':invite['code']},token=other)
        self.req('/api/runs/'+run['id']+'/start',{},token=other)
        for _ in range(10):
            self.app.engine.advance(run['id'])
        self.denied('/api/runs/'+run['id']+'/approve',{},token=other,code=403)
        user_id=self.app.accounts.session(other)['id']
        self.req('/api/members/'+user_id,{'role':'reviewer'})
        self.denied('/api/runs/'+run['id']+'/cancel',{},token=other,code=403)
        approved=self.req('/api/runs/'+run['id']+'/approve',{'note':'reviewed'},token=other)
        self.assertEqual(approved['review']['actor']['id'],user_id)
        self.req('/api/members/'+user_id,{'action':'remove'})
        self.denied('/api/runs/'+run['id'],token=other)
        self.denied('/api/runs',token=other,workspace=space)

    def test_last_admin_cannot_be_removed_or_demoted(self):
        for payload in [{'action':'remove'},{'role':'viewer'}]:
            self.denied('/api/members/'+self.session['id'],payload,code=400)
        self.assertEqual(self.req('/api/bootstrap')['workspace']['role'],'admin')

    def test_login_cookie_logout_and_password_revocation(self):
        request=Request(self.base+'/api/auth/login',json.dumps({'username':'TEST_ADMIN','password':'test-password-12345'}).encode(),
                        {'Content-Type':'application/json','X-Workbench-Token':self.app.token})
        with urlopen(request) as response:
            cookie=response.headers['Set-Cookie']
            self.assertIn('HttpOnly',cookie)
            self.assertIn('SameSite=Strict',cookie)
            second=cookie.split(';')[0].split('=',1)[1]
        self.assertIsNotNone(self.app.accounts.session(second))
        self.req('/api/auth/password',{'current_password':'test-password-12345','new_password':'new-password-123456'})
        self.assertIsNone(self.app.accounts.session(second))
        self.assertIsNone(self.app.accounts.session(self.session_token))
        self.denied('/api/runs',token=second,code=401)
        token=self.app.accounts.login({'username':'test_admin','password':'new-password-123456'},'test')
        self.req('/api/auth/logout',{},token=token)
        self.assertIsNone(self.app.accounts.session(token))

    def test_password_storage_and_persistent_sessions(self):
        with self.app.store.db() as db:
            hashed=db.execute('SELECT password_hash FROM users WHERE id=?',(self.session['id'],)).fetchone()[0]
        self.assertNotIn('test-password',hashed)
        self.assertTrue(verify_password('test-password-12345',hashed))
        self.assertNotEqual(password_hash('test-password-12345'),hashed)
        reopened=Accounts(self.app.store)
        self.assertEqual(reopened.session(self.session_token)['id'],self.session['id'])
        with self.app.store.db() as db:
            db.execute('UPDATE sessions SET expires_at=?',(time.time()-1,))
        self.denied('/api/runs',code=401)

    def test_registration_and_login_rate_limits(self):
        for index in range(10):
            self.denied('/api/auth/login',{'username':'missing','password':'bad-password'},token='',code=401)
        self.denied('/api/auth/login',{'username':'missing','password':'bad-password'},token='',code=429)

    def test_expired_invitation_and_admin_only_audit(self):
        code=self.req('/api/invitations',{'role':'viewer'})['code']
        with self.app.store.db() as db:
            db.execute('UPDATE invitations SET expires_at=? WHERE token_hash=?',(time.time()-1,digest(code)))
        other=self.other()
        self.denied('/api/invitations/accept',{'code':code},token=other,code=400)
        self.assertTrue(self.req('/api/audit'))

    def test_first_account_claims_legacy_data_only_once(self):
        # setUp's first account already claimed the legacy project.
        other=self.other()
        a=self.req('/api/bootstrap')
        b=self.req('/api/bootstrap',token=other)
        self.assertNotEqual(a['projects'][0]['id'],b['projects'][0]['id'])
        with self.app.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM projects WHERE workspace_id IS NULL').fetchone()[0],0)

    def test_legacy_schema_backup_and_claim(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as root:
            with sqlite3.connect(str(Path(root)/'workbench.sqlite3')) as db:
                db.execute('CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,created_at TEXT NOT NULL)')
                db.execute("INSERT INTO projects VALUES ('legacy','Existing project','2026-01-01')")
            store=Store(root)
            self.assertEqual(len(list((Path(root)/'backups').glob('*.sqlite3'))),1)
            accounts=Accounts(store)
            token=accounts.register({'username':'legacy_owner','display_name':'Legacy user','password':'legacy-password-12345'})
            spaces=accounts.workspaces(accounts.session(token))
            self.assertEqual(store.projects()[0]['workspace_id'],spaces[0]['id'])
            self.assertEqual(store.projects()[0]['name'],'Existing project')

    def test_email_registration_normalization_and_login(self):
        token=self.app.accounts.register({'email':' Research+Lab@Example.COM ','display_name':'Email user','password':'email-password-12345','email_verified':True})
        user=self.app.accounts.session(token)
        self.assertEqual(user['email'],'research+lab@example.com')
        self.assertEqual(user['email_verified'],0)
        logged=self.app.accounts.login({'identifier':'RESEARCH+LAB@EXAMPLE.COM','password':'email-password-12345'},'email-test')
        self.assertEqual(self.app.accounts.session(logged)['id'],user['id'])
        self.assertEqual(self.req('/api/bootstrap',token=token)['user']['email'],'research+lab@example.com')
        self.denied('/api/auth/register',{'email':'research+lab@example.com','display_name':'Duplicate email','password':'email-password-12345'},code=400)

    def test_invalid_email_and_missing_email_are_distinct(self):
        for email in ['not-email','a@@example.com','a..b@example.com','a@localhost','a@-example.com','a@example.com\r\nBcc:evil',None,'']:
            with self.subTest(email=email):
                self.denied('/api/auth/register',{'email':email,'display_name':'Invalid email','password':'email-password-12345'},code=400)
        # Legacy username login remains available after schema upgrade.
        token=self.app.accounts.login({'identifier':'test_admin','password':'test-password-12345'},'legacy-test')
        self.assertEqual(self.app.accounts.session(token)['id'],self.session['id'])

    def test_bind_email_requires_password_and_revokes_sessions(self):
        self.denied('/api/auth/email',{'email':'bound@example.com','current_password':'wrong'},code=403)
        self.req('/api/auth/email',{'email':'bound@example.com','current_password':'test-password-12345'})
        self.assertIsNone(self.app.accounts.session(self.session_token))
        token=self.app.accounts.login({'identifier':'bound@example.com','password':'test-password-12345'},'bound-test')
        self.assertEqual(self.app.accounts.session(token)['id'],self.session['id'])

    def test_binding_cannot_steal_existing_email(self):
        self.app.accounts.register({'email':'existing@example.com','display_name':'Another account','password':'email-password-12345'})
        self.denied('/api/auth/email',{'email':'EXISTING@EXAMPLE.COM','current_password':'test-password-12345'},code=400)
        self.assertIsNotNone(self.app.accounts.session(self.session_token))


if __name__=='__main__':
    unittest.main()
