#!/usr/bin/env python3
"""Varre o repositório atrás de dados que não podem ser publicados.

Roda no CI (.github/workflows/segredos.yml) e localmente:

    python scripts/verificar-segredos.py

Sai com código 1 se encontrar algo. Falsos positivos conhecidos são
ignorados por ALLOWLIST, sempre pelo valor exato — nunca desligando a regra.
"""

import pathlib
import re
import sys

RAIZ = pathlib.Path(__file__).resolve().parents[1]

IGNORAR_DIR = {".git", "build", "__pycache__", ".venv", "venv", ".idea"}
IGNORAR_ARQ = {"scripts/verificar-segredos.py"}
EXTENSOES = {
    ".md", ".py", ".sh", ".json", ".yml", ".yaml", ".txt",
    ".service", ".example", ".env", ".toml", ".cfg", ".ini",
}

# Valores de exemplo, documentação e testes: não são reais.
ALLOWLIST = {"123456789012", "cnpj-rf-123456789012"}

PADROES = {
    "id de conta AWS": r"(?<!\d)\d{12}(?!\d)",
    "ARN com conta": r"arn:aws:[\w-]+:[\w-]*:\d{12}:",
    "chave de acesso": r"\b(AKIA|ASIA)[0-9A-Z]{12,}\b",
    "id de recurso AWS": r"\b(ami|i|sg|subnet|snap|vol|vpc|eni|rtb|acl)-[0-9a-f]{8,17}\b",
    "e-mail": r"[\w.+-]+@[\w-]+\.[\w.]{2,}",
    "token do WebDAV": r"public\.php/dav/files/[A-Za-z0-9]{6,}",
    "bucket do projeto": r"cnpj-rf-\d{12}",
    "caminho de usuário Windows": r"[A-Za-z]:\\\\Users\\\\[^\\\\\s]+",
    "segredo em texto claro": r"(?i)(aws_secret_access_key|secret_key|password)\s*[:=]\s*\S{6,}",
    "chave privada": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
}

COMPILADOS = {nome: re.compile(pat) for nome, pat in PADROES.items()}


def arquivos():
    for p in RAIZ.rglob("*"):
        if not p.is_file():
            continue
        if any(parte in IGNORAR_DIR for parte in p.parts):
            continue
        if p.relative_to(RAIZ).as_posix() in IGNORAR_ARQ:
            continue
        if p.suffix.lower() in EXTENSOES or p.name in {".gitignore", ".env.example"}:
            yield p


def main():
    achados = []
    for arq in arquivos():
        try:
            texto = arq.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for nome, regex in COMPILADOS.items():
            for m in regex.finditer(texto):
                valor = m.group(0)
                if valor in ALLOWLIST:
                    continue
                linha = texto.count("\n", 0, m.start()) + 1
                achados.append((arq.relative_to(RAIZ).as_posix(), linha, nome, valor[:80]))

    for arq, linha, nome, valor in achados:
        print(f"{arq}:{linha}: {nome}: {valor}")

    if achados:
        print(f"\n{len(achados)} ocorrência(s) para remover ou parametrizar.")
        return 1

    print("nenhum dado sensível encontrado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
