from __future__ import annotations

from ._urllib import UrllibRH
from ._websocket import WebSocketsRequestHandler
from .common import (
    HEADRequest,
    PUTRequest,
    Request,
    RequestDirector,
    RequestHandler,
)

REQUEST_HANDLERS = [UrllibRH, WebSocketsRequestHandler]

__all__ = ['UrllibRH', 'REQUEST_HANDLERS', 'Request', 'HEADRequest', 'PUTRequest', 'RequestDirector', 'RequestHandler']
