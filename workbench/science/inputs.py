import json
import math
from pathlib import Path


def validate(value):
    if value is None:return {}
    if not isinstance(value,dict) or len(json.dumps(value,ensure_ascii=False))>150000:
        raise ValueError('Scientific inputs must be a JSON object no larger than 150 KB')
    molecules=value.get('molecules',[])
    if not isinstance(molecules,list) or len(molecules)>64:
        raise ValueError('A run can contain at most 64 molecules')
    for item in molecules:
        if not isinstance(item,dict) or not isinstance(item.get('smiles'),str) or not 1<=len(item['smiles'])<=2000:
            raise ValueError('Each molecule must include a valid-length SMILES field')
        if item.get('role','candidate') not in ('model','candidate','seed'):
            raise ValueError('Molecule role must be model, candidate, or seed')
        if not isinstance(item.get('name',''),str) or len(item.get('name',''))>200:
            raise ValueError('Invalid molecule name')
    for key,maximum in [('max_candidates',64),('max_docking_ligands',8),('max_retro_targets',3),('exhaustiveness',16)]:
        if key in value and (type(value[key]) is not int or not 1<=value[key]<=maximum):
            raise ValueError(key+' is outside the allowed range')
    for flag in ('allow_public_lookup','generate_analogs'):
        if flag in value and type(value[flag]) is not bool:raise ValueError(flag+' must be a Boolean')
    target=value.get('target',{})
    if not isinstance(target,dict):raise ValueError('target must be an object')
    for key in ('center','box_size'):
        if key in target:
            points=target[key]
            if not isinstance(points,list) or len(points)!=3 or any(type(x) not in (int,float) or not math.isfinite(x) for x in points):
                raise ValueError(key+' must contain three finite numbers')
            if key=='box_size' and any(not 4<=x<=35 for x in points):raise ValueError('Docking-box side lengths must be 4–35 Å')
    if 'mol_ratio' in value:
        ratio=value['mol_ratio']
        if not isinstance(ratio,list) or len(ratio)!=2 or any(type(x) not in (int,float) or not 0<x<=10 for x in ratio):
            raise ValueError('mol_ratio requires two positive numbers, each no greater than 10')
    def finite(obj):
        if isinstance(obj,float) and not math.isfinite(obj):raise ValueError('Parameters cannot contain NaN or infinity')
        if isinstance(obj,dict):
            for v in obj.values():finite(v)
        elif isinstance(obj,list):
            for v in obj:finite(v)
    finite(value)
    return json.loads(json.dumps(value))


class Inputs:
    def __init__(self,root,run,files):
        self.root=Path(root)
        self.run=run
        self.science=validate(run.get('science',{}))
        self.files={r['name']:r for r in files}
        self.artifacts=self.root/'artifacts'/run['id']
        self.artifacts.mkdir(parents=True,exist_ok=True)

    def file(self,name):
        if not isinstance(name,str) or name not in self.files:
            raise ValueError('Requested file is not attached to this run: '+str(name)[:100])
        record=self.files[name]
        path=self.root/'files'/record['id']
        import hashlib
        if hashlib.sha256(path.read_bytes()).hexdigest()!=record['sha256']:
            raise ValueError('File integrity check failed')
        return path

    def save(self,name,data):
        if Path(name).name!=name:raise ValueError('Invalid artifact filename')
        path=self.artifacts/name
        if isinstance(data,str):path.write_text(data,encoding='utf-8')
        else:path.write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
        return name

    def prior(self,stage):
        return self.run.get('outputs',{}).get(stage,{}).get('tool_result',{}).get('data',{})

    def molecules(self):
        rows=list(self.science.get('molecules',[]))
        from rdkit import Chem
        import csv
        for name in self.files:
            lower=name.lower()
            if lower.endswith('.smi'):
                for line in self.file(name).read_text().splitlines()[:64]:
                    if not line.strip() or line.startswith('#'):continue
                    parts=line.split(maxsplit=1)
                    rows.append({'smiles':parts[0],'name':parts[1] if len(parts)>1 else parts[0],'source_file':name})
            elif lower.endswith('.sdf'):
                for i,mol in enumerate(Chem.SDMolSupplier(str(self.file(name)))):
                    if i>=64:break
                    if mol is not None:rows.append({'smiles':Chem.MolToSmiles(mol),'name':mol.GetProp('_Name') if mol.HasProp('_Name') else 'SDF-'+str(i+1),'source_file':name})
            elif lower.endswith('.csv'):
                with self.file(name).open(encoding='utf-8-sig') as f:
                    reader=csv.DictReader(f)
                    if 'smiles' in (reader.fieldnames or []):
                        for i,row in enumerate(reader):
                            if i>=64:break
                            row['source_file']=name
                            rows.append(row)
        return rows[:64]
