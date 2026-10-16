import unittest
from unittest.mock import patch

import main
from services import nginx_proxy


class ProxyRouteTests(unittest.TestCase):
    def test_location_without_proxy_does_not_borrow_the_next_target(self):
        conf = '''
server_name example.test;
listen 443 ssl;
location /assets/ {
    root /static;
}
location /app/ {
    proxy_pass http://127.0.0.1:5017;
}
'''
        self.assertEqual(nginx_proxy._parse(conf, {'127.0.0.1'}), {5017: 'https://example.test/app/'})
        self.assertEqual(len(nginx_proxy._parse_routes(conf)), 1)

    def test_location_scoped_variables_resolve_to_separate_services(self):
        conf = '''
server_name example.test;
listen 443 ssl;
location /ops {
    set $upstream app;
    set $backend_port 5016;
    proxy_pass http://$upstream:$backend_port;
}
location /ui {
    set $upstream temporal-ui;
    proxy_pass http://$upstream:8080;
}
'''
        self.assertEqual(nginx_proxy._parse_routes(conf), [
            {'host': 'app', 'port': 5016, 'url': 'https://example.test/ops/'},
            {'host': 'temporal-ui', 'port': 8080, 'url': 'https://example.test/ui/'},
        ])

    def test_remote_host_is_retained_without_becoming_a_local_port(self):
        conf = '''
server_name home.example.test;
listen 443 ssl;
set $server "192.168.1.131";
set $port 8123;
'''
        self.assertEqual(nginx_proxy._parse(conf, {'127.0.0.1'}), {})
        self.assertEqual(nginx_proxy._parse_routes(conf)[0]['port'], 8123)

    def test_subpath_beats_root_for_the_same_local_port(self):
        conf = '''
server_name example.test;
set $server "127.0.0.1";
set $port 5011;
location /akai-mpc {
    proxy_pass http://127.0.0.1:5011;
}
location / {
    proxy_pass http://127.0.0.1:5011;
}
'''
        self.assertEqual(nginx_proxy._parse(conf, {'127.0.0.1'})[5011], 'http://example.test/akai-mpc/')


class AppLinkTests(unittest.TestCase):
    def attach(self, app, routes, local=None):
        with patch.object(main.nginx_proxy, 'public_routes', return_value=routes), \
                patch.object(main.nginx_proxy, 'public_urls', return_value=local or {}), \
                patch.object(main, '_load_settings', return_value={}):
            main._attach_public_urls([app])
        return app.get('url')

    def test_public_health_url_matches_the_most_specific_proxy_path(self):
        routes = [
            {'host': 'localhost', 'port': 3001, 'url': 'https://example.test/'},
            {'host': 'app', 'port': 5016, 'url': 'https://example.test/ops/'},
        ]
        app = {'healthCheck': {'type': 'http', 'target': 'https://example.test:443/ops/health'}}
        self.assertEqual(self.attach(app, routes), 'https://example.test/ops/')

    def test_dashboard_public_url_is_not_looked_up_by_https_port(self):
        url = 'https://dashboard.example.test/'
        app = {'publicUrl': url, 'healthCheck': {'type': 'http', 'target': url + 'health'}}
        self.assertEqual(self.attach(app, [{'host': 'localhost', 'port': 8765, 'url': url}]), url)

    def test_home_assistant_can_match_a_unique_remote_route(self):
        app = {'healthCheck': {'type': 'http', 'target': 'http://localhost:8123'}}
        url = 'https://home.example.test/'
        self.assertEqual(self.attach(app, [{'host': '192.168.1.131', 'port': 8123, 'url': url}]), url)

    def test_ambiguous_remote_ports_do_not_create_a_wrong_link(self):
        app = {'healthCheck': {'type': 'http', 'target': 'http://localhost:8123'}, 'url': 'https://stale.test/'}
        routes = [
            {'host': '192.168.1.131', 'port': 8123, 'url': 'https://one.test/'},
            {'host': '192.168.1.132', 'port': 8123, 'url': 'https://two.test/'},
        ]
        self.assertIsNone(self.attach(app, routes))

    def test_app_without_a_proxy_route_gets_no_public_link(self):
        app = {'healthCheck': {'type': 'http', 'target': 'http://localhost:3000/project'}}
        self.assertIsNone(self.attach(app, []))
