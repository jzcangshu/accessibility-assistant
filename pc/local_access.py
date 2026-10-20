"""电脑本机界面的访问边界。"""
import ipaddress
from urllib.parse import urlsplit

from aiohttp import web


def local_only(request):
    try:
        local = ipaddress.ip_address(request.remote or "").is_loopback
        host = request.url.host
        local_host = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = local_host = False
    origin = request.headers.get("Origin")
    source = urlsplit(origin) if origin else None
    if not local or not local_host or (source and (
            source.netloc != request.host or source.scheme != request.scheme)):
        raise web.HTTPForbidden(text="此操作仅限电脑本机使用")
