"""URL normalization and site-scope helpers."""
from __future__ import annotations

import posixpath
import re
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

NON_PAGE_EXTENSIONS = {
    # images
    "jpg", "jpeg", "png", "gif", "webp", "avif", "svg", "ico", "bmp", "tif", "tiff",
    # documents / archives
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "zip", "gz", "rar", "7z", "tar", "csv",
    # media
    "mp3", "mp4", "m4a", "mov", "avi", "webm", "wav", "ogg",
    # code / fonts
    "css", "js", "mjs", "json", "xml", "txt", "woff", "woff2", "ttf", "otf", "eot", "map",
}

_DEFAULT_PORTS = {"http": 80, "https": 443}
_HOST = re.compile(r"^[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?(\.[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?)*$")
_SAFE_PATH = "/%:@!$&'()*+,;=-._~"
_SAFE_QUERY = "/?%:@!$&'()*+,;=-._~"


def ensure_scheme(raw: str) -> str:
    raw = raw.strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        raw = "https://" + raw.lstrip("/")
    return raw


def normalize_url(url: str, base: str | None = None) -> str | None:
    """Absolute, canonical-ish form used as the identity of a URL in the Site Model.

    Lowercases scheme and host, drops default ports and fragments, makes the empty path
    "/", resolves dot segments and normalizes percent-encoding. Query strings are kept
    as-is (order matters to some servers). Returns None for non-http(s) URLs.
    """
    if not url:
        return None
    url = url.strip()
    if base:
        try:
            url = urljoin(base, url)
        except ValueError:
            return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    elif not _HOST.match(host):
        return None
    netloc = host if port in (None, _DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    path = parts.path or "/"
    trailing = path.endswith("/")
    path = posixpath.normpath(path)
    if path == ".":
        path = "/"
    if not path.startswith("/"):
        path = "/" + path
    if path.startswith("//"):
        path = "/" + path.lstrip("/")
    if trailing and not path.endswith("/"):
        path += "/"
    path = quote(unquote(path), safe=_SAFE_PATH)
    query = quote(unquote(parts.query), safe=_SAFE_QUERY) if parts.query else ""
    return urlunsplit((scheme, netloc, path, query, ""))


def is_public_style_host(host: str) -> bool:
    """A host a user would type for a website: has a dot, or is an IP literal / localhost."""
    return "." in host or host == "localhost" or host.startswith("[")


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def site_key(host: str) -> str:
    """Rate-limit key: www and non-www count as the same site."""
    return host[4:] if host.startswith("www.") else host


def toggle_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else "www." + host


def origin_of(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def path_extension(url: str) -> str:
    path = urlsplit(url).path
    last = path.rsplit("/", 1)[-1]
    return last.rsplit(".", 1)[-1].lower() if "." in last else ""


def looks_like_page(url: str) -> bool:
    return path_extension(url) not in NON_PAGE_EXTENSIONS


def filename_of(url: str) -> str:
    return unquote(urlsplit(url).path.rsplit("/", 1)[-1])


class SiteScope:
    """Decides which hosts are 'internal' for a crawl."""

    def __init__(self, host: str, include_subdomains: bool = False):
        self.host = host.lower()
        self.hosts = {self.host, toggle_www(self.host)}
        self.root = site_key(self.host)
        self.include_subdomains = include_subdomains

    def is_internal(self, url: str) -> bool:
        h = host_of(url)
        if h in self.hosts:
            return True
        return self.include_subdomains and h.endswith("." + self.root)


def template_key(url: str) -> str:
    """Rough page-template signature from the path shape.

    /blog/my-post -> /blog/*      /products/123/red -> /products/*/*      / -> /
    """
    path = urlsplit(url).path
    segs = [s for s in path.split("/") if s]
    if not segs:
        return "/"
    head = segs[0] if not re.search(r"\d{3,}", segs[0]) else "*"
    return "/" + "/".join([head] + ["*"] * (len(segs) - 1))
