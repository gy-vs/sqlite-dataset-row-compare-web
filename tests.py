import os
import shutil
import sqlite3
import tempfile
import unittest

from peewee import SqliteDatabase
from playhouse.dataset import DataSet

from sqlite_web import sqlite_web as sw
from sqlite_web.executor import Result
from sqlite_web.executor import is_read
from sqlite_web.executor import key_decode
from sqlite_web.executor import key_encode
from sqlite_web.executor import run_one
from sqlite_web.executor import run_script
from sqlite_web.executor import split_statements
from sqlite_web.executor import wrap
from sqlite_web.executor import typed_key_decode
from sqlite_web.executor import typed_key_encode
from sqlite_web import diff as diff_mod


class BaseExecutorTestCase(unittest.TestCase):
    def setUp(self):
        self.db = SqliteDatabase(':memory:')
        self.dataset = DataSet(self.db)
        self.dataset.query('CREATE TABLE users (id INTEGER PRIMARY KEY, '
                           'username TEXT)')
        for username in ('huey', 'mickey', 'zaizee'):
            self.dataset.query('INSERT INTO users (username) VALUES (?)',
                               (username,))

    def user_count(self):
        return self.dataset.query('SELECT COUNT(*) FROM users').fetchone()[0]


class TestRunOne(BaseExecutorTestCase):
    def test_read_paginates(self):
        r = run_one(self.dataset, 'SELECT * FROM users', page_size=2)
        self.assertEqual(r.kind, 'rows')
        self.assertEqual(r.columns, ['id', 'username'])
        self.assertEqual(len(r.rows), 2)
        self.assertTrue(r.has_next)
        self.assertIsNone(r.keys)

        r = run_one(self.dataset, 'SELECT * FROM users', page=2, page_size=2)
        self.assertEqual(len(r.rows), 1)
        self.assertFalse(r.has_next)

    def test_page_bounds(self):
        r = run_one(self.dataset, 'SELECT * FROM users', page=0, page_size=2)
        self.assertEqual(len(r.rows), 2)

        r = run_one(self.dataset, 'SELECT * FROM users', page=99, page_size=2)
        self.assertEqual(r.kind, 'rows')
        self.assertEqual(r.rows, [])
        self.assertFalse(r.has_next)

    def test_ordering(self):
        r = run_one(self.dataset, 'SELECT * FROM users', ordering=-2)
        self.assertEqual(r.rows[0][1], 'zaizee')
        r = run_one(self.dataset, 'SELECT * FROM users', ordering=2)
        self.assertEqual(r.rows[0][1], 'huey')

    def test_write_autocommits(self):
        r = run_one(self.dataset, "INSERT INTO users (username) VALUES ('x')")
        self.assertEqual(r.kind, 'affected')
        self.assertEqual(r.affected, 1)
        self.assertFalse(self.db.connection().in_transaction)
        self.assertEqual(self.user_count(), 4)

    def test_ddl(self):
        r = run_one(self.dataset, 'CREATE TABLE t2 (id INTEGER)')
        self.assertEqual(r.kind, 'affected')
        self.assertEqual(r.affected, -1)
        self.assertEqual(run_one(self.dataset, 'SELECT * FROM t2').kind,
                         'rows')

    def test_pragma(self):
        r = run_one(self.dataset, 'PRAGMA journal_mode')
        self.assertEqual(r.kind, 'rows')
        self.assertEqual(len(r.rows), 1)

    def test_returning(self):
        r = run_one(self.dataset,
                    "INSERT INTO users (username) VALUES ('r') RETURNING *")
        self.assertEqual(r.kind, 'rows')
        self.assertEqual(len(r.rows), 1)
        self.assertEqual(self.user_count(), 4)

    def test_error(self):
        r = run_one(self.dataset, 'SELECT nocolumn FROM users')
        self.assertEqual(r.kind, 'error')
        self.assertIn('nocolumn', r.error)

    def test_multi_statement_is_error(self):
        r = run_one(self.dataset, 'SELECT 1; SELECT 2')
        self.assertEqual(r.kind, 'error')

    def test_trailing_junk(self):
        for sql in ('SELECT * FROM users -- a comment',
                    'SELECT * FROM users;',
                    'SELECT * FROM users; \n ;'):
            r = run_one(self.dataset, sql)
            self.assertEqual(r.kind, 'rows', sql)
            self.assertEqual(len(r.rows), 3, sql)


class TestSplitStatements(unittest.TestCase):
    def test_split(self):
        script = ('CREATE TABLE t1 (id INTEGER);\n'
                  'CREATE TRIGGER trg AFTER INSERT ON t1 BEGIN '
                  'UPDATE t1 SET id = id; END;\n'
                  'INSERT INTO t1 VALUES (1);')
        stmts = split_statements(script)
        self.assertEqual(len(stmts), 3)
        self.assertTrue(stmts[1].startswith('CREATE TRIGGER'))
        self.assertTrue(stmts[1].endswith('END;'))

    def test_semicolon_in_string(self):
        self.assertEqual(split_statements("SELECT ';'; SELECT 2;"),
                         ["SELECT ';';", 'SELECT 2;'])

    def test_trailing_comment_chunk(self):
        self.assertEqual(split_statements('SELECT 1; -- done'),
                         ['SELECT 1;', '-- done'])


