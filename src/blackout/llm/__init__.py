"""Safe, cached, budgeted access to Claude (docs/PRD.md section 8.8, milestone M10).

- ``config``: models, effort and the dated price table (configs/models.yaml).
- ``accounting``: token usage and its cost.
- ``schemas``: strict JSON schemas for structured output and strict tools.
- ``cache``: the SQLite response cache.
- ``budget``: the spend ledger and the budget guard (configs/budget.yaml, runs/ledger.json).
- ``client``: ``LLMClient``, the one way the project calls the Messages API.
- ``fixtures``: recorded responses, so tests and offline runs never touch the network.
- ``loop``: the manual, append-only tool loop.
- ``batches``: the Message Batches helper (precomputes into the cache).
- ``live``: the live-mode guard and API key lookup.
"""
