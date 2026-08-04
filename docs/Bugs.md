_No application bugs recorded yet — no project code has been written. The entries below are environment and tooling failures encountered while evaluating candidate platforms; they are recorded because they blocked evaluation work and because the causes are likely to recur once implementation begins._

---

## 1. OpenHands cannot reach the local OpenAI-compatible LLM server — `litellm.InternalServerError: OpenAIException - Connection error.`

| Field | Detail |
|---|---|
| **Symptom** | With an OpenHands LLM profile pointed at a self-hosted OpenAI-compatible server on another device, every request fails in the UI with `litellm.InternalServerError: InternalServerError: OpenAIException - Connection error.` No agent work runs; the failure is at the transport layer, before any model call succeeds. |
| **Date** | 2026-08-03 |
| **Environment** | Windows 11, project directory `C:\Users\LOQ\Desktop\Projects\Bhai-To-Bhai`. OpenHands configured via the Advanced tab with LLM Provider `OpenAI`, model `openai/<model-id>`, base URL `http://192.168.1.14:3001/v1`, and an API key (a placeholder is acceptable where the server does not authenticate). |
| **Diagnosis** | **Not established.** `Connection error` from LiteLLM means the HTTP request never completed, which narrows it to reachability or address rather than model behaviour, but the specific cause was not isolated before the trial was abandoned. Untested hypotheses, in rough order of likelihood: (a) OpenHands runs in a container, so `192.168.1.14` must be reachable *from inside that container*, not merely from the Windows host; (b) the serving device's firewall blocks inbound connections on port 3001; (c) the model ID does not exactly match an `id` returned by `http://192.168.1.14:3001/v1/models`, though this would normally surface as a model error rather than a connection error; (d) the server binds to loopback only rather than `0.0.0.0`. |
| **Resolution** | **Unresolved.** The trial was not resumed: the next step considered was to bypass the UI and drive OpenHands through its Python SDK, but the execution layer decision moved to Maestro the same day (ADR-004), so OpenHands is no longer on the path. |
| **Prevention** | Verify endpoint reachability *from the process that will actually make the call* before configuring any tool against it — for a containerised consumer that means `curl http://<host>:<port>/v1/models` from inside the container, not from the host shell. This applies directly to the planned DeepSeek adapter, which will target an OpenAI-compatible endpoint the same way (Research topic 6). |

---

## 2. `claude` not found on PATH after native Windows install

| Field | Detail |
|---|---|
| **Symptom** | The Claude Code installer (`irm https://claude.ai/install.ps1 \| iex`) reported success and installed v2.1.220 to `C:\Users\LOQ\.local\bin\claude.exe`, but invoking `claude` in PowerShell failed with `CommandNotFoundException`. The installer itself flagged the cause in its setup notes. |
| **Date** | 2026-08-03 |
| **Environment** | Windows 11, PowerShell. Binary present and functional — `& "C:\Users\LOQ\.local\bin\claude.exe" --version` returned `2.1.220 (Claude Code)`. |
| **Diagnosis** | The installer placed the binary in `C:\Users\LOQ\.local\bin` without adding that directory to the user PATH. Purely an environment issue; the installation was intact. |
| **Resolution** | **Fixed.** Appended `C:\Users\LOQ\.local\bin` to the user PATH environment variable. Note that an already-open terminal keeps its stale PATH — either restart it, or patch the live session with `$env:Path += ";C:\Users\LOQ\.local\bin"`. |
| **Prevention** | Relevant beyond this one install: the orchestrator invokes every coding agent as a subprocess, so each agent CLI must be resolvable from the environment the orchestrator runs under — not merely from an interactive shell where the user has fixed PATH by hand. Worth an explicit startup preflight that resolves each configured agent binary and fails loudly if one is missing, rather than surfacing it later as an opaque adapter failure. |

---
