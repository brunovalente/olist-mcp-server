"""Tools UI v0 — Playwright sobre a UI do Tiny para gaps nao cobertos pela API oficial.

Escopo desta rev:
- trocar_transportador: altera nome/CNPJ/IE do transportador de uma NF nao autorizada.

Convencoes:
- Toda tool v0 faz pre-check via API oficial antes de abrir browser (recusa se ja autorizada).
- Toda tool v0 faz pos-check via API oficial para provar efeito.
- Log estruturado JSONL em OLIST_UI_LOG_FILE.
- Selectors da UI ficam concentrados aqui; se a Olist mudar, ajustar apenas este arquivo.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from src.token_auth import get_token_manager as _get_api_token_manager
from src.oauth import OAuthTokenManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _log(op: str, alvo: str, status: str, started_at: float, extra: Optional[dict] = None) -> None:
    log_path = _env("OLIST_UI_LOG_FILE", "/app/data/ui-v0.log")
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "operacao": op,
        "alvo": alvo,
        "status": status,
        "duracao_ms": int((time.time() - started_at) * 1000),
        "origem": "mcp",
    }
    if extra:
        entry.update(extra)
    try:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # log best-effort


async def _api_get(oauth: OAuthTokenManager, path: str) -> dict:
    import httpx
    base = _env("API_BASE_URL", "https://api.tiny.com.br/public-api/v3").rstrip("/")
    token = await oauth.get_access_token()
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{base}{path}", headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json()


def _get_session_state_path() -> str:
    return _env("OLIST_UI_SESSION_FILE", "/app/data/ui-v0-session.json")


async def _login_on_page(page) -> None:
    """Faz login na pagina atual usando OLIST_UI_USER/OLIST_UI_PASSWORD.

    Nao fecha/reabre browser. Necessario porque reabrir o contexto com
    storage_state recem-salvo dispara invalidacao de sessao pelo Olist
    (detecta como novo dispositivo) e redireciona pra /login/.
    """
    user = _env("OLIST_UI_USER")
    password = _env("OLIST_UI_PASSWORD")
    if not user or not password:
        raise RuntimeError("OLIST_UI_USER/OLIST_UI_PASSWORD ausentes no ambiente do container")
    await page.locator('input[name="username"]').fill(user)
    await page.locator('input[name="password"]').fill(password)
    try:
        await page.locator('button[type="submit"], input[type="submit"]').first.click()
    except Exception:
        await page.keyboard.press("Enter")
    await page.wait_for_load_state("networkidle", timeout=30000)
    url = page.url.lower()
    if "login" in url or "accounts.tiny.com.br" in url:
        raise RuntimeError(
            "login falhou: continua em pagina de login (credenciais invalidas, captcha ou MFA)"
        )


# ---------------------------------------------------------------------------
# Implementacoes das operacoes
# ---------------------------------------------------------------------------

async def _do_trocar_transportador(id_nota: str, nome: str, cnpj: str, ie: str) -> dict:
    from playwright.async_api import async_playwright

    base_url = _env("OLIST_UI_BASE_URL", "https://erp.olist.com").rstrip("/")
    headless = _env("OLIST_UI_HEADLESS", "true").lower() != "false"
    sess = _get_session_state_path()
    has_session = Path(sess).exists()
    nf_url = f"{base_url}/notas_fiscais#edit/{id_nota}"

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        ctx_kwargs = {"viewport": {"width": 1920, "height": 1080}}
        if has_session:
            ctx_kwargs["storage_state"] = sess
        ctx = await browser.new_context(**ctx_kwargs)
        page = await ctx.new_page()
        try:
            await page.goto(nf_url, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass

            # Se apareceu o form de login (fluxo accounts.tiny.com.br ou
            # /login/ sem session_state), faz login inline no mesmo contexto.
            # IMPORTANTE: nao fechar o browser entre login e NF (isso dispara
            # invalidacao de sessao por detecao de novo dispositivo).
            if await page.locator('input[name="username"]').count() > 0:
                started_login = time.time()
                await _login_on_page(page)
                Path(sess).parent.mkdir(parents=True, exist_ok=True)
                await ctx.storage_state(path=sess)
                try:
                    os.chmod(sess, 0o600)
                except Exception:
                    pass
                _log("session-refresh", sess, "ok", started_login)
                await page.goto(nf_url, wait_until="domcontentloaded")
                try:
                    await page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass

            # Modal "Este usuario ja esta logado em outro dispositivo": clicar
            # "login" para assumir a sessao (Olist limita sessoes concorrentes).
            try:
                await page.wait_for_selector(
                    "text=já está logado em outro dispositivo", timeout=5000
                )
                await page.get_by_role("button", name="login", exact=False).first.click()
                await page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass

            await page.wait_for_selector("text=Transportador / Volumes", timeout=30000)

            set_js = """
            (args) => {
              const [name, value] = args;
              const el = document.querySelector('input[name="' + name + '"]');
              if (!el) return {ok:false, reason:'input nao encontrado: '+name};
              const proto = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
              proto.set.call(el, value);
              el.dispatchEvent(new Event('input',  {bubbles:true}));
              el.dispatchEvent(new Event('change', {bubbles:true}));
              el.dispatchEvent(new Event('blur',   {bubbles:true}));
              return {ok:true, current: el.value};
            }
            """
            for field, value in [("transportador", nome), ("cnpjTransportador", cnpj), ("ieTransportador", ie)]:
                r = await page.evaluate(set_js, [field, value])
                if not r.get("ok"):
                    raise RuntimeError(f"falha ao setar {field}: {r}")

            r = await page.evaluate("""() => {
              if (typeof salvarNotaFiscal !== 'function') return {ok:false, reason:'funcao salvarNotaFiscal ausente'};
              salvarNotaFiscal();
              return {ok:true};
            }""")
            if not r.get("ok"):
                raise RuntimeError(f"falha ao clicar salvar: {r}")
            await page.wait_for_load_state("networkidle", timeout=30000)
        finally:
            await browser.close()

    return {"ok": True}


# ---------------------------------------------------------------------------
# Registro das tools UI v0 no MCP
# ---------------------------------------------------------------------------

def register_ui_v0_tools(mcp: FastMCP, get_oauth: callable) -> int:
    """Registra as tools UI v0 no servidor MCP. Retorna o numero de tools registradas."""

    count = 0

    @mcp.tool(
        name="trocar_transportador",
        description=(
            "[UI v0] Altera nome, CPF/CNPJ e IE do transportador de uma nota fiscal NAO autorizada. "
            "Usa automacao de browser (Playwright) porque a API oficial v2/v3 nao atualiza o CNPJ do "
            "transportador via PUT /notas/{id}/despacho. "
            "Recusa com erro se a NF ja tiver chaveAcesso (autorizada na SEFAZ). "
            "Parametros: idNota (obrigatorio). Informe UMA das opcoes de transportador: idContato "
            "(recomendado; puxa nome/cpfCnpj/inscricaoEstadual do cadastro) OU nome+cnpj+ie literais. "
            "Pre-check via API v3 (situacao/chaveAcesso) e pos-check via API v3 (CNPJ efetivo). "
            "Exige sessao UI ja salva no container; se expirada, retorna erro estruturado."
        ),
    )
    async def trocar_transportador(
        idNota: str,
        idContato: str | None = None,
        nome: str | None = None,
        cnpj: str | None = None,
        ie: str | None = None,
    ) -> dict:
        started = time.time()
        try:
            oauth = get_oauth()
            nf = await _api_get(oauth, f"/notas/{idNota}")
        except Exception as e:
            _log("trocar_transportador", idNota, "erro-precheck", started, {"erro": str(e)})
            return {"ok": False, "motivo": "precheck-api-falhou", "erro": str(e)}

        chave = nf.get("chaveAcesso") or ""
        if chave:
            _log("trocar_transportador", idNota, "recusado-autorizada", started, {"chave": chave})
            return {
                "ok": False,
                "motivo": "nf-autorizada",
                "mensagem": f"NF {idNota} ja autorizada na SEFAZ (chaveAcesso={chave}). Operacao recusada.",
            }

        # Resolver dados do transportador
        if idContato:
            try:
                contato = await _api_get(oauth, f"/contatos/{idContato}")
            except Exception as e:
                _log("trocar_transportador", idNota, "erro-contato", started, {"erro": str(e)})
                return {"ok": False, "motivo": "contato-nao-encontrado", "erro": str(e)}
            nome_f = contato.get("nome", "") or ""
            cnpj_f = contato.get("cpfCnpj", "") or ""
            ie_f = contato.get("inscricaoEstadual", "") or ""
        else:
            if not nome or not cnpj:
                return {
                    "ok": False,
                    "motivo": "parametros-insuficientes",
                    "mensagem": "Informe idContato OU (nome + cnpj). IE e opcional.",
                }
            nome_f, cnpj_f, ie_f = nome, cnpj, ie or ""

        try:
            await _do_trocar_transportador(idNota, nome_f, cnpj_f, ie_f)
        except Exception as e:
            _log("trocar_transportador", idNota, "erro-ui", started, {"erro": str(e)})
            motivo = "sessao-expirada" if "sessao" in str(e).lower() else "falha-automacao"
            return {"ok": False, "motivo": motivo, "erro": str(e)}

        # Pos-check
        try:
            nf_pos = await _api_get(oauth, f"/notas/{idNota}")
        except Exception as e:
            _log("trocar_transportador", idNota, "erro-poscheck", started, {"erro": str(e)})
            return {"ok": False, "motivo": "poscheck-api-falhou", "erro": str(e)}

        t = nf_pos.get("transportador") or {}
        got_cnpj = t.get("cpfCnpj", "") or ""
        got_nome = t.get("nome", "") or ""
        if got_cnpj != cnpj_f:
            _log("trocar_transportador", idNota, "poscheck-fail", started,
                 {"esperado_cnpj": cnpj_f, "obtido_cnpj": got_cnpj})
            return {
                "ok": False,
                "motivo": "poscheck-divergente",
                "mensagem": f"CNPJ na NF continua '{got_cnpj}' (esperado '{cnpj_f}').",
            }

        _log("trocar_transportador", idNota, "ok", started, {"cnpj": got_cnpj, "nome": got_nome})
        return {
            "ok": True,
            "idNota": idNota,
            "transportador": {"nome": got_nome, "cpfCnpj": got_cnpj, "ie": t.get("ie", "")},
        }

    count += 1
    return count
