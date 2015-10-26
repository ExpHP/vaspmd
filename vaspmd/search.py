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
#   * everything in one module (pylint's argument checker can't cross module boundaries)
#   * long argument lists (to allow linting against unused arguments)
#
# I am not proud.

from os.path import join, exists, isdir, relpath

# This script is componentized to interface with 3 other scripts:
#
# cmd-init:
#    Initialize input files for computation with a specific value for the search parameter.
#    It is invoked as follows:
#
#        cmd-init  TRIALNAME  VALUE
#
#    and is expected to create the directory ./TRIALNAME and set up input files within it.
#    The current directory will contain links to any files specified in the 'files' config option.
#
#    cmd-init must be prepared for the circumstance where the directory has already been previously
#    initialized and contains incomplete output from a previously interrupted run.  This is most
#    easily achieved by restricting it to idempotent operations (like 'mkdir -p', 'ln -sf',
#    'rm -f'), and to have it clean up any invalid files that could present an obstacle to running
#    the trial.
#
# cmd-run:
#    A command to run a calculation in the current directory (which was constructed by cmd-init),
#    and produce output files of some form.
#
# cmd-next:
#    Reviews output from a collection of trials and determines the next range to search.
#    It is invoked in the parent directory of the individual trial dirs, as
#
#        cmd-next  TRIALNAME...
#
#    And is expected to write two floating point values (freely formatted) to stdout
#    in the form MINVAL MAXVAL .
#
# In all cases, the command string will be tokenized according to shell syntax, so a setting such
#  as ``cmd-init = "./init.sh 'hello world' -v"`` is perfectly acceptable (assuming ./init.sh takes
#  3 positional arguments)

CONF_START_MIN = 'start-min'
CONF_START_MAX = 'start-max'
CONF_NPOINTS   = 'npoints'
CONF_CMD_RUN   = 'cmd-run'
CONF_CMD_INIT  = 'cmd-init'
CONF_CMD_NEXT  = 'cmd-next'
CONF_FILES     = 'files'

START_NUM = 1

VARFILE_ALLDIRS = 'search.leaves'

def main():
	from argparse import ArgumentParser
	from pytoml import load
	parser = ArgumentParser()
	parser.parse_args()

	try:
		with open('search.toml') as f:
			conf = load(f)
	except FileNotFoundError:
		parser.error('missing search.toml!')

	_main(
		start_min = conf.pop(CONF_START_MIN),
		start_max = conf.pop(CONF_START_MAX),
		npoints   = conf.pop(CONF_NPOINTS),
		cmd_init  = conf.pop(CONF_CMD_INIT),
		cmd_next  = conf.pop(CONF_CMD_NEXT),
		cmd_run   = conf.pop(CONF_CMD_RUN),
		files     = conf.pop(CONF_FILES),
		unknown   = conf,
	)

def _main(*, start_min, start_max, npoints, cmd_init, cmd_next, cmd_run, files, unknown):
	from warnings import warn
	for arg in unknown:
		warn('Unknown key in config: {!r}'.format(arg))

	def dirname(depth):
		return 'set-{:03d}'.format(depth)

	# state tuple contents:
	#   depth: Current search depth (increases each iteration)
	#   {min,max}val: Range being searched at this depth
	#   curdir: Corresponding directory for this depth (included so we can change the dirname
	#            format string without wrecking an incomplete subsearch)
	#   leaves: A list of all previous 'leaf' nodes in the computation tree; these are
	#            directories where vasp was run directly, and where you will find e.g.
	#            vasprun.xml and OSZICAR files
	def do_iter(depth=1, minval=start_min, maxval=start_max, curdir=dirname(1), leaves=()):

		mkdir(curdir)
		with pushd(curdir):
			for name in files:
				symlink(join('..', name), name)

		with pushd(curdir):
			newleaves = do_subsearch(cmd_run, minval=minval, maxval=maxval, npoints=npoints,
				cmd_init=cmd_init)

			newmin,newmax = invoke_cmd_next(cmd_next, newleaves)

			# we ultimately want these saved as paths relative to the md root dir
			newleaves = tuple([relpath(x, '..') for x in newleaves])

		leaves += tuple(newleaves)
		write_lines(leaves, VARFILE_ALLDIRS)

		return (depth+1, newmin, newmax, dirname(depth+1), leaves)

	persistent_loop(do_iter, path='search.state')

def do_subsearch(cmd_run, *, minval, maxval, npoints, cmd_init):
	from numpy import linspace # noqa

	# These values are only used if this is our first time running the stage.
	# When resuming an interrupted run, we use the names/sizes originally chosen for that run.
	names_if_new = ['{:03d}'.format(i+1) for i in range(npoints)]
	values_if_new = list(map(float, linspace(minval, maxval, npoints)))
	assert len(names_if_new) == len(values_if_new)

	def do_iter(i=0, values=values_if_new, names=names_if_new): # pylint: disable=dangerous-default-value
		if i == len(values):
			# let code after the loop know the names that were actually used,
			# since they may differ from `names_if_new`
			return EndLoop(names)

		name, value = names[i], values[i]

		# [Re]generate trial directory
		invoke_cmd_init(cmd_init, name, value)

		with pushd(name):
			invoke_cmd_run(cmd_run)

		return i+1, values, names

	true_names = persistent_loop(do_iter, path='subsearch.state')
	return true_names

#-----------------------------------------------------

def invoke_cmd_next(cmd_next, dirnames):
	assert isinstance(cmd_next, str)
	assert not isinstance(dirnames, str)
	from shlex import split
	from subprocess import Popen, PIPE
	args = split(cmd_next)
	args.extend(dirnames)
	(out, _) = Popen(args, stdout=PIPE).communicate()

	words = out.split()
	floats = list(map(float, words))

	try: minval,maxval = floats
	except ValueError:
		with open('bad_next.out', 'wb') as f:
			f.write(out)
		raise RuntimeError('cmd_next did not produce two floats! Output of cmd_next logged to bad_next.out')

	return minval,maxval

def invoke_cmd_init(cmd_init, dirname, value):
	assert isinstance(cmd_init, str)
	assert isinstance(dirname, str)
	from shlex import split
	from subprocess import check_call
	args = split(cmd_init)
	args.append(dirname)
	args.append(str(value))
	check_call(args)

def invoke_cmd_run(cmd_run):
	assert isinstance(cmd_run, str)
	from subprocess import check_call
	check_call(cmd_run, shell=True)

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

