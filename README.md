# TRD PULSE — market breadth v12 patch

## Fix
`market_engine.py` previously calculated 1h change only from local history. On the first monitoring runs there is no one-hour-old sample yet, so breadth became `0% / 0%` even though CoinGecko already returned 1h changes.

This patch keeps the local history calculation when a full hour is available, but falls back to CoinGecko's `price_change_percentage_1h_in_currency` (or `price_change_percentage_1h`) when history is not ready.

## Replace
Replace only the project's root `market_engine.py` with the file from this patch.

Do not add the patch folder to the repository.

## Verification
- `python -m unittest discover -s tests -v` — 3 new breadth tests pass.
- `python -m py_compile market_engine.py` — passes.
