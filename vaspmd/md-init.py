#!/usr/bin/env python3

import argparse
import shutil
from glob import glob
import os
import md


TEMP_REPL = '茶'
NPAR_REPL = '道'

# constants for the linter's sake
STAGE_LINEAR = 'linear'
STAGE_NOSE   = 'nose'
STAGE_NVE    = 'nve'

def main():
	parser = argparse.ArgumentParser()
	parser.add_argument('OUTDIR', type=str)
	parser.add_argument('--temp', required=True, type=int)
	parser.add_argument('--poscar', required=True, type=str)
	parser.add_argument('--steps', required=True, type=int, nargs=3, metavar=['LIN_STEPS','NOSE_STEPS','NVE_STEPS'])
	parser.add_argument('--npar', required=True, type=int)
	parser.add_argument('--blocksize', required=True, type=int, help='applicable stages are split up into computations of this many steps')
	parser.add_argument('--no-zero', action='store_true', help="start with an nvt stage rather than scaling up from absolute zero")

	args = parser.parse_args()

	# give the linter an easier time by tearing args apart into local vars
	_main(
		outdir=args.OUTDIR,
		temperature=args.temp,
		poscar_path=args.poscar,
		linear_steps=args.steps[0],
		nose_steps=args.steps[1],
		nve_steps=args.steps[2],
		blocksize=args.blocksize,
		npar=args.npar,
		no_zero=args.no_zero,
	)

def _main(outdir, temperature, poscar_path, linear_steps, nose_steps, nve_steps, blocksize, npar, no_zero):
	os.mkdir(outdir)
	def out(fname):
		return os.path.join(outdir, fname)

	# same-name copies
	for fname in ['INCAR.linear', 'INCAR.nose', 'INCAR.nve', 'POTCAR', 'KPOINTS']:
		shutil.copyfile(fname, out(fname))
	# renamed copies
	shutil.copyfile(poscar_path,     out('POSCAR'))
	shutil.copyfile('INCAR.general', out('INCAR.part'))

	# write variables into INCARs
	for fname in glob(outdir + '/INCAR*'):
		file_subst(fname, TEMP_REPL,  temperature)
		file_subst(fname, NPAR_REPL,  npar)

	md.write_conf(mddir=outdir,
		temperature=temperature,
		from_zero=not no_zero,
		blocksize=blocksize,
		linear_steps=linear_steps,
		nose_steps=nose_steps,
		nve_steps=nve_steps,
	)

# sed s/old/new/g (inplace)
def file_subst(path, old, new):
	new = str(new)

	with open(path) as f:
		s = f.read()

	s = s.replace(old, new)
	with open(path, 'w') as f:
		f.write(s)

if __name__ == '__main__':
	main()
