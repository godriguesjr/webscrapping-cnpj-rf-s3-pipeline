#!/usr/bin/env python3
"""Gera os JSONs de IAM em build/infra a partir dos templates de infra/.

Os templates usam ${AWS_ACCOUNT_ID}, ${AWS_REGION} e ${CNPJ_BUCKET}; nada de
valor real fica versionado. Lê as variáveis do ambiente (ou de um .env).

Uso:
    python scripts/render-infra.py            # imprime onde escreveu
    python scripts/render-infra.py --check    # só valida, não escreve
"""

import json
import os
import pathlib
import string
import sys

RAIZ = pathlib.Path(__file__).resolve().parents[1]
ORIGEM = RAIZ / "infra"
DESTINO = RAIZ / "build" / "infra"
NECESSARIAS = ("AWS_ACCOUNT_ID", "AWS_REGION", "CNPJ_BUCKET")


def carregar_env():
    """Carrega .env da raiz sem sobrescrever o que já veio do ambiente."""
    env = RAIZ / ".env"
    if not env.exists():
        return
    for linha in env.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, valor = linha.split("=", 1)
        os.environ.setdefault(chave.strip(), valor.strip().strip('"').strip("'"))


def main():
    carregar_env()
    valores = {k: os.environ.get(k, "").strip() for k in NECESSARIAS}
    faltando = [k for k, v in valores.items() if not v]
    if faltando:
        print("faltam variáveis:", ", ".join(faltando))
        print("copie .env.example para .env e preencha")
        return 1

    somente_validar = "--check" in sys.argv
    gerados = 0

    for tpl in sorted(ORIGEM.rglob("*.json")):
        texto = tpl.read_text(encoding="utf-8")
        try:
            saida = string.Template(texto).substitute(valores)
        except KeyError as exc:
            print(f"{tpl}: variável desconhecida {exc}")
            return 1
        try:
            json.loads(saida)
        except json.JSONDecodeError as exc:
            print(f"{tpl}: JSON inválido depois da substituição: {exc}")
            return 1

        if somente_validar:
            print(f"ok   {tpl.relative_to(RAIZ)}")
            continue

        destino = DESTINO / tpl.relative_to(ORIGEM)
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(saida, encoding="utf-8", newline="\n")
        print(f"gerado {destino.relative_to(RAIZ)}")
        gerados += 1

    return 0 if (somente_validar or gerados) else 1


if __name__ == "__main__":
    sys.exit(main())
