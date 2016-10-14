# | Copyright 2012-2016 Karlsruhe Institute of Technology
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

from grid_control.parameters.psource_base import NullParameterSource, ParameterError, ParameterSource  # pylint:disable=line-too-long
from hpfwk import AbstractError, Plugin
from python_compat import all, ichain, imap, irange, izip, lchain, lfilter, lmap, md5_hex, reduce, set  # pylint:disable=line-too-long


class ForwardingParameterSource(ParameterSource):
	def __init__(self, psrc):
		ParameterSource.__init__(self)
		self._psrc = psrc

	def can_finish(self):
		return self._psrc.can_finish()

	def fill_parameter_content(self, pnum, result):
		self._psrc.fill_parameter_content(pnum, result)

	def fill_parameter_metadata(self, result):
		self._psrc.fill_parameter_metadata(result)

	def get_parameter_len(self):
		return self._psrc.get_parameter_len()

	def get_psrc_hash(self):
		return self._psrc.get_psrc_hash()

	def get_used_psrc_list(self):
		return [self] + self._psrc.get_used_psrc_list()

	def resync_psrc(self):
		return self._psrc.resync_psrc()

	def show_psrc(self):
		return ParameterSource.show_psrc(self) + lmap(lambda x: '\t' + x, self._psrc.show_psrc())


class MultiParameterSource(ParameterSource):  # Meta processing of parameter psrc_list
	def __new__(cls, *psrc_list):
		psrc_list = _strip_null_sources(psrc_list)
		if len(psrc_list) == 1:
			return psrc_list[0]
		elif not psrc_list:
			return NullParameterSource()
		return ParameterSource.__new__(cls)

	def __init__(self, *psrc_list):
		ParameterSource.__init__(self)
		self._psrc_list = _strip_null_sources(psrc_list)
		self._psrc_max_list = lmap(lambda p: p.get_parameter_len(), self._psrc_list)
		self._psrc_max = self._init_psrc_max()

	def __repr__(self):
		return '%s(%s)' % (self.alias_list[0], str.join(', ', imap(repr, self._psrc_list)))

	def can_finish(self):
		return all(imap(lambda p: p.can_finish(), self._psrc_list))

	def fill_parameter_metadata(self, result):
		map_vn2psrc = {}
		for psrc in self._psrc_list:
			metadata_list = []
			psrc.fill_parameter_metadata(metadata_list)
			for metadata in metadata_list:
				vn = metadata.value
				if vn in map_vn2psrc:
					raise ParameterError('Collisions of parameter %s between %s and %s' % (
						metadata.value, psrc, map_vn2psrc[vn]))
				map_vn2psrc[vn] = psrc
				result.append(metadata)

	def get_nested_psrc_list(self):
		return list(self._psrc_list)

	def get_parameter_len(self):
		return self._psrc_max

	def get_psrc_hash(self):
		return md5_hex(self.__class__.__name__ + str(lmap(lambda p: p.get_psrc_hash(), self._psrc_list)))

	def get_used_psrc_list(self):
		return [self] + lchain(imap(lambda ps: ps.get_used_psrc_list(), self._psrc_list))

	def resync_psrc(self):
		psrc_max_old = self._psrc_max
		# Perform resync of subsources
		psrc_resync_list = lmap(lambda p: p.resync_psrc(), self._psrc_list)
		# Update max for _translate_pnum
		self._psrc_max_list = lmap(lambda p: p.get_parameter_len(), self._psrc_list)
		self._psrc_max = self._init_psrc_max()
		# translate affected pnums from subsources
		(result_redo, result_disable, _) = ParameterSource.get_empty_resync_result()
		for (psrc_idx, psrc_resync) in enumerate(psrc_resync_list):
			(psrc_redo, psrc_disable, _) = psrc_resync
			for pnum in psrc_redo:
				result_redo.update(self._translate_pnum(psrc_idx, pnum))
			for pnum in psrc_disable:
				result_disable.update(self._translate_pnum(psrc_idx, pnum))
		return (result_redo, result_disable, psrc_max_old != self._psrc_max)

	def show_psrc(self):
		result = ParameterSource.show_psrc(self)
		result.extend(imap(lambda x: '\t' + x, ichain(imap(lambda ps: ps.show_psrc(), self._psrc_list))))
		return result

	def _init_psrc_max(self):
		raise AbstractError

	def _translate_pnum(self, psrc_idx, pnum):
		# Get local parameter numbers (result) from psrc index and subsource parameter number (pnum)
		raise AbstractError


