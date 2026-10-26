# Security and privacy

hypnos stores what your agents see. Treat its database like the data it contains.

- **Local only:** memory lives in SQLite files under `~/.hypnos/spaces/`. The service listens on `127.0.0.1` only.
- **Redaction before storage (R1):** API keys, tokens, private keys, JWTs, passwords in `key=value` form, and credentials in URLs are removed before anything is logged. The redactor catches common patterns, not everything, so don't pipe secret-heavy output through it on purpose.
- **Real deletion:** `hypnos forget "..." --hard` replaces the original log text with a tombstone, and flags every fact that cited it.
- **What leaves the machine:**
  - During sleep, redacted log text goes to the writer LLM you configure.
  - Short questions and evidence go to the System One model: to TypeSafe if you chose Jev; Laya runs on your machine; with no System One model, the writer LLM answers them.
  - Choose a local writer (Ollama through the `openai` writer) and Laya (or no System One model) to keep everything on the machine.
- **Free-tier writers:** OpenCode's free models may use your prompts and completions for training (check each model's terms in opencode). Don't send sensitive data to them; pick a paid or local writer for that.

Please report security issues privately to the repository owner.
