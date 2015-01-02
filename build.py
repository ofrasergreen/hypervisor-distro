#!/usr/bin/env python

from __future__ import print_function

import yaml
import json
import os
import sys
import io
import hashlib
import docker
import urllib
import jinja2
import shutil
import subprocess
import copy

docker_client = docker.Client(base_url='tcp://localhost:4243')

class Log:
	def __init__(self, key):
		self.len = 0
		self.key = key
		self.status = None
		self.progress = None
		self.show()

	def set_status(self, status):
		self.status = status
		self.progress = None
		self.show()

	def set_progress(self, progress):
		n = int(progress * 42)
		#print(n)
		if n != self.progress:
			self.progress = n
			self.show()

	def done(self):
		self.progress = None
		self.show()
		print('')

	def show(self):
		# Backspace
		print('\r' * self.len, end='')

		line = self.key
		if self.status:
			line = "%s: %s" % (line, self.status)
		if self.progress:
			line = "%s (%s%s)" % (line, '#' * self.progress, '_' * (42 - self.progress))
		spaces = self.len - len(line)
		self.len = len(line)

		print(line, end='')

		if spaces > 0:
			print(' ' * spaces + '\r' * spaces, end='')

		sys.stdout.flush()


class Template:
	def __init__(self, desc):
                self.context = copy.deepcopy(desc)
		self.add_context('pass1_dir', '/pass1')
                self.add_context('target', 'x86_64-hypervisor-linux-gnu')

	def add_context(self, k, v):
		if k is not None:
			self.context[k] = v

	def render(self, src):
		def render_str(s):
			return jinja2.Template(s).render(self.context)

		if src is None:
			return None
		elif isinstance(src, list):
			return map(render_str, src)
		else:
			return render_str(src)

class Package:
	def __init__(self, build_dir, yml):
		self.yml = yml
		self.desc = yaml.load(yml)
		self.name = self.desc['name']
		self.template = Template(self.desc)
		#print(yaml)
		
		self.dependencies = self.desc.get('dependencies', [])
		self.pass1 = self.template.render(self.desc.get('pass1'))
		dl = self.template.render(self.desc.get('download'))
		if dl is None:
			self.download = None
		else:
			self.download = Download(build_dir, dl, self.name)

	def __repr__(self):
		return "Package(name='" + self.name + "')"

	def dependencies_satisfied(self, packages):
		for d in self.dependencies:
			found = False
			for p in packages:
				if d == p.name:
					found = True
					break
			if not found:
				return False
		return True


class Download:
	def __init__(self, build_dir, url, name):
		self.url = url
                self.name = name
		self.filename = build_dir.downloads + '/' + url.split('/')[-1]
		self.build_dir = build_dir

	def run(self, log):
		if not os.path.exists(self.filename):
			self.download_url(log)

		self.unpack(log)

	def unpack(self, log):
		log.set_status('unpacking')
		self.build_dir.prepare_source(self.name)
                f = str(self.filename)
		if f.endswith('.tar.gz') or f.endswith('.tar.bz2') or f.endswith('.tar.xz'):
			subprocess.call(['tar', '--directory=' + self.build_dir.source, '-xf', self.filename])
                else:
			print("Uncompressing %s not supported" % self.filename)
			sys.exit(-1)

	def download_url(self, log):
		def progress(blocks_transferred, block_size, file_size):
			if file_size > 0:
				p = float(blocks_transferred * block_size) / float(file_size)
				#print(p)
				log.set_progress(p)

		log.set_status('downloading')
                try:
                        urllib.urlretrieve(self.url, self.filename, progress)
                except IOError as e:
                        print("Download of '%s' failed: %s" % (self.url, e), file=\
sys.stderr)
                        sys.exit(-1)
              
                        
	
