# | Copyright 2007-2016 Karlsruhe Institute of Technology
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

import time, logging
from grid_control.gc_plugin import ConfigurablePlugin
from grid_control.utils.data_structures import make_enum
from hpfwk import AbstractError, NestedException
from python_compat import irange


class JobError(NestedException):
	pass


class Job(object):
	def __init__(self):
		self.state = Job.INIT
		self.attempt = 0
		self.history = {}
		self.gc_id = None
		self.submitted = 0
		self.changed = 0
		self.dict = {}

	def assign_id(self, gc_id):
		self.gc_id = gc_id
		self.attempt = self.attempt + 1
		self.submitted = time.time()

	def get(self, key, default=None):
		return self.dict.get(key, default)

	def get_dict(self):
		return {'id': self.gc_id, 'status': Job.enum2str(self.state),
			'attempt': self.attempt, 'submitted': self.submitted, 'changed': self.changed}

	def set(self, key, value):
		self.dict[key] = value

	def update(self, state):
		self.state = state
		self.changed = time.time()
		self.history[self.attempt] = self.dict.get('dest', 'N/A')

make_enum(['INIT', 'SUBMITTED', 'DISABLED', 'READY', 'WAITING', 'QUEUED', 'ABORTED',
		'RUNNING', 'CANCEL', 'UNKNOWN', 'CANCELLED', 'DONE', 'FAILED', 'SUCCESS'], Job)


class JobClassHolder(object):
	def __init__(self, *states):
		self.states = states


class JobDB(ConfigurablePlugin):
	def __init__(self, config, job_limit=-1, job_selector=None):
		ConfigurablePlugin.__init__(self, config)
		self._log = logging.getLogger('jobs.db')
		(self._job_limit, self._always_selector) = (job_limit, job_selector)
		(self._default_job_obj, self._path_work) = (Job(), config.get_work_path())

	def __len__(self):
		return self._job_limit

	def commit(self, jobnum, job_obj):
		raise AbstractError

	def get_job(self, jobnum):
		raise AbstractError

	def get_job_len(self, job_selector=None, subset=None):
		return len(self.get_job_list(job_selector, subset))  # fastest method! (iter->list written in C)

	def get_job_list(self, job_selector=None, subset=None):
		return list(self.iter_jobs(job_selector, subset))

	def get_job_persistent(self, jobnum):
		raise AbstractError

	def get_job_transient(self, jobnum):
		raise AbstractError

	def get_work_path(self):  # TODO: only used by report class
		return self._path_work

	def iter_jobs(self, job_selector=None, subset=None):
		if subset is None:
			subset = irange(self._job_limit)

		if job_selector and self._always_selector:
			def select(*args):
				return job_selector(*args) and self._always_selector(*args)
		elif job_selector or self._always_selector:
			select = job_selector or self._always_selector
		else:
			for jobnum in subset:
				yield jobnum
		if job_selector or self._always_selector:
			for jobnum in subset:
				if select(jobnum, self.get_job_transient(jobnum)):
					yield jobnum

	def set_job_limit(self, value):
		self._job_limit = value


class JobClass(JobClassHolder):
	ATWMS = JobClassHolder(Job.SUBMITTED, Job.WAITING, Job.READY, Job.QUEUED, Job.UNKNOWN)
	CANCEL = JobClassHolder(Job.CANCEL)
	DISABLED = JobClassHolder(Job.DISABLED)
	DONE = JobClassHolder(Job.DONE)
	ENDSTATE = JobClassHolder(Job.SUCCESS, Job.DISABLED)
	PROCESSED = JobClassHolder(Job.SUCCESS, Job.FAILED, Job.CANCELLED, Job.ABORTED)
	PROCESSING = JobClassHolder(Job.SUBMITTED, Job.WAITING, Job.READY,
		Job.QUEUED, Job.UNKNOWN, Job.RUNNING)
	RUNNING_DONE = JobClassHolder(Job.RUNNING, Job.DONE)
	FAILING = JobClassHolder(Job.FAILED, Job.ABORTED, Job.CANCELLED)
	SUBMIT_CANDIDATES = JobClassHolder(Job.INIT, Job.FAILED, Job.ABORTED, Job.CANCELLED)
	SUCCESS = JobClassHolder(Job.SUCCESS)
