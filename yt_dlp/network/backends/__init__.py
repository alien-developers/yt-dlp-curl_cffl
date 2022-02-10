from ._urllib import UrllibHandler
from ..common import YDLBackendHandler
from ...exceptions import RequestError


class UnsupportedBackendHandler(YDLBackendHandler):
    def can_handle(self, request, **req_kwargs):
        raise RequestError('This request is not supported')


__all__ = ['UrllibHandler', 'UnsupportedBackendHandler']