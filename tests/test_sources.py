import socket
import ssl
import unittest
from unittest.mock import patch

from researchflow import sources


class Response:
    def __init__(self, body=b"A public research document. " * 8, status=200, headers=None):
        self.status = status
        self.headers = {"Content-Type": "text/plain", **(headers or {})}
        self.body = body

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read(self, limit):
        return self.body[:limit]


class Connection:
    response = Response()
    instances = []

    def __init__(self, host, addresses):
        self.host, self.addresses = host, addresses
        self.closed = False
        self.requests = []
        self.instances.append(self)

    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


class SourceTests(unittest.TestCase):
    def setUp(self):
        Connection.instances = []
        Connection.response = Response()

    def test_url_policy(self):
        for url in ("http://example.com", "https://localhost/", "https://host.local/", "https://u:p@example.com", "https://example.com:444/", "https://example.com/#fragment", "https://example.com/\r\nX:evil", "https://example.com\\@127.0.0.1/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                sources.validate_url(url)
        self.assertEqual(sources.validate_url("https://example.com:443/a?q=1"), "https://example.com/a?q=1")

    def test_rejects_any_private_dns_address(self):
        addresses = PUBLIC + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch.object(socket, "getaddrinfo", return_value=addresses), self.assertRaises(ValueError):
            sources._public_addresses("example.com")

    def test_ipv6_transition_addresses_rejected(self):
        for ip in ("::ffff:8.8.8.8", "2002:0808:0808::1", "::1", "fe80::1"):
            addresses = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, 443, 0, 0))]
            with self.subTest(ip=ip), patch.object(socket, "getaddrinfo", return_value=addresses), self.assertRaises(ValueError):
                sources._public_addresses("example.com")

    def test_fetch_passes_pinned_addresses_and_closes(self):
        with patch.object(socket, "getaddrinfo", return_value=PUBLIC), patch.object(sources, "_PinnedHTTPSConnection", Connection):
            result = sources.fetch_source("https://example.com/research", "S1")
        self.assertEqual(result["error"], "")
        self.assertEqual(result["id"], "S1")
        self.assertGreater(len(result["text"]), 80)
        self.assertEqual(Connection.instances[0].addresses, PUBLIC)
        self.assertTrue(Connection.instances[0].closed)
        headers = Connection.instances[0].requests[0][1]["headers"]
        self.assertNotIn("Cookie", headers)

    def test_redirect_to_private_source_is_rejected(self):
        Connection.response = Response(status=302, headers={"Location": "https://localhost/admin"})
        with patch.object(socket, "getaddrinfo", return_value=PUBLIC), patch.object(sources, "_PinnedHTTPSConnection", Connection):
            result = sources.fetch_source("https://example.com/", "S1")
        self.assertIn("Local sources", result["error"])
        self.assertEqual(len(Connection.instances), 1)

    def test_relative_redirect_is_bounded(self):
        Connection.response = Response(status=302, headers={"Location": "/again"})
        with patch.object(socket, "getaddrinfo", return_value=PUBLIC), patch.object(sources, "_PinnedHTTPSConnection", Connection):
            result = sources.fetch_source("https://example.com/", "S1")
        self.assertIn("redirect limit", result["error"])
        self.assertEqual(len(Connection.instances), sources.MAX_REDIRECTS + 1)
        self.assertTrue(all(c.closed for c in Connection.instances))

    def test_redirect_dns_is_checked_again(self):
        Connection.response = Response(status=302, headers={"Location": "https://other.example/path"})
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]
        with patch.object(socket, "getaddrinfo", side_effect=[PUBLIC, private]), patch.object(sources, "_PinnedHTTPSConnection", Connection):
            result = sources.fetch_source("https://example.com/", "S1")
        self.assertIn("public addresses", result["error"])
        self.assertEqual(len(Connection.instances), 1)

    def test_html_excludes_navigation_and_scripts(self):
        parser = sources._PageText()
        parser.feed("<title>Research &amp; evidence</title><nav>secret navigation<div>nested</div></nav><main><h1>Facts</h1><p>First <b>verified</b> point.</p><script>alert('x')</script><footer>footer</footer><p>Second point.</p></main>")
        title, text = parser.result()
        self.assertEqual(title, "Research & evidence")
        self.assertIn("First verified point.", text)
        self.assertIn("Second point.", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("alert", text)
        self.assertNotIn("footer", text)

    def test_size_and_content_type_limits(self):
        for response, expected in ((Response(body=b"x" * (sources.MAX_BYTES + 1)), "one-megabyte"), (Response(headers={"Content-Type": "application/pdf"}), "HTML page"), (Response(headers={"Content-Encoding": "gzip"}), "Compressed"), (Response(body=b"tiny"), "too little")):
            Connection.response = response
            with self.subTest(expected=expected), patch.object(socket, "getaddrinfo", return_value=PUBLIC), patch.object(sources, "_PinnedHTTPSConnection", Connection):
                result = sources.fetch_source("https://example.com/", "S1")
            self.assertIn(expected, result["error"])
            self.assertEqual(result["text"], "")

    def test_markdown_sources_preserve_quotable_text(self):
        content = b"# Official documentation\n\n" + b"Durable execution preserves completed steps across restarts. " * 3
        Connection.response = Response(body=content, headers={"Content-Type": "text/markdown; charset=utf-8"})
        with patch.object(socket, "getaddrinfo", return_value=PUBLIC), patch.object(sources, "_PinnedHTTPSConnection", Connection):
            result = sources.fetch_source("https://example.com/overview.md", "S1")
        self.assertEqual(result["error"], "")
        self.assertEqual(result["text"], content.decode())

    def test_network_error_is_returned_as_data(self):
        with patch.object(socket, "getaddrinfo", side_effect=socket.gaierror("DNS failed")):
            result = sources.fetch_source("https://example.com/", "S1")
        self.assertEqual(result["error"], "DNS failed")

    def test_pinned_connection_does_not_resolve_again(self):
        raw = unittest.mock.Mock()
        tls = unittest.mock.Mock()
        with patch.object(socket, "socket", return_value=raw), patch.object(socket, "getaddrinfo", side_effect=AssertionError("unexpected re-resolution")):
            connection = sources._PinnedHTTPSConnection("example.com", PUBLIC)
            connection._context = tls
            connection.connect()
        raw.connect.assert_called_once_with(("93.184.216.34", 443))
        tls.wrap_socket.assert_called_once_with(raw, server_hostname="example.com")
        connection.close()

    def test_tls_authenticates_original_hostname(self):
        connection = sources._PinnedHTTPSConnection("example.com", PUBLIC)
        self.assertTrue(connection._context.check_hostname)
        self.assertEqual(connection._context.verify_mode, ssl.CERT_REQUIRED)
        connection.close()

    def test_absolute_deadline_stops_slow_response(self):
        raw = unittest.mock.Mock()
        transport = sources._DeadlineSocket(raw, 10)
        reader = sources._DeadlineReader(transport)
        with patch.object(sources.time, "monotonic", return_value=9):
            reader.readinto(bytearray(1))
        raw.settimeout.assert_called_with(1)
        with patch.object(sources.time, "monotonic", return_value=11), self.assertRaises(TimeoutError):
            reader.readinto(bytearray(1))
        self.assertEqual(raw.recv_into.call_count, 1)

    def test_demo_is_explicitly_labeled(self):
        for url in ("https://docs.langchain.com/oss/python/langgraph/overview", "https://docs.langchain.com/oss/python/langchain/overview"):
            result = sources.fetch_demo(url, "S1")
            self.assertIn("demo fixture", result["title"])
            self.assertIn("not a live page download", result["text"])


if __name__ == "__main__":
    unittest.main()
