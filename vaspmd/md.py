#!/usr/bin/env python3

#SBATCH -J md-runner

# This script is written in such a way as to maximize the code surface
#  area checkable by e.g. pylint, because it REALLY SUCKS to wait for
#  hours in a slurm queue only to discover that we made a typo.
#
# This module contains a number of convoluted practices all to this effect, such as:
#   * heavy abuse of 'from x import y', because pylint won't catch a mistake like
#     `import os; os.exists` but it WILL catch `from os import exists`
#   * awkward uses of required keyword arguments (wherever I fear I'll transpose arguments)
#   * config reading done directly in main
#     (this COULD be done via a Config class with properties, but it'd be pointless because
#      I still wouldn't want to pass around a config *object*; see the next point)
#   * everything in one module (pylint's argument checker can't cross module boundaries)
#   * long argument lists (to allow linting against unused arguments)
#
# I am not proud.

from os.path import join, exists, isdir

VASP_BIN_NAME = 'vasp.g.slm'

# constants for the linter's sake
STAGE_LINEAR = 'linear'
STAGE_NOSE   = 'nose'
STAGE_NVE    = 'nve'

CONF_TEMPERATURE ='temperature'
CONF_FROM_ZERO   ='start-from-zero'
CONF_BLOCKSIZE   ='steps-block'
CONF_LINEAR_STEPS='steps-linear'
CONF_NOSE_STEPS  ='steps-nose'
CONF_NVE_STEPS   ='steps-nve'

TEBEG_REPL = '無'
STEPS_REPL = '数'

START_NUM = 1

VARFILE_SERIES_ALLDIRS = 'series.alldirs'
VARFILE_FINAL_TEMP     = 'md.final-temp'

def main():
	from argparse import ArgumentParser
	from json import load
	from os import getcwd
	parser = ArgumentParser()
	parser.parse_args()

	try:
		with open('md.conf') as f:
			conf = load(f)
	except FileNotFoundError:
		parser.error('missing md.conf!')

	_main(
		mddir        = getcwd(),
		temperature  = conf.pop(CONF_TEMPERATURE),
		from_zero    = conf.pop(CONF_FROM_ZERO),
		blocksize    = conf.pop(CONF_BLOCKSIZE),
		linear_steps = conf.pop(CONF_LINEAR_STEPS),
		nose_steps   = conf.pop(CONF_NOSE_STEPS),
		nve_steps    = conf.pop(CONF_NVE_STEPS),
		unknown      = conf,
	)

def write_conf(mddir, *, temperature, from_zero, blocksize, linear_steps, nose_steps, nve_steps):
	from json import dump
	conf = {
		CONF_TEMPERATURE:  temperature,
		CONF_FROM_ZERO:    from_zero,
		CONF_BLOCKSIZE:    blocksize,
		CONF_LINEAR_STEPS: linear_steps,
		CONF_NOSE_STEPS:   nose_steps,
		CONF_NVE_STEPS:    nve_steps,
	}
	with open(join(mddir, 'md.conf'), 'w') as f:
		dump(conf, f, indent=1)

def _main(mddir, *, temperature, from_zero, blocksize, linear_steps, nose_steps, nve_steps, unknown):
	from warnings import warn
	for arg in unknown:
		warn('Unknown key in config: {!r}'.format(arg))

	def vasp_cmd():
		from subprocess import check_call
		check_call(VASP_BIN_NAME, shell=True)

	def iter_stages():
		prevdir = None
		for num in iota(1):
			for stage in (STAGE_LINEAR, STAGE_NOSE, STAGE_NVE):
				curdir = stage_dir_name(num=num, stage=stage)
				yield stage, curdir, prevdir
				prevdir = curdir

	prev_end_temp = 0 if from_zero else temperature
	def do_stage(stage, curdir):
		nonlocal prev_end_temp # Terrible terrible hack
		                       # We just reassign this whenever we can

		with pushd(curdir):
			for _ in runonce('md.has_run'):

				if stage == STAGE_LINEAR:
					do_linear(vasp_cmd, steps=linear_steps, from_temp=prev_end_temp)

				elif stage == STAGE_NOSE:
					do_nose(vasp_cmd, steps=nose_steps)

				elif stage == STAGE_NVE:
					do_nve(vasp_cmd, steps=nve_steps, blocksize=blocksize)
					prev_end_temp = int(stripped_lines(VARFILE_FINAL_TEMP)[0])

				else: assert False, 'complete switch'

	with pushd(mddir):
		for stage, d, prev in iter_stages():
			make_trial_subdir(root='.', name=d, continue_from_name=prev)
			cat_files('INCAR.part', 'INCAR.%s'%stage, dest=join(d,'INCAR'))
			do_stage(stage, d)

