import io
import json
import math
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
import numpy as np
from rdkit import Chem,DataStructs,rdBase
from rdkit.Chem import AllChem,Descriptors,Lipinski,QED,Crippen,rdMolDescriptors,BRICS
from rdkit.Chem.MolStandardize import rdMolStandardize

ROOT=Path(__file__).resolve().parents[2]


def output(status,summary,data=None,missing=None,evidence=None,artifacts=None):
    return {'status':status,'summary':summary,'data':data or {},'missing':missing or [],'evidence':evidence or [],
            'artifacts':artifacts or [],'version':{'adapter':'atlas-science-0.3','rdkit':rdBase.rdkitVersion}}


def records(ctx):
    rows=[];errors=[];seen=set()
    for index,item in enumerate(ctx.molecules()):
        mol=Chem.MolFromSmiles(item.get('smiles',''))
        if mol is None:
            errors.append({'index':index,'name':item.get('name',''),'reason':'SMILES parsing failed'});continue
        mol=rdMolStandardize.Cleanup(mol)
        smiles=Chem.MolToSmiles(mol,isomericSmiles=True)
        if smiles in seen:continue
        seen.add(smiles)
        if mol.GetNumHeavyAtoms()>120:
            errors.append({'index':index,'reason':'Exceeded the local small-molecule atom limit'});continue
        row=dict(item,smiles=smiles,name=item.get('name') or 'Molecule-'+str(index+1),role=item.get('role','candidate'))
        row['descriptors']={'mw':round(Descriptors.MolWt(mol),4),'logp':round(Crippen.MolLogP(mol),4),
            'tpsa':round(rdMolDescriptors.CalcTPSA(mol),4),'hbd':Lipinski.NumHDonors(mol),'hba':Lipinski.NumHAcceptors(mol),
            'rotatable_bonds':Lipinski.NumRotatableBonds(mol),'formal_charge':Chem.GetFormalCharge(mol),'qed':round(QED.qed(mol),5)}
        row['descriptors']['lipinski_violations']=sum([row['descriptors']['mw']>500,row['descriptors']['logp']>5,row['descriptors']['hbd']>5,row['descriptors']['hba']>10])
        rows.append(row)
    return rows,errors


def fetch(url,max_bytes=12000000):
    request=urllib.request.Request(url,headers={'User-Agent':'ResearchAtlas/0.3 (bounded public scientific lookup)'})
    with urllib.request.urlopen(request,timeout=25) as response:
        data=response.read(max_bytes+1)
    if len(data)>max_bytes:raise ValueError('Public data exceeded the download limit')
    return data


