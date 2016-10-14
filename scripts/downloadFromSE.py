#!/usr/bin/env python
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

import os, sys, time, random, logging, gcSupport
from gcSupport import ClassSelector, FileInfoProcessor, Job, JobClass, Options, Plugin
from grid_control.backends.storage import se_copy, se_exists, se_mkdir, se_rm
from grid_control.utils.thread_tools import GCLock, start_daemon
from python_compat import imap, irange, lfilter, lmap, md5


log = logging.getLogger()

def md5sum(filename):
	m = md5()
	# use 4M blocksize:
	blocksize = 4096 * 1024
	f = open(filename, 'r')
	while True:
		s = f.read(blocksize)
		m.update(s)
		if len(s) != blocksize:
			break
	return m.hexdigest()

def parse_cmd_line():
	help_msg = '\n\nDEFAULT: The default is to download the SE file and check them with MD5 hashes.'
	help_msg += '\n * In case all files are transferred sucessfully, the job is marked'
	help_msg += '\n   as already downloaded, so that the files are not copied again.'
	help_msg += '\n * Failed transfer attempts will mark the job as failed, so that it'
	help_msg += '\n   can be resubmitted.'
	parser = Options(usage = '%s [OPTIONS] <config file>' + help_msg)

	def add_boolOpt(group, short_pair, option_base, help_base, default = False,
			option_prefix_pair = ('', 'no'), help_prefix_pair = ('', 'do not '), dest = None):
		def create_opt(idx):
			return str.join('-', option_prefix_pair[idx].split() + option_base.split())
		def create_help(idx):
			help_def = ''
			if (default and (idx == 0)) or ((not default) and (idx == 1)):
				help_def = ' [Default]'
			return help_prefix_pair[idx] + help_base + help_def
		parser.add_flag(group, short_pair, (create_opt(0), create_opt(1)), default = default, dest = dest,
			help_pair = (create_help(0), create_help(1)))

	add_boolOpt(None, 'v ', 'verify-md5',        default = True,  help_base = 'MD5 verification of SE files',
		help_prefix_pair = ('enable ', 'disable '))
	add_boolOpt(None, 'l ', 'loop',              default = False, help_base = 'loop over jobs until all files are successfully processed')
	add_boolOpt(None, 'L ', 'infinite',          default = False, help_base = 'process jobs in an infinite loop')
	add_boolOpt(None, '  ', 'shuffle',           default = False, help_base = 'shuffle download order')
	add_boolOpt(None, '  ', '',                  default = False, help_base = 'files which are already on local disk',
		option_prefix_pair = ('skip-existing', 'overwrite'), help_prefix_pair = ('skip ', 'overwrite '), dest = 'skip_existing')

	parser.section('jobs', 'Job state / flag handling')
	add_boolOpt('jobs', '  ', 'mark-dl',         default = True,  help_base = 'mark sucessfully downloaded jobs as such')
	add_boolOpt('jobs', '  ', 'mark-dl',         default = False, help_base = 'mark about sucessfully downloaded jobs',
		option_prefix_pair = ('ignore', 'use'), help_prefix_pair = ('ignore ', 'use '), dest = 'mark_ignore_dl')
	add_boolOpt('jobs', '  ', 'mark-fail',       default = True,  help_base = 'mark jobs failing verification as such')
	add_boolOpt('jobs', '  ', 'mark-empty-fail', default = False, help_base = 'mark jobs without any files as failed')

	parser.section('file', 'Local / SE file handling')
	for (option, help_base) in [
			('local-ok',   'files of successful jobs in local directory'),
			('local-fail', 'files of failed jobs in local directory'),
			('se-ok',      'files of successful jobs on SE'),
			('se-fail',    'files of failed jobs on the SE'),
		]:
		add_boolOpt('file', '  ', option, default = False, help_base = help_base,
			option_prefix_pair = ('rm', 'keep'), help_prefix_pair = ('remove ', 'keep '))

	parser.add_text(None, 'o', 'output',    default = None,
		help = 'specify the local output directory')
	parser.add_text(None, 'T', 'token',     default = 'VomsProxy',
		help = 'specify the access token used to determine ability to download - VomsProxy or TrivialAccessToken')
	parser.add_list(None, 'S', 'selectSE',  default = None,
		help = 'specify the SE paths to process')
	parser.add_text(None, 'r', 'retry',
		help = 'how often should a transfer be attempted [Default: 0]')
	parser.add_text(None, 't', 'threads',   default = 0,
		help = 'how many parallel download threads should be used to download files [Default: no multithreading]')
	parser.add_text(None, ' ', 'slowdown',  default = 2,
		help = 'specify time between downloads [Default: 2 sec]')
	parser.add_bool(None, ' ', 'show-host', default = False,
		help = 'show SE hostname during download')

	parser.section('short', 'Shortcuts')
	parser.add_fset('short', 'm', 'move',        help = 'Move files from SE - shorthand for:'.ljust(100) + '%s',
		flag_set = '--verify-md5 --overwrite --mark-dl --use-mark-dl --mark-fail --rm-se-fail --rm-local-fail --rm-se-ok --keep-local-ok')
	parser.add_fset('short', 'c', 'copy',        help = 'Copy files from SE - shorthand for:'.ljust(100) + '%s',
		flag_set = '--verify-md5 --overwrite --mark-dl --use-mark-dl --mark-fail --rm-se-fail --rm-local-fail --keep-se-ok --keep-local-ok')
	parser.add_fset('short', 'j', 'just-copy',   help = 'Just copy files from SE - shorthand for:'.ljust(100) + '%s',
		flag_set = '--verify-md5 --skip-existing --no-mark-dl --ignore-mark-dl --no-mark-fail --keep-se-fail --keep-local-fail --keep-se-ok --keep-local-ok')
	parser.add_fset('short', 's', 'smart-copy',
		help = 'Copy correct files from SE, but remember already downloaded files and delete corrupt files - shorthand for: '.ljust(100) + '%s',
		flag_set = '--verify-md5 --mark-dl --mark-fail --rm-se-fail --rm-local-fail --keep-se-ok --keep-local-ok')
	parser.add_fset('short', 'V', 'just-verify', help = 'Just verify files on SE - shorthand for:'.ljust(100) + '%s',
		flag_set = '--verify-md5 --no-mark-dl --keep-se-fail --rm-local-fail --keep-se-ok --rm-local-ok --ignore-mark-dl')
	parser.add_fset('short', 'D', 'just-delete', help = 'Just delete all finished files on SE - shorthand for:'.ljust(100) + '%s',
		flag_set = '--skip-existing --rm-se-fail --rm-se-ok --rm-local-fail --keep-local-ok --no-mark-dl --ignore-mark-dl')

	return parser.parse()


