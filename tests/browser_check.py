"""Optional Chrome DevTools UI check; requires a separately launched test browser.

Usage: python3 tests/browser_check.py
Chrome must expose a debugging port on 127.0.0.1:9226 with a disposable profile.
Uses temporary app data, and never modifies the user's workbench database.
"""
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent


class CDP:
    def __init__(self):
        with urlopen('http://127.0.0.1:9226/json') as response:
            tabs = json.load(response)
        tab = next(t for t in tabs if t['type'] == 'page')
        from urllib.parse import urlsplit
        url = urlsplit(tab['webSocketDebuggerUrl'])
        self.sock = socket.create_connection((url.hostname, url.port), timeout=10)
        self.sock.sendall(('GET %s HTTP/1.1\r\nHost: %s:%s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\nOrigin: http://127.0.0.1:9226\r\n\r\n' %
                          (url.path, url.hostname, url.port, base64.b64encode(os.urandom(16)).decode())).encode())
        header = b''
        while not header.endswith(b'\r\n\r\n'):
            header += self.sock.recv(1)
        if not header.startswith(b'HTTP/1.1 101 '):
            raise RuntimeError(header.decode())
        self.counter = 0
        self.errors = []

    def recv(self, size):
        output = b''
        while len(output) < size:
            part = self.sock.recv(size - len(output))
            if not part:
                raise RuntimeError('Chrome connection closed')
            output += part
        return output

    def call(self, method, params=None):
        self.counter += 1
        payload = json.dumps({'id': self.counter, 'method': method, 'params': params or {}}).encode()
        length = len(payload)
        prefix = bytes([0x81, 0x80 | length]) if length < 126 else bytes([0x81, 0x80 | 126]) + struct.pack('!H', length)
        mask = os.urandom(4)
        self.sock.sendall(prefix + mask + bytes(v ^ mask[i % 4] for i, v in enumerate(payload)))
        while True:
            frame = self.recv(2)
            length = frame[1] & 127
            if length == 126:
                length = struct.unpack('!H', self.recv(2))[0]
            elif length == 127:
                length = struct.unpack('!Q', self.recv(8))[0]
            body = self.recv(length)
            item = json.loads(body)
            if item.get('method') == 'Runtime.exceptionThrown':
                self.errors.append(item['params'])
            if item.get('id') == self.counter:
                if 'error' in item:
                    raise RuntimeError(item['error'])
                return item.get('result', {})

    def evaluate(self, expression):
        result = self.call('Runtime.evaluate', {'expression': expression, 'returnByValue': True, 'awaitPromise': True})
        if result.get('exceptionDetails'):
            raise AssertionError(result['exceptionDetails'])
        return result.get('result', {}).get('value')

    def wait(self, expression, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.evaluate('Boolean(' + expression + ')'):
                return
            time.sleep(.1)
        raise AssertionError('Timed out: ' + expression)

    def screenshot(self, filename):
        content = self.call('Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': True})
        Path(filename).write_bytes(base64.b64decode(content['data']))


