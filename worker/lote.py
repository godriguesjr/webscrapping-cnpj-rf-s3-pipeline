#!/usr/bin/env python3
"""
Extrai meses da base CNPJ (Receita Federal / WebDAV publico) para o S3.

Regras do projeto:
  - SOMENTE rclone copy / check. Nunca sync, delete, purge, move, rmdir.
  - Idempotente: mes cujo total (arquivos + bytes) ja bate com o bucket e pulado.
  - Nunca chama shutdown: para a instancia via API (StopInstances).
    O atributo InstanceInitiatedShutdownBehavior continua "terminate".
  - Sempre grava state/last-run.json e sobe o log, mesmo em falha.

Configuracao (ambiente): CNPJ_BUCKET (obrigatorio), CNPJ_DESDE, AWS_REGION,
CNPJ_PREFIX. Nenhum valor real fica no codigo.
"""

import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
PREFIX = os.environ.get("CNPJ_PREFIX", "cnpj")
DESDE = os.environ.get("CNPJ_DESDE", "2026-08")   # piso historico
RCLONE = "/usr/local/bin/rclone"
LOG = "/var/log/cnpj-lote.log"
STATE_KEY = "state/last-run.json"
MES_RE = re.compile(r"^\d{4}-\d{2}$")

BUCKET = os.environ.get("CNPJ_BUCKET", "")
if not BUCKET:
    raise SystemExit("defina CNPJ_BUCKET (ou /opt/cnpj/cnpj.env)")

s3 = boto3.client("s3", region_name=REGION)
ec2 = boto3.client("ec2", region_name=REGION)


def log(msg):
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}", flush=True)


