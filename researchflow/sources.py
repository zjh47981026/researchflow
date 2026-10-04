"""Bounded public-HTTPS source collection with DNS addresses pinned per request."""
from __future__ import annotations

import http.client
import io
import ipaddress
import socket
import ssl
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

MAX_BYTES = 1_000_000
MAX_TEXT = 24_000
TIMEOUT = 12
MAX_REDIRECTS = 3


def validate_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 33 for c in url):
        raise ValueError("Use a public HTTPS URL without whitespace.")
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid URL port.") from exc
    if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("Only public HTTPS URLs without credentials are allowed.")
    if port not in (None, 443) or parsed.fragment or "\\" in url:
        raise ValueError("Use HTTPS port 443 and remove URL fragments.")
    host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("Local sources are not allowed.")
    host_part = f"[{host}]" if ":" in host else host
    return urlunsplit(("https", host_part, parsed.path or "/", parsed.query, ""))


def _public_addresses(host: str) -> list[tuple]:
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError("Source hostname has no addresses.")
    for _, _, _, _, address in addresses:
        ip = ipaddress.ip_address(address[0])
        if not ip.is_global or ip.is_multicast or ip.is_unspecified or ip.is_loopback or ip.is_reserved:
            raise ValueError("Source must resolve exclusively to public addresses.")
        if isinstance(ip, ipaddress.IPv6Address) and (ip.ipv4_mapped or ip.sixtofour or ip.teredo):
            raise ValueError("IPv6 transition addresses are not accepted.")
    return addresses


class _DeadlineReader(io.RawIOBase):
    def __init__(self, transport):
        super().__init__()
        self.transport = transport

    def readable(self):
        return True

    def readinto(self, buffer):
        self.transport.prepare()
        return self.transport.socket.recv_into(buffer)


class _DeadlineSocket:
    """Apply the same absolute deadline to every header/body socket read."""
    def __init__(self, sock, deadline):
        self.socket, self.deadline = sock, deadline

    def prepare(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Source exceeded the request time limit.")
        self.socket.settimeout(remaining)

    def sendall(self, data):
        self.prepare()
        return self.socket.sendall(data)

    def makefile(self, mode):
        if mode != "rb":
            raise ValueError("Only response reads are supported.")
        return io.BufferedReader(_DeadlineReader(self))

    def close(self):
        self.socket.close()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, addresses: list[tuple]):
        # certifi is present through model dependencies; system trust remains a fallback.
        try:
            import certifi
            context = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            context = ssl.create_default_context()
        super().__init__(host, port=443, timeout=TIMEOUT, context=context)
        self._addresses = addresses
        self._deadline = time.monotonic() + TIMEOUT

    def connect(self) -> None:
        last_error = None
        # Never re-resolve the hostname between policy validation and connection.
        for family, socktype, proto, _, address in self._addresses[:4]:
            raw = socket.socket(family, socktype, proto)
            try:
                remaining = self._deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Source exceeded the request time limit.")
                raw.settimeout(remaining)
                raw.connect(address)
                raw.settimeout(max(0.001, self._deadline - time.monotonic()))
                secured = self._context.wrap_socket(raw, server_hostname=self.host)
                self.sock = _DeadlineSocket(secured, self._deadline)
                return
            except OSError as exc:
                last_error = exc
                raw.close()
        raise OSError("Could not connect to the public source.") from last_error


