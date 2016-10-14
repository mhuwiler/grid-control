# | Copyright 2009-2016 Karlsruhe Institute of Technology
# |
# | Licensed under the Apache License, Version 2.0 (the "License");
# | you may not use this file except in compliance with the License.
# | You may obtain a copy of the License at
# |
# |     http://www.apache.org/licenses/LICENSE-2.0
# |
# | Unless required by applicable law or agreed to in writing, software
# | distributed under the License is distributed on an "AS IS" BASIS,
# | WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# | See the License for the specific language governing permissions and
# | limitations under the License.

import os, glob, time, shlex, shutil, tempfile
from grid_control import utils
from grid_control.backends.aspect_cancel import CancelAndPurgeJobs, CancelJobs
from grid_control.backends.broker_base import Broker
from grid_control.backends.wms import BackendError, BasicWMS, WMS
from grid_control.utils.activity import Activity
from grid_control.utils.file_objects import VirtualFile
from grid_control.utils.process_base import LocalProcess
from grid_control.utils.thread_tools import GCLock
from hpfwk import AbstractError, ExceptionCollector
from python_compat import ifilter, imap, ismap, lchain, lfilter, lmap


local_purge_lock = GCLock()

class SandboxHelper(object):
	def __init__(self, config):
		self._cache = []
		self._path = config.get_path('sandbox path', config.get_work_path('sandbox'), must_exist = False)
		utils.ensure_dir_exists(self._path, 'sandbox base', BackendError)

	def get_path(self):
		return self._path

	def get_sandbox(self, gc_id):
		# Speed up function by caching result of listdir
		def searchSandbox(source):
			for path in imap(lambda sbox: os.path.join(self._path, sbox), source):
				if os.path.exists(os.path.join(path, gc_id)):
					return path
		result = searchSandbox(self._cache)
		if result:
			return result
		oldCache = self._cache[:]
		self._cache = lfilter(lambda x: os.path.isdir(os.path.join(self._path, x)), os.listdir(self._path))
		return searchSandbox(ifilter(lambda x: x not in oldCache, self._cache))


class LocalPurgeJobs(CancelJobs):
	def __init__(self, config, sandbox_helper):
		CancelJobs.__init__(self, config)
		self._sandbox_helper = sandbox_helper

	def execute(self, wms_id_list, wms_name): # yields list of purged (wms_id,)
		activity = Activity('waiting for jobs to finish')
		time.sleep(5)
		for wms_id in wms_id_list:
			path = self._sandbox_helper.get_sandbox('WMSID.%s.%s' % (wms_name, wms_id))
			if path is None:
				self._log.warning('Sandbox for job %r could not be found', wms_id)
				continue
			local_purge_lock.acquire()
			try:
				shutil.rmtree(path)
			except Exception:
				self._log.critical('Unable to delete directory %r: %r', path, os.listdir(path))
				local_purge_lock.release()
				raise BackendError('Sandbox for job %r could not be deleted', wms_id)
			local_purge_lock.release()
			yield (wms_id,)
		activity.finish()