def structure(ctx):
    from Bio.PDB import PDBParser,MMCIFParser
    config=ctx.science.get('target',{})
    name=config.get('structure_file') or next((n for n in ctx.files if n.lower().endswith(('.pdb','.cif'))),None)
    evidence=[];artifacts=[]
    if name:
        text=ctx.file(name).read_text();fmt='cif' if name.lower().endswith('.cif') else 'pdb'
        evidence=[{'source_file':name,'sha256':ctx.files[name]['sha256']}]
    elif config.get('pdb_id'):
        pdb=config['pdb_id']
        if not isinstance(pdb,str) or not re.fullmatch(r'[0-9A-Za-z]{4}',pdb):return output('invalid_input','PDB ID must contain four alphanumeric characters.')
        if not ctx.science.get('allow_public_lookup'):return output('needs_input','A PDB ID was provided, but public-data lookup has not been authorized.',missing=['Set allow_public_lookup=true or upload a structure file'])
        url='https://files.rcsb.org/download/'+pdb.upper()+'.pdb'
        text=fetch(url).decode();fmt='pdb';artifacts=[ctx.save('receptor-source.pdb',text)]
        evidence=[{'source':url,'note':'Raw RCSB structure; receptor preparation is not complete'}]
    else:return output('needs_input','A structure file or PDB ID is required. A binding site will not be inferred from the target name.',missing=['target.structure_file or target.pdb_id'])
    parser=MMCIFParser(QUIET=True) if fmt=='cif' else PDBParser(QUIET=True)
    model=next(parser.get_structure('target',io.StringIO(text)).get_models())
    chains=[];nonprotein=[];selected=[];altloc=0;missing_backbone=[]
    site=set(str(x) for x in config.get('site_residues',[]))
    for chain in model:
        residues=[]
        for residue in chain:
            if residue.id[0]==' ':
                residues.append(residue.id[1])
                absent=[atom for atom in ('N','CA','C','O') if atom not in residue]
                if absent:missing_backbone.append({'chain':chain.id,'residue':residue.id[1],'missing':absent})
                if (not config.get('chain') or chain.id==config['chain']) and str(residue.id[1]) in site:
                    selected.extend(atom.coord.tolist() for atom in residue if atom.element!='H')
            else:nonprotein.append({'chain':chain.id,'residue':residue.resname,'number':residue.id[1]})
            altloc+=sum(atom.is_disordered() for atom in residue)
        chains.append({'chain':chain.id,'residue_count':len(residues),'numbering_gaps':[[a,b] for a,b in zip(residues,residues[1:]) if b>a+1]})
    center=config.get('center')
    if center is None and selected:center=np.mean(selected,axis=0).tolist()
    data={'chains':chains,'hetero_residues':nonprotein[:100],'alternate_atoms':int(altloc),'incomplete_backbone':missing_backbone[:100],
          'site_center_angstrom':center,'box_size_angstrom':config.get('box_size',[20,20,20]),
          'limitations':['Numbering gaps do not necessarily indicate missing residues. Missing residues were not added; protonation, metal coordination, and biological conformation were not automatically validated.']}
    artifacts.append(ctx.save('structure-analysis.json',data))
    return output('computed','Parsed the structure and checked chains, residue numbering, backbone atoms, and alternate conformations.',data,[] if center else ['Docking-box center or explicitly defined site residues'],evidence,artifacts)


def design(ctx):
    rows,errors=records(ctx);evidence=[]
    target=ctx.science.get('chembl_target_id')
    if target and ctx.science.get('allow_public_lookup'):
        if not re.fullmatch(r'CHEMBL[0-9]+',target):return output('invalid_input','Invalid ChEMBL target ID format.')
        url='https://www.ebi.ac.uk/chembl/api/data/activity.json?target_chembl_id='+target+'&standard_type__in=IC50,Ki,Kd&standard_relation=%3D&pchembl_value__isnull=false&limit=20'
        activities=json.loads(fetch(url)).get('activities',[])
        known={r['smiles'] for r in rows}
        for activity in activities[:20]:
            mid=activity.get('molecule_chembl_id')
            if not mid or not re.fullmatch(r'CHEMBL[0-9]+',mid):continue
            compound=json.loads(fetch('https://www.ebi.ac.uk/chembl/api/data/molecule/'+mid+'.json'))
            smiles=(compound.get('molecule_structures') or {}).get('canonical_smiles')
            if not smiles or smiles in known:continue
            known.add(smiles)
            mol=Chem.MolFromSmiles(smiles)
            if mol is None:continue
            row={'name':mid,'smiles':Chem.MolToSmiles(mol),'role':'candidate','source':'ChEMBL',
                 'activity':{k:activity.get(k) for k in ('standard_type','standard_value','standard_units','assay_chembl_id','document_chembl_id','pchembl_value')}}
            rows.append(row)
        evidence.append({'source':url,'license':'ChEMBL CC-BY-SA 3.0; preserve release/record attribution','note':'Values across assays are not directly comparable; purchasability and compatibility with the current target conformation are unverified'})
    if ctx.science.get('generate_analogs') and rows:
        # Enumerate chemically valid substitutions; no ungrounded efficacy ranking.
        reaction=AllChem.ReactionFromSmarts('[cH:1]>>[c:1]F')
        generated=[];seen={r['smiles'] for r in rows}
        for row in rows[:4]:
            for products in reaction.RunReactants((Chem.MolFromSmiles(row['smiles']),)):
                try:
                    mol=products[0];Chem.SanitizeMol(mol);smiles=Chem.MolToSmiles(mol)
                    if smiles in seen:continue
                    seen.add(smiles);generated.append({'name':row['name']+'-F-'+str(len(generated)+1),'smiles':smiles,'parent_smiles':row['smiles'],'modification':'Aromatic C–H to fluorine enumeration; activity and synthetic feasibility are not established'})
                except Exception:continue
                if len(generated)>=12:break
            if len(generated)>=12:break
        rows.extend(generated)
    if not rows:return output('needs_input','No parseable candidate library or public activity target ID was provided.',{'invalid_molecules':errors},['science.molecules, an SMI/SDF/CSV compound library, or a ChEMBL target ID'])
    rows=rows[:ctx.science.get('max_candidates',32)]
    artifact=ctx.save('molecule-candidates.json',rows)
    return output('partial','Prepared '+str(len(rows))+' input, retrieved, or rule-enumerated candidate(s); target activity is not established.',{'candidates':rows,'invalid_molecules':errors},['Candidate activity, selectivity, and experimental validation'],evidence,[artifact])


