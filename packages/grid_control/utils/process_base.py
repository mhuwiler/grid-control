#-#  Copyright 2016 Karlsruhe Institute of Technology
#-#
#-#  Licensed under the Apache License, Version 2.0 (the "License");
#-#  you may not use this file except in compliance with the License.
#-#  You may obtain a copy of the License at
#-#
#-#      http://www.apache.org/licenses/LICENSE-2.0
#-#
#-#  Unless required by applicable law or agreed to in writing, software
#-#  distributed under the License is distributed on an "AS IS" BASIS,
#-#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#-#  See the License for the specific language governing permissions and
#-#  limitations under the License.

import os, pty, sys, time, errno, fcntl, select, signal, logging, termios, threading
from grid_control.utils.thread_tools import GCEvent, GCLock, GCQueue
from hpfwk import AbstractError
from python_compat import bytes2str, irange, str2bytes

try:
	FD_MAX = os.sysconf('SC_OPEN_MAX')
except (AttributeError, ValueError):
	FD_MAX = 256

def safeClose(fd):
	try:
		os.close(fd)
	except OSError:
		pass

def exit_without_cleanup(code):
	getattr(os, '_exit')(code)


class ProcessError(Exception):
	pass

class ProcessTimeout(ProcessError):
	pass


class ProcessStream(object):
	def __init__(self, buffer, log):
		(self._buffer, self._log) = (buffer, log)

	def read_log(self):
		if self._log is not None:
			return self._log
		return ''

	def clear_log(self):
		result = self._log
		if self._log is not None:
			self._log = ''
		return result

	def __repr__(self):
		return '%s(buffer = %r)' % (self.__class__.__name__, self.read_log())


class ProcessReadStream(ProcessStream):
	def __init__(self, buffer, event_shutdown, event_finished, log = None):
		ProcessStream.__init__(self, buffer, log)
		(self._event_shutdown, self._event_finished, self._iter_buffer) = (event_shutdown, event_finished, '')

	def read(self, timeout):
		result = self._buffer.get(timeout)
		if self._log is not None:
			self._log += result
		return result

	def read_log(self):
		result = self.read(0) # flush buffer
		return ProcessStream.read_log(self) or result

	# wait until stream fulfills condition
	def wait(self, timeout, cond):
		result = ''
		status = None
		timeout_left = timeout
		while True:
			t_start = time.time()
			result += self.read(timeout = timeout_left)
			timeout_left -= time.time() - t_start
			if cond(result):
				break
			if status is not None: # check before update to make at least one last read from stream
				break
			status = self.status(0)
			if timeout_left < 0:
				raise ProcessTimeout('Stream result did not fulfill condition after waiting for %d seconds' % timeout)
		return result

	def iter(self, timeout, timeout_soft = False, timeout_shutdown = 10):
		waitedForShutdown = False
		while True:
			# yield lines from buffer
			while self._iter_buffer.find('\n') != -1:
				posEOL = self._iter_buffer.find('\n')
				yield self._iter_buffer[:posEOL + 1]
				self._iter_buffer = self._iter_buffer[posEOL + 1:]
			# block until new data in buffer / timeout or process is finished
			tmp = self._buffer.get(timeout)
			if tmp: # new data
				self._iter_buffer += tmp
			elif self._event_shutdown.is_set() and not waitedForShutdown: # shutdown in progress
				waitedForShutdown = True
				self._event_finished.wait(timeout_shutdown, 'process shutdown to complete') # wait for shutdown to complete
			elif self._event_finished.is_set() or timeout_soft:
				break # process finished / soft timeout
			else:
				raise ProcessTimeout('Stream did not yield more lines after waiting for %d seconds' % timeout) # hard timeout
		if self._iter_buffer: # return rest of buffer
			yield self._iter_buffer


class ProcessWriteStream(ProcessStream):
	def __init__(self, buffer, close_token = None, log = None):
		ProcessStream.__init__(self, buffer, log)
		self._close_token = close_token

	def write(self, value, log = True):
		if log and (self._log is not None):
			self._log += value
		self._buffer.put(value)

	def set_eof(self, value):
		self._close_token = value

	def close(self):
		self.write(self._close_token)