class BuildDir:
	def __init__(self, path):
		if not os.path.exists(path):
			print("Build directory does not exists: " + path, file=sys.stderr)
			sys.exit(-1)

		self.path = path
		self.downloads = path + '/downloads'
		self.source = path + '/source'
		if not os.path.exists(self.downloads):
			os.mkdir(self.downloads)

	def prepare_source(self, name):
                if os.path.exists(self.source):
                        shutil.rmtree(self.source)
                if os.path.exists(name):
                        shutil.copytree(name, self.source)
                else:
                        os.mkdir(self.source)

class Build:
	def __init__(self, repository, base, sha1, build, build_dir, docker_build = None, download = None):
		self.repository = repository
		self.sha1 = sha1
		self.image = repository + ":" + sha1
		self.base = base
		self.build = build
		self.build_dir = build_dir
		self.docker_build = docker_build
		self.download = download

	def run(self):
		log = Log(self.repository)
		try:
			docker_client.history(self.image)
			log.set_status('skipping')
		except docker.errors.APIError as e:
			if self.download:
				self.download.run(log)
			log.set_status('building')

			if (self.docker_build):
				self.build_docker(self.docker_build)
			else:
				self.create_docker(log)
		log.done()


	def create_docker(self, log):
		build_script = open(self.build_dir.source + '/' + self.repository, 'w')
		build_script.write('''
#!/bin/bash
set +h
set -xeuo pipefail
LC_ALL=POSIX
PATH=/tools/bin:/bin:/usr/bin
export LC_ALL PATH
cp -r * /work
cd /work

''')
		for step in self.build:
			build_script.write(step + '\n')
		build_script.write('''
rm -fr /work/*
''')

		build_script.close()

		container = docker_client.create_container(image=self.base, command=self.repository, detach=False)
		container_id = container.get('Id')
		docker_client.start(container=container_id, binds = {
			self.build_dir.source: {
				'bind': '/source',
				'ro': False
			}
			})
		exit_code = docker_client.wait(container=container_id)
		if exit_code != 0:
			log.set_status('Container %s failed with exit code %s' % (container_id, exit_code))
			log.done()
			sys.exit(-1)

		log.set_status("committing")
		docker_client.commit(container=container_id, repository=self.repository, tag=self.sha1)

	def build_docker(self, docker_build):
		dockerfile = '''
FROM %s
%s

RUN %s
	''' % (self.base, '\n'.join(docker_build), ' && '.join(self.build))
		f = io.BytesIO(dockerfile.encode('utf-8'))
		for line in docker_client.build(fileobj=f, tag=self.image):
			out = json.loads(line)
			if 'stream' in out:
				print("    " + out['stream'], end="")
			elif 'errorDetail' in out:
				print("    " + out['errorDetail']['message'], file=sys.stderr)
			#print(out)



if os.getenv('BUILD_DIR') is None:
	print("Please set BUILD_DIR environment variable", file=sys.stderr)
	sys.exit(-1)
build_dir = BuildDir(os.getenv('BUILD_DIR'))

packages = []
for fn in os.listdir('.'):
	if os.path.isfile(fn) and fn.endswith(".yml"):
		with open(fn) as f:
			p = Package(build_dir, f.read())
			packages.append(p)

sha1 = hashlib.sha1()

# pass1
pass1_base_build = ['yum -y groupinstall "Development tools"', 'mkdir /pass1 /tools /work']
sha1.update(str(pass1_base_build))
repository = 'pass1'
build = Build(repository, 'centos:latest', sha1.hexdigest(), pass1_base_build, build_dir,
	docker_build=['MAINTAINER Owen Fraser-Green <owen@fraser-green.com>', 
	'ENTRYPOINT ["/bin/bash"]',
	'VOLUME ["/source"]',
	'WORKDIR /source'])
build.run()
	

pass1_packages = []
changed = True
while changed:
	changed = False
	for p in packages:
		if p in pass1_packages or not p.pass1:
			continue
		if p.dependencies_satisfied(pass1_packages):
			changed = True
			pass1_packages.append(p)

for p in pass1_packages:
	sha1.update(p.yml)
	base = build.image
	build = Build(repository + "-" + p.name, base, sha1.hexdigest(), p.pass1, build_dir, download = p.download)
	build.run()
