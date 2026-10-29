import base64
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from workbench.engine import Conflict
from workbench.server import Application, make_handler, report
from workbench.tools import Registry


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LLM_BASE_URL": "", "LLM_MODEL": "", "LLM_API_KEY": "",'SMTP_HOST':'','SMTP_USERNAME':'','SMTP_PASSWORD':'','MAIL_FROM':''})
        self.env.start()
        self.app = Application(self.tmp.name,enable_science=False)

    def tearDown(self):
        self.app.engine.stop()
        self.env.stop()
        self.tmp.cleanup()

    def create(self, pack="drug_discovery", **values):
        payload = {"project_id": self.app.store.projects()[0]["id"], "pack_id": pack,
                   "title": "Test research", "objective": "Test target and research constraints", "constraints": "No experimental data supplied"}
        payload.update(values)
        return self.app.create_run(payload)

    def to_review(self, run):
        self.app.engine.action(run["id"], "start")
        for _ in range(20):
            current = self.app.store.get(run["id"])
            if current["status"] == "awaiting_review":
                return current
            self.app.engine.advance(run["id"])
        self.fail("Workflow did not reach researcher review")

    def test_both_complete_flows_require_review(self):
        for pack in self.app.packs:
            with self.subTest(pack=pack):
                run = self.to_review(self.create(pack))
                self.assertIsNone(run["review"])
                self.assertTrue(any(o["status"] == "unavailable" for o in run["outputs"].values()))
                self.app.engine.advance(run["id"])
                self.assertEqual(self.app.store.get(run["id"])["status"], "awaiting_review")
                self.app.engine.action(run["id"], "approve", "Add experimental data and validation engines")
                self.app.engine.advance(run["id"])
                final = self.app.store.get(run["id"])
                self.assertEqual(final["status"], "completed")
                self.assertEqual(final["step"], len(final["pack"]["stages"]))
                self.assertIn("Scientific computation and experimental validation remain outstanding", final["outputs"]["report"]["summary"])
                self.assertIn("Add experimental data and validation engines", report(final))

    def test_invalid_approval_and_double_start(self):
        run = self.create()
        with self.assertRaises(Conflict):
            self.app.engine.action(run["id"], "approve")
        self.app.engine.action(run["id"], "start")
        with self.assertRaises(Conflict):
            self.app.engine.action(run["id"], "start")

    def test_cancel_never_resumes(self):
        run = self.create()
        self.app.engine.action(run["id"], "start")
        self.app.engine.action(run["id"], "cancel")
        self.app.engine.advance(run["id"])
        self.assertEqual(self.app.store.get(run["id"])["outputs"], {})

    def test_cancel_during_model_discards_output(self):
        self.app.model.base, self.app.model.model = "http://unused", "test-model"
        run = self.create(use_model=True)
        self.app.engine.action(run["id"], "start")
        def cancel(*_):
            self.app.engine.action(run["id"], "cancel")
            return {"text": "should not persist", "usage": {}}
        self.app.model.advise = cancel
        self.app.engine.advance(run["id"])
        current = self.app.store.get(run["id"])
        self.assertEqual(current["status"], "cancelled")
        self.assertEqual(current["outputs"], {})

    def test_restart_preserves_checkpoint_and_recovery(self):
        run = self.create()
        self.app.engine.action(run["id"], "start")
        self.app.engine.advance(run["id"])
        self.app.store.mutate(run["id"], lambda item, db: item.update(status="running"))
        reopened = Application(self.tmp.name)
        reopened.store.recover()
        recovered = reopened.store.get(run["id"])
        self.assertEqual(recovered["step"], 1)
        self.assertIn("scope", recovered["outputs"])
        self.assertEqual(recovered["status"], "queued")
        reopened.engine.advance(run["id"])
        self.assertEqual(reopened.store.get(run["id"])["step"], 2)

    def test_domain_snapshot_is_immutable(self):
        run = self.create()
        self.app.packs["drug_discovery"]["stages"][0]["name"] = "changed"
        self.assertNotEqual(self.app.store.get(run["id"])["pack"]["stages"][0]["name"], "changed")

    def test_upload_integrity_and_draft_only(self):
        run = self.create()
        uploaded = self.app.upload(run["id"], {"name": "example.smi", "content": base64.b64encode(b"CCO").decode()})
        self.assertEqual(uploaded["files"][0]["size"], 3)
        self.app.engine.action(run["id"], "start")
        self.app.engine.advance(run["id"])
        current = self.app.store.get(run["id"])
        self.assertTrue(current["outputs"]["scope"]["attachments"][0]["integrity"])
        with self.assertRaises(Conflict):
            self.app.upload(run["id"], {"name":"more.smi", "content":"Q0NP"})

    def test_upload_rejects_traversal_and_bad_encoding(self):
        run = self.create()
        for name, content in [("../x.txt", "YQ=="), ("x.py", "YQ=="), ("x.txt", "!!!"), ("x.txt", "")]:
            with self.subTest(name=name, content=content), self.assertRaises(ValueError):
                self.app.upload(run["id"], {"name":name,"content":content})

    def test_model_attempt_limit_includes_failure(self):
        self.app.model.base, self.app.model.model = "http://unused", "test-model"
        self.app.model.advise = lambda *_: (_ for _ in ()).throw(TimeoutError())
        run = self.to_review(self.create(use_model=True, max_model_calls=1))
        self.assertEqual(run["model_attempts"], 1)
        self.assertEqual(run["model_calls"], 0)
        self.assertIn("model_error", run["outputs"]["scope"])

    def test_registered_engine_result_preserved(self):
        self.app.registry.register("structure.prepare", lambda run, files: {"status":"predicted", "summary":"fixture calculation", "evidence":[{"source":"test"}], "version":"fixture-1"})
        run = self.to_review(self.create())
        self.assertEqual(run["outputs"]["structure"]["tool_result"]["version"], "fixture-1")

    def test_creation_validation(self):
        for payload in ({"title":""},{"pack_id":"missing"},{"project_id":"missing"},{"max_model_calls":100},{"use_model":True}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.create(**payload)


class APITests(unittest.TestCase):
    def setUp(self):
        WorkflowTests.setUp(self)
        self.session_token = self.app.accounts.register({'username':'test_admin','display_name':'Test Admin','password':'test-password-12345'})
        self.session = self.app.accounts.session(self.session_token)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join()
        WorkflowTests.tearDown(self)

    def request(self, path, data=None, headers=None):
        body = None if data is None else json.dumps(data).encode()
        actual = {"Content-Type":"application/json", "X-Workbench-Token":self.session['csrf'], 'Cookie':'atlas_session='+self.session_token}
        actual.update(headers or {})
        return urlopen(Request(self.base+path,body,actual), timeout=5)

    def test_http_create_run_and_export(self):
        with self.request("/api/bootstrap") as response:
            bootstrap = json.load(response)
            self.assertNotIn("key", bootstrap["model"])
        with self.request("/api/runs", {"title":"HTTP research", "objective":"target", "pack_id":"cocrystal", "project_id":bootstrap["projects"][0]["id"]}) as response:
            self.assertEqual(response.status, 201)
            run = json.load(response)
        with self.request("/api/runs/"+run["id"]+"/report") as response:
            self.assertIn("attachment", response.headers["Content-Disposition"])
            self.assertIn("HTTP research", response.read().decode())

    def test_csrf_host_and_origin_rejected(self):
        for headers in ({"X-Workbench-Token":"bad"},{"Origin":"https://evil.example"},{"Host":"evil.example"}):
            with self.subTest(headers=headers), self.assertRaises(HTTPError) as caught:
                self.request("/api/projects", {"name":"bad"}, headers)
            self.assertEqual(caught.exception.code, 403)

    def test_static_and_missing_task(self):
        with self.request("/") as response:
            self.assertIn("Research Atlas", response.read().decode())
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        with self.assertRaises(HTTPError) as caught:
            self.request("/api/runs/missing")
        self.assertEqual(caught.exception.code, 404)

    def test_model_provider_handoff_and_usage(self):
        requests = []
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                body = json.dumps({'choices':[{'message':{'content':'Test model advice; not a scientific conclusion'}}],
                                   'usage':{'prompt_tokens':25,'completion_tokens':10}}).encode()
                self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        provider = ThreadingHTTPServer(('127.0.0.1',0), Provider)
        worker = threading.Thread(target=provider.serve_forever,daemon=True)
        worker.start()
        try:
            self.app.model.base = 'http://127.0.0.1:%s/v1' % provider.server_address[1]
            self.app.model.model = 'fixture-model'
            run = WorkflowTests.create(self,use_model=True,max_model_calls=2)
            self.app.engine.action(run['id'],'start')
            self.app.engine.advance(run['id'])
            self.app.engine.advance(run['id'])
            current = self.app.store.get(run['id'])
            self.assertEqual(current['model_calls'],2)
            self.assertEqual(current['outputs']['structure']['model_usage']['completion_tokens'],10)
            second_context = json.loads(requests[1]['messages'][1]['content'])
            self.assertEqual(len(second_context['prior_outputs']),1)
            self.assertIn('Test model advice',second_context['prior_outputs'][0]['advice'])
        finally:
            provider.shutdown()
            provider.server_close()
            worker.join()


if __name__ == "__main__":
    unittest.main()
