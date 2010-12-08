#!/usr/bin/env python
import os, json, glob, copy
import numpy as np
import cPickle
import pyfits

from contextlib  import contextmanager
from collections import defaultdict

import query_parser as qp
import bhpix
import utils
import pool2
import native

from interval    import intervalset
from colgroup    import ColGroup
from table       import Table

class TabletCache:
	""" An cache of tablets loaded while performing a Query.

		TODO: Perhaps merge it with DB? Or make it a global?
	"""
	cache = {}		# Cache of loaded tables, in the form of cache[cell_id][include_cached][table][cgroup] = rows

	root_name = None	# The name of the root table (string). Used for figuring out if _not_ to load the cached rows.
	include_cached = False	# Should we load the cached rows from the root table?

	def __init__(self, root_name, include_cached = False):
		self.cache = {}
		self.root_name = root_name
		self.include_cached = include_cached

	def load_column(self, cell_id, name, table, autoexpand=True, resolve_blobs=False):
		# Return the column 'name' from table 'table'.
		# Load its tablet if necessary, and cache it for further reuse.
		#
		# NOTE: Unless resolve_blobs=True, this method DOES NOT resolve blobrefs to BLOBs
		include_cached = self.include_cached if table.name == self.root_name else True

		# Figure out which table contains this column
		cgroup = table.columns[name].cgroup

		# Create self.cache[cell_id][table.name][include_cached][cgroup] hierarchy if needed
		if  cell_id not in self.cache:
			self.cache[cell_id] = {}
		if table.name not in self.cache[cell_id]:
			self.cache[cell_id][table.name] = {}
		if include_cached not in self.cache[cell_id][table.name]:		# This bit is to support (in the future) self-joins. Note that (yes) this can be implemented in a much smarter way.
			self.cache[cell_id][table.name][include_cached] = {}

		# See if we have already loaded the required tablet
		tcache = self.cache[cell_id][table.name][include_cached]
		if cgroup not in tcache:
			# Load and cache the tablet
			rows = table.fetch_tablet(cell_id, cgroup, include_cached=include_cached)

			# Ensure it's as long as the primary table (this allows us to support "sparse" tablets)
			if autoexpand and cgroup != table.primary_cgroup:
				nrows = len(self.load_column(cell_id, table.primary_key.name, table))
				rows.resize(nrows)

			tcache[cgroup] = rows
		else:
			rows = tcache[cgroup]

		col = rows[name]
		
		# resolve blobs, if requested
		if resolve_blobs:
			col = self.resolve_blobs(cell_id, col, name, table)

		return col

	def resolve_blobs(self, cell_id, col, name, table):
		# Resolve blobs (if blob column). NOTE: the resolved blobs
		# will not be cached.

		if table.columns[name].is_blob:
			include_cached = self.include_cached if table.name == self.root_name else True
			col = table.fetch_blobs(cell_id, column=name, refs=col, include_cached=include_cached)

		return col

class TableEntry:
	""" A table that is a member of a Query. See DB.construct_join_tree() for details.
	"""
	table    = None	# A Table() instance
	name     = None # Table name, as named in the query (may be different from table.name, if 'table AS other' construct was used)
	relation = None	# Relation to use to join with the parent
	joins    = None	# List of JoinEntries

	def __init__(self, table, name):
		self.table = table
		self.name  = name if name is not None else name
		self.joins = []

	def get_cells(self, bounds, include_cached=True):
		""" Get populated cells of self.table that overlap
			the requested bounds. Do it recursively.
			
			The bounds can be either a list of (Polygon, intervalset) tuples,
			or None.
			
			For a static cell_id, a dynamic table will return all
			temporal cells within the same spatial cell. For a dynamic
			cell_id, a static table will return the overlapping
			static cell_id.
		"""

		# Fetch our populated cells
		pix = self.table.pix
		cells = self.table.get_cells(bounds, return_bounds=True, include_cached=include_cached)

		# Autodetect if we're a static or temporal table
		self.static = True
		for cell_id in cells:
			if self.table.pix.is_temporal_cell(cell_id):
				self.static = False
				break
		#print self.table.name, ":", self.static

		# Fetch the children's populated cells
		for ce in self.joins:
			cc, op = ce.get_cells(bounds)
			if   op == 'and':
				ret = dict()
				for cell_id, cbounds in cc.iteritems():
					assert cell_id not in cells or cells[cell_id] == cbounds
#					if not (cell_id not in cells or cells[cell_id] == cbounds): # TODO: Debugging -- make sure the timespace constraints are the same
#						aa = cells[cell_id]
#						bb = cbounds
#						pass
					assert cell_id not in cells or cells[cell_id] == cbounds # TODO: Debugging -- make sure the timespace constraints are the same
					if cell_id in cells:
						ret[cell_id] = cbounds
					else:
						static_cell = pix.static_cell_for_cell(cell_id)
						if static_cell in cells:
							ret[static_cell] = cells[static_cell]
							ret[cell_id] = cbounds
			elif op == 'or':
				ret = cells
				for cell_id, cbounds in cc.iteritems():
					assert cell_id not in cells or cells[cell_id] == cbounds # TODO: Debugging -- make sure the timespace constraints are the same