class RangeParameterSource(ForwardingParameterSource):
	alias_list = ['range']

	def __init__(self, psrc, pos_start=None, pos_end=None):
		ForwardingParameterSource.__init__(self, psrc)
		(self._pos_start, self._pos_end_user) = (pos_start or 0, pos_end)
		self._pos_end = self._get_pos_end()

	def __repr__(self):
		param_list = [str(self._pos_start)]
		if self._pos_end_user is not None:
			param_list.append(str(self._pos_end_user))
		return 'range(%r, %s)' % (self._psrc, str.join(', ', param_list))

	def fill_parameter_content(self, pnum, result):
		self._psrc.fill_parameter_content(pnum + self._pos_start, result)

	def get_parameter_len(self):
		return self._pos_end - self._pos_start + 1

	def get_psrc_hash(self):
		return md5_hex(self._psrc.get_psrc_hash() + str([self._pos_start, self._pos_end]))

	def resync_psrc(self):
		(result_redo, result_disable, _) = ParameterSource.get_empty_resync_result()
		# size change is irrelevant if outside of range
		(psrc_redo, psrc_disable, _) = self._psrc.resync_psrc()

		def _translate(source, target):
			for pnum in source:
				if (pnum >= self._pos_start) and (pnum <= self._pos_end):
					target.add(pnum - self._pos_start)
		_translate(psrc_redo, result_redo)
		_translate(psrc_disable, result_disable)
		pos_end_old = self._pos_end
		self._pos_end = self._get_pos_end()
		return (result_redo, result_disable, pos_end_old != self._pos_end)

	def show_psrc(self):
		result = ForwardingParameterSource.show_psrc(self)
		result[0] += ' range = (%s, %s)' % (self._pos_start, self._pos_end)
		return result

	def _get_pos_end(self):
		if self._pos_end_user is None:
			return self._psrc.get_parameter_len() - 1
		return self._pos_end_user


class SubSpaceParameterSource(ForwardingParameterSource):
	alias_list = ['pspace']

	def __init__(self, name, factory, repository):
		(self._name, self._factory_name) = (name, factory.__class__.__name__)
		ForwardingParameterSource.__init__(self, factory.get_psrc(repository))

	def __repr__(self):
		if self._factory_name == 'SimpleParameterFactory':
			return 'pspace(%r)' % self._name
		return 'pspace(%r, %r)' % (self._name, self._factory_name)

	def create_psrc(cls, pconfig, repository, name='subspace', factory='SimpleParameterFactory'):  # pylint:disable=arguments-differ
		try:
			config = pconfig.get_config(view_class='SimpleConfigView', add_sections=[name])
			pfactory = Plugin.get_class('ParameterFactory').create_instance(factory, config)
			return SubSpaceParameterSource(name, pfactory, repository)
		except:
			raise ParameterError('Unable to create subspace %r using factory %r' % (name, factory))
	create_psrc = classmethod(create_psrc)

	def show_psrc(self):
		tmp = ['%s: name = %s, factory = %s' % (self.__class__.__name__, self._name, self._factory_name)]
		return tmp + lmap(lambda x: '\t' + x, self._psrc.show_psrc())


class TruncateParameterSource(ForwardingParameterSource):
	alias_list = ['truncate']

	def __new__(cls, psrc, max_len):  # pylint:disable=arguments-differ
		if max_len == 0:
			return NullParameterSource()
		elif max_len < 0:
			return psrc
		return ParameterSource.__new__(cls)

	def __init__(self, psrc, max_len):
		ForwardingParameterSource.__init__(self, psrc)
		self._psrc_len = psrc.get_parameter_len()
		self._max_len = max_len

	def __repr__(self):
		return 'truncate(%r, %d)' % (self._psrc, self._max_len)

	def fill_parameter_content(self, pnum, result):
		if (self._psrc_len is None) or (pnum < self._psrc_len):
			self._psrc.fill_parameter_content(pnum, result)

	def get_parameter_len(self):
		return self._max_len

	def get_psrc_hash(self):
		return md5_hex(self._psrc.get_psrc_hash() + str(self._max_len))

	def resync_psrc(self):
		(psrc_redo, psrc_disable, _) = self._psrc.resync_psrc()
		result_redo = set(lfilter(lambda pnum: pnum < self._max_len, psrc_redo))
		result_disable = set(lfilter(lambda pnum: pnum < self._max_len, psrc_disable))
		self._psrc_len = self._psrc.get_parameter_len()
		return (result_redo, result_disable, False)  # size can never change on-the-fly

	def show_psrc(self):
		return ['%s: truncate = %s' % (self.__class__.__name__, self._max_len)]


