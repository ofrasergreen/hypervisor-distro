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
                self.add_context('target', 'x86_64-hypervisor-linux-gnu')
                self.add_context('build', {'dir': '/build'})
                self.add_context('tools', {'dir': '/tools'})

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
                try:
                        self.desc = yaml.load(yml)
                except Exception as e:
                        print("Failed to read \n---%s\n---\n%s" % (yml, e), file=sys.stderr)
                        sys.exit(-1)
              
		self.name = self.desc['name']
		self.template = Template(self.desc)
		#print(yaml)		
		self.build = self.desc.get('build', {})
		dl = self.template.render(self.desc.get('download'))
		if dl is None:
			self.download = None
		else:
			self.download = Download(build_dir, dl, self.name)

	def __repr__(self):
		return "Package(name='" + self.name + "')"

        def build_steps(self, stage):
                return self.template.render(self.build.get('common', {}).get('steps', []) + self.build.get(stage, {}).get('steps', []))

	def dependencies_satisfied(self, packages, step):
                deps = self.build.get(step).get('dependencies', [])
		for d in deps:
			found = False
			for p in packages:
				if d == p.name:
					found = True
					break
			if not found:
				return False
		return True


class Download:
	def __init__(self, build_dir, urls, name):
                if isinstance(urls, list):
			self.urls = urls
                else:
                        self.urls = [urls]
                self.name = name
		self.build_dir = build_dir

        def filename(self, url):
                return self.build_dir.downloads + '/' + url.split('/')[-1]

	def run(self, log):
                for url in self.urls:
                        filename = self.filename(url)
                        if not os.path.exists(filename):
                                self.download_url(log, url, filename)
                                
                        self.unpack(log, filename)

	def unpack(self, log, filename):
		log.set_status('unpacking %s' % filename)
                f = str(filename)
		if f.endswith('.tar.gz') or f.endswith('.tar.bz2') or f.endswith('.tar.xz'):
			subprocess.call(['tar', '--directory=' + self.build_dir.source, '-xf', filename])
                else:
			print("Uncompressing %s not supported" % filename)
			sys.exit(-1)

	def download_url(self, log, url, filename):
                def progress(blocks_transferred, block_size, file_size):
			if file_size > 0:
				p = float(blocks_transferred * block_size) / float(file_size)
				#print(p)
				log.set_progress(p)

		log.set_status('downloading')
                try:
                        urllib.urlretrieve(url, filename, progress)
                except IOError as e:
                        print("Download of '%s' failed: %s" % (url, e), file=sys.stderr)
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
	def __init__(self, name, repository, base, sha1, steps, build_dir, docker_build = None, download = None):
                self.name = name
		self.repository = repository
		self.sha1 = sha1
		self.image = repository + ":" + sha1
		self.base = base
		self.steps = steps
		self.build_dir = build_dir
		self.docker_build = docker_build
		self.download = download

	def run(self):
		self.build_dir.prepare_source(self.name)
		log = Log(self.repository)
		try:
			docker_client.history(self.image)
			log.set_status('skipping')
		except docker.errors.APIError as e:
			if self.download:
				self.download.run(log)
			log.set_status('building')

			if (self.docker_build):
				self.build_docker(self.base, self.docker_build, self.steps)
			else:
				self.create_docker(log)
		log.done()


	def create_docker(self, log):
                build_script_file = self.build_dir.source + '/' + self.repository
		build_script = open(build_script_file, 'w')
                if self.repository.startswith('tools'):
                        path = "/tools/bin:/bin:/usr/bin"
                else:
                        path = "/bin:/usr/bin:/sbin:/usr/sbin:/tools/bin"

		build_script.write('''
#!/bin/bash
set +h
set -xeuo pipefail
LC_ALL=POSIX
PATH=''' + path + '''
export LC_ALL PATH
mkdir -v /work
cp -ar * /work
cd /work

''')
		for step in self.steps:
			build_script.write(step + '\n')
		build_script.write('''
rm -fr /work
''')
                
		build_script.close()
                os.chmod(self.build_dir.source + '/' + self.repository, 0755)

		container = docker_client.create_container(image=self.base, command='/source/' + self.repository, detach=False)
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

                if self.repository == 'tools4-finalize':
                        log.set_status('creating root image')
                        self.build_docker('scratch', [
                                'MAINTAINER Owen Fraser-Green <owen@fraser-green.com>', 
                                'VOLUME ["/source"]',
                                'ADD tools.tar.bz2 /',
                                'ENV HOME /source',
                                'ENV PATH /bin:/usr/bin:/sbin:/usr/sbin:/tools/bin',
                                'WORKDIR /source',
                                'ENTRYPOINT ["/tools/bin/bash", "-c"]',
                                'CMD ["/tools/bin/bash"]'
                                ])
                else:
                        log.set_status("committing")
                        docker_client.commit(container=container_id, repository=self.repository, tag=self.sha1)


	def build_docker(self, base, docker_build, steps=[]):
		dockerfile = open(self.build_dir.source + '/Dockerfile', 'w')
                dockerfile.write('''
FROM %s
%s
''' % (base, '\n'.join(docker_build)))

                if steps:
                        dockerfile.write('''
RUN %s
''' % ' && '.join(steps))
		dockerfile.close()

                print()
                print("tag: %s" % self.image)
		for line in docker_client.build(path=self.build_dir.source,tag=self.image):
			out = json.loads(line)
			if 'stream' in out:
				print("    " + out['stream'], end="")
			elif 'errorDetail' in out:
				print("    " + out['errorDetail']['message'], file=sys.stderr)

        def tag_stage(self, stage):
                tag = "latest"
                docker_client.tag(image=self.image, repository=stage, tag=tag)
                return stage + ':' + tag