#					if not (cell_id not in cells or cells[cell_id] == cbounds): # TODO: Debugging -- make sure the timespace constraints are the same
#						aa = cells[cell_id]
#						bb = cbounds
#						pass
					assert cell_id not in cells or cells[cell_id] == cbounds # Debugging -- make sure the timespace constraints are the same
					ret[cell_id] = cbounds
			cells = ret

		if self.relation is None:
			# Remove all static cells if there's even a single temporal cell
			cells2 = dict(( v for v in cells.iteritems() if pix.is_temporal_cell(v[0]) ))
			cells = cells2 if cells2 else cells
			return cells
		else:
			return cells, self.relation.join_op()

	def evaluate_join(self, cell_id, bounds, tcache, r = None, key = None, idxkey = None):
		""" Constructs a JOIN index array.

			* If the result of the join is no rows, return None

		    * If the result is not empty, the return is a ColGroup() instance
		      that _CAN_ (but DOESN'T HAVE TO; see below) have the 
		      following columns:

		    	<tabname> : the index array that will materialize
				    the JOIN result when applied to the corresponding
				    table's column as:
				    
				    	res = col[<tabname>]

		    	<tabname>._NULL : a boolean array indicating whether
		    	            the <tabname> index is a dummy (zero) and
		    	            actually the column has JOINed to NULL
		    	            (this only happens with outer joins)

		      -- If the result of a JOIN for a particular table leaves
		         no NULLs, there'll be no <tabname>._NULL column.
		      -- If the col[<tabname>] would be == col, there'll be no
		         <tabname> column.
		"""
		hasBounds = bounds != [(None, None)] and bounds != [(None, intervalset((-np.inf, np.inf)))]
		if len(bounds) > 1:
			pass;
		# Skip everything if this is a single-table read with no bounds
		# ("you don't pay for what you don't use")
		if self.relation is None and not hasBounds and not self.joins:
			return ColGroup()

		# Load ourselves
		id = tcache.load_column(cell_id, self.table.get_primary_key(), self.table)
		s = ColGroup()
		mykey = '%s._ID' % self.name

		s.add_column(mykey, id)				# The key on which we'll do the joins
		s.add_column(self.name, np.arange(len(id)))	# The index into rows of this tablet, as loaded from disk

		if self.relation is None:
			# We are root.
			r = s

			# Setup spatial bounds filter
			if hasBounds:
				ra, dec = self.table.get_spatial_keys()
				lon = tcache.load_column(cell_id,  ra, self.table)
				lat = tcache.load_column(cell_id, dec, self.table)

				r = self.filter_space(r, lon, lat, bounds)	# Note: this will add the _INBOUNDS column to r
		else:
			# We're a child. Join with the parent table
			idx1, idx2, isnull = self.relation.join(cell_id, r[key], s[mykey], r[idxkey], s[self.name], tcache)

			# Handle the special case of OUTER JOINed empty 's'
			if len(s) == 0 and len(idx2) != 0:
				assert isnull.all()
				s = ColGroup(dtype=s.dtype, size=1)

			# Perform the joins
			r = r[idx1]
			s = s[idx2]
			r.add_columns(s.items())

			# Add the NULL column only if there were any NULLs
			if isnull.any():
				r.add_column("%s._NULL" % self.name, isnull)

		# Perform spacetime cuts, if we have a time column
		# (and the JOIN didn't result in all NULLs)
		if hasBounds:
			tk = self.table.get_temporal_key()
			if tk is not None:
				t = tcache.load_column(cell_id,  tk, self.table)
				if len(t):
					r = self.filter_spacetime(r, t[idx2], bounds)

		# Let children JOIN themselves onto us
		for ce in self.joins:
			r = ce.evaluate_join(cell_id, bounds, tcache, r, mykey, self.name)

		# Cleanup: once we've joined with the parent and all children,
		# no need to keep the primary key around
		r.drop_column(mykey)

		if self.relation is None:
			# Return None if the query yielded no rows
			if len(r) == 0:
				return None
			# Cleanup: if we are root, remove the _INBOUNDS helper column
			if '_INBOUNDS' in r:
				r.drop_column('_INBOUNDS')

		return r

	def filter_space(self, r, lon, lat, bounds):
		# _INBOUNDS is a cache of spatial bounds hits, so we can
		# avoid repeated (expensive) Polygon.isInside* calls in
		# filter_time()
		inbounds = np.ones((len(r), len(bounds)), dtype=np.bool)
		r.add_column('_INBOUNDS', inbounds) # inbounds[j, i] is true if the object in row j falls within bounds[i]

		x, y = None, None
		for (i, (bounds_xy, _)) in enumerate(bounds):
			if bounds_xy is not None:
				if x is None:
					(x, y) = bhpix.proj_bhealpix(lon, lat)

				inbounds[:, i] &= bounds_xy.isInsideV(x, y)

		# Keep those that fell within at least one of the bounds present in the bounds set
		# (and thus may appear in the final result, depending on time cuts later on)
		in_  = np.any(inbounds, axis=1)
		if not in_.all():
			r = r[in_]
		return r

	def filter_spacetime(self, r, t, bounds):
		# Cull on time (for all)
		inbounds = r['_INBOUNDS']

		# This essentially looks for at least one bound specification that contains a given row
		in_ = np.zeros(len(inbounds), dtype=np.bool)
		for (i, (_, bounds_t)) in enumerate(bounds):
			if bounds_t is not None:
				in_t = bounds_t.isInside(t)
				in_ |= inbounds[:, i] & in_t
			else:
				in_ |= inbounds[:, i]

		if not in_.all():
			r = r[in_]
		return r

	def _str_tree(self, level):
		s = '    '*level + '\-- ' + self.name
		if self.relation is not None:
			s += '(%s)' % self.relation
		s += '\n'
		for e in self.joins:
			s += e._str_tree(level+1)
		return s

	def __str__(self):
		return self._str_tree(0)

class JoinRelation:
	kind   = 'inner'
	db     = None	# Controlling database instance
	tableR = None	# Right-hand side table of the relation
	tableS = None	# Left-hand side table of the relation
	
	def __init__(self, db, tableR, tableS, kind, **joindef):
		self.db     = db
		self.kind   = kind
		self.tableR = tableR
		self.tableS = tableS

	def join_op(self):	# Returns 'and' if the relation has an inner join-like effect, and 'or' otherwise
		return 'and' if self.kind == 'inner' else 'or'

	def join(self, cell_id, id1, id2, idx1, idx2, tcache):	# Returns idx1, idx2, isnull
		raise NotImplementedError('You must override this method from a derived class')

class IndirectJoin(JoinRelation):
	m1_colspec = None	# (table, column) tuple giving the location of m1
	m2_colspec = None	# (table, column) tuple giving the location of m2

	def fetch_join_map(self, cell_id, m1_colspec, m2_colspec, tcache):
		"""
			Return a list of crossmatches corresponding to ids
		"""
		table1, column_from = m1_colspec
		table2, column_to   = m2_colspec
		
		# This allows tripple
		# joins of the form static-static-temporal, where the cell_id
		# will be temporal even when fetching the static-static join
		# table for the two other table.
		cell_id_from = table1.static_if_no_temporal(cell_id)
		cell_id_to   = table2.static_if_no_temporal(cell_id)

		if    not table1.tablet_exists(cell_id_from) \
		   or not table2.tablet_exists(cell_id_to):
			return np.empty(0, dtype=np.uint64), np.empty(0, dtype=np.uint64)

		m1 = tcache.load_column(cell_id_from, column_from, table1)
		m2 = tcache.load_column(cell_id_to  , column_to  , table2)
		assert len(m1) == len(m2)

		return m1, m2

	def join(self, cell_id, id1, id2, idx1, idx2, tcache):
		"""
		    Perform a JOIN on id1, id2, that were obtained by
		    indexing their origin tables with idx1, idx2.
		"""
		(m1, m2) = self.fetch_join_map(cell_id, self.m1_colspec, self.m2_colspec, tcache)
		return native.table_join(id1, id2, m1, m2, self.kind)

	def __init__(self, db, tableR, tableS, kind, **joindef):
		JoinRelation.__init__(self, db, tableR, tableS, kind, **joindef)

		m1_tabname, m1_colname = joindef['m1']
		m2_tabname, m2_colname = joindef['m2']

		self.m1_colspec = (db.table(m1_tabname), m1_colname)
		self.m2_colspec = (db.table(m2_tabname), m2_colname)

	def __str__(self):
		return "%s indirect via [%s.%s, %s.%s]" % (
			self.kind,
			self.m1_colspec[0], self.m1_colspec[1].name,
			self.m2_colspec[0], self.m2_colspec[1].name,
		)