def stage_dir_name(*, num, stage):
	return '{}-{}'.format(num,stage)


#-----------------------------------------------------

# Handles creation of non-INCAR input files for a 'sub-trial'
def make_trial_subdir(root, name, continue_from_name=None):

	with pushd(root):
		mkdir(name)
		with pushd(name):
			symlink('../POTCAR', 'POTCAR')
			symlink('../KPOINTS', 'KPOINTS')

		if continue_from_name is None:
			symlink('../POSCAR', join(name, 'POSCAR'))
			copy_if_exists('WAVECAR', join(name, 'WAVECAR'))
		else:
			prev = continue_from_name
			copy_file(join(prev, 'WAVECAR'), join(name, 'WAVECAR'))
			copy_file(join(prev, 'CONTCAR'), join(name, 'POSCAR'))


#-------------------------------------
# FIXME outdated comment
# init methods
# Called on a stage directory once it has POSCAR, KPOINTS, INCAR, and POTCAR
#  to perform initialization specific to the stage type.
# Might be used to perform additional substitutions into the INCAR file, or to
#  set up a sequence of trials for a multipart run.

def do_linear(vasp_cmd, *, steps, from_temp):
	file_subst('INCAR', STEPS_REPL, steps)
	file_subst('INCAR', TEBEG_REPL, from_temp)

	vasp_cmd()

def do_nve(vasp_cmd, *, steps, blocksize):
	for _ in runonce('nve.has_init'):
		# set up a series run
		fullblocks, remainder = divmod(steps, blocksize)
		extrablock = (1 if remainder else 0)

		part_names = ['{:03d}'.format(i+1) for i in range(fullblocks + extrablock)]
		part_sizes = [blocksize]*fullblocks + [remainder]*extrablock
		assert len(part_names) == len(part_sizes)
		assert sum(part_sizes) == steps

		with open(VARFILE_SERIES_ALLDIRS, 'wt') as f:
			f.writelines('%s\n' % s for s in part_names)

		for name,size in zip(part_names, part_sizes):
			mkdir(name)
			with pushd(name):
				copy_file('../INCAR', 'INCAR')
				file_subst('INCAR', STEPS_REPL, size)

	# run
	for _ in runonce('nve.has_run'):
		do_series(vasp_cmd)

	# finalize
	lastdir = stripped_lines(VARFILE_SERIES_ALLDIRS)[-1]
	temperature = read_final_temp(join(lastdir, 'OSZICAR'))
	with open(VARFILE_FINAL_TEMP, 'wt') as f:
		f.write('%s\n'%temperature)

def do_nose(vasp_cmd, *, steps):
	file_subst('INCAR', STEPS_REPL, steps)

	vasp_cmd()



def read_final_temp(oszicar):
	# lazy hacky un-robust way
	with open(oszicar, 'rt') as f:
		temperature = None
		for line in f:
			if 'T=' in line:
				# third word
				word = line.strip().split()[2]

				assert word[-1] == '.'
				word = word[:-1]

				try: temperature = int(word)
				except ValueError: pass

		if temperature is None:
			raise RuntimeError('read_final_temp failed to match pattern')
		return temperature

