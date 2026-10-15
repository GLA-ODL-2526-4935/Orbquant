# Alpaca local request restrictions

OrbQuant always blocks `api.alpaca.markets` and its subdomains, irrespective of
scheme, path or port. This includes `https://api.alpaca.markets` and
`wss://api.alpaca.markets/stream`. There is no live-mode configuration switch.

The fixed paper origins are `https://paper-api.alpaca.markets` and
`wss://paper-api.alpaca.markets/stream`. Market data remains separate at
`https://data.alpaca.markets` and `wss://stream.data.alpaca.markets`.
Paper authentication does not confer additional market-data entitlements.

## Enforcement

`src/orbquant_network_guard.py` installs local checks at each HTTPX request hop
(synchronous and asynchronous, including redirects and proxy use), both
WebSocket connection APIs, standard-library HTTP requests/proxy tunnels, TLS
server names, and Python socket hostname audit events. Rejections raise
`LiveTradingBlocked` before dispatching the blocked request to its transport.
Checks canonicalize case, trailing dots, IDNA and percent-encoded hostname text.
Paper and market-data origins remain available. The policy does not log keys.

`orbquant.__init__` installs the policy on package import. An environment configured with `tools/install_guard_startup.py` also loads `zzz_orbquant_network_guard.pth` at normal Python startup,
even for scripts that do not import OrbQuant. If startup cannot load the guard,
the interpreter stops with exit code 78. The packaged distribution includes the
guard module; a newly created environment needs the startup file installed again
to protect scripts that never import OrbQuant. Existing running processes must
restart to pick up the policy; no other workloads were interrupted.

## Boundary

This is an application-level Python safety control, not a Windows firewall or
hostile-code sandbox. Windows does not impose endpoint rules by working folder.
Unrelated executables, `python -S`, a different interpreter that never imports
OrbQuant, deliberately bypassed TLS/transport code, raw IP requests, or a remote
forwarding service are outside this guarantee. Do not use subprocess HTTP tools
or unguarded clients from this repository. An independent OS/network egress
sandbox would be necessary to enforce this against arbitrary executables.
No hosts-file or system firewall changes were made.


## Public adapter

`orbquant.market_data.ReadOnlyAlpaca` exposes only GET account status and stock quotes. It submits no orders. The offline demo performs no network access. See the README for paper-only scope, limitations and live-integration requirements.
