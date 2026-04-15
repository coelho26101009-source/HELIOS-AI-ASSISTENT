"""
H.E.L.I.O.S. Plugin Loader
Auto-descoberta de plugins. Larga um .py em plugins/ → carrega automaticamente.
Contrato obrigatório: o ficheiro deve expor get_tools() → list[dict]
Contrato opcional:    TOOL_HANDLERS: dict[str, Callable]
"""

import asyncio
import importlib.util
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("helios.plugin_loader")

_loaded_plugins: dict[str, Any] = {}
_all_tools:      list[dict]     = []
_all_handlers:   dict[str, Any] = {}


def load_all_plugins(plugins_dir: Path | None = None) -> tuple[list[dict], dict]:
    global _all_tools, _all_handlers

    if plugins_dir is None:
        plugins_dir = Path(__file__).parent.parent / "plugins"

    if not plugins_dir.exists():
        logger.warning(f"Pasta de plugins não encontrada: {plugins_dir}")
        return [], {}

    _all_tools.clear()
    _all_handlers.clear()

    for plugin_path in sorted(plugins_dir.glob("*.py")):
        if plugin_path.name.startswith("_"):
            continue

        name = plugin_path.stem
        try:
            module = _import_module(name, plugin_path)

            if not hasattr(module, "get_tools"):
                logger.debug(f"'{name}' ignorado: sem get_tools()")
                continue

            tools = module.get_tools()
            if not isinstance(tools, list):
                logger.warning(f"'{name}': get_tools() deve devolver list")
                continue

            _all_tools.extend(tools)
            _loaded_plugins[name] = module

            if hasattr(module, "TOOL_HANDLERS"):
                _all_handlers.update(module.TOOL_HANDLERS)

            tool_names = [t["function"]["name"] for t in tools]
            logger.info(f"✅ Plugin '{name}' → {tool_names}")

        except Exception as exc:
            logger.error(f"❌ Falha ao carregar '{name}': {exc}", exc_info=True)

    logger.info(f"Arsenal: {len(_loaded_plugins)} plugins, {len(_all_tools)} ferramentas")
    return _all_tools, _all_handlers


def _import_module(name: str, path: Path):
    spec   = importlib.util.spec_from_file_location(f"helios.plugins.{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def execute_tool(tool_name: str, arguments: dict) -> dict:
    handler = _all_handlers.get(tool_name)
    if handler is None:
        return {
            "error": (
                f"Ups Simão, a ferramenta '{tool_name}' não está registada. "
                f"Disponíveis: {list(_all_handlers.keys())}"
            )
        }
    try:
        result = handler(arguments)
        if asyncio.iscoroutine(result):
            result = await result
        return result if isinstance(result, dict) else {"result": result}
    except Exception as exc:
        logger.error(f"Erro ao executar '{tool_name}': {exc}", exc_info=True)
        return {
            "error": (
                f"Ups Simão, '{tool_name}' falhou: {exc}. "
                "Já registei no log. Queres que tente outra abordagem?"
            )
        }


def get_all_tools() -> list[dict]:
    return _all_tools.copy()


def list_plugins() -> dict[str, list[str]]:
    return {
        name: [t["function"]["name"] for t in (mod.get_tools() if hasattr(mod, "get_tools") else [])]
        for name, mod in _loaded_plugins.items()
    }


async def reload_plugin(plugin_name: str, plugins_dir: Path | None = None) -> bool:
    if plugins_dir is None:
        plugins_dir = Path(__file__).parent.parent / "plugins"

    plugin_path = plugins_dir / f"{plugin_name}.py"
    if not plugin_path.exists():
        return False

    if plugin_name in _loaded_plugins:
        old = _loaded_plugins[plugin_name]
        if hasattr(old, "get_tools"):
            old_names = {t["function"]["name"] for t in old.get_tools()}
            _all_tools[:]  = [t for t in _all_tools if t["function"]["name"] not in old_names]
            for n in old_names:
                _all_handlers.pop(n, None)

    try:
        module = _import_module(plugin_name, plugin_path)
        if hasattr(module, "get_tools"):
            _all_tools.extend(module.get_tools())
        if hasattr(module, "TOOL_HANDLERS"):
            _all_handlers.update(module.TOOL_HANDLERS)
        _loaded_plugins[plugin_name] = module
        logger.info(f"🔄 Plugin recarregado: '{plugin_name}'")
        return True
    except Exception as exc:
        logger.error(f"Falha ao recarregar '{plugin_name}': {exc}")
        return False
