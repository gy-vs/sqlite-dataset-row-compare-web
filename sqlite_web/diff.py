"""
Row-by-row diff engine.

Two result sets are matched on a user-chosed set of key columns. Rows are
classified as added (only on side B), deleted (only on side A) or changed
(key present on both sides, at least one common column differs).

Values carry their SQLite storage type, so NULL versus a value and values
of different types (e.g. the integer 1 and the text "1") always count as
differences. Everything here is read-only: sides are validated as single
SELECT statements and pulled through the dataset's own connection.
"""

from dataclasses import dataclass, field

try:
    from sqlite_web.executor import is_read, wrap
except ImportError:
    from executor import is_read, wrap


#
# Typed values.
#

def type_tag(value):
    # The sqlite driver never returns bool, but be explicit in case a
    # caller hands us one: SQLite stores it as an integer.
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'integer'
    if isinstance(value, int):
        return 'integer'
    if isinstance(value, float):
        return 'real'
    if isinstance(value, (bytes, bytearray, memoryview)):
        return 'blob'
    return 'text'


def normalize(value):
    if value is None:
        return None
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytearray):
        value = bytes(value)
    return value


def make_cell(value):
    """A cell is a (type-tag, value) pair, so comparisons are type-aware."""
    return type_tag(value), normalize(value)


#
# Result containers.
#

@dataclass
class DiffEntry:
    kind: str  # 'added', 'deleted', 'changed'
    key: tuple
    a: dict = None       # column -> (tag, value), None for an added row
    b: dict = None       # None for a deleted row
    changed_cols: list = field(default_factory=list)
    href_a: str = ''     # Jump back to the row in side A's database.
    href_b: str = ''


@dataclass
class SideData:
    dataset_key: str
    source_type: str     # 'table' or 'query'
    table: str
    sql: str
    columns: list
    rows: dict           # key tuple -> {column: (tag, value)}
    duplicate_keys: int = 0
    duplicate_columns: list = field(default_factory=list)


@dataclass
class DiffResult:
    columns: list = field(default_factory=list)       # display order
    key_columns: list = field(default_factory=list)
    only_a: list = field(default_factory=list)
    only_b: list = field(default_factory=list)
    added: list = field(default_factory=list)
    deleted: list = field(default_factory=list)
    changed: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def total(self):
        return len(self.added) + len(self.deleted) + len(self.changed)

    def section(self, name):
        return {'all': None, 'added': self.added,
                'deleted': self.deleted, 'changed': self.changed}[name]


#
# Sides.
#

def quote_ident(name):
    return '"%s"' % name.replace('"', '""')


def side_sql(source_type, table=None, sql=None):
    if source_type == 'table':
        if not table:
            raise ValueError('A table must be selected.')
        return 'SELECT * FROM %s' % quote_ident(table)
    if not sql or not sql.strip():
        raise ValueError('A query is required.')
    return sql.strip().rstrip('; \t\r\n')


def read_columns(dataset, sql):
    """Validate a single read query and return its column names.

    Two checks, because either alone is not enough. EXPLAIN compiles the
    statement without running it, which surfaces a bad table/column name
    with a precise message (a wrapped LIMIT 0 reports the same failure as a
    non-SELECT). The wrapped LIMIT 0 then proves the statement is a
    SELECT-shaped read and yields the result columns.
    """
    head = sql.lstrip('( \t\r\n').lstrip().upper()
    if not (head.startswith('SELECT') or head.startswith('WITH')):
        raise ValueError('Each side must be a single read-only SELECT '
                         'query.')
    try:
        # Prepares the statement (catching unknown columns) without
        # executing it.
        dataset.query('EXPLAIN %s' % sql).fetchall()
    except Exception as exc:
        raise ValueError(str(exc))
    try:
        cursor = dataset.query(wrap(sql, limit=0))
    except Exception as exc:
        raise ValueError('Each side must be a single read-only SELECT '
                         'query. (%s)' % exc)
    if cursor.description is None:
        raise ValueError('The query did not return any columns.')
    return [d[0] for d in cursor.description]


def fetch_side(dataset, dataset_key, source_type, key_columns, table=None,
               sql=None, batch_size=1000):
    sql = side_sql(source_type, table, sql)
    columns = read_columns(dataset, sql)

    seen = set()
    duplicate_columns = []
    for name in columns:
        if name in seen:
            duplicate_columns.append(name)
        seen.add(name)

    # Keep the first occurrence of a duplicated result-column name.
    unique_columns = []
    seen = set()
    for name in columns:
        if name not in seen:
            unique_columns.append(name)
            seen.add(name)

    cursor = dataset.query(sql)
    rows = {}
    duplicate_keys = 0
    while True:
        batch = cursor.fetchmany(batch_size)
        if not batch:
            break
        for raw in batch:
            row = {}
            for name, value in zip(columns, raw):
                if name in row:
                    continue
                row[name] = make_cell(value)
            key = tuple(row[c] for c in key_columns)
            if key in rows:
                duplicate_keys += 1
                continue
            rows[key] = row

    return SideData(
        dataset_key=dataset_key,
        source_type=source_type,
        table=table or '',
        sql=sql,
        columns=unique_columns,
        rows=rows,
        duplicate_keys=duplicate_keys,
        duplicate_columns=sorted(set(duplicate_columns)))


#
# The diff itself.
#

def display_columns(side_a, side_b, key_columns):
    cols = list(key_columns)
    for name in side_a.columns:
        if name not in cols and name in side_b.columns:
            cols.append(name)
    only_a = [c for c in side_a.columns if c not in side_b.columns]
    only_b = [c for c in side_b.columns if c not in side_a.columns]
    return cols, only_a, only_b


def compute_diff(side_a, side_b, key_columns, href_a='', href_b=''):
    columns, only_a, only_b = display_columns(side_a, side_b, key_columns)

    added, deleted, changed = [], [], []
    keys_a, keys_b = side_a.rows, side_b.rows
    common = set(keys_a).intersection(keys_b)

    for key, row_a in keys_a.items():
        if key not in keys_b:
            deleted.append(DiffEntry('deleted', key, a=row_a,
                                     href_a=href_a))
            continue
        row_b = keys_b[key]
        changed_cols = [c for c in columns
                        if c not in key_columns and
                        row_a.get(c) != row_b.get(c)]
        if changed_cols:
            changed.append(DiffEntry('changed', key, a=row_a, b=row_b,
                                     changed_cols=changed_cols,
                                     href_a=href_a, href_b=href_b))

    # Side B's own order drives the added list, so new rows surface in the
    # order they appear in the newer snapshot.
    for key, row_b in keys_b.items():
        if key not in keys_a:
            added.append(DiffEntry('added', key, b=row_b, href_b=href_b))

    warnings = []
    for label, side in (('left', side_a), ('right', side_b)):
        if side.duplicate_columns:
            warnings.append(
                'The %s side has duplicate column names (%s); only the '
                'first occurrence of each was used.' % (
                    label, ', '.join(side.duplicate_columns)))
        if side.duplicate_keys:
            warnings.append(
                'The %s side has %s row(s) with a duplicated key; only '
                'the first row for each key was compared.' % (
                    label, side.duplicate_keys))

    return DiffResult(
        columns=columns,
        key_columns=key_columns,
        only_a=only_a,
        only_b=only_b,
        added=added,
        deleted=deleted,
        changed=changed,
        warnings=warnings)
