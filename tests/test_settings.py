import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

import app
import router_settings
import routing


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.initial = routing.Policy({'aisc_hosts':['aisc.must.edu.mo'], 'school_networks':['10.20.0.0/16']})
        app.save(self.state/'routing.json', self.initial.data)
        self.server = router_settings.make_server(self.state, app.ROOT/'router_settings.html',
            lambda policy: app.save(self.state/'routing.json',policy.data))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.close)
        self.prefix = self.server.url[len(self.server.origin):]

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(3)

    def request(self, path='api/settings', method='GET', fields=None, headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            conn.request(method, self.prefix+path, body=None if fields is None else json.dumps(fields), headers=headers or {})
            response = conn.getresponse(); body = response.read()
            return response.status, dict(response.getheaders()), body
        finally: conn.close()

    def write_headers(self):
        return {'Origin':self.server.origin,'Content-Type':'application/json','X-Router-Settings':self.prefix.strip('/')}

    def test_page_and_config_work_without_vm(self):
        status, headers, body = self.request('')
        self.assertEqual(status,200); self.assertIn('连接设置',body.decode())
        self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])
        status, _, body = self.request()
        self.assertEqual(status,200); self.assertFalse(json.loads(body)['server_ready'])
        self.assertFalse((self.state/'session.json').exists())

    def test_save_modes_preserves_unrelated_scope_and_reloads(self):
        for mode in ['ssh','all','domains']:
            status, _, body = self.request(method='POST', fields={'traffic_mode':mode,
                'proxy_domains':['AISC.MUST.EDU.MO.','aisc.must.edu.mo'],'fallback':False,
                'ssh_ports':[22,2222],'direct_timeout':2},headers=self.write_headers())
            self.assertEqual(status,200,body)
            policy = json.loads(self.request()[2])['policy']
            self.assertEqual(policy['traffic_mode'],mode)
            self.assertEqual(policy['school_networks'],['10.20.0.0/16'])
            self.assertEqual(policy['proxy_domains'],['aisc.must.edu.mo'])
        self.assertFalse((self.state/'session.json').exists())

    def test_invalid_domains_ports_and_fields_do_not_change_disk(self):
        original = (self.state/'routing.json').read_bytes()
        for fields in [{'traffic_mode':'domains','proxy_domains':[]}, {'proxy_domains':['https://aisc.must.edu.mo']},
                       {'ssh_ports':[0]}, {'ssh_ports':[True]}, {'direct_timeout':61}, {'aisc_hosts':['evil.example']}]:
            status, _, _ = self.request(method='POST', fields=fields, headers=self.write_headers())
            self.assertEqual(status,400)
            self.assertEqual((self.state/'routing.json').read_bytes(),original)

    def test_cross_origin_missing_token_and_rebinding_are_rejected(self):
        for headers in [{}, {**self.write_headers(),'Origin':'https://example.com'},
                        {**self.write_headers(),'X-Router-Settings':'wrong'},
                        {**self.write_headers(),'Host':'evil.example'}]:
            status, _, _ = self.request(method='POST',fields={'traffic_mode':'ssh'},headers=headers)
            self.assertEqual(status,403)
        self.assertEqual(routing.Policy.load(self.state).data['traffic_mode'],'all')

    def test_close_requires_same_origin_and_stops_only_settings(self):
        status, _, _ = self.request('api/close','POST',{},self.write_headers())
        self.assertEqual(status,200); self.assertTrue(self.server.stopping.is_set())
        self.assertFalse((self.state/'session.json').exists())


if __name__=='__main__': unittest.main()
