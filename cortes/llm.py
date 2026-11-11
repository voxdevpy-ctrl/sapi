"""Invocação de agentes do Claude Code em modo headless (usa a assinatura já autenticada).

Cada chamada roda `claude -p --agent <nome>` com saída JSON validada por schema.
Não usa chave de API: se ANTHROPIC_API_KEY estiver definida, ela é removida do ambiente
do subprocesso para impedir cobrança por API sem autorização.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import config, log


class LLMError(RuntimeError):
    pass


class LLMQuotaError(LLMError):
    """Limite de uso da assinatura/sobrecarga: não é defeito do trabalho — esperar e tentar de novo."""


_QUOTA = re.compile(r"(?i)usage limit|rate.?limit|limit reached|overloaded|\b429\b|\b529\b|too many requests|"
                    r"quota|try again later|resets? at")


def _raise(agent: str, msg: str):
    raise (LLMQuotaError if _QUOTA.search(msg) else LLMError)(f"agente {agent}: {msg[-400:]}")


def claude_exe() -> str:
    cfg = config.load()["llm"]
    if cfg.get("claude_exe") and Path(cfg["claude_exe"]).exists():
        return cfg["claude_exe"]
    if shutil.which("claude"):
        return shutil.which("claude")
    pats = [os.path.expandvars(r"%LOCALAPPDATA%\Claude-Data\*\claude-code\*\*\claude.exe"),
            os.path.expanduser(r"~\.local\bin\claude.exe")]
    found = [p for pat in pats for p in glob.glob(pat)]
    if not found:
        raise LLMError("executável do Claude Code não encontrado; defina llm.claude_exe na configuração")
    return max(found, key=os.path.getmtime)


_STATUS: tuple[float, dict] = (0.0, {})


def status(refresh: bool = False) -> dict:
    """Conexão com o Claude Code DESTA máquina (cada pessoa usa o próprio, instalado e logado no próprio PC).

    Não lê credenciais: só verifica se o executável existe, a versão (`claude --version`, sem gastar uso)
    e se há um login salvo (existência do arquivo, nunca o conteúdo). Caminhos saem com ~ no lugar da pasta do usuário.
    """
    import time
    global _STATUS
    if not refresh and time.time() - _STATUS[0] < 60:
        return _STATUS[1]
    home = str(Path.home())
    out = {"instalado": False, "versao": None, "caminho": None, "login_salvo": None, "habilitado": config.load()["llm"]["habilitado"],
           "orientacao": "", "site": "https://claude.com/product/claude-code"}
    try:
        exe = claude_exe()
        out.update(instalado=True, caminho=exe.replace(home, "~"))
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=20, env={k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"})
        out["versao"] = (r.stdout or r.stderr).strip().splitlines()[0][:80] if (r.stdout or r.stderr).strip() else None
    except (LLMError, OSError, subprocess.TimeoutExpired) as e:
        out["orientacao"] = ("Instale o Claude Code no seu computador (site oficial abaixo) e abra um terminal com o comando "
                             "`claude` para fazer login com a SUA conta. Depois toque em Verificar de novo.")
        out["erro"] = str(e).replace(home, "~")[:200]
    if out["instalado"]:
        cred = Path.home() / ".claude" / ".credentials.json"
        out["login_salvo"] = cred.exists() or bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
        if not out["login_salvo"]:
            out["orientacao"] = ("O Claude Code está instalado, mas não achei um login salvo. Abra um terminal, rode `claude` "
                                 "e entre com a SUA conta. Nenhuma senha passa pelo SAPI.")
    _STATUS = (time.time(), out)
    return out


def _extract_json(text: str):
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def call_agent(agent: str, prompt: str, schema: dict, *, task_id: str | None = None,
               item_id: str | None = None, tools: list[str] | None = None, timeout: int | None = None) -> dict:
    cfg = config.load()["llm"]
    if not cfg["habilitado"]:
        raise LLMError("chamadas de IA desabilitadas na configuração")
    cmd = [claude_exe(), "-p", prompt, "--agent", agent, "--output-format", "json",
           "--json-schema", json.dumps(schema), "--permission-mode", "dontAsk"]
    cmd += ["--allowedTools", ",".join([*(tools or []), "StructuredOutput"])]
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           cwd=config.ROOT, env=env, timeout=timeout or cfg["timeout_s"])
    except subprocess.TimeoutExpired as e:
        raise LLMError(f"agente {agent} excedeu o tempo limite") from e
    log.event(task_id=task_id, item_id=item_id, agent=agent, requested="invocar agente IA",
              exit_code=r.returncode, error=(r.stderr[-300:] or None) if r.returncode else None)
    if r.returncode != 0:
        _raise(agent, f"falhou ({r.returncode}): {r.stderr or r.stdout}")
    env_out = _extract_json(r.stdout)
    if env_out.get("is_error"):
        _raise(agent, f"retornou erro: {env_out.get('result')}")
    data = env_out.get("structured_output")
    if data is None:
        data = _extract_json(env_out.get("result") or "")
    missing = [k for k in schema.get("required", []) if k not in data]
    if missing:
        raise LLMError(f"resposta do agente {agent} sem campos obrigatórios: {missing}")
    return data
