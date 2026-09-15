# Notebook Copilot

An Open WebUI **Tool** that lets a chat model (e.g. Nemotron 3) read a JupyterHub
notebook's cells/outputs for context, and execute code against the notebook's
**live kernel** when one is running, so it can see the user's actual variables.

## How it works

- `list_notebooks(directory)` — lists `.ipynb` files via the Jupyter Contents API.
- `get_notebook_context(path)` — reads a notebook's cells + outputs, formatted as text.
- `run_code(path, code)` — finds a running kernel session attached to that notebook
  (via the Jupyter Sessions API) and executes code in it over the kernel websocket
  protocol. If no session is running, it spins up a scratch kernel of the same
  kernel type instead (clearly labeled in the response, since that kernel's state
  won't show up in the user's notebook UI).

All requests go through `{jupyterhub_url}/user/{username}/api/...`, which is how
JupyterHub proxies REST/websocket calls to a user's single-user server using their
own Hub API token — no separate notebook-server token needed.

## Target stack

This is written for a **bare-metal/VM deployment**: JupyterHub and Open WebUI
each run as their own `systemd` service (not Docker/Kubernetes), and
JupyterHub authenticates against LDAP/AD. None of that changes the tool's
code — JupyterHub API tokens work the same regardless of the authenticator
backing the Hub, and classic JupyterHub always proxies a user's server at
`{jupyterhub_url}/user/{username}/api/...`, which is what this tool assumes.
(If your JupyterHub is actually Kubeflow Notebooks instead, the URL scheme is
different — `/notebook/{namespace}/{name}/...` — and `_user_base_url` in
`notebook_copilot.py` needs rewriting before this will work.)

## Deployment steps

**1. Nothing new to run.** The tool is a Python module executed inside Open
WebUI's own process — there's no separate service, container, or systemd
unit to add.

**2. Make sure its two dependencies are importable by Open WebUI.** Open
WebUI usually auto-installs the `requirements:` frontmatter
(`websocket-client`, `requests`) when the tool is saved. If a call fails with
`ModuleNotFoundError`, find the interpreter the unit actually uses and
install into it directly:
```bash
systemctl show open-webui -p ExecStart
pip install websocket-client requests   # into that same interpreter
```

**3. Register the tool in Open WebUI.**
- Quick path: admin login → Workspace → Tools → **+ Create new tool** → paste
  in [`notebook_copilot.py`](notebook_copilot.py) → save.
- Reproducible path: since this repo is version-controlled, push it via Open
  WebUI's REST API (`POST /api/v1/tools/create` with an admin bearer token)
  as part of your normal deploy/provisioning step, instead of a manual UI
  paste that can drift from what's in git.

**4. Wire it to the Nemotron 3 model.** Workspace → Models → the Nemotron 3
entry → enable this tool, and set **Function Calling: Native** under
Advanced. That's necessary but not sufficient — whatever is serving
Nemotron 3 (e.g. vLLM) also needs OpenAI-style tool calling enabled
server-side (for vLLM: `--enable-auto-tool-choice` plus a matching
`--tool-call-parser`).

**5. Confirm the network path.** The Open WebUI host needs to reach the
JupyterHub host/port, and any reverse proxy in front of JupyterHub must pass
`Upgrade: websocket` through on `/user/*/api/kernels/*/channels`. This is
almost certainly already true, since JupyterLab's own in-browser kernel
connections depend on that exact same path.

**6. Each user sets their own token, once.** Log into JupyterHub (LDAP) →
generate a token at `{jupyterhub_url}/hub/token` → paste it into the tool's
`UserValves.api_token` in their Open WebUI settings. Worth a line in
onboarding docs.

**7. Verify end to end.** Watch `journalctl -u open-webui -f` while testing
`get_notebook_context` first — it's read-only and exercises the whole chain
(LDAP-issued token → Hub proxy → Contents API) — before trying `run_code`'s
websocket path.

## Notes / limitations

- **Tool-calling support**: Nemotron 3 needs to support function calling, and
  Open WebUI needs "native" tool calling enabled for the model (Workspace →
  Models → Advanced → Function Calling: Native). If native calling isn't
  reliable for your build, Open WebUI's default prompt-based tool calling still
  works but is less consistent about *when* the model decides to call a tool —
  test with a few prompts before rolling out.
- **Execution is real code execution** in the user's own kernel/account — scope
  this tool to trusted users, same as you would a notebook itself. There's no
  sandboxing here beyond whatever JupyterHub/the kernel environment already
  provides.
- **Paths** are relative to the user's home directory on their single-user
  server, matching what you'd see in the JupyterLab file browser.
- If `run_code` starts a scratch kernel (no live session found), its variables
  won't appear in the open notebook — tell the user to open/run the notebook
  first if they want copilot suggestions to build on their existing session.
