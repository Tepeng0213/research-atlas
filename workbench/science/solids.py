import csv
import math
from pathlib import Path
import numpy as np
from scipy.signal import find_peaks
from scipy.optimize import brentq,nnls
from rdkit import Chem,DataStructs
from rdkit.Chem import AllChem
from .chemistry import output,records,ROOT


def number(value):
    try:
        result=float(value)
        return result if math.isfinite(result) else None
    except (TypeError,ValueError):return None


def paired(ctx):
    rows,errors=records(ctx)
    model=next((r for r in rows if r.get('role')=='model'),rows[0] if rows else None)
    return model,[r for r in rows if model and r['smiles']!=model['smiles']],errors


def pair(ctx):
    model,candidates,errors=paired(ctx)
    if not model or not candidates:return output('needs_input','Provide at least one model drug and one distinct counterpart candidate.',{'invalid_molecules':errors},['Molecular SMILES; set role=model for the model drug'])
    ratios=ctx.science.get('mol_ratio',[1,1]);results=[]
    for candidate in candidates:
        molar_ratio=None
        d1,d2=number(model.get('dose_mg')),number(candidate.get('dose_mg'))
        if d1 and d2 and d1>0 and d2>0:
            molar_ratio=(d1/model['descriptors']['mw'])/(d2/candidate['descriptors']['mw'])
        results.append({'model':model,'counterpart':candidate,'dose_molar_ratio':molar_ratio,'requested_crystal_molar_ratio':ratios[0]/ratios[1],
                        'dose_stoichiometry_fold_difference':max(molar_ratio/(ratios[0]/ratios[1]),(ratios[0]/ratios[1])/molar_ratio) if molar_ratio else None,
                        'therapeutic_evidence':candidate.get('combination_evidence') or 'No verifiable combination-therapy evidence was provided',
                        'clinical_synergy':'not_assessed','pharmacokinetic_compatibility':'not_assessed'})
    return output('partial','Structure standardization, physicochemical descriptors, and available dose-to-molar-ratio checks are complete; therapeutic benefit is not established.',{'pairs':results,'invalid_molecules':errors},['Combination-therapy evidence, pharmacokinetics, and route-of-administration compatibility'],artifacts=[ctx.save('drug-pairing.json',results)])


def concept(ctx):
    pairs=ctx.prior('pairing').get('pairs') or pair(ctx).get('data',{}).get('pairs',[])
    if not pairs:return output('needs_input','No molecular pair is available for conceptual screening.',missing=['Model drug and counterpart structure'])
    results=[]
    for p in pairs:
        a,b=p['model'],p['counterpart'];pa,pb=a['descriptors'],b['descriptors']
        complementary=min(pa['hbd'],pb['hba'])+min(pb['hbd'],pa['hba'])
        deltas=[]
        for acid,base in [(a,b),(b,a)]:
            acid_value,base_value=number(acid.get('pka_acid')),number(base.get('pka_base'))
            if acid_value is not None and base_value is not None:
                delta=base_value-acid_value
                deltas.append({'acid':acid['name'],'base':base['name'],'delta_pka':delta,
                               'tendency':'salt_favored' if delta>3 else 'neutral_favored' if delta<0 else 'ambiguous',
                               'source':'User-supplied values; confirm that solvent, temperature, and pKa species are consistent'})
        results.append({'model':a,'counterpart':b,'hydrogen_bond_complementarity_count':complementary,'delta_pka_assessments':deltas,
                        'interpretation':'Donor/acceptor counts and empirical acid-base rules; this does not confirm geometric synthons or formation probability'})
    return output('computed','Calculated hydrogen-bond donor/acceptor complementarity and acid-base tendencies from supplied pKa values; uncertainty is retained.',{'pairs':results},['3D synthon geometry, competing phases, and experimental validation'],artifacts=[ctx.save('cocrystal-concept.json',results)])


