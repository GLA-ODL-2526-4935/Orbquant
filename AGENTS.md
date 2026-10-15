# OrbQuant mandatory Alpaca endpoint safety

Before any Alpaca access, ensure `orbquant_network_guard` is installed and active.
Never disable, bypass or weaken it, including via alternate executables, direct
IP addressing, proxy forwarding, Python `-S`, or alternative transports.

Only `https://paper-api.alpaca.markets` and
`wss://paper-api.alpaca.markets/stream` may be used for Alpaca trading APIs.
Live trading host `api.alpaca.markets` is forbidden for every scheme/path/port.
Keep market-data requests on their separate official data origins.

Use the ignored `alpaca-credentials.toml` through the existing credential loader.
Never print credentials, include them in reports, or commit the private file.
Endpoint access audits are read-only; paper orders also require explicit user
authorization. Preserve deterministic risk and accounting. See
`docs/ALPACA_LOCAL_SAFETY.md` for enforcement and its application-level boundary.
