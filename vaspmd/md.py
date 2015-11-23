#!/usr/bin/env python3

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
#   * long argument lists (to allow linting against unused arguments)
#   * everything in one module (pylint's argument checker can't cross module boundaries)
#
# I am not proud.

from os.path import join, exists, isdir, relpath

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

VARFILE_MD_ALLDIRS     = 'md.leaves'
VARFILE_FINAL_TEMP     = 'md.final-temp'

def main():
	from argparse import ArgumentParser
	from json import load
	parser = ArgumentParser()
	parser.parse_args()

	try:
		with open('md.conf') as f:
			conf = load(f)
	except FileNotFoundError:
		parser.error('missing md.conf!')

	_main(
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

def _main(*, temperature, from_zero, blocksize, linear_steps, nose_steps, nve_steps, unknown):
	from warnings import warn
	for arg in unknown:
		warn('Unknown key in config: {!r}'.format(arg))

	# state tuple contents:
	#   num:      Current iteration of the main loop (which does each stage in order)
	#   stage:    Which stage are we currently on
	#   prevtemp: Temperature of system at end of previous run. (for the linear stage
	#              to start at)
	#   prevdir:  Directory associated with the previous stage, or None.
	#              (used to locate CONTCAR, WAVECAR)
	#   leaves:   A list of all previous 'leaf' nodes in the computation tree; these are
	#              directories where vasp was run directly, and where you will find e.g.
	#              vasprun.xml and OSZICAR files

	initial_temp = 0 if from_zero else temperature
	def do_iter(num=1, stage=STAGE_LINEAR, prevtemp=initial_temp, prevdir=None, leaves=()):

		curdir = stage_dir_name(num=num, stage=stage)
		make_trial_subdir(curdir, prevdir)

		cat_files('INCAR.part', 'INCAR.%s'%stage, dest=join(curdir,'INCAR'))

		with pushd(curdir):
			newleaves = do_stage(do_vasp, stage=stage, prevtemp=prevtemp, blocksize=blocksize,
					linear_steps=linear_steps, nose_steps=nose_steps, nve_steps=nve_steps,
			)

			# we ultimately want these saved as paths relative to the md root dir
			newleaves = tuple([relpath(x, '..') for x in newleaves])

		endtemp  = read_final_temp(join(newleaves[-1], 'OSZICAR'))
		leaves += tuple(newleaves)
		newnum, newstage = next_stage(num=num, stage=stage)

		write_lines(leaves, VARFILE_MD_ALLDIRS)

		return (newnum, newstage, endtemp, curdir, leaves)

	persistent_loop(do_iter, path='md.state')

def next_stage(num, stage):
	if stage == STAGE_LINEAR:  return (num,   STAGE_NOSE)
	elif stage == STAGE_NOSE:  return (num,   STAGE_NVE)
	elif stage == STAGE_NVE:   return (num+1, STAGE_LINEAR)
	else: assert False, 'complete switch'

# Expects to be in a stage directory, with POSCAR/KPOINTS/POTCAR, and an INCAR
#   that still requires substitution for NSW and/or possibly TEBEG
def do_stage(vasp_cmd, *, stage, prevtemp, blocksize, linear_steps, nose_steps, nve_steps):
	if stage == STAGE_LINEAR:
		return do_linear(vasp_cmd, steps=linear_steps, from_temp=prevtemp)
	elif stage == STAGE_NOSE:
		return do_nose(vasp_cmd, steps=nose_steps, blocksize=blocksize)
	elif stage == STAGE_NVE:
		return do_nve(vasp_cmd, steps=nve_steps, blocksize=blocksize)
	else: assert False, 'complete switch'

def stage_dir_name(*, num, stage):
	return '{}-{}'.format(num,stage)


#-----------------------------------------------------

# Handles creation of non-INCAR input files for a 'sub-trial'
def make_trial_subdir(name, continue_from_name=None):
	from os.path import sep
	if sep in name:
		raise ValueError('name must be a single path component, not {!r}'.format(name))

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
# 'do_x' functions
# Similar to VASP, these expect a POSCAR, KPOINTS, INCAR, and POTCAR in the current directory,
#  and produce at minimum a WAVECAR and a CONTCAR.
# They may perform initialization specific to the stage type (such as additional substitutions
#  into INCAR), and they may or may not further divide their work up into multiple VASP runs.
#  They return a list of "leaf" directories (as paths relative to '.') where VASP was run directly.

# TODO need some easier way to reason about which replacement patterns in INCAR have been replaced/
#  need to be replaced.  I'd imagine that simply turning the REPL globals into function arguments
#  would help immensely.

# NOTE: This is trickier to split up into a "series" computation than the other stages are
#  due to TEBEG and TEEND.  I mean, it isn't impossible, but it would involve duplicating most
#  of the code in `do_series`, as I really don't see a good abstraction for this.
def do_linear(vasp_cmd, *, steps, from_temp):
	file_subst('INCAR', STEPS_REPL, steps)
	file_subst('INCAR', TEBEG_REPL, from_temp)

	vasp_cmd()

	return ['.']

def do_nve(vasp_cmd, *, steps, blocksize):
	# TODO search for decent starting point
	return do_series(vasp_cmd, steps=steps, blocksize=blocksize)

def do_nose(vasp_cmd, *, steps, blocksize):
	return do_series(vasp_cmd, steps=steps, blocksize=blocksize)

# split a simple computation up into multiple steps with identical INCARs (up to NSW), for the
#  purpose of reducing the amount of work lost from an interrupted computation
def do_series(vasp_cmd, *, steps, blocksize):

	# set up a series run
	fullblocks, remainder = divmod(steps, blocksize)
	extrablock = (1 if remainder else 0)

	# These values are only used if this is our first time running the stage.
	# When resuming an interrupted run, we use the names/sizes originally chosen for that run.
	# (thus, it is safe to e.g. modify this script and change the format of the names, and this
	#  will not impact any existing, incomplete runs)
	names_if_new = ['{:03d}'.format(i+1) for i in range(fullblocks + extrablock)]
	sizes_if_new = [blocksize]*fullblocks + [remainder]*extrablock
	assert len(names_if_new) == len(sizes_if_new)
	assert sum(sizes_if_new) == steps

	def do_iter(i=0, sizes=sizes_if_new, names=names_if_new, prev=None):
		if i == len(sizes):
			# let code after the loop know the names that were actually used,
			# since they may differ from `names_if_new`
			return EndLoop(names)

		cur, size = names[i], sizes[i]

		make_trial_subdir(cur, prev)
		with pushd(cur):
			copy_file('../INCAR', 'INCAR')
			file_subst('INCAR', STEPS_REPL, size)

			vasp_cmd()

		return i+1, sizes, names, cur

	true_names = persistent_loop(do_iter, path='series.state')

	# finalize
	copy_file(join(true_names[-1], 'WAVECAR'), 'WAVECAR')
	copy_file(join(true_names[-1], 'CONTCAR'), 'CONTCAR')

	return true_names


def do_vasp():
	from subprocess import check_call
	check_call(VASP_BIN_NAME, shell=True)

#------------------------------------------------

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

def write_lines(lines, path):
	with open(path, 'wt') as f:
		f.writelines('%s\n' % x for x in lines)

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

class EndLoop():
	def __init__(self, value):
		self.value = value

# Make a sort of iterator that records its current state in a file.
#
#  f(*args) -> nextargs  A function performed each iteration, which either returns:
#                         a) A tuple containing the next values for the arguments
#                         b) EndLoop(result), or
#                         c) EndLoop (equivalent to EndLoop(None))
#                        Arguments must be pickleable.
#  path                  Where to save the state.
#
# Use like this:
#
#     # `sum(i for i in range(10))`  written as a persistent loop
#     def do_iter(acc=0, i=0, n=10):
#         if i >= n:
#             return EndLoop(acc)
#         return acc+i, i+1, n
#
#     s = persistent_loop(do_iter, 'numbers')
#
# The function must anticipate the following contingencies:
#   * That any number of iterations at the beginning may be skipped
#     (because they already ran in a previous run of the program)
#   * That the state of the environment at the beginning of any given iteration may
#     reflect changes from a previously interrupted run of the same iteration
#
# One must be cautious when allowing the function to close over variables.
# While it may seem odd to keep passing `n` around in the above snippet, consider the following:
#
#     n = 10
#     def do_iter(acc=0, i=0):
#         if i >= n:
#             return EndLoop(acc)
#         return acc+i, i+1
#
#     s = persistent_loop(do_iter, 'numbers')
#
# These snippets will have different behavior if one were to interrupt a computation and then
#  modify the value of `n`; upon resuming, the first snippet would continue to use 'n=10' (because
#  it is part of the recorded state), while the second snippet would use the new value (because it
#  gathers the value from outside the function).
#
# On that note, please think VERY CAREFULLY before putting a 'nonlocal' declaration in the
#  function.  Chances are it will be much easier to avoid a logic error by simply keeping the
#  variable in the state tuple instead, and returning it as part of the end result.
#
# This is partly the reason why `persistent_loop` has such an unusual API, rather than behaving
#  more like an iterator.  In truth, several iterator-like designs were tried out prior to this
#  design, but each were found to encourage making a variety of logic errors with regards to
#  modification of local variables not preserved in the state tuple.
def persistent_loop(f, path, initialstate=()):
	def load():
		from pickle import load as _load
		with open(path, 'rb') as f:
			return _load(f)

	def save(st):
		from pickle import dump
		from os import rename
		tmppath = path + '.tmp'
		with open(tmppath, 'wb') as f:
			dump(st, f)
		rename(tmppath, path)

	if not exists(path):
		save(initialstate)

	while True:
		state = load()

		if isinstance(state, EndLoop):
			return state.value

		state = f(*state)
		# Support returning just EndLoop (without instantiation)
		if state is EndLoop:
			state = EndLoop(None)

		save(state)

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

