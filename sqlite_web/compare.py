"""
Row-by-row comparison of two SQLite result sets.

The two sides are independent read queries, each runnable against any loaded
database file. Each side's rows are materialized once into a private scratch
database (temp tables whose columns have BLOB affinity, so values keep their
exact storage class), and all diffing -- added / deleted / changed rows --
is then done with SQL.

Matching is strict in both directions required by the UI: NULL only equals
NULL, and values with different storage classes (e.g. 1 and 1.0, '1') are
different rows or different values.

Nothing here ever writes to either source database. The sources are opened
with mode=ro and only SELECT is allowed (validated with the same read-check
the query tab uses).
"""

import os
import tempfile
import urllib.parse

from peewee import SqliteDatabase
from playhouse.dataset import DataSet

try:
    from sqlite_web.executor import is_read, split_statements
except ImportError:
    from executor import is_read, split_statements


DIFF_ROWS_PER_PAGE = 50
FETCH_BATCH = 1000


class CompareError(Exception):
    pass


def _qi(name):
    """Quote an identifier."""
    return '"%s"' % name.replace('"', '""')


def type_name(value):
    # Mirrors SQLite's five storage classes (bool never comes back from the
    # sqlite3 driver, but normalize defensively).
    if value is None:
        return 'null'
    if isinstance(value, (bytes, bytearray, memoryview)):
        return 'blob'
    if isinstance(value, bool):
        return 'integer'
    if isinstance(value, int):
        return 'integer'
    if isinstance(value, float):
        return 'real'
    return 'text'


def values_equal(left, right):
    """NULL- and type-strict equality, used wherever the SQL results are
    compared in Python."""
    if type_name(left) != type_name(right):
        return False
    if isinstance(left, memoryview):
        left = bytes(left)
    if isinstance(right, memoryview):
        right = bytes(right)
    return left == right


def _normalize(value):
    # The sqlite3 driver returns BLOBs as memoryview; store bytes.
    if isinstance(value, memoryview):
        return bytes(value)
    return value


def _source_uri(path):
    # mode=ro keeps the source untouched and lets concurrent users keep
    # working. The path is percent-encoded so '?' or '#' in a filename
    # cannot inject URI parameters.
    return 'file:%s?mode=ro' % urllib.parse.quote(os.path.abspath(path), safe='')


def _strict_key_match(left_alias, right_alias, keys):
    # IS is NULL-aware; the typeof guard makes the match storage-class
    # strict (1 and 1.0 or '1' are different rows).
    parts = []
    for key in keys:
        lk = '%s.%s' % (left_alias, _qi(key))
        rk = '%s.%s' % (right_alias, _qi(key))
        parts.append('(%s IS %s AND typeof(%s) = typeof(%s))' % (lk, rk, lk, rk))
    return ' AND '.join(parts)


