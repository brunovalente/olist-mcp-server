"""Tools de etiqueta — geram ZPL + publicam no servico oneOS `temp` via HTTP.

Escopo desta rev:
- gerar_etiqueta_envio: pedido com entrega propria (transportador = PANA TEXTIL,
  idContato 776636263). Renderiza ZPL a partir de template embutido e publica
  via POST temp.pana.oneos.work/etiquetas/publish.

Pra Correios, continua usando `olist_erp_etiqueta_correios.sh` no oneOS — o
fluxo SIGEPweb exige Playwright na UI e ja esta coberto por outra ferramenta.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import unicodedata
import urllib.parse
import xml.dom.minidom
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP

from src.oauth import OAuthTokenManager


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

REMETENTE_CONTATO_ID = 776636263  # PANA TEXTIL LTDA (transportador = entrega propria)
TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _tz() -> ZoneInfo:
    return ZoneInfo(_env("ONEOS_TZ", "America/Fortaleza"))


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
        pass


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

async def _api_get(oauth: OAuthTokenManager, path: str) -> dict:
    import httpx
    base = _env("API_BASE_URL", "https://api.tiny.com.br/public-api/v3").rstrip("/")
    token = await oauth.get_access_token()
    async with httpx.AsyncClient(timeout=40.0) as client:
        r = await client.get(f"{base}{path}", headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return r.json()


async def _temp_publish(filename: str, content: bytes, label_type: str, reference: str) -> dict:
    import httpx
    url = _env("TEMP_PUBLISH_URL", "https://temp.pana.oneos.work/etiquetas/publish")
    token = _env("TEMP_PUBLISH_TOKEN")
    if not token:
        raise RuntimeError("TEMP_PUBLISH_TOKEN ausente — daemon temp_publish nao configurado")
    payload = {
        "filename": filename,
        "content_b64": base64.b64encode(content).decode("ascii"),
        "label_type": label_type,
        "reference": reference,
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            content=json.dumps(payload).encode("utf-8"),
        )
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# Sanitizacao + endereco (portado de olist_erp_etiqueta.sh)
# ---------------------------------------------------------------------------

def _sanitize(value: Optional[str], limit: Optional[int] = None) -> str:
    value = (value or "").replace("^", " ").replace("~", " ")
    value = unicodedata.normalize("NFD", value)
    value = "".join(c for c in value if unicodedata.category(c) != "Mn")
    value = re.sub(r"\s+", " ", value.strip())
    if limit and len(value) > limit:
        value = value[: max(0, limit - 3)].rstrip() + "..."
    return value


def _only_digits(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _format_cep_dest(raw) -> str:
    d = _only_digits(raw)
    return f"{d[:2]}.{d[2:5]}-{d[5:]}" if len(d) == 8 else _sanitize(raw)


def _format_cep_rem(raw) -> str:
    d = _only_digits(raw)
    return f"{d[:5]}-{d[5:]}" if len(d) == 8 else _sanitize(raw)


def _address_lines_propria(address: dict, dest: bool = False) -> tuple[str, str, str, str]:
    street = _sanitize(address.get("endereco", ""))
    numero = _sanitize(address.get("numero", ""))
    comp = _sanitize(address.get("complemento", ""))
    bairro = _sanitize(address.get("bairro", ""))
    cep = _format_cep_dest(address.get("cep", ""))
    mun = _sanitize(address.get("municipio", ""))
    uf = _sanitize(address.get("uf", ""))
    line1 = street
    if numero:
        line1 = f"{line1}, {numero}" if line1 else numero
    line2 = comp
    city_uf = f"{mun}/{uf}" if mun and uf else mun or uf
    if bairro and city_uf:
        max_b = max(0, 53 - len(city_uf) - 3)
        bairro = _sanitize(bairro, max_b) if max_b > 0 else ""
        line3 = f"{bairro} - {city_uf}" if bairro else city_uf
    else:
        line3 = bairro or city_uf or "-"
    line4 = f"CEP: {cep}" if cep else "-"
    s1 = _sanitize(line1 or "-", 42)
    s2 = _sanitize(line2, 53)
    s3 = _sanitize(line3, 53)
    s4 = _sanitize(line4, 42)
    if not s2.strip():
        return s1, s3, s4, ""
    return s1, s2, s3, s4


def _delivery_address(order: dict) -> dict:
    end_entrega = order.get("enderecoEntrega") or {}
    cliente = order.get("cliente") or {}
    inner = end_entrega.get("endereco") if isinstance(end_entrega, dict) else None
    if isinstance(inner, dict):
        return inner
    if isinstance(end_entrega, dict) and (end_entrega.get("cep") or end_entrega.get("municipio")):
        return {
            "endereco": inner or "",
            "numero": end_entrega.get("numero", ""),
            "complemento": end_entrega.get("complemento", ""),
            "bairro": end_entrega.get("bairro", ""),
            "municipio": end_entrega.get("municipio", ""),
            "cep": end_entrega.get("cep", ""),
            "uf": end_entrega.get("uf", ""),
            "pais": end_entrega.get("pais", ""),
        }
    return cliente.get("endereco") or {}


def _render_zpl_entrega_propria(order: dict, remitter: dict) -> str:
    template_path = TEMPLATE_DIR / "97x150-etiqueta-entrega-propria.zpl"
    template = template_path.read_text(encoding="utf-8")

    cliente = order.get("cliente") or {}
    end_entrega = order.get("enderecoEntrega") or {}
    address = _delivery_address(order)
    pedido_num = str(order.get("numeroPedido", ""))

    rem_address = (remitter.get("endereco") or {})
    rem_name = _sanitize(remitter.get("nome") or remitter.get("fantasia") or "Remetente", 30)

    dest_nome = _sanitize(
        (end_entrega.get("nome") if isinstance(end_entrega, dict) else None)
        or cliente.get("nome")
        or "Destinatario",
        34,
    )
    l1, l2, l3, l4 = _address_lines_propria(address, dest=True)

    _rl1, _rl2, _rl3, _rl4 = _address_lines_propria(rem_address)
    if not _rl4.strip():
        rl1, rl2, rl3 = _rl1, _rl2, _rl3
    else:
        rl1, rl2, rl3 = _rl1, _rl2, _sanitize(f"{_rl3} - {_rl4}", 53)

    address_for_map = " ".join(
        _sanitize(p) for p in [
            address.get("endereco"), address.get("numero"), address.get("complemento"),
            address.get("bairro"), address.get("municipio"), address.get("uf"), address.get("cep"),
        ] if _sanitize(p)
    )
    google_maps_url = "https://maps.google.com/?q=" + urllib.parse.quote_plus(address_for_map)
    waze_url = "https://waze.com/ul?q=" + urllib.parse.quote_plus(address_for_map)

    placeholders = {
        "{{PEDIDO_NUMERO}}": pedido_num,
        "{{DESTINATARIO_NOME}}": dest_nome,
        "{{DESTINATARIO_LINHA_1}}": l1,
        "{{DESTINATARIO_LINHA_2}}": l2,
        "{{DESTINATARIO_LINHA_3}}": l3,
        "{{DESTINATARIO_LINHA_4}}": l4,
        "{{REMETENTE_NOME}}": rem_name,
        "{{REMETENTE_LINHA_1}}": rl1,
        "{{REMETENTE_LINHA_2}}": rl2,
        "{{REMETENTE_LINHA_3}}": rl3,
        "{{WAZE_URL}}": waze_url,
        "{{GOOGLE_MAPS_URL}}": google_maps_url,
    }
    content = template
    for ph, val in placeholders.items():
        content = content.replace(ph, val)
    return content


# ---------------------------------------------------------------------------
# Driver async
# ---------------------------------------------------------------------------

async def _do_gerar_etiqueta_envio(oauth: OAuthTokenManager, numero_pedido: int) -> dict:
    started = time.time()
    numero_str = str(numero_pedido)

    page = await _api_get(oauth, f"/pedidos?numero={urllib.parse.quote(numero_str)}&limit=10")
    matches = [p for p in (page.get("itens") or []) if str(p.get("numeroPedido")) == numero_str]
    if not matches:
        _log("gerar_etiqueta_envio", numero_str, "nao-encontrado", started)
        return {"ok": False, "motivo": "pedido-nao-encontrado", "numero": numero_str}

    pedido_id = matches[0]["id"]
    order = await _api_get(oauth, f"/pedidos/{pedido_id}")

    transport = order.get("transportador") or {}
    transport_id = str(transport.get("id") or "")
    if transport_id != str(REMETENTE_CONTATO_ID):
        _log("gerar_etiqueta_envio", numero_str, "transportador-nao-suportado", started,
             {"transportador_id": transport_id})
        return {
            "ok": False,
            "motivo": "transportador-nao-suportado",
            "detalhe": (
                "MVP: tool cobre apenas entrega propria (transportador PANA TEXTIL id=776636263). "
                "Pra Correios, use olist_erp_etiqueta_correios.sh no oneOS."
            ),
            "transportador_id": transport_id,
        }

    remitter = await _api_get(oauth, f"/contatos/{REMETENTE_CONTATO_ID}")

    zpl = _render_zpl_entrega_propria(order, remitter)
    now_local = datetime.now(_tz())
    ts = now_local.strftime("%Y%m%d-%H%M%S")
    pedido_num = str(order.get("numeroPedido"))
    cliente_id = (order.get("cliente") or {}).get("id", "-")
    filename = f"pedido-{pedido_num}-etiqueta-entrega_propria-{ts}.zpl"
    reference = f"pedido {pedido_num} / cliente {cliente_id}"
    pub = await _temp_publish(filename, zpl.encode("utf-8"), "entrega_propria", reference)

    danfe_publication = None
    try:
        notas_page = await _api_get(oauth, f"/notas?idVenda={pedido_id}&limit=1")
        notas = notas_page.get("itens") or []
        if notas:
            nf_id = notas[0]["id"]
            xml_resp = await _api_get(oauth, f"/notas/{nf_id}/xml")
            xml_content = xml_resp.get("xmlNfe") or xml_resp.get("xml") or ""
            if xml_content:
                pretty = xml.dom.minidom.parseString(xml_content).toprettyxml(indent="  ")
                lines = pretty.split("\n")
                lines[0] = '<?xml version="1.0" ?>'
                content = "\n".join(lines)
                nf_match = re.search(r"<nNF>(\d+)</nNF>", content)
                nf_num = nf_match.group(1) if nf_match else "nf"
                xml_filename = f"pedido-{pedido_num}-nfe-{nf_num}-{now_local.strftime('%Y%m%d')}.xml"
                xml_ref = f"pedido {pedido_num} / NF-e {nf_num}"
                danfe_publication = await _temp_publish(
                    xml_filename, content.encode("utf-8"), "nf-e xml", xml_ref,
                )
    except Exception as exc:
        # DANFE e best-effort — etiqueta nao depende disso
        danfe_publication = {"error": str(exc)}

    result = {
        "ok": True,
        "label_type": "entrega_propria",
        "reference": reference,
        "pedido_num": pedido_num,
        "pedido_id": pedido_id,
        "publication": pub,
    }
    if danfe_publication is not None:
        result["danfe_publication"] = danfe_publication

    _log("gerar_etiqueta_envio", numero_str, "ok", started, {"filename": filename})
    return result


# ---------------------------------------------------------------------------
# Registro
# ---------------------------------------------------------------------------

def register_etiqueta_tools(mcp: FastMCP, get_oauth) -> int:
    count = 0

    @mcp.tool(
        name="gerar_etiqueta_envio",
        description=(
            "[Etiqueta] Gera etiqueta ZPL de envio de um pedido com entrega propria "
            "(transportador = PANA TEXTIL, idContato 776636263) e publica em "
            "temp.pana.oneos.work/etiquetas com TTL 10 dias. Tambem busca a NF-e do "
            "pedido (se ja emitida) e publica o XML pretty-printed como DANFE no mesmo "
            "endpoint. NAO cobre pedidos Correios — use olist_erp_etiqueta_correios.sh "
            "no oneOS pra etiqueta SIGEPweb nativa. Pre-checks: pedido existe via "
            "GET /pedidos?numero=, transportador.id == 776636263. Caso de uso PANA: "
            "operador encerra um pedido de entrega propria e quer ZPL + DANFE publicados "
            "numa unica chamada, com URLs estaveis pra anexar ao cliente. Retorna "
            "publication.file_url (ZPL), publication.preview_url (PNG do Labelary), "
            "publication.index_url (galeria), e danfe_publication.file_url se NF emitida."
        ),
    )
    async def gerar_etiqueta_envio(numero_pedido: int) -> dict:
        oauth = get_oauth()
        return await _do_gerar_etiqueta_envio(oauth, int(numero_pedido))

    count += 1
    return count