class BaseZipParameterSource(MultiParameterSource):
	# Base class for psrc_list invoking their sub-psrc_list in parallel
	def __init__(self, len_fun, *psrc_list):
		self._len_fun = len_fun
		MultiParameterSource.__init__(self, *_simplify_nested_sources(self.__class__, psrc_list))

	def fill_parameter_content(self, pnum, result):
		for (psrc, psrc_len) in izip(self._psrc_list, self._psrc_max_list):
			if psrc_len is not None:
				if pnum < psrc_len:
					psrc.fill_parameter_content(pnum, result)
			else:
				psrc.fill_parameter_content(pnum, result)

	def resync_psrc(self):  # Quicker version than the general purpose implementation
		result = ParameterSource.get_empty_resync_result()
		for psrc in self._psrc_list:
			result = _combine_resync_result(result, psrc.resync_psrc())
		psrc_max_old = self._psrc_max
		self._psrc_max_list = lmap(lambda p: p.get_parameter_len(), self._psrc_list)
		self._psrc_max = self._init_psrc_max()
		return (result[0], result[1], psrc_max_old != self._psrc_max)

	def _init_psrc_max(self):
		psrc_max = lfilter(lambda n: n is not None, self._psrc_max_list)
		if len(psrc_max):
			return self._len_fun(psrc_max)


class ChainParameterSource(MultiParameterSource):
	alias_list = ['chain']

	def __init__(self, *psrc_list):
		psrc_list_new = []
		for psrc in _strip_null_sources(psrc_list):  # wrap infinite parameter sources in truncate(..., 1)
			if psrc.get_parameter_len() is None:
				psrc_list_new.append(TruncateParameterSource(psrc, 1))
			else:
				psrc_list_new.append(psrc)
		MultiParameterSource.__init__(self, *psrc_list_new)

	def fill_parameter_content(self, pnum, result):
		limit = 0
		for (psrc, psrc_max) in izip(self._psrc_list, self._psrc_max_list):
			if pnum < limit + psrc_max:
				return psrc.fill_parameter_content(pnum - limit, result)
			limit += psrc_max

	def fill_parameter_metadata(self, result):
		map_vn2tracking_status = {}
		map_vn2psrc_list = {}
		for psrc in self._psrc_list:
			metadata_list = []
			psrc.fill_parameter_metadata(metadata_list)
			for metadata in metadata_list:
				vn = metadata.value
				tracking_status = map_vn2tracking_status.setdefault(vn, metadata.untracked)
				if tracking_status != metadata.untracked:
					raise ParameterError('Collisions of tracking status for parameter %s between %s and %s' % (
						metadata.value, psrc, str.join('; ', imap(repr, map_vn2psrc_list[vn]))))
				if vn not in map_vn2psrc_list:
					result.append(metadata)
				map_vn2psrc_list.setdefault(vn, []).append(psrc)

	def _init_psrc_max(self):
		self._offset_list = lmap(lambda psrc_idx: sum(self._psrc_max_list[:psrc_idx]),
			irange(len(self._psrc_list)))  # calculate partial pnum_max sums for each psrc
		return sum(self._psrc_max_list)

	def _translate_pnum(self, psrc_idx, pnum):
		return [pnum + self._offset_list[psrc_idx]]


class CrossParameterSource(MultiParameterSource):
	alias_list = ['cross']

	def __new__(cls, *psrc_list):
		psrc_list = _strip_null_sources(psrc_list)
		if len(lfilter(lambda p: p.get_parameter_len() is not None, psrc_list)) < 2:
			return ZipLongParameterSource(*psrc_list)
		return ParameterSource.__new__(cls)

	def __init__(self, *psrc_list):
		MultiParameterSource.__init__(self, *_simplify_nested_sources(CrossParameterSource, psrc_list))

	def fill_parameter_content(self, pnum, result):
		for (psrc, psrc_max, psrc_group_size) in self._psrc_info_list:
			if psrc_max:
				psrc.fill_parameter_content(int(pnum / psrc_group_size) % psrc_max, result)
			elif psrc_max is None:
				psrc.fill_parameter_content(pnum, result)

	def _init_psrc_max(self):
		self._psrc_info_list = []
		psrc_group_size = 1
		for (psrc, psrc_max) in izip(self._psrc_list, self._psrc_max_list):
			self._psrc_info_list.append((psrc, psrc_max, psrc_group_size))
			if psrc_max:
				psrc_group_size *= psrc_max
		psrc_max_list = lfilter(lambda n: n is not None, self._psrc_max_list)
		if psrc_max_list:
			return reduce(lambda a, b: a * b, psrc_max_list)

	def _translate_pnum(self, psrc_idx, pnum):
		# psrc irrelevant for pnum translation
		(_, psrc_max, psrc_group_size) = self._psrc_info_list[psrc_idx]
		return lfilter(lambda x: int(x / psrc_group_size) % psrc_max == pnum,
			irange(self.get_parameter_len()))


