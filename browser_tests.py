"""
Real-browser verification for the row comparison feature.

These tests drive the actual running application with Chromium via Playwright,
covering the things the Flask test client cannot: the in-place results panel,
the rapid-condition race (last request wins), tab/pagination clicks and the
jump-back link that highlights a row in the original query.

Usage:
    python browser_tests.py

Requires: playwright (`pip install playwright && playwright install chromium`)
and the usual Chromium shared libraries. Skips gracefully when unavailable.
"""

import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request


PORT = int(os.environ.get('SQLITE_WEB_TEST_PORT', '8191'))
BASE = 'http://127.0.0.1:%d' % PORT


def build_databases(tmp):
    """Two independent snapshot files plus a pair for pagination/races."""
    before = os.path.join(tmp, 'before.db')
    after = os.path.join(tmp, 'after.db')
    schema = ('CREATE TABLE orders (id INTEGER PRIMARY KEY, order_no TEXT, '
              'amount REAL, status TEXT);')
    before_rows = [(1, 'A-001', 100.0, None),
                   (2, 'A-002', 50.0, 'pending'),
                   (4, 'A-004', 1.0, 'gone')]
    after_rows = [(1, 'A-001', 100.0, 'settled'),     # NULL -> settled
                  (2, 'A-002', 51.0, 'pending'),      # 50 -> 51
                  (3, 'A-003', 5.0, None)]            # added; 4 deleted
    for path, rows in ((before, before_rows), (after, after_rows)):
        conn = sqlite3.connect(path)
        conn.execute(schema)
        conn.executemany('INSERT INTO orders VALUES (?, ?, ?, ?)', rows)
        conn.commit()
        conn.close()

    big_l = os.path.join(tmp, 'big_l.db')
    big_r = os.path.join(tmp, 'big_r.db')
    for path, lo in ((big_l, 0), (big_r, 50)):
        conn = sqlite3.connect(path)
        conn.execute('CREATE TABLE t (k TEXT PRIMARY KEY, v TEXT)')
        # Overlapping keys (k050..k099) carry identical values on both
        # sides, so they match rather than showing as changed.
        conn.executemany('INSERT INTO t VALUES (?, ?)',
                         [('k%03d' % (lo + i), 'v%03d' % (lo + i))
                          for i in range(100)])
        conn.commit()
        conn.close()
    return before, after, big_l, big_r