def screen(ctx):
    pairs=ctx.prior('concept').get('pairs') or concept(ctx).get('data',{}).get('pairs',[])
    if not pairs:return output('needs_input','A molecular pair is required.',missing=['Model drug and candidate structure'])
    results=[];predictions=None;model_manifest=ROOT/'data'/'models'/'cocrystal'/'manifest.json'
    missing=['A validated, appropriately licensed cocrystal-prediction model; current rules do not return formation probabilities','Periodic crystal structures and thermodynamic validation']
    if model_manifest.exists():
        import json,hashlib,onnxruntime as ort
        manifest=json.loads(model_manifest.read_text())
        model_name=manifest.get('file','')
        if Path(model_name).name!=model_name or not model_name.endswith('.onnx'):raise ValueError('Invalid cocrystal model filename')
        path=model_manifest.parent/model_name
        if manifest.get('features')!='morgan_pair_2048_v1' or not manifest.get('license') or hashlib.sha256(path.read_bytes()).hexdigest()!=manifest.get('sha256'):
            raise ValueError('Cocrystal model features, license, or checksum metadata are incomplete')
        vectors=[]
        for p in pairs:
            vector=[]
            for row in (p['model'],p['counterpart']):
                fp=AllChem.GetMorganFingerprintAsBitVect(Chem.MolFromSmiles(row['smiles']),2,nBits=2048)
                array=np.zeros(2048,dtype=np.float32);DataStructs.ConvertToNumpyArray(fp,array);vector.extend(array.tolist())
            vectors.append(vector)
        session=ort.InferenceSession(str(path),providers=['CPUExecutionProvider'])
        raw=session.run(None,{session.get_inputs()[0].name:np.asarray(vectors,dtype=np.float32)})[0]
        predictions=np.asarray(raw).reshape(len(pairs),-1).tolist()
        missing[0]='The model returns a score only; independent calibration, applicability-domain assessment, and experimental validation are required'
    energies=ctx.science.get('interaction_energies',[])
    for index,p in enumerate(pairs):
        item={'model':p['model']['name'],'counterpart':p['counterpart']['name'],
              'hydrogen_bond_complementarity_count':p['hydrogen_bond_complementarity_count'],'formation_probability':None,
              'model_score':predictions[index] if predictions is not None else None,'interaction_energy_kj_mol':None}
        for e in energies[:64]:
            if e.get('counterpart')!=p['counterpart']['name']:continue
            values=[number(e.get(k)) for k in ('dimer_hartree','model_hartree','counterpart_hartree')]
            if all(v is not None for v in values) and e.get('method'):
                item['interaction_energy_kj_mol']=(values[0]-values[1]-values[2])*2625.499639
                item['energy_provenance']={'source':'User-supplied quantum-chemical energies','method':e['method'],'limitations':'Matching theory levels, basis sets, solvents, BSSE, and zero-point corrections were not verified; this is not a lattice free energy'}
        results.append(item)
    return output('partial','Paired molecular-feature analysis completed'+(' and inference with the configured ONNX model.' if predictions is not None else '; no appropriately licensed predictive model is configured, so no formation probability is generated.'),{'pairs':results},missing,artifacts=[ctx.save('cocrystal-screening.json',results)])


def spectrum(ctx,name,x_key,y_key):
    with ctx.file(name).open(encoding='utf-8-sig') as file:
        reader=csv.DictReader(file)
        if not all(k in (reader.fieldnames or []) for k in (x_key,y_key)):raise ValueError(name+' requires columns '+x_key+' and '+y_key)
        rows=[]
        for i,row in enumerate(reader):
            if i>=50000:raise ValueError('A spectrum may contain at most 50,000 data points')
            x,y=number(row[x_key]),number(row[y_key])
            if x is None or y is None:raise ValueError('Spectrum contains invalid numeric values')
            rows.append((x,y))
    array=np.asarray(rows,dtype=float)
    if len(array)<7 or np.any(np.diff(array[:,0])<=0):raise ValueError('A spectrum needs at least 7 data points; x values must increase strictly with no duplicates')
    return array[:,0],array[:,1]


