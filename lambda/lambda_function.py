"""
cnpj-rf-watchdog

Roda 1x por dia (23:30 America/Sao_Paulo, via EventBridge Scheduler).
Compara a origem (WebDAV da Receita) com o que ja esta no bucket, a partir de
CNPJ_DESDE. Se faltar arquivo, liga a EC2 cnpj-rf-worker. A EC2 baixa, envia,
grava o estado e chama StopInstances.

Sem dependencias externas: usa só stdlib + boto3 (ja presente no runtime).
Nao usa VPC: precisa de internet para o PROPFIND e de rota para o S3.

Variaveis de ambiente da funcao:
  CNPJ_BUCKET        (obrigatoria)
  CNPJ_WEBDAV_TOKEN  (obrigatoria) - trecho do link publico da Receita
  CNPJ_DESDE         (padrao 2026-08)
  CNPJ_WEBDAV_BASE   (padrao https://arquivos.receitafederal.gov.br/public.php/dav/files)
"""

import base64
import json
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import boto3

BUCKET = os.environ.get("CNPJ_BUCKET", "")
PREFIX = os.environ.get("CNPJ_PREFIX", "cnpj")
DESDE = os.environ.get("CNPJ_DESDE", "2026-08")
BASE = os.environ.get(
    "CNPJ_WEBDAV_BASE",
    "https://arquivos.receitafederal.gov.br/public.php/dav/files",
)
TOKEN = os.environ.get("CNPJ_WEBDAV_TOKEN", "")
NOME_WORKER = "cnpj-rf-worker"
TAG_PROJETO = "cnpj-s3"
MAX_HORAS_RODANDO = 12
NS = {"d": "DAV:"}
MES_RE = re.compile(r"^\d{4}-\d{2}$")

if not BUCKET or not TOKEN:
    raise SystemExit("defina CNPJ_BUCKET e CNPJ_WEBDAV_TOKEN no ambiente da funcao")

s3 = boto3.client("s3")
ec2 = boto3.client("ec2")


# --------------------------------------------------------------------------
# origem (WebDAV)
# --------------------------------------------------------------------------
def propfind(caminho, depth="1"):
    url = f"{BASE}/{TOKEN}/{caminho}".rstrip("/") + "/"
    auth = base64.b64encode(f"{TOKEN}:".encode()).decode()
    corpo = (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<d:propfind xmlns:d="DAV:"><d:prop>'
        b"<d:getcontentlength/><d:getlastmodified/><d:resourcetype/>"
        b"</d:prop></d:propfind>"
    )
    req = urllib.request.Request(
        url,
        data=corpo,
        method="PROPFIND",
        headers={
            "Authorization": f"Basic {auth}",
            "Depth": depth,
            "Content-Type": "application/xml; charset=utf-8",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return ET.fromstring(resp.read())


def respostas(root):
    itens = []
    for resp in root.findall("d:response", NS):
        href = resp.findtext("d:href", default="", namespaces=NS)
        nome = urllib.parse.unquote(href.rstrip("/").split("/")[-1])
        tamanho, modificado, colecao = None, None, False
        for ps in resp.findall("d:propstat", NS):
            if "200" not in ps.findtext("d:status", default="", namespaces=NS):
                continue
            prop = ps.find("d:prop", NS)
            if prop is None:
                continue
            t = prop.findtext("d:getcontentlength", default=None, namespaces=NS)
            if t is not None and t != "":
                tamanho = int(t)
            m = prop.findtext("d:getlastmodified", default=None, namespaces=NS)
            if m:
                modificado = m
            if prop.find("d:resourcetype/d:collection", NS) is not None:
                colecao = True
        itens.append({"nome": nome, "tamanho": tamanho,
                      "modificado": modificado, "colecao": colecao})
    return itens


def meses_origem():
    nomes = {i["nome"] for i in respostas(propfind("")) if i["colecao"]}
    return sorted(n for n in nomes if MES_RE.match(n) and n >= DESDE)


def arquivos_origem(mes):
    return {
        i["nome"]: i["tamanho"]
        for i in respostas(propfind(f"{mes}/"))
        if not i["colecao"] and i["tamanho"] is not None
    }


# --------------------------------------------------------------------------
# destino (S3)
# --------------------------------------------------------------------------
def arquivos_bucket(mes):
    achados = {}
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=BUCKET, Prefix=f"{PREFIX}/{mes}/"
    ):
        for obj in page.get("Contents", []):
            achados[obj["Key"].split("/")[-1]] = int(obj["Size"])
    return achados


def diferencas():
    """{mes: {arquivo: {origem, bucket}}} - vazio significa tudo em dia."""
    faltando = {}
    for mes in meses_origem():
        origem = arquivos_origem(mes)
        if not origem:
            continue
        destino = arquivos_bucket(mes)
        falta = {
            nome: {"origem": tam, "bucket": destino.get(nome)}
            for nome, tam in origem.items()
            if destino.get(nome) != tam
        }
        if falta:
            faltando[mes] = falta
    return faltando


# --------------------------------------------------------------------------
# EC2
# --------------------------------------------------------------------------
def worker():
    resp = ec2.describe_instances(Filters=[
        {"Name": "tag:Name", "Values": [NOME_WORKER]},
        {"Name": "tag:Project", "Values": [TAG_PROJETO]},
        {"Name": "instance-state-name",
         "Values": ["pending", "running", "stopping", "stopped"]},
    ])
    inst = [i for r in resp["Reservations"] for i in r["Instances"]]
    if not inst:
        # Nao ha worker vivo (terminado ou nunca criado): falha alto.
        raise RuntimeError(
            f"worker '{NOME_WORKER}' nao encontrado em estado utilizavel; "
            "recriar a instancia a partir da AMI cnpj-rf-worker"
        )
    i = inst[0]
    return {"id": i["InstanceId"], "estado": i["State"]["Name"]}


def batimento():
    """Ultimo estado publicado pelo job (state/worker.json)."""
    try:
        obj = s3.get_object(Bucket=BUCKET, Key="state/worker.json")
        return json.loads(obj["Body"].read())
    except Exception:  # noqa: BLE001 - ausencia do arquivo e estado valido
        return {}


def estado_anterior():
    """O que a propria Lambda registrou na execucao anterior."""
    try:
        obj = s3.get_object(Bucket=BUCKET, Key="state/watchdog.json")
        return json.loads(obj["Body"].read())
    except Exception:  # noqa: BLE001
        return {}


def idade_horas(iso):
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - t).total_seconds() / 3600