class BuildPipeline:
        def __init__(self, stages, packages):
                self.sha1 = hashlib.sha1()
                self.stages = stages
                self.packages = packages

        def run(self):
                build = None
                for stage in self.stages:
                        build = self.run_stage(stage, build)

        def run_stage(self, stage, build):
                if stage == 'base':
                        return self.build_base()
                else:
                        packages = self.stage_packages(stage)
                        return self.build_packages(stage, packages, build)

        def stage_packages(self, stage):
                packages = []
                changed = True
                while changed:
                        changed = False
                        for p in self.packages:
                                if p in packages or stage not in p.build:
                                        continue
                                if p.dependencies_satisfied(packages, stage):
                                        changed = True
                                        packages.append(p)
                return packages

        def build_packages(self, stage, packages, base):
                build = None
                for p in packages:
                        self.sha1.update(p.yml)
                        build = Build(p.name, stage + "-" + p.name, base, self.sha1.hexdigest(), p.build_steps(stage), build_dir, download = p.download)
                        base = build.image
                        build.run()
                return build.tag_stage(stage)

        def build_base(self):
                tools_base_build = ['yum -y groupinstall "Development tools"', 
                                    'yum -y install autoconf automake patch gcc-c++ bison diffutils findutils gawk grep m4 sed flex flex-devel bison-devel file',
                                    'mkdir -pv /build/tools /work', 
                                    'mkdir -v /build/tools/lib',
                                    'ln -sv /build/tools /',
                                    'ln -sv /tools/lib /tools/lib64']
                self.sha1.update(str(tools_base_build))
                repository = 'base'
                build = Build(repository, repository, 'fedora:20', self.sha1.hexdigest(), tools_base_build, build_dir,
                              docker_build=['MAINTAINER Owen Fraser-Green <owen@fraser-green.com>', 
                                            'ENTRYPOINT ["/bin/bash"]',
                                            'VOLUME ["/source"]',
                                            'WORKDIR /source'])
                build.run()
                return build.tag_stage(repository)
                

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

BuildPipeline(['base', 'tools1', 'tools2', 'tools3', 'tools4', 'system1'], packages).run()