def thermal(ctx):
    model,candidates,_=paired(ctx)
    phase=[];artifacts=[];missing=[]
    if model:
        for candidate in candidates:
            ta,tb=number(model.get('melting_point_k')),number(candidate.get('melting_point_k'))
            ha,hb=number(model.get('fusion_enthalpy_j_mol')),number(candidate.get('fusion_enthalpy_j_mol'))
            if not all(v is not None and v>0 for v in (ta,tb,ha,hb)):continue
            r=8.314462618
            def residual(t):return math.exp(ha/r*(1/ta-1/t))+math.exp(hb/r*(1/tb-1/t))-1
            te=brentq(residual,min(ta,tb)*.05,min(ta,tb))
            xe=math.exp(ha/r*(1/ta-1/te))
            x=np.linspace(.005,.995,199)
            liquid_a=1/(1/ta-r/ha*np.log(x));liquid_b=1/(1/tb-r/hb*np.log(1-x))
            phase.append({'counterpart':candidate['name'],'ideal_eutectic_temperature_k':te,'ideal_eutectic_model_mole_fraction':xe,
                          'x_model':x.tolist(),'liquidus_a_k':liquid_a.tolist(),'liquidus_b_k':liquid_b.tolist(),
                          'assumptions':['Ideal liquid phase','The solid phases are immiscible','Heat-capacity differences are ignored','Assumes no intermediate compounds; does not predict a new cocrystal phase or cocrystal melting point']})
    analysis={};spectra=ctx.science.get('spectra',{})
    if spectra.get('dsc'):
        x,y=spectrum(ctx,spectra['dsc'],'temperature_c','heat_flow_w_g')
        baseline=np.linspace(y[0],y[-1],len(y));corrected=y-baseline
        sign=ctx.science.get('endotherm_sign',1)
        if sign not in (1,-1):raise ValueError('endotherm_sign must be 1 or -1')
        indexes,props=find_peaks(sign*corrected,prominence=max(float(np.ptp(corrected))*.05,1e-12))
        peaks=[];rate=number(ctx.science.get('heating_rate_k_min'))
        for i,left,right in zip(indexes,props['left_bases'],props['right_bases']):
            peak={'temperature_c':float(x[i]),'prominence_w_g':float(sign*corrected[i]),'integration_window_c':[float(x[left]),float(x[right])]}
            if rate and rate>0:peak['linear_baseline_area_j_g']=float(np.trapz(corrected[left:right+1],x[left:right+1])*60/rate)
            peaks.append(peak)
        analysis['dsc']={'peaks':peaks,'baseline':'Linear endpoint baseline; overlapping peaks, baseline drift, and peak assignments require manual review','endotherm_sign':sign}
    if spectra.get('tga'):
        x,y=spectrum(ctx,spectra['tga'],'temperature_c','mass_percent')
        derivative=np.gradient(y,x);peaks,_=find_peaks(-derivative,prominence=max(float(np.ptp(derivative))*.05,1e-12))
        analysis['tga']={'mass_loss_percent':float(y[0]-y[-1]),'dtg_peak_temperatures_c':x[peaks].tolist(),'interpretation':'Mass loss and derivative peaks are not automatically assigned to decomposition or desolvation'}
    if not phase:missing.append('The ideal baseline requires measured melting points (K) and fusion enthalpies (J/mol) for both components')
    if not analysis:missing.append('measured DSC/TGA CSV files')
    data={'ideal_binary_baseline':phase,'thermal_analysis':analysis}
    artifacts.append(ctx.save('thermal-analysis.json',data))
    return output('computed' if phase or analysis else 'needs_input','Calculated the ideal binary baseline and/or analyzed measured thermal data.' if phase or analysis else 'The thermal-analysis adapter is connected; thermodynamic parameters or measured thermograms are required.',data,missing,[{'source':'https://pmc.ncbi.nlm.nih.gov/articles/PMC7179121/','method':'Schröder–van Laar ideal solid-liquid baseline'}],artifacts)


