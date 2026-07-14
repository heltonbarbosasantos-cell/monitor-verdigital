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

COL_CLIENTE  = 0   # Coluna A
COL_SITUACAO = 3   # Coluna D
COL_LINK     = 18  # Coluna S

TIMEOUT       = 8
TAMANHO_MIN   = 1000   # HTML menor que isso = página com problema
TEMPO_LENTO   = 3000   # ms — acima disso = lento

IGNORAR_LINKS = {
    '', 'nao achei', 'não achei', 'deixar em branco', '-',
    'nao tem site', 'não tem site', 'sem site', 'em desenvolvimento'
}

# ─────────────────────────────────────────────
# FUNÇÕES
# ─────────────────────────────────────────────

def buscar_clientes():
    """Lê planilha do Google Sheets e retorna lista de clientes ativos com site"""
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={SHEET_GID}"
    print(f"📋 Buscando planilha...")

    try:
        resp = requests.get(url, timeout=30, allow_redirects=True)
        resp.raise_for_status()
    except Exception as e:
        print(f"❌ Erro ao ler planilha: {e}")
        return []

    clientes = []
    linhas   = resp.text.strip().split('\n')

    for i, linha in enumerate(linhas):
        if i == 0:
            continue  # pula cabeçalho

        # Parse CSV simples
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

        nome     = campos[COL_CLIENTE].strip()  if len(campos) > COL_CLIENTE  else ''
        situacao = campos[COL_SITUACAO].strip() if len(campos) > COL_SITUACAO else ''
        link     = campos[COL_LINK].strip()     if len(campos) > COL_LINK     else ''

        if not nome:
            continue
        if situacao.lower() != 'ativo':
            continue
        if link.lower() in IGNORAR_LINKS:
            continue

        # Pega só primeira linha se tiver quebra
        link = link.split('\n')[0].strip()

        if not link.startswith('http'):
            link = 'https://' + link

        clientes.append({"cliente": nome, "site": link.rstrip('/')})

    print(f"✅ {len(clientes)} clientes ativos encontrados")
    return clientes


def checar_site(url):
    """Verifica se o site está online, mede tempo e verifica tamanho do HTML"""
    headers = {"User-Agent": "MonitorBot/1.0"}

    try:
        inicio = time.time()
        resp   = requests.get(url, timeout=TIMEOUT, headers=headers,
                              verify=False, allow_redirects=True)
        tempo_ms = round((time.time() - inicio) * 1000)
        codigo   = resp.status_code
        tamanho  = len(resp.text)

        if codigo >= 400:
            return {"status": "fora", "detalhe": f"HTTP {codigo}", "tempo_ms": tempo_ms}

        if tamanho < TAMANHO_MIN:
            return {"status": "fora", "detalhe": f"Página vazia ({tamanho} chars)", "tempo_ms": tempo_ms}

        if tempo_ms > TEMPO_LENTO:
            return {"status": "lento", "detalhe": f"{tempo_ms}ms — {tamanho} chars", "tempo_ms": tempo_ms}

        return {"status": "ok", "detalhe": f"HTTP {codigo} — {tempo_ms}ms", "tempo_ms": tempo_ms}

    except requests.exceptions.ConnectionError as e:
        return {"status": "fora", "detalhe": f"Sem conexão: {str(e)[:80]}", "tempo_ms": None}
    except requests.exceptions.Timeout:
        return {"status": "fora", "detalhe": f"Timeout após {TIMEOUT}s", "tempo_ms": None}
    except Exception as e:
        return {"status": "fora", "detalhe": str(e)[:80], "tempo_ms": None}


def enviar_whatsapp(mensagem):
    """Envia alerta no WhatsApp via Callmebot"""
    if not WHATSAPP_NUMERO or not WHATSAPP_APIKEY:
        return
    try:
        url = f"https://api.callmebot.com/whatsapp.php?phone={WHATSAPP_NUMERO}&text={requests.utils.quote(mensagem)}&apikey={WHATSAPP_APIKEY}"
        requests.get(url, timeout=10)
    except Exception as e:
        print(f"[WhatsApp] Erro: {e}")


def carregar_status_anterior():
    """Carrega o status.json anterior pra detectar mudanças"""
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
    resultados       = []
    alertas          = []
    total_ok         = 0
    total_lento      = 0
    total_erro       = 0

    for cliente in clientes:
        nome = cliente["cliente"]
        site = cliente["site"]

        print(f"  🔍 {nome} ({site})")
        res    = checar_site(site)
        status = res["status"]

        if status == "ok":    total_ok += 1
        elif status == "lento": total_ok += 1; total_lento += 1
        elif status == "fora":  total_erro += 1

        # Detecta mudança de estado pra não spammar WhatsApp
        ant_status = status_anterior.get(nome, {}).get("status", "ok")

        if status == "fora" and ant_status != "fora":
            alertas.append(f"🔴 SITE FORA: {nome}\n{site}\n{res['detalhe']}")
        if status == "lento" and ant_status != "lento":
            alertas.append(f"🟡 SITE LENTO: {nome}\n{site}\n{res['detalhe']}")
        if status == "ok" and ant_status == "fora":
            alertas.append(f"✅ SITE VOLTOU: {nome}\n{site}")

        resultados.append({
            "cliente":  nome,
            "site_url": site,
            "status":   status,
            "site_info": res["detalhe"],
            "tempo_ms": res["tempo_ms"],
        })

        time.sleep(0.5)  # 0.5s entre cada verificação

    # Envia alertas WhatsApp
    for alerta in alertas:
        print(f"[ALERTA] {alerta}")
        enviar_whatsapp(alerta)

    # Salva status.json
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