class Comparison(object):
    def __init__(self, sources, keys, page_size=DIFF_ROWS_PER_PAGE):
        """sources is a [(path, sql), (path, sql)] pair; keys is the list of
        column names that identify the same row on both sides."""
        self.sources = sources
        self.keys = list(keys)
        self.page_size = max(1, page_size)
        self.columns = [None, None]
        self.left_only = []
        self.right_only = []
        self.shared_nonkey = []
        self._scratch_path = None
        self._scratch = None
        self.summary = None

    #
    # Setup: validate the two read queries, materialize them, index keys.
    #

    def _read_side(self, index, path, sql, table_name):
        if not sql or not sql.strip():
            raise CompareError('Side %d: a SELECT query is required.' %
                               (index + 1))
        statements = split_statements(sql)
        if len(statements) != 1:
            raise CompareError(
                'Side %d: exactly one SELECT statement is required (got %d).' %
                (index + 1, len(statements)))

        # The DataSet constructor introspects the database and leaves the
        # connection open, so do not connect() again with reuse_if_open=False.
        reader = DataSet(SqliteDatabase(_source_uri(path), uri=True))
        try:
            reader.query('PRAGMA busy_timeout = 5000')
            try:
                readable = is_read(reader, sql)
            except Exception as exc:
                raise CompareError('Side %d: %s' % (index + 1, exc))
            if not readable:
                raise CompareError(
                    'Side %d: only a single read-only SELECT can be compared.'
                    % (index + 1))
            try:
                cursor = reader.query(sql)
            except Exception as exc:
                raise CompareError('Side %d: %s' % (index + 1, exc))
            if cursor.description is None:
                raise CompareError(
                    'Side %d: the statement did not return any rows.' %
                    (index + 1))
            columns = [d[0] for d in cursor.description]
            if len(set(columns)) != len(columns):
                raise CompareError(
                    'Side %d: duplicate column names in the result set.' %
                    (index + 1))

            conn = self._scratch.connection()
            defs = ', '.join('%s BLOB' % _qi(c) for c in columns)
            conn.execute('CREATE TEMP TABLE %s (%s)' % (table_name, defs))
            insert = 'INSERT INTO temp.%s VALUES (%s)' % (
                table_name, ', '.join('?' * len(columns)))
            while True:
                batch = cursor.fetchmany(FETCH_BATCH)
                if not batch:
                    break
                conn.executemany(
                    insert,
                    [tuple(_normalize(v) for v in row) for row in batch])
        finally:
            reader.close()
        return columns

    def __enter__(self):
        (l_path, l_sql), (r_path, r_sql) = self.sources
        fd, self._scratch_path = tempfile.mkstemp(prefix='sqlite-web-compare-',
                                                  suffix='.db')
        os.close(fd)
        self._scratch = SqliteDatabase(self._scratch_path)
        self._scratch.connect()
        try:
            return self._setup(l_path, l_sql, r_path, r_sql)
        except Exception:
            # Any failure -- bad key, non-unique keys, a source query that
            # errors during setup -- must remove the scratch database.
            self._close()
            raise

    def _setup(self, l_path, l_sql, r_path, r_sql):
        left_cols = self._read_side(0, l_path, l_sql, 'l')
        right_cols = self._read_side(1, r_path, r_sql, 'r')
        self.columns = [left_cols, right_cols]

        lset, rset = set(left_cols), set(right_cols)
        self.left_only = [c for c in left_cols if c not in rset]
        self.right_only = [c for c in right_cols if c not in lset]
        common = [c for c in left_cols if c in rset]
        if not self.keys:
            raise CompareError('Choose at least one key column.')
        missing = [k for k in self.keys if k not in lset or k not in rset]
        if missing:
            raise CompareError(
                'Key column(s) must exist on both sides: %s' %
                ', '.join(missing))
        if len(set(self.keys)) != len(self.keys):
            raise CompareError('A key column was selected more than once.')
        self.shared_nonkey = [c for c in common if c not in set(self.keys)]

        conn = self._scratch.connection()
        for alias in ('l', 'r'):
            for i, key in enumerate(self.keys):
                # Temp schema is inferred from the table; CREATE INDEX
                # itself does not accept a schema-qualified table name.
                conn.execute('CREATE INDEX %s_k%d ON %s (%s)' % (
                    alias, i, alias, _qi(key)))
        self._check_unique_keys(conn, 'l', 'left')
        self._check_unique_keys(conn, 'r', 'right')
        # Data is loaded; lock the scratch connection down as well.
        conn.execute('PRAGMA query_only = 1')
        self.summary = self._build_summary(conn)
        return self

    def __exit__(self, exc_type, exc, tb):
        self._close()

    def _close(self):
        if self._scratch is not None and not self._scratch.is_closed():
            self._scratch.close()
        if self._scratch_path and os.path.exists(self._scratch_path):
            os.unlink(self._scratch_path)
        self._scratch_path = None

    def _check_unique_keys(self, conn, alias, label):
        # typeof in the grouping keeps int/real/text apart, matching the
        # strict join used everywhere else.
        group = ', '.join('%s, typeof(%s)' % (_qi(k), _qi(k))
                          for k in self.keys)
        row = conn.execute(
            'SELECT 1 FROM temp.%s GROUP BY %s HAVING COUNT(*) > 1 LIMIT 1' %
            (alias, group)).fetchone()
        if row is not None:
            raise CompareError(
                'Key column(s) %s are not unique on the %s side; refine the '
                'key so every row is identified unambiguously.' %
                (', '.join(self.keys), label))

    #
    # Summary counts.
    #

    def _exists(self, outer, inner):
        return 'NOT EXISTS (SELECT 1 FROM temp.%s WHERE %s)' % (
            inner, _strict_key_match(inner, outer, self.keys))

    def _changed_predicate(self):
        parts = []
        for col in self.shared_nonkey:
            lc = 'l.%s' % _qi(col)
            rc = 'r.%s' % _qi(col)
            parts.append('(%s IS NOT %s OR typeof(%s) != typeof(%s))' %
                         (lc, rc, lc, rc))
        return ' OR '.join(parts) if parts else '0'

    def _build_summary(self, conn):
        n_left = conn.execute('SELECT COUNT(*) FROM temp.l').fetchone()[0]
        n_right = conn.execute('SELECT COUNT(*) FROM temp.r').fetchone()[0]
        n_added = conn.execute(
            'SELECT COUNT(*) FROM temp.r WHERE %s' %
            self._exists('r', 'l')).fetchone()[0]
        n_deleted = conn.execute(
            'SELECT COUNT(*) FROM temp.l WHERE %s' %
            self._exists('l', 'r')).fetchone()[0]
        n_modified = 0
        n_unchanged = 0
        if self.shared_nonkey:
            n_modified = conn.execute(
                'SELECT COUNT(*) FROM temp.r JOIN temp.l ON %s WHERE %s' %
                (_strict_key_match('l', 'r', self.keys),
                 self._changed_predicate())).fetchone()[0]
        n_matched = n_left - n_deleted
        n_unchanged = n_matched - n_modified
        return {
            'left_rows': n_left,
            'right_rows': n_right,
            'added': n_added,
            'deleted': n_deleted,
            'modified': n_modified,
            'unchanged': n_unchanged,
            'total': n_added + n_deleted + n_modified,
        }

    #
    # Paged row fetching per tab.
    #

    def _order_by(self, alias):
        return 'ORDER BY %s' % ', '.join(
            '%s.%s' % (alias, _qi(k)) for k in self.keys)

    def _fetch(self, sql):
        conn = self._scratch.connection()
        offset = (self.page - 1) * self.page_size
        rows = conn.execute(sql, (self.page_size + 1, offset)).fetchall()
        return rows[:self.page_size], len(rows) > self.page_size

    def page_rows(self, tab):
        """Return (rows, has_next) for the given tab at self.page.

        Row layouts:
          added/deleted: full tuples in that side's column order
          modified: key values, then (left, right) pairs for every shared
                    non-key column, then left-only then right-only values
          all: kind (0=deleted,1=added,2=modified), values across the union
                    column order (left order then right-only), then the
                    ORDER BY helper columns (ignored by the template)
        """
        if tab == 'added':
            cols = ', '.join('r.%s' % _qi(c) for c in self.columns[1])
            sql = ('SELECT %s FROM temp.r WHERE %s %s LIMIT ? OFFSET ?' %
                   (cols, self._exists('r', 'l'), self._order_by('r')))
            return self._fetch(sql)

        if tab == 'deleted':
            cols = ', '.join('l.%s' % _qi(c) for c in self.columns[0])
            sql = ('SELECT %s FROM temp.l WHERE %s %s LIMIT ? OFFSET ?' %
                   (cols, self._exists('l', 'r'), self._order_by('l')))
            return self._fetch(sql)

        if tab == 'modified':
            select = ['r.%s' % _qi(k) for k in self.keys]
            for col in self.shared_nonkey:
                select.append('l.%s' % _qi(col))
                select.append('r.%s' % _qi(col))
            select += ['l.%s' % _qi(c) for c in self.left_only]
            select += ['r.%s' % _qi(c) for c in self.right_only]
            if not self.shared_nonkey:
                return [], False
            sql = (
                'SELECT %s FROM temp.r JOIN temp.l ON %s WHERE %s %s '
                'LIMIT ? OFFSET ?' % (
                    ', '.join(select),
                    _strict_key_match('l', 'r', self.keys),
                    self._changed_predicate(),
                    self._order_by('r')))
            return self._fetch(sql)

        # 'all': deleted, added, modified as one ordered stream.
        union_cols = self.columns[0] + self.right_only
        n_union = len(union_cols)

        def values(alias, present):
            out = []
            for col in union_cols:
                if col in present:
                    out.append('%s.%s' % (alias, _qi(col)))
                else:
                    out.append('NULL')
            return out

        sort = ['%s.%s' % ('l', _qi(k)) for k in self.keys]
        deleted_sel = ['0'] + values('l', set(self.columns[0])) + sort
        added_sel = ['1'] + values('r', set(self.columns[1])) + \
            ['r.%s' % _qi(k) for k in self.keys]
        modified_sel = ['2'] + values('l', set(self.columns[0])) + \
            ['r.%s' % _qi(k) for k in self.keys]
        branches = [
            'SELECT %s FROM temp.l WHERE %s' % (
                ', '.join(deleted_sel), self._exists('l', 'r')),
            'SELECT %s FROM temp.r WHERE %s' % (
                ', '.join(added_sel), self._exists('r', 'l')),
        ]
        if self.shared_nonkey:
            branches.append(
                'SELECT %s FROM temp.r JOIN temp.l ON %s WHERE %s' % (
                    ', '.join(modified_sel),
                    _strict_key_match('l', 'r', self.keys),
                    self._changed_predicate()))
        order_names = ['dk'] + ['sk%d' % i for i in range(len(self.keys))]
        # Alias the helper columns on the first branch; UNION takes result
        # column names from it.
        first = branches[0].split(' FROM ', 1)
        first_select = first[0].split(', ')
        for i, alias in enumerate(order_names):
            first_select[i] = '%s AS %s' % (first_select[i], alias)
        branches[0] = ' FROM '.join([', '.join(first_select), first[1]])
        sql = ('SELECT * FROM (%s) ORDER BY %s LIMIT ? OFFSET ?' % (
            ' UNION ALL '.join(branches),
            ', '.join(order_names)))
        rows, has_next = self._fetch(sql)
        # Drop the ORDER BY helper columns before handing rows to the view.
        rows = [row[:1 + n_union] for row in rows]
        return rows, has_next

    #
    # Per-cell change flags for the modified tab, computed in Python with
    # the same strict equality used to find the rows.
    #

    def modified_changes(self, row):
        flags = []
        pos = len(self.keys)
        for col in self.shared_nonkey:
            flags.append(not values_equal(row[pos], row[pos + 1]))
            pos += 2
        return flags