#class EquijoinJoin(IndirectJoin):
#	def __init__(self, db, tableR, tableS, kind, **joindef):
#		JoinRelation.__init__(self, db, tableR, tableS, kind, **joindef)
#
#		# Emulating direct join with indirect one
#		# TODO: Write a specialized direct join routine
#		self.m1_colspec = tableR, joindef['id1']
#		self.m2_colspec = tableS, joindef['id2']

def create_join(db, fn, jkind, tableR, tableS):
	data = json.loads(file(fn).read())
	assert 'type' in data

	jclass = data['type'].capitalize() + 'Join'
	return globals()[jclass](db, tableR, tableS, jkind, **data)

class iarray(np.ndarray):
	""" Subclass of ndarray allowing per-row indexing.
	
	    Used by ColDict.load_column()
	"""
	def __call__(self, *args):
		"""
		   Apply numpy indexing on a per-row basis. A rough
		   equivalent of:
			
			self[ arange(len(self)) , *args]

		   where any tuples in args will be converted to a
		   corresponding slice, while integers and numpy
		   arrays will be passed in as-given. Any numpy array
		   given as index must be of len(self).

		   Simple example: assuming we have a chip_temp column
		   that was defined with 64f8, to select the temperature
		   of the chip corresponding to the observation, do:
		   
		   	chip_temp(chip_id)

		   Note: A tuple of the form (x,y,z) is will be
		   conveted to [x:y:z] slice. (x,y) converts to [x:y:]
		   and (x,) converts to [x::]
		"""
		# Note: numpy multidimensional indexing is mind numbing...
		# The stuff below works for 1D arrays, I don't guarantee it for higher dimensions.
		#       See: http://docs.scipy.org/doc/numpy/reference/arrays.indexing.html#advanced-indexing
		idx = [ (slice(*arg) if len(arg) > 1 else np.s_[arg[0]:]) if isinstance(arg, tuple) else arg for arg in args ]

		firstidx = np.arange(len(self)) if len(self) else np.s_[:]
		idx = tuple([ firstidx ] + idx)

		return self.__getitem__(idx)

class TableColsProxy:
	""" A dict-like object for query_parser.resolve_wildcards()
	"""
	tables = None
	root_table = None

	def __init__(self, root_table, tables):
		self.root_table = root_table
		self.tables = tables

	def __getitem__(self, tabname):
		# Return a list of columns in table tabname, unless
		# they're pseudocolumns
		if tabname == '':
			tabname = self.root_table
		table = self.tables[tabname].table
		return [ name for (name, coldef) in table.columns.iteritems() if not table._is_pseudotablet(coldef.cgroup) ]

class QueryInstance(object):
	# Internal working state variables
	tcache   = None		# TabletCache() instance
	columns  = None		# Cache of already referenced and evaluated columns (dict)
	cell_id  = None		# cell_id on which we're operating
	jmap 	 = None		# index map used to materialize the JOINs
	bounds   = None

	# These will be filled in from a QueryEngine instance
	db       = None		# The controlling database instance
	tables   = None		# A name:Table dict() with all the tables listed on the FROM line.
	root	 = None		# TableEntry instance with the primary (root) table
	query_clauses = None	# Tuple with parsed query clauses
	pix      = None         # Pixelization object (TODO: this should be moved to class DB)
	locals   = None		# Extra local variables to be made available within the query

	def __init__(self, q, cell_id, bounds, include_cached):
		self.db            = q.db
		self.tables	   = q.tables
		self.root	   = q.root
		self.query_clauses = q.query_clauses
		self.pix           = q.root.table.pix
		self.locals        = q.locals

		self.cell_id	= cell_id
		self.bounds	= bounds

		self.tcache	= TabletCache(self.root.name, include_cached)
		self.columns	= {}
		
	def peek(self):
		assert self.cell_id is None
		return self.eval_select()

	def __iter__(self):
		assert self.cell_id is not None # Cannot call iter when peeking

		# Evaluate the JOIN map
		self.jmap   	    = self.root.evaluate_join(self.cell_id, self.bounds, self.tcache)

		if self.jmap is not None:
			# TODO: We could optimize this by evaluating WHERE first, using the result
			#       to cull the output number of rows, and then evaluating the columns.
			#		When doing so, care must be taken to fall back onto evaluating a
			#		column from the SELECT clause, if it's referenced in WHERE

			globals_ = self.prep_globals()

			# eval individual columns in select clause to slurp them up from disk
			# and have them ready for the WHERE clause
			rows = self.eval_select(globals_)

			if len(rows):
				in_  = self.eval_where(globals_)

				if not in_.all():
					rows = rows[in_]

				# Attach metadata
				rows.cell_id = self.cell_id
				rows.where__ = in_

				yield rows

		# We yield nothing if the result set is empty.

	def prep_globals(self):
		globals_ = globals()

		# Import packages of interest (numpy)
		for i in np.__all__:
			if len(i) >= 2 and i[:2] == '__':
				continue
			globals_[i] = np.__dict__[i]

		# Add implicit global objects present in queries
		globals_['_PIX'] = self.root.table.pix
		globals_['_DB']  = self.db

		return globals_

	def eval_where(self, globals_ = None):
		(_, where_clause, _, _) = self.query_clauses

		if globals_ is None:
			globals_ = self.prep_globals()

		# evaluate the WHERE clause, to obtain the final filter
		in_    = np.empty(len(next(self.columns.itervalues())), dtype=bool)
		in_[:] = eval(where_clause, globals_, self)

		return in_

	def eval_select(self, globals_ = None):
		(select_clause, _, _, _) = self.query_clauses

		if globals_ is None:
			globals_ = self.prep_globals()

		rows = ColGroup()
		for (asname, name) in select_clause:
#			col = self[name]	# For debugging
			col = eval(name, globals_, self)