class LocalWMS(BasicWMS):
	config_section_list = BasicWMS.config_section_list + ['local']

	def __init__(self, config, name, submitExec, check_executor, cancel_executor, nodesFinder = None, queuesFinder = None):
		config.set('broker', 'RandomBroker')
		config.set_int('wait idle', 20)
		config.set_int('wait work', 5)
		self.submitExec = submitExec
		self._sandbox_helper = SandboxHelper(config)
		BasicWMS.__init__(self, config, name, check_executor = check_executor,
			cancel_executor = CancelAndPurgeJobs(config, cancel_executor, LocalPurgeJobs(config, self._sandbox_helper)))

		def getNodes():
			if nodesFinder:
				return lmap(lambda x: x['name'], self._nodes_finder.discover())

		self.brokerSite = config.get_plugin('site broker', 'UserBroker', cls = Broker,
			inherit = True, tags = [self], pargs = ('sites', 'sites', getNodes))

		def getQueues():
			if queuesFinder:
				result = {}
				for entry in queuesFinder.discover():
					result[entry.pop('name')] = entry
				return result

		self.brokerQueue = config.get_plugin('queue broker', 'UserBroker', cls = Broker,
			inherit = True, tags = [self], pargs = ('queue', 'queues', getQueues))

		self.scratchPath = config.get_list('scratch path', ['TMPDIR', '/tmp'], on_change = True)
		self.submitOpts = config.get('submit options', '', on_change = None)
		self.memory = config.get_int('memory', -1, on_change = None)


	# Submit job and yield (jobnum, WMS ID, other data)
	def _submit_job(self, jobnum, module):
		activity = Activity('submitting job %d' % jobnum)

		try:
			sandbox = tempfile.mkdtemp('', '%s.%04d.' % (module.task_id, jobnum), self._sandbox_helper.get_path())
		except Exception:
			raise BackendError('Unable to create sandbox directory "%s"!' % sandbox)
		sbPrefix = sandbox.replace(self._sandbox_helper.get_path(), '').lstrip('/')
		def translateTarget(d, s, t):
			return (d, s, os.path.join(sbPrefix, t))
		self._sm_sb_in.doTransfer(ismap(translateTarget, self._get_in_transfer_info_list(module)))

		self._write_job_config(os.path.join(sandbox, '_jobconfig.sh'), jobnum, module, {
			'GC_SANDBOX': sandbox, 'GC_SCRATCH_SEARCH': str.join(' ', self.scratchPath)})
		reqs = self.brokerSite.brokerAdd(module.get_requirement_list(jobnum), WMS.SITES)
		reqs = dict(self.brokerQueue.brokerAdd(reqs, WMS.QUEUES))
		if (self.memory > 0) and (reqs.get(WMS.MEMORY, 0) < self.memory):
			reqs[WMS.MEMORY] = self.memory # local jobs need higher (more realistic) memory requirements

		(stdout, stderr) = (os.path.join(sandbox, 'gc.stdout'), os.path.join(sandbox, 'gc.stderr'))
		job_name = module.get_description(jobnum).job_name
		submit_args = shlex.split(self.submitOpts)
		submit_args.extend(shlex.split(self.getSubmitArguments(jobnum, job_name, reqs, sandbox, stdout, stderr)))
		submit_args.append(utils.get_path_share('gc-local.sh'))
		submit_args.extend(shlex.split(self.get_job_arguments(jobnum, sandbox)))
		proc = LocalProcess(self.submitExec, *submit_args)
		exit_code = proc.status(timeout = 20, terminate = True)
		gc_idText = proc.stdout.read(timeout = 0).strip().strip('\n')
		try:
			gc_id = self.parseSubmitOutput(gc_idText)
		except Exception:
			gc_id = None

		activity.finish()

		if exit_code != 0:
			self._log.warning('%s failed:', self.submitExec)
		elif gc_id is None:
			self._log.warning('%s did not yield job id:\n%s', self.submitExec, gc_idText)
		if gc_id:
			gc_id = self._create_gc_id(gc_id)
			open(os.path.join(sandbox, gc_id), 'w')
		else:
			self._log.log_process(proc)
		return (jobnum, gc_id or None, {'sandbox': sandbox})


	def _get_jobs_output(self, ids):
		if not len(ids):
			raise StopIteration

		activity = Activity('retrieving %d job outputs' % len(ids))
		for gc_id, jobnum in ids:
			path = self._sandbox_helper.get_sandbox(gc_id)
			if path is None:
				yield (jobnum, None)
				continue

			# Cleanup sandbox
			outFiles = lchain(imap(lambda pat: glob.glob(os.path.join(path, pat)), self._output_fn_list))
			utils.remove_files(ifilter(lambda x: x not in outFiles, imap(lambda fn: os.path.join(path, fn), os.listdir(path))))

			yield (jobnum, path)
		activity.finish()


	def _get_sandbox_file_list(self, module, monitor, smList):
		files = BasicWMS._get_sandbox_file_list(self, module, monitor, smList)
		for idx, authFile in enumerate(self._token.getAuthFiles()):
			files.append(VirtualFile(('_proxy.dat.%d' % idx).replace('.0', ''), open(authFile, 'r').read()))
		return files


	def checkReq(self, reqs, req, test = lambda x: x > 0):
		if req in reqs:
			return test(reqs[req])
		return False

	def get_job_arguments(self, jobnum, sandbox):
		raise AbstractError

	def getSubmitArguments(self, jobnum, job_name, reqs, sandbox, stdout, stderr):
		raise AbstractError

	def parseSubmitOutput(self, data):
		raise AbstractError


class Local(WMS):
	config_section_list = WMS.config_section_list + ['local']

	def __new__(cls, config, name):
		def createWMS(wms):
			try:
				wmsCls = WMS.get_class(wms)
			except Exception:
				raise BackendError('Unable to load backend class %s' % repr(wms))
			wms_config = config.change_view(view_class = 'TaggedConfigView', set_classes = [wmsCls])
			return WMS.create_instance(wms, wms_config, name)
		wms = config.get('wms', '')
		if wms:
			return createWMS(wms)
		ec = ExceptionCollector()
		for cmd, wms in [('sacct', 'SLURM'), ('sgepasswd', 'OGE'), ('pbs-config', 'PBS'),
				('qsub', 'OGE'), ('bsub', 'LSF'), ('job_slurm', 'JMS')]:
			try:
				utils.resolve_install_path(cmd)
			except Exception:
				ec.collect()
				continue
			return createWMS(wms)
		ec.raise_any(BackendError('No valid local backend found!')) # at this point all backends have failed!