def lambda_handler(event, context):
    agora = datetime.now(timezone.utc)
    faltando = diferencas()
    w = worker()
    hb = batimento()
    anterior = estado_anterior()
    started_at = anterior.get("started_at")
    acao, detalhe = "nada", ""

    # Instancia travada: 12 h desde que ESTA Lambda a ligou (nao depende de heartbeat,
    # porque um job que morre no import nunca escreve heartbeat).
    if w["estado"] == "running":
        idade = idade_horas(started_at)
        if idade is None:
            idade = idade_horas(hb.get("inicio"))
        if idade is not None and idade >= MAX_HORAS_RODANDO:
            ec2.stop_instances(InstanceIds=[w["id"]])
            acao, detalhe = "stop_timeout", f"rodando ha {idade:.1f}h; parada forcada"
        elif hb.get("status") == "done" and not faltando:
            ec2.stop_instances(InstanceIds=[w["id"]])
            acao, detalhe = "stop_ocioso", "job concluido e bucket em dia; stop repetido"
        else:
            acao, detalhe = "ja_rodando", "worker de pe"
    elif faltando:
        if w["estado"] == "stopped":
            ec2.start_instances(InstanceIds=[w["id"]])
            started_at = agora.isoformat()
            acao, detalhe = "start", "faltam dados; worker iniciado"
        elif w["estado"] == "pending":
            acao, detalhe = "ja_rodando", "worker iniciando"
        else:
            raise RuntimeError(f"worker em estado '{w['estado']}'")
    else:
        acao, detalhe = "nada", "bucket sincronizado com a origem"
        started_at = None

    resumo = {
        mes: {
            "arquivos_faltando": len(itens),
            "bytes_faltando": sum(v["origem"] for v in itens.values()),
        }
        for mes, itens in faltando.items()
    }

    estado = {
        "ts": agora.isoformat(),
        "acao": acao,
        "detalhe": detalhe,
        "started_at": started_at,
        "worker": w,
        "faltando": resumo,
        "total_meses_faltando": len(resumo),
    }
    try:
        s3.put_object(Bucket=BUCKET, Key="state/watchdog.json",
                      Body=json.dumps(estado, indent=2, ensure_ascii=False).encode(),
                      ContentType="application/json")
    except Exception as exc:  # noqa: BLE001
        print(f"AVISO: nao gravou state/watchdog.json: {exc}")

    print(json.dumps(estado, ensure_ascii=False))
    return estado
