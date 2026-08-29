#!/usr/bin/env python3
"""Forward a local TCP port to a tailnet host through tailscaled's SOCKS5 proxy.

In a Claude cloud session, tailscaled runs in userspace-networking mode
(scripts/tailscale_up.sh), so tailnet peers are reachable only via its SOCKS5
listener — and ib_async has no SOCKS support. This script bridges the gap:

    python examples/tws_tunnel.py DESKTOP-ABC123 &
    python examples/tws_status.py        # works unchanged

It listens on 127.0.0.1:7496 and forwards each connection through SOCKS5
(127.0.0.1:1055) to DESKTOP-ABC123:7496 over the tailnet. Every script in
this repo then works as-is, because IBKRClient defaults to 127.0.0.1:7496
(ibkr/client.py). The host may be a MagicDNS machine name or a 100.x.y.z
tailnet IP; names are resolved inside tailscaled.

--paper switches both ends to 7497 (TWS paper port). Stdlib only — safe to
run anywhere, no broker or SOCKS packages needed. See docs/tailscale.md.
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import threading

SOCKS_REPLY = {
    0x00: "succeeded",
    0x01: "general SOCKS server failure",
    0x02: "connection not allowed by ruleset",
    0x03: "network unreachable",
    0x04: "host unreachable (machine offline or name unknown to the tailnet?)",
    0x05: "connection refused (nothing listening — is TWS open and the "
          "tailscale serve/portproxy forward configured on the Windows side?)",
    0x06: "TTL expired",
    0x07: "command not supported",
    0x08: "address type not supported",
}


def socks5_connect(socks_host: str, socks_port: int, dest_host: str, dest_port: int,
                   timeout: float = 15.0) -> socket.socket:
    """Open a TCP connection to dest via a SOCKS5 proxy (no auth, RFC 1928).

    The destination is always sent as a domain name (ATYP=0x03) so that
    tailscaled resolves MagicDNS machine names itself.
    """
    s = socket.create_connection((socks_host, socks_port), timeout=timeout)
    try:
        s.sendall(b"\x05\x01\x00")  # ver 5, 1 method, no-auth
        if _recv_exact(s, 2) != b"\x05\x00":
            raise ConnectionError("SOCKS5 handshake refused")
        host_b = dest_host.encode("idna")
        s.sendall(b"\x05\x01\x00\x03" + bytes([len(host_b)]) + host_b
                  + struct.pack(">H", dest_port))
        ver, rep, _rsv, atyp = _recv_exact(s, 4)
        if rep != 0x00:
            raise ConnectionError(f"SOCKS5 connect failed: "
                                  f"{SOCKS_REPLY.get(rep, f'code {rep}')}")
        # Drain the bound-address field so payload bytes line up.
        if atyp == 0x01:
            _recv_exact(s, 4 + 2)
        elif atyp == 0x04:
            _recv_exact(s, 16 + 2)
        elif atyp == 0x03:
            (alen,) = _recv_exact(s, 1)
            _recv_exact(s, alen + 2)
        s.settimeout(None)
        return s
    except Exception:
        s.close()
        raise


def _recv_exact(s: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("SOCKS5 proxy closed the connection")
        buf += chunk
    return buf


def _pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for sock, how in ((dst, socket.SHUT_WR), (src, socket.SHUT_RD)):
            try:
                sock.shutdown(how)
            except OSError:
                pass


def _serve_one(client: socket.socket, peer: str, args: argparse.Namespace) -> None:
    try:
        upstream = socks5_connect(args.socks_host, args.socks_port, args.host, args.port)
    except Exception as exc:
        print(f"[tunnel] {peer}: cannot reach {args.host}:{args.port} — {exc}")
        client.close()
        return
    print(f"[tunnel] {peer} -> {args.host}:{args.port} connected")
    t = threading.Thread(target=_pump, args=(client, upstream), daemon=True)
    t.start()
    _pump(upstream, client)
    t.join()
    client.close()
    upstream.close()
    print(f"[tunnel] {peer} closed")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("host", help="tailnet name or 100.x IP of the machine running TWS")
    ap.add_argument("--port", type=int, default=7496, help="remote TWS port (default 7496 live)")
    ap.add_argument("--listen", type=int, default=None,
                    help="local listen port (default: same as --port)")
    ap.add_argument("--paper", action="store_true", help="shortcut: use 7497 on both ends")
    ap.add_argument("--socks", default="127.0.0.1:1055",
                    help="tailscaled SOCKS5 address (default 127.0.0.1:1055)")
    args = ap.parse_args()
    if args.paper:
        args.port = 7497
    if args.listen is None:
        args.listen = args.port
    args.socks_host, socks_port = args.socks.rsplit(":", 1)
    args.socks_port = int(socks_port)

    # Fail fast if tailscaled's SOCKS listener isn't there at all.
    try:
        socket.create_connection((args.socks_host, args.socks_port), timeout=3).close()
    except OSError:
        print(f"tailscaled SOCKS5 proxy not listening at {args.socks} — "
              f"run scripts/tailscale_up.sh first.", file=sys.stderr)
        return 2

    # One test dial so reachability problems surface immediately, not on the
    # first ib_async connect. Not fatal: TWS may simply not be open yet.
    try:
        socks5_connect(args.socks_host, args.socks_port, args.host, args.port).close()
        print(f"[tunnel] reachability check OK: {args.host}:{args.port} answers over the tailnet")
    except Exception as exc:
        print(f"[tunnel] WARNING: {args.host}:{args.port} not reachable yet — {exc}")

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", args.listen))
    srv.listen(8)
    print(f"[tunnel] listening on 127.0.0.1:{args.listen} -> {args.host}:{args.port} "
          f"via SOCKS5 {args.socks} (Ctrl-C to stop)")
    try:
        while True:
            client, addr = srv.accept()
            threading.Thread(target=_serve_one, args=(client, f"{addr[0]}:{addr[1]}", args),
                             daemon=True).start()
    except KeyboardInterrupt:
        print("\n[tunnel] stopped")
    finally:
        srv.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
