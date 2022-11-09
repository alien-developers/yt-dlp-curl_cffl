from __future__ import annotations

import contextlib
import functools
import http.client
import random
import ssl
import sys
import urllib.parse
import urllib.request
import uuid

from .exceptions import UnsupportedRequest
from ..dependencies import certifi
from ..socks import ProxyType
from ..utils import CaseInsensitiveDict, traverse_obj


def random_user_agent():
    _USER_AGENT_TPL = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/%s Safari/537.36'
    _CHROME_VERSIONS = (
        '90.0.4430.212',
        '90.0.4430.24',
        '90.0.4430.70',
        '90.0.4430.72',
        '90.0.4430.85',
        '90.0.4430.93',
        '91.0.4472.101',
        '91.0.4472.106',
        '91.0.4472.114',
        '91.0.4472.124',
        '91.0.4472.164',
        '91.0.4472.19',
        '91.0.4472.77',
        '92.0.4515.107',
        '92.0.4515.115',
        '92.0.4515.131',
        '92.0.4515.159',
        '92.0.4515.43',
        '93.0.4556.0',
        '93.0.4577.15',
        '93.0.4577.63',
        '93.0.4577.82',
        '94.0.4606.41',
        '94.0.4606.54',
        '94.0.4606.61',
        '94.0.4606.71',
        '94.0.4606.81',
        '94.0.4606.85',
        '95.0.4638.17',
        '95.0.4638.50',
        '95.0.4638.54',
        '95.0.4638.69',
        '95.0.4638.74',
        '96.0.4664.18',
        '96.0.4664.45',
        '96.0.4664.55',
        '96.0.4664.93',
        '97.0.4692.20',
    )
    return _USER_AGENT_TPL % random.choice(_CHROME_VERSIONS)


std_headers = CaseInsensitiveDict({
    'User-Agent': random_user_agent(),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-us,en;q=0.5',
    'Sec-Fetch-Mode': 'navigate',
})


def ssl_load_certs(context: ssl.SSLContext, params):
    if certifi is not None and 'no-certifi' not in params.get('compat_opts', []):
        context.load_verify_locations(cafile=certifi.where())
    else:
        try:
            context.load_default_certs()
        # Work around the issue in load_default_certs when there are bad certificates. See:
        # https://github.com/yt-dlp/yt-dlp/issues/1060,
        # https://bugs.python.org/issue35665, https://bugs.python.org/issue45312
        except ssl.SSLError:
            # enum_certificates is not present in mingw python. See https://github.com/yt-dlp/yt-dlp/issues/1151
            if sys.platform == 'win32' and hasattr(ssl, 'enum_certificates'):
                for storename in ('CA', 'ROOT'):
                    _ssl_load_windows_store_certs(context, storename)
            context.set_default_verify_paths()


def _ssl_load_windows_store_certs(ssl_context, storename):
    # Code adapted from _load_windows_store_certs in https://github.com/python/cpython/blob/main/Lib/ssl.py
    try:
        certs = [cert for cert, encoding, trust in ssl.enum_certificates(storename)
                 if encoding == 'x509_asn' and (
                     trust is True or ssl.Purpose.SERVER_AUTH.oid in trust)]
    except PermissionError:
        return
    for cert in certs:
        with contextlib.suppress(ssl.SSLError):
            ssl_context.load_verify_locations(cadata=cert)


def make_socks_proxy_opts(socks_proxy):
    url_components = urllib.parse.urlparse(socks_proxy)
    if url_components.scheme.lower() == 'socks5':
        socks_type = ProxyType.SOCKS5
    elif url_components.scheme.lower() in ('socks', 'socks4'):
        socks_type = ProxyType.SOCKS4
    elif url_components.scheme.lower() == 'socks4a':
        socks_type = ProxyType.SOCKS4A

    def unquote_if_non_empty(s):
        if not s:
            return s
        return urllib.parse.unquote_plus(s)
    return {
        'proxytype': socks_type,
        'addr': url_components.hostname,
        'port': url_components.port or 1080,
        'rdns': True,
        'username': unquote_if_non_empty(url_components.username),
        'password': unquote_if_non_empty(url_components.password),
    }


def bypass_proxies(url, no_proxy):
    # Should we bypass the proxies for this url going by no proxy?
    # This is a default configuration making use of urllib handling
    url_components = urllib.parse.urlparse(url)
    hostport = str(url_components.hostname) + (f':{url_components.port}' if url_components.port is not None else '')
    if urllib.request.proxy_bypass_environment(hostport, {'no': no_proxy}):
        return True
    elif urllib.request.proxy_bypass(hostport):  # check system settings
        return True

    return False


def select_proxy(url, proxies, no_proxies_func=bypass_proxies):
    """Unified proxy selector for all backends"""
    url_components = urllib.parse.urlparse(url)

    if 'no' in proxies and no_proxies_func and no_proxies_func(url, proxies['no']):
        return

    priority = [
        url_components.scheme or 'http',  # prioritise more specific mappings
        'all'
    ]
    return traverse_obj(proxies, *priority)


def get_redirect_method(method, status):
    """Unified redirect method handling"""

    # A 303 must either use GET or HEAD for subsequent request
    # https://datatracker.ietf.org/doc/html/rfc7231#section-6.4.4
    if status == 303 and method != 'HEAD':
        method = 'GET'
    # 301 and 302 redirects are commonly turned into a GET from a POST
    # for subsequent requests by browsers, so we'll do the same.
    # https://datatracker.ietf.org/doc/html/rfc7231#section-6.4.2
    # https://datatracker.ietf.org/doc/html/rfc7231#section-6.4.3
    if status in (301, 302) and method == 'POST':
        method = 'GET'
    return method


def handle_request_errors(func):
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        except UnsupportedRequest as e:
            if e.handler is None:
                e.handler = self
            raise
    return wrapper


def inject_header_http_conn(hc: http.client.HTTPConnection, hooks_dict):
    """
    Injects final header hook handling into http.client.HTTPConnection-like object.
    hooks_dict is assumed to be a shared dictionary that stores hook_id:hook_func
    """

    def putheader(self, header, *values):
        self._header_buffer[header] = values

    def endheaders(self, *args, **kwargs):
        hook_ids = self._header_buffer.pop('Ytdl-Header-Hook-Id', None)
        if hook_ids:
            hook_ids = hook_ids[0].split(',')
        for hook_id in hook_ids:
            hook = hooks_dict.get(hook_id)
            if hook:
                hook(self._header_buffer)
        for header, values in self._header_buffer.items():
            for value in values:
                self.putheader_real(header, value)
        self._header_buffer.clear()
        return self.endheaders_real(*args, **kwargs)

    hc._header_buffer = {}
    hc.putheader_real = hc.putheader
    hc.endheaders_real = hc.endheaders
    hc.putheader = functools.partial(putheader, hc)
    hc.endheaders = functools.partial(endheaders, hc)

    return hc


def register_header_hook(hooks_dict, hook_func):
    """
    Registers a hook function that will be called when a request is made.
    The hook function will be passed a dict of headers that will be sent.
    """
    if hook_func is None:
        return
    hook_id = str(uuid.uuid4())
    hooks_dict[hook_id] = hook_func
    return hook_id


def generate_header_hook_header(*hook_ids):
    return {'Ytdl-Header-Hook-Id': ','.join(hook_ids)}
