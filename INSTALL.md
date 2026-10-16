# Installing Hypnos

Hypnos is a human-like memory for AI agents. It writes down everything during a session, sorts it into checked facts while it "sleeps", and gives each new session the memories it needs, with the evidence. It runs only while one of your agents is open, with no windows, and switches itself off about a minute and a half after the last one closes. For servers or robots, set `stay_running = true` in `~/.hypnos/config.toml` to keep it on.

## One line

**macOS / Linux**
```sh
curl -fsSL https://agent-memory-architecture-chi.vercel.app/install.sh | sh
```

**Windows** (PowerShell, Windows 8.1 / 10 / 11)
```powershell
irm https://agent-memory-architecture-chi.vercel.app/install.ps1 | iex
```

It installs everything it needs (uv, and Python 3.12 through uv), connects every coding agent it finds (Claude Code, opencode, Codex, Gemini CLI, Cursor, Windsurf), asks which System One model to use, and starts the memory in the background. Restart your agent afterwards. `/memory-cleanup` lists every saved fact, numbered under its topic, and deletes the ones you pick.

Uninstall: `hypnos setup --uninstall`, then `uv tool uninstall hypnos`. Your saved memory stays in `~/.hypnos` until you delete that folder.

## System One model (recommended)

Hypnos works best with a **System One model**: a model that answers yes/no and pick-one questions in one fast step instead of writing text. Memory uses it to decide what to keep and what to recall, for a tiny fraction of what an LLM costs. You save a lot of money and tokens.

| Option | What it is |
|---|---|
| **Jev** (TypeSafe) | hosted API; needs a key from typesafe.ai |
| **Laya** | open weights, free, runs on your machine ([laya-ai.com](https://laya-ai.com)); about 1-3 GB to download. Running Laya locally next to your coding agent, terminal agent, automation or robot makes memory cost nothing extra. |
| **None** | the memory model you choose (below) also does these checks while saving. Answers can be a little more accurate, but memory uses more tokens. |

## Memory model

Separately, memory needs an LLM to write facts and summaries while it sleeps. Nothing is fixed to one model: setup lists every model your machine can use (free opencode models, a local Ollama, Claude through Claude Code, Codex, and any API key it finds: OpenAI, DeepSeek, Gemini, OpenRouter, Mistral, Groq, xAI) and suggests the best value: free and local first, then small, fast versions, big models only as a last resort. If one model is down or out of quota, the next one takes over.

## For coding agents installing Hypnos for a user

Follow these steps exactly. They work on macOS, Linux and Windows.

1. Install without questions:
   - macOS / Linux: `curl -fsSL https://agent-memory-architecture-chi.vercel.app/install.sh | sh -s -- --yes --systemone none`
   - Windows: `$env:HYPNOS_SETUP_YES='1'; $env:HYPNOS_SETUP_SYSTEMONE='none'; irm https://agent-memory-architecture-chi.vercel.app/install.ps1 | iex`
   - If the user gave a Jev key, use `--systemone jev --jev-key KEY` (Windows: `$env:HYPNOS_SETUP_SYSTEMONE='jev'; $env:TYPESAFE_API_KEY='KEY'`). If they run Laya locally, use `--systemone laya`.
2. Pick the memory model. Run `hypnos setup --list-models`. It prints JSON: every usable model with `spec`, `name`, `cost` (free, local, plan, paid) and `suggested`. Choose 2-5 models, best first, using what you know about them:
   - the cheapest model that is still reliable at reading a log and writing short, exact JSON facts (free or local if one is good enough);
   - prefer small, fast versions of strong families (flash, mini, haiku, luna, lightning...) and the newest version of a family;
   - put a different provider second, so one outage or quota limit doesn't stop memory;
   - use a large model (opus, pro, ultra...) only as the last entry, or not at all.
3. Apply it: `hypnos setup --yes --memory-model "SPEC1,SPEC2,SPEC3"` (keep the `--systemone` choice from step 1).
4. Tell the user what you chose and why in one or two lines, and that they should restart their agent.
5. If the user has no System One key, show them this note: "Hypnos works best with a System One model (Jev, or Laya running locally for free). Without one it still works, a little more accurately in places, but uses more tokens."

Do not edit the agents' config files by hand; `hypnos setup` does it and backs each file up first (`<file>.bak-hypnos`).
