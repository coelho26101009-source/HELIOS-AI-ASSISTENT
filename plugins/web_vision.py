"""
H.E.L.I.O.S. Plugin: Web Vision & Automation
Playwright headless — navega, extrai, pesquisa, interage.

Instalação: pip install playwright && playwright install chromium
"""

import asyncio
import base64
import json
import logging
from typing import Any

logger = logging.getLogger("helios.plugins.web_vision")

_browser  = None
_context  = None
_page     = None


async def _get_page(headless: bool = True):
    global _browser, _context, _page
    if _browser is None or not _browser.is_connected():
        from playwright.async_api import async_playwright
        playwright = await async_playwright().start()
        _browser = await playwright.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--disable-infobars", "--window-size=1440,900"],
        )
        _context = await _browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="pt-PT",
        )
        await _context.route(
            "**/*.{png,jpg,jpeg,gif,svg,woff,woff2,ttf,otf}",
            lambda r: r.abort() if _is_tracker(r.request.url) else r.continue_(),
        )
        _page = await _context.new_page()
    return _page


def _is_tracker(url: str) -> bool:
    return any(b in url for b in ["google-analytics", "doubleclick", "facebook.net",
                                   "hotjar", "clarity.ms", "googletagmanager"])


async def close_browser():
    global _browser, _context, _page
    for obj in [_page, _context, _browser]:
        if obj:
            try:
                await obj.close()
            except Exception:
                pass
    _browser = _context = _page = None


# ─── Tools ───────────────────────────────────────────────────────────────────

async def navigate_and_extract(url: str, extract_mode: str = "text") -> dict:
    try:
        from playwright.async_api import TimeoutError as PwTimeout
        page = await _get_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        await page.wait_for_timeout(1500)

        result = {"url": page.url, "title": await page.title()}

        if extract_mode == "text":
            result["content"] = await page.evaluate("""() => {
                ['script','style','nav','footer','header','aside',
                 '[class*="cookie"]','[class*="popup"]','[class*="banner"]']
                .forEach(s => document.querySelectorAll(s).forEach(e => e.remove()));
                return document.body.innerText
                    .replace(/\\n{3,}/g,'\\n\\n').trim().slice(0,12000);
            }""")

        elif extract_mode == "markdown":
            result["content"] = await page.evaluate("""() => {
                function toMd(n) {
                    if (n.nodeType===3) return n.textContent;
                    const t = n.tagName?.toLowerCase();
                    const c = () => Array.from(n.childNodes).map(toMd).join('');
                    if (!t) return c();
                    if (['script','style','nav','footer','aside'].includes(t)) return '';
                    if (/^h[1-6]$/.test(t)) return '\\n'+'#'.repeat(+t[1])+' '+n.innerText?.trim()+'\\n';
                    if (t==='p') return '\\n'+n.innerText?.trim()+'\\n';
                    if (t==='a') return `[${n.innerText?.trim()}](${n.href})`;
                    if (t==='li') return '\\n- '+n.innerText?.trim();
                    if (t==='strong'||t==='b') return `**${c()}**`;
                    if (t==='code') return '`'+c()+'`';
                    return c();
                }
                return toMd(document.body).replace(/\\n{3,}/g,'\\n\\n').trim().slice(0,10000);
            }""")

        elif extract_mode == "screenshot":
            img = await page.screenshot(full_page=False, type="jpeg", quality=80)
            result["screenshot_b64"] = base64.b64encode(img).decode()

        return result

    except Exception as exc:
        return {"error": f"Erro ao navegar para {url}: {exc}"}


async def extract_prices(url: str) -> dict:
    try:
        page = await _get_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        await page.wait_for_timeout(2000)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
        await page.wait_for_timeout(800)

        products = await page.evaluate("""() => {
            const re = /[€$£]?\\s*\\d+[.,]\\d{2}\\s*[€$£]?/g;
            const out = [];
            document.querySelectorAll('[itemtype*="Product"],[itemtype*="Offer"]').forEach(el => {
                const name  = el.querySelector('[itemprop="name"]')?.textContent?.trim();
                const price = el.querySelector('[itemprop="price"]')?.content
                           || el.querySelector('[itemprop="price"]')?.textContent?.trim();
                if (name && price) out.push({name, price, source:'schema'});
            });
            if (!out.length) {
                document.querySelectorAll('article,.product,[class*="product"],[class*="item"],li')
                .forEach(b => {
                    const txt    = b.innerText;
                    const prices = txt.match(re);
                    if (prices) {
                        const name = txt.split('\\n').find(l=>l.trim().length>3&&!l.match(re))?.trim();
                        if (name && name.length<200) out.push({name, price:prices[0], source:'heuristic'});
                    }
                });
            }
            return out.slice(0,30);
        }""")

        return {"url": page.url, "title": await page.title(),
                "products": products, "count": len(products)}

    except Exception as exc:
        return {"error": f"Não consegui extrair preços: {exc}"}


