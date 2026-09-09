# TRD PULSE v11 — OpenAI Structured Outputs

This patch changes only `news_engine.py` and `pulse_engine.py`.

- Uses Responses API `text.format` with `json_schema` and `strict: true`.
- Keeps NEWS web search.
- Adds explicit handling for refusal / empty / incomplete output.
- Sets `store: false` for these Responses API calls.
- Removes manual JSON-format instructions and code-fence stripping.
- Does not change bot.py, SQLite, GitHub Actions, or market scoring.

Copy the two files from `news_patch/` into the project root, replacing the existing files.
Then run the existing unit-test workflow.