class TestRunScript(BaseExecutorTestCase):
    def run_sql(self, script, **kwargs):
        return run_script(self.dataset, split_statements(script), **kwargs)

    def test_statements_apply_independently(self):
        results = self.run_sql(
            "INSERT INTO users (username) VALUES ('a');"
            "INSERT INTO users (username) VALUES ('b');")
        self.assertEqual([r.kind for r in results], ['affected', 'affected'])
        self.assertFalse(self.db.connection().in_transaction)
        self.assertEqual(self.user_count(), 5)

    def test_stops_at_first_error(self):
        results = self.run_sql(
            "INSERT INTO users (username) VALUES ('a');"
            'CREATE TABLE t2 (id INTEGER);'
            'SELECT nocolumn FROM users;'
            "INSERT INTO users (username) VALUES ('never');")
        self.assertEqual([r.kind for r in results],
                         ['affected', 'affected', 'error'])
        # Statements before the error stay applied, DDL included.
        self.assertEqual(self.user_count(), 4)
        self.assertEqual(run_one(self.dataset, 'SELECT * FROM t2').kind,
                         'rows')

    def test_select_inside_script(self):
        results = self.run_sql(
            'SELECT * FROM users; SELECT COUNT(*) FROM users;', page_size=2)
        self.assertEqual(len(results[0].rows), 2)
        self.assertTrue(results[0].has_next)
        self.assertEqual(results[1].rows[0][0], 3)

    def test_user_owned_transaction(self):
        results = self.run_sql(
            "BEGIN; INSERT INTO users (username) VALUES ('a'); COMMIT;")
        self.assertEqual([r.kind for r in results], ['affected'] * 3)
        self.assertFalse(self.db.connection().in_transaction)
        self.assertEqual(self.user_count(), 4)

        self.run_sql(
            "BEGIN; INSERT INTO users (username) VALUES ('b'); ROLLBACK;")
        self.assertEqual(self.user_count(), 4)

    def test_dangling_begin_rolls_back_on_close(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SqliteDatabase(os.path.join(tmpdir, 'x.db'))
            ds = DataSet(db)
            ds.query('CREATE TABLE t1 (id INTEGER)')
            results = run_script(ds, split_statements(
                'BEGIN; INSERT INTO t1 VALUES (1); SELECT nocolumn FROM t1;'))
            self.assertEqual(results[-1].kind, 'error')
            self.assertTrue(db.connection().in_transaction)
            # Request teardown closes the connection; sqlite rolls back.
            db.close()
            db.connect()
            self.assertEqual(
                ds.query('SELECT COUNT(*) FROM t1').fetchone()[0], 0)
            db.close()


class TestIsRead(BaseExecutorTestCase):
    def test_is_read(self):
        self.assertTrue(is_read(self.dataset, 'SELECT * FROM users'))
        self.assertTrue(is_read(self.dataset, 'SELECT * FROM users -- x'))
        self.assertFalse(is_read(self.dataset, 'DROP TABLE users'))
        self.assertFalse(is_read(self.dataset,
                                 "UPDATE users SET username = 'x'"))
        self.assertFalse(is_read(self.dataset, 'SELECT 1; SELECT 2'))
        self.assertEqual(self.user_count(), 3)


class TestWrap(unittest.TestCase):
    def test_shapes(self):
        self.assertEqual(wrap('SELECT 1;'),
                         'SELECT * FROM (\nSELECT 1\n) AS _')
        self.assertEqual(wrap('SELECT 1', ordering=-2),
                         'SELECT * FROM (\nSELECT 1\n) AS _ ORDER BY 2 DESC')
        self.assertEqual(wrap('SELECT 1', ordering=2, limit=51, offset=50),
                         'SELECT * FROM (\nSELECT 1\n) AS _ '
                         'ORDER BY 2 ASC LIMIT 51 OFFSET 50')
        self.assertEqual(wrap('SELECT 1', limit=0),
                         'SELECT * FROM (\nSELECT 1\n) AS _ LIMIT 0 OFFSET 0')
        self.assertEqual(wrap('SELECT 1', select='COUNT(*)'),
                         'SELECT COUNT(*) FROM (\nSELECT 1\n) AS _')


class TestRowKey(unittest.TestCase):
    def test_round_trips(self):
        for values in ([42], ['abc'], [b'\x01\x02\xff'], ['US', 'A:::B'],
                       [None, 1.5], ['✓']):
            self.assertEqual(key_decode(key_encode(values)), values)

    def test_url_safe(self):
        token = key_encode([b'\xfb\xff' * 30])
        self.assertNotIn('+', token)
        self.assertNotIn('/', token)


class BaseAppTestCase(unittest.TestCase):
    SCHEMA = """
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT);
        INSERT INTO users (username) VALUES ('huey'), ('mickey'), ('zaizee');
        CREATE TABLE comp (a TEXT, b TEXT, label TEXT, PRIMARY KEY (a, b));
        INSERT INTO comp VALUES ('US', 'A:::B', 'composite-row');
        CREATE TABLE blobs (id BLOB PRIMARY KEY, note TEXT);
        CREATE TABLE parent (id INTEGER PRIMARY KEY, name TEXT);
        INSERT INTO parent (name) VALUES ('p-one');
        CREATE TABLE child (id INTEGER PRIMARY KEY,
            parent_id INTEGER REFERENCES parent, label TEXT);
        INSERT INTO child (parent_id, label) VALUES (1, 'c-one');
        CREATE TABLE nopk (a TEXT);
        INSERT INTO nopk VALUES ('no-pk-row');
        CREATE TABLE oddpk ("user id" INTEGER NOT NULL, grp TEXT NOT NULL,
            val TEXT, PRIMARY KEY ("user id", grp));
        INSERT INTO oddpk VALUES (7, 'a', 'odd-row'), (8, 'a', 'same-grp');
        CREATE TABLE tag (name TEXT PRIMARY KEY);
        INSERT INTO tag VALUES (''), ('red');
        CREATE TABLE post (id INTEGER PRIMARY KEY,
            tag TEXT REFERENCES tag(name), body TEXT);
        CREATE VIEW v_users AS SELECT * FROM users;
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, 'app.db')
        conn = sqlite3.connect(self.db_path)
        conn.executescript(self.SCHEMA)
        conn.execute('INSERT INTO blobs VALUES (?, ?)', (b'\x00\xff', 'blob'))
        conn.commit()
        conn.close()
        sw.datasets.clear()
        sw.initialize_app([self.db_path])
        sw.app.config['TESTING'] = True
        self.client = sw.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def dbrows(self, sql, *params):
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return rows


class TestExecutionPolicy(BaseAppTestCase):
    def test_cross_site_post_rejected(self):
        r = self.client.post('/query/', data={'sql': 'SELECT 1'},
                             headers={'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(r.status_code, 403)
        r = self.client.post('/query/', data={'sql': 'SELECT 1'},
                             headers={'Sec-Fetch-Site': 'same-origin'})
        self.assertEqual(r.status_code, 200)
        r = self.client.post('/query/', data={'sql': 'SELECT 1'})
        self.assertEqual(r.status_code, 200)

    def test_get_does_not_execute_writes(self):
        self.client.get('/query/', query_string={'sql': 'DELETE FROM users'})
        self.assertEqual(self.dbrows('SELECT COUNT(*) FROM users')[0][0], 3)

    def test_post_runs_write_and_ddl(self):
        r = self.client.post('/query/',
                             data={'sql': "INSERT INTO users (username) "
                                          "VALUES ('x')"})
        self.assertIn(b'Rows modified', r.data)
        self.assertEqual(self.dbrows('SELECT COUNT(*) FROM users')[0][0], 4)
        self.client.post('/query/', data={'sql': 'CREATE TABLE t2 (id INT)'})
        self.assertEqual(self.client.get('/t2/').status_code, 200)

    def test_export_refuses_non_select(self):
        r = self.client.post('/query/', data={'sql': 'DROP TABLE users',
                                              'export_csv': '1'})
        self.assertIn(b'Only a single query may be exported', r.data)
        self.assertTrue(self.dbrows('SELECT COUNT(*) FROM users'))


class TestValueFilter(unittest.TestCase):
    def setUp(self):
        self._truncate = sw.app.config['TRUNCATE_VALUES']

    def tearDown(self):
        sw.app.config['TRUNCATE_VALUES'] = self._truncate

    def test_link_requires_full_match(self):
        url = 'https://example.com/x'
        self.assertEqual(sw.value_filter(url),
                         '<a href="%s">%s</a>' % (url, url))
        self.assertNotIn('<a ', sw.value_filter(url + ' trailing text'))

    def test_mailto(self):
        self.assertIn('<a href="mailto:huey@example.com"',
                      sw.value_filter('mailto:huey@example.com'))

    def test_long_link_label_truncated(self):
        url = 'https://example.com/' + 'x' * 60
        out = sw.value_filter(url)
        self.assertIn('href="%s"' % url, out)
        self.assertIn('...', out)

    def test_multiline_value_wrapped(self):
        self.assertEqual(sw.value_filter('line one\nline two'),
                         '<span class="pre">line one\nline two</span>')
        self.assertEqual(sw.value_filter('plain'), 'plain')

    def test_blob_respects_truncate_flag(self):
        data = b'\xff' * 600  # Undecodable, 1200 hex chars.
        sw.app.config['TRUNCATE_VALUES'] = True
        self.assertNotIn('ff' * 600, sw.value_filter(data))
        sw.app.config['TRUNCATE_VALUES'] = False
        self.assertIn('ff' * 600, sw.value_filter(data))


class TestExplain(BaseAppTestCase):
    def test_explain_select(self):
        r = self.client.post('/query/', data={'sql': 'SELECT * FROM users',
                                              'explain': '1'})
        self.assertIn(b'SCAN', r.data)

    def test_explain_compiles_writes_without_running(self):
        self.client.post('/query/', data={
            'sql': "INSERT INTO users (username) VALUES ('x')",
            'explain': '1'})
        self.assertEqual(self.dbrows('SELECT COUNT(*) FROM users')[0][0], 3)

    def test_explain_suppresses_row_keys(self):
        # Plan rows have an "id" column, which must not become edit links.
        r = self.client.post('/users/query/', data={
            'sql': 'SELECT * FROM users', 'explain': '1'})
        self.assertNotIn(b'/users/update/', r.data)
        self.assertNotIn(b'/users/row/', r.data)
        self.assertNotIn(b'name="count"', r.data)


class TestPaginationGating(BaseAppTestCase):
    def test_read_paginates(self):
        r = self.client.post('/users/query/',
                             data={'sql': 'SELECT * FROM users'})
        self.assertIn(b'name="count"', r.data)
        self.assertIn(b'bulk-action', r.data)

    def test_returning_hides_pagination_and_bulk(self):
        # The count button and bulk form re-submit the sql. For a write
        # that would execute it again.
        r = self.client.post('/users/query/', data={
            'sql': "INSERT INTO users (username) VALUES ('r') RETURNING *"})
        self.assertNotIn(b'name="count"', r.data)
        self.assertNotIn(b'bulk-action', r.data)
        self.assertEqual(self.dbrows('SELECT COUNT(*) FROM users')[0][0], 4)

    def test_query_tab_bulk_delete(self):
        r = self.client.post('/users/query/', data={
            'sql': 'SELECT * FROM users', 'action': 'bulk-delete',
            'pk': key_encode([1])})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.dbrows('SELECT COUNT(*) FROM users')[0][0], 2)
        self.assertIn(b'bulk-action', r.data)  # Fresh results offer bulk.


class TestForeignKeyLinks(BaseAppTestCase):
    def test_content_links_fk_values(self):
        r = self.client.get('/child/content/')
        self.assertIn(b'/parent/query/', r.data)

    def test_table_query_links_fk_values(self):
        r = self.client.post('/child/query/',
                             data={'sql': 'SELECT * FROM child'})
        self.assertIn(b'/parent/query/', r.data)

    def test_structure_shows_fk_target(self):
        r = self.client.get('/child/')
        self.assertIn(b'<code>parent.id</code>', r.data)

    def test_fk_link_resolves(self):
        r = self.client.get('/parent/query/', query_string={
            'sql': 'SELECT * FROM "parent" WHERE "id" = 1'})
        self.assertIn(b'p-one', r.data)

    def test_no_fk_links_on_generic_query(self):
        r = self.client.post('/query/', data={'sql': 'SELECT * FROM child'})
        self.assertNotIn(b'/parent/query/', r.data)


class TestLastViewed(BaseAppTestCase):
    def test_single_capped_session_key(self):
        with self.client.session_transaction() as s:
            s['users.last_viewed'] = [5, None]  # Legacy per-table key.
        self.client.get('/users/content/')
        self.client.get('/child/content/')
        with self.client.session_transaction() as s:
            self.assertNotIn('users.last_viewed', s)
            self.assertEqual([e[0] for e in s['last_viewed']],
                             ['child', 'users'])

    def test_back_links_carry_saved_position(self):
        # Real destination urls rendered into the hrefs, no bounce route.
        with self.client.session_transaction() as s:
            s['last_viewed'] = [['users', 3, -2]]
        for url in ('/users/row/%s/' % key_encode([1]),
                    '/users/update/%s/' % key_encode([1])):
            r = self.client.get(url)
            self.assertIn(b'page=3', r.data, url)
            self.assertIn(b'ordering=-2', r.data, url)
        # No saved entry falls back to plain content.
        r = self.client.get('/child/row/%s/' % key_encode([1]))
        self.assertIn(b'href="/child/content/"', r.data)

    def test_redirect_to_previous_uses_saved_position(self):
        with self.client.session_transaction() as s:
            s['last_viewed'] = [['users', 3, -2]]
        r = self.client.post('/users/delete/%s/' % key_encode([1]))
        self.assertIn(r.status_code, (302, 303))
        self.assertIn('page=3', r.headers['Location'])
        self.assertIn('ordering=-2', r.headers['Location'])


class TestRowKeyRoutes(BaseAppTestCase):
    def test_composite_key_with_delimiter(self):
        token = key_encode(['US', 'A:::B'])
        r = self.client.get('/comp/update/%s/' % token)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'composite-row', r.data)

    def test_blob_update_form_renders_hex(self):
        r = self.client.get('/blobs/update/%s/' % key_encode([b'\x00\xff']))
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'00ff', r.data)

    def test_blob_bulk_delete(self):
        token = key_encode([b'\x00\xff'])
        r = self.client.post('/blobs/content/',
                             data={'action': 'bulk-delete', 'pk': token},
                             headers={'Sec-Fetch-Site': 'same-origin'})
        self.assertIn(r.status_code, (302, 303))
        self.assertEqual(self.dbrows('SELECT COUNT(*) FROM blobs')[0][0], 0)

    def test_text_pk_valued_like_old_sentinel(self):
        self.client.post('/query/', data={'sql': "INSERT INTO users (id, "
                                          "username) VALUES (42, '__uneditable__')"})
        # A row whose value collides with the retired sentinel is still edited
        # by its own pk, not the username.
        token = key_encode([42])
        r = self.client.get('/users/update/%s/' % token)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'__uneditable__', r.data)

    def test_malformed_key_404s(self):
        self.assertEqual(self.client.get('/users/update/@@bad@@/').status_code,
                         404)

    def test_short_composite_token_cannot_multi_delete(self):
        # A one-value token against the two-column pk must be refused, a
        # zip would otherwise under-constrain the WHERE.
        token = key_encode(['US'])
        r = self.client.post('/comp/delete/%s/' % token)
        self.assertIn(r.status_code, (302, 303))
        self.assertEqual(self.dbrows('SELECT COUNT(*) FROM comp')[0][0], 1)
        r = self.client.get('/comp/row/%s/' % token)
        self.assertIn(r.status_code, (302, 303))

    def test_sanitized_pk_column_stays_in_key(self):
        # "user id" reflects as user_id and still keys the row. A grp-only
        # pk would target every row sharing grp.
        token = key_encode([7, 'a'])
        r = self.client.get('/oddpk/content/')
        self.assertIn(('/oddpk/row/%s/' % token).encode(), r.data)
        r = self.client.post('/oddpk/delete/%s/' % token)
        self.assertIn(r.status_code, (302, 303))
        self.assertEqual(self.dbrows('SELECT val FROM oddpk'), [('same-grp',)])


class TestDownload(BaseAppTestCase):
    def test_download_is_a_valid_snapshot(self):
        r = self.client.get('/download/')
        self.assertEqual(r.status_code, 200)
        self.assertIn('attachment', r.headers['Content-Disposition'])
        self.assertIn('app.db', r.headers['Content-Disposition'])
        self.assertTrue(r.data.startswith(b'SQLite format 3\x00'))

        path = os.path.join(self.tmp, 'snapshot.db')
        with open(path, 'wb') as f:
            f.write(r.data)
        r.close()  # Fires call_on_close, which removes the temp snapshot.
        conn = sqlite3.connect(path)
        count, = conn.execute('SELECT COUNT(*) FROM users').fetchone()
        conn.close()
        self.assertEqual(count, 3)

    def test_temp_dir_removed_after_streaming(self):
        marker = os.path.join(self.tmp, 'dl-tmp')
        os.mkdir(marker)
        orig = sw.tempfile.mkdtemp
        sw.tempfile.mkdtemp = lambda: marker
        try:
            r = self.client.get('/download/')
            self.assertTrue(os.path.exists(marker))  # Held while streaming.
            self.assertTrue(r.data.startswith(b'SQLite format 3\x00'))
            self.assertFalse(os.path.exists(marker))  # Gone once consumed.
            r.close()
        finally:
            sw.tempfile.mkdtemp = orig

    def test_head_request_does_not_leak(self):
        # HEAD never starts the body generator, so cleanup must not depend
        # on the generator running.
        marker = os.path.join(self.tmp, 'dl-head')
        os.mkdir(marker)
        orig = sw.tempfile.mkdtemp
        sw.tempfile.mkdtemp = lambda: marker
        try:
            r = self.client.head('/download/')
            self.assertEqual(r.status_code, 200)
            self.assertIn('attachment', r.headers['Content-Disposition'])
            r.close()
            self.assertFalse(os.path.exists(marker))
        finally:
            sw.tempfile.mkdtemp = orig

    def test_temp_dir_removed_on_abandoned_download(self):
        # A client that disconnects mid-stream must not leak the snapshot.
        marker = os.path.join(self.tmp, 'dl-abandon')
        os.mkdir(marker)
        orig = sw.tempfile.mkdtemp
        sw.tempfile.mkdtemp = lambda: marker
        try:
            r = self.client.get('/download/')
            self.assertTrue(os.path.exists(marker))
            r.close()  # Closed without ever reading the body.
            self.assertFalse(os.path.exists(marker))
        finally:
            sw.tempfile.mkdtemp = orig

    def test_error_flashes_and_cleans_up(self):
        marker = os.path.join(self.tmp, 'dl-err')
        os.mkdir(marker)
        os.chmod(marker, 0o500)  # VACUUM INTO cannot create its file.
        orig = sw.tempfile.mkdtemp
        sw.tempfile.mkdtemp = lambda: marker
        try:
            r = self.client.get('/download/', follow_redirects=True)
            self.assertEqual(r.status_code, 200)
            self.assertIn(b'Error creating database snapshot', r.data)
            self.assertFalse(os.path.exists(marker))
        finally:
            sw.tempfile.mkdtemp = orig
            if os.path.exists(marker):
                os.chmod(marker, 0o700)


class TestMultiDb(BaseAppTestCase):
    def setUp(self):
        super().setUp()
        self.db2 = os.path.join(self.tmp, 'two.db')
        conn = sqlite3.connect(self.db2)
        conn.execute('CREATE TABLE t2 (id INTEGER PRIMARY KEY)')
        conn.commit()
        conn.close()
        sw.datasets.clear()
        sw.initialize_app([self.db_path, self.db2])
        self.client = sw.app.test_client()

    def test_download_follows_selected_dataset(self):
        self.client.get('/select-dataset/',
                        query_string={'name': os.path.realpath(self.db2)})
        r = self.client.get('/download/')
        self.assertIn('two.db', r.headers['Content-Disposition'])
        path = os.path.join(self.tmp, 'snap2.db')
        with open(path, 'wb') as f:
            f.write(r.data)
        r.close()
        conn = sqlite3.connect(path)
        tables = [t for t, in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        conn.close()
        self.assertEqual(tables, ['t2'])

        self.client.get('/select-dataset/',
                        query_string={'name': os.path.realpath(self.db_path)})
        r = self.client.get('/download/')
        self.assertIn('app.db', r.headers['Content-Disposition'])
        r.close()


class TestDuplicateBasenames(BaseAppTestCase):
    def setUp(self):
        super().setUp()
        self.db2 = os.path.join(self.tmp, 'alt', 'app.db')
        os.makedirs(os.path.dirname(self.db2))
        conn = sqlite3.connect(self.db2)
        conn.execute('CREATE TABLE t2 (id INTEGER PRIMARY KEY)')
        conn.commit()
        conn.close()
        sw.datasets.clear()
        sw.initialize_app([self.db_path, self.db2])
        sw.app.config['ENABLE_FILESYSTEM'] = True
        self.client = sw.app.test_client()

    def tearDown(self):
        super().tearDown()
        sw.app.config['ENABLE_FILESYSTEM'] = False

    def select(self, path):
        return self.client.get('/select-dataset/',
                               query_string={'name': os.path.realpath(path)})

    def test_both_databases_are_loaded(self):
        self.assertEqual(sorted(sw.datasets), sorted([
            os.path.realpath(self.db_path), os.path.realpath(self.db2)]))

    def test_each_database_is_selectable(self):
        self.select(self.db2)
        self.assertIn(b'href="/t2/"', self.client.get('/').data)

        self.select(self.db_path)
        self.assertIn(b'href="/users/"', self.client.get('/').data)

    def test_menu_shows_the_parent_directory(self):
        # Long paths stay in the link target, out of the menu text.
        r = self.client.get('/')
        self.assertIn(b'>alt/app.db<', r.data)
        self.assertIn(b'>%s/app.db<'
                      % os.path.basename(os.path.realpath(self.tmp)).encode(),
                      r.data)
        self.assertNotIn(b'>%s<' % os.path.realpath(self.db2).encode(), r.data)

    def test_download_uses_basename(self):
        self.select(self.db2)
        r = self.client.get('/download/')
        self.assertIn('app.db', r.headers['Content-Disposition'])
        r.close()

    def test_runtime_load_leaves_loaded_databases_alone(self):
        before = list(sw.datasets)
        third = os.path.join(self.tmp, 'other', 'app.db')
        os.makedirs(os.path.dirname(third))
        conn = sqlite3.connect(third)
        conn.execute('CREATE TABLE t3 (id INTEGER PRIMARY KEY)')
        conn.commit()
        conn.close()
        r = self.client.post('/load/', data={'mode': 'filesystem',
                                             'filename': third})
        self.assertIn(r.status_code, (302, 303))
        self.assertEqual(list(sw.datasets),
                         before + [os.path.realpath(third)])
        # The newly-loaded database becomes the selected one.
        self.assertIn(b'href="/t3/"', self.client.get('/').data)

    def test_unload_removes_only_the_named_database(self):
        r = self.client.post('/unload/',
                             data={'dataset': os.path.realpath(self.db2)})
        self.assertIn(r.status_code, (302, 303))
        self.assertEqual(list(sw.datasets), [os.path.realpath(self.db_path)])


class TestRowDetailEdges(BaseAppTestCase):
    def test_blob_pk_detail(self):
        r = self.client.get('/blobs/row/%s/' % key_encode([b'\x00\xff']))
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'blob', r.data)

    def test_extra_key_values_ignored(self):
        # Same behavior as update/delete, the first value drives the lookup.
        r = self.client.get('/users/row/%s/' % key_encode([1, 2]))
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'huey', r.data)

    def test_empty_key_404s(self):
        # A token holding no values, [] or a crafted {}, must 404 instead
        # of raising IndexError in decode_pk.
        for token in (key_encode([]), 'e30='):
            for route in ('row', 'update', 'delete'):
                url = '/users/%s/%s/' % (route, token)
                self.assertEqual(self.client.get(url).status_code, 404, url)

    def test_no_pk_table(self):
        r = self.client.get('/nopk/content/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'no-pk-row', r.data)
        self.assertNotIn(b'/nopk/row/', r.data)
        r = self.client.get('/nopk/row/%s/' % key_encode(['no-pk-row']))
        self.assertIn(r.status_code, (302, 303))

    def test_sql_view(self):
        r = self.client.get('/v_users/content/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'huey', r.data)
        self.assertNotIn(b'/v_users/row/', r.data)
        for route in ('row', 'update', 'delete'):
            r = self.client.get('/v_users/%s/%s/' % (route, key_encode([1])))
            self.assertIn(r.status_code, (302, 303), route)


class TestRowDetail(BaseAppTestCase):
    def test_detail_page(self):
        r = self.client.get('/users/row/%s/' % key_encode([1]))
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'huey', r.data)
        self.assertIn(b'/users/update/', r.data)

    def test_missing_row_redirects(self):
        r = self.client.get('/users/row/%s/' % key_encode([999]))
        self.assertIn(r.status_code, (302, 303))

    def test_malformed_key_404s(self):
        self.assertEqual(self.client.get('/users/row/@@bad@@/').status_code,
                         404)

    def test_composite_pk_detail(self):
        r = self.client.get('/comp/row/%s/' % key_encode(['US', 'A:::B']))
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'composite-row', r.data)

    def test_detail_links_fk_values(self):
        r = self.client.get('/child/row/%s/' % key_encode([1]))
        self.assertIn(b'/parent/query/', r.data)

    def test_view_links_on_content_and_query_tabs(self):
        self.assertIn(b'/users/row/', self.client.get('/users/content/').data)
        r = self.client.post('/users/query/',
                             data={'sql': 'SELECT * FROM users'})
        self.assertIn(b'/users/row/', r.data)

    def test_no_view_links_on_generic_query(self):
        r = self.client.post('/query/', data={'sql': 'SELECT * FROM users'})
        self.assertNotIn(b'/users/row/', r.data)


class TestReadOnlyRowDetail(BaseAppTestCase):
    def setUp(self):
        super().setUp()
        sw.datasets.clear()
        sw.initialize_app([self.db_path], read_only=True)
        self.client = sw.app.test_client()

    def tearDown(self):
        super().tearDown()
        sw.dataset_config['read_only'] = False

    def test_read_only_gets_view_but_not_edit(self):
        r = self.client.get('/users/content/')
        self.assertIn(b'/users/row/', r.data)
        self.assertNotIn(b'/users/update/', r.data)
        self.assertNotIn(b'toggle-pk-all', r.data)

    def test_read_only_detail_page(self):
        r = self.client.get('/users/row/%s/' % key_encode([2]))
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'mickey', r.data)
        self.assertNotIn(b'/users/update/', r.data)

    def test_read_only_download(self):
        # VACUUM INTO runs against the mode=ro connection.
        r = self.client.get('/download/')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data.startswith(b'SQLite format 3\x00'))
        r.close()

    def test_read_only_smoke(self):
        for url in ('/', '/users/', '/users/content/', '/users/query/'):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        r = self.client.get('/users/content/')
        self.assertNotIn(b'/users/insert/', r.data)
        r = self.client.post('/users/query/',
                             data={'sql': 'SELECT * FROM users'})
        self.assertIn(b'huey', r.data)

    def test_read_only_writes_fail_at_the_database(self):
        # Enforcement is the mode=ro connection. Writes must fail safely
        # with data unchanged and no 500s.
        r = self.client.post('/users/insert/', data={
            'chk_username': 'on', 'username': 'nope'},
            follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.dbrows('SELECT COUNT(*) FROM users')[0][0], 3)

        r = self.client.post('/users/drop/', follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.dbrows('SELECT COUNT(*) FROM users'))

        r = self.client.post('/users/update/%s/' % key_encode([1]),
                             data={'chk_username': 'on', 'username': 'x'},
                             follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        rows = self.dbrows('SELECT username FROM users WHERE id = 1')
        self.assertEqual(rows, [('huey',)])

        r = self.client.post('/create-table/', data={
            'table_name': 'zz', 'redirect': '/'}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Error', r.data)


class TestPasswordlessLogin(BaseAppTestCase):
    def test_login_redirects_when_no_password(self):
        r = self.client.get('/login/')
        self.assertIn(r.status_code, (302, 303))
        r = self.client.post('/login/', data={})
        self.assertIn(r.status_code, (302, 303))
        with self.client.session_transaction() as s:
            self.assertNotIn('authorized', s)


class TestCreateTable(BaseAppTestCase):
    def test_create_failure_keeps_flash_destination(self):
        # The sqlite_ prefix is reserved, so creation fails. The redirect
        # must go back to the caller, not to a 404ing import page.
        r = self.client.post('/create-table/', data={
            'table_name': 'sqlite_nope', 'redirect': '/'},
            follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Error', r.data)

    def test_create_success_lands_on_import(self):
        r = self.client.post('/create-table/', data={
            'table_name': 'fresh', 'redirect': '/'})
        self.assertIn('/fresh/import/', r.headers['Location'])


class TestErrorPages(BaseAppTestCase):
    def test_404_renders_in_chrome(self):
        r = self.client.get('/nope-not-a-table/')
        self.assertEqual(r.status_code, 404)
        self.assertIn(b'Not Found', r.data)
        self.assertIn(b'powered by', r.data)

    def test_403_renders_in_chrome(self):
        r = self.client.post('/query/', data={'sql': 'SELECT 1'},
                             headers={'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(r.status_code, 403)
        self.assertIn(b'powered by', r.data)

    def test_empty_registry_500_renders(self):
        # The error page renders even when no dataset can be resolved.
        sw.datasets.clear()
        r = self.client.get('/')
        self.assertEqual(r.status_code, 500)
        self.assertIn(b'powered by', r.data)


class TestQueryTemplates(BaseAppTestCase):
    def test_shared_form_renders_on_both_pages(self):
        for url, textarea_id in (('/query/', b'id="sql"'),
                                 ('/users/query/', b'id="table-sql"')):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200)
            self.assertIn(b'name="explain"', r.data)
            self.assertIn(b'id="bookmark-modal"', r.data)
            self.assertIn(b'id="import-bookmarks"', r.data)
            self.assertIn(b'__TABLE__', r.data)
            self.assertIn(b'id="sql-image-modal"', r.data)
            self.assertIn(textarea_id, r.data)

    def test_copy_affordances_present(self):
        r = self.client.get('/users/content/')
        self.assertIn(b'copy-row', r.data)
        self.assertIn(b'data-col="username"', r.data)

    def test_script_results_offer_copy(self):
        r = self.client.post('/users/query/',
                             data={'sql': 'SELECT 1; SELECT 2;'})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'copy-row', r.data)
        self.assertNotIn(b'/users/row/', r.data)

    def test_missing_table_query_keeps_sql(self):
        # A bookmark can reference a dropped or foreign table. Its sql
        # falls back to the generic query page.
        r = self.client.get('/nope/query/', query_string={'sql': 'SELECT 1'})
        self.assertEqual(r.status_code, 302)
        self.assertIn('/query/?sql=SELECT', r.headers['Location'])
        r = self.client.get('/nope/query/', query_string={'sql': 'SELECT 1'},
                            follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'SELECT 1', r.data)

    def test_missing_table_query_404s_without_sql(self):
        self.assertEqual(self.client.get('/nope/query/').status_code, 404)


class TestInsertForm(BaseAppTestCase):
    def test_blank_numeric_inserts_null(self):
        # The form pre-enables every column, so a blank numeric input must
        # mean NULL instead of failing validation.
        r = self.client.post('/child/insert/', data={
            'chk_parent_id': 'on', 'parent_id': '',
            'chk_label': 'on', 'label': 'blank-num'})
        self.assertIn(r.status_code, (302, 303))
        rows = self.dbrows(
            'SELECT parent_id, label FROM child WHERE label = ?', 'blank-num')
        self.assertEqual(rows, [(None, 'blank-num')])

    def test_blank_text_inserts_empty_string(self):
        r = self.client.post('/child/insert/', data={
            'chk_label': 'on', 'label': ''})
        self.assertIn(r.status_code, (302, 303))
        rows = self.dbrows("SELECT COUNT(*) FROM child WHERE label = ''")
        self.assertEqual(rows, [(1,)])

    def test_blank_text_fk_keeps_empty_string(self):
        # A TEXT primary key can legitimately be '', so a blank input on
        # a text-keyed fk must not become NULL.
        r = self.client.post('/post/insert/', data={
            'chk_tag': 'on', 'tag': '',
            'chk_body': 'on', 'body': 'text-fk'})
        self.assertIn(r.status_code, (302, 303))
        rows = self.dbrows("SELECT tag FROM post WHERE body = 'text-fk'")
        self.assertEqual(rows, [('',)])


class TestContentTab(BaseAppTestCase):
    def test_renders_with_row_actions(self):
        r = self.client.get('/users/content/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'/users/update/', r.data)
        self.assertIn(b'/users/delete/', r.data)
        self.assertIn(b'toggle-pk-all', r.data)

    def test_ordinal_ordering(self):
        asc = self.client.get('/users/content/',
                              query_string={'ordering': '2'}).data
        desc = self.client.get('/users/content/',
                               query_string={'ordering': '-2'}).data
        self.assertLess(asc.index(b'huey'), asc.index(b'zaizee'))
        self.assertLess(desc.index(b'zaizee'), desc.index(b'huey'))

    def test_bad_ordering_ignored(self):
        for value in ('99', 'abc', '-abc'):
            r = self.client.get('/users/content/',
                                query_string={'ordering': value})
            self.assertEqual(r.status_code, 200)

    def test_disabled_pager_arrows_are_not_links(self):
        # Single page of data, so all four arrows must render as spans.
        r = self.client.get('/users/content/')
        for arrow in (b'&laquo;', b'&lsaquo;', b'&rsaquo;', b'&raquo;'):
            self.assertIn(b'<span class="page-link">' + arrow, r.data)

    def test_flash_alerts_are_dismissible(self):
        r = self.client.get('/users/row/%s/' % key_encode([999]),
                            follow_redirects=True)
        self.assertIn(b'alert-dismissible', r.data)
        self.assertNotIn(b'alert-dismissable', r.data)


class TestUrlPrefix(BaseAppTestCase):
    def setUp(self):
        super(TestUrlPrefix, self).setUp()
        self.wsgi_app = sw.app.wsgi_app
        sw.initialize_app([], url_prefix='/sqlite/')

    def tearDown(self):
        sw.app.wsgi_app = self.wsgi_app
        sw.app.config['SESSION_COOKIE_PATH'] = None
        super(TestUrlPrefix, self).tearDown()

    def test_session_cookie_scoped_to_prefix(self):
        r = self.client.get('/sqlite/users/content/')
        self.assertIn('Path=/sqlite', r.headers['Set-Cookie'])


#
# Diff engine.
#

class DiffEngineTestCase(unittest.TestCase):
    def setUp(self):
        self.left = DataSet(SqliteDatabase(':memory:'))
        self.right = DataSet(SqliteDatabase(':memory:'))
        self.left.query(
            'CREATE TABLE orders (order_no TEXT PRIMARY KEY, '
            'amount REAL, status TEXT)')
        self.right.query(
            'CREATE TABLE orders (order_no TEXT PRIMARY KEY, '
            'amount REAL, status TEXT)')

    def load(self, ds, rows):
        ds.query('DELETE FROM orders')
        for row in rows:
            ds.query('INSERT INTO orders VALUES (?, ?, ?)', row)

    def diff(self, left_rows, right_rows, keys=('order_no',)):
        self.load(self.left, left_rows)
        self.load(self.right, right_rows)
        sa = diff_mod.fetch_side(self.left, 'l', 'table', list(keys),
                                 table='orders')
        sb = diff_mod.fetch_side(self.right, 'r', 'table', list(keys),
                                 table='orders')
        return diff_mod.compute_diff(sa, sb, list(keys))

    def test_added_deleted_changed(self):
        d = self.diff(
            [('O1', 100.0, None), ('O2', 50.0, 'pending')],
            [('O2', 50.0, 'settled'), ('O3', 75.0, None)])
        self.assertEqual([e.key for e in d.added],
                         [(('text', 'O3'),)])
        self.assertEqual([e.key for e in d.deleted],
                         [(('text', 'O1'),)])
        self.assertEqual([e.key for e in d.changed],
                         [(('text', 'O2'),)])
        self.assertEqual(d.changed[0].changed_cols, ['status'])
        self.assertEqual(d.total, 3)

    def test_null_vs_value_is_change(self):
        d = self.diff([('O1', 1.0, None)], [('O1', 1.0, 'settled')])
        self.assertEqual(len(d.changed), 1)
        self.assertEqual(d.changed[0].changed_cols, ['status'])

    def test_type_difference_is_change(self):
        # Integer 1 versus text "1", integer 2 versus real 2.0, and NULL
        # versus a real value.
        self.left.query('CREATE TABLE t (id INTEGER PRIMARY KEY, v)')
        self.right.query('CREATE TABLE t (id INTEGER PRIMARY KEY, v)')
        self.left.query('INSERT INTO t VALUES (?, ?)', (1, 1))
        self.right.query('INSERT INTO t VALUES (?, ?)', (1, '1'))
        self.left.query('INSERT INTO t VALUES (?, ?)', (2, 2))
        self.right.query('INSERT INTO t VALUES (?, ?)', (2, 2.0))
        self.left.query('INSERT INTO t VALUES (?, ?)', (3, None))
        self.right.query('INSERT INTO t VALUES (?, ?)', (3, 3.0))
        sa = diff_mod.fetch_side(self.left, 'l', 'table', ['id'], table='t')
        sb = diff_mod.fetch_side(self.right, 'r', 'table', ['id'], table='t')
        d = diff_mod.compute_diff(sa, sb, ['id'])
        self.assertEqual(len(d.changed), 3)
        self.assertTrue(all(e.changed_cols == ['v'] for e in d.changed))

    def test_equal_values_with_same_type_unchanged(self):
        d = self.diff(
            [('O1', 1.5, 'ok'), ('O2', None, None)],
            [('O1', 1.5, 'ok'), ('O2', None, None)])
        self.assertEqual(d.total, 0)

    def test_blob_typed_and_compared(self):
        self.left.query('CREATE TABLE bl (id BLOB PRIMARY KEY, n INTEGER)')
        self.right.query('CREATE TABLE bl (id BLOB PRIMARY KEY, n INTEGER)')
        self.left.query('INSERT INTO bl VALUES (?, ?)', (b'\x00\xff', 1))
        self.right.query('INSERT INTO bl VALUES (?, ?)', (b'\x00\xff', 2))
        # A text-looking key must not match the blob key.
        sa = diff_mod.fetch_side(self.left, 'l', 'table', ['id'], table='bl')
        sb = diff_mod.fetch_side(self.right, 'r', 'table', ['id'], table='bl')
        d = diff_mod.compute_diff(sa, sb, ['id'])
        self.assertEqual(len(d.changed), 1)
        self.assertEqual(d.changed[0].key, (('blob', b'\x00\xff'),))

    def test_columns_only_on_one_side(self):
        self.left.query('CREATE TABLE asym (id INTEGER PRIMARY KEY, a TEXT)')
        self.right.query(
            'CREATE TABLE asym (id INTEGER PRIMARY KEY, a TEXT, b TEXT)')
        self.left.query("INSERT INTO asym VALUES (1, 'x')")
        self.right.query("INSERT INTO asym VALUES (1, 'x', 'y')")
        sa = diff_mod.fetch_side(self.left, 'l', 'table', ['id'],
                                 table='asym')
        sb = diff_mod.fetch_side(self.right, 'r', 'table', ['id'],
                                 table='asym')
        d = diff_mod.compute_diff(sa, sb, ['id'])
        self.assertEqual(d.only_a, [])
        self.assertEqual(d.only_b, ['b'])
        # An extra column with the same key/common values is not a change.
        self.assertEqual(d.changed, [])

    def test_duplicate_keys_reported(self):
        self.left.query('CREATE TABLE dup (k TEXT, v TEXT)')
        self.right.query('CREATE TABLE dup (k TEXT, v TEXT)')
        self.left.query("INSERT INTO dup VALUES ('a', '1'), ('a', '2')")
        self.right.query("INSERT INTO dup VALUES ('a', '1')")
        sa = diff_mod.fetch_side(self.left, 'l', 'table', ['k'], table='dup')
        sb = diff_mod.fetch_side(self.right, 'r', 'table', ['k'], table='dup')
        self.assertEqual(sa.duplicate_keys, 1)
        d = diff_mod.compute_diff(sa, sb, ['k'])
        self.assertTrue(any('duplicated key' in w for w in d.warnings))

    def test_composite_key(self):
        self.left.query(
            'CREATE TABLE c2 (a TEXT, b TEXT, v INTEGER, '
            'PRIMARY KEY (a, b))')
        self.right.query(
            'CREATE TABLE c2 (a TEXT, b TEXT, v INTEGER, '
            'PRIMARY KEY (a, b))')
        self.left.query("INSERT INTO c2 VALUES ('US', 'x', 1)")
        self.right.query("INSERT INTO c2 VALUES ('US', 'x', 2)")
        sa = diff_mod.fetch_side(self.left, 'l', 'table', ['a', 'b'],
                                 table='c2')
        sb = diff_mod.fetch_side(self.right, 'r', 'table', ['a', 'b'],
                                 table='c2')
        d2 = diff_mod.compute_diff(sa, sb, ['a', 'b'])
        self.assertEqual(d2.changed[0].key,
                         (('text', 'US'), ('text', 'x')))
        self.assertEqual(d2.changed[0].changed_cols, ['v'])

    def test_read_rejects_writes(self):
        with self.assertRaises(ValueError):
            diff_mod.read_columns(self.left, 'DROP TABLE orders')
        with self.assertRaises(ValueError):
            diff_mod.read_columns(self.left, 'SELECT 1; SELECT 2')

    def test_query_source(self):
        self.load(self.left, [('O1', 1.0, 'x'), ('O2', 2.0, 'y')])
        sa = diff_mod.fetch_side(
            self.left, 'l', 'query', ['order_no'],
            sql="SELECT * FROM orders WHERE order_no = 'O1'")
        self.assertEqual(list(sa.rows), [(('text', 'O1'),)])


class TestTypedKey(unittest.TestCase):
    def test_round_trip_preserves_type(self):
        cells = [('null', None), ('integer', 1), ('real', 1.5),
                 ('text', 'a'), ('blob', b'\x00\xff')]
        decoded = typed_key_decode(typed_key_encode(cells))
        self.assertEqual(decoded, cells)

    def test_url_safe(self):
        token = typed_key_encode([('blob', b'\xfb\xff' * 20)])
        self.assertNotIn('+', token)
        self.assertNotIn('/', token)


class TwoSnapshotTestCase(unittest.TestCase):
    """Two independent database files, the month-end scenario."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.before = os.path.join(self.tmp, 'orders_before.db')
        self.after = os.path.join(self.tmp, 'orders_after.db')
        self.create(self.before, [
            ('O1', 100.0, None),
            ('O2', 50.0, 'open'),
            ('O3', 9.99, 'settled')])
        self.create(self.after, [
            ('O2', 55.0, 'settled'),       # amount + status changed
            ('O3', 9.99, 'settled'),       # unchanged
            ('O4', 75.0, None)])           # added
        sw.datasets.clear()
        sw.initialize_app([self.before, self.after])
        sw.app.config['TESTING'] = True
        self.client = sw.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def create(self, path, rows, extra_ddl=''):
        conn = sqlite3.connect(path)
        conn.execute(
            'CREATE TABLE orders (order_no TEXT PRIMARY KEY, '
            'amount REAL, status TEXT)')
        if extra_ddl:
            conn.executescript(extra_ddl)
        conn.executemany('INSERT INTO orders VALUES (?, ?, ?)', rows)
        conn.commit()
        conn.close()

    def params(self, **overrides):
        p = {
            'a_dataset': os.path.realpath(self.before),
            'a_source': 'table', 'a_table': 'orders', 'a_sql': '',
            'b_dataset': os.path.realpath(self.after),
            'b_source': 'table', 'b_table': 'orders', 'b_sql': '',
            'key': 'order_no'}
        p.update(overrides)
        return p

    def test_compare_page_renders_summary(self):
        r = self.client.get('/compare/', query_string=self.params())
        self.assertEqual(r.status_code, 200)
        body = r.data.decode()
        self.assertIn('Differences: <strong>3</strong>', body)
        self.assertIn('+1 added', body)
        self.assertIn('&minus;1 deleted', body)
        self.assertIn('~1 changed', body)
        # The changed row shows old -> new for each changed column.
        self.assertIn('50.0', body)
        self.assertIn('55.0', body)
        self.assertIn('open', body)
        self.assertIn('settled', body)
        # NULL for the deleted row's status renders distinctly.
        self.assertIn('diff-null', body)

    def test_changed_cell_marks_field(self):
        body = self.client.get('/compare/',
                               query_string=self.params()).data.decode()
        # Exactly the amount and status cells of O2 are marked changed.
        self.assertIn('title="Changed: amount"', body)
        self.assertIn('title="Changed: status"', body)

    def test_columns_endpoint_for_table_and_query(self):
        r = self.client.get('/compare/columns/', query_string={
            'dataset': os.path.realpath(self.after),
            'source': 'table', 'table': 'orders'})
        self.assertEqual(r.get_json()['columns'],
                         ['order_no', 'amount', 'status'])
        r = self.client.get('/compare/columns/', query_string={
            'dataset': os.path.realpath(self.after),
            'source': 'query',
            'sql': 'SELECT order_no, amount FROM orders'})
        self.assertEqual(r.get_json()['columns'], ['order_no', 'amount'])

    def test_columns_endpoint_rejects_write(self):
        r = self.client.get('/compare/columns/', query_string={
            'dataset': os.path.realpath(self.after),
            'source': 'query', 'sql': 'DELETE FROM orders'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('read-only', r.get_json()['error'])

    def test_missing_key_reports_error_not_500(self):
        p = self.params()
        del p['key']
        r = self.client.get('/compare/', query_string=p)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'key columns', r.data)

    def test_key_column_missing_on_side(self):
        r = self.client.get('/compare/',
                            query_string=self.params(key='nope'))
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'not present on both sides', r.data)

    def test_asymmetric_columns_listed(self):
        conn = sqlite3.connect(self.after)
        conn.execute('ALTER TABLE orders ADD COLUMN cleared_at TEXT')
        conn.commit()
        conn.close()
        body = self.client.get('/compare/',
                               query_string=self.params()).data.decode()
        self.assertIn('Only on the right', body)
        self.assertIn('<code>cleared_at</code>', body)

    def test_fragment_only_returns_results(self):
        r = self.client.get('/compare/',
                            query_string=self.params(fragment='1'))
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b'<form id="compare-form"', r.data)
        self.assertIn(b'diff-summary', r.data)

    def test_goto_switches_database_and_keeps_sql(self):
        token = typed_key_encode([('text', 'O2')])
        r = self.client.get('/compare/goto/', query_string={
            'dataset': os.path.realpath(self.after),
            'source': 'query',
            'sql': "SELECT * FROM orders WHERE status = 'settled'",
            'focus_cols': 'order_no', 'focus_key': token})
        self.assertIn(r.status_code, (302, 303))
        loc = r.headers['Location']
        self.assertIn('/query/?sql=', loc)
        self.assertIn('focus_key=', loc)
        self.assertIn('focus_cols=order_no', loc)
        with self.client.session_transaction() as s:
            self.assertEqual(s['dataset'], os.path.realpath(self.after))
        page = self.client.get(loc)
        self.assertIn(b'focus-row', page.data)

    def test_goto_table_source_uses_table_query(self):
        token = typed_key_encode([('text', 'O2')])
        r = self.client.get('/compare/goto/', query_string={
            'dataset': os.path.realpath(self.after),
            'source': 'table', 'table': 'orders',
            'sql': 'SELECT * FROM "orders"',
            'focus_cols': 'order_no', 'focus_key': token})
        self.assertIn('/orders/query/', r.headers['Location'])

    def test_goto_rejects_bad_token(self):
        r = self.client.get('/compare/goto/', query_string={
            'dataset': os.path.realpath(self.after),
            'source': 'table', 'table': 'orders', 'sql': 'SELECT 1',
            'focus_cols': 'order_no', 'focus_key': '@@bad@@'})
        self.assertEqual(r.status_code, 404)

    def test_type_aware_goto_distinguishes_int_from_text(self):
        conn = sqlite3.connect(self.after)
        conn.execute('CREATE TABLE typ (id INTEGER PRIMARY KEY, v)')
        conn.executemany('INSERT INTO typ VALUES (?, ?)',
                         [(1, 'text-one'), (2, 2)])
        conn.commit()
        conn.close()
        conn = sqlite3.connect(self.before)
        conn.execute('CREATE TABLE typ (id INTEGER PRIMARY KEY, v)')
        conn.executemany('INSERT INTO typ VALUES (?, ?)',
                         [(1, 1), (2, 'two')])
        conn.commit()
        conn.close()
        # id=1: text "text-one" on after must not collide with anything.
        token_text = typed_key_encode([('integer', 1)])
        sw.app.config['QUERY_ROWS_PER_PAGE'] = 50
        r = self.client.get('/compare/goto/', query_string={
            'dataset': os.path.realpath(self.after),
            'source': 'table', 'table': 'typ',
            'sql': 'SELECT * FROM "typ"',
            'focus_cols': 'id', 'focus_key': token_text})
        page = self.client.get(r.headers['Location'])
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'text-one', page.data)

    def test_diff_is_read_only(self):
        before_stat = os.stat(self.before)
        after_stat = os.stat(self.after)
        self.client.get('/compare/', query_string=self.params())
        self.client.get('/compare/',
                        query_string=self.params(
                            a_source='query',
                            a_sql='SELECT * FROM orders',
                            b_source='query',
                            b_sql='SELECT * FROM orders'))
        self.assertEqual(os.stat(self.before).st_size, before_stat.st_size)
        self.assertEqual(os.stat(self.after).st_size, after_stat.st_size)
        conn = sqlite3.connect(self.before)
        count, = conn.execute('SELECT COUNT(*) FROM orders').fetchone()
        self.assertEqual(count, 3)
        conn.close()


class ComparePaginationTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.a = os.path.join(self.tmp, 'a.db')
        self.b = os.path.join(self.tmp, 'b.db')
        for path, n in ((self.a, 120), (self.b, 120)):
            conn = sqlite3.connect(path)
            conn.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)')
            if path == self.a:
                conn.executemany('INSERT INTO t VALUES (?, ?)',
                                 [(i, 'x') for i in range(120)])
            else:
                # ids 0..49 unchanged; 120..189 added (70); a-side ids
                # 50..119 are deleted (70).
                conn.executemany('INSERT INTO t VALUES (?, ?)',
                                 [(i, 'x') for i in range(50)] +
                                 [(i, 'y') for i in range(120, 190)])
            conn.commit()
            conn.close()
        sw.datasets.clear()
        sw.initialize_app([self.a, self.b])
        sw.app.config['TESTING'] = True
        self.client = sw.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def params(self, **ov):
        p = {'a_dataset': os.path.realpath(self.a), 'a_source': 'table',
             'a_table': 't', 'a_sql': '',
             'b_dataset': os.path.realpath(self.b), 'b_source': 'table',
             'b_table': 't', 'b_sql': '', 'key': 'id'}
        p.update(ov)
        return p

    def test_paginates_large_result(self):
        r = self.client.get('/compare/', query_string=self.params())
        self.assertIn(b'Page 1 of 3', r.data)  # 140 diffs / 50
        r = self.client.get('/compare/',
                            query_string=self.params(page='3'))
        self.assertIn(b'Page 3 of 3', r.data)

    def test_filter_paginates_independently(self):
        r = self.client.get('/compare/',
                            query_string=self.params(show='added'))
        self.assertIn(b'Page 1 of 2', r.data)
        r = self.client.get('/compare/',
                            query_string=self.params(show='added', page='2'))
        self.assertIn(b'51&ndash;70 of 70', r.data)
        r = self.client.get('/compare/',
                            query_string=self.params(show='changed'))
        self.assertNotIn(b'pagination', r.data)  # no changed rows, no pager

    def test_page_out_of_bounds_clamped(self):
        r = self.client.get('/compare/',
                            query_string=self.params(page='99'))
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Page 3 of 3', r.data)


class ReadOnlyCompareTestCase(TwoSnapshotTestCase):
    def setUp(self):
        super().setUp()
        sw.datasets.clear()
        sw.initialize_app([self.before, self.after], read_only=True)
        sw.app.config['TESTING'] = True
        self.client = sw.app.test_client()

    def tearDown(self):
        super().tearDown()
        sw.dataset_config['read_only'] = False

    def test_compare_works_read_only(self):
        r = self.client.get('/compare/', query_string=self.params())
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Differences:', r.data)


if __name__ == '__main__':
    unittest.main()
