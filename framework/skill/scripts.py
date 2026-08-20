"""Safe & resilient subprocess script execution for deployed Skills."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from domain import SkillScriptSpec, SkillSpec
from framework.skill.errors import SkillScriptError
from framework.skill.loader import SkillLoader

AuditCallback = Callable[[str, dict[str, Any]], None]

# Environment variables safe to propagate into the subprocess
_SAFE_ENV_VARS = {
    "PATH",
    "PYTHONPATH",
    "VIRTUAL_ENV",
    "LANG",
    "LC_ALL",
    "HOME",
    "USER",
    "TMPDIR",
}


class SkillScriptRunner:
    """Executes a declared Skill script in an isolated subprocess with crash & timeout protection."""

    @classmethod
    async def run(
        cls,
        spec: SkillSpec,
        script_name: str,
        arguments: dict[str, Any],
        *,
        loader: SkillLoader,
        audit: AuditCallback | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        audit_fn = audit or (lambda _evt, _data: None)

        # 1. Locate the script spec in the Skill
        script_spec = cls._find_script_spec(spec, script_name)

        if timeout_seconds is not None:
            effective_timeout = min(timeout_seconds, float(script_spec.timeout_seconds))
        else:
            effective_timeout = float(script_spec.timeout_seconds)

        audit_fn(
            "skill_script_started",
            {
                "name": spec.name,
                "version": spec.version,
                "script": script_spec.name,
                "path": script_spec.path,
                "interpreter": script_spec.interpreter,
                "timeout_seconds": effective_timeout,
            },
        )

        # 2. Extract script content and prepare temporary directory
        with tempfile.TemporaryDirectory(prefix=f"skill_{spec.name}_") as tmpdir:
            tmp_path = Path(tmpdir)

            # Materialize script file and references/assets if available from artifact
            script_file_path = cls._materialize_files(spec, script_spec, tmp_path, loader=loader)

            # Prepare arguments JSON
            input_json_str = json.dumps(arguments, ensure_ascii=False)
            input_json_bytes = input_json_str.encode("utf-8")
            input_file = tmp_path / "input.json"
            input_file.write_bytes(input_json_bytes)

            # Prepare minimal sanitized environment
            env = {k: os.environ[k] for k in _SAFE_ENV_VARS if k in os.environ}
            env["SKILL_NAME"] = spec.name
            env["SKILL_VERSION"] = spec.version
            env["SKILL_SCRIPT_INPUT_JSON"] = input_json_str

            # Select interpreter
            if script_spec.interpreter == "python":
                cmd = [sys.executable, str(script_file_path), str(input_file)]
            elif script_spec.interpreter == "bash":
                cmd = ["bash", str(script_file_path), str(input_file)]
            else:
                err_msg = f"Unsupported interpreter {script_spec.interpreter!r}"
                audit_fn(
                    "skill_script_failed",
                    {"name": spec.name, "version": spec.version, "script": script_spec.name, "error": err_msg},
                )
                raise SkillScriptError(err_msg)

            # 3. Execute in subprocess without shell=True (isolated process group)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(tmp_path),
                    env=env,
                    start_new_session=True,
                )
            except Exception as exc:
                err_msg = f"Failed to spawn script process: {exc}"
                audit_fn(
                    "skill_script_failed",
                    {"name": spec.name, "version": spec.version, "script": script_spec.name, "error": err_msg},
                )
                return {"success": False, "error": err_msg, "error_type": "SPAWN_ERROR"}

            try:
                stdout_data, stderr_data = await asyncio.wait_for(
                    proc.communicate(input=input_json_bytes),
                    timeout=effective_timeout,
                )
            except TimeoutError:
                try:
                    if hasattr(os, "killpg") and hasattr(os, "getpgid"):
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    else:
                        proc.kill()
                    await proc.wait()
                except Exception:
                    pass
                err_msg = f"Script execution timed out after {effective_timeout}s"
                audit_fn(
                    "skill_script_failed",
                    {"name": spec.name, "version": spec.version, "script": script_spec.name, "error": err_msg},
                )
                return {"success": False, "error": err_msg, "error_type": "TIMEOUT"}
            except Exception as exc:
                try:
                    if hasattr(os, "killpg") and hasattr(os, "getpgid"):
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    else:
                        proc.kill()
                    await proc.wait()
                except Exception:
                    pass
                err_msg = f"Script execution error: {exc}"
                audit_fn(
                    "skill_script_failed",
                    {"name": spec.name, "version": spec.version, "script": script_spec.name, "error": err_msg},
                )
                return {"success": False, "error": err_msg, "error_type": "EXEC_ERROR"}

            # 4. Truncate outputs if necessary
            max_bytes = script_spec.max_output_bytes
            stdout_truncated = False
            stderr_truncated = False
            if len(stdout_data) > max_bytes:
                stdout_data = stdout_data[:max_bytes]
                stdout_truncated = True
            if len(stderr_data) > max_bytes:
                stderr_data = stderr_data[:max_bytes]
                stderr_truncated = True

            stdout_str = stdout_data.decode("utf-8", errors="replace").strip()
            stderr_str = stderr_data.decode("utf-8", errors="replace").strip()

            # 5. Handle non-zero exit code
            if proc.returncode != 0:
                audit_fn(
                    "skill_script_failed",
                    {
                        "name": spec.name,
                        "version": spec.version,
                        "script": script_spec.name,
                        "exit_code": proc.returncode,
                        "stderr": stderr_str[:500],
                    },
                )
                return {
                    "success": False,
                    "exit_code": proc.returncode,
                    "stdout": stdout_str,
                    "stderr": stderr_str,
                    "error": f"Script failed with exit code {proc.returncode}: {stderr_str or stdout_str}",
                    "error_type": "NON_ZERO_EXIT",
                }

            # 6. Parse structured result if stdout is JSON
            parsed_result: Any = None
            if stdout_str:
                try:
                    parsed_result = json.loads(stdout_str)
                except Exception:
                    # Try finding the last non-empty line as JSON
                    lines = [line.strip() for line in stdout_str.splitlines() if line.strip()]
                    if lines:
                        try:
                            parsed_result = json.loads(lines[-1])
                        except Exception:
                            parsed_result = None

            result_payload: dict[str, Any] = {
                "success": True,
                "exit_code": 0,
                "result": parsed_result if parsed_result is not None else stdout_str,
                "stdout": stdout_str,
                "stderr": stderr_str,
            }
            if stdout_truncated:
                result_payload["stdout_truncated"] = True
            if stderr_truncated:
                result_payload["stderr_truncated"] = True

            audit_fn(
                "skill_script_succeeded",
                {
                    "name": spec.name,
                    "version": spec.version,
                    "script": script_spec.name,
                    "exit_code": 0,
                    "output_bytes": len(stdout_data),
                },
            )
            return result_payload

    @classmethod
    def _find_script_spec(cls, spec: SkillSpec, script_name: str) -> SkillScriptSpec:
        for s in spec.scripts:
            if s.name == script_name or s.path == script_name or Path(s.path).name == script_name:
                return s

        # Check if artifact has a matching script path
        if spec.artifact:
            for f in spec.artifact:
                if f.path.startswith("scripts/") and (
                    f.path == script_name
                    or Path(f.path).name == script_name
                    or f.path == f"scripts/{script_name}"
                    or f.path == f"scripts/{script_name}.py"
                ):
                    interpreter = "python" if f.path.endswith(".py") else "bash" if f.path.endswith(".sh") else "python"
                    return SkillScriptSpec(name=script_name, path=f.path, interpreter=interpreter)

        raise SkillScriptError(f"Script {script_name!r} is not declared or found in Skill {spec.name!r}")

    @classmethod
    def _materialize_files(
        cls,
        spec: SkillSpec,
        script_spec: SkillScriptSpec,
        tmp_path: Path,
        *,
        loader: SkillLoader,
    ) -> Path:
        loader.materialize_to_directory(spec, tmp_path)
        script_target = tmp_path / script_spec.path
        if script_target.exists():
            return script_target

        # Fallback if path didn't match directly
        for candidate in tmp_path.rglob("*"):
            if candidate.is_file() and (
                candidate.name == script_spec.name or candidate.name == Path(script_spec.path).name
            ):
                return candidate

        raise SkillScriptError(f"Script file {script_spec.path!r} not found for skill {spec.name!r}")