async def search_web(query: str, engine: str = "duckduckgo") -> dict:
    urls = {
        "duckduckgo": f"https://html.duckduckgo.com/html/?q={query.replace(' ','+')}",
        "google":     f"https://www.google.com/search?q={query.replace(' ','+')}",
        "bing":       f"https://www.bing.com/search?q={query.replace(' ','+')}",
    }
    try:
        page = await _get_page()
        await page.goto(urls.get(engine, urls["duckduckgo"]),
                        wait_until="domcontentloaded", timeout=15_000)

        results = await page.evaluate("""() => {
            const out = [];
            document.querySelectorAll('.result__body').forEach(r => {
                const title = r.querySelector('.result__title')?.innerText?.trim();
                const url   = r.querySelector('.result__url')?.innerText?.trim();
                const snip  = r.querySelector('.result__snippet')?.innerText?.trim();
                if (title) out.push({title, url, snippet: snip});
            });
            if (!out.length) {
                document.querySelectorAll('#search .g').forEach(r => {
                    const title = r.querySelector('h3')?.innerText?.trim();
                    const url   = r.querySelector('a')?.href;
                    const snip  = r.querySelector('.VwiC3b')?.innerText?.trim();
                    if (title) out.push({title, url, snippet: snip});
                });
            }
            return out.slice(0,8);
        }""")

        return {"query": query, "results": results}

    except Exception as exc:
        return {"error": f"Pesquisa falhou: {exc}"}


async def click_and_interact(action: str, selector: str | None = None,
                              text: str | None = None, value: str | None = None) -> dict:
    try:
        page = await _get_page()
        if action == "click":
            if selector:
                await page.click(selector, timeout=8_000)
            elif text:
                await page.get_by_text(text, exact=False).first.click(timeout=8_000)
        elif action == "type" and selector and value:
            await page.fill(selector, value)
        elif action == "scroll":
            await page.evaluate(f"window.scrollBy(0,{int(value or 500)})")
        elif action == "wait":
            await page.wait_for_timeout(min(int(value or 2000), 10_000))
        elif action == "screenshot":
            img = await page.screenshot(type="jpeg", quality=75)
            return {"screenshot_b64": base64.b64encode(img).decode(), "url": page.url}
        return {"success": True, "url": page.url, "title": await page.title()}
    except Exception as exc:
        return {"error": f"Interação falhou: {exc}"}


async def take_screenshot(full_page: bool = False) -> dict:
    try:
        page = await _get_page()
        img  = await page.screenshot(full_page=full_page, type="jpeg", quality=80)
        return {"screenshot_b64": base64.b64encode(img).decode(),
                "url": page.url, "title": await page.title()}
    except Exception as exc:
        return {"error": str(exc)}


# ─── Contrato do plugin ───────────────────────────────────────────────────────

def get_tools() -> list[dict]:
    return [
        {"type": "function", "function": {
            "name": "web_navigate_extract",
            "description": "Navega para uma URL e extrai o conteúdo (texto, markdown ou screenshot). Usa para resumir páginas, artigos ou documentações.",
            "parameters": {"type": "object", "required": ["url"], "properties": {
                "url":          {"type": "string", "description": "URL completo com https://"},
                "extract_mode": {"type": "string", "enum": ["text","markdown","screenshot"],
                                 "description": "Formato de extração (default: text)"},
            }},
        }},
        {"type": "function", "function": {
            "name": "web_extract_prices",
            "description": "Extrai preços e produtos de páginas de e-commerce ou lojas online.",
            "parameters": {"type": "object", "required": ["url"], "properties": {
                "url": {"type": "string"},
            }},
        }},
        {"type": "function", "function": {
            "name": "web_search",
            "description": "Pesquisa na web e devolve resultados com títulos, URLs e snippets.",
            "parameters": {"type": "object", "required": ["query"], "properties": {
                "query":  {"type": "string"},
                "engine": {"type": "string", "enum": ["duckduckgo","google","bing"],
                           "description": "Motor de pesquisa (default: duckduckgo)"},
            }},
        }},
        {"type": "function", "function": {
            "name": "web_interact",
            "description": "Interage com a página actual: clica, preenche campos, faz scroll.",
            "parameters": {"type": "object", "required": ["action"], "properties": {
                "action":   {"type": "string", "enum": ["click","type","scroll","wait","screenshot"]},
                "selector": {"type": "string", "description": "CSS selector"},
                "text":     {"type": "string", "description": "Texto visível do elemento"},
                "value":    {"type": "string", "description": "Valor para type/scroll"},
            }},
        }},
        {"type": "function", "function": {
            "name": "web_screenshot",
            "description": "Tira screenshot do browser actual.",
            "parameters": {"type": "object", "properties": {
                "full_page": {"type": "boolean"},
            }},
        }},
    ]


TOOL_HANDLERS: dict = {
    "web_navigate_extract": lambda a: navigate_and_extract(**a),
    "web_extract_prices":   lambda a: extract_prices(**a),
    "web_search":           lambda a: search_web(**a),
    "web_interact":         lambda a: click_and_interact(**a),
    "web_screenshot":       lambda a: take_screenshot(**a),
}
