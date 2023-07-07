import contextlib
import functools
import http.client
import logging
import re
import socket
import warnings

from ..dependencies import OptionalDependencyWarning, brotli, requests, urllib3

if requests is None:
    raise ImportError('requests module is not installed')

if urllib3 is None:
    raise ImportError('urllib3 module is not installed')

from http.client import HTTPConnection

import requests.adapters
import requests.utils
import urllib3.connection
import urllib3.exceptions

from .common import Features, RequestHandler, Response
from .exceptions import (
    HTTPError,
    IncompleteRead,
    ProxyError,
    RequestError,
    SSLError,
    TransportError, CertificateVerifyError,
)
from .utils import (
    InstanceStoreMixin,
    add_accept_encoding_header,
    get_redirect_method,
    make_socks_proxy_opts,
    select_proxy,
)
from ..socks import ProxyError as SocksProxyError
from ..socks import sockssocket
from ..utils import int_or_none

SUPPORTED_ENCODINGS = [
    'gzip', 'deflate'
]


urllib3_version = urllib3.__version__.split('.')
if len(urllib3_version) == 2:
    urllib3_version.append('0')

urllib3_version = tuple(map(functools.partial(int_or_none, default=0), urllib3_version[:3]))

# urllib3 does not support brotlicffi on versions < 1.26.9 [1], and brotli on < 1.25.1 [2]
# 1: https://github.com/urllib3/urllib3/blob/1.26.x/CHANGES.rst#1269-2022-03-16
# 2: https://github.com/urllib3/urllib3/blob/main/CHANGES.rst#1251-2019-04-24
if (brotli is not None
        and not (brotli.__name__ == 'brotlicffi' and urllib3_version < (1, 26, 9))
        and not (brotli.__name__ == 'brotli' and urllib3_version < (1, 25, 1))):
    SUPPORTED_ENCODINGS.append('br')

# requests < 2.24.0 always uses pyopenssl by default if installed.
# We do not support pyopenssl's SSLContext, so we need to revert this.
# See: https://github.com/psf/requests/pull/5443
if requests.__build__ < 0x022400:
    with contextlib.suppress(ImportError, AttributeError):
        from urllib3.contrib import pyopenssl
        pyopenssl.extract_from_urllib3()

"""
Override urllib3's behavior to not convert lower-case percent-encoded characters
to upper-case during url normalization process.

RFC3986 defines that the lower or upper case percent-encoded hexidecimal characters are equivalent
and normalizers should convert them to uppercase for consistency [1].

However, some sites may have an incorrect implementation where they provide
a percent-encoded url that is then compared case-sensitively.[2]

While this is a very rare case, since urllib does not do this normalization step, it
is best to avoid it here too for compatability reasons.

1: https://tools.ietf.org/html/rfc3986#section-2.1
2: https://github.com/streamlink/streamlink/pull/4003
"""


class _Urllib3PercentREOverride:
    def __init__(self, r: re.Pattern):
        self.re = r

    # pass through all other attribute calls to the original re
    def __getattr__(self, item):
        return self.re.__getattribute__(item)

    def subn(self, repl, string, *args, **kwargs):
        return string, self.re.subn(repl, string, *args, **kwargs)[1]

    def findall(self, component, *args, **kwargs):
        return [c.upper() for c in self.re.findall(component, *args, **kwargs)]


if urllib3_version >= (1, 25, 4):
    # urllib3 >= 1.25.8 uses subn:
    # https://github.com/urllib3/urllib3/commit/a2697e7c6b275f05879b60f593c5854a816489f0
    # 1.25.4 <= urllib3 < 1.25.8 uses findall:
    # https://github.com/urllib3/urllib3/commit/5b047b645f5f93900d5e2fc31230848c25eb1f5f
    import urllib3.util.url
    if hasattr(urllib3.util.url, 'PERCENT_RE'):
        urllib3.util.url.PERCENT_RE = _Urllib3PercentREOverride(urllib3.util.url.PERCENT_RE)
    elif hasattr(urllib3.util.url, '_PERCENT_RE'):  # urllib3 >= 2.0.0
        urllib3.util.url._PERCENT_RE = _Urllib3PercentREOverride(urllib3.util.url._PERCENT_RE)
    else:
        warnings.warn('Failed to patch PERCENT_RE in urllib3 (does the attribute exist?)', OptionalDependencyWarning)
