# Allow direct execution
import functools
import http.server
import gzip
import io
import os
import subprocess
import sys
import unittest
import urllib.request
from http.cookiejar import Cookie
from random import random

from yt_dlp.networking import UrllibRH, REQUEST_HANDLERS, UnsupportedRH, RequestsRH
from yt_dlp.networking.common import Request, RequestHandlerBroker, HEADRequest
from yt_dlp.utils import HTTPError, SSLError, TransportError, IncompleteRead

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test.helper import http_server_port, FakeYDL, is_download_test, get_params
from yt_dlp import YoutubeDL
from yt_dlp.compat import compat_http_server
import ssl
import threading

TEST_DIR = os.path.dirname(os.path.abspath(__file__))

HTTP_TEST_BACKEND_HANDLERS = [UrllibRH, RequestsRH]


class FakeLogger(object):
    def debug(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


class HTTPTestRequestHandler(compat_http_server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'  # required for persistent connections

    def log_message(self, format, *args):
        pass

    def _redirect(self):
        self.send_response(int(self.path[len('/redirect_'):]))
        self.send_header('Location', '/gen_204')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_HEAD(self):
        if self.path.startswith('/redirect_'):
            self._redirect()
        else:
            assert False

    def do_GET(self):
        if self.path == '/video.html':
            payload = b'<html><video src="/vid.mp4" /></html>'
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))  # required for persistent connections
            self.end_headers()
            self.wfile.write(payload)
        elif self.path == '/vid.mp4':
            payload = b'\x00\x00\x00\x00\x20\x66\x74[video]'
            self.send_response(200)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif self.path == '/%E4%B8%AD%E6%96%87.html':
            payload = b'<html><video src="/vid.mp4" /></html>'
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif self.path.startswith('/gen_'):
            payload = b'<html></html>'
            self.send_response(int(self.path[len('/gen_'):]))
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif self.path.startswith('/redirect_loop'):
            self.send_response(301)
            self.send_header('Location', self.path)
            self.send_header('Content-Length', '0')
            self.end_headers()
        elif self.path.startswith('/redirect_'):
            self._redirect()
        elif self.path.startswith('/incompleteread'):
            payload = b'<html></html>'
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', '234234')
            self.end_headers()
            self.wfile.write(payload)
            self.finish()
        elif self.path.startswith('/headers'):
            payload = str(self.headers).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif self.path == '/trailing_garbage':
            payload = b'<html><video src="/vid.mp4" /></html>'
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Encoding', 'gzip')
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode='wb') as f:
                f.write(payload)
            compressed = buf.getvalue()
            self.send_header('Content-Length', str(len(compressed)+len(b'trailing garbage')))
            self.end_headers()
            self.wfile.write(compressed + b'trailing garbage')
        else:
            assert False