def wait_for_server(timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(BASE + '/', timeout=1)
            return True
        except Exception:
            time.sleep(0.2)
    return False


class Checks(object):
    def __init__(self):
        self.passed = 0
        self.failed = []

    def check(self, name, cond, detail=''):
        if cond:
            self.passed += 1
            print('  PASS:', name)
        else:
            self.failed.append((name, detail))
            print('  FAIL:', name, detail)


def run_scenarios(before, after, big_l, big_r):
    from playwright.sync_api import sync_playwright
    from urllib.parse import parse_qs, urlparse

    checks = Checks()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        def configure(l_idx, r_idx):
            page.goto(BASE + '/compare/')
            page.select_option('#dataset-l', index=l_idx)
            page.select_option('#dataset-r', index=r_idx)
            page.fill('#sql-l', 'SELECT * FROM orders')
            page.fill('#sql-r', 'SELECT * FROM orders')
            page.click('#compare-load-columns')
            page.wait_for_selector('.compare-key-option input[value="id"]')
            page.check('.compare-key-option input[value="id"]')

        print('Scenario: snapshot settlement diff')
        configure(0, 1)
        with page.expect_response(lambda r: '/compare/results/' in r.url):
            page.click('#compare-form button[type=submit]')
        page.wait_for_selector('.compare-summary')
        text = page.inner_text('#compare-results')
        checks.check('total count at the top', 'Total differences: 4' in text)
        checks.check('added/deleted/changed counts',
                     all(s in text for s in
                         ('Added 1', 'Deleted 1', 'Changed 2')))

        print('Scenario: changed cells pinpoint fields incl. NULL')
        page.click('.compare-tab[data-tab="modified"]')
        page.wait_for_selector('.compare-modified')
        checks.check('changed cells highlighted',
                    page.locator('.diff-changed').count() == 4)
        titles = page.eval_on_selector_all(
            '.diff-changed', 'els => els.map(e => e.title)')
        checks.check('NULL vs text distinguished',
                    any('null' in t for t in titles) and
                    any('text' in t for t in titles), str(titles))

        print('Scenario: jump back keeps the original query and marks row')
        with page.expect_navigation():
            page.click('.compare-modified:first-child .compare-jump')
        checks.check('lands on query page', '/query/' in page.url)
        checks.check('row highlighted', page.locator('#focus-row').count() == 1)
        checks.check('right row', 'A-001' in page.inner_text('#focus-row'))
        checks.check('original sql in url',
                    'SELECT * FROM orders' in
                    parse_qs(urlparse(page.url).query).get('sql', [''])[0])

        print('Scenario: columns only on one side are listed, not an error')
        configure(0, 1)
        page.fill('#sql-r', "SELECT *, 'x' AS tag FROM orders")
        with page.expect_response(
                lambda r: '/compare/results/' in r.url and 'tag' in r.url):
            page.click('#compare-form button[type=submit]')
        text = page.inner_text('#compare-results')
        checks.check('right-only column named', 'tag' in text and
                    'only on the right' in text, text[:200])

        print('Scenario: pagination over large tables')
        page.select_option('#dataset-l', index=2)
        page.select_option('#dataset-r', index=3)
        page.fill('#sql-l', 'SELECT * FROM t')
        page.fill('#sql-r', 'SELECT * FROM t')
        page.click('#compare-load-columns')
        page.wait_for_selector('.compare-key-option input[value="k"]')
        page.check('.compare-key-option input[value="k"]')
        with page.expect_response(lambda r: '/compare/results/' in r.url):
            page.click('#compare-form button[type=submit]')
        page.wait_for_selector('.compare-summary')
        text = page.inner_text('#compare-results')
        checks.check('100 differences', 'Total differences: 100' in text)
        checks.check('50 rows on first page',
                    page.locator('.compare-row').count() == 50)
        page.click('.compare-page[data-page="2"]')
        page.wait_for_function(
            "document.querySelector('.compare-pagination').textContent"
            ".includes('Page 2 of 2')")
        checks.check('last page bounded', page.locator('.compare-row').count() == 50)

        print('Scenario: rapid condition changes show only the last result')
        delays = {'SELECT * FROM t': 1500}

        def handle(route):
            q = parse_qs(urlparse(route.request.url).query)
            d = delays.get(q.get('sql_r', [''])[0], 0)
            if d:
                page.wait_for_timeout(d)
            route.continue_()
        page.route('**/compare/results/**', handle)
        page.fill('#sql-r', 'SELECT * FROM t')
        page.click('#compare-form button[type=submit]')  # slow full compare
        page.wait_for_timeout(200)
        page.fill('#sql-r', "SELECT * FROM t WHERE k = 'k050'")  # final
        delays.clear()
        page.click('#compare-form button[type=submit]')  # fast
        page.wait_for_timeout(2500)
        text = page.inner_text('#compare-results')
        # Final right side holds the single shared key k050, which matches
        # the left unchanged; the other 99 left rows show as deleted.
        checks.check('final condition dominates (99 deleted, 0 added)',
                    'Deleted 99' in text and 'Added 0' in text and
                    'Unchanged 1' in text, text[:150])
        page.unroute('**/compare/results/**')

        print('Scenario: original functions keep working')
        page.goto(BASE + '/select-dataset/?name=' +
                  urllib.parse.quote(big_l, safe=''))
        page.goto(BASE + '/t/content/')
        checks.check('content tab', page.locator('table').count() >= 1)
        with page.expect_download() as info:
            page.goto(BASE + '/t/export/')
            page.click('form[action$="/t/export/"] button[type=submit]')
        checks.check('export downloads', info.value.suggested_filename
                    .startswith('t-'))

        browser.close()
    return checks


def main():
    import importlib.util
    if importlib.util.find_spec('playwright') is None:
        print('Skipping browser tests: playwright is not installed.')
        return 0

    tmp = tempfile.mkdtemp(prefix='sqlite-web-browser-')
    proc = None
    try:
        before, after, big_l, big_r = build_databases(tmp)
        env = dict(os.environ)
        proc = subprocess.Popen(
            [sys.executable, '-m', 'sqlite_web.sqlite_web',
             '-x', '-p', str(PORT), '-H', '127.0.0.1',
             before, after, big_l, big_r],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        if not wait_for_server():
            print('Server did not start:\n', proc.stdout.read().decode())
            return 2
        checks = run_scenarios(before, after, big_l, big_r)
    finally:
        if proc is not None:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=10)
        shutil.rmtree(tmp, ignore_errors=True)

    print('\n%d passed, %d failed' % (
        checks.passed, len(checks.failed)))
    return 1 if checks.failed else 0


if __name__ == '__main__':
    sys.exit(main())