def preparation(ctx):
    model,candidates,_=paired(ctx)
    if not model or not candidates:return output('needs_input','A molecular pair is required for preparation planning.',missing=['Model drug and counterpart drug'])
    a,b=ctx.science.get('mol_ratio',[1,1]);design=[]
    for candidate in candidates:
        ma,mb=model['descriptors']['mw'],candidate['descriptors']['mw']
        design.append({'model':model['name'],'counterpart':candidate['name'],'molar_ratio':[a,b],'mass_fraction_model':a*ma/(a*ma+b*mb),
                       'experimental_options':[{'method':'neat_grinding','purpose':'Solvent-free mechanochemical screening; assess thermal stability and mechanical sensitivity'},
                           {'method':'liquid_assisted_grinding','purpose':'Use as a control against dry grinding; choose liquid and liquid-to-solid ratio based on solubility, stability, and laboratory constraints'},
                           {'method':'slurry_or_evaporation','purpose':'Cross-check against mechanochemical results; component solubilities and phase-equilibrium data are required'}],
                       'controls':['Individual-component PXRD/DSC','Untreated physical mixture at the same molar ratio','Process each component separately using the same method','Independent replicates and composition analysis'],
                       'conditions':None,'reason':'No validated solvent/phase-equilibrium data or precedents were provided. Temperatures, grinding times, solvent amounts, and SOPs are not invented.'})
    return output('partial','Calculated component mass fractions for the specified stoichiometry and drafted a control-based experiment design; this is not a validated preparation SOP.',{'designs':design},['Solvent compatibility, solubility/phase equilibrium, thermal stability, experimental precedents, and expert review'],artifacts=[ctx.save('crystallization-design.json',design)])


def evaluation(ctx):
    spectra=ctx.science.get('spectra',{})
    if not spectra.get('pxrd'):return output('needs_input','The PXRD analysis adapter is connected; numeric product and reference patterns are required.',missing=['spectra.pxrd CSV (two_theta_deg,intensity) and pxrd_references'])
    x,y=spectrum(ctx,spectra['pxrd'],'two_theta_deg','intensity')
    if np.any(y<0):raise ValueError('PXRD intensities must not be negative')
    scale=float(np.max(y))
    if scale<=0:raise ValueError('All PXRD intensities are zero')
    indexes,props=find_peaks(y,prominence=scale*.05)
    references=[];reference_peaks=[]
    for name in spectra.get('pxrd_references',[])[:8]:
        rx,ry=spectrum(ctx,name,'two_theta_deg','intensity')
        if np.any(ry<0) or np.max(ry)<=0:raise ValueError('Invalid reference PXRD intensities')
        if rx[0]>x[0] or rx[-1]<x[-1]:raise ValueError('Reference patterns must cover the full product 2θ range')
        ri,_=find_peaks(ry,prominence=float(np.max(ry))*.05)
        reference_peaks.extend(rx[ri].tolist());references.append(np.interp(x,rx,ry)/np.max(ry))
    unmatched=[];mixture=None
    if references:
        unmatched=[float(p) for p in x[indexes] if not any(abs(p-r)<=.2 for r in reference_peaks)]
        matrix=np.asarray(references).T
        weights,_=nnls(matrix,y/scale)
        residual=float(np.linalg.norm(matrix@weights-y/scale)/max(np.linalg.norm(y/scale),1e-12))
        mixture={'nonnegative_fit_weights':weights.tolist(),'relative_residual':residual,'interpretation':'Fit coefficients are not mass fractions; peak shifts, preferred orientation, and amorphous background affect the fit'}
    data={'product_peaks_two_theta_deg':x[indexes].tolist(),'unmatched_peaks_two_theta_deg':unmatched,
          'peak_match_tolerance_deg':.2,'reference_count':len(references),'reference_mixture_fit':mixture,
          'phase_identity':'not_confirmed','interpretation':'New peaks and mixture-fit residuals indicate a need for further review; they cannot independently confirm a cocrystal, purity, or crystal structure'}
    return output('computed' if references else 'partial','PXRD peaks were extracted and compared with reference-mixture models; a new phase still requires confirmation by multiple characterization methods.',data,['Composition, DSC, single-crystal or refined structure, dissolution, and stability validation'],artifacts=[ctx.save('pxrd-evaluation.json',data)])