# Input files:
#   ./series.alldirs
#   ./POSCAR
#   ./WAVECAR (optional)
#   ./POTCAR
#   ./KPOINTS
#   ./(each entry in series.alldirs)/INCAR
#   Possibly some output files in the individual entry dirs if
#     we're continuing a previously interrupted run
# Output files
#   ./(each entry in series.alldirs)/(typical outputs)
#   ./WAVECAR.in   (= the original WAVECAR)
#   ./WAVECAR      (= the finished WAVECAR)
#   ./CONTCAR
def do_series(vasp_cmd):
	from os import rename
	dirs = stripped_lines(VARFILE_SERIES_ALLDIRS)

	for _ in runonce('series.has_init'):
		# Make WAVECAR.in
		if not exists('WAVECAR.in'):
			if exists('WAVECAR'):
				rename('WAVECAR', 'WAVECAR.in')
			else:
				touch('WAVECAR.in')

		for d in dirs:
			with pushd(d):
				symlink('../KPOINTS', 'KPOINTS')
				symlink('../POTCAR', 'POTCAR')

	# first sub-trial
	with pushd(dirs[0]):
		for _ in runonce('entry.finished'):
			copy_file('../POSCAR', 'POSCAR')
			copy_file('../WAVECAR.in', 'WAVECAR')
			vasp_cmd()

	# subsequent sub-trials
	for prev, cur in window2(dirs):
		for _ in runonce(join(cur, 'entry.finished')):
			copy_file(join(prev, 'CONTCAR'), join(cur, 'POSCAR'))
			copy_file(join(prev, 'WAVECAR'), join(cur, 'WAVECAR'))
			with pushd(cur):
				vasp_cmd()

	# finalize
	copy_file(join(dirs[-1], 'WAVECAR'), 'WAVECAR')
	copy_file(join(dirs[-1], 'CONTCAR'), 'CONTCAR')




#-------------------------------------------

def iota(start=0):
	i = start
	while True:
		yield i
		i += 1

def file_subst(path, old, new):
	new = str(new)

	with open(path) as f:
		s = f.read()

	s = s.replace(old, new)
	with open(path, 'w') as f:
		f.write(s)

def cat_files(*srcs, dest=None):
	lines = []
	for src in srcs:
		with open(src) as f:
			lines.extend(f)
		lines.append('\n')

	if dest:
		with open(dest, 'wt') as f:
			f.writelines(lines)
	return lines

def window2(it):
	it = iter(it)
	prev = next(it)
	while True:
		x = next(it)
		yield prev,x
		prev = x

#------------------------------------------------
# file utils

# Get the stripped, non-empty lines from a file,
#  as a list of strings
def stripped_lines(path):
	with open(path, 'rt') as f:
		lines = [s.strip() for s in f]
		lines = [s for s in lines if s]
		return lines

# like ln -sf
def symlink(src, dest):
	from os import symlink as _symlink, unlink
	if exists(dest):
		unlink(dest)
	_symlink(src, dest)

# like mkdir -p
def mkdir(path):
	from os import mkdir as _mkdir
	if exists(path) and isdir(path):
		return # nothing to do
	_mkdir(path)

# like cp -T
def copy_file(src, dest):
	from shutil import copyfile
	copyfile(src, dest)

# like cp -fT
def copy_if_exists(src, dest):
	if exists(src):
		copy_file(src, dest)

# touch. might not update timestamps
def touch(path):
	with open(path, 'a'):
		pass

#----------------------------------------------------
# Some very un-Pythonic syntax hacks in an attempt to make the code
#  easier to read and verify

# TODO kill 'runonce', it increases the number of paths through the code
#   and actually makes it MORE difficult to reason about correctness

# Conditionally perform actions only if a certain file does not exist,
#  and create it once we're done.
#
# The way it works is as an iterator which creates the file on the
#  second iteration (and which does zero iterations if the file already
#  exists)
#
# Use it like this:
#     for _ in runonce('./has_initialized'):
#         ... # do things if the file 'has_initialized' does not exist
#     # upon exiting the block successfully, the file is created.
#
def runonce(markerfile):
	if not exists(markerfile):
		yield None # do one iteration
		with open(markerfile, 'a'):
			pass # create file

# Like a shell pushd/popd pair
# Use via 'with' syntax, like this:
#
#     with pushd('some_dir'):
#         ... # do things inside the directory
from os import getcwd,chdir
class pushd:
	def __init__(self, path):
		self.path = path
		self.prev = getcwd()
	def __enter__(self):
		chdir(self.path)
		print('entered: {}'.format(getcwd()))
	def __exit__(self, exc_type, exc_val, traceback):
		chdir(self.prev)
		print('exited to: {}'.format(getcwd()))

if __name__ == '__main__':
	main()

