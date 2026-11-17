#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Monitor de Sites — Ver Digital
Roda via GitHub Actions a cada 15 minutos

Verifica:
  1. Se o site responde (HTTP, tamanho, página suspensa)
  2. Se o layout carrega (CSS/JS do próprio site respondem) — pega site "bagunçado"
  3. Se a planilha foi lida corretamente (avisa se vier sem clientes)
"""

import csv
import io
import json
import os
import re
import smtplib
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.mime.text import MIMEText
from urllib.parse import urljoin, urlparse

import requests

# ─────────────────────────────────────────────
# CONFIGURAÇÕES (via GitHub Secrets)
# ─────────────────────────────────────────────

SHEET_ID        = os.environ.get("SHEET_ID", "")
SHEET_GID       = os.environ.get("SHEET_GID", "675222443")
WHATSAPP_NUMERO = os.environ.get("WHATSAPP_NUMERO", "")
WHATSAPP_APIKEY = os.environ.get("WHATSAPP_APIKEY", "")

EMAIL_REMETENTE = os.environ.get("EMAIL_REMETENTE", "")
EMAIL_SENHA     = os.environ.get("EMAIL_SENHA", "")
EMAIL_DESTINO   = os.environ.get("EMAIL_DESTINO", "")
EMAIL_SMTP_HOST = os.environ.get("EMAIL_SMTP_HOST", "smtp.gmail.com")
EMAIL_SMTP_PORT = int(os.environ.get("EMAIL_SMTP_PORT", "587"))

# As colunas são localizadas pelo NOME do cabeçalho da planilha.
# Se alguém excluir/inserir colunas, o monitor continua funcionando.
# Os números abaixo só são usados se o cabeçalho não for encontrado.
CABECALHOS = {
    "cliente":    ({"cliente"},                          0),
    "situacao":   ({"situacao"},                         3),
    "link":       ({"link do site", "link site"},        17),
    "hospedagem": ({"cpanel", "hospedagem"},             18),
}

TIMEOUT       = 10
TAMANHO_MIN   = 1000
TEMPO_LENTO   = 6000  # GitHub Actions roda fora do Brasil — latência natural é maior

# Verificação de layout (CSS/JS)
VERIFICAR_LAYOUT   = True
ASSET_TIMEOUT      = 8
MAX_CSS_POR_SITE   = 25
MAX_JS_POR_SITE    = 15
MIN_JS_FALHANDO    = 2   # CSS falhando = alerta; JS só alerta se >= este número

ACK_URL    = "https://heltonbarbosasantos-cell.github.io/monitor-verdigital/acknowledged.json"
STATUS_URL = "https://heltonbarbosasantos-cell.github.io/monitor-verdigital/status.json"

HEADERS_NAVEGADOR = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

IGNORAR_LINKS = {
    '', 'nao achei', 'não achei', 'deixar em branco', '-',
    'nao tem site', 'não tem site', 'sem site', 'em desenvolvimento'
}

INDICADORES_SUSPENSAO_URL = [
    'suspendedpage.cgi', 'account-suspended', 'accountsuspended',
    'cgi-sys/suspendedpage',
]

INDICADORES_SUSPENSAO_TEXTO = [
    'conta precisa de atenção', 'conta foi suspensa', 'account has been suspended',
    'this account has been suspended', 'entre em contato com o seu provedor',
    'domain has expired', 'domínio expirou', 'website is currently unavailable',
]

# ─────────────────────────────────────────────
# FUNÇÕES
# ─────────────────────────────────────────────

def normalizar(texto):
    return (texto or '').strip().lower()

def _sem_acento(texto):
    texto = unicodedata.normalize("NFD", texto or '')
    return ''.join(c for c in texto if unicodedata.category(c) != 'Mn')

def _norm_cabecalho(texto):
    return re.sub(r'\s+', ' ', _sem_acento(texto).strip().lower())

def buscar_acknowledged():
    try:
        resp = requests.get(ACK_URL, timeout=10)
        if resp.status_code == 200:
            return set(resp.json())
    except Exception:
        pass
    return set()

def carregar_status_anterior():
    """
    Busca o resultado da execução ANTERIOR publicado no GitHub Pages.
    Cada execução do GitHub Actions começa do zero, então não dá pra ler
    arquivo local — precisa buscar o status.json já publicado.
    """
    try:
        resp = requests.get(STATUS_URL, timeout=15)
        if resp.status_code == 200:
            dados = resp.json()
            return {c["cliente"]: c for c in dados.get("clientes", [])}
    except Exception as e:
        print(f"[Aviso] Não foi possível carregar status anterior: {e}")
    return {}

def achar_colunas(cabecalho):
    """Descobre o índice de cada coluna pelo nome do cabeçalho."""
    normalizados = [_norm_cabecalho(h) for h in cabecalho]
    indices = {}
    for chave, (nomes, fallback) in CABECALHOS.items():
        indice = None
        for i, h in enumerate(normalizados):
            if h in nomes:
                indice = i
                break
        if indice is None:
            print(f"[Aviso] Cabeçalho '{'/'.join(sorted(nomes))}' não encontrado — usando coluna fixa {fallback}")
            indice = fallback
        indices[chave] = indice
    print("[Config] Colunas: " + ", ".join(f"{k}={v}" for k, v in indices.items()))
    return indices

def buscar_clientes():
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={SHEET_GID}"
    print("📋 Buscando planilha...")

    try:
        resp = requests.get(url, timeout=30, allow_redirects=True)
        resp.raise_for_status()
        resp.encoding = 'utf-8'
        texto_csv = resp.text
    except Exception as e:
        print(f"❌ Erro ao ler planilha: {e}")
        return []

    linhas = list(csv.reader(io.StringIO(texto_csv)))
    if not linhas:
        return []

    col = achar_colunas(linhas[0])
    clientes = []

    for campos in linhas[1:]:
        def pegar(chave):
            i = col[chave]
            return campos[i].strip() if len(campos) > i else ''

        nome       = pegar("cliente")
        situacao   = pegar("situacao")
        link       = pegar("link")
        hospedagem = pegar("hospedagem")

        if not nome:
            continue
        if normalizar(situacao) != 'ativo':
            continue

        link_normalizado = normalizar(link)
        if link_normalizado in IGNORAR_LINKS:
            continue
        if 'tem site' in link_normalizado or 'nao achei' in link_normalizado or 'não achei' in link_normalizado:
            continue

        link = link.split('\n')[0].strip()

        if not link or ' ' in link or '.' not in link:
            continue

        if not link.startswith('http'):
            link = 'https://' + link

        if not hospedagem or normalizar(hospedagem) in ('-', 'none'):
            hospedagem = ''

        clientes.append({
            "cliente": nome,
            "site": link.rstrip('/'),
            "hospedagem": hospedagem,
        })

    print(f"✅ {len(clientes)} clientes ativos encontrados")
    return clientes

def _detectar_pagina_suspensa(url_final, texto_pagina):
    url_lower = (url_final or '').lower()
    for indicador in INDICADORES_SUSPENSAO_URL:
        if indicador in url_lower:
            return True

    texto_lower = (texto_pagina or '')[:5000].lower()
    for indicador in INDICADORES_SUSPENSAO_TEXTO:
        if indicador in texto_lower:
            return True

    return False

# ─────────────────────────────────────────────
# VERIFICAÇÃO DE LAYOUT (CSS / JS)
# ─────────────────────────────────────────────

def _host(url):
    return (urlparse(url).netloc or '').lower().split(':')[0].replace('www.', '', 1)

def extrair_assets(html, url_pagina):
    """Lista os CSS e JS do PRÓPRIO site chamados pela página."""
    host = _host(url_pagina)
    css, js = [], []

    for tag in re.findall(r'<link\b[^>]*>', html, flags=re.I):
        if not re.search(r'rel\s*=\s*["\']?[^"\'>]*stylesheet', tag, flags=re.I):
            continue
        m = re.search(r'href\s*=\s*["\']([^"\']+)["\']', tag, flags=re.I)
        if m:
            css.append(m.group(1))

    for m in re.finditer(r'<script\b[^>]*?\bsrc\s*=\s*["\']([^"\']+)["\']', html, flags=re.I):
        js.append(m.group(1))

    def resolver(lista, limite):
        saida, vistos = [], set()
        for href in lista:
            href = href.strip().replace('&#038;', '&').replace('&amp;', '&')
            if href.startswith(('data:', 'javascript:', '#')):
                continue
            absoluta = urljoin(url_pagina, href)
            if not absoluta.startswith(('http://', 'https://')):
                continue
            if _host(absoluta) != host:
                continue  # CDN/terceiros: falha deles não é problema do site
            if absoluta in vistos:
                continue
            vistos.add(absoluta)
            saida.append(absoluta)
            if len(saida) >= limite:
                break
        return saida

    return resolver(css, MAX_CSS_POR_SITE), resolver(js, MAX_JS_POR_SITE)

def _checar_asset(url):
    """
    Retorna uma descrição do problema se o arquivo está quebrado, senão None.
    Timeout/erro de conexão é inconclusivo (não conta como quebrado).
    """
    try:
        resp = requests.get(
            url, timeout=ASSET_TIMEOUT, headers=HEADERS_NAVEGADOR,
            verify=False, stream=True, allow_redirects=True,
        )
        try:
            codigo = resp.status_code
            tipo = (resp.headers.get("Content-Type") or "").lower()
            if codigo >= 400:
                return f"HTTP {codigo}"
            pedaco = next(resp.iter_content(2048), b'')
        finally:
            resp.close()
        if not pedaco:
            return "arquivo vazio"
        if "text/html" in tipo:
            return "devolve página HTML no lugar do arquivo"
        return None
    except Exception:
        return None

def _nome_curto(url):
    caminho = urlparse(url).path or url
    return caminho.rsplit('/', 1)[-1] or caminho

def verificar_layout(html, url_pagina):
    """
    Confere se CSS/JS do site carregam.
    Retorna {"quebrado": bool, "css": [...], "js": [...], "resumo": str}
    """
    css_urls, js_urls = extrair_assets(html, url_pagina)
    todos = [("css", u) for u in css_urls] + [("js", u) for u in js_urls]
    if not todos:
        return {"quebrado": False, "css": [], "js": [], "resumo": ""}

    def rodar(itens):
        with ThreadPoolExecutor(max_workers=8) as pool:
            erros = list(pool.map(lambda it: _checar_asset(it[1]), itens))
        return [(it, e) for it, e in zip(itens, erros) if e]

    falhas = rodar(todos)

    if falhas:
        time.sleep(5)  # segunda chance: descarta falha momentânea
        falhas = rodar([it for it, _ in falhas])

    falhas_css = [(u, e) for (t, u), e in falhas if t == "css"]
    falhas_js  = [(u, e) for (t, u), e in falhas if t == "js"]

    quebrado = bool(falhas_css) or len(falhas_js) >= MIN_JS_FALHANDO

    partes = []
    if falhas_css:
        ex = ", ".join(f"{_nome_curto(u)} ({e})" for u, e in falhas_css[:2])
        partes.append(f"{len(falhas_css)} CSS não carrega: {ex}")
    if falhas_js:
        ex = ", ".join(f"{_nome_curto(u)} ({e})" for u, e in falhas_js[:2])
        partes.append(f"{len(falhas_js)} JS não carrega: {ex}")

    return {
        "quebrado": quebrado,
        "css": [u for u, _ in falhas_css],
        "js":  [u for u, _ in falhas_js],
        "resumo": " | ".join(partes),
    }

# ─────────────────────────────────────────────
# VERIFICAÇÃO DO SITE
# ─────────────────────────────────────────────

def _tentar_requisicao(url):
    try:
        inicio = time.time()
        resp   = requests.get(url, timeout=TIMEOUT, headers=HEADERS_NAVEGADOR,
                              verify=False, allow_redirects=True)
        tempo_ms = round((time.time() - inicio) * 1000)
        codigo   = resp.status_code
        texto    = resp.text
        tamanho  = len(texto)

        if codigo >= 400:
            return False, {"status": "fora", "detalhe": f"HTTP {codigo}", "tempo_ms": tempo_ms}

        if tamanho < TAMANHO_MIN:
            return False, {"status": "fora", "detalhe": f"Página vazia ({tamanho} chars)", "tempo_ms": tempo_ms}

        if _detectar_pagina_suspensa(resp.url, texto):
            return False, {
                "status": "fora",
                "detalhe": "Página de conta suspensa / domínio parqueado detectada (servidor responde, mas não é o site real)",
                "tempo_ms": tempo_ms
            }

        extra = {"_html": texto, "_url": resp.url}

        if tempo_ms > TEMPO_LENTO:
            return True, {"status": "lento", "detalhe": f"{tempo_ms}ms — {tamanho} chars", "tempo_ms": tempo_ms, **extra}

        return True, {"status": "ok", "detalhe": f"HTTP {codigo} — {tempo_ms}ms", "tempo_ms": tempo_ms, **extra}

    except requests.exceptions.ConnectionError as e:
        return False, {"status": "fora", "detalhe": f"Sem conexão: {str(e)[:80]}", "tempo_ms": None}
    except requests.exceptions.Timeout:
        return False, {"status": "fora", "detalhe": f"Timeout após {TIMEOUT}s", "tempo_ms": None}
    except Exception as e:
        return False, {"status": "fora", "detalhe": str(e)[:80], "tempo_ms": None}

def checar_site(url, pular_retentativas=False):
    esperas = [0] if pular_retentativas else [0, 4, 8]

    resultado = None
    for i, espera in enumerate(esperas):
        if espera:
            time.sleep(espera)
        sucesso, resultado = _tentar_requisicao(url)
        if sucesso:
            if i > 0:
                resultado["detalhe"] += f" (confirmado OK na tentativa {i+1})"

            html = resultado.pop("_html", "")
            url_final = resultado.pop("_url", url)
            resultado["layout"] = None
            if VERIFICAR_LAYOUT:
                try:
                    resultado["layout"] = verificar_layout(html, url_final)
                except Exception as e:
                    print(f"[Layout] Erro ao verificar {url}: {e}")
            return resultado

    if not pular_retentativas and resultado and "Sem conexão" in resultado.get("detalhe", ""):
        resultado["detalhe"] += " — se o site abrir normal no navegador, pode ser bloqueio de IP do monitor, não queda real"

    return resultado

def eh_erro_de_conexao(detalhe):
    if not detalhe:
        return False
    return "Sem conexão" in detalhe or "Timeout" in detalhe

# ─────────────────────────────────────────────
# ALERTAS
# ─────────────────────────────────────────────

def enviar_whatsapp(mensagem):
    if not WHATSAPP_NUMERO or not WHATSAPP_APIKEY:
        return
    try:
        url = f"https://api.callmebot.com/whatsapp.php?phone={WHATSAPP_NUMERO}&text={requests.utils.quote(mensagem)}&apikey={WHATSAPP_APIKEY}"
        requests.get(url, timeout=10)
    except Exception as e:
        print(f"[WhatsApp] Erro: {e}")

def enviar_email(assunto, mensagem):
    if not EMAIL_REMETENTE or not EMAIL_SENHA or not EMAIL_DESTINO:
        print("[Email] Não enviado: faltam credenciais")
        return
    try:
        msg = MIMEText(mensagem)
        msg["Subject"] = assunto
        msg["From"]    = EMAIL_REMETENTE
        msg["To"]      = EMAIL_DESTINO

        with smtplib.SMTP(EMAIL_SMTP_HOST, EMAIL_SMTP_PORT, timeout=15) as servidor:
            servidor.starttls()
            servidor.login(EMAIL_REMETENTE, EMAIL_SENHA)
            servidor.sendmail(EMAIL_REMETENTE, [EMAIL_DESTINO], msg.as_string())
        print(f"[Email] ✅ Enviado com sucesso para {EMAIL_DESTINO}")
    except Exception as e:
        print(f"[Email] ❌ Erro ao enviar: {type(e).__name__}: {e}")

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    import urllib3
    urllib3.disable_warnings()

    os.makedirs("output", exist_ok=True)

    print(f"[Config] WhatsApp configurado: {'sim' if (WHATSAPP_NUMERO and WHATSAPP_APIKEY) else 'não'}")
    print(f"[Config] Email configurado: {'sim' if (EMAIL_REMETENTE and EMAIL_SENHA and EMAIL_DESTINO) else 'não'}")

    clientes = buscar_clientes()

    if not clientes:
        aviso = ("⚠️ MONITOR SEM CLIENTES\n"
                 "Não consegui ler nenhum cliente ativo da planilha. "
                 "Pode ter mudado o nome de uma coluna (Cliente, Situação, Link do site), "
                 "o compartilhamento da planilha, ou a aba. "
                 "Nenhum site está sendo monitorado até isso ser corrigido.")
        print("[ALERTA] " + aviso)
        enviar_whatsapp(aviso)
        enviar_email("Monitor Ver Digital — ⚠️ monitor sem clientes", aviso)
        sys.exit(1)  # falha a execução: o painel mantém o último status bom

    status_anterior  = carregar_status_anterior()
    acknowledged     = buscar_acknowledged()
    print(f"[Config] Status anterior carregado: {len(status_anterior)} clientes")

    resultados         = []
    sites_fora         = []
    sites_layout       = []
    sites_voltaram     = []
    total_ok           = 0
    total_lento        = 0
    total_erro         = 0
    total_verificando  = 0

    for cliente in clientes:
        nome       = cliente["cliente"]
        site       = cliente["site"]
        hospedagem = cliente["hospedagem"]

        esta_silenciado = nome in acknowledged

        print(f"  🔍 {nome} ({site})" + (" [silenciado — check rápido]" if esta_silenciado else ""))
        res    = checar_site(site, pular_retentativas=esta_silenciado)
        status = res["status"]
        tipo   = ""

        ant_info = status_anterior.get(nome, {})
        falhas_consecutivas_anterior = ant_info.get("falhas_consecutivas", 0)
        falhas_layout_anterior       = ant_info.get("falhas_layout", 0)
        falhas_layout                = 0

        # Site responde, mas o layout pode estar quebrado (CSS/JS não carregam)
        layout = res.get("layout")
        if status in ("ok", "lento") and layout and layout["quebrado"]:
            falhas_layout = falhas_layout_anterior + 1
            if falhas_layout < 2:
                status = "verificando"
                res["detalhe"] = f"Layout suspeito (1ª ocorrência, confirmando na próxima verificação): {layout['resumo']}"
            else:
                status = "fora"
                tipo   = "layout"
                res["detalhe"] = f"Layout quebrado (site no ar, mas CSS/JS não carregam): {layout['resumo']}"

        if status == "fora" and tipo != "layout" and eh_erro_de_conexao(res.get("detalhe", "")) and not esta_silenciado:
            falhas_consecutivas = falhas_consecutivas_anterior + 1
            if falhas_consecutivas < 2:
                status = "verificando"
                res["detalhe"] = f"Falha de conexão ({falhas_consecutivas}ª ocorrência, confirmando na próxima verificação): {res['detalhe']}"
        else:
            falhas_consecutivas = 0

        if status == "ok":           total_ok += 1
        elif status == "lento":       total_lento += 1
        elif status == "fora":        total_erro += 1
        elif status == "verificando": total_verificando += 1

        ant_status = ant_info.get("status", "ok")

        if status == "fora" and ant_status != "fora" and not esta_silenciado:
            if tipo == "layout":
                sites_layout.append(f"🟠 {nome}\n{site}\n{res['detalhe']}")
            else:
                sites_fora.append(f"🔴 {nome}\n{site}\n{res['detalhe']}")
        if status == "ok" and ant_status == "fora":
            sites_voltaram.append(f"✅ {nome} — {site}")

        resultados.append({
            "cliente":             nome,
            "site_url":            site,
            "hospedagem":          hospedagem,
            "status":              status,
            "tipo":                tipo,
            "site_info":           res["detalhe"],
            "tempo_ms":            res["tempo_ms"],
            "silenciado":          esta_silenciado,
            "falhas_consecutivas": falhas_consecutivas,
            "falhas_layout":       falhas_layout,
        })

        time.sleep(0.5)

    partes_mensagem = []
    if sites_fora:
        partes_mensagem.append(f"🔴 SITES FORA ({len(sites_fora)}):\n\n" + "\n\n".join(sites_fora))
    if sites_layout:
        partes_mensagem.append(f"🟠 LAYOUT QUEBRADO ({len(sites_layout)}) — site no ar, mas desconfigurado:\n\n" + "\n\n".join(sites_layout))
    if sites_voltaram:
        partes_mensagem.append(f"✅ SITES QUE VOLTARAM ({len(sites_voltaram)}):\n\n" + "\n".join(sites_voltaram))

    if partes_mensagem:
        mensagem_completa = "\n\n".join(partes_mensagem)
        print(f"[ALERTA] Enviando resumo — {len(sites_fora)} fora, {len(sites_layout)} layout, {len(sites_voltaram)} voltaram")

        enviar_whatsapp(mensagem_completa)

        pedacos = []
        if sites_fora:
            pedacos.append(f"{len(sites_fora)} fora do ar")
        if sites_layout:
            pedacos.append(f"{len(sites_layout)} com layout quebrado")
        if sites_voltaram:
            pedacos.append(f"{len(sites_voltaram)} recuperado(s)")
        icone = "🔴" if sites_fora else ("🟠" if sites_layout else "✅")
        enviar_email(f"Monitor Ver Digital — {icone} " + ", ".join(pedacos), mensagem_completa)
    else:
        print("[ALERTA] Nenhuma mudança de status — nenhum alerta enviado")

    nomes_fora_agora = {r["cliente"] for r in resultados if r["status"] == "fora"}
    acknowledged_atualizado = sorted(acknowledged & nomes_fora_agora)

    with open("output/acknowledged.json", "w", encoding="utf-8") as f:
        json.dump(acknowledged_atualizado, f, ensure_ascii=False, indent=2)

    output = {
        "ultima_verificacao": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        "total_clientes":     len(clientes),
        "total_ok":           total_ok,
        "total_lento":        total_lento,
        "total_erro":         total_erro,
        "total_verificando":  total_verificando,
        "clientes":           resultados,
    }

    with open("output/status.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✅ Concluído: {total_ok} ok ({total_lento} lentos), {total_erro} com problema, {total_verificando} confirmando")

if __name__ == "__main__":
    main()
