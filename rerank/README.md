# DevBrain local reranker

A cross-encoder that rescores deep_search's top 30 candidates. It runs on the
DevBrain host only: it binds 127.0.0.1, refuses other bind addresses, and runs
with `HF_HUB_OFFLINE=1`. The MCP client also refuses non-loopback URLs. No
query or memory text leaves the machine, which matters because BrightBrain
memories may contain PHI.

- **Model:** `Alibaba-NLP/gte-reranker-modernbert-base` (Apache-2.0, 149M
  parameters), pinned to the audited revision in `install.sh`.
- **Device:** Apple-silicon GPU (MPS).
- **Port:** **18600** (`DEVBRAIN_RERANK_PORT`), in the Nooma-Stack API range
  18000–18999. It was free on both Studios on 2026-10-17. Register it in the
  port registry before reusing the range.
- **Latency:** about 350 ms median and about 405 ms p95 for 30 candidates on an
  M3 Ultra.

## Install / update / remove

```bash
rerank/install.sh              # venv (rerank/.venv), model download, launchd agent, health check
rerank/install.sh --uninstall  # unload + remove the agent (venv and model cache stay)
curl -s http://127.0.0.1:18600/health
```

The agent is `com.devbrain.rerank`. Logs go to `logs/rerank.err.log`; only
request lines are logged, never bodies.

Re-run `install.sh` after a `git pull` that touches `rerank/`.

## API

- `POST /rerank` with `{"query": "...", "documents": ["...", ...]}` returns
  `{"scores": [...], "model": "...", "ms": ...}`.
- Scores are relevance probabilities in the range 0 to 1, in the order of
  `documents`.
- At most 200 documents per request. Each document is cut to 4,000
  characters; deep_search sends at most 2,000.

## deep_search side

`search.rerank*` in `config/devbrain.yaml` (see `devbrain.yaml.example`).

- `DEVBRAIN_RERANK=off` turns it off.
- If the service is down or slow, searches keep the un-reranked order:
  - A refused connection pauses reranking for 30 s.
  - A timeout skips only that search.
