"""Loopback-only local API and static UI. Run with python3 -m workbench."""
import base64
import binascii
import hashlib
import json
import mimetypes
import os
import secrets
import sqlite3
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit

from .domains import load_packs
from .engine import Conflict, Engine
from .store import Store, now, uid
from .tools import ModelClient, Registry
from .auth import Accounts, AuthError, SESSION_TTL
from .mail import Mailer, MailWorker
from .science.inputs import validate as validate_science

ROOT = Path(__file__).resolve().parent.parent
MAX_BODY = 8 * 1024 * 1024
EXTENSIONS = {".txt", ".md", ".csv", ".tsv", ".json", ".pdb", ".pdbqt", ".cif", ".sdf", ".mol", ".smi", ".pdf"}


def text_field(data, name, limit, required=True):
    value = data.get(name, "")
    if not isinstance(value, str) or len(value.strip()) > limit or (required and not value.strip()):
        raise ValueError("Invalid field: " + name)
    return value.strip()


def report(run):
    lines = ["# " + run["title"], "", "Run ID: " + run["id"], "Domain pack: " + run["pack"]["name"] + " " + run["pack"]["version"],
             "", "This report records calculations, open requirements, and researcher review. Workflow completion does not establish experimental validation or drug discovery.", "", "## Research Objective", run["objective"],
             "", "## Constraints", run["constraints"] or "Not provided", "", "## Research Files"]
    for f in run["files"]:
        lines += ["- " + f["name"] + " · SHA-256: " + f["sha256"] + " (parsed by the relevant scientific stage)"]
    for stage in run["pack"]["stages"]:
        output = run["outputs"].get(stage["id"])
        lines += ["", "## " + stage["name"] + " · " + stage["role"]]
        if output:
            lines += ["Status: " + output["status"], "", output["summary"]]
            tool=output.get('tool_result',{})
            if tool.get('missing'):lines += ['', 'Still needed: '+'; '.join(tool['missing'])]
            if tool.get('artifacts'):lines += ['', 'Artifacts: '+', '.join(tool['artifacts'])]
            if tool.get('evidence'):lines += ['', 'Evidence and sources: '+json.dumps(tool['evidence'],ensure_ascii=False)]
            if output.get("instruction"):
                lines += ["", "Stage instructions: " + output["instruction"]]
            if output.get("advice"):
                lines += ["", "### LLM Advice (not scientifically validated)", output["advice"]]
            if output.get("model_error"):
                lines += ["", output["model_error"]]
        else:
            lines += ["Not run."]
    if run.get("review"):
        lines += ["", "## Researcher Review", run["review"]["note"] or "Research plan approved", run["review"]["at"]]
    return "\n".join(lines)