def main():
    with tempfile.TemporaryDirectory(prefix='atlas-browser-') as data:
        process = subprocess.Popen([sys.executable, '-m', 'workbench', '--port', '8766', '--data-dir', data], cwd=ROOT,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        browser = None
        try:
            for _ in range(50):
                try:
                    urlopen('http://127.0.0.1:8766/api/bootstrap', timeout=1).close()
                    break
                except OSError:
                    time.sleep(.1)
            browser = CDP()
            browser.call('Runtime.enable')
            browser.call('Page.enable')
            browser.call('Emulation.setDeviceMetricsOverride', {'width':1440,'height':1050,'deviceScaleFactor':1,'mobile':False})
            browser.call('Page.navigate', {'url':'http://127.0.0.1:8766'})
            browser.wait("document.querySelector('#auth-form')")
            browser.screenshot('/tmp/research-atlas-auth.png')
            browser.evaluate("document.querySelector('[name=display_name]').value='Browser Test Admin';document.querySelector('[name=email]').value='browser-admin@example.com';document.querySelector('[name=password]').value='browser-test-password-12345';document.querySelector('[name=confirm_password]').value='browser-test-password-12345';document.querySelector('#auth-form').requestSubmit()")
            browser.wait("document.querySelector('[data-create]') && document.body.innerText.includes('Make research traceable')")
            browser.screenshot('/tmp/research-atlas-desktop.png')
            browser.evaluate("document.querySelector('[data-create=\"cocrystal\"]').click()")
            browser.wait("document.querySelector('#create-dialog').open")
            browser.evaluate("document.querySelector('[name=title]').value='Browser integration check';document.querySelector('#objective').value='Verify the complete cocrystal research workflow without asserting scientific conclusions';document.querySelector('#create-form').requestSubmit()")
            browser.wait("document.querySelector('[data-action=start]')")
            browser.evaluate("document.querySelector('[data-action=start]').click()")
            browser.wait("document.querySelector('[data-action=approve]')")
            browser.evaluate("const n=document.querySelector('#review-note');n.value='Record capability gaps and add experimental data';n.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('[data-action=approve]').click()")
            browser.wait("document.body.innerText.includes('Completed') && document.body.innerText.includes('The research plan was prepared and reviewed')")
            browser.screenshot('/tmp/research-atlas-task.png')
            result = browser.evaluate("fetch('/api/runs').then(r=>r.json()).then(r=>({status:r[0].status,stages:Object.keys(r[0].outputs).length,review:r[0].review.note}))")
            assert result['status'] == 'completed' and result['stages'] == 10
            assert 'add experimental data' in result['review']
            # Check exported record, then next-round prefill and cancellation.
            export_check = browser.evaluate("fetch(document.querySelector('a[href$=\"/report\"]').href).then(r=>r.text()).then(t=>t.includes('add experimental data') && t.includes('No adapter is connected'))")
            assert export_check
            browser.evaluate("document.querySelector('[data-next]').click()")
            browser.wait("document.querySelector('#create-dialog').open")
            assert browser.evaluate("document.querySelector('[name=title]').value.includes('next iteration')")
            browser.evaluate("document.querySelector('#close-dialog').click();location.hash='domains'")
            browser.wait("document.body.innerText.includes('Scientific Tool Registry')")
            browser.evaluate("location.hash='settings'")
            browser.wait("document.body.innerText.includes('Runtime Settings') && document.body.innerText.includes('Not configured')")
            browser.evaluate("location.hash='workspace'")
            browser.wait("document.body.innerText.includes('Make research traceable')")
            browser.call('Emulation.setDeviceMetricsOverride', {'width':390,'height':844,'deviceScaleFactor':1,'mobile':True})
            assert browser.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            browser.screenshot('/tmp/research-atlas-mobile.png')
            browser.call('Emulation.setDeviceMetricsOverride', {'width':1440,'height':1050,'deviceScaleFactor':1,'mobile':False})
            browser.evaluate("location.hash='team'")
            browser.wait("document.querySelector('#invite-form')")
            browser.evaluate("document.querySelector('#invite-form [name=role]').value='viewer';document.querySelector('#invite-form').requestSubmit()")
            browser.wait("document.querySelector('.invite-code')")
            invitation=browser.evaluate("document.querySelector('.invite-code').textContent")
            browser.screenshot('/tmp/research-atlas-team.png')
            browser.evaluate("location.hash='account'")
            browser.wait("document.querySelector('[data-logout]')")
            browser.evaluate("document.querySelector('[data-logout]').click()")
            browser.wait("document.querySelector('#auth-form')")
            browser.evaluate("document.querySelector('[data-auth-mode=register]').click()")
            browser.evaluate("document.querySelector('[name=display_name]').value='Browser Test Member';document.querySelector('[name=email]').value='browser-member@example.com';document.querySelector('[name=password]').value='member-test-password-12345';document.querySelector('[name=confirm_password]').value='member-test-password-12345';document.querySelector('#auth-form').requestSubmit()")
            browser.wait("document.body.innerText.includes('Make research traceable')")
            assert browser.evaluate("fetch('/api/runs').then(r=>r.json()).then(r=>r.length===0)")
            browser.evaluate("location.hash='team'")
            browser.wait("document.querySelector('#join-form')")
            browser.evaluate("document.querySelector('#join-form [name=code]').value="+json.dumps(invitation)+";document.querySelector('#join-form').requestSubmit()")
            browser.wait("document.querySelector('#workspace-role')?.textContent==='Read-only member'")
            browser.wait("document.body.innerText.includes('Browser integration check')")
            assert browser.evaluate("Array.from(document.querySelectorAll('[data-create]')).every(b=>b.hidden)")
            browser.screenshot('/tmp/research-atlas-viewer.png')
            assert not browser.errors, browser.errors
            print(json.dumps({'browser':'passed','accounts':'registration, logout, workspace isolation, invitation, viewer permissions passed','workflow':result,'desktop_screenshot':'/tmp/research-atlas-desktop.png',
                              'task_screenshot':'/tmp/research-atlas-task.png','mobile_screenshot':'/tmp/research-atlas-mobile.png',
                              'javascript_errors':len(browser.errors)}, ensure_ascii=False))
        except Exception:
            if browser:
                print(json.dumps({'page':browser.evaluate('document.body.innerText'), 'errors':browser.errors}, ensure_ascii=False))
                browser.screenshot('/tmp/research-atlas-browser-failure.png')
            raise
        finally:
            if browser:
                browser.sock.close()
            process.terminate()
            process.wait(timeout=5)


if __name__ == '__main__':
    main()
