"""
H.E.L.I.O.S. Guardrails Engine
Human-in-the-loop para ações sensíveis ou destrutivas.
O frontend recebe o pedido via Eel, Simão confirma ou cancela.
"""

import asyncio
import logging
from typing import Any, Callable, Awaitable

logger = logging.getLogger("helios.guardrails")

SENSITIVE_TOOLS: dict[str, dict] = {
    "web_interact": {
        "check":   lambda args: _is_form_submit(args),
        "message": "Vou submeter um formulário na web. Confirmas?",
    },
    "system_run_powershell": {
        "check":   lambda args: _is_dangerous_command(args),
        "message": "Este comando PowerShell pode alterar o sistema. Confirmas a execução?",
    },
    "system_delete_file":    {"check": lambda _: True, "message": "Vou apagar um ficheiro permanentemente."},
    "system_kill_process":   {"check": lambda _: True, "message": "Vou terminar um processo do sistema."},
    "system_registry_write": {"check": lambda _: True, "message": "Vou escrever no registo do Windows."},
    "system_format_drive":   {"check": lambda _: True, "message": "⚠️ ATENÇÃO: Vou formatar uma drive!"},
}

ALWAYS_CONFIRM: set[str] = {
    "system_delete_file",
    "system_format_drive",
    "system_registry_write",
    "system_kill_process",
}


def _is_form_submit(args: dict) -> bool:
    sel  = (args.get("selector") or "").lower()
    text = (args.get("text") or "").lower()
    danger = ["submit", "comprar", "pagar", "confirmar", "apagar", "eliminar", "deletar", "checkout"]
    return any(d in sel or d in text for d in danger)


def _is_dangerous_command(args: dict) -> bool:
    cmd = (args.get("command") or "").lower()
    danger = ["remove-item", "format", "del ", "rd ", "rmdir", "stop-process",
              "shutdown", "restart-computer", "clear-disk"]
    return any(d in cmd for d in danger)


class GuardrailsEngine:
    def __init__(self):
        self._confirm_callback: Callable[[str, dict], Awaitable[bool]] | None = None
        self._pending: dict[str, asyncio.Future] = {}

    def set_confirm_callback(self, cb: Callable[[str, dict], Awaitable[bool]]):
        self._confirm_callback = cb

    def requires_confirmation(self, tool_name: str, args: dict) -> bool:
        if tool_name in ALWAYS_CONFIRM:
            return True
        if tool_name in SENSITIVE_TOOLS:
            check = SENSITIVE_TOOLS[tool_name].get("check")
            if check and check(args):
                return True
        return False

    async def ask_confirmation(self, tool_name: str, args: dict) -> bool:
        rule    = SENSITIVE_TOOLS.get(tool_name, {})
        message = rule.get("message", f"Vou executar '{tool_name}'. Confirmas?")
        logger.warning(f"⚠️  Guardrail: '{tool_name}' args={args}")

        if self._confirm_callback is None:
            logger.error("Guardrail sem callback — operação bloqueada por segurança.")
            return False

        try:
            return await asyncio.wait_for(
                self._confirm_callback(message, {"tool": tool_name, "args": args}),
                timeout=60.0,
            )
        except asyncio.TimeoutError:
            logger.warning(f"Timeout no guardrail '{tool_name}' — cancelado.")
            return False

    def resolve_confirmation(self, request_id: str, confirmed: bool):
        future = self._pending.pop(request_id, None)
        if future and not future.done():
            future.set_result(confirmed)
