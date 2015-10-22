
from __future__ import print_function

import sys
if sys.version_info[0] < 3:
	print('This package does not support python2. Try `python3 setup.py`', file=sys.stderr)
	sys.exit(1)

import subprocess
from setuptools import setup
from setuptools import find_packages

setup(
	name='vaspmd',
	version = '0.0',
	description = 'Personal scripts for automating MD runs in Vasp',
	url = 'https://github.com/ExpHP/vaspmd',
	author = 'Michael Lamparski',
	author_email = 'lampam@rpi.edu',

	entry_points={
		'console_scripts':[
			'md-init = vaspmd.md_init:main',
		],
	},

	scripts=[
		'scripts/md-run',
	],

	install_requires=[
	],

	packages=find_packages(), # include sub-packages
)