class Application:
    def __init__(self, data_dir, enable_science=True):
        self.store = Store(data_dir)
        self.accounts = Accounts(self.store)
        self.mailer = Mailer(ROOT)
        self.mail_worker = MailWorker(self.store,self.accounts,self.mailer)
        self.packs = load_packs(ROOT / "domain_packs")
        self.registry, self.model = Registry(), ModelClient()
        from .science.executor import install
        if enable_science:install(self.registry,self.store)
        self.engine = Engine(self.store, self.registry, self.model)
        self.token = secrets.token_urlsafe(32)

    def create_run(self, data, user=None):
        pack_id = text_field(data, "pack_id", 80)
        if pack_id not in self.packs:
            raise ValueError("Domain pack not found")
        project_id = text_field(data, "project_id", 80)
        if project_id not in {p["id"] for p in self.store.projects()}:
            raise ValueError("Project not found")
        use_model = data.get("use_model", False)
        calls = data.get("max_model_calls", 4)
        if not isinstance(use_model, bool) or type(calls) is not int or not 0 <= calls <= 20:
            raise ValueError("Invalid model configuration")
        if use_model and not self.model.configured:
            raise ValueError("Configure the LLM provider in the server environment first")
        return self.store.create({"project_id": project_id, "pack": self.packs[pack_id],
            "title": text_field(data, "title", 120), "objective": text_field(data, "objective", 6000),
            "constraints": text_field(data, "constraints", 6000, False), "use_model": use_model,
            "max_model_calls": calls, "science":validate_science(data.get('science')), "mode": "scientific_workflow", "created_by": self.accounts.public(user) if user else None},
            authorize=(lambda db: self.accounts.require_project(db, user, project_id, 'write')) if user else None)

    def upload(self, run_id, data, user=None):
        name = text_field(data, "name", 180)
        if name != Path(name).name or "\\" in name or any(ord(c) < 32 for c in name):
            raise ValueError("Invalid filename")
        if Path(name).suffix.lower() not in EXTENSIONS:
            raise ValueError("Unsupported file type")
        encoded = data.get("content")
        if not isinstance(encoded, str):
            raise ValueError("Invalid file content")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("Invalid file encoding")
        if not raw or len(raw) > 5 * 1024 * 1024:
            raise ValueError("Files must be between 1 byte and 5 MB")
        file_id = uid()
        folder = self.store.root / "files"
        folder.mkdir(exist_ok=True)
        path = folder / file_id
        def attach(run, db):
            if user:
                self.accounts.require_run(db, user, run_id, 'write')
            if run["status"] != "draft":
                raise Conflict("Files can only be added to a draft. Create a new iteration to add more data.")
            if db.execute("SELECT COUNT(*) FROM files WHERE run_id=?", (run_id,)).fetchone()[0] >= 20:
                raise ValueError("A run can contain at most 20 files")
            path.write_bytes(raw)
            db.execute("INSERT INTO files VALUES (?,?,?,?,?,?)", (file_id, run_id, name, len(raw), hashlib.sha256(raw).hexdigest(), now()))
            self.store.event(db, run_id, "file_added", "Added research file: " + name + " (stored and hash-checked; not yet parsed)")
            if user:
                workspace = self.accounts.require_run(db, user, run_id, 'write')
                self.accounts.audit(db, workspace, user['id'], 'file.upload', file_id)
        try:
            return self.store.mutate(run_id, attach)
        except Exception:
            path.unlink(missing_ok=True)
            raise


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ResearchWorkbench/0.2"

        def log_message(self, fmt, *args):
            # Avoid URLs and user data in routine logs.
            pass

        def send(self, value, status=200, content_type="application/json; charset=utf-8", filename=None, cookie=None):
            body = json.dumps(value, ensure_ascii=False).encode() if content_type.startswith("application/json") else value
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header('Vary', 'Cookie')
            if cookie is not None:
                self.send_header('Set-Cookie', cookie)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if filename:
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(filename))
            self.end_headers()
            self.wfile.write(body)

        def guard(self, mutation=False):
            host = self.headers.get("Host", "")
            port = self.server.server_address[1]
            if host not in ("127.0.0.1:" + str(port), "localhost:" + str(port)):
                self.send({"error": "Local access only"}, 403)
                return False
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + host:
                self.send({"error": "Cross-origin request rejected"}, 403)
                return False
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get('Cookie', ''))
            except CookieError:
                pass
            self.user = app.accounts.session(cookie['atlas_session'].value if 'atlas_session' in cookie else '')
            csrf = self.user['csrf'] if self.user else app.token
            if mutation and not secrets.compare_digest(self.headers.get("X-Workbench-Token", ""), csrf):
                self.send({"error": "Your local session has expired. Refresh the page."}, 403)
                return False
            return True

        def cookie(self, token=''):
            # HTTP loopback during local development; enable Secure for HTTPS deployment.
            secure = '; Secure' if os.environ.get('WORKBENCH_SECURE_COOKIE') == '1' else ''
            return 'atlas_session=' + token + '; Path=/; HttpOnly; SameSite=Strict; Max-Age=' + (str(SESSION_TTL) if token else '0') + secure

        def authenticated(self):
            if not self.user:
                raise AuthError('Please sign in first')
            return self.user

        def workspace(self, permission='read'):
            user = self.authenticated()
            spaces = app.accounts.workspaces(user)
            workspace = self.headers.get('X-Workspace-ID') or (spaces[0]['id'] if spaces else '')
            with app.store.db() as db:
                app.accounts.require_workspace(db, user, workspace, permission)
            return workspace

        def do_GET(self):
            if not self.guard():
                return
            path = urlsplit(self.path).path
            try:
                if path == "/api/bootstrap":
                    if not self.user:
                        with app.store.db() as db:
                            setup = not db.execute('SELECT 1 FROM users LIMIT 1').fetchone()
                        self.send({'token':app.token, 'user':None, 'setup_required':setup,'mail':{'configured':app.mailer.configured}})
                    else:
                        spaces = app.accounts.workspaces(self.user)
                        selected = next((w for w in spaces if w['id'] == self.headers.get('X-Workspace-ID')), spaces[0] if spaces else None)
                        with app.store.db() as db:
                            projects = [dict(r) for r in db.execute('SELECT * FROM projects WHERE workspace_id=? ORDER BY created_at', (selected['id'],))] if selected else []
                            mail_status=db.execute('SELECT kind,status,updated_at FROM mail_jobs WHERE user_id=? ORDER BY created_at DESC LIMIT 1',(self.user['id'],)).fetchone()
                        self.send({"token": self.user['csrf'], 'user':app.accounts.public(self.user), 'workspaces':spaces, 'workspace':selected,
                                   "packs": list(app.packs.values()), "projects": projects,
                                   "capabilities": app.registry.capabilities(), "model": {"configured": app.model.configured, "name": app.model.model},
                                   'mail':{'configured':app.mailer.configured,'last':dict(mail_status) if mail_status else None},
                                   "version": "0.2.0", "mode": "local_research_planning"})
                elif path.startswith('/api/science/example/'):
                    self.authenticated()
                    key=path.rsplit('/',1)[-1]
                    names={'drug_discovery':'drug-discovery.json','cocrystal':'cocrystal.json'}
                    if key not in names:raise AuthError('Example not found',404)
                    self.send(json.loads((ROOT/'examples'/names[key]).read_text()))
                elif path == "/api/runs":
                    workspace = self.workspace()
                    with app.store.db() as db:
                        self.send([app.store.unpack(r) for r in db.execute('SELECT r.* FROM runs r JOIN projects p ON r.project_id=p.id WHERE p.workspace_id=? ORDER BY r.created_at DESC', (workspace,))])
                elif path == '/api/members':
                    self.send(app.accounts.members(self.authenticated(), self.workspace()))
                elif path == '/api/audit':
                    workspace = self.workspace('members')
                    with app.store.db() as db:
                        self.send([dict(r) for r in db.execute('SELECT a.*,u.display_name FROM audit a LEFT JOIN users u ON a.actor_id=u.id WHERE workspace_id=? ORDER BY seq DESC LIMIT 100', (workspace,))])
                elif path.startswith('/api/files/'):
                    file_id = path.split('/')[-1]
                    with app.store.db() as db:
                        record = db.execute('SELECT * FROM files WHERE id=?', (file_id,)).fetchone()
                        self.authenticated()
                        if not record:
                            raise AuthError('Resource not found or access denied',404)
                        app.accounts.require_run(db, self.user, record['run_id'])
                    self.send((app.store.root/'files'/file_id).read_bytes(), content_type='application/octet-stream', filename=record['name'])
                elif path.startswith("/api/runs/"):
                    parts = path.strip("/").split("/")
                    with app.store.db() as db:
                        app.accounts.require_run(db, self.authenticated(), parts[2])
                    run = app.store.get(parts[2])
                    if len(parts) == 3:
                        self.send(run)
                    elif len(parts) == 4 and parts[3] == "report":
                        self.send(report(run).encode(), content_type="text/markdown; charset=utf-8", filename="research-" + run["id"][:8] + ".md")
                    elif len(parts) == 4 and parts[3] == "export":
                        self.send(json.dumps(run, ensure_ascii=False, indent=2).encode(), content_type="application/octet-stream", filename="research-" + run["id"][:8] + ".json")
                    elif len(parts)==5 and parts[3]=='artifacts':
                        name=parts[4]
                        import re
                        if not re.fullmatch(r'[A-Za-z0-9_-]+\.(json|csv|sdf|pdb|pdbqt)',name):
                            raise AuthError('Artifact not found',404)
                        folder=app.store.root/'artifacts'/run['id']
                        path=folder/name
                        allowed={a for o in run['outputs'].values() for a in o.get('tool_result',{}).get('artifacts',[])}
                        if name not in allowed or not path.is_file():raise AuthError('Artifact not found',404)
                        self.send(path.read_bytes(),content_type='application/octet-stream',filename=name)
                    else:
                        self.send({"error": "Resource not found"}, 404)
                elif path in ("/", "/app.js", "/style.css", "/auth.css"):
                    file = ROOT / "web" / ("index.html" if path == "/" else path[1:])
                    kind = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}[file.suffix]
                    self.send(file.read_bytes(), content_type=kind + "; charset=utf-8")
                elif path == "/favicon.ico":
                    self.send(b"", 204, "image/x-icon")
                else:
                    self.send({"error": "Resource not found"}, 404)
            except AuthError as e:
                self.send({'error':str(e)}, e.status)
            except KeyError:
                self.send({"error": "Run not found"}, 404)
            except Exception:
                self.send({"error": "Unable to read the requested resource"}, 500)

        def do_POST(self):
            if not self.guard(True):
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY:
                    self.send({"error": "Request is empty or too large"}, 413)
                    return
                if self.headers.get_content_type() != "application/json":
                    self.send({"error": "A JSON request is required"}, 415)
                    return
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("Request body must be a JSON object")
                parts = urlsplit(self.path).path.strip("/").split("/")
                if parts in (["api", "auth", "register"], ["api", "auth", "login"]):
                    if parts[2] == 'register':
                        app.accounts.limited('register:' + self.client_address[0], 20)
                        token = app.accounts.register(data)
                    else:
                        token = app.accounts.login(data, self.client_address[0])
                    self.send({'ok':True}, cookie=self.cookie(token))
                elif parts == ['api','auth','logout']:
                    user = self.authenticated()
                    with app.store.db() as db:
                        db.execute('DELETE FROM sessions WHERE token_hash=?', (user['token_hash'],))
                    self.send({'ok':True}, cookie=self.cookie())
                elif parts == ['api','auth','password']:
                    self.authenticated()
                    app.accounts.limited('password:' + self.user['id'])
                    app.accounts.change_password(self.user, data)
                    self.send({'ok':True}, cookie=self.cookie())
                elif parts == ['api','auth','email']:
                    self.authenticated()
                    app.accounts.limited('email:' + self.user['id'])
                    app.accounts.change_email(self.user,data)
                    self.send({'ok':True}, cookie=self.cookie())
                elif parts == ['api','auth','request-verification']:
                    user=self.authenticated()
                    if not user['email']:
                        raise ValueError('Add an email address first')
                    self.send(app.mail_worker.queue(user['email'],'verify',user,self.client_address[0]),202)
                elif parts == ['api','auth','verify-email']:
                    user=self.authenticated()
                    app.accounts.limited('verify:'+user['id'])
                    app.accounts.consume_email_token(data.get('token'),'verify',user,data.get('current_password'))
                    self.send({'ok':True})
                elif parts == ['api','auth','forgot-password']:
                    email=app.accounts.email(data.get('email'))
                    self.send(app.mail_worker.queue(email,'reset',ip=self.client_address[0]),202)
                elif parts == ['api','auth','reset-password']:
                    app.accounts.limited('reset-ip:'+self.client_address[0],20)
                    app.accounts.consume_email_token(data.get('token'),'reset',password=data.get('new_password'))
                    self.send({'ok':True},cookie=self.cookie())
                elif parts == ['api','workspaces']:
                    user = self.authenticated()
                    workspace = uid()
                    with app.store.db() as db:
                        db.execute('BEGIN IMMEDIATE')
                        db.execute('INSERT INTO workspaces VALUES (?,?,?)', (workspace,text_field(data,'name',100),now()))
                        db.execute('INSERT INTO memberships VALUES (?,?,?)', (workspace,user['id'],'admin'))
                        app.accounts.audit(db,workspace,user['id'],'workspace.created',workspace)
                    self.send({'workspace_id':workspace},201)
                elif parts == ['api','invitations']:
                    self.send(app.accounts.invite(self.authenticated(),self.workspace('members'),data.get('role')),201)
                elif parts == ['api','invitations','accept']:
                    self.authenticated()
                    app.accounts.limited('join:' + self.user['id'],20)
                    self.send(app.accounts.join(self.user,data.get('code')))
                elif len(parts) == 3 and parts[:2] == ['api','members']:
                    workspace = self.workspace('members')
                    if data.get('action') == 'remove':
                        role = None
                    else:
                        role = text_field(data,'role',30)
                    app.accounts.update_member(self.authenticated(),workspace,parts[2],role)
                    self.send({'ok':True})
                elif parts == ["api", "projects"]:
                    workspace = self.workspace('write')
                    self.send(app.store.project(text_field(data, "name", 100),workspace,
                        authorize=lambda db: app.accounts.require_workspace(db,self.user,workspace,'write')), 201)
                elif parts == ["api", "runs"]:
                    self.send(app.create_run(data,self.authenticated()), 201)
                elif len(parts) == 4 and parts[:2] == ["api", "runs"]:
                    self.authenticated()
                    permission = 'review' if parts[3] == 'approve' else 'write'
                    with app.store.db() as db:
                        app.accounts.require_run(db,self.user,parts[2],permission)
                    if parts[3] == "files":
                        self.send(app.upload(parts[2], data,self.user), 201)
                    elif parts[3] in ("start", "approve", "cancel"):
                        self.send(app.engine.action(parts[2], parts[3], text_field(data, "note", 6000, False),
                            authorize=lambda db: app.accounts.require_run(db,self.user,parts[2],permission),actor=app.accounts.public(self.user)))
                    else:
                        self.send({"error": "Unknown action"}, 404)
                else:
                    self.send({"error": "Unknown endpoint"}, 404)
            except AuthError as e:
                self.send({'error':str(e)}, e.status)
            except Conflict as e:
                self.send({"error": str(e)}, 409)
            except KeyError:
                self.send({"error": "Run not found"}, 404)
            except (ValueError, TypeError, UnicodeError) as e:
                self.send({"error": str(e)[:200]}, 400)
            except Exception:
                self.send({"error": "Unable to save. Please try again."}, 500)
    return Handler


def serve(port=8765, data_dir=None):
    app = Application(data_dir or os.environ.get("WORKBENCH_DATA_DIR", str(ROOT / "data")))
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    app.engine.start()
    app.mail_worker.start()
    print("Research Atlas: http://127.0.0.1:%s" % server.server_address[1], flush=True)
    print("Local mode · Data directory: %s · Press Ctrl+C to stop" % app.store.root, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.engine.stop()
        app.mail_worker.stop()