def dlfs_rm(path, msg):
	procRM = se_rm(path)
	if procRM.status(timeout = 60) != 0:
		log.critical('\t\tUnable to remove %s!', msg)
		log.critical('%s\n%s\n', procRM.stdout.read(timeout = 0), procRM.stderr.read(timeout = 0))


def transfer_monitor(output, fileIdx, path, lock, abort):
	path = path.replace('file://', '')
	(csize, osize, stime, otime, lttime) = (0, 0, time.time(), time.time(), time.time())
	while not lock.acquire(False): # Loop until monitor lock is available
		if csize != osize:
			lttime = time.time()
		if time.time() - lttime > 5*60: # No size change in the last 5min!
			output.error('Transfer timeout!')
			abort.acquire()
			break
		if os.path.exists(path):
			csize = os.path.getsize(path)
			output.update_progress(fileIdx, csize, osize, stime, otime)
			(osize, otime) = (csize, time.time())
		else:
			stime = time.time()
		time.sleep(0.1)
	lock.release()


def download_monitored(jobnum, output, fileIdx, checkPath, sourcePath, target_path):
	copyAbortLock = GCLock()
	monitorLock = GCLock()
	monitorLock.acquire()
	monitor = start_daemon('Download monitor %s' % jobnum, transfer_monitor, output, fileIdx, checkPath, monitorLock, copyAbortLock)
	result = -1
	procCP = se_copy(sourcePath, target_path, tmp = checkPath)
	while True:
		if not copyAbortLock.acquire(False):
			monitor.join()
			break
		copyAbortLock.release()
		result = procCP.status(timeout = 0)
		if result is not None:
			monitorLock.release()
			monitor.join()
			break
		time.sleep(0.02)

	if result != 0:
		output.error('Unable to copy file from SE!')
		log.critical('%s\n%s\n', procCP.stdout.read(timeout = 0), procCP.stderr.read(timeout = 0))
		return False
	return True


