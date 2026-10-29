import base64
import csv
import hashlib
import importlib.util
import io
import json
import math
from pathlib import Path
import tempfile
import unittest

from workbench.science.inputs import Inputs,validate

READY=importlib.util.find_spec('rdkit') is not None
FIXTURES=Path(__file__).parent/'fixtures'/'vina'


@unittest.skipUnless(READY,'Run scientific tests using .venv/bin/python')
class ScientificTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        (self.root/'files').mkdir()
        self.run={'id':'scientific-test','science':{'molecules':[
            {'name':'model','smiles':'NC(=O)c1ccccc1','role':'model','pka_base':2.0,'melting_point_k':500,'fusion_enthalpy_j_mol':20000},
            {'name':'counterpart','smiles':'O=C(O)c1ccccc1','pka_acid':4.0,'melting_point_k':400,'fusion_enthalpy_j_mol':15000}]},'outputs':{}}
        self.files=[]

    def tearDown(self):self.tmp.cleanup()

    def add(self,name,content):
        content=content.encode() if isinstance(content,str) else content
        file_id=hashlib.sha256(name.encode()).hexdigest()[:32]
        (self.root/'files'/file_id).write_bytes(content)
        self.files.append({'id':file_id,'name':name,'sha256':hashlib.sha256(content).hexdigest()})

    def ctx(self):return Inputs(self.root,self.run,self.files)

    def test_descriptor_values_deduplication_and_invalid_smiles(self):
        from workbench.science.chemistry import records
        self.run['science']['molecules']=[{'smiles':'CCO'},{'smiles':'OCC'},{'smiles':'not-a-smiles'}]
        rows,errors=records(self.ctx())
        self.assertEqual(len(rows),1)
        self.assertAlmostEqual(rows[0]['descriptors']['mw'],46.069,places=2)
        self.assertEqual(len(errors),1)

    def test_concept_preserves_unknowns_and_does_not_predict_probability(self):
        from workbench.science.solids import concept,screen
        result=concept(self.ctx())
        self.assertEqual(result['data']['pairs'][0]['delta_pka_assessments'][0]['delta_pka'],-2)
        self.assertGreater(result['data']['pairs'][0]['hydrogen_bond_complementarity_count'],0)
        self.assertIsNone(screen(self.ctx())['data']['pairs'][0]['formation_probability'])

    def test_ideal_eutectic_is_below_both_melting_points(self):
        from workbench.science.solids import thermal
        result=thermal(self.ctx())['data']['ideal_binary_baseline'][0]
        self.assertLess(result['ideal_eutectic_temperature_k'],400)
        self.assertTrue(0<result['ideal_eutectic_model_mole_fraction']<1)
        self.assertIn('does not predict formation of a new cocrystal phase',result['assumptions'][-1])

    def test_dsc_tga_and_reference_pxrd_processing(self):
        from workbench.science.solids import thermal,evaluation
        xs=[i*.1 for i in range(101)]
        product='two_theta_deg,intensity\n'+'\n'.join(f'{x},{100*math.exp(-((x-5)/.2)**2)+10}' for x in xs)
        reference='two_theta_deg,intensity\n'+'\n'.join(f'{x},{100*math.exp(-((x-3)/.2)**2)+10}' for x in xs)
        dsc='temperature_c,heat_flow_w_g\n'+'\n'.join(f'{x+100},{math.exp(-((x-5)/.3)**2)}' for x in xs)
        tga='temperature_c,mass_percent\n'+'\n'.join(f'{x+100},{100-x}' for x in xs)
        for name,content in [('product.csv',product),('reference.csv',reference),('dsc.csv',dsc),('tga.csv',tga)]:self.add(name,content)
        self.run['science'].update(spectra={'pxrd':'product.csv','pxrd_references':['reference.csv'],'dsc':'dsc.csv','tga':'tga.csv'},heating_rate_k_min=10)
        thermo=thermal(self.ctx())
        self.assertAlmostEqual(thermo['data']['thermal_analysis']['dsc']['peaks'][0]['temperature_c'],105,places=1)
        self.assertAlmostEqual(thermo['data']['thermal_analysis']['tga']['mass_loss_percent'],10)
        pxrd=evaluation(self.ctx())
        self.assertEqual(pxrd['data']['phase_identity'],'not_confirmed')
        self.assertAlmostEqual(pxrd['data']['unmatched_peaks_two_theta_deg'][0],5,places=1)

    def test_nonmonotonic_spectrum_rejected(self):
        from workbench.science.solids import spectrum
        self.add('bad.csv','temperature_c,heat_flow_w_g\n'+ '\n'.join('1,2' for _ in range(8)))
        with self.assertRaises(ValueError):spectrum(self.ctx(),'bad.csv','temperature_c','heat_flow_w_g')

    def test_structure_parses_actual_official_protein(self):
        from workbench.science.chemistry import structure
        self.add('protein.pdb',(FIXTURES/'1iep_receptorH.pdb').read_bytes())
        self.run['science']['target']={'structure_file':'protein.pdb','chain':'A','site_residues':[286,315]}
        result=structure(self.ctx())
        self.assertEqual(result['status'],'computed')
        self.assertGreater(sum(c['residue_count'] for c in result['data']['chains']),100)

    def test_real_vina_docking_creates_pose(self):
        from rdkit import Chem
        from workbench.science.chemistry import evaluate
        mol=next(m for m in Chem.SDMolSupplier(str(FIXTURES/'1iep_ligand.sdf')) if m is not None)
        self.run['science'].update(molecules=[{'name':'official imatinib ligand','smiles':Chem.MolToSmiles(mol)}],max_docking_ligands=1,exhaustiveness=1,
            target={'receptor_file':'receptor.pdbqt','center':[15.190,53.903,16.917],'box_size':[20,20,20]})
        self.add('receptor.pdbqt',(FIXTURES/'1iep_receptor.pdbqt').read_bytes())
        result=evaluate(self.ctx())
        self.assertTrue(result['data']['molecules'][0]['docking']['scores_kcal_mol'])
        self.assertTrue((self.ctx().artifacts/'docked-0.pdbqt').exists())

    def test_real_aizynthfinder_returns_route_trees(self):
        from workbench.science.chemistry import synthesis
        self.run['science']['molecules']=[{'name':'aspirin','smiles':'CC(=O)Oc1ccccc1C(=O)O'}]
        result=synthesis(self.ctx())
        self.assertEqual(result['status'],'partial')
        self.assertIn('routes',result['data']['targets'][0])
        self.assertTrue((self.ctx().artifacts/'retrosynthesis-routes.json').exists())


class InputContractTests(unittest.TestCase):
    def test_invalid_bounds_and_nonfinite(self):
        for value in ({'max_docking_ligands':100},{'target':{'box_size':[100,20,20]}},{'molecules':'bad'},{'allow_public_lookup':'yes'},{'mol_ratio':[0,1]},{'x':float('nan')}):
            with self.subTest(value=value),self.assertRaises(ValueError):validate(value)
