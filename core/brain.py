"""
H.E.L.I.O.S. Brain — Motor de Decisão
Groq (Llama-3.3-70b) para velocidade + Ollama fallback offline.
Loop de function-calling multi-turno com streaming real.
"""

import asyncio
import json
import logging
from typing import AsyncIterator, Any

import httpx
from groq import AsyncGroq

from core.plugin_loader import get_all_tools, execute_tool
from core.guardrails import GuardrailsEngine
from core.memory import MemoryEngine

logger = logging.getLogger("helios.brain")

GROQ_MODEL      = "llama-3.1-8b-instant"   # mais rápido, sem rate limit
OLLAMA_MODEL    = "qwen2.5:3b"
OLLAMA_URL      = "http://localhost:11434/api/chat"
MAX_TOOL_ROUNDS = 8

SYSTEM_PROMPT = """És o H.E.L.I.O.S. (High-Efficiency Local Intelligence & Operating System).
O teu utilizador chama-se Simão. Trata-o sempre pelo nome com tom amigável, empático e descontraído — como o melhor amigo e braço direito dele.

Personalidade:
- Cumprimentos calorosos: "Olá Simão!", "Deixa isso comigo, amigo.", "Grande, vou a isso!"  
- Quando algo falha: "Ups Simão, [problema]. Queres que tente [alternativa]?"
- Quando consegues: "Pronto Simão! [resultado]"
- Nunca és genérico nem robótico. És eficiente, rápido e directo.

Capacidades:
- Automação web: navegar, extrair conteúdo, preços, pesquisar, interagir com páginas
- Controlo do PC: PowerShell, ficheiros, processos, rede, volume, brilho
- Organização: renomear ficheiros com IA, limpar cache, monitorizar recursos
- Memória: lembras-te de conversas passadas e preferências do Simão
- Modos de trabalho: activar ambientes com 1 comando ("modo hacker", "modo foco")

Regras:
- Usa SEMPRE as ferramentas quando pedes informação da web ou do sistema
- Para ações destrutivas pede SEMPRE confirmação
- Responde em Português de Portugal
- Sê conciso — o Simão é produtivo, não gosta de textos longos sem necessidade"""


class Brain:
    def __init__(self, api_key: str, guardrails: GuardrailsEngine, memory: MemoryEngine):
        self.client    = AsyncGroq(api_key=api_key)
        self.guardrails = guardrails
        self.memory    = memory
        self.conversation: list[dict] = []

    def reset_conversation(self):
        self.conversation.clear()

    async def chat(self, user_message: str, stream: bool = True) -> AsyncIterator[str]:
        self.conversation.append({"role": "user", "content": user_message})
        tools = get_all_tools()

        for round_num in range(MAX_TOOL_ROUNDS):
            try:
                response = await self.client.chat.completions.create(
                    model    = GROQ_MODEL,
                    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *self.conversation],
                    tools       = tools if tools else None,
                    tool_choice = "auto" if tools else None,
                    temperature = 0.65,
                    max_tokens  = 4096,
                )
            except Exception as exc:
                logger.error(f"Groq error (round {round_num}): {exc}")
                async for token in self._ollama_fallback(user_message):
                    yield token
                return

            choice  = response.choices[0]
            message = choice.message

            # Resposta final de texto
            if choice.finish_reason == "stop" or not message.tool_calls:
                text = message.content or ""
                self.conversation.append({"role": "assistant", "content": text})
                if stream and text:
                    for i in range(0, len(text), 4):
                        yield text[i:i+4]
                        await asyncio.sleep(0.008)
                else:
                    yield text
                return

            # Tool calls
            self.conversation.append({
                "role": "assistant",
                "content": message.content,
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in message.tool_calls
                ],
            })

            names = [tc.function.name for tc in message.tool_calls]
            yield f"_thinking_:⚙️ {', '.join(names)}..."

            results = await asyncio.gather(*[
                self._run_tool(tc) for tc in message.tool_calls
            ])

            for tc, result in zip(message.tool_calls, results):
                self.conversation.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })

        yield "Simão, atingi o limite de operações encadeadas. Podes reformular o pedido?"

    async def _run_tool(self, tool_call) -> dict:
        name = tool_call.function.name
        try:
            args = json.loads(tool_call.function.arguments)
        except json.JSONDecodeError:
            args = {}

        if self.guardrails.requires_confirmation(name, args):
            confirmed = await self.guardrails.ask_confirmation(name, args)
            if not confirmed:
                return {"cancelled": True, "message": "Operação cancelada pelo Simão."}

        logger.info(f"Tool: {name}({json.dumps(args, ensure_ascii=False)[:120]})")
        return await execute_tool(name, args)

    async def _ollama_fallback(self, message: str) -> AsyncIterator[str]:
        yield "_thinking_:Groq indisponível — a usar modelo local..."
        payload = {
            "model": OLLAMA_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": message},
            ],
            "stream": True,
        }
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                async with client.stream("POST", OLLAMA_URL, json=payload) as resp:
                    async for line in resp.aiter_lines():
                        if line:
                            try:
                                data  = json.loads(line)
                                token = data.get("message", {}).get("content", "")
                                if token:
                                    yield token
                            except json.JSONDecodeError:
                                continue
        except httpx.ConnectError:
            yield (
                "Ups Simão, tanto o Groq como o Ollama estão inacessíveis. "
                "Confirma a tua ligação e que o Ollama está a correr com `ollama serve`."
            )