def evaluate(ctx):
    rows=ctx.prior('design').get('candidates') or records(ctx)[0]
    if not rows:return output('needs_input','No molecules are available for assessment.',missing=['Candidate molecules'])
    data=[];artifacts=[];missing=['A validated ADMET/toxicity model and its applicability domain; molecular descriptors are not an ADMET prediction']
    target=ctx.science.get('target',{})
    receptor_name=target.get('receptor_file')
    receptor=None
    if receptor_name:receptor=ctx.file(receptor_name)
    center=target.get('center') or ctx.prior('structure').get('site_center_angstrom')
    vina=ROOT/'.runtime'/'bin'/'vina'
    if not (receptor and center and vina.exists()):missing.append('Docking requires receptor_file (PDBQT), center, and Vina')
    seed=Chem.MolFromSmiles(rows[0]['smiles']);seedfp=AllChem.GetMorganGenerator(radius=2,fpSize=2048).GetFingerprint(seed) if hasattr(AllChem,'GetMorganGenerator') else AllChem.GetMorganFingerprintAsBitVect(seed,2,nBits=2048)
    for index,row in enumerate(rows[:64]):
        mol=Chem.MolFromSmiles(row['smiles'])
        if mol is None:continue
        props={'mw':Descriptors.MolWt(mol),'logp':Crippen.MolLogP(mol),'tpsa':rdMolDescriptors.CalcTPSA(mol),'qed':QED.qed(mol),
               'hbd':Lipinski.NumHDonors(mol),'hba':Lipinski.NumHAcceptors(mol),'rotatable_bonds':Lipinski.NumRotatableBonds(mol)}
        fp=AllChem.GetMorganFingerprintAsBitVect(mol,2,nBits=2048)
        item=dict(row,descriptors=props,similarity_to_first=DataStructs.TanimotoSimilarity(seedfp,fp),docking=None)
        if receptor and center and vina.exists() and index<ctx.science.get('max_docking_ligands',3):
            from meeko import MoleculePreparation,PDBQTWriterLegacy
            molecule=Chem.AddHs(mol)
            if AllChem.EmbedMolecule(molecule,randomSeed=2026)!=0:
                item['docking_error']='3D conformer generation failed';data.append(item);continue
            if AllChem.MMFFHasAllMoleculeParams(molecule):AllChem.MMFFOptimizeMolecule(molecule,maxIters=200)
            prepared=MoleculePreparation().prepare(molecule)[0]
            pdbqt,ok,error=PDBQTWriterLegacy.write_string(prepared)
            if not ok:raise ValueError('Ligand conversion to PDBQT failed')
            ligand=ctx.artifacts/('ligand-'+str(index)+'.pdbqt');ligand.write_text(pdbqt)
            pose=ctx.artifacts/('docked-'+str(index)+'.pdbqt')
            command=[str(vina),'--receptor',str(receptor),'--ligand',str(ligand),'--out',str(pose),'--cpu','1','--seed','2026','--exhaustiveness',str(ctx.science.get('exhaustiveness',4)),'--num_modes','3']
            for axis,point,size in zip('xyz',center,target.get('box_size',[20,20,20])):command+=['--center_'+axis,str(point),'--size_'+axis,str(size)]
            process=subprocess.run(command,capture_output=True,text=True,timeout=100)
            if process.returncode!=0:item['docking_error']='Vina rejected the input or the calculation failed. Check receptor preparation and the search box.'
            elif pose.exists():
                scores=[float(m) for m in re.findall(r'REMARK VINA RESULT:\s+(-?[0-9.]+)',pose.read_text())]
                item['docking']={'scores_kcal_mol':scores,'engine':'AutoDock Vina 1.2.7','seed':2026,'cpu':1,'center':center,'box_size':target.get('box_size',[20,20,20]),'exhaustiveness':ctx.science.get('exhaustiveness',4),'interpretation':'Docking score; not measured affinity or binding free energy'}
                artifacts.extend([ligand.name,pose.name])
        data.append(item)
    artifacts.append(ctx.save('molecule-evaluation.json',data))
    return output('partial','Calculated '+str(len(data))+' molecule(s): calculated physicochemical descriptors'+(' and AutoDock Vina docking.' if any(r.get('docking') for r in data) else '; docking inputs were not available.'),{'molecules':data},missing,artifacts=artifacts)