class Process(object):
	def __init__(self, cmd, *args, **kwargs):
		self._event_shutdown = GCEvent()
		self._event_finished = GCEvent()
		self._buffer_stdin = GCQueue(str)
		self._buffer_stdout = GCQueue(str)
		self._buffer_stderr = GCQueue(str)
		# Stream setup
		do_log = kwargs.get('logging', True) or None
		self.stdout = ProcessReadStream(self._buffer_stdout, self._event_shutdown, self._event_finished, log = do_log)
		self.stderr = ProcessReadStream(self._buffer_stderr, self._event_shutdown, self._event_finished, log = do_log)
		self.stdin = ProcessWriteStream(self._buffer_stdin, log = do_log)
		self.clear_logs() # reset log to proper start value

		self._args = []
		for arg in args:
			self._args.append(str(arg))
		if not cmd:
			raise RuntimeError('Invalid executable!')
		if not os.path.isabs(cmd): # Resolve executable path
			for path in os.environ.get('PATH', '').split(os.pathsep):
				if os.path.exists(os.path.join(path, cmd)):
					cmd = os.path.join(path, cmd)
					break
		if not os.access(cmd, os.X_OK):
			raise OSError('Unable to execute %r' % cmd)
		self._log = logging.getLogger('process.%s' % os.path.basename(cmd))
		self._log.debug('External programm called: %s %s', cmd, self._args)
		self._cmd = cmd
		self.start()

	def __repr__(self):
		return '%s(cmd = %s, args = %s, status = %s, flushed stdout = %r, flushed stderr = %r)' % (
			self.__class__.__name__, self._cmd, repr(self._args), self.status(0), self.stdout.read_log(), self.stderr.read_log())

	def clear_logs(self):
		self.stdout.clear_log()
		self.stderr.clear_log()
		self.stdin.clear_log()

	def get_call(self):
		return str.join(' ', [self._cmd] + self._args)

	def start(self):
		raise AbstractError

	def terminate(self, timeout):
		raise AbstractError

	def kill(self, sig = signal.SIGTERM):
		raise AbstractError

	def restart(self):
		if self.status(0) is None:
			self.kill()
		self.start()

	def status(self, timeout, terminate = False):
		raise AbstractError

	def status_raise(self, timeout):
		status = self.status(timeout)
		if status is None:
			self.terminate(timeout = 1)
			raise ProcessTimeout('Process is still running after waiting for %d seconds' % timeout) # hard timeout
		return status

	def get_output(self, timeout, raise_errors = False):
		t_end = time.time() + timeout
		result = self.stdout.read(timeout)
		status = self.status(timeout = max(0, t_end - time.time()))
		if status is None:
			self.terminate(timeout = 1)
		if raise_errors and (status is None):
			raise ProcessTimeout('Process is still running after waiting for %d seconds' % timeout)
		elif raise_errors and (status != 0):
			raise ProcessError('Command %s %s returned with exit code %s' % (self._cmd, repr(self._args), status))
		return result

	def finish(self, timeout):
		status = self.status_raise(timeout)
		return (status, self.stdout.read(timeout = 0), self.stderr.read(timeout = 0))


