"""End-to-end smoke tests for VU CampusFix (standard library only).

Run from the project root:  python -m unittest discover -s tests -v
"""
import http.client, json, os, sys, tempfile, threading, unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix='campusfix_test_')
os.environ.update({
    'CAMPUSFIX_DATA_DIR': _TMP, 'CAMPUSFIX_DB_PATH': str(Path(_TMP) / 'test.db'),
    'ADMIN_TOKEN': 'test-token-123', 'TRUST_PROXY': 'true', 'UNRESTRICTED_MODE': 'false',
    'REPORT_RATE_LIMIT': '5', 'ANALYZE_RATE_LIMIT': '8', 'LOGIN_MAX_FAILURES': '5',
    'OLLAMA_HOST': 'http://127.0.0.1:9',  # nothing listens here -> rule-based fallback
})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'app'))
import campusfix_vu as app  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

app.Handler.log_message = lambda *args, **kwargs: None  # keep test output quiet

PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='
_counter = [0]


def fresh_ip():
    _counter[0] += 1
    return f'10.0.{_counter[0] // 250}.{_counter[0] % 250}'


class Client:
    def __init__(self, port, ip=None):
        self.port, self.ip, self.cookie = port, ip or fresh_ip(), ''

    def call(self, method, path, body=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=20)
        headers = {'Content-Type': 'application/json', 'X-Forwarded-For': self.ip}
        if self.cookie: headers['Cookie'] = self.cookie
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        res = conn.getresponse(); raw = res.read()
        set_cookie = res.getheader('Set-Cookie')
        if set_cookie: self.cookie = set_cookie.split(';')[0] if not set_cookie.split(';')[0].endswith('=') else ''
        conn.close()
        try: data = json.loads(raw.decode() or '{}')
        except ValueError: data = {'_raw': raw.decode(errors='replace')}
        return res.status, data, res


class CampusFixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close()

    def client(self, ip=None): return Client(self.port, ip)

    def admin(self):
        c = self.client(); status, _, _ = c.call('POST', '/api/auth/admin', {'token': 'test-token-123'})
        self.assertEqual(status, 200); return c

    def make_report(self, c, text='Water is overflowing from the sink near Room 204 and the floor is slippery.'):
        status, data, _ = c.call('POST', '/api/reports', {'user_description': text, 'user_location': 'Academic Wing A · Level 2', 'building_id': 'academic-a'})
        self.assertEqual(status, 201, data); return data

    # ---- public side ----
    def test_page_and_public_api(self):
        c = self.client(); status, data, res = c.call('GET', '/')
        self.assertEqual(status, 200); html = data['_raw']
        self.assertIn('CampusFix', html); self.assertNotIn('accounts.google.com', html); self.assertNotIn('__CATEGORIES__', html)
        self.assertIn("default-src 'self'", res.getheader('Content-Security-Policy'))
        self.assertEqual(c.call('GET', '/api/places')[0], 200)
        self.assertEqual(len(c.call('GET', '/api/places')[1]), 11)
        _, health, _ = c.call('GET', '/api/health'); self.assertEqual(health['status'], 'healthy'); self.assertFalse(health['model_available'])
        self.assertEqual(c.call('GET', '/api/auth/status')[1], {'is_admin': False, 'unrestricted': False})

    def test_public_can_submit_but_not_moderate(self):
        c = self.client(); report = self.make_report(c)
        self.assertEqual(report['status'], 'Submitted'); self.assertEqual(report['verification_state'], 'Pending verification')
        for key in ('reporter_email', 'reporter_sub', 'staff_notes', 'verified_by', 'image_data'): self.assertNotIn(key, report)
        rid = report['id']
        self.assertEqual(c.call('PATCH', f'/api/reports/{rid}', {'status': 'Resolved'})[0], 401)
        self.assertEqual(c.call('DELETE', f'/api/reports/{rid}')[0], 401)
        self.assertEqual(c.call('POST', '/api/places', {'name': 'Hacked block'})[0], 401)
        self.assertEqual(c.call('POST', '/api/places/reset', {})[0], 401)
        self.assertEqual(c.call('PATCH', '/api/places/cafeteria', {'x': 1})[0], 401)
        self.assertEqual(c.call('DELETE', '/api/places/cafeteria')[0], 401)
        self.assertEqual(c.call('GET', '/api/audit')[0], 401)
        self.assertEqual(c.call('GET', f'/api/reports/{rid}')[1]['status'], 'Submitted')

    def test_list_hides_staff_fields_and_images(self):
        admin = self.admin(); c = self.client()
        _, created, _ = c.call('POST', '/api/reports', {'user_description': 'Broken chair in the lecture hall, a leg is cracked.', 'user_location': 'Academic Wing B', 'building_id': 'academic-b', 'image_data': PNG})
        rid = created['id']
        admin.call('PATCH', f'/api/reports/{rid}', {'staff_notes': 'internal note about this chair'})
        public_list = c.call('GET', '/api/reports')[1]; mine = next(r for r in public_list if r['id'] == rid)
        self.assertNotIn('staff_notes', mine); self.assertNotIn('image_data', mine); self.assertTrue(mine['has_image'])
        self.assertNotIn('internal note', json.dumps(c.call('GET', f'/api/reports/{rid}')[1]))
        full = c.call('GET', f'/api/reports/{rid}')[1]; self.assertEqual(full['image_data'], PNG)
        admin_view = admin.call('GET', f'/api/reports/{rid}')[1]; self.assertEqual(admin_view['staff_notes'], 'internal note about this chair')

    def test_validation(self):
        c = self.client()
        base = {'user_description': 'Light not working in the corridor.', 'user_location': 'Library', 'building_id': 'library-study'}
        self.assertEqual(c.call('POST', '/api/reports', {**base, 'building_id': 'nope'})[0], 400)
        self.assertEqual(c.call('POST', '/api/reports', {**base, 'user_location': ''})[0], 400)
        self.assertEqual(c.call('POST', '/api/reports', {**base, 'user_description': ''})[0], 400)
        self.assertEqual(c.call('POST', '/api/reports', {**base, 'image_data': 'data:text/html;base64,PHNjcmlwdD4='})[0], 400)
        self.assertEqual(c.call('POST', '/api/reports/preflight', {})[0], 400)
        self.assertEqual(c.call('GET', '/api/reports/does-not-exist')[0], 404)
        self.assertEqual(c.call('GET', '/api/nope')[0], 404)

    def test_triage_fallback(self):
        c = self.client(); status, data, _ = c.call('POST', '/api/reports/analyze', {'description': 'Exposed wire sparking near the lab door', 'location': 'Labs'})
        self.assertEqual(status, 200); self.assertEqual(data['source'], 'rules-fallback')
        self.assertEqual(data['ai_analysis']['category'], 'Electrical'); self.assertEqual(data['ai_analysis']['priority'], 'Urgent')
        self.assertIn('integrity_score', data['ai_analysis'])

    def test_stored_text_is_returned_verbatim_for_client_escaping(self):
        c = self.client(); rid = self.make_report(c, '<img src=x onerror=alert(1)> water leak near the sink in Room 12')['id']
        self.assertIn('<img src=x', c.call('GET', f'/api/reports/{rid}')[1]['user_description'])

    # ---- moderator side ----
    def test_admin_flow(self):
        c = self.client(); self.assertEqual(c.call('POST', '/api/auth/admin', {'token': 'wrong'})[0], 401)
        a = self.admin(); self.assertEqual(a.call('GET', '/api/auth/status')[1]['is_admin'], True)
        rid = self.make_report(a)['id']
        status, updated, _ = a.call('PATCH', f'/api/reports/{rid}', {'status': 'In Progress', 'assigned_to': 'Plumbing maintenance', 'verification_state': 'Confirmed genuine', 'staff_notes': 'Checked on site.'})
        self.assertEqual(status, 200); self.assertEqual(updated['status'], 'In Progress'); self.assertEqual(updated['verified_by'], 'moderator')
        self.assertEqual(a.call('PATCH', f'/api/reports/{rid}', {'status': 'Bogus'})[0], 400)
        self.assertEqual(a.call('PATCH', f'/api/reports/{rid}', {'verification_state': 'Possible prank'})[0], 400)
        self.assertEqual(a.call('PATCH', '/api/reports/missing', {'status': 'Resolved'})[0], 404)
        actions = {e['action'] for e in a.call('GET', '/api/audit')[1]}
        self.assertTrue({'REPORT_CREATED', 'REPORT_UPDATED', 'MODERATOR_SIGN_IN'} <= actions)
        self.assertEqual(a.call('DELETE', f'/api/reports/{rid}')[0], 200); self.assertEqual(a.call('GET', f'/api/reports/{rid}')[0], 404)

    def test_map_editing_and_zone_protection(self):
        a = self.admin(); status, zone, _ = a.call('POST', '/api/places', {'name': 'Test Pavilion', 'kind': 'Outdoor'}); self.assertEqual(status, 201)
        self.assertEqual(a.call('PATCH', f"/api/places/{zone['id']}", {'x': 5000, 'y': -4})[1]['x'], 980)
        self.assertEqual(a.call('DELETE', f"/api/places/{zone['id']}")[0], 200)
        self.make_report(a)  # references academic-a
        self.assertEqual(a.call('DELETE', '/api/places/academic-a')[0], 409)

    def test_logout_and_forged_cookie(self):
        a = self.admin(); self.assertEqual(a.call('POST', '/api/auth/logout', {})[0], 200)
        self.assertEqual(a.call('GET', '/api/auth/status')[1]['is_admin'], False)
        forged = self.client(); forged.cookie = f'{app.COOKIE_NAME}=' + app.b64e(json.dumps({'role': 'admin', 'exp': 9999999999}).encode()) + '.AAAA'
        self.assertEqual(forged.call('GET', '/api/auth/status')[1]['is_admin'], False)
        self.assertEqual(forged.call('GET', '/api/audit')[0], 401)

    # ---- abuse limits ----
    def test_login_lockout_after_wrong_tokens(self):
        c = self.client()
        for _ in range(5): self.assertEqual(c.call('POST', '/api/auth/admin', {'token': 'guess'})[0], 401)
        self.assertEqual(c.call('POST', '/api/auth/admin', {'token': 'test-token-123'})[0], 429)

    def test_report_rate_limit(self):
        c = self.client()
        for _ in range(5): self.make_report(c)
        self.assertEqual(c.call('POST', '/api/reports', {'user_description': 'Another broken light', 'user_location': 'Library', 'building_id': 'library-study'})[0], 429)
        self.assertEqual(self.admin().call('POST', '/api/reports', {'user_description': 'Moderators are exempt from the limit.', 'user_location': 'Library', 'building_id': 'library-study'})[0], 201)

    def test_oversized_body_rejected(self):
        c = self.client(); conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        conn.request('POST', '/api/reports', b'{}', {'Content-Length': str(200 * 1024 * 1024), 'Content-Type': 'application/json'})
        # server answers before reading the body
        self.assertEqual(conn.getresponse().status, 400); conn.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
