"""
TLS configuration contract tests.

Financial services deployments require encryption in transit for all
client-facing traffic — including traffic on private internal networks.
Without TLS, POST /shorten request bodies (which contain destination URLs
that may carry OAuth codes, session tokens, or sensitive query parameters)
travel in plaintext and are visible to any process on the host with
network access.

This is not a simplification: it is an unmitigated security risk that
requires explicit security sign-off before any internal deployment.

These tests enforce that the Nginx layer in the Compose stack addresses
the gap by:
  1. Terminating TLS on port 443 with a self-signed certificate.
  2. Issuing a permanent HTTP → HTTPS redirect on port 80 so no plaintext
     traffic ever reaches the application tier.
  3. Restricting the TLS version to TLSv1.2+ (TLSv1.0 and TLSv1.1 are
     deprecated by RFC 8996 and prohibited by PCI-DSS 3.2+).
  4. Protecting the private key with file-permission 600.

Self-signed certs eliminate plaintext on the wire. Certificate management
(CA-signed cert, ACM / Let's Encrypt rotation) is the remaining step
before a production deployment.
"""
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _nginx_conf() -> str:
    return (ROOT / "nginx" / "nginx.conf").read_text()


def _compose() -> str:
    return (ROOT / "docker-compose.yaml").read_text()


def _nginx_dockerfile() -> str:
    return (ROOT / "nginx" / "Dockerfile").read_text()


# ── nginx.conf ────────────────────────────────────────────────────────────────

class TestNginxTLS:
    """nginx.conf must terminate TLS and redirect HTTP → HTTPS."""

    def test_listens_on_443_ssl(self):
        assert "listen 443 ssl" in _nginx_conf()

    def test_ssl_certificate_configured(self):
        assert "ssl_certificate " in _nginx_conf()

    def test_ssl_certificate_key_configured(self):
        assert "ssl_certificate_key " in _nginx_conf()

    def test_modern_tls_protocols_only(self):
        """ssl_protocols directive must not list TLSv1.0 or TLSv1.1 (RFC 8996 / PCI-DSS 3.2+)."""
        conf = _nginx_conf()
        proto_line = next(
            (ln.strip() for ln in conf.splitlines()
             if ln.strip().startswith("ssl_protocols")),
            None,
        )
        assert proto_line is not None, "ssl_protocols directive missing from nginx.conf"
        assert "TLSv1.0" not in proto_line
        assert "TLSv1.1" not in proto_line
        assert "TLSv1.2" in proto_line

    def test_http_redirects_to_https(self):
        """Port 80 must issue a permanent redirect to HTTPS, not proxy traffic."""
        conf = _nginx_conf()
        assert "listen 80" in conf
        assert "return 301 https://" in conf

    def test_no_proxy_pass_on_port_80(self):
        """proxy_pass must not appear in the port-80 server block."""
        conf = _nginx_conf()
        http_block = conf.split("listen 443")[0]
        assert "proxy_pass" not in http_block


# ── docker-compose.yaml ───────────────────────────────────────────────────────

class TestComposeExposesHttps:
    """docker-compose.yaml must publish port 443 alongside port 80."""

    def test_port_443_exposed(self):
        assert "443:443" in _compose()

    def test_port_80_still_exposed(self):
        """Port 80 must remain open to serve the HTTP → HTTPS redirect."""
        assert "80:80" in _compose()

    def test_nginx_built_not_pulled(self):
        """Nginx must be built from nginx/Dockerfile, not pulled as a plain image."""
        compose = _compose()
        # The custom build context replaces the bare `image: nginx:...` line.
        assert "build: ./nginx" in compose or "build:\n      context: ./nginx" in compose


# ── nginx/Dockerfile ──────────────────────────────────────────────────────────

class TestNginxDockerfile:
    """nginx/Dockerfile must generate a self-signed TLS certificate at build time."""

    def test_dockerfile_exists(self):
        assert (ROOT / "nginx" / "Dockerfile").exists(), \
            "nginx/Dockerfile missing — needed to generate the self-signed cert"

    def test_installs_openssl(self):
        assert "openssl" in _nginx_dockerfile()

    def test_generates_certificate(self):
        df = _nginx_dockerfile()
        assert ".crt" in df

    def test_generates_private_key(self):
        df = _nginx_dockerfile()
        assert ".key" in df

    def test_restricts_key_permissions(self):
        """Private key file must not be world-readable (chmod 600)."""
        df = _nginx_dockerfile()
        assert "chmod 600" in df or "chmod 400" in df