class LocalProcess(Process):
	def __init__(self, cmd, *args, **kwargs):
		self._status = None
		Process.__init__(self, cmd, *args, **kwargs)
		self.stdin.set_eof(chr(ord(termios.tcgetattr(self._fd_terminal)[6][termios.VEOF])))
		self._signal_dict = {}
		for attr in dir(signal):
			if attr.startswith('SIG') and ('_' not in attr):
				self._signal_dict[getattr(signal, attr)] = attr

	def start(self):
		# Setup of file descriptors
		LocalProcess.fdCreationLock.acquire()
		try:
			fd_parent_stderr, fd_child_stderr = os.pipe() # Returns (r, w) FDs
		finally:
			LocalProcess.fdCreationLock.release()

		self._pid, self._fd_terminal = pty.fork()
		fd_parent_stdin = self._fd_terminal
		fd_parent_stdout = self._fd_terminal
		if self._pid == 0: # We are in the child process - redirect streams and exec external program
			os.environ['TERM'] = 'vt100'
			os.dup2(fd_child_stderr, 2)
			for fd in irange(3, FD_MAX):
				safeClose(fd)
			try:
				os.execv(self._cmd, [self._cmd] + self._args)
			except Exception:
				invoked = 'os.execv(%s, [%s] + %s)' % (repr(self._cmd), repr(self._cmd), repr(self._args))
				sys.stderr.write('Error while calling %s: ' % invoked + repr(sys.exc_info()[1]))
				for fd in irange(0, 3):
					safeClose(fd)
				exit_without_cleanup(os.EX_OSERR)
			exit_without_cleanup(os.EX_OK)

		else: # Still in the parent process - setup threads to communicate with external program
			safeClose(fd_child_stderr)
			for fd in [fd_parent_stdout, fd_parent_stderr]:
				fcntl.fcntl(fd, fcntl.F_SETFL, os.O_NONBLOCK | fcntl.fcntl(fd, fcntl.F_GETFL))
			self._event_shutdown_stdin = GCEvent() # flag to start shutdown of input handlers

			def handleOutput(fd, buffer):
				def readToBuffer():
					while True:
						try:
							tmp = bytes2str(os.read(fd, 32*1024))
						except OSError:
							tmp = ''
						if not tmp:
							break
						buffer.put(tmp)
				while not self._event_shutdown.is_set():
					try:
						select.select([fd], [], [], 0.2)
					except Exception:
						pass
					readToBuffer()
				readToBuffer() # Final readout after process finished
				safeClose(fd)

			def handleInput():
				local_buffer = ''
				while not self._event_shutdown.is_set():
					if local_buffer: # if local buffer ist leftover from last write - just poll for more
						local_buffer += self._buffer_stdin.get(timeout = 0)
					else: # empty local buffer - wait for data to process
						local_buffer = self._buffer_stdin.get(timeout = 1)
					if local_buffer:
						try:
							(rl, write_list, xl) = select.select([], [fd_parent_stdin], [], 0.2)
						except Exception:
							pass
						if write_list and not self._event_shutdown.is_set():
							written = os.write(fd_parent_stdin, str2bytes(local_buffer))
							local_buffer = local_buffer[written:]
					elif self._event_shutdown_stdin.is_set():
						break
				safeClose(fd_parent_stdin)

			def checkStatus():
				thread_in = threading.Thread(target = handleInput)
				thread_in.start()
				thread_out = threading.Thread(target = handleOutput, args = (fd_parent_stdout, self._buffer_stdout))
				thread_out.start()
				thread_err = threading.Thread(target = handleOutput, args = (fd_parent_stderr, self._buffer_stderr))
				thread_err.start()
				while self._status is None:
					try:
						(pid, status) = os.waitpid(self._pid, 0) # blocking (with spurious wakeups!)
					except OSError: # unable to wait for child
						(pid, status) = (self._pid, -1)
					if pid == self._pid:
						self._status = status
				self._event_shutdown.set() # start shutdown of handlers and wait for it to finish
				self._event_shutdown_stdin.set() # start shutdown of handlers and wait for it to finish
				self._buffer_stdin.finish() # wakeup process input handler
				thread_in.join()
				thread_out.join()
				thread_err.join()
				self._buffer_stdout.finish() # wakeup pending output buffer waits
				self._buffer_stderr.finish()
				self._event_finished.set()

			thread = threading.Thread(target = checkStatus)
			thread.daemon = True
			thread.start()
		self.setup_terminal()

	def setup_terminal(self):
		attr = termios.tcgetattr(self._fd_terminal)
		attr[1] = attr[1] & (~termios.ONLCR) | termios.ONLRET
		attr[3] = attr[3] & ~termios.ECHO
		termios.tcsetattr(self._fd_terminal, termios.TCSANOW, attr)

	def write_stdin_eof(self):
		self.write_stdin(chr(ord(termios.tcgetattr(self._fd_terminal)[6][termios.VEOF])))

	def status(self, timeout, terminate = False):
		self._event_finished.wait(timeout, 'process to finish')
		if self._status is not None: # return either signal name or exit code
			if os.WIFSIGNALED(self._status):
				return self._signal_dict.get(os.WTERMSIG(self._status), 'SIG_UNKNOWN')
			elif os.WIFEXITED(self._status):
				return os.WEXITSTATUS(self._status)
		if terminate:
			return self.terminate(timeout = 1)

	def terminate(self, timeout):
		status = self.status(timeout = 0)
		if status is not None:
			return status
		self.kill(signal.SIGTERM)
		result = self.status(timeout, terminate = False)
		if result is not None:
			return result
		self.kill(signal.SIGKILL)
		return self.status(timeout, terminate = False)

	def kill(self, sig = signal.SIGTERM):
		if not self._event_finished.is_set():
			try:
				os.kill(self._pid, sig)
			except OSError:
				if sys.exc_info()[1].errno != errno.ESRCH: # errno.ESRCH: no such process (already dead)
					raise

LocalProcess.fdCreationLock = GCLock()