def download_file(opts, output, jobnum, fileIdx, fileInfo):
	(hash, _, name_dest, pathSE) = fileInfo
	output.update_progress(fileIdx)

	# Copy files to local folder
	outFilePath = os.path.join(opts.output, name_dest)
	if opts.selectSE:
		if not (True in imap(lambda s: s in pathSE, opts.selectSE)):
			output.error('skip file because it is not located on selected SE!')
			return
	if opts.skip_existing and (se_exists(outFilePath).status(timeout = 10) == 0):
		output.error('skip file as it already exists!')
		return
	if se_exists(os.path.dirname(outFilePath)).status(timeout = 10) != 0:
		se_mkdir(os.path.dirname(outFilePath)).status(timeout = 10)

	checkPath = 'file:///tmp/dlfs.%s' % name_dest
	if 'file://' in outFilePath:
		checkPath = outFilePath

	if not download_monitored(jobnum, output, fileIdx, checkPath, os.path.join(pathSE, name_dest), outFilePath):
		return False

	# Verify => compute md5hash
	if opts.verify_md5:
		try:
			hashLocal = md5sum(checkPath.replace('file://', ''))
			if not ('file://' in outFilePath):
				dlfs_rm('file://%s' % checkPath, 'SE file')
		except KeyboardInterrupt:
			raise
		except Exception:
			hashLocal = None
		output.update_hash(fileIdx, hashLocal)
		if hash != hashLocal:
			return False
	else:
		output.update_hash(fileIdx)
	return True


def cleanup_files(opts, files, failJob, output):
	for (fileIdx, fileInfo) in enumerate(files):
		(_, _, name_dest, pathSE) = fileInfo
		# Remove downloaded files in case of failure
		if (failJob and opts.rm_local_fail) or (not failJob and opts.rm_local_ok):
			output.update_status(fileIdx, 'Deleting file %s from local...' % name_dest)
			outFilePath = os.path.join(opts.output, name_dest)
			if se_exists(outFilePath).status(timeout = 10) == 0:
				dlfs_rm(outFilePath, 'local file')
		# Remove SE files in case of failure
		if (failJob and opts.rm_se_fail) or (not failJob and opts.rm_se_ok):
			output.update_status(fileIdx, 'Deleting file %s...' % name_dest)
			dlfs_rm(os.path.join(pathSE, name_dest), 'SE file')
		output.update_status(fileIdx, None)


def download_job_output(opts, incInfo, workDir, job_db, token, jobnum, output):
	output.init(jobnum)
	job_obj = job_db.get_job(jobnum)
	# Only run over finished and not yet downloaded jobs
	if job_obj.state != Job.SUCCESS:
		output.error('Job has not yet finished successfully!')
		return incInfo('Processing')
	if job_obj.get('download') == 'True' and not opts.mark_ignore_dl:
		if not int(opts.threads):
			output.error('All files already downloaded!')
		return incInfo('Downloaded')
	retry = int(job_obj.get('download attempt', 0))
	failJob = False

	if not token.can_submit(20*60, True):
		sys.stderr.write('Please renew access token!')
		sys.exit(os.EX_UNAVAILABLE)

	# Read the file hash entries from job info file
	files = FileInfoProcessor().process(os.path.join(workDir, 'output', 'job_%d' % jobnum)) or []
	if files:
		files = lmap(lambda fi: (fi[FileInfoProcessor.Hash], fi[FileInfoProcessor.NameLocal],
			fi[FileInfoProcessor.NameDest], fi[FileInfoProcessor.Path]), files)
	output.update_files(files)
	if not files:
		if opts.mark_empty_fail:
			failJob = True
		else:
			return incInfo('Job without output files')

	for (fileIdx, fileInfo) in enumerate(files):
		failJob = failJob or not download_file(opts, output, jobnum, fileIdx, fileInfo)

	# Ignore the first opts.retry number of failed jobs
	if failJob and opts.retry and (retry < int(opts.retry)):
		output.error('Download attempt #%d failed!' % (retry + 1))
		job_obj.set('download attempt', str(retry + 1))
		job_db.commit(jobnum, job_obj)
		return incInfo('Download attempts')

	cleanup_files(opts, files, failJob, output)

	if failJob:
		incInfo('Failed downloads')
		if opts.mark_fail:
			# Mark job as failed to trigger resubmission
			job_obj.state = Job.FAILED
	else:
		incInfo('Successful download')
		if opts.mark_dl:
			# Mark as downloaded
			job_obj.set('download', 'True')

	# Save new job status infos
	job_db.commit(jobnum, job_obj)
	output.finish()
	time.sleep(float(opts.slowdown))


