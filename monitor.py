#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Monitor de Sites — Ver Digital
Roda via GitHub Actions a cada 15 minutos
"""

import json
import os
import time
import requests
from datetime import datetime

# ─────────────────────────────────────────────
# CONFIGURAÇÕES (via GitHub Secrets)
# ─────────────────────────────────────────────

SHEET_ID        = os.environ.get("SHEET_ID", "")
SHEET_GID       = os.environ.get("SHEET_GID", "675222443")
WHATSAPP_NUMERO = os.environ.get("WHATSAPP_NUMERO", "")
WHATSAPP_APIKEY = os.environ.get("WHATSAPP_APIKEY", "")

COL_CLIENTE     = 0    # Coluna A
COL_SITUACAO    = 3    # Coluna D
COL_LINK        = 18   # Coluna S
COL_HOSPEDAGEM  = 19   # Coluna T

TIMEOUT       = 10
TAMANHO_MIN   = 1000
TEMPO_LENTO   = 3000

# URL pública onde o acknowledged.json fica publicado (mesma do painel)
ACK_URL = "https://heltonbarbosasantos-cell.github.io/monitor-verdigital/acknowledged.json"

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

# ─────────────────────────────────────────────
# FUNÇÕES
# ─────────────────────────────────────────────

def normalizar(texto):
    return texto.strip().lower()


def buscar_acknowledged():
    """Busca a lista de clientes com alertas silenciados (marcados como 'ciente')"""
    try:
        resp = requests.get(ACK_URL, timeout=10)
        if resp.status_code == 200:
            return set(resp.json())
    except Exception:
        pass
    return set()


def buscar_clientes():
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={SHEET_GID}"
    print(f"📋 Buscando planilha...")

    try:
        resp = requests.get(url, timeout=30, allow_redirects=True)
        resp.raise_for_status()
        resp.encoding = 'utf-8'
        texto_csv = resp.text
    except Exception as e:
        print(f"❌ Erro ao ler planilha: {e}")
        return []

    clientes = []
    linhas   = texto_csv.strip().split('\n')

    for i, linha in enumerate(linhas):
        if i == 0:
            continue

        campos = []
        campo_atual = ''
        dentro_aspas = False
        for char in linha:
            if char == '"':
                dentro_aspas = not dentro_aspas
            elif char == ',' and not dentro_aspas:
                campos.append(campo_atual.strip())
                campo_atual = ''
            else:
                campo_atual += char
        campos.append(campo_atual.strip())

        nome        = campos[COL_CLIENTE].strip()       if len(campos) > COL_CLIENTE      else ''
        situacao    = campos[COL_SITUACAO].strip()       if len(campos) > COL_SITUACAO     else ''
        link        = campos[COL_LINK].strip()           if len(campos) > COL_LINK         else ''
        hospedagem  = campos[COL_HOSPEDAGEM].strip()     if len(campos) > COL_HOSPEDAGEM   else ''

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


def _tentar_requisicao(url):
    try:
        inicio = time.time()
        resp   = requests.get(url, timeout=TIMEOUT, headers=HEADERS_NAVEGADOR,
                              verify=False, allow_redirects=True)
        tempo_ms = round((time.time() - inicio) * 1000)
        codigo   = resp.status_code
        tamanho  = len(resp.text)

        if codigo >= 400:
            return False, {"status": "fora", "detalhe": f"HTTP {codigo}", "tempo_ms": tempo_ms}
        if tamanho < TAMANHO_MIN:
            return False, {"status": "fora", "detalhe": f"Página vazia ({tamanho} chars)", "tempo_ms": tempo_ms}
        if tempo_ms > TEMPO_LENTO:
            return True, {"status": "lento", "detalhe": f"{tempo_ms}ms — {tamanho} chars", "tempo_ms": tempo_ms}
        return True, {"status": "ok", "detalhe": f"HTTP {codigo} — {tempo_ms}ms", "tempo_ms": tempo_ms}

    except requests.exceptions.ConnectionError as e:
        return False, {"status": "fora", "detalhe": f"Sem conexão: {str(e)[:80]}", "tempo_ms": None}
    except requests.exceptions.Timeout:
        return False, {"status": "fora", "detalhe": f"Timeout após {TIMEOUT}s", "tempo_ms": None}
    except Exception as e:
        return False, {"status": "fora", "detalhe": str(e)[:80], "tempo_ms": None}


def checar_site(url):
    sucesso, resultado = _tentar_requisicao(url)
    if sucesso:
        return resultado

    time.sleep(4)
    sucesso2, resultado2 = _tentar_requisicao(url)

    if sucesso2:
        resultado2["detalhe"] += " (confirmado OK na 2ª tentativa)"
        return resultado2

    return resultado2


def enviar_whatsapp(mensagem):
    if not WHATSAPP_NUMERO or not WHATSAPP_APIKEY:
        return
    try:
        url = f"https://api.callmebot.com/whatsapp.php?phone={WHATSAPP_NUMERO}&text={requests.utils.quote(mensagem)}&apikey={WHATSAPP_APIKEY}"
        requests.get(url, timeout=10)
    except Exception as e:
        print(f"[WhatsApp] Erro: {e}")


def carregar_status_anterior():
    try:
        with open("output/status.json", "r", encoding="utf-8") as f:
            dados = json.load(f)
            return {c["cliente"]: c for c in dados.get("clientes", [])}
    except Exception:
        return {}


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    import urllib3
    urllib3.disable_warnings()

    os.makedirs("output", exist_ok=True)

    clientes         = buscar_clientes()
    status_anterior  = carregar_status_anterior()
    acknowledged     = buscar_acknowledged()  # clientes com alerta silenciado
    resultados       = []
    alertas          = []
    total_ok         = 0
    total_lento      = 0
    total_erro       = 0

    for cliente in clientes:
        nome       = cliente["cliente"]
        site       = cliente["site"]
        hospedagem = cliente["hospedagem"]

        print(f"  🔍 {nome} ({site})")
        res    = checar_site(site)
        status = res["status"]

        if status == "ok":    total_ok += 1
        elif status == "lento": total_ok += 1; total_lento += 1
        elif status == "fora":  total_erro += 1

        ant_status = status_anterior.get(nome, {}).get("status", "ok")
        esta_silenciado = nome in acknowledged

        # Só alerta no WhatsApp quando o site está FORA (lento não avisa mais)
        # e só se o cliente não estiver marcado como "ciente"
        if status == "fora" and ant_status != "fora" and not esta_silenciado:
            alertas.append(f"🔴 SITE FORA: {nome}\n{site}\n{res['detalhe']}")
        if status == "ok" and ant_status == "fora":
            alertas.append(f"✅ SITE VOLTOU: {nome}\n{site}")

        resultados.append({
            "cliente":    nome,
            "site_url":   site,
            "hospedagem": hospedagem,
            "status":     status,
            "site_info":  res["detalhe"],
            "tempo_ms":   res["tempo_ms"],
            "silenciado": esta_silenciado,
        })

        time.sleep(0.5)

    for alerta in alertas:
        print(f"[ALERTA] {alerta}")
        enviar_whatsapp(alerta)

    # Limpa a lista de "silenciados" — mantém só quem ainda está fora.
    # Assim que o site volta ao normal, ele sai da lista automaticamente
    # e volta a alertar numa próxima queda.
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
        "clientes":           resultados,
    }

    with open("output/status.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✅ Concluído: {total_ok} ok ({total_lento} lentos), {total_erro} com problema")


if __name__ == "__main__":
    main()
