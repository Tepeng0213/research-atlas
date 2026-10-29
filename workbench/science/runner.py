"""Bounded subprocess entry point. No shell, pickle, or uploaded Python execution."""
import json
from pathlib import Path
import sys
from .inputs import Inputs


def main():
    job=json.loads(Path(sys.argv[1]).read_text())
    destination=Path(sys.argv[2])
    try:
        import random,numpy as np
        random.seed(2026);np.random.seed(2026)
        from .chemistry import structure,design,evaluate,synthesis
        from .solids import pair,concept,screen,thermal,preparation,evaluation
        methods={'structure.prepare':structure,'molecule.design':design,'molecule.evaluate':evaluate,'synthesis.plan':synthesis,
                 'cocrystal.pair':pair,'cocrystal.concept':concept,'cocrystal.screen':screen,'cocrystal.thermal':thermal,
                 'cocrystal.prepare':preparation,'cocrystal.evaluate':evaluation}
        ctx=Inputs(job['root'],job['run'],job['files'])
        result=methods[job['tool']](ctx)
    except ImportError as error:
        result={'status':'needs_dependency','summary':'Scientific dependency is unavailable: '+str(error.name),'missing':['Install requirements-science.txt'],
                'evidence':[],'version':'atlas-science-0.3'}
    except ValueError as error:
        result={'status':'invalid_input','summary':str(error)[:400],'evidence':[],'version':'atlas-science-0.3'}
    except Exception as error:
        import traceback
        traceback.print_exc()
        result={'status':'failed','summary':'Scientific calculation did not complete: '+type(error).__name__+'. The failure was not reported as success.','evidence':[],'version':'atlas-science-0.3'}
    destination.write_text(json.dumps(result,ensure_ascii=False,allow_nan=False,default=lambda value:value.item() if hasattr(value,'item') else str(value)))

if __name__=='__main__':main()
