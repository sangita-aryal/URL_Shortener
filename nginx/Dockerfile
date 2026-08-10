FROM nginx:1.27-alpine

# Generate a self-signed TLS certificate at build time.
#
# This eliminates plaintext traffic on the wire for all client-facing
# connections. Self-signed means browsers will warn on first visit;
# the remaining production step is replacing this cert with a CA-signed
# certificate (Let's Encrypt / ACM) and adding OCSP stapling.
#
# Key details:
#   - RSA 2048-bit (meets NIST SP 800-131A and PCI-DSS minimums)
#   - 10-year validity — appropriate for a dev/internal prototype
#   - CN=localhost covers local Compose usage; add SANs for named hosts
#   - chmod 600 on the private key: readable only by the nginx process (root)
RUN apk add --no-cache openssl \
 && mkdir -p /etc/nginx/ssl \
 && openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
      -keyout /etc/nginx/ssl/server.key \
      -out    /etc/nginx/ssl/server.crt \
      -subj   "/C=US/O=URL Shortener/CN=localhost" \
 && chmod 600 /etc/nginx/ssl/server.key
