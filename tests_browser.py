"""
Browser tests for the compare view, driven through a real browser as the
requirement asks. They start sqlite-web's own Flask app on a live port
against two temporary snapshot databases, then drive the UI like a user.

They skip themselves when Playwright or a browser is unavailable, so a
plain "python tests_browser.py" on a machine without a browser does not
fail the suite. Set LD_LIBRARY_PATH for a locally-extracted browser when
needed (see the project README notes in the environment).
"""

import multiprocessing
import os
import shutil
import sqlite3
import tempfile
import time
import unittest

from sqlite_web import sqlite_web as sw


def _build_snapshots(tmp):
    before = os.path.join(tmp, 'orders_before.db')
    after = os.path.join(tmp, 'orders_after.db')
    conn = sqlite3.connect(before)
    conn.executescript("""
        CREATE TABLE orders (order_no TEXT PRIMARY KEY,
                             amount REAL, status TEXT);
        INSERT INTO orders VALUES ('O1', 100.0, NULL);
        INSERT INTO orders VALUES ('O2', 50.0, 'open');
        INSERT INTO orders VALUES ('O3', 9.99, 'settled');
        CREATE TABLE big (id INTEGER PRIMARY KEY, v TEXT);
    """)
    conn.executemany('INSERT INTO big VALUES (?, ?)',
                     [(i, 'same') for i in range(120)])
    conn.commit()
    conn.close()

    conn = sqlite3.connect(after)
    conn.executescript("""
        CREATE TABLE orders (order_no TEXT PRIMARY KEY,
                             amount REAL, status TEXT, note TEXT);
        INSERT INTO orders VALUES ('O2', 55.0, 'settled', 'paid');
        INSERT INTO orders VALUES ('O3', 9.99, 'settled', NULL);
        INSERT INTO orders VALUES ('O4', 75.0, NULL, NULL);
        CREATE TABLE big (id INTEGER PRIMARY KEY, v TEXT);
    """)
    conn.executemany('INSERT INTO big VALUES (?, ?)',
                     [(i, 'same') for i in range(50)] +
                     [(i, 'new') for i in range(120, 190)])
    conn.commit()
    conn.close()
    return before, after


def _serve(before, after, port, ready):
    sw.datasets.clear()
    sw.initialize_app([before, after], read_only=True)
    sw.app.config['TESTING'] = False
    ready.set()
    sw.app.run(host='127.0.0.1', port=port, threaded=True,
               use_reloader=False)


class BrowserCompareTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest('playwright not installed')
        cls._pw = sync_playwright().start()
        try:
            cls._browser = cls._pw.chromium.launch()
        except Exception as exc:
            cls._pw.stop()
            raise unittest.SkipTest('headless chromium unavailable: %s' % exc)

        cls.tmp = tempfile.mkdtemp(prefix='sw-browser-')
        before, after = _build_snapshots(cls.tmp)
        cls.before, cls.after = os.path.realpath(before), os.path.realpath(after)
        cls.port = 8931
        ready = multiprocessing.Event()
        cls.proc = multiprocessing.Process(
            target=_serve, args=(cls.before, cls.after, cls.port, ready))
        cls.proc.start()
        ready.wait(5)
        time.sleep(1)
        cls.base = 'http://127.0.0.1:%d' % cls.port

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, 'proc'):
            cls.proc.terminate()
            cls.proc.join(5)
        if hasattr(cls, '_browser'):
            cls._browser.close()
        if hasattr(cls, '_pw'):
            cls._pw.stop()
        if hasattr(cls, 'tmp'):
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.page = self._browser.new_page()

    def tearDown(self):
        self.page.close()

    def select_side(self, side, db_path, table='orders'):
        p = self.page
        p.select_option('#%s-dataset' % side, db_path)
        p.wait_for_function(
            "document.getElementById('%s-table').options.length > 1" % side)
        p.select_option('#%s-table' % side, table)

    def check_key(self, name):
        box = '#compare-keys input[value="%s"]' % name.replace('"', '\\"')
        self.page.check(box)

    def submit(self):
        self.page.click('#compare-form button[type="submit"]')
        self.page.wait_for_selector('.diff-summary')

    def test_full_compare_workflow(self):
        p = self.page
        p.goto(self.base + '/compare/')
        self.assertIn('Compare two result sets', p.inner_text('h3'))

        # Configure both sides from separate database files.
        self.select_side('a', self.before)
        self.select_side('b', self.after)

        # Key checkboxes are derived from the common columns.
        p.wait_for_selector('#compare-keys input[value="order_no"]')
        self.check_key('order_no')
        self.submit()

        # Total on top, counts per kind.
        summary = p.inner_text('.diff-summary')
        self.assertIn('Differences: 3', summary)
        self.assertIn('1 added', summary)
        self.assertIn('1 deleted', summary)
        self.assertIn('1 changed', summary)

        # Column present on only one side is called out, not an error.
        warning = p.inner_text('.alert-warning')
        self.assertIn('Only on the right', warning)
        self.assertIn('note', warning)

        # The changed row marks the exact fields old -> new.
        changed_row = p.locator('tr.diff-changed')
        self.assertEqual(changed_row.count(), 1)
        cell_titles = changed_row.locator('.diff-changed-cell').evaluate_all(
            "els => els.map(e => e.getAttribute('title'))")
        self.assertEqual(sorted(cell_titles),
                         ['Changed: amount', 'Changed: status'])
        self.assertIn('50', changed_row.inner_text())
        self.assertIn('55', changed_row.inner_text())
        self.assertIn('open', changed_row.inner_text())
        self.assertIn('settled', changed_row.inner_text())
        # NULL for the deleted status renders as its own marker.
        self.assertTrue(p.locator('tr.diff-deleted .diff-null').count() >= 1)

    def test_filter_tabs(self):
        p = self.page
        p.goto(self.base + '/compare/')
        self.select_side('a', self.before)
        self.select_side('b', self.after)
        p.wait_for_selector('#compare-keys input[value="order_no"]')
        self.check_key('order_no')
        self.submit()

        p.click('a.nav-pill-added')
        p.wait_for_selector('.diff-summary')
        self.assertEqual(p.locator('tr.diff-added').count(), 1)
        self.assertEqual(p.locator('tr.diff-deleted').count(), 0)
        self.assertEqual(p.locator('tr.diff-changed').count(), 0)

        p.click('a.nav-pill-changed')
        p.wait_for_selector('.diff-summary')
        self.assertEqual(p.locator('tr.diff-changed').count(), 1)

        p.click('a.nav-pill-deleted')
        p.wait_for_selector('.diff-summary')
        self.assertEqual(p.locator('tr.diff-deleted').count(), 1)

    def test_pagination_on_large_table(self):
        p = self.page
        p.goto(self.base + '/compare/')
        self.select_side('a', self.before, 'big')
        self.select_side('b', self.after, 'big')
        p.wait_for_selector('#compare-keys input[value="id"]')
        self.check_key('id')
        self.submit()
        p.wait_for_selector('.pagination')
        # 140 diffs (70 deleted + 70 added), 50 per page -> 3 pages.
        self.assertIn('Page 1 of 3', p.inner_text('.pagination'))
        self.assertEqual(p.locator('tr.diff-row').count(), 50)
        p.click('.pagination a:has-text("»")')
        p.wait_for_selector('.diff-summary')
        self.assertIn('Page 3 of 3', p.inner_text('.pagination'))
        self.assertEqual(p.locator('tr.diff-row').count(), 40)

    def test_goto_jumps_to_row_and_keeps_query(self):
        p = self.page
        p.goto(self.base + '/compare/')
        self.select_side('a', self.before)
        self.select_side('b', self.after)
        p.wait_for_selector('#compare-keys input[value="order_no"]')
        self.check_key('order_no')
        self.submit()

        # "right" jump on the changed row switches to the after database
        # and lands on the row, with the sql still in the query box.
        with p.expect_navigation():
            p.click('tr.diff-changed a.diff-goto-b')
        p.wait_for_selector('#focus-row')
        self.assertIn('/orders/query/', p.url)
        self.assertIn('focus_key=', p.url)
        sql = p.input_value('#table-sql')
        self.assertTrue(sql.upper().startswith('SELECT * FROM'))
        # The highlighted row contains O2's new values.
        focus = p.inner_text('#focus-row')
        self.assertIn('O2', focus)
        self.assertIn('55', focus)
        # And the current database is the "after" snapshot.
        self.assertIn('orders_after.db',
                      p.inner_text('.primary-nav'))

    def test_query_sources_and_rapid_changes_show_last_result(self):
        p = self.page
        p.goto(self.base + '/compare/')
        p.select_option('#a-dataset', self.before)
        p.wait_for_function(
            "document.getElementById('a-table').options.length > 1")
        p.check('#a-source-query')
        p.fill('#a-sql', "SELECT * FROM orders WHERE order_no = 'O1'")

        p.select_option('#b-dataset', self.after)
        p.wait_for_function(
            "document.getElementById('b-table').options.length > 1")
        p.check('#b-source-query')
        p.fill('#b-sql', "SELECT * FROM orders WHERE order_no = 'O4'")

        # Key checkboxes discovered via the queries.
        p.wait_for_selector('#compare-keys input[value="order_no"]')
        self.check_key('order_no')

        # Fire two submits back to back; the last one must win even if the
        # first response is slower. The fragment guard + abort enforces it.
        p.click('#compare-form button[type="submit"]')
        p.wait_for_timeout(30)
        # Change the query and submit again immediately.
        p.fill('#b-sql', "SELECT * FROM orders WHERE order_no IN ('O2')")
        p.click('#compare-form button[type="submit"]')
        p.wait_for_selector('.diff-summary')
        p.wait_for_timeout(300)  # Let any late first response arrive.
        summary = p.inner_text('.diff-summary')
        # O1 left, O2 right => 1 deleted + 1 changed... the second query's
        # shape: left O1 only, right O2 only => 1 added + 1 deleted.
        self.assertIn('1 added', summary)
        self.assertIn('1 deleted', summary)
        self.assertIn('0 changed', summary)
        # O4 belonged to the stale first request and must not be shown.
        body = p.inner_text('#compare-results')
        self.assertNotIn('O4', body)

    def test_write_query_rejected_in_columns_lookup(self):
        # The columns endpoint must refuse non-SELECT sides.
        resp = self.page.request.get(
            self.base + '/compare/columns/',
            params={'dataset': self.before, 'source': 'query',
                    'sql': 'DROP TABLE orders'})
        self.assertEqual(resp.status, 400)
        self.assertIn('read-only', resp.json()['error'])

    def test_nav_has_compare_link(self):
        p = self.page
        p.goto(self.base + '/')
        self.assertTrue(p.locator('a[href="/compare/"]').count() >= 1)


if __name__ == '__main__':
    unittest.main()