#			exit()

			self[asname] = col
			rows.add_column(asname, col)

		return rows

	#################

	def load_column(self, name, tabname):
		# If we're just peeking, construct the column from schema
		if self.cell_id is None:
			assert self.bounds is None
			cdef = self.tables[tabname].table.columns[name]
			dtype = np.dtype(cdef.dtype)

			# Handle blobs
			if cdef.is_blob:
				col = np.empty(0, dtype=np.dtype(('O', dtype.shape)))
			else:
				col = np.empty(0, dtype=dtype)
		else:
			# Load the column from cgroup 'cgroup' of table 'tabname'
			# Also cache the loaded tablet, for future reuse
			table = self.tables[tabname].table

			# Load the column (via cache)
			col = self.tcache.load_column(self.cell_id, name, table)

			# Join/filter if needed
			if tabname in self.jmap:
				isnullKey  = tabname + '._NULL'
				idx			= self.jmap[tabname]
				if len(col):
					col         = col[idx]
	
					if isnullKey in self.jmap:
						isnull		= self.jmap[isnullKey]
						col[isnull] = table.NULL
				elif len(idx):
					# This tablet is empty, but columns show up here because of an OUTER join.
					# Just return NULLs of proper dtype
					assert self.jmap[isnullKey].all()
					assert (idx == 0).all()
					col = np.zeros(shape=(len(idx),) + col.shape[1:], dtype=col.dtype)

			# Resolve blobs (if a blobref column)
			col = self.tcache.resolve_blobs(self.cell_id, col, name, table)

		# Return the column as an iarray
		col = col.view(iarray)
		return col

	def load_pseudocolumn(self, name):
		""" Generate per-query pseudocolumns.
		
		    Developer note: When adding a new pseudocol, make sure to also add
		       it to the if() statement in __getitem__
		"""
		# Detect the number of rows
		nrows = len(self['_ID'])

		if name == '_ROWNUM':
			# like Oracle's ROWNUM, but on a per-cell basis (and zero-based)
			return np.arange(nrows, dtype=np.uint64)
		elif name == '_CELLID':
			ret = np.empty(nrows, dtype=np.uint64)
			ret[:] = self.cell_id
			return ret
		else:
			raise Exception('Unknown pseudocolumn %s' % name)

	def __getitem__(self, name):
		# An already loaded column?
		if name in self.columns:
			return self.columns[name]

		# A yet unloaded column from one of the joined tables
		# (including the primary)? Try to find it in cgroups of
		# joined tables. It may be prefixed by the table name, in
		# which case we force the lookup of that table only.
		if name.find('.') == -1:
			# Do this to ensure the primary table is the first
			# to get looked up when resolving column names.
			tables = [ (self.root.name, self.tables[self.root.name]) ]
			tables.extend(( (name, table) for name, table in self.tables.iteritems() if name != self.root.name ))
			colname = name
		else:
			# Force lookup of a specific table
			(tabname, colname) = name.split('.')
			tables = [ (tabname, self.tables[tabname]) ]
		for (tabname, e) in tables:
			colname = e.table.resolve_alias(colname)
			if colname in e.table.columns:
				self[name] = self.load_column(colname, tabname)
				return self.columns[name]

		# A query pseudocolumn?
		if name in ['_ROWNUM', '_CELLID']:
			col = self[name] = self.load_pseudocolumn(name)
			return col

		# A name of a table? Return a proxy object
		if name in self.tables:
			return TableProxy(self, name)

		# Is this one of the local variables passed in by the user?
		if name in self.locals:
			return self.locals[name]

		# This object is unknown to us -- let it fall through, it may
		# be a global variable or function
		raise KeyError(name)

	def __setitem__(self, key, val):
		if len(self.columns):
			assert len(val) == len(next(self.columns.itervalues())), "%s: %d != %d" % (key, len(val), len(self.columns.values()[0]))

		self.columns[key] = val

class IntoWriter(object):
	""" Implementation of the writing logic for '... INTO ...' query clause
	"""
	into_clause = None
	_tcache = None
	rows = None
	locals = None
	db = None
	pix = None
	table = None

	def __init__(self, db, into_clause, locals = {}):
		# This handles INTO clauses. Stores the data into
		# destination table, returning the IDs of stored rows.
		if isinstance(into_clause, str):
			query = "_ FROM _ INTO %s" % into_clause
			_, _, _, into_clause = qp.parse(query)

		self.db          = db
		self.into_clause = into_clause
		self.locals      = locals

	@property
	def tcache(self):
		# Auto-create a tablet cache if needed
		if self._tcache is None:
			into_table   = self.into_clause[0]
			self._tcache = TabletCache(into_table, False)
		return self._tcache

	#########
	def __getitem__(self, name):
		# Do we have this column in the query result rows?
		if name in self.rows:
			return self.rows[name]

		# Is this one of the local variables passed in by the user?
		if name in self.locals:
			return self.locals[name]

		# This object is unknown to us -- let it fall through, it may
		# be a global variable or function
		raise KeyError(name)

	#################

	def peek(self, rows):
		# We always return uint64 arrays
		return np.empty(0, dtype='u8')

	def write(self, cell_id, rows):
		assert isinstance(rows, ColGroup)
		self.rows = rows

		# The table may have been created on previous pass
		if self.table is None:
			self.table  = self.create_into_table()

		rows = self.eval_into(cell_id, rows)
		return (cell_id, rows)

	def _find_into_dest_rows(self, cell_id, table, into_col, vals):
		""" Return the keys of rows in table whose 'into_col' value
		    matches vals. Return zero for vals that have no match.
		"""
		into_col = table.resolve_alias(into_col)
		col = self.tcache.load_column(cell_id, into_col, table, autoexpand=False, resolve_blobs=True)
#		print "XX:", col, vals;

		if len(col) == 0:
			return np.zeros(len(vals), dtype=np.uint64)

		# Find corresponding rows
		ii = col.argsort()
		scol = col[ii]
		idx = np.searchsorted(scol, vals)
#		print "XX:", scol, ii

		idx[idx == len(col)] = 0
		app = scol[idx] != vals
#		print "XX:", idx, app

		# Reorder to original ordering
		idx = ii[idx]
#		print "XX:", idx, app

		# TODO: Verify (debugging, remove when happy)
		in2 = np.in1d(vals, col)
#		print "XX:", in2
		assert np.all(in2 == ~app)

		id = self.tcache.load_column(cell_id, table.primary_key.name, table, autoexpand=False)
		id = id[idx]		# Get the values of row indices that we have
		id[app] = 0		# Mark empty indices with 0
#		print "XX:", id;

		return id

	def prep_globals(self):
		globals_ = globals()

		# Add implicit global objects present in queries
		globals_['_PIX'] = self.table.pix
		globals_['_DB']  = self.db
		
		return globals_

	def eval_into(self, cell_id, rows):
		# Insert into the destination table
		(into_table, _, into_col, keyexpr, kind) = self.into_clause

		table = self.db.table(into_table)
		if kind == 'append':
			rows.add_column('_ID', cell_id, 'u8')
			ids = table.append(rows)
		elif kind in ['update/ignore', 'update/insert']:
			# Evaluate the key expression
			globals_ = self.prep_globals()
			vals = eval(keyexpr, globals_, self)
#			print rows['mjd_obs'], rows['mjdorig'], keyexpr, vals; exit()

			# Match rows
			id = self._find_into_dest_rows(cell_id, table, into_col, vals)
			if table.primary_key.name not in rows:
				rows.add_column('_ID', id)
				key = '_ID'
			else:
				key = table.primary_key.name
				assert np.all(rows[key][id != 0] == id[id != 0])
				#rows[key] = id

			if kind == 'update/ignore':
				# Remove rows that don't exist, and update existing
				rows = rows[id != 0]
			else:
				# Append the rows whose IDs are unspecified
				rows[key][ rows[key] == 0 ] = cell_id
			ids = table.append(rows, _update=True)
#			print rows['_ID'], ids

			assert np.all(id[id != 0] == ids)
		elif kind == 'insert':	# Insert/update new rows (the expression give the key)
			# Evaluate the key expression
			globals_ = self.prep_globals()
			id = eval(keyexpr, globals_, self)

			if table.primary_key.name not in rows:
				rows.add_column('_ID', id)
			else:
				assert np.all(rows[table.primary_key.name] == id)
				rows[table.primary_key.name] = id

			# Update and/or add new rows
			ids = table.append(rows, _update=True)

		# Return IDs only
		for col in rows.keys():
			rows.drop_column(col)
		rows.add_column('_ID', ids)
