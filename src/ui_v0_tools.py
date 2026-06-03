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


async def _api_put(oauth: OAuthTokenManager, path: str, body: dict) -> dict:
    import httpx
    base = _env("API_BASE_URL", "https://api.tiny.com.br/public-api/v3").rstrip("/")
    token = await oauth.get_access_token()
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.put(
            f"{base}{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {"status_code": r.status_code}


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
                    os.chmod(sess, 0o660)  # 0o660 (nao 0o600): preserva o mask da ACL no mount compartilhado; container uid 1002=pana_agent precisa rw
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


async def _do_trocar_transportador_pedido(
    id_pedido: str,
    id_forma_envio: str,
    id_forma_frete: str,
    id_transportador: str,
    nome_transportador: str,
    frete_por_conta: str,
) -> dict:
    """Altera transportador + forma de envio/frete na pagina de edicao do PEDIDO.

    Cobre o gap em que PUT /pedidos/{id}/despacho persiste so idContatoTransportadora
    (formaEnvio/formaFrete sao ignorados). Aqui a UI toca os selects nativos do form de
    edicao do pedido (/vendas#edit/<id>) e persiste tudo via salvarVenda().
    """
    from playwright.async_api import async_playwright

    base_url = _env("OLIST_UI_BASE_URL", "https://erp.olist.com").rstrip("/")
    headless = _env("OLIST_UI_HEADLESS", "true").lower() != "false"
    sess = _get_session_state_path()
    has_session = Path(sess).exists()
    pedido_url = f"{base_url}/vendas#edit/{id_pedido}"
    vendas_url = f"{base_url}/vendas"

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        ctx_kwargs = {"viewport": {"width": 1920, "height": 1080}}
        if has_session:
            ctx_kwargs["storage_state"] = sess
        ctx = await browser.new_context(**ctx_kwargs)
        page = await ctx.new_page()
        try:
            async def wait_quiet(timeout: int = 20000) -> None:
                try:
                    await page.wait_for_load_state("networkidle", timeout=timeout)
                except Exception:
                    pass

            async def ensure_pedido_route() -> bool:
                """Abre o pedido mesmo quando o SSO aterrissa na home autenticada.

                O Olist/Tiny as vezes aceita a sessao mas ignora a primeira URL com hash,
                deixando o browser em https://erp.olist.com/. Nessa situacao, navegar para
                /vendas e disparar o hash do SPA replica o fluxo que funciona no browser.
                """
                for attempt in range(3):
                    if attempt == 0:
                        await page.goto(pedido_url, wait_until="domcontentloaded")
                    elif attempt == 1:
                        await page.goto(vendas_url, wait_until="domcontentloaded")
                        await wait_quiet()
                        await page.evaluate(
                            """(id) => {
                              window.location.hash = `#edit/${id}`;
                              window.dispatchEvent(new HashChangeEvent('hashchange'));
                            }""",
                            str(id_pedido),
                        )
                    else:
                        await page.goto(pedido_url, wait_until="domcontentloaded")
                    await wait_quiet()

                    modal = page.locator("#bs-modal-ui-popup").first
                    if await modal.count() > 0:
                        btn = modal.locator('button.btn-primary:has-text("login")').first
                        if await btn.count() > 0:
                            await btn.click()
                            await wait_quiet()

                    if await page.locator('input[name="username"]').count() > 0:
                        return False
                    if "vendas" in page.url:
                        return True
                    try:
                        await page.wait_for_selector(
                            'button:has-text("editar"), select#idFormaEnvio',
                            timeout=5000,
                        )
                        return True
                    except Exception:
                        pass
                return "vendas" in page.url

            async def handle_kick_modal() -> bool:
                """Assume a sessao quando o Olist bloqueia por login em outro dispositivo."""
                try:
                    body = await page.locator("body").inner_text(timeout=3000)
                except Exception:
                    body = ""
                if "logado em outro dispositivo" not in body:
                    return False

                btn = page.locator('#bs-modal-ui-popup button.btn-primary:has-text("login")').first
                if await btn.count() == 0:
                    btn = page.locator('button:has-text("login")').first
                if await btn.count() == 0:
                    raise RuntimeError(
                        "modal de sessao concorrente apareceu, mas o botao 'login' nao foi encontrado"
                    )
                await btn.click()
                await wait_quiet()
                return True

            await ensure_pedido_route()

            # Login inline se aparecer o form (mesmo contexto — nao reabrir o browser,
            # senao o Olist invalida a sessao por detecao de novo dispositivo).
            if await page.locator('input[name="username"]').count() > 0:
                started_login = time.time()
                await _login_on_page(page)
                Path(sess).parent.mkdir(parents=True, exist_ok=True)
                await ctx.storage_state(path=sess)
                try:
                    os.chmod(sess, 0o660)  # 0o660 (nao 0o600): preserva o mask da ACL no mount compartilhado; container uid 1002=pana_agent precisa rw
                except Exception:
                    pass
                _log("session-refresh", sess, "ok", started_login)
                await ensure_pedido_route()

            # Modal "ja esta logado em outro dispositivo" — assumir a sessao.
            if await handle_kick_modal():
                await ensure_pedido_route()

            if "vendas" not in page.url:
                raise RuntimeError(
                    f"nao consegui navegar para pedido {id_pedido} (url={page.url})"
                )

            # Entra em modo edicao (botao "editar" na visualizacao do pedido).
            edit_btn = page.locator('button:has-text("editar")').first
            if await edit_btn.count() == 0:
                raise RuntimeError("botao 'editar' nao encontrado na visualizacao do pedido")
            await edit_btn.click()
            await page.wait_for_selector("select#idFormaEnvio", timeout=20000)

            # Pre-check via UI: pedido com expedicao criada bloqueia alteracao de transporte.
            aviso = page.locator("#divMsgVendaEdicaoDadosExpedicao").first
            try:
                if await aviso.count() > 0 and await aviso.is_visible():
                    raise RuntimeError(
                        "pedido tem expedicao criada — UI bloqueia alteracao de transporte. "
                        "Cancelar a expedicao no Olist antes."
                    )
            except RuntimeError:
                raise
            except Exception:
                pass

            # 1) Forma de envio (select nativo). onChangeIdFormaEnvio repopula #idFormaFrete.
            await page.select_option("select#idFormaEnvio", value=str(id_forma_envio))
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

            # 2) Forma de frete (espera o option async carregar).
            if id_forma_frete:
                deadline = time.time() + 10
                ok = False
                while time.time() < deadline:
                    has = await page.evaluate(
                        "(v) => !!document.querySelector(`select#idFormaFrete option[value='${v}']`)",
                        str(id_forma_frete),
                    )
                    if has:
                        ok = True
                        break
                    await page.wait_for_timeout(500)
                if not ok:
                    opts = await page.evaluate(
                        "() => Array.from(document.querySelectorAll('select#idFormaFrete option'))"
                        ".map(o => ({v:o.value, t:o.text}))"
                    )
                    raise RuntimeError(
                        f"forma de frete {id_forma_frete} nao disponivel para "
                        f"forma_envio={id_forma_envio}. Opcoes: {opts}"
                    )
                await page.select_option("select#idFormaFrete", value=str(id_forma_frete))

            # 3) Transportador (hidden id + texto visivel) via proto setter — bypassa autocomplete.
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
            for field, value in [
                ("idTransportador", str(id_transportador)),
                ("transportador", nome_transportador),
            ]:
                r = await page.evaluate(set_js, [field, value])
                if not r.get("ok"):
                    raise RuntimeError(f"falha ao setar {field}: {r}")

            # 4) Frete por conta (opcional).
            if frete_por_conta:
                await page.select_option("select#fretePorConta", value=str(frete_por_conta))

            # 5) Salvar. Tiny expoe salvarVenda() global; fallback pro botao.
            saved = await page.evaluate(
                "() => { if (typeof salvarVenda === 'function') { salvarVenda(); return {ok:true}; } return {ok:false}; }"
            )
            if not saved.get("ok"):
                btn = page.locator('button:has-text("salvar")').first
                if await btn.count() == 0:
                    raise RuntimeError("nao consegui salvar: nem salvarVenda() nem botao 'salvar'")
                await btn.click()
            await page.wait_for_load_state("networkidle", timeout=30000)

            # Persiste sessao (cookies podem ter rotacionado).
            try:
                await ctx.storage_state(path=sess)
                os.chmod(sess, 0o660)  # 0o660 (nao 0o600): preserva o mask da ACL no mount compartilhado; container uid 1002=pana_agent precisa rw
            except Exception:
                pass
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
            "[UI v0] Altera o transportador de uma nota fiscal NAO autorizada e, quando a NF veio de "
            "um pedido de venda, tambem atualiza o transportador do pedido. Caso de uso tipico na "
            "PANA: operador pede 'trocar transportadora' em um pedido recem-feito — cliente escolheu "
            "Correios no site, mas o envio vai sair por entrega propria (transportador = PANA TEXTIL, "
            "idContato=776636263). "
            "Detalhes do fluxo: (1) pre-check via API v3 recusa NF com chaveAcesso; (2) se "
            "nf.origem.tipo=venda e idContato informado, chama PUT /pedidos/{id}/despacho com "
            "idContatoTransportadora (API barata; se falhar aborta antes da UI); (3) automacao "
            "Playwright na pagina de edicao da NF para alterar nome/CNPJ/IE (necessario porque "
            "PUT /notas/{id}/despacho da API nao persiste CNPJ); (4) pos-check via API v3. "
            "GAP CONHECIDO: API v3 nao atualiza formaEnvio/formaFrete do pedido — sao aceitos no "
            "payload mas ignorados. Se a troca for de Correios para entrega propria, o pedido fica "
            "com transportador novo e formaEnvio antiga (ex: 'Correios - SEDEX'). A tool retorna "
            "aviso_forma nesse caso. Nao afeta a etiqueta gerada nem a NF autorizada, apenas a "
            "visualizacao no ERP. "
            "Parametros: idNota (obrigatorio). Informe UMA das opcoes: idContato (recomendado — "
            "puxa dados do cadastro e habilita atualizacao do pedido) OU nome+cnpj+ie literais "
            "(atualiza somente a NF). "
            "Se a sessao UI expirou, a tool faz auto-login headless."
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

        # Se a NF veio de um pedido (origem.tipo=venda), atualizar o transportador
        # no pedido tambem. Feito ANTES da automacao UI da NF porque e API barata
        # e um erro aqui evita abrir browser. Precisa de idContato — se o chamador
        # passou nome/cnpj literais, nao da pra derivar o contato, entao pula.
        id_pedido = None
        pedido_updated = False
        origem = nf.get("origem") or {}
        if origem.get("tipo") == "venda" and origem.get("id"):
            id_pedido = str(origem["id"])
        if id_pedido and idContato:
            try:
                await _api_put(
                    oauth,
                    f"/pedidos/{id_pedido}/despacho",
                    {"idContatoTransportadora": int(idContato)},
                )
                pedido_updated = True
            except Exception as e:
                _log("trocar_transportador", idNota, "erro-pedido", started,
                     {"idPedido": id_pedido, "erro": str(e)})
                return {
                    "ok": False,
                    "motivo": "falha-atualizar-pedido",
                    "mensagem": (
                        f"Nao consegui atualizar o transportador no pedido {id_pedido} "
                        f"(origem da NF). Abortando antes da UI para evitar divergencia."
                    ),
                    "erro": str(e),
                }

        try:
            await _do_trocar_transportador(idNota, nome_f, cnpj_f, ie_f)
        except Exception as e:
            _log("trocar_transportador", idNota, "erro-ui", started,
                 {"erro": str(e), "pedido_updated": pedido_updated, "idPedido": id_pedido})
            motivo = "sessao-expirada" if "sessao" in str(e).lower() else "falha-automacao"
            return {"ok": False, "motivo": motivo, "erro": str(e),
                    "aviso_pedido": (
                        f"ATENCAO: pedido {id_pedido} ja foi atualizado para o novo transportador, "
                        f"mas a NF falhou. Estado divergente — reexecutar ou reverter o pedido."
                    ) if pedido_updated else None}

        # Pos-check NF
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

        result = {
            "ok": True,
            "idNota": idNota,
            "transportador": {"nome": got_nome, "cpfCnpj": got_cnpj, "ie": t.get("ie", "")},
            "idPedido": id_pedido,
            "pedidoAtualizado": pedido_updated,
        }
        if pedido_updated:
            result["aviso_forma"] = (
                "pedido.formaEnvio/formaFrete nao sao atualizaveis via API v3 — o contato foi "
                "trocado mas a forma original pode ter ficado (ex: 'Correios - SEDEX' mesmo com "
                "transportador novo). Isso nao afeta a etiqueta nem a NF; ajustar manualmente no "
                "ERP se a visualizacao importar."
            )
        _log("trocar_transportador", idNota, "ok", started,
             {"cnpj": got_cnpj, "nome": got_nome, "pedido_updated": pedido_updated,
              "idPedido": id_pedido})
        return result

    count += 1

    @mcp.tool(
        name="trocar_transportador_pedido",
        description=(
            "[UI v0] Altera o transportador E a forma de envio/frete de um PEDIDO de venda. "
            "Use quando o pedido ainda NAO tem NF, ou quando so quer ajustar o transporte do pedido. "
            "Diferente de trocar_transportador (que opera numa NF por idNota), aqui o alvo e o pedido. "
            "Cobre o gap em que PUT /pedidos/{id}/despacho persiste so o contato do transportador e "
            "IGNORA formaEnvio/formaFrete — esta tool toca os selects nativos da pagina de edicao do "
            "pedido (Playwright) e persiste tudo via salvarVenda(). "
            "Caso de uso PANA: pedido recem-criado em que o cliente escolheu 'Correios' no site mas o "
            "envio vai sair por SEDEX/PAC especifico ou entrega propria — passe o transportador e as "
            "formas corretas. "
            "Fluxo: (1) resolve idPedido (por numero, se preciso) e o nome do transportador via API v3; "
            "(2) Playwright: entra em edicao, seta forma de envio (que repopula as formas de frete), "
            "forma de frete, transportador (id+nome) e frete por conta; salva; (3) pos-check via API v3 "
            "(endpoint de lista) confirma transportador + forma de envio. "
            "Recusa se o pedido ja tem expedicao criada (a UI bloqueia). Se a sessao UI expirou, faz "
            "auto-login headless. "
            "Parametros: informe idPedido OU numero (um dos dois); idContato = id do contato do "
            "transportador (ex Correios=579759491, PANA TEXTIL=776636263); idFormaEnvio (obrigatorio); "
            "idFormaFrete (opcional); fretePorConta (opcional: R=remetente, D=destinatario, "
            "T=terceiros, S=sem frete)."
        ),
    )
    async def trocar_transportador_pedido(
        idContato: str,
        idFormaEnvio: str,
        idPedido: str | None = None,
        numero: str | None = None,
        idFormaFrete: str | None = None,
        fretePorConta: str | None = None,
    ) -> dict:
        started = time.time()
        if not idPedido and not numero:
            return {
                "ok": False,
                "motivo": "parametros-insuficientes",
                "mensagem": "Informe idPedido OU numero do pedido.",
            }

        oauth = get_oauth()

        # Resolve idPedido (por numero, se preciso) e captura o numeroPedido p/ o pos-check.
        try:
            if not idPedido:
                lst = await _api_get(oauth, f"/pedidos?numero={numero}")
                itens = lst.get("itens") or []
                if not itens:
                    return {
                        "ok": False,
                        "motivo": "pedido-nao-encontrado",
                        "mensagem": f"Nenhum pedido com numero {numero}.",
                    }
                idPedido = str(itens[0].get("id"))
            pedido = await _api_get(oauth, f"/pedidos/{idPedido}")
            numero_pedido = str(pedido.get("numeroPedido") or numero or "")
        except Exception as e:
            _log("trocar_transportador_pedido", str(idPedido or numero), "erro-precheck", started, {"erro": str(e)})
            return {"ok": False, "motivo": "precheck-api-falhou", "erro": str(e)}

        # Resolve o nome do transportador a partir do contato.
        try:
            contato = await _api_get(oauth, f"/contatos/{idContato}")
        except Exception as e:
            _log("trocar_transportador_pedido", str(idPedido), "erro-contato", started, {"erro": str(e)})
            return {"ok": False, "motivo": "contato-nao-encontrado", "erro": str(e)}
        nome_t = contato.get("nome", "") or ""

        # Automacao UI.
        try:
            await _do_trocar_transportador_pedido(
                str(idPedido),
                str(idFormaEnvio),
                str(idFormaFrete or ""),
                str(idContato),
                nome_t,
                str(fretePorConta or ""),
            )
        except Exception as e:
            msg = str(e).lower()
            motivo = "falha-automacao"
            if "expedicao" in msg:
                motivo = "expedicao-criada"
            elif "login" in msg or "sessao" in msg:
                motivo = "sessao-expirada"
            _log("trocar_transportador_pedido", str(idPedido), "erro-ui", started, {"erro": str(e)})
            return {"ok": False, "motivo": motivo, "erro": str(e)}

        # Pos-check via endpoint de LISTA (o obter /pedidos/{id} nao retorna transportador).
        try:
            lst_pos = await _api_get(oauth, f"/pedidos?numero={numero_pedido}")
            itens_pos = lst_pos.get("itens") or []
            pos = itens_pos[0] if itens_pos else {}
        except Exception as e:
            _log("trocar_transportador_pedido", str(idPedido), "erro-poscheck", started, {"erro": str(e)})
            return {"ok": False, "motivo": "poscheck-api-falhou", "erro": str(e)}

        t = pos.get("transportador") or {}
        got_tid = str(t.get("id") or "")
        got_fe = str((t.get("formaEnvio") or {}).get("id") or "")
        if got_tid != str(idContato) or (idFormaEnvio and got_fe != str(idFormaEnvio)):
            _log("trocar_transportador_pedido", str(idPedido), "poscheck-fail", started,
                 {"esperado_tid": idContato, "obtido_tid": got_tid,
                  "esperado_fe": idFormaEnvio, "obtido_fe": got_fe})
            return {
                "ok": False,
                "motivo": "poscheck-divergente",
                "mensagem": (
                    f"Pos-check divergente no pedido {numero_pedido}: "
                    f"transportador={got_tid} (esperado {idContato}), "
                    f"formaEnvio={got_fe} (esperado {idFormaEnvio})."
                ),
            }

        result = {
            "ok": True,
            "idPedido": idPedido,
            "numeroPedido": numero_pedido,
            "transportador": {"id": got_tid, "nome": t.get("nome", "")},
            "formaEnvio": t.get("formaEnvio") or {},
            "formaFrete": t.get("formaFrete") or {},
            "fretePorConta": t.get("fretePorConta", ""),
        }
        _log("trocar_transportador_pedido", str(idPedido), "ok", started,
             {"tid": got_tid, "fe": got_fe, "numero": numero_pedido})
        return result

    count += 1
    return count
