# Surfshark (WireGuard) client

AutoZCoder's Surfshark IP source runs **wireproxy** v1.1.3, unmodified, once per chosen location. wireproxy is a WireGuard client that keeps the
tunnel inside its own process and offers it as a local SOCKS5 port on 127.0.0.1: it adds no network adapter and changes no route, DNS or
firewall rule, so only the generator's own requests use it.

- Source: https://github.com/pufferffish/wireproxy (release v1.1.3, published 2026-07-16)
- Licence: ISC (see LICENSE)
- Files: `wireproxy-windows-amd64.exe`, `wireproxy-linux-amd64`, `wireproxy-linux-arm64`, each the `wireproxy` binary from that release's
  `wireproxy_<os>_<arch>.tar.gz`, checked against the release's `checksums.txt`. `sha256.txt` lists the files as shipped.

AutoZCoder writes each location's WireGuard settings (the account's key pair from Surfshark's manual setup, and the location's server and key
from Surfshark's public server list) to a file in its own data folder only while the client starts, and deletes it once the tunnel is up.
