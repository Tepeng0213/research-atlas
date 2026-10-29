import functools
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]


class Executor:
    def __init__(self,store):
        self.store=store
        local=ROOT/'.venv'/'bin'/'python'
        self.python=str(local) if local.exists() else sys.executable

    def ready(self):
        return bool(list((ROOT/'.venv'/'lib').glob('python*/site-packages/rdkit'))) or importlib.util.find_spec('rdkit') is not None

    def metadata(self,name):
        ready=self.ready()
        binary=(ROOT/'.runtime'/'bin'/'vina').exists()
        assets=ROOT/'data'/'models'/'retrosynthesis'
        retro_ready=all((assets/name).exists() for name in ('uspto_model.onnx','uspto_filter_model.onnx','uspto_templates.csv.gz','zinc_stock.hdf5'))
        detail={
            'structure.prepare':'BioPython PDB/mmCIF parsing, binding-site center, and optional RCSB retrieval',
            'molecule.design':'RDKit compound processing, fluorine enumeration, and optional ChEMBL activity lookup',
            'molecule.evaluate':'RDKit descriptors, Meeko ligand preparation, and AutoDock Vina; ADMET model not configured',
            'synthesis.plan':'AiZynthFinder, ONNX, USPTO, and historical ZINC stock; conditions and live purchasing are not guaranteed',
            'cocrystal.pair':'Molecular structures and dose-to-stoichiometry comparison; pharmacology and PK evidence are still required',
            'cocrystal.concept':'RDKit hydrogen-bond complementarity and empirical analysis of supplied pKa values',
            'cocrystal.screen':'Paired molecular features and supplied quantum-chemical interaction energies; ONNX model hook requires a licensed model',
            'cocrystal.thermal':'DSC/TGA processing and an ideal binary solid-liquid baseline',
            'cocrystal.prepare':'Stoichiometric mass fractions and control-based experiment outline; not a validated SOP',
            'cocrystal.evaluate':'PXRD peak extraction and non-negative reference-pattern fitting; phase identity is not automatically confirmed'}[name]
        modules={
            'structure.prepare':('Bio',),
            'molecule.design':('rdkit',),
            'molecule.evaluate':('rdkit','meeko'),
            'synthesis.plan':('rdkit','aizynthfinder'),
            'cocrystal.pair':('rdkit',),
            'cocrystal.concept':('rdkit',),
            'cocrystal.screen':('rdkit','scipy'),
            'cocrystal.thermal':('scipy',),
            'cocrystal.prepare':('rdkit',),
            'cocrystal.evaluate':('scipy',),
        }
        dependencies_ok=True
        for module in modules[name]:
            try:dependencies_ok=dependencies_ok and importlib.util.find_spec(module) is not None
            except (ImportError,ModuleNotFoundError):dependencies_ok=False
        state='needs_dependency' if not dependencies_ok else 'ready'
        if name in ('molecule.design','molecule.evaluate','synthesis.plan','cocrystal.pair','cocrystal.screen','cocrystal.thermal','cocrystal.prepare','cocrystal.evaluate'):
            state='partial' if dependencies_ok else 'needs_dependency'
        if name=='molecule.evaluate' and not binary:state='partial' if dependencies_ok else 'needs_dependency'
        if name=='synthesis.plan' and not retro_ready:state='partial' if dependencies_ok else 'needs_dependency'
        return {'available':dependencies_ok,'state':state,'details':detail}

    @staticmethod
    def terminate(process):
        try:os.killpg(process.pid,signal.SIGTERM)
        except ProcessLookupError:pass
        try:process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            process.wait()

    def execute(self,name,run,files):
        folder=self.store.root/'artifacts'/run['id']
        folder.mkdir(parents=True,exist_ok=True)
        stem=name.replace('.','-')
        source=folder/(stem+'-input.json');target=folder/(stem+'-result.json')
        # No SMTP/model credentials or raw session state are passed to workers.
        source.write_text(json.dumps({'root':str(self.store.root.resolve()),'tool':name,'run':run,'files':files},ensure_ascii=False))
        environment={key:value for key,value in os.environ.items() if key in ('PATH','LANG','LC_ALL','TMPDIR','SYSTEMROOT')}
        environment.update({'OMP_NUM_THREADS':'1','OPENBLAS_NUM_THREADS':'1','MKL_NUM_THREADS':'1','NUMBA_NUM_THREADS':'1','PYTHONDONTWRITEBYTECODE':'1',
                            'MPLCONFIGDIR':str(self.store.root.resolve()/'matplotlib-cache')})
        with (folder/(stem+'-log.txt')).open('w') as logfile:
            process=subprocess.Popen([self.python,'-B','-m','workbench.science.runner',str(source.resolve()),str(target.resolve())],cwd=ROOT,env=environment,
                                     stdout=logfile,stderr=logfile,start_new_session=True)
            deadline=time.monotonic()+240
            while process.poll() is None:
                if self.store.get(run['id'])['status']=='cancelled':
                    self.terminate(process)
                    return {'status':'cancelled','summary':'The researcher cancelled the run; the scientific subprocess was terminated.','evidence':[],'version':'atlas-science-0.3'}
                if time.monotonic()>deadline:
                    self.terminate(process)
                    return {'status':'timeout','summary':'The scientific stage exceeded the 240-second limit and was terminated.','evidence':[],'version':'atlas-science-0.3'}
                time.sleep(.2)
        if process.returncode!=0 or not target.exists():
            return {'status':'failed','summary':'The scientific subprocess exited unexpectedly and produced no valid result.','evidence':[],'version':'atlas-science-0.3'}
        result=json.loads(target.read_text())
        import hashlib
        result['execution']={'input_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'seed':2026,'cpu_limit_hint':1,'timeout_seconds':240}
        result.setdefault('artifacts',[]).append(target.name)
        return result


def install(registry,store):
    from ..tools import SCIENTIFIC
    executor=Executor(store)
    for name in SCIENTIFIC:
        registry.register(name,functools.partial(executor.execute,name),metadata=lambda name=name:executor.metadata(name))