def download_multithreaded_main(opts, workDir, jobList, incInfo, job_db, token, DisplayClass, screen, errorOutput):
	(active, todo) = ([], list(jobList))
	todo.reverse()
	screen.move(0, 0)
	screen.save_pos()
	while True:
		screen.erase()
		screen.load_pos()
		active = lfilter(lambda thread_display: thread_display[0].isAlive(), active)
		while len(active) < int(opts.threads) and len(todo):
			display = DisplayClass()
			active.append((start_daemon('Download %s' % todo[-1], download_job_output,
				opts, incInfo, workDir, job_db, token, todo.pop(), display), display))
		for (_, display) in active:
			sys.stdout.write(str.join('\n', display.output))
		sys.stdout.write(str.join('\n', ['=' * 50] + errorOutput))
		sys.stdout.flush()
		if len(active) == 0:
			break
		time.sleep(0.01)


def download_multithreaded(opts, workDir, jobList, incInfo, job_db, token):
	from grid_control_gui.ansi import Console
	errorOutput = []
	class ThreadDisplay:
		def __init__(self):
			self.output = []
		def init(self, jobnum):
			self.jobnum = jobnum
			self.output = ['Job %5d' % jobnum, '']
		def _infoline(self, fileIdx, msg = ''):
			return 'Job %5d [%i/%i] %s %s' % (self.jobnum, fileIdx + 1, len(self._files), self._files[fileIdx][2], msg)
		def update_files(self, files):
			(self._files, self.output, self.tr) = (files, self.output[1:], ['']*len(files))
			for x in irange(len(files)):
				self.output.insert(2*x, self._infoline(x))
				self.output.insert(2*x+1, '')
		def update_progress(self, idx, csize = None, osize = None, stime = None, otime = None):
			if otime:
				trfun = lambda sref, tref: gcSupport.prettySize(((csize - sref) / max(1, time.time() - tref)))
				self.tr[idx] = '%7s avg. - %7s/s inst.' % (gcSupport.prettySize(csize), trfun(0, stime))
				self.output[2*idx] = self._infoline(idx, '(%s - %7s/s)' % (self.tr[idx], trfun(osize, otime)))
		def update_hash(self, idx, hashLocal = None):
			file_hash = self._files[idx][0]
			if hashLocal:
				if file_hash == hashLocal:
					result = Console.fmt('MATCH', [Console.COLOR_GREEN])
				else:
					result = Console.fmt('FAIL', [Console.COLOR_RED])
				msg = '(R:%s L:%s) => %s' % (file_hash, hashLocal, result)
			else:
				msg = ''
			self.output[2*idx] = self._infoline(idx, '(%s)' % self.tr[idx])
			self.output[2*idx+1] = msg
		def error(self, msg):
			errorOutput.append(msg)
		def update_status(self, idx, msg):
			self.output[2*idx] = str.join(' ', [self._infoline(idx, '(%s)' % self.tr[idx])] + (msg or '').split())
		def finish(self):
#			self.output.append(str(self.jobnum) + 'FINISHED')
			pass

	download_multithreaded_main(opts, workDir, jobList, incInfo, job_db, token, ThreadDisplay, Console(sys.stdout), errorOutput)