class RepeatParameterSource(MultiParameterSource):
	alias_list = ['repeat']

	def __new__(cls, psrc, times):  # pylint:disable=arguments-differ
		if times < 0:
			return psrc
		elif psrc.get_parameter_len() is None:
			return TruncateParameterSource(psrc, times)
		elif times == 0:
			return NullParameterSource()
		elif times == 1:
			return psrc
		return ParameterSource.__new__(cls)

	def __init__(self, psrc, times):
		self._psrc = psrc
		self._times = times
		MultiParameterSource.__init__(self, psrc)

	def __repr__(self):
		return 'repeat(%s, %d)' % (repr(self._psrc), self._times)

	def fill_parameter_content(self, pnum, result):
		self._psrc.fill_parameter_content(pnum % self._psrc_child_max, result)

	def fill_parameter_metadata(self, result):
		self._psrc.fill_parameter_metadata(result)

	def get_psrc_hash(self):
		return md5_hex(self._psrc.get_psrc_hash() + str(self._times))

	def show_psrc(self):
		result = ParameterSource.show_psrc(self)
		result[0] += ' count = %d' % self._times
		return result + lmap(lambda x: '\t' + x, self._psrc.show_psrc())

	def _init_psrc_max(self):
		self._psrc_child_max = self._psrc.get_parameter_len()
		if self._psrc_child_max is not None:
			return self._times * self._psrc_child_max
		return self._times

	def _translate_pnum(self, psrc_idx, pnum):
		return lmap(lambda i: pnum + i * self._psrc_child_max, irange(self._times))


class ZipLongParameterSource(BaseZipParameterSource):
	alias_list = ['zip']

	def __init__(self, *psrc_list):
		BaseZipParameterSource.__init__(self, max, *psrc_list)


class ZipShortParameterSource(BaseZipParameterSource):
	alias_list = ['szip']

	def __init__(self, *psrc_list):
		BaseZipParameterSource.__init__(self, min, *psrc_list)


class ErrorParameterSource(ChainParameterSource):
	alias_list = ['variation']

	def __init__(self, *psrc_list):
		psrc_list = _strip_null_sources(psrc_list)
		self._psrc_list_raw = psrc_list
		central_psrc_list = lmap(lambda p: RangeParameterSource(p, 0, 0), psrc_list)
		result_psrc_list = [ZipLongParameterSource(*central_psrc_list)]
		for psrc_idx, psrc in enumerate(psrc_list):
			if psrc.get_parameter_len() is not None:
				variation_psrc_list = list(central_psrc_list)  # perform copy and overwrite psrc_idx-th element
				variation_psrc_list[psrc_idx] = RangeParameterSource(psrc_list[psrc_idx], 1, None)
				result_psrc_list.append(CrossParameterSource(*variation_psrc_list))
		ChainParameterSource.__init__(self, *result_psrc_list)

	def __repr__(self):
		return 'variation(%s)' % str.join(', ', imap(repr, self._psrc_list_raw))


def _combine_resync_result(resync_result_a, resync_result_b, sc_fun=bool.__or__):
	(redo_a, disable_a, size_change_a) = resync_result_a
	(redo_b, disable_b, size_change_b) = resync_result_b
	redo_a.update(redo_b)
	disable_a.update(disable_b)
	return (redo_a, disable_a, sc_fun(size_change_a, size_change_b))


def _simplify_nested_sources(cls, psrc_list):
	for psrc in psrc_list:
		if isinstance(psrc, cls):
			for child_ps in psrc.get_nested_psrc_list():
				yield child_ps
		else:
			yield psrc


def _strip_null_sources(psrc_list):
	return lfilter(lambda p: not isinstance(p, NullParameterSource), psrc_list)
