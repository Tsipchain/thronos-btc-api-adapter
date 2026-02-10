# Thronos BTC API Adapter

This repository contains a lightweight service that proxies and aggregates
Bitcoin blockchain data from one or more external providers (such as
Blockstream or Mempool.space) and exposes it under a stable API
namespace for the Thronos ecosystem.  The goal is to make it easy for
other Thronos services (pledge watcher, miner kits, wallets, etc.) to
fetch Bitcoin chain state without tying themselves directly to any
third‑party provider.

## Features

- **Blockstream API compatibility.**  The adapter implements a subset of
  the endpoints exposed by [`blockstream.info`](https://blockstream.info/api).  Clients
  that already speak the Blockstream API can drop in this service by
  changing the base URL.
- **Multiple upstreams.**  The service reads a comma‑separated list of
  upstream API base URLs from the `UPSTREAMS` environment variable.
  When handling a request it will attempt to fetch data from each
  upstream in order until one responds successfully.  This makes the
  service resilient to provider downtime.
- **Simple caching.**  Responses are cached in memory for a
  configurable time‑to‑live (`CACHE_TTL` seconds) to avoid repeatedly
  hitting upstream APIs.  Caching is per endpoint and per argument.
- **Rate limiting.**  A per‑process rate limiter limits the number of
  requests sent to upstream providers.  The maximum requests per
  second is configurable via `RATE_LIMIT_RPS`.
- **Health endpoint.**  A basic `/api/health` endpoint reports the
  service status for monitoring.

## Endpoints

All endpoints are served under the `/api` prefix.  They mirror the
equivalent routes of the Blockstream API:

| Endpoint | Description |
|---------:|:------------|
| `GET /api/health` | Returns `{"status": "ok"}` if the service is running. |
| `GET /api/blocks/tip/height` | Returns the latest Bitcoin block height. |
| `GET /api/block-height/{height}` | Returns the block hash at the given height. |
| `GET /api/block/{block_hash}` | Returns details for a block by its hash. |
| `GET /api/tx/{txid}` | Returns transaction details for a given TXID. |
| `GET /api/address/{address}/utxo` | Returns the list of unspent outputs for the given address. |
| `GET /api/address/{address}/txs` | Returns a list of transactions for the given address. |

### Notes

* The adapter does not implement every endpoint in the Blockstream API.  Only
  the routes above are currently supported.  Additional routes can be
  added as needed.
* When an upstream returns non‑JSON content (for example, a plain
  number representing the block height) the adapter will attempt to
  convert it to an integer.  Otherwise it returns the raw text.
* Error responses from upstreams are ignored; the adapter moves on to
  the next upstream.  If all upstreams fail the service returns HTTP
  502.

## Environment variables

| Variable | Description | Default |
|---------:|:------------|:-------|
| `UPSTREAMS` | Comma‑separated list of upstream API base URLs.  The
  service will query each in order until one returns a successful
  response.  The `/api` prefix should not be included in these URLs. |
| `CACHE_TTL` | Time‑to‑live for cached responses in seconds. | `30` |
| `RATE_LIMIT_RPS` | Maximum number of requests per second to send to upstreams.  0 disables rate limiting. | `5` |
| `HOST` | Network interface to bind. | `0.0.0.0` |
| `PORT` | Port to listen on. | `8000` |

## Running locally

1. Install Python 3.10+ and pip.
2. Clone this repository and install dependencies:

   ```sh
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and edit as needed.  At minimum you should
   set `UPSTREAMS` with one or more Bitcoin API providers.

4. Start the server using Uvicorn:

   ```sh
   uvicorn main:app --host $HOST --port $PORT
   ```

The service will be available at `http://$HOST:$PORT/api`.  Use curl or
your browser to test endpoints such as:

```sh
curl http://localhost:8000/api/blocks/tip/height
```

## Deployment

This service is intended to be deployed on Railway, Render or any
platform that supports Python.  When deploying on Railway you can
configure the environment variables in the dashboard.  A basic
`Dockerfile` is included for containerized deployments.

### Railway example

Environment variables:

```
UPSTREAMS=https://blockstream.info/api,https://mempool.space/api
CACHE_TTL=60
RATE_LIMIT_RPS=10
```

### Render example

Create a new web service on Render, point it to this repository and
set the environment variables above.  Render will automatically build
and run the service using the provided `Dockerfile`.

## Contributing

Contributions are welcome!  Feel free to submit issues or pull
requests for additional endpoints, improved caching or error
handling.

## License

This project is released under the MIT License.  See the `LICENSE`
file for details.