def download_sequential(opts, workDir, jobList, incInfo, job_db, token):
	class DefaultDisplay:
		def init(self, jobnum):
			sys.stdout.write('Job %d: ' % jobnum)
		def update_files(self, files):
			self._files = files
			sys.stdout.write('The job wrote %d file%s to the SE\n' % (len(files), ('s', '')[len(files) == 1]))
		def update_progress(self, idx, csize = None, osize = None, stime = None, otime = None):
			(_, _, name_dest, pathSE) = self._files[idx]
			if otime:
				tr = lambda sref, tref: gcSupport.prettySize(((csize - sref) / max(1, time.time() - tref)))
				tmp = name_dest
				if opts.show_host:
					tmp += ' [%s]' % pathSE.split('//')[-1].split('/')[0].split(':')[0]
				self._write('\r\t%s (%7s - %7s/s avg. - %7s/s inst.)' % (tmp,
					gcSupport.prettySize(csize), tr(0, stime), tr(osize, otime)))
				sys.stdout.flush()
			else:
				self._write('\t%s' % name_dest)
				sys.stdout.flush()
		def update_hash(self, idx, hashLocal = None):
			file_hash = self._files[idx][0]
			self._write(' => %s\n' % ('\33[0;91mFAIL\33[0m', '\33[0;92mMATCH\33[0m')[file_hash == hashLocal])
			self._write('\t\tRemote site: %s\n' % file_hash)
			self._write('\t\t Local site: %s\n' % hashLocal)
		def error(self, msg):
			sys.stdout.write('\nJob %d: %s' % (jobnum, msg.strip()))
		def update_status(self, idx, msg):
			if msg:
				self._write('\t' + msg + '\r')
			else:
				self._write(' ' * len('\tDeleting file %s from SE...\r' % self._files[idx][2]) + '\r')
		def _write(self, msg):
			sys.stdout.write(msg)
		def finish(self):
			sys.stdout.write('\n')

	for jobnum in jobList:
		download_job_output(opts, incInfo, workDir, job_db, token, jobnum, DefaultDisplay())


def loop_download(opts, args):
	# Init everything in each loop to pick up changes
	(config, job_db) = gcSupport.initGC(args)
	token = Plugin.get_class('AccessToken').create_instance(opts.token, config, 'access')#, OSLayer.create(config))
	workDir = config.get_work_path()
	jobList = job_db.get_job_list(ClassSelector(JobClass.SUCCESS))

	# Create SE output dir
	if not opts.output:
		opts.output = os.path.join(workDir, 'se_output')
	if '://' not in opts.output:
		opts.output = 'file:///%s' % os.path.abspath(opts.output)

	infos = {}
	def incInfo(x):
		infos[x] = infos.get(x, 0) + 1

	if opts.shuffle:
		random.shuffle(jobList)
	else:
		jobList.sort()

	if int(opts.threads):
		download_multithreaded(opts, workDir, jobList, incInfo, job_db, token)
	else:
		download_sequential(opts, workDir, jobList, incInfo, job_db, token)

	# Print overview
	if infos:
		print('\nStatus overview:')
		for (state, num) in infos.items():
			if num > 0:
				print('\t%20s: [%d/%d]' % (state, num, len(jobList)))

	# return True if download is finished
	return ('Downloaded' in infos) and (infos['Downloaded'] == len(job_db))


def main(args):
	(opts, args, _) = parse_cmd_line()

	# Disable loop mode if it is pointless
	if (opts.loop and not opts.skip_existing) and (opts.mark_ignore_dl or not opts.mark_dl):
		sys.stderr.write('Loop mode was disabled to avoid continuously downloading the same files\n')
		(opts.loop, opts.infinite) = (False, False)

	# we need exactly one positional argument (config file)
	if len(args) != 1:
		sys.stderr.write('usage: %s [options] <config file>\n\n' % os.path.basename(sys.argv[0]))
		sys.stderr.write('Config file not specified!\n')
		sys.stderr.write('Use --help to get a list of options!\n')
		sys.exit(os.EX_USAGE)

	while True:
		try:
			if (loop_download(opts, args) or not opts.loop) and not opts.infinite:
				break
			time.sleep(60)
		except KeyboardInterrupt:
			log.critical('\n\nDownload aborted!\n')
			sys.exit(os.EX_TEMPFAIL)

if __name__ == '__main__':
	sys.exit(main(sys.argv[1:]))