def _build_proxy_handler(name):
    class HTTPTestRequestHandler(compat_http_server.BaseHTTPRequestHandler):
        proxy_name = name

        def log_message(self, format, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write('{self.proxy_name}: {self.path}'.format(self=self).encode('utf-8'))
    return HTTPTestRequestHandler

# TODO: what to do with request handlers that do not support everything
# TODO: is there a better way


class RequestHandlerTestBase:
    handler = UnsupportedRH

    def make_ydl(self, params=None, fake=True):
        ydl = (FakeYDL if fake else YoutubeDL)(params)
        ydl.http = ydl.build_http([self.handler])
        return ydl


class RequestHandlerCommonTestsBase(RequestHandlerTestBase):
    def setUp(self):
        # HTTP server
        self.http_httpd = http.server.ThreadingHTTPServer(
            ('127.0.0.1', 0), HTTPTestRequestHandler)
        self.http_port = http_server_port(self.http_httpd)
        self.http_server_thread = threading.Thread(target=self.http_httpd.serve_forever)
        self.http_server_thread.daemon = True
        self.http_server_thread.start()

        # HTTPS server
        certfn = os.path.join(TEST_DIR, 'testcert.pem')
        self.https_httpd = http.server.ThreadingHTTPServer(
            ('127.0.0.1', 0), HTTPTestRequestHandler)
        self.https_httpd.socket = ssl.wrap_socket(
            self.https_httpd.socket, certfile=certfn, server_side=True)
        self.https_port = http_server_port(self.https_httpd)
        self.https_server_thread = threading.Thread(target=self.https_httpd.serve_forever)
        self.https_server_thread.daemon = True
        self.https_server_thread.start()

        # HTTP Proxy server
        self.proxy = http.server.ThreadingHTTPServer(
            ('127.0.0.1', 0), _build_proxy_handler('normal'))
        self.proxy_port = http_server_port(self.proxy)
        self.proxy_thread = threading.Thread(target=self.proxy.serve_forever)
        self.proxy_thread.daemon = True
        self.proxy_thread.start()

        # Geo proxy server
        self.geo_proxy = http.server.ThreadingHTTPServer(
            ('127.0.0.1', 0), _build_proxy_handler('geo'))
        self.geo_port = http_server_port(self.geo_proxy)
        self.geo_proxy_thread = threading.Thread(target=self.geo_proxy.serve_forever)
        self.geo_proxy_thread.daemon = True
        self.geo_proxy_thread.start()

    def test_nocheckcertificate(self):
        ydl = self.make_ydl({'logger': FakeLogger()})
        self.assertRaises(
            SSLError,
            ydl.urlopen, 'https://127.0.0.1:%d/video.html' % self.https_port)
        ydl = self.make_ydl({'logger': FakeLogger(), 'nocheckcertificate': True}, fake=False)
        r = ydl.extract_info('https://127.0.0.1:%d/video.html' % self.https_port)
        self.assertEqual(r['entries'][0]['url'], 'https://127.0.0.1:%d/vid.mp4' % self.https_port)

    def test_http_proxy(self):
        geo_proxy = '127.0.0.1:{0}'.format(self.geo_port)
        geo_proxy2 = 'localhost:{0}'.format(self.geo_port)  # ensure backend can support this format
        ydl = self.make_ydl({
            'proxy': '127.0.0.1:{0}'.format(self.proxy_port),
            'geo_verification_proxy': geo_proxy,
        })
        url = 'http://foo.com/bar'
        response = ydl.urlopen(url).read().decode('utf-8')
        self.assertEqual(response, 'normal: {0}'.format(url))
        req = Request(url)
        req.add_header('Ytdl-request-proxy', geo_proxy2)
        response1 = ydl.urlopen(req).read().decode('utf-8')
        response2 = ydl.urlopen(Request(url, proxies={'http': geo_proxy})).read().decode('utf-8')
        self.assertEqual(response1, 'geo: {0}'.format(url))
        self.assertEqual(response2, 'geo: {0}'.format(url))
        # test that __noproxy__ disables all proxies for that request
        real_url = 'http://127.0.0.1:%d/headers' % self.http_port
        response3 = ydl.urlopen(
            Request(real_url, headers={'Ytdl-request-proxy': '__noproxy__'})).read().decode('utf-8')
        self.assertNotEqual(response3, f'normal: {real_url}')
        self.assertNotIn('Ytdl-request-proxy', response3)
        self.assertIn('Accept', response3)

    def test_http_proxy_with_idn(self):
        ydl = self.make_ydl({
            'proxy': '127.0.0.1:{0}'.format(self.proxy_port),
        })
        url = 'http://中文.tw/'
        response = ydl.urlopen(url).read().decode('utf-8')
        # b'xn--fiq228c' is '中文'.encode('idna')
        self.assertEqual(response, 'normal: http://xn--fiq228c.tw/')

    def test_raise_http_error(self):
        ydl = self.make_ydl()
        for bad_status in (400, 500, 599, 302):
            with self.assertRaises(HTTPError):
                ydl.urlopen('http://127.0.0.1:%d/gen_%d' % (self.http_port, bad_status))
            # wait for server to detect that the connection has been dropped; since it can only handle one at a time
        # Should not raise an error
        ydl.urlopen('http://127.0.0.1:%d/gen_200' % self.http_port)

    def test_redirect_loop(self):
        ydl = self.make_ydl()
        with self.assertRaisesRegex(HTTPError, r'HTTP Error 301: Moved Permanently \(redirect loop detected\)'):
            ydl.urlopen('http://127.0.0.1:%d/redirect_loop' % self.http_port)

    def test_get_url(self):
        ydl = self.make_ydl()
        res = ydl.urlopen('http://127.0.0.1:%d/redirect_301' % self.http_port)
        self.assertEqual(res.url, 'http://127.0.0.1:%d/gen_204' % self.http_port)
        res.close()
        res2 = ydl.urlopen('http://127.0.0.1:%d/gen_200' % self.http_port)
        self.assertEqual(res2.url, 'http://127.0.0.1:%d/gen_200' % self.http_port)
        res2.close()

    def test_redirect(self):
        # TODO
        ydl = self.make_ydl()
        # HEAD request. Should follow through with head request to gen_204 which should fail.
        with self.assertRaises(TransportError):
            ydl.urlopen(HEADRequest('http://127.0.0.1:%d/redirect_301' % self.http_port))

        #res = ydl.urlopen('http://127.0.0.1:%d/redirect_301' % self.http_port)
        #self.assertEquals(res.method, 'GET')

    def test_incompleteread(self):
        ydl = self.make_ydl({'socket_timeout': 2})
        with self.assertRaises(IncompleteRead):
            ydl.urlopen('http://127.0.0.1:%d/incompleteread' % self.http_port).read()

    def test_cookiejar(self):
        ydl = self.make_ydl()
        ydl.cookiejar.set_cookie(
            Cookie(
                0, 'test', 'ytdlp', None, False, '127.0.0.1', True,
                False, '/headers', True, False, None, False, None, None, {}))
        data = ydl.urlopen('http://127.0.0.1:%d/headers' % self.http_port).read()
        self.assertIn(b'Cookie: test=ytdlp', data)

    def test_request_types(self):
        ydl = self.make_ydl()
        url = 'http://127.0.0.1:%d/headers' % self.http_port
        test_header = {'X-ydl-test': '1'}
        # by url
        self.assertTrue(ydl.urlopen(url).read())

        # urllib Request compat and ydl Request
        for request in (urllib.request.Request(url, headers=test_header), Request(url, headers=test_header)):
            data = ydl.urlopen(request).read()
            self.assertIn(b'X-Ydl-Test: 1', data)

        with self.assertRaises(AssertionError):
            ydl.urlopen(None)

    def test_no_compression(self):
        ydl = self.make_ydl()
        url = 'http://127.0.0.1:%d/headers' % self.http_port
        for request in (Request(url, compression=False), Request(url, headers={'Youtubedl-no-compression': '1'})):
            data = ydl.urlopen(request).read()
            if b'Accept-Encoding' in data:
                self.assertIn(b'Accept-Encoding: identity', data)

    def test_gzip_trailing_garbage(self):
        # https://github.com/ytdl-org/youtube-dl/commit/aa3e950764337ef9800c936f4de89b31c00dfcf5
        # https://github.com/ytdl-org/youtube-dl/commit/6f2ec15cee79d35dba065677cad9da7491ec6e6f
        ydl = self.make_ydl()
        data = ydl.urlopen('http://localhost:%d/trailing_garbage' % self.http_port).read().decode('utf-8')
        self.assertEqual(data, '<html><video src="/vid.mp4" /></html>')


def with_request_handlers(handlers=HTTP_TEST_BACKEND_HANDLERS):
    def inner_func(test):
        @functools.wraps(test)
        def wrapper(self, *args, **kwargs):
            for handler in handlers:
                with self.subTest(handler=handler.__name__):
                    self.handler = handler
                    test(self, *args, **kwargs)
        return wrapper
    return inner_func


class TestUrllibRH(RequestHandlerCommonTestsBase, unittest.TestCase):
    handler = UrllibRH


class TestRequestsRH(RequestHandlerCommonTestsBase, unittest.TestCase):
    """
    Notes
    - test_redirect_loop: the error doesn't say we hit a loop
    """
    handler = RequestsRH

    def test_close_conn_on_http_error(self):
        from urllib3.util.connection import is_connection_dropped
        ydl = self.make_ydl()
        res = ydl.urlopen(Request('http://127.0.0.1:%d/gen_200' % self.http_port, compression=False))
        # Get connection before we read, since it gets released back to pool after read
        conn = res.raw.raw.connection
        self.assertIsNotNone(conn)
        a = res.read()
        self.assertFalse(is_connection_dropped(conn))
        with self.assertRaises(HTTPError) as e:
            ydl.urlopen(Request('http://127.0.0.1:%d/gen_404' % self.http_port, compression=False))
        self.assertIs(conn, e.exception.response.raw.raw.connection)
        e.exception.response.read()
        self.assertTrue(is_connection_dropped(conn))

    def test_no_persistent_connections(self):
        ydl = self.make_ydl({'no_persistent_connections': True})
        content = str(ydl.urlopen(Request('http://127.0.0.1:%d/headers' % self.http_port, compression=False)).read())
        self.assertIn('Connection: close', content)


if __name__ == '__main__':
    unittest.main()
