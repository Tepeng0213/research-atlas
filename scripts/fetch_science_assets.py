"""Download official AiZynthFinder public assets; never uses account credentials."""
import hashlib
import json
from pathlib import Path
import urllib.request

ROOT=Path(__file__).resolve().parent.parent
ASSETS={
    'uspto_model.onnx':('https://zenodo.org/api/records/7797465/files/uspto_model.onnx/content','b466a436b6a8eceaba8c14ef5e2a88c8','CC-BY-4.0'),
    'uspto_filter_model.onnx':('https://zenodo.org/api/records/7797465/files/uspto_filter_model.onnx/content','ab0df54b62a0aeceecd68051f8dc3f2e','CC-BY-4.0'),
    'uspto_templates.csv.gz':('https://zenodo.org/api/records/7341155/files/uspto_unique_templates.csv.gz/content','e8fb29d4dcd54073658000e212b7845a','CC-BY-4.0'),
    'zinc_stock.hdf5':('https://ndownloader.figshare.com/files/23086469',None,'See upstream AiZynthFinder/ZINC data terms; historic stock, not live supplier inventory'),
}

def main():
    destination=ROOT/'data'/'models'/'retrosynthesis'
    destination.mkdir(parents=True,exist_ok=True)
    manifest=[]
    for name,(url,expected,license_name) in ASSETS.items():
        path=destination/name
        if not path.exists() or (expected and hashlib.md5(path.read_bytes()).hexdigest()!=expected):
            temp=destination/(name+'.part')
            print('Downloading '+name,flush=True)
            request=urllib.request.Request(url,headers={'User-Agent':'ResearchAtlas/0.3 scientific-assets'})
            with urllib.request.urlopen(request,timeout=120) as response,temp.open('wb') as output:
                while True:
                    block=response.read(1024*1024)
                    if not block:break
                    output.write(block)
            if expected and hashlib.md5(temp.read_bytes()).hexdigest()!=expected:
                raise RuntimeError('Checksum mismatch: '+name)
            temp.replace(path)
        manifest.append({'file':name,'source':url,'license':license_name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'bytes':path.stat().st_size})
    (destination/'sources.json').write_text(json.dumps(manifest,indent=2))
    print('AiZynthFinder model assets ready',flush=True)

if __name__=='__main__':main()