def imds(path):
    """Le da IMDSv2 (token obrigatorio: IMDSv1 fica bloqueado)."""
    req = urllib.request.Request(
        "http://169.254.169.254/latest/api/token",
        method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        token = resp.read().decode()
    req = urllib.request.Request(
        f"http://169.254.169.254/latest/meta-data/{path}",
        headers={"X-aws-ec2-metadata-token": token},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.read().decode()


def run(args, capture=True):
    log("$ " + " ".join(args))
    return subprocess.run(
        args,
        text=True,
        capture_output=capture,
        check=False,
    )


def meses_origem():
    p = run([RCLONE, "lsd", "rfb:", "--max-depth", "1"])
    if p.returncode != 0:
        raise RuntimeError(f"rclone lsd falhou: {(p.stderr or '').strip()}")
    meses = set()
    for linha in p.stdout.splitlines():
        partes = linha.split()
        if not partes:
            continue
        nome = partes[-1]
        if MES_RE.match(nome) and nome >= DESDE:
            meses.add(nome)
    return sorted(meses)


def origem(mes):
    """(arquivos, bytes) na origem."""
    p = run([RCLONE, "size", f"rfb:{mes}", "--json"])
    if p.returncode != 0:
        raise RuntimeError(f"rclone size falhou em {mes}: {(p.stderr or '').strip()}")
    d = json.loads(p.stdout)
    if d.get("sizeless"):
        log(f"AVISO: {mes} tem {d['sizeless']} arquivo(s) sem tamanho na origem")
    return int(d["count"]), int(d["bytes"])


def bucket(mes):
    """(arquivos, bytes) no prefixo do mes."""
    n = t = 0
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=BUCKET, Prefix=f"{PREFIX}/{mes}/"
    ):
        for obj in page.get("Contents", []):
            n += 1
            t += int(obj["Size"])
    return n, t


def copia(mes):
    return run([
        RCLONE, "copy", f"rfb:{mes}", f"s3:{BUCKET}/{PREFIX}/{mes}",
        "--transfers", "4",
        "--checkers", "8",
        "--stats", "60s",
        "--stats-one-line",
        "--s3-chunk-size", "64M",
        "--s3-storage-class", "INTELLIGENT_TIERING",
        "--s3-no-check-bucket",
        "--retries", "10",
        "--low-level-retries", "20",
        "-v",
    ]).returncode


def confere(mes):
    """0 = sem diferencas. A origem nao expoe hash: so tamanho."""
    return run([
        RCLONE, "check", f"rfb:{mes}", f"s3:{BUCKET}/{PREFIX}/{mes}",
        "--size-only",
    ]).returncode


def main():
    inicio = datetime.now(timezone.utc)
    iid = None
    ok = True
    rel = {}

    try:
        iid = imds("instance-id")
        log(f"=== inicio {inicio.isoformat()} instancia={iid} ===")

        # batimento cardiaco: a Lambda usa isto para saber se o job esta vivo
        s3.put_object(
            Bucket=BUCKET,
            Key="state/worker.json",
            Body=json.dumps({
                "status": "running",
                "inicio": inicio.isoformat(),
                "instancia": iid,
            }).encode(),
            ContentType="application/json",
        )

        p = run([RCLONE, "version"])
        if p.returncode == 0 and p.stdout:
            log(p.stdout.splitlines()[0])

        # Prova de credencial antes de baixar qualquer coisa.
        p = run([RCLONE, "lsd", f"s3:{BUCKET}"])
        if p.returncode != 0:
            log("ERRO: sem acesso ao bucket (role/regiao). Abortando.")
            return 2

        meses = meses_origem()
        log(f"meses na origem >= {DESDE}: {', '.join(meses) if meses else '(nenhum)'}")

        for mes in meses:
            n_o, b_o = origem(mes)
            n_b, b_b = bucket(mes)

            if (n_o, b_o) == (n_b, b_b):
                log(f"{mes}: ja completo no bucket ({n_b} arq, {b_b} B) - pulando")
                rel[mes] = {"status": "ja_completo", "arquivos": n_b, "bytes": b_b}
                continue

            log(f"{mes}: incompleto - origem {n_o} arq/{b_o} B, bucket {n_b} arq/{b_b} B")
            rc = copia(mes)
            n_b, b_b = bucket(mes)
            rc_check = confere(mes) if rc == 0 else None
            mes_ok = rc == 0 and rc_check == 0 and (n_o, b_o) == (n_b, b_b)
            ok = ok and mes_ok
            rel[mes] = {
                "status": "copiado" if mes_ok else "falhou",
                "origem": {"arquivos": n_o, "bytes": b_o},
                "bucket": {"arquivos": n_b, "bytes": b_b},
                "rc_copy": rc,
                "rc_check": rc_check,
            }
            log(f"{mes}: {'OK' if mes_ok else 'FALHOU'} "
                f"(copy={rc}, check={rc_check}, bucket={n_b} arq/{b_b} B)")

    except Exception as exc:  # noqa: BLE001 - precisa registrar e ainda parar a instancia
        ok = False
        log(f"ERRO NAO TRATADO: {type(exc).__name__}: {exc}")

    finally:
        fim = datetime.now(timezone.utc)
        estado = {
            "inicio": inicio.isoformat(),
            "fim": fim.isoformat(),
            "duracao_s": int((fim - inicio).total_seconds()),
            "ok": ok,
            "instancia": iid,
            "meses": rel,
        }
        try:
            s3.put_object(
                Bucket=BUCKET,
                Key=STATE_KEY,
                Body=json.dumps(estado, indent=2, ensure_ascii=False).encode(),
                ContentType="application/json",
            )
            log(f"estado gravado em s3://{BUCKET}/{STATE_KEY}")
        except Exception as exc:  # noqa: BLE001
            log(f"AVISO: nao gravou {STATE_KEY}: {exc}")

        try:
            s3.put_object(
                Bucket=BUCKET,
                Key="state/worker.json",
                Body=json.dumps({
                    "status": "done",
                    "inicio": inicio.isoformat(),
                    "fim": fim.isoformat(),
                    "ok": ok,
                    "instancia": iid,
                }).encode(),
                ContentType="application/json",
            )
        except Exception as exc:  # noqa: BLE001
            log(f"AVISO: nao gravou state/worker.json: {exc}")

        try:
            if os.path.exists(LOG):
                chave = f"logs/lote-{inicio:%Y-%m-%dT%H%M%S}Z.log"
                s3.upload_file(LOG, BUCKET, chave)
                log(f"log publicado em s3://{BUCKET}/{chave}")
        except Exception as exc:  # noqa: BLE001
            log(f"AVISO: nao subiu o log: {exc}")

        if iid:
            try:
                ec2.stop_instances(InstanceIds=[iid])
                log(f"stop solicitado para {iid}")
            except Exception as exc:  # noqa: BLE001
                log(f"ERRO ao parar a instancia: {exc}")

        log(f"=== fim {fim.isoformat()} ok={ok} ===")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