elif (1, 25, 0) <= urllib3_version < (1, 25, 4):
    # 1.25.0 <= urllib3 < 1.25.4 uses rfc3986 normalizers package:
    # https://github.com/urllib3/urllib3/commit/a74c9cfbaed9f811e7563cfc3dce894928e0221a
    # https://github.com/urllib3/urllib3/commit/0aa3e24fcd75f1bb59ab159e9f8adb44055b2271
    import urllib3.packages.rfc3986.normalizers as normalizers
    normalizers.PERCENT_MATCHER = _Urllib3PercentREOverride(normalizers.PERCENT_MATCHER)

"""
Workaround for issue in urllib.util.ssl_.py. ssl_wrap_context does not pass
server_hostname to SSLContext.wrap_socket if server_hostname is an IP,
however this is an issue because we set check_hostname to True in our SSLContext.

Monkey-patching IS_SECURETRANSPORT forces ssl_wrap_context to pass server_hostname regardless.

This has been fixed in urllib3 2.0.
See: https://github.com/urllib3/urllib3/issues/517
"""

if urllib3_version < (2, 0, 0):
    try:
        urllib3.util.IS_SECURETRANSPORT = urllib3.util.ssl_.IS_SECURETRANSPORT = True
    except AttributeError:
        pass


class RequestsResponse(Response):
    def __init__(self, res: requests.models.Response):
        super().__init__(
            fp=res.raw, headers=res.headers, url=res.url,
            status=res.status_code, reason=res.reason)

        self.requests_response = res

    def read(self, amt: int = None):
        try:
            # Interact with urllib3 response directly.
            return self.raw.read(amt, decode_content=True)
        # raw is an urllib3 HTTPResponse, so exceptions will be from urllib3
        except urllib3.exceptions.HTTPError as e:
            handle_urllib3_read_exceptions(e)
            raise TransportError(cause=e) from e


def find_original_error(e, err_types):
    if not isinstance(e, Exception):
        return
    return next(
        (err for err in (e, e.__cause__, *(e.args or [])) if
         isinstance(err, err_types)), None)


def handle_urllib3_read_exceptions(e):
    # Sometimes IncompleteRead is wrapped by urllib.exceptions.ProtocolError, so we have to check the args
    ic_read_err = find_original_error(e, (http.client.IncompleteRead, urllib3.exceptions.IncompleteRead))
    if ic_read_err is not None:
        raise IncompleteRead(partial=ic_read_err.partial, expected=ic_read_err.expected)
    if isinstance(e, urllib3.exceptions.SSLError):
        raise SSLError(cause=e) from e


class RequestsHTTPAdapter(requests.adapters.HTTPAdapter):
    def __init__(self, ssl_context=None, proxy_ssl_context=None, source_address=None, **kwargs):
        self._pm_args = {}
        if ssl_context:
            self._pm_args['ssl_context'] = ssl_context
        if source_address:
            self._pm_args['source_address'] = (source_address, 0)
        self._proxy_ssl_context = proxy_ssl_context or ssl_context
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        return super().init_poolmanager(*args, **kwargs, **self._pm_args)

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        extra_kwargs = {}
        if not proxy.lower().startswith('socks') and self._proxy_ssl_context and urllib3_version >= (1, 26, 0):
            extra_kwargs['proxy_ssl_context'] = self._proxy_ssl_context
        return super().proxy_manager_for(proxy, **proxy_kwargs, **self._pm_args, **extra_kwargs)

    def cert_verify(*args, **kwargs):
        # lean on SSLContext for cert verification
        pass


