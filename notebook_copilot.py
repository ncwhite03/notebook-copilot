"""
title: Notebook Copilot
author: nick
version: 0.1.0
description: Read JupyterHub notebook context and execute code against a user's live kernel.
requirements: websocket-client, requests
"""

import json
import time
import uuid
from typing import Optional

import requests
import websocket
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        jupyterhub_url: str = Field(
            default="http://localhost:8000",
            description="Base URL of the JupyterHub deployment, no trailing slash (e.g. https://jupyter.internal.team).",
        )
        request_timeout: int = Field(
            default=15, description="Timeout in seconds for REST calls to JupyterHub."
        )
        exec_timeout: int = Field(
            default=30, description="Timeout in seconds to wait for code execution to finish."
        )

    class UserValves(BaseModel):
        api_token: str = Field(
            default="", description="Your personal JupyterHub API token (Hub UI -> Token)."
        )

    def __init__(self):
        self.valves = self.Valves()

    # ---------- internal helpers ----------

    def _headers(self, user_valves: "Tools.UserValves") -> dict:
        if not user_valves.api_token:
            raise ValueError(
                "No JupyterHub API token configured. Set it in this tool's UserValves "
                "(Settings -> Tools -> Notebook Copilot) before asking about a notebook."
            )
        return {"Authorization": f"token {user_valves.api_token}"}

    def _whoami(self, user_valves: "Tools.UserValves") -> str:
        r = requests.get(
            f"{self.valves.jupyterhub_url}/hub/api/user",
            headers=self._headers(user_valves),
            timeout=self.valves.request_timeout,
        )
        r.raise_for_status()
        return r.json()["name"]

    def _user_base_url(self, user_valves: "Tools.UserValves") -> str:
        username = self._whoami(user_valves)
        return f"{self.valves.jupyterhub_url}/user/{username}"

    def _api_get(self, base_url: str, path: str, user_valves: "Tools.UserValves") -> dict:
        r = requests.get(
            f"{base_url}/api/{path}",
            headers=self._headers(user_valves),
            timeout=self.valves.request_timeout,
        )
        r.raise_for_status()
        return r.json()

    def _find_existing_kernel(
        self, base_url: str, notebook_path: str, user_valves: "Tools.UserValves"
    ) -> Optional[str]:
        sessions = self._api_get(base_url, "sessions", user_valves)
        for s in sessions:
            if s.get("path") == notebook_path or s.get("notebook", {}).get("path") == notebook_path:
                return s["kernel"]["id"]
        return None

    def _start_scratch_kernel(
        self, base_url: str, kernel_name: str, user_valves: "Tools.UserValves"
    ) -> str:
        r = requests.post(
            f"{base_url}/api/kernels",
            headers=self._headers(user_valves),
            json={"name": kernel_name},
            timeout=self.valves.request_timeout,
        )
        r.raise_for_status()
        return r.json()["id"]

    def _execute_on_kernel(
        self,
        base_url: str,
        kernel_id: str,
        code: str,
        user_valves: "Tools.UserValves",
    ) -> dict:
        ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://")
        token = user_valves.api_token
        session_id = uuid.uuid4().hex
        url = f"{ws_url}/api/kernels/{kernel_id}/channels?session_id={session_id}&token={token}"

        ws = websocket.create_connection(
            url,
            header=[f"Authorization: token {token}"],
            timeout=self.valves.exec_timeout,
        )
        ws.settimeout(2)

        msg_id = uuid.uuid4().hex
        request = {
            "header": {
                "msg_id": msg_id,
                "username": "notebook-copilot",
                "session": session_id,
                "msg_type": "execute_request",
                "version": "5.3",
            },
            "parent_header": {},
            "metadata": {},
            "content": {
                "code": code,
                "silent": False,
                "store_history": True,
                "user_expressions": {},
                "allow_stdin": False,
                "stop_on_error": True,
            },
            "channel": "shell",
        }
        ws.send(json.dumps(request))

        stdout_parts = []
        result_parts = []
        error_traceback = None
        saw_busy = False
        deadline = time.time() + self.valves.exec_timeout

        try:
            while time.time() < deadline:
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not raw:
                    continue
                m = json.loads(raw)
                if m.get("parent_header", {}).get("msg_id") != msg_id:
                    continue
                mtype = m["header"]["msg_type"]
                content = m.get("content", {})

                if mtype == "stream":
                    stdout_parts.append(content.get("text", ""))
                elif mtype in ("execute_result", "display_data"):
                    data = content.get("data", {})
                    if "text/plain" in data:
                        result_parts.append(data["text/plain"])
                elif mtype == "error":
                    error_traceback = "\n".join(content.get("traceback", []))
                elif mtype == "status":
                    state = content.get("execution_state")
                    if state == "busy":
                        saw_busy = True
                    elif state == "idle" and saw_busy:
                        break
        finally:
            ws.close()

        return {
            "stdout": "".join(stdout_parts),
            "result": "\n".join(result_parts),
            "error": error_traceback,
        }

    def _format_cells(self, nb_content: dict, max_output_chars: int = 800) -> str:
        cells = nb_content.get("cells", [])
        lines = []
        for i, cell in enumerate(cells):
            ctype = cell.get("cell_type", "unknown")
            source = "".join(cell.get("source", []))
            lines.append(f"--- Cell {i} ({ctype}) ---\n{source}")
            if ctype == "code":
                out_texts = []
                for out in cell.get("outputs", []):
                    if out.get("output_type") == "stream":
                        out_texts.append("".join(out.get("text", [])))
                    elif out.get("output_type") in ("execute_result", "display_data"):
                        data = out.get("data", {})
                        if "text/plain" in data:
                            out_texts.append("".join(data["text/plain"]))
                    elif out.get("output_type") == "error":
                        out_texts.append(
                            "\n".join(out.get("traceback", [out.get("evalue", "")]))
                        )
                if out_texts:
                    combined = "\n".join(out_texts)
                    if len(combined) > max_output_chars:
                        combined = combined[:max_output_chars] + "\n...[truncated]"
                    lines.append(f"[output]\n{combined}")
        return "\n\n".join(lines)

    # ---------- tool-callable methods ----------

    def list_notebooks(
        self, directory: str = "", __user__: dict = {}
    ) -> str:
        """
        List notebook (.ipynb) files under a directory on the user's JupyterHub server.

        :param directory: Directory path relative to the user's home, empty string for home directory.
        :return: A list of notebook paths, or an error message.
        """
        user_valves = self._get_user_valves(__user__)
        try:
            base_url = self._user_base_url(user_valves)
            listing = self._api_get(base_url, f"contents/{directory}", user_valves)
            if listing.get("type") != "directory":
                return f"'{directory}' is not a directory."
            notebooks = [
                item["path"] for item in listing.get("content", []) if item["type"] == "notebook"
            ]
            if not notebooks:
                return f"No notebooks found under '{directory or '/'}'."
            return "\n".join(notebooks)
        except Exception as e:
            return f"Error listing notebooks: {e}"

    def get_notebook_context(
        self, path: str, __user__: dict = {}
    ) -> str:
        """
        Read a Jupyter notebook's cells (code + markdown) and their outputs for context.
        Use this before answering questions about what a notebook does, explaining an
        error it produced, or suggesting the next cell.

        :param path: Path to the .ipynb file relative to the user's home directory (e.g. "analysis/eda.ipynb").
        :return: The notebook's cells and outputs formatted as text.
        """
        user_valves = self._get_user_valves(__user__)
        try:
            base_url = self._user_base_url(user_valves)
            nb = self._api_get(base_url, f"contents/{path}", user_valves)
            if nb.get("type") != "notebook":
                return f"'{path}' is not a notebook."
            return self._format_cells(nb["content"])
        except requests.HTTPError as e:
            return f"Error reading notebook '{path}': {e}"
        except Exception as e:
            return f"Error reading notebook '{path}': {e}"

    def run_code(
        self, path: str, code: str, __user__: dict = {}
    ) -> str:
        """
        Execute Python code and return its output. If the notebook at `path` has a live
        kernel running (i.e. it's open in JupyterLab), the code runs in THAT kernel and
        sees the user's existing variables. Otherwise a scratch kernel of the same type
        is started (its state is not visible in the notebook UI).

        :param path: Path to the .ipynb file relative to the user's home directory.
        :param code: Python code to execute.
        :return: stdout, the last expression's value, and any error traceback.
        """
        user_valves = self._get_user_valves(__user__)
        try:
            base_url = self._user_base_url(user_valves)

            kernel_id = self._find_existing_kernel(base_url, path, user_valves)
            reused = kernel_id is not None
            if not kernel_id:
                nb = self._api_get(base_url, f"contents/{path}", user_valves)
                kernel_name = (
                    nb.get("content", {})
                    .get("metadata", {})
                    .get("kernelspec", {})
                    .get("name", "python3")
                )
                kernel_id = self._start_scratch_kernel(base_url, kernel_name, user_valves)

            result = self._execute_on_kernel(base_url, kernel_id, code, user_valves)

            header = "[ran in the notebook's live kernel]" if reused else "[ran in a new scratch kernel, not visible in the notebook UI]"
            parts = [header]
            if result["stdout"]:
                parts.append(f"stdout:\n{result['stdout']}")
            if result["result"]:
                parts.append(f"result:\n{result['result']}")
            if result["error"]:
                parts.append(f"error:\n{result['error']}")
            if len(parts) == 1:
                parts.append("(no output)")
            return "\n\n".join(parts)
        except Exception as e:
            return f"Error executing code against '{path}': {e}"

    # ---------- valve plumbing ----------

    def _get_user_valves(self, __user__: dict) -> "Tools.UserValves":
        raw = (__user__ or {}).get("valves")
        if isinstance(raw, Tools.UserValves):
            return raw
        if isinstance(raw, dict):
            return Tools.UserValves(**raw)
        return Tools.UserValves()