#		print "HERE", rows; exit()
		
		return rows

	def create_into_table(self):
		# called to auto-create the destination table
		(tabname, dtype, into_col, keyexpr) = self.into_clause[:4]
		assert dtype is None, "User-supplied dtype not supported yet."

		db = self.db
		dtype = self.rows.dtype
		schema = {
			'columns': [],
			'filters': { 'complevel': 1, 'complib': 'zlib', 'fletcher32': True }, # Enable compression and checksumming
		}
		with db.lock():
			if db.table_exists(tabname):
				# Must have a designated key for updating to work
#				if into_col is None:
#					raise Exception('If selecting into an existing table (%s), you must specify the column with IDs that will be updated (" ... INTO ... AT keycol") construct).' % tabname)

				table = db.table(tabname)

				# Find any new columns we'll need to create
				for name in dtype.names:
					rname = table.resolve_alias(name)
					if rname not in table.columns:
						schema['columns'].append((name, utils.str_dtype(dtype[name])))

				# Disallow creating new columns
				if into_col is None and schema['columns']:
					raise Exception('If selecting into an existing table (%s) with no INTO ... WHERE clause, all columns present in the query must already exist in the table' % tabname)
			else:
				# Creating a new table
				table = db.table(tabname, True)

				# Create all columns
				schema['columns'] = [ (name, utils.str_dtype(dtype[name])) for name in dtype.names ]
				
				# If key is specified, and is a column name from self.rows, name the primary
				# key after it
				#if keyexpr is not None and keyexpr in self.rows:
				#	schema['primary_key'] = keyexpr
				#else:
				#	schema['primary_key'] = '_id'
				schema['primary_key'] = '_id'

			# Adding columns starting with '_' is prohibited. Enforce it here
			for (col, _) in schema['columns']:
				if col[0] == '_':
					raise Exception('Storing columns starting with "_" is prohibited. Use the "col AS alias" construct to rename the offending column ("%s")' % col)

			# Add a primary key column (if needed)
			if 'primary_key' in schema and schema['primary_key'] not in dict(schema['columns']):
				schema['columns'].insert(0, (schema['primary_key'], 'u8'))

			# Create a new cgroup (if needed)
			if schema['columns']:
				for x in xrange(1, 100000):
					tname = 'auto%03d' % x
					if tname not in table._cgroups:
						break
				table.create_cgroup(tname, schema)

		##print "XXX:", tname, schema;# exit()
		return table

class QueryEngine(object):
	# These are a part of the mappers' public API
	db       = None		# The controlling database instance
	pix      = None         # Pixelization object (TODO: this should be moved to class DB)
	tables = None		# A name:Table dict() with all the tables listed on the FROM line.
	root	 = None		# TableEntry instance with the primary (root) table
	query_clauses  = None	# Parsed query clauses
	locals   = None		# Extra variables to be made local to the query

	def __init__(self, db, query, locals = {}):
		self.db = db

		# parse query
		(select_clause, where_clause, from_clause, into_clause) = qp.parse(query)

		self.root, self.tables = db.construct_join_tree(from_clause);
		select_clause            = qp.resolve_wildcards(select_clause, TableColsProxy(self.root.name, self.tables))

		self.query_clauses       = (select_clause, where_clause, from_clause, into_clause)

		self.locals = locals

		# Aux variables that mappers can access
		self.pix = self.root.table.pix

	def on_cell(self, cell_id, bounds=None, include_cached=False):
		return QueryInstance(self, cell_id, bounds, include_cached)

	def on_cells(self, partspecs, include_cached=False):
		# Set up the args for __iter__
		self._partspecs = partspecs
		self._include_cached = include_cached
		return self

	def __iter__(self):
		# Generate a single stream of row blocks for a list of cells+bounds
		partspecs, include_cached = self._partspecs, self._include_cached

		for cell_id, bounds in partspecs:
			for rows in QueryInstance(self, cell_id, bounds, include_cached):
				yield rows

	def peek(self):
		return QueryInstance(self, None, None, None).peek()

