"""Browser acceptance with an isolated in-memory mail transport. No real email."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from browser_check import CDP


def fixture(root):
    from http.server import ThreadingHTTPServer
    from workbench.server import Application,make_handler
    app=Application(root)
    class Sink:
        configured=True
        def send(self,email,kind,token):
            with (Path(root)/'mail-fixture.jsonl').open('a') as output:
                output.write(json.dumps({'email':email,'kind':kind,'token':token})+'\n')
    app.mailer=Sink()
    app.mail_worker.mailer=app.mailer
    app.engine.start()
    app.mail_worker.start()
    ThreadingHTTPServer(('127.0.0.1',8766),make_handler(app)).serve_forever()


def main():
    with tempfile.TemporaryDirectory(prefix='atlas-mail-browser-') as root:
        environment=dict(os.environ,SMTP_HOST='',SMTP_USERNAME='',SMTP_PASSWORD='',MAIL_FROM='')
        process=subprocess.Popen([sys.executable,__file__,'--serve',root],cwd=ROOT,env=environment,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        browser=None
        try:
            for _ in range(80):
                try:
                    urlopen('http://127.0.0.1:8766/api/bootstrap',timeout=1).close()
                    break
                except OSError:
                    time.sleep(.1)
            browser=CDP()
            browser.call('Runtime.enable')
            browser.call('Emulation.setDeviceMetricsOverride',{'width':1280,'height':1000,'deviceScaleFactor':1,'mobile':False})
            browser.call('Page.navigate',{'url':'http://127.0.0.1:8766/'})
            browser.wait("document.querySelector('#auth-form [name=email]')")
            browser.evaluate("document.querySelector('[name=display_name]').value='Mail flow test';document.querySelector('[name=email]').value='mail-browser@example.com';document.querySelector('[name=password]').value='original-password-12345';document.querySelector('[name=confirm_password]').value='original-password-12345';document.querySelector('#auth-form').requestSubmit()")
            browser.wait("document.body.innerText.includes('Make research traceable')")
            browser.evaluate("location.hash='account'")
            browser.wait("document.querySelector('[data-request-verification]')")
            browser.evaluate("document.querySelector('[data-request-verification]').click()")
            def delivered(kind):
                end=time.monotonic()+8
                while time.monotonic()<end:
                    path=Path(root)/'mail-fixture.jsonl'
                    if path.exists():
                        matches=[json.loads(line) for line in path.read_text().splitlines() if json.loads(line)['kind']==kind]
                        if matches:
                            return matches[-1]['token']
                    time.sleep(.1)
                raise AssertionError('Test mail was not queued')
            token=delivered('verify')
            browser.call('Page.navigate',{'url':'http://127.0.0.1:8766/#verify-email/'+token})
            browser.wait("document.querySelector('#verify-form')")
            browser.screenshot('/tmp/atlas-email-confirm.png')
            browser.evaluate("document.querySelector('#verify-form [name=current_password]').value='original-password-12345';document.querySelector('#verify-form').requestSubmit()")
            browser.wait("document.body.innerText.includes('Email verified')")
            browser.evaluate("document.querySelector('[data-logout]').click()")
            browser.wait("document.querySelector('[data-forgot]')")
            browser.evaluate("document.querySelector('[data-forgot]').click()")
            browser.wait("document.querySelector('#forgot-form')")
            browser.evaluate("document.querySelector('#forgot-form [name=email]').value='mail-browser@example.com';document.querySelector('#forgot-form').requestSubmit()")
            token=delivered('reset')
            browser.call('Page.navigate',{'url':'http://127.0.0.1:8766/#reset-password/'+token})
            browser.wait("document.querySelector('#reset-form')")
            browser.screenshot('/tmp/atlas-password-reset.png')
            browser.evaluate("document.querySelector('#reset-form [name=new_password]').value='recovered-password-12345';document.querySelector('#reset-form [name=confirm_password]').value='recovered-password-12345';document.querySelector('#reset-form').requestSubmit()")
            browser.wait("document.querySelector('#auth-form [name=identifier]')")
            browser.evaluate("document.querySelector('[name=identifier]').value='mail-browser@example.com';document.querySelector('[name=password]').value='recovered-password-12345';document.querySelector('#auth-form').requestSubmit()")
            browser.wait("document.body.innerText.includes('Make research traceable')")
            assert not browser.errors,browser.errors
            print(json.dumps({'browser_mail_flow':'passed','checks':['request verification','confirm with password','request reset','set new password','login with new password'],'real_emails_sent':0}))
        except Exception:
            if browser:
                print(json.dumps({'page':browser.evaluate('document.body.innerText'),'errors':browser.errors},ensure_ascii=False))
            raise
        finally:
            if browser:
                browser.sock.close()
            process.terminate()
            process.wait(timeout=5)


if __name__=='__main__':
    if '--serve' in sys.argv:
        fixture(sys.argv[-1])
    else:
        main()
