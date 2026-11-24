# Psiphon

AutoZCoder's generator can send signups through Psiphon. It starts Psiphon's own client (`psiphon-tunnel-core`) once for each region you choose,
each on its own local SOCKS port, and uses the next ready one for each signup (zcode_gateway/psiphon.py).

## Files

| File | What it is |
|---|---|
| `psiphon-tunnel-core-windows-i686.exe` | Psiphon's client for Windows (32-bit build, runs on 64-bit Windows) |
| `psiphon-tunnel-core-linux-amd64` | Psiphon's client for Linux x86-64 |
| `psiphon.json` | The client settings Psiphon needs and nothing more: platform, client version, propagation channel and sponsor IDs, and the public key that checks server entries are signed by Psiphon |
| `server-list.dat` | The starting list of Psiphon servers (signed entries). Servers hand out more during each connection's handshake. |
| `LICENSE` | GNU GPL v3, the licence of `psiphon-tunnel-core` |
| `sha256.txt` | SHA-256 of every file above |

No remote server-list URLs or download keys are included: AutoZCoder does not download server lists. "Update nodes" runs a fresh connection,
which brings new servers in its handshake.

## Where the programs come from

Both programs are Psiphon Inc.'s official builds from
[Psiphon-Labs/psiphon-tunnel-core-binaries](https://github.com/Psiphon-Labs/psiphon-tunnel-core-binaries), commit
`c49818e8b2fe7b64657aea27b53f12d1374d21eb` (2026-11-10), files `windows/psiphon-tunnel-core-i686.exe` and
`linux/psiphon-tunnel-core-x86_64`, unchanged. Their source code is at
[Psiphon-Labs/psiphon-tunnel-core](https://github.com/Psiphon-Labs/psiphon-tunnel-core) under GPL v3. AutoZCoder runs them as separate
programs; it does not link to them.

There is no official Linux arm64 build, so Psiphon is not offered on Linux arm64.