def synthesis(ctx):
    folder=ROOT/'data'/'models'/'retrosynthesis'
    required=['uspto_model.onnx','uspto_templates.csv.gz','uspto_filter_model.onnx','zinc_stock.hdf5']
    if not all((folder/f).exists() for f in required):return output('needs_dependency','The retrosynthesis adapter is connected, but the official model, templates, or stock data are not fully downloaded.',missing=required)
    from aizynthfinder.aizynthfinder import AiZynthFinder
    rows=ctx.prior('assay').get('molecules') or ctx.prior('design').get('candidates') or records(ctx)[0]
    if not rows:return output('needs_input','A candidate structure is required for retrosynthesis.',missing=['Candidate SMILES'])
    config={'expansion':{'uspto':[str(folder/required[0]),str(folder/required[1])]},'filter':{'uspto':str(folder/required[2])},'stock':{'zinc':str(folder/required[3])},
            'search':{'max_transforms':4,'iteration_limit':40,'time_limit':40,'return_first':True}}
    finder=AiZynthFinder(configdict=config)
    finder.expansion_policy.select('uspto');finder.filter_policy.select('uspto');finder.stock.select('zinc')
    results=[]
    for row in rows[:ctx.science.get('max_retro_targets',1)]:
        finder.target_smiles=row['smiles'];finder.prepare_tree();finder.tree_search();finder.build_routes()
        routes=[]
        for route in finder.routes.reaction_trees[:3]:
            routes.append({'tree':route.to_dict(),'solved_in_reference_stock':bool(route.is_solved),'maximum_depth':max((int(route.depth(n)) for n in route.graph),default=0),'note':'Model-generated route proposal; reaction conditions, yield, stereoselectivity, current stock, and lab feasibility are not guaranteed'})
        results.append({'smiles':row['smiles'],'routes':routes,'statistics':finder.extract_statistics()})
    artifact=ctx.save('retrosynthesis-routes.json',results)
    return output('partial','Neural-network-guided retrosynthesis search completed and route trees saved. Stock matches refer only to the historical ZINC library.',{'targets':results},['Reaction precedents and conditions, supplier availability, and chemist review'],[{'source':'https://github.com/MolecularAI/aizynthfinder','model_sources':'data/models/retrosynthesis/sources.json'}],[artifact])