class _PageText(HTMLParser):
    SKIP = {"script", "style", "nav", "footer", "aside", "form", "button", "svg", "noscript", "template"}
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    BLOCK = {"p", "div", "section", "article", "main", "li", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "br", "tr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skipped: list[str] = []
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if self.skipped:
            if tag not in self.VOID:
                self.skipped.append(tag)
            return
        if tag in self.SKIP:
            self.skipped.append(tag)
        elif tag == "title":
            self.in_title = True
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag == "br" and not self.skipped:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.skipped:
            if tag in self.skipped:
                index = len(self.skipped) - 1 - self.skipped[::-1].index(tag)
                del self.skipped[index:]
            return
        if tag == "title":
            self.in_title = False
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.skipped:
            return
        if self.in_title:
            self.title_parts.append(data)
        else:
            self.parts.append(data)

    def result(self) -> tuple[str, str]:
        title = " ".join("".join(self.title_parts).split())[:200]
        lines = [" ".join(line.split()) for line in "".join(self.parts).splitlines()]
        return title, "\n".join(line for line in lines if line)[:MAX_TEXT]


def _read_url(url: str) -> tuple[str, str, str]:
    current = validate_url(url)
    for step in range(MAX_REDIRECTS + 1):
        parsed = urlsplit(current)
        connection = _PinnedHTTPSConnection(parsed.hostname, _public_addresses(parsed.hostname))
        try:
            path = parsed.path + ("?" + parsed.query if parsed.query else "")
            connection.request("GET", path, headers={"User-Agent": "ResearchFlow/1.0 (public-source research)", "Accept": "text/html,text/plain,text/markdown", "Accept-Encoding": "identity"})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location or step == MAX_REDIRECTS:
                    raise ValueError("Source exceeded the redirect limit.")
                current = validate_url(urljoin(current, location))
                continue
            if response.status != 200:
                raise ValueError(f"Source returned HTTP {response.status}.")
            content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type not in ("text/html", "text/plain", "text/markdown", "application/xhtml+xml"):
                raise ValueError("Source must be an HTML page, plain text, or Markdown.")
            if response.getheader("Content-Encoding", "identity").lower() not in ("identity", ""):
                raise ValueError("Compressed responses are not accepted.")
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ValueError("Source exceeds the one-megabyte limit.")
            decoded = body.decode("utf-8", errors="replace")
            if content_type in ("text/plain", "text/markdown"):
                title, text = parsed.hostname, decoded[:MAX_TEXT]
            else:
                parser = _PageText()
                parser.feed(decoded)
                title, text = parser.result()
            if len(text.strip()) < 80:
                raise ValueError("Source contains too little readable text.")
            return current, title or parsed.hostname, text
        finally:
            connection.close()
    raise ValueError("Source exceeded the redirect limit.")


def fetch_source(url: str, source_id: str) -> dict[str, str]:
    """Return source text or a bounded public error, without propagating fetch exceptions."""
    result = {"id": str(source_id), "url": str(url), "title": "", "text": "", "error": ""}
    try:
        result["url"], result["title"], result["text"] = _read_url(url)
    except (OSError, ValueError, http.client.HTTPException, UnicodeError) as exc:
        result["error"] = str(exc)[:240] or "Source could not be collected."
    return result


_DEMO = {
    "graph": ("LangGraph overview — demo fixture", "LangGraph is a low-level orchestration framework and runtime for building, managing, and deploying long-running, stateful agents. Durable execution allows workflows to resume after failures. Human-in-the-loop workflows can pause execution to inspect and approve actions. Persistence stores graph state as checkpoints, organized into threads. A graph can use conditional edges to route execution based on the current state. LangGraph can be used without LangChain. This is a bundled demonstration excerpt, not a live page download."),
    "chain": ("LangChain overview — demo fixture", "LangChain is a framework for developing applications powered by language models. It provides integrations for models, tools, and retrieval. LangChain agents are built on top of LangGraph to provide durable execution and human-in-the-loop support. Standard model interfaces make it easier to swap model providers. LangChain supports building retrieval-augmented generation applications. This is a bundled demonstration excerpt, not a live page download."),
}


def fetch_demo(url: str, source_id: str) -> dict[str, str]:
    """Offline fixtures make workflow behavior inspectable without model or network access."""
    key = "graph" if "langgraph" in url.lower() else "chain"
    title, text = _DEMO[key]
    return {"id": str(source_id), "url": str(url), "title": title, "text": text, "error": ""}