class RequestsSession(requests.sessions.Session):
    """
    Ensure unified redirect method handling with our urllib redirect handler.
    """
    def rebuild_method(self, prepared_request, response):
        new_method = get_redirect_method(prepared_request.method, response.status_code)

        # HACK: requests removes headers/body on redirect unless code was a 307/308.
        if not new_method != prepared_request.method:
            response._real_status_code = response.status_code
            response.status_code = 308

        prepared_request.method = new_method

    def rebuild_auth(self, prepared_request, response):
        # HACK: undo status code change from rebuild_method, if applicable.
        # rebuild_auth runs after requests would remove headers/body based on status code
        if hasattr(response, '_real_status_code'):
            response.status_code = response._real_status_code
            del response._real_status_code
        return super().rebuild_auth(prepared_request, response)


class Urllib3LoggingFilter(logging.Filter):

    def filter(self, record):
        # Ignore HTTP request messages since HTTPConnection prints those
        if record.msg == '%s://%s:%s "%s %s %s" %s %s':
            return False
        return True


class Urllib3LoggingHandler(logging.Handler):
    """Redirect urllib3 logs to our logger"""
    def __init__(self, logger, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._logger = logger

    def emit(self, record):
        try:
            msg = self.format(record)
            if record.levelno >= logging.ERROR:
                self._logger.report_error(msg)
            else:
                self._logger.to_stdout(msg)

        except Exception:
            self.handleError(record)


class RequestsRH(RequestHandler, InstanceStoreMixin):

    """Requests RequestHandler
    Params:

    @param max_pools: Max number of urllib3 connection pools to cache.
    @param pool_maxsize: Max number of connections per pool.
    """
    _SUPPORTED_URL_SCHEMES = ('http', 'https')
    _SUPPORTED_ENCODINGS = tuple(SUPPORTED_ENCODINGS)
    _SUPPORTED_PROXY_SCHEMES = ('http', 'socks4', 'socks4a', 'socks5', 'socks5h')
    if urllib3_version >= (1, 26, 0):
        _SUPPORTED_PROXY_SCHEMES = (*_SUPPORTED_PROXY_SCHEMES, 'https')
    _SUPPORTED_FEATURES = (Features.NO_PROXY, Features.ALL_PROXY)
    RH_NAME = 'requests'

    DEFAULT_POOLSIZE = requests.adapters.DEFAULT_POOLSIZE

    def __init__(self, max_conn_pools: int = None, conn_pool_maxsize: int = None, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.max_conn_pools = max_conn_pools or self.DEFAULT_POOLSIZE  # default from urllib3
        self.conn_pool_maxsize = conn_pool_maxsize or self.DEFAULT_POOLSIZE

        # Forward urllib3 debug messages to our logger
        logger = logging.getLogger('urllib3')
        handler = Urllib3LoggingHandler(logger=self._logger)
        handler.setFormatter(logging.Formatter('requests: %(message)s'))
        handler.addFilter(Urllib3LoggingFilter())
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)

        if self.verbose:
            # Setting this globally is not ideal, but is easier than hacking with urllib3.
            # It could technically be problematic for scripts embedding yt-dlp.
            # However, it is unlikely debug traffic is used in that context in a way this will cause problems.
            HTTPConnection.debuglevel = 1
            logger.setLevel(logging.DEBUG)
        # this is expected if we are using --no-check-certificate
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def close(self):
        self._clear_instances()

    def _create_instance(self, cookiejar):
        session = RequestsSession()
        _http_adapter = RequestsHTTPAdapter(
            ssl_context=self._make_sslcontext(),
            source_address=self.source_address,
            max_retries=urllib3.util.retry.Retry(False),
            pool_maxsize=self.conn_pool_maxsize,
            pool_connections=self.max_conn_pools,
            pool_block=False,
        )
        session.adapters.clear()
        session.headers = requests.models.CaseInsensitiveDict({'Connection': 'keep-alive'})
        session.mount('https://', _http_adapter)
        session.mount('http://', _http_adapter)
        session.cookies = cookiejar
        session.trust_env = False  # no need, we already load proxies from env
        return session

    def _send(self, request):

        headers = self._merge_headers(request.headers)
        add_accept_encoding_header(headers, SUPPORTED_ENCODINGS)

        max_redirects_exceeded = False

        session = self._get_instance(
            cookiejar=request.extensions.get('cookiejar') or self.cookiejar
        )

        try:
            res = session.request(
                method=request.method,
                url=request.url,
                data=request.data,
                headers=headers,
                timeout=float(request.extensions.get('timeout') or self.timeout),
                proxies=request.proxies or self.proxies,
                allow_redirects=True,  # TODO(future): add extension to enable/disable redirects
                stream=True
            )

        except requests.exceptions.TooManyRedirects as e:
            max_redirects_exceeded = True
            res = e.response
        except requests.exceptions.SSLError as e:
            if 'CERTIFICATE_VERIFY_FAILED' in str(e):
                raise CertificateVerifyError(cause=e) from e
            raise SSLError(cause=e) from e
        except requests.exceptions.ProxyError as e:
            raise ProxyError(cause=e) from e
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            # Some urllib3 exceptions such as IncompleteRead are wrapped by ConnectionError on request
            handle_urllib3_read_exceptions(find_original_error(e, (urllib3.exceptions.HTTPError,)))
            raise TransportError(cause=e) from e
        except urllib3.exceptions.HTTPError as e:
            # Catch any urllib3 exceptions that may leak through
            handle_urllib3_read_exceptions(e)
            raise TransportError(cause=e) from e
        # Any misc Requests exception. May not necessary be network related e.g. InvalidURL
        except requests.exceptions.RequestException as e:
            raise RequestError(cause=e) from e

        requests_res = RequestsResponse(res)

        if not 200 <= requests_res.status < 300:
            raise HTTPError(requests_res, redirect_loop=max_redirects_exceeded)

        return requests_res


# Since we already have a socks proxy implementation,
# we can use that with urllib3 instead of requiring an extra dependency.
class SocksHTTPConnection(urllib3.connection.HTTPConnection):
    def __init__(self, _socks_options, *args, **kwargs):  # must use _socks_options to pass PoolKey checks
        self._proxy_args = _socks_options
        super().__init__(*args, **kwargs)

    def _new_conn(self):
        sock = sockssocket()
        sock.setproxy(**self._proxy_args)
        if isinstance(self.timeout, (int, float)):
            sock.settimeout(self.timeout)
        try:
            sock.connect((self.host, self.port))
        except (socket.timeout, TimeoutError) as e:
            raise urllib3.exceptions.ConnectTimeoutError(self, f'Connection to {self.host} timed out. (connect timeout={self.timeout})') from e
        except SocksProxyError as e:
            raise urllib3.exceptions.ProxyError(str(e), e) from e
        except (OSError, socket.error) as e:
            raise urllib3.exceptions.NewConnectionError(self, f'Failed to establish a new connection: {e}') from e

        return sock


class SocksHTTPSConnection(SocksHTTPConnection, urllib3.connection.HTTPSConnection):
    pass


class SocksHTTPConnectionPool(urllib3.HTTPConnectionPool):
    ConnectionCls = SocksHTTPConnection


class SocksHTTPSConnectionPool(urllib3.HTTPSConnectionPool):
    ConnectionCls = SocksHTTPSConnection


class SocksProxyManager(urllib3.PoolManager):

    def __init__(self, socks_proxy, username=None, password=None, num_pools=10, headers=None, **connection_pool_kw):
        connection_pool_kw['_socks_options'] = make_socks_proxy_opts(socks_proxy)
        super().__init__(num_pools, headers, **connection_pool_kw)
        self.pool_classes_by_scheme = {
            'http': SocksHTTPConnectionPool,
            'https': SocksHTTPSConnectionPool
        }


requests.adapters.SOCKSProxyManager = SocksProxyManager

# XXX: Requests will not automatically handle no_proxy by default due to buggy no_proxy handling with proxy dict [1].
requests.adapters.select_proxy = select_proxy
