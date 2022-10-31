import re

from .common import InfoExtractor
from ..utils import classproperty


class KnownUnsupportedBaseIE(InfoExtractor):
    IE_DESC = False  # Do not list
    UNSUPPORTED_SITES = ()
    TEMPLATE = None

    @classproperty
    def _VALID_URL(cls):
        return r'https?://(%s)' % '|'.join(cls.UNSUPPORTED_SITES)

    def _real_extract(self, url):
        self.report_warning(self.TEMPLATE)
        return self.url_result(url, 'Generic')


class KnownDRMIE(KnownUnsupportedBaseIE):
    IE_NAME = 'unsupported:drm'
    UNSUPPORTED_SITES = (
        'play.hbomax.com',
        r'(?:www\.)?tvnow\.(?:de|at|ch)'
    )
    TEMPLATE = (
        'The requested site is known to use DRM protection. '
        'It will NOT be supported by yt-dlp, and will most likely fail to download. '
        'Please DO NOT open an issue, unless you have evidence that the video is not DRM protected'
    )


