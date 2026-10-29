"""Single local worker with persistent, bounded role handoffs."""
import threading
import time
from .store import now
from .tools import inspect_file


class Conflict(ValueError):
    pass


class Engine:
    def __init__(self, store, registry, model):
        self.store, self.registry, self.model = store, registry, model
        self.stopping = threading.Event()
        self.thread = None

    def start(self):
        self.store.recover()
        self.thread = threading.Thread(target=self.loop, daemon=True, name="research-worker")
        self.thread.start()

    def stop(self):
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=2)

    def action(self, run_id, action, note="", authorize=None, actor=None):
        def change(run, db):
            if authorize:
                authorize(db)
            status = run["status"]
            if action == "start" and status == "draft":
                run["status"] = "queued"
                self.store.event(db, run_id, "queued", "Added to the local workflow queue.")
            elif action == "approve" and status == "awaiting_review":
                run["review"] = {"decision": "approved", "note": note, "at": now(), "actor": actor}
                run["outputs"][run["pack"]["stages"][run["step"]]["id"]] = {
                    "role": "Researcher", "name": "Researcher review", "status": "reviewed", "summary": note or "The research plan was reviewed.", "at": now()}
                run["step"] += 1
                run["status"] = "queued"
                self.store.event(db, run_id, "approved", "The researcher approved the plan. This does not trigger wet-lab work or any unconfigured calculations.")
            elif action == "cancel" and status in ("draft", "queued", "running", "awaiting_review"):
                run["status"] = "cancelled"
                self.store.event(db, run_id, "cancelled", "The run was cancelled. External requests already sent may still incur charges; their results will not be saved to this run.")
            else:
                raise Conflict("This action is not available for the run's current status. Refresh and try again.")
            if actor:
                self.store.event(db, run_id, 'actor', actor['display_name'] + ' · ' + action)
                db.execute('INSERT INTO audit(workspace_id,actor_id,action,subject_id,created_at) SELECT workspace_id,?,?,?,? FROM projects WHERE id=?',
                           (actor['id'], 'run.' + action, run_id, now(), run['project_id']))
        return self.store.mutate(run_id, change)

    def loop(self):
        while not self.stopping.is_set():
            queued = [r for r in self.store.runs() if r["status"] == "queued"]
            if not queued:
                self.stopping.wait(0.3)
                continue
            for run in queued:
                if self.stopping.is_set():
                    return
                try:
                    self.advance(run["id"])
                except Exception:
                    def fail(item, db):
                        if item["status"] in ("queued", "running"):
                            item["status"] = "failed"
                            self.store.event(db, item["id"], "error", "A workflow stage failed. Check the inputs and adapters. Sensitive error details were not logged.")
                    self.store.mutate(run["id"], fail)

    def advance(self, run_id):
        claimed = []
        def claim(run, db):
            if run["status"] != "queued":
                return
            stage = run["pack"]["stages"][run["step"]]
            if stage.get("kind") == "approval":
                run["status"] = "awaiting_review"
                self.store.event(db, run_id, "review", "Waiting for the researcher to review stage outputs and open requirements before approving the report.")
                return
            run["status"] = "running"
            call_model = run["use_model"] and self.model.configured and run["model_attempts"] < run["max_model_calls"]
            if call_model:
                # Reserve before network request, including attempts interrupted by restart.
                run["model_attempts"] += 1
            claimed.append((stage, call_model))
            self.store.event(db, run_id, "stage_started", stage["role"] + " · " + stage["name"])
        run = self.store.mutate(run_id, claim)
        if not claimed:
            return
        stage, call_model = claimed[0]
        files = run["files"]
        tool = self.registry.execute(stage["tool"], run, files)
        result = {"name": stage["name"], "role": stage["role"], "status": tool["status"], "summary": tool["summary"],
                  "instruction": stage["instruction"], "tool": stage["tool"], "tool_result": tool,
                  "at": now(), "mode": "local_rules", "advice": "", "model_usage": None}
        if not stage['tool'].startswith('research.'):result['mode']='scientific'
        if stage["id"] == "scope":
            result["summary"] = "Research objective: " + run["objective"] + "\nConstraints: " + (run["constraints"] or "Not provided")
            result["attachments"] = [inspect_file(self.store.root, f) for f in files]
        if stage["id"] == "critic":
            missing = [o['name']+': '+('; '.join(o.get('tool_result',{}).get('missing',[])) or o['summary']) for o in run['outputs'].values() if o['status'] in ('unavailable','needs_input','needs_dependency','invalid_input','failed','partial','timeout')]
            result['summary']='Results and open requirements from this run:\n'+'\n'.join(missing)+'\nCalculations do not establish clinical activity, synthetic feasibility, or a new solid phase.'
        if stage["id"] == "report":
            done=[o['name'] for o in run['outputs'].values() if o.get('mode')=='scientific' and o['status'] in ('computed','partial')]
            result['summary']=('Scientific stages executed: '+', '.join(done)+'. See each stage for artifacts and open requirements. Experimental validation is still outstanding.') if done else 'The research plan was prepared and reviewed. Scientific computation and experimental validation remain outstanding; provide the inputs or dependencies required for this run.'
        if call_model:
            try:
                answer = self.model.advise(stage, run, files)
                result.update(advice=answer["text"], mode="model_advisory", model_usage=answer["usage"], model=answer["model"])
            except Exception:
                result["model_error"] = "The LLM request failed or timed out. The local result was retained; this attempt counts toward the request limit and may still incur provider charges."
        def finish(item, db):
            if item["status"] != "running":
                return
            item["outputs"][stage["id"]] = result
            if result["mode"] == "model_advisory":
                item["model_calls"] += 1
            item["step"] += 1
            item["status"] = "completed" if item["step"] == len(item["pack"]["stages"]) else "queued"
            self.store.event(db, run_id, "stage_finished", stage["name"] + ": " + tool['status'])
        self.store.mutate(run_id, finish)
