"""
H.E.L.I.O.S. — Main Entry Point
"""

import asyncio
import logging
import os
import sys
import threading
from pathlib import Path

import eel
import yaml
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.brain import Brain
from core.guardrails import GuardrailsEngine
from core.plugin_loader import load_all_plugins
from core.memory import MemoryEngine
from core.logger import setup_logger
from core.voice import VoiceEngine

setup_logger()
logger = logging.getLogger("helios.main")

CONFIG_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CONFIG = yaml.safe_load(f)

guardrails = GuardrailsEngine()
memory     = MemoryEngine()
voice      = VoiceEngine(CONFIG.get("voice", {}))
brain      = Brain(
    api_key    = os.getenv("GROQ_API_KEY", ""),
    guardrails = guardrails,
    memory     = memory,
)

load_all_plugins(Path(__file__).parent.parent / "plugins")

FRONTEND_DIR = Path(__file__).parent.parent / "frontend" / "out"
eel.init(str(FRONTEND_DIR) if FRONTEND_DIR.exists() else "web")


@eel.expose
def send_message(user_text: str):
    if not user_text or not user_text.strip():
        return {"error": "Mensagem vazia"}
    logger.info(f"Mensagem recebida: '{user_text[:80]}'")
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(_process_message(user_text))
        return result
    except Exception as exc:
        logger.error(f"Erro ao processar: {exc}", exc_info=True)
        return {"error": str(exc), "text": f"Ups, ocorreu um erro: {exc}"}
    finally:
        try:
            loop.close()
        except Exception:
            pass


@eel.expose
def confirm_action(request_id: str, confirmed: bool):
    guardrails.resolve_confirmation(request_id, confirmed)
    return {"ok": True}


@eel.expose
def start_voice_listen():
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        text = loop.run_until_complete(voice.listen())
        return {"text": text or ""}
    except Exception as exc:
        logger.error(f"Erro de voz: {exc}")
        return {"error": str(exc)}
    finally:
        try:
            loop.close()
        except Exception:
            pass


@eel.expose
def get_conversation_history():
    return memory.get_recent_messages(limit=50)


@eel.expose
def clear_conversation():
    brain.reset_conversation()
    return {"ok": True}


@eel.expose
def get_loaded_plugins():
    from core.plugin_loader import list_plugins
    return list_plugins()


async def _process_message(user_text: str) -> dict:
    try:
        memory.save_message("user", user_text)
        full_response = ""
        status_updates = []

        async for token in brain.chat(user_text, stream=False):
            if token.startswith("_thinking_:"):
                status = token.replace("_thinking_:", "")
                status_updates.append(status)
                logger.info(f"Status: {status}")
            else:
                full_response += token

        if not full_response.strip():
            full_response = "Desculpa Simão, não consegui gerar uma resposta. Tenta de novo."

        memory.save_message("assistant", full_response)
        logger.info(f"Resposta gerada ({len(full_response)} chars): {full_response[:80]}")

        if CONFIG.get("voice", {}).get("tts_enabled", False):
            def _falar():
                loop2 = asyncio.new_event_loop()
                asyncio.set_event_loop(loop2)
                try:
                    loop2.run_until_complete(voice.speak(full_response))
                finally:
                    loop2.close()
            threading.Thread(target=_falar, daemon=True).start()

        return {"text": full_response, "status": status_updates, "ok": True}

    except Exception as exc:
        logger.error(f"Erro no _process_message: {exc}", exc_info=True)
        return {"text": f"Ups Simão, ocorreu um erro: {exc}", "ok": False, "error": str(exc)}


def main():
    logger.info("🌟 H.E.L.I.O.S. V7 a iniciar...")
    logger.info(f"   Frontend : {FRONTEND_DIR} ({'✅' if FRONTEND_DIR.exists() else '❌ não existe!'})")
    logger.info(f"   API Key  : {'✅' if os.getenv('GROQ_API_KEY') else '❌ FALTA!'}")

    try:
        eel.start(
            "index.html",
            mode="chrome",
            size=(1440, 900),
            port=0,
            block=True,
        )
    except (SystemExit, KeyboardInterrupt):
        logger.info("H.E.L.I.O.S. encerrado.")
    except Exception as exc:
        logger.warning(f"Chrome não encontrado: {exc}. A tentar browser padrão...")
        try:
            eel.start("index.html", mode="default", size=(1440, 900), port=0, block=True)
        except Exception as exc2:
            logger.critical(f"Impossível abrir UI: {exc2}")


if __name__ == "__main__":
    main()