class Query(object):
	db      = None
	qengine = None
	qwriter = None
	query_string = None

	def __str__(self):
		return self.query_string

	def __init__(self, db, query, locals = {}):
		self.db		  = db
		self.query_string = query
		self.qengine = QueryEngine(db, query, locals=locals)

		(_, _, _, into_clause) = qp.parse(query)
		if into_clause:
			self.qwriter = IntoWriter(db, into_clause, locals)

	def execute(self, kernels, bounds=None, include_cached=False, cells=[], group_by_static_cell=False, testbounds=True, nworkers=None, progress_callback=None, _yield_empty=False):
		""" A MapReduce implementation, where rows from individual cells
		    get mapped by the mapper, with the result reduced by the reducer.
		    
		    Mapper, reducer, and all *_args must be pickleable.
		    
		    The mapper must be a callable expecting at least one argument, 'rows'.
		    'rows' is always the first argument; if any extra arguments are passed 
		    via mapper_args, they will come after it.
		    'rows' will be a numpy array of table records (with named columns)

		    The mapper must return a sequence of key-value pairs. All key-value
		    pairs will be merged by key into (key, [values..]) pairs that shall
		    be passed to the reducer.

		    The reducer must expect two parameters, the first being the key
		    and the second being a sequence of all values that the mappers
		    returned for that key. The return value of the reducer is passed back
		    to the user and is user-defined.
   
		    If the reducer is None, only the mapping step is performed and the
		    return value of the mapper is passed to the user.
		"""
		partspecs = dict()

		# Add explicitly requested cells
		for cell_id in cells:
			partspecs[cell_id] = [(None, None)]

		# Add cells within bounds
		if len(cells) == 0 or bounds is not None:
			partspecs.update(self.qengine.root.get_cells(bounds, include_cached=include_cached))

		# Tell _mapper not to test spacetime boundaries if the user requested so
		if not testbounds:
			partspecs = dict([ (cell_id, None) for (cell_id, _) in partspecs.iteritems() ])

		# Reorganize cells to a per-static-cell basis, if requested
		if group_by_static_cell:
			pix = self.qengine.root.table.pix
			p2 = defaultdict(list)
			for cell_id, bounds in partspecs.iteritems():
				if pix.is_temporal_cell(cell_id):
					cell_id2 = pix.static_cell_for_cell(cell_id)
				else:
					cell_id2 = cell_id
				p2[cell_id2].append((cell_id, bounds))
			partspecs = p2

			# Resort by time, but make the static cell (if any) be at the end
			def order_by_time(part):
				cell_id, _ = part
				_, _, t = pix._xyt_from_cell_id(cell_id)
				if t == pix.t0:
					t = +np.inf
				return t
			for cell_id, parts in partspecs.iteritems():
				parts.sort(key=order_by_time)

			#for cell_id, parts in partspecs.iteritems():
			#	parts = [ pix._xyt_from_cell_id(cell_id)[2] for cell_id, _ in parts ]
			#	print cell_id, parts
			#exit()
		else:
			partspecs = dict([ (cell_id, [(cell_id, bounds)]) for (cell_id, bounds) in partspecs.iteritems() ])

		# Insert our feeder mapper into the kernel chain
		kernels = list(kernels)
		kernels[0] = (_mapper, kernels[0], self.qengine, include_cached)

		# Append a writer mapper if the query has an INTO clause
		if self.qwriter:
			kernels.append((_into_writer, self.qwriter))

		# start and run the workers
		pool = pool2.Pool(nworkers)
		yielded = False
		for result in pool.map_reduce_chain(partspecs.items(), kernels, progress_callback=progress_callback):
			yield result
			yielded = True

		# Yield an empty row, if requested
		# WARNING: This is NOT a flag designed for use by users -- it is only to be used from .fetch()!
		if not yielded and _yield_empty:
			if self.qwriter:
				yield 0, self.qwriter.peek()
			else:
				yield 0, self.qengine.peek()

	def iterate(self, bounds=None, include_cached=False, cells=[], return_blocks=False, filter=None, testbounds=True, nworkers=None, progress_callback=None, _yield_empty=False):
		""" Yield rows (either on a row-by-row basis if return_blocks==False
		    or in chunks (numpy structured array)) within the
		    given bounds. Calls 'filter' callable (if given) to filter
		    the returned rows.

		    See the documentation for Table.fetch for discussion of
		    'filter' callable.
		"""

		mapper = filter if filter is not None else _iterate_mapper

		for (cell_id, rows) in self.execute(
				[mapper], bounds, include_cached,
				cells=cells, testbounds=testbounds, nworkers=nworkers, progress_callback=progress_callback,
				_yield_empty=_yield_empty):
			if return_blocks:
				yield rows
			else:
				for row in rows:
					yield row

	def fetch(self, bounds=None, include_cached=False, cells=[], filter=None, testbounds=True, nworkers=None, progress_callback=None):
		""" Return a table (numpy structured array) of all rows within the
		    given bounds. Calls 'filter' callable (if given) to filter
		    the returned rows. Returns None if there are no rows to return.

		    The 'filter' callable should expect a single argument, rows,
		    being the set of rows (numpy structured array) to filter. It
		    must return the set of filtered rows (also as numpy structured
		    array). E.g., identity filter function would be:
		    
		    	def identity(rows):
		    		return rows

		    while a function filtering on column 'r' may look like:
		    
		    	def r_filter(rows):
		    		return rows[rows['r'] < 21.5]
		   
		    The filter callable must be piclkeable. Extra arguments to
		    filter may be given in 'filter_args'
		"""

		ret = None
		for rows in self.iterate(
				bounds, include_cached,
				cells=cells, return_blocks=True, filter=filter, testbounds=testbounds,
				_yield_empty=True,
				nworkers=nworkers, progress_callback=progress_callback):
			# ensure enough memory has been allocated (and do it
			# intelligently if not)
			if ret is None:
				ret = rows
				nret = len(rows)
			else:
				while len(ret) < nret + len(rows):
					ret.resize(2 * max(len(ret),1))

				# append
				ret[nret:nret+len(rows)] = rows
				nret = nret + len(rows)

		if ret is not None:
			ret.resize(nret)

		return ret

	def fetch_cell(self, cell_id, include_cached=False):
		""" Execute the query on a given (single) cell.

		    Does not launch extra workers, nor shows the
		    progress bar.
		"""
		return self.fetch(cells=[cell_id], include_cached=include_cached, nworkers=1, progress_callback=pool2.progress_pass)

