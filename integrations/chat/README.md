# hypnos for normal chats

An **OpenAI-compatible proxy** gives memory to any chat app or SDK that lets you set a base URL. That includes Open WebUI, LibreChat, Chatbox, Continue, the OpenAI and LiteLLM SDKs, and scripts.

```bash
hypnos chat-proxy --upstream https://openrouter.ai/api/v1 --space chat-default     # or http://localhost:11434/v1 for Ollama
```

Point your chat app at `http://127.0.0.1:7440/v1`, keeping your usual API key. Each request goes through five steps:

1. The user's latest message is **redacted and logged**.
2. **G1** asks whether it needs memory. If yes, **v0.8 recall** (System One-routed: the smallest context that answers) runs, and the result is added as a system message.
3. The request is forwarded **unchanged otherwise**. Streaming passes straight through.
4. The reply is **logged**.
5. The service **sleeps** after 10 idle minutes, turning chats into verified facts: preferences, personal facts, projects, plans.

The service stops about 90 seconds after nothing is using it, and the proxy starts it again with the next request; whatever was not sorted yet is done then. To keep it running, set `stay_running = true` in `~/.hypnos/config.toml`.

Send an `X-Session-Id` header to keep separate conversations apart.

- **Trust order (R11):** in the `chat` profile, what the user says is stored as `source: user`, which outranks anything the model inferred.
- **Forget:** `hypnos forget "..." --hard --space chat-default` removes the facts **and** deletes the original text (a privacy tombstone).
- **Chat apps without a base-URL setting,** like claude.ai or the ChatGPT app, can't be proxied. Use the MCP server there, if the app supports MCP connectors.