class DB(object):
	path = None		# Root of the database, where all tables reside

	def __init__(self, path):
		self.path = path
		
		if not os.path.isdir(path):
			raise Exception('"%s" is not an acessible directory.' % (path))

		self.tables = dict()	# A cache of table instances

	@contextmanager
	def lock(self, retries=-1):
		""" Lock the database for reading/writing """
		# create a lock file
		lockfile = self.path + '/dblock.lock'
		utils.shell('/usr/bin/lockfile -1 -r%d "%s"' % (retries, lockfile) )

		# yield self
		yield self

		# unlock
		os.unlink(lockfile)

	def resolve_uri(self, uri):
		""" Resolve the given URI into something that can be
		    passed on to file() for loading.
		"""
		if uri[:4] != 'lsd:':
			return uri

		_, tabname, _ = uri.split(':', 2)
		return self.table(tabname).resolve_uri(uri)

	@contextmanager
	def open_uri(self, uri, mode='r', clobber=True):
		if uri[:4] != 'lsd:':
			# Interpret this as a general URL
			import urllib

			f = urllib.urlopen(uri)
			yield f

			f.close()
		else:
			# Pass on to the controlling table
			_, tabname, _ = uri.split(':', 2)
			with self.table(tabname).open_uri(uri, mode, clobber) as f:
				yield f

	def query(self, query, locals={}):
		return Query(self, query, locals=locals)

	def _aux_create_table(self, table, tname, schema):
		schema = copy.deepcopy(schema)

		# Remove any extra fields off schema['column']
		schema['columns'] = [ v[:2] for v in schema['columns'] ]

		table.create_cgroup(tname, schema)

	def create_table(self, tabname, tabdef):
		"""
			Creates the table given the extended schema description.
		"""
		table = self.table(tabname, create=True)

		# Add fgroups
		if 'fgroups' in tabdef:
			for fgroup, fgroupdef in tabdef['fgroups'].iteritems():
				table.define_fgroup(fgroup, fgroupdef)

		# Add filters
		table.set_default_filters(**tabdef.get('filters', {}))

		# Add column groups
		tschema = tabdef['schema']

		# Ensure we create the primary table first
		schemas = []
		for tname, schema in tschema.iteritems():
			schemas.insert(np.where('primary_key' in schema, 0, len(schemas)), (tname, schema))
		assert len(schemas) and 'primary_key' in schemas[0][1]
		for tname, schema in schemas:
			self._aux_create_table(table, tname, schema)

		# Add aliases (must do this last, as the aliased columns have to exist)
		if 'aliases' in tabdef:
			for alias, colname in tabdef['aliases'].iteritems():
				table.define_alias(alias, colname)

		return table

	def define_join(self, name, type, **joindef):
		#- .join file structure:
		#	- indirect joins:			Example: ps1_obj:ps1_det.join
		#		type:	indirect		"type": "indirect"
		#		m1:	(tab1, col1)		"m1:":	["ps1_obj2det", "id1"]
		#		m2:	(tab2, col2)		"m2:":	["ps1_obj2det", "id2"]
		#	- equijoins:				Example: ps1_det:ps1_exp.join		(!!!NOT IMPLEMENTED!!!)
		#		type:	equi			"type": "equijoin"
		#		id1:	colA			"id1":	"exp_id"
		#		id2:	colB			"id2":	"exp_id"
		#	- direct joins:				Example: ps1_obj:ps1_calib.join.json	(!!!NOT IMPLEMENTED!!!)
		#		type:	direct			"type": "direct"

		fname = '%s/%s.join' % (self.path, name)
		if os.access(fname, os.F_OK):
			raise Exception('Join relation %s already exist (in file %s)' % (name, fname))

		joindef['type'] = type
	
		f = open(fname, 'w')
		f.write(json.dumps(joindef, indent=4, sort_keys=True))
		f.close()

	def define_default_join(self, tableA, tableB, type, **joindef):
		return self.define_join('.%s:%s' % (tableA, tableB), type, **joindef)

	def table_exists(self, tabname):
		try:
			self.table(tabname)
			return True
		except IOError:
			return False

	def table(self, tabname, create=False):
		""" Given the table name, returns a Table object.
		"""
		if tabname not in self.tables:
			path = '%s/%s' % (self.path, tabname)
			if not create:
				self.tables[tabname] = Table(path)
			else:
				self.tables[tabname] = Table(path, name=tabname, mode='c')
		else:
			assert not create

		return self.tables[tabname]

	def construct_join_tree(self, from_clause):
		tablist = []

		# Instantiate the tables
		for tabname, tabpath, jointype in from_clause:
			tablist.append( ( tabname, (TableEntry(self.table(tabpath), tabname), jointype) ) )
		tables = dict(tablist)

		# Discover and set up JOIN links based on defined JOIN relations
		# TODO: Expand this to allow the 'joined to via' and related syntax, once it becomes available in the parser
		for tabname, (e, _) in tablist:
			# Check for tables that can be joined onto this one (where this one is on the right hand side of the relation)
			# Look for default .join files named ".<tabname>:*.join"
			pattern = "%s/.%s:*.join" % (self.path, tabname)
			for fn in glob.iglob(pattern):
				jtabname = fn[fn.rfind(':')+1:fn.rfind('.join')]
				if jtabname not in tables:
					continue

				je, jkind = tables[jtabname]
				if je.relation is not None:	# Already joined
					continue

				je.relation = create_join(self, fn, jkind, e.table, je.table)
				e.joins.append(je)
	
		# Discover the root (the one and only one table that has no links pointing to it)
		root = None
		for _, (e, jkind) in tables.iteritems():
			if e.relation is None:
				assert root is None	# Can't have more than one roots
				assert jkind == 'inner'	# Can't have something like 'ps1_obj(outer)' on root table
				root = e
		assert root is not None			# Can't have zero roots
	
		# TODO: Verify all tables are reachable from the root (== there are no circular JOINs)
	
		# Return the root of the tree, and a dict of all the Table instances
		return root, dict((  (tabname, table) for (tabname, (table, _)) in tables.iteritems() ))

	def build_neighbor_cache(self, tabname, margin_x_arcsec=30):
		""" Cache the objects found within margin_x (arcsecs) of
		    each cell into neighboring cells as well, to support
		    efficient nearest-neighbor lookups.

		    This routine works in tandem with _cache_maker_mapper
		    and _cache_maker_reducer auxilliary routines.
		"""
		margin_x = np.sqrt(2.) / 180. * (margin_x_arcsec/3600.)

		ntotal = 0
		ncells = 0
		query = "_ID, _LON, _LAT FROM %s" % (tabname)
		for (_, ncached) in self.query(query).execute([
						(_cache_maker_mapper,  margin_x, self, tabname),
						(_cache_maker_reducer, self, tabname)
					]):
			ntotal = ntotal + ncached
			ncells = ncells + 1
			#print self._cell_prefix(cell_id), ": ", ncached, " cached objects"
		print "Total %d cached objects in %d cells" % (ntotal, ncells)

	def compute_summary_stats(self, *tables):
		""" Compute frequently used summary statistics and
		    store them into the dbinfo file. This should be called
		    to refresh the stats after insertions.
		"""
		from tasks import compute_counts
		from fcache import TabletTreeCache
		for tabname in tables:
			table = self.table(tabname)

			# Scan the number of rows
			table._nrows = compute_counts(self, tabname)
			table._store_schema()

			# Compute the tablet tree cache
			data_path = table._get_cgroup_data_path(table.primary_cgroup)
			pattern   = table._tablet_filename(table.primary_cgroup)
			table.tablet_tree = TabletTreeCache().create_cache(table.pix, data_path, pattern, table.path + '/tablet_tree.pkl')

###################################################################
## Auxilliary functions implementing DB.build_neighbor_cache
## functionallity
def _cache_maker_mapper(qresult, margin_x, db, tabname):
	# Map: fetch all rows to be copied to adjacent cells,
	# yield them keyed by destination cell ID
	for rows in qresult:
		cell_id = rows.cell_id
		p, _ = qresult.pix.cell_bounds(cell_id)

		# Find all objects within 'margin_x' from the cell pixel edge
		# The pixel can be a rectangle, or a triangle, so we have to
		# handle both situations correctly.
		(x1, x2, y1, y2) = p.boundingBox()
		d = x2 - x1
		(cx, cy) = p.center()
		if p.nPoints() == 4:
			s = 1. - 2*margin_x / d
			p.scale(s, s, cx, cy)
		elif p.nPoints() == 3:
			if (cx - x1) / d > 0.5:
				ax1 = x1 + margin_x*(1 + 2**.5)
				ax2 = x2 - margin_x
			else:
				ax1 = x1 + margin_x
				ax2 = x2 - margin_x*(1 + 2**.5)

			if (cy - y1) / d > 0.5:
				ay2 = y2 - margin_x
				ay1 = y1 + margin_x*(1 + 2**.5)
			else:
				ay1 = y1 + margin_x
				ay2 = y2 - margin_x*(1 + 2**.5)
			p.warpToBox(ax1, ax2, ay1, ay2)
		else:
			raise Exception("Expecting the pixel shape to be a rectangle or triangle!")

		# Now reject everything not within the margin, and
		# (for simplicity) send everything within the margin,
		# no matter close to which edge it actually is, to
		# all neighbors.
		(x, y) = bhpix.proj_bhealpix(rows['_LON'], rows['_LAT'])
		inMargin = ~p.isInsideV(x, y)

		if not inMargin.any():
			continue

		# Fetch only the rows that are within the margin
		idsInMargin = rows['_ID'][inMargin]
		q           = db.query("* FROM '%s' WHERE np.in1d(_ID, idsInMargin, assume_unique=True)" % tabname, {'idsInMargin': idsInMargin} )
		data        = q.fetch_cell(cell_id)

		# Send these to neighbors
		if data is not None:
			for neighbor in qresult.pix.neighboring_cells(cell_id):
				yield (neighbor, data)

		##print "Scanned margins of %s*.h5 (%d objects)" % (db.table(tabname)._cell_prefix(cell_id), len(data))

def _cache_maker_reducer(kv, db, tabname):
	# Cache all rows to be cached in this cell
	cell_id, rowblocks = kv

	# Delete existing neighbors
	table = db.table(tabname)
	table.drop_row_group(cell_id, 'cached')

	# Add to cache
	ncached = 0
	for rows in rowblocks:
		table.append(rows, cell_id=cell_id, group='cached')
		ncached += len(rows)
		##print cell_id, len(rows), table.pix.path_to_cell(cell_id)

	# Return the number of new rows cached into this cell
	yield cell_id, ncached

###############################################################
# Aux. functions implementing Query.iterate() and
# Query.fetch()
class TableProxy:
	coldict = None
	prefix = None

	def __init__(self, coldict, prefix):
		self.coldict = coldict
		self.prefix = prefix

	def __getattr__(self, name):
		return self.coldict[self.prefix + '.' + name]

def _mapper(partspec, mapper, qengine, include_cached):
	(group_cell_id, cell_list) = partspec
	mapper, mapper_args = utils.unpack_callable(mapper)

	# Pass on to mapper (and yield its results)
	qresult = qengine.on_cells(cell_list, include_cached)
	for result in mapper(qresult, *mapper_args):
		yield result

def _iterate_mapper(qresult):
	for rows in qresult:
		if len(rows):	# Don't return empty sets. TODO: Do we need this???
			yield (rows.cell_id, rows)

def _into_writer(kw, qwriter):
	cell_id, irows = kw
	for rows in irows:
		rows = qwriter.write(cell_id, rows)
		yield rows

###############################

def _fitskw_dumb(hdrs, kw):
	# Easy way
	import StringIO
	res = []
	for ahdr in hdrs:
		hdr = pyfits.Header( txtfile=StringIO(ahdr) )
		res.append(hdr[kw])
	return res

def fits_quickparse(header):
	""" An ultra-simple FITS header parser. Does not support
	    CONTINUE statements, HIERARCH, or anything of the sort;
	    just plain vanilla:
	    	key = value / comment
	    one-liners. The upshot is that it's fast.

	    Assumes each 80-column line has a '\n' at the end
	"""
	res = {}
	for line in header.split('\n'):
		at = line.find('=')
		if at == -1 or at > 8:
			continue

		# get key
		key = line[0:at].strip()

		# parse value (string vs number, remove comment)
		val = line[at+1:].strip()
		if val[0] == "'":
			# string
			val = val[1:val[1:].find("'")]
		else:
			# number or T/F
			at = val.find('/')
			if at == -1: at = len(val)
			val = val[0:at].strip()
			if val.lower() in ['t', 'f']:
				# T/F
				val = val.lower() == 't'
			else:
				# Number
				val = float(val)
				if int(val) == val:
					val = int(val)
		res[key] = val
	return res;

def fitskw(hdrs, kw, default=0):
	""" Intelligently extract a keyword kw from an arbitrarely
	    shaped object ndarray of FITS headers.
	"""
	shape = hdrs.shape
	hdrs = hdrs.reshape(hdrs.size)

	res = []
	cache = dict()
	for ahdr in hdrs:
		ident = id(ahdr)
		if ident not in cache:
			if ahdr is not None:
				#hdr = pyfits.Header( txtfile=StringIO(ahdr) )
				hdr = fits_quickparse(ahdr)
				cache[ident] = hdr.get(kw, default)
			else:
				cache[ident] = default
		res.append(cache[ident])

	#assert res == _fitskw_dumb(hdrs, kw)

	res = np.array(res).reshape(shape)
	return res

def ffitskw(uris, kw, default = False, db=None):
	""" Intelligently load FITS headers stored in
	    <uris> ndarray, and fetch the requested
	    keyword from them.
	"""

	if len(uris) == 0:
		return np.empty(0)

	uuris, idx = np.unique(uris, return_inverse=True)
	idx = idx.reshape(uris.shape)

	if db is None:
		# _DB is implicitly defined inside queries
		global _DB
		db = _DB

	ret = []
	for uri in uuris:
		if uri is not None:
			with db.open_uri(uri) as f:
				hdr_str = f.read()
			hdr = fits_quickparse(hdr_str)
			ret.append(hdr.get(kw, default))
		else:
			ret.append(default)

	# Broadcast
	ret = np.array(ret)[idx]

	assert ret.shape == uris.shape, '%s %s %s' % (ret.shape, uris.shape, idx.shape)

	return ret

def OBJECT(uris, db=None):
	""" Dereference blobs referred to by URIs,
	    assuming they're pickled Python objects.
	"""
	return deref(uris, db, True)

def BLOB(uris, db=None):
	""" Dereference blobs referred to by URIs,
	    loading them as plain files
	"""
	return deref(uris, db, False)

def deref(uris, db=None, unpickle=False):
	""" Dereference blobs referred to by URIs,
	    either as BLOBs or Python objects
	"""
	if len(uris) == 0:
		return np.empty(0, dtype=object)

	uuris, idx = np.unique(uris, return_inverse=True)
	idx = idx.reshape(uris.shape)

	if db is None:
		# _DB is implicitly defined inside queries
		db = _DB

	ret = np.empty(len(uuris), dtype=object)
	for i, uri in enumerate(uuris):
		if uri is not None:
			with db.open_uri(uri) as f:
				if unpickle:
					ret[i] = cPickle.load(f)
				else:
					ret[i] = f.read()
		else:
			ret[i] = None

	# Broadcast
	ret = np.array(ret)[idx]

	assert ret.shape == uris.shape, '%s %s %s' % (ret.shape, uris.shape, idx.shape)

	return ret

###############################

def test_kernel(qresult):
	for rows in qresult:
		yield qresult.cell_id, len(rows)

if __name__ == "__main__":
	def test():
		from tasks import compute_coverage
		db = DB('../../../ps1/db')
		query = "_LON, _LAT FROM sdss(outer), ps1_obj, ps1_det, ps1_exp(outer)"
		compute_coverage(db, query)
		exit()

		import bounds
		query = "_ID, ps1_det._ID, ps1_exp._ID, sdss._ID FROM '../../../ps1/sdss'(outer) as sdss, '../../../ps1/ps1_obj' as ps1_obj, '../../../ps1/ps1_det' as ps1_det, '../../../ps1/ps1_exp'(outer) as ps1_exp WHERE True"
		query = "_ID, ps1_det._ID, ps1_exp._ID, sdss._ID FROM sdss(outer), ps1_obj, ps1_det, ps1_exp(outer)"
		cell_id = np.uint64(6496868600547115008 + 0xFFFFFFFF)
		include_cached = False
		bounds = [(bounds.rectangle(120, 20, 160, 60), None)]
		bounds = None
#		for rows in QueryEngine(query).on_cell(cell_id, bounds, include_cached):
#			pass;

		db = DB('../../../ps1/db')
#		dq = db.query(query)
#		for res in dq.execute([test_kernel], bounds, include_cached, nworkers=1):
#			print res

		dq = db.query(query)
#		for res in dq.iterate(bounds, include_cached, nworkers=1):
#			print res

		res = dq.fetch(bounds, include_cached, nworkers=1)
		print len(res)

	test()