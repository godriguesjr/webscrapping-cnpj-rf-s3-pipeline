#!/bin/bash
# ExecStopPost do cnpj-lote.service.
# Roda sempre que o servico termina. So age se o job falhou (EXIT_STATUS != 0):
# nesse caso para a instancia, para nao deixar maquina ligada sem trabalho.
set -u

if [ "${EXIT_STATUS:-0}" = "0" ]; then
  exit 0
fi

TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
ID=$(curl -s -H "X-aws-ec2-metadata-token: ${TOKEN}" \
  "http://169.254.169.254/latest/meta-data/instance-id")

echo "job falhou (EXIT_STATUS=${EXIT_STATUS}); parando ${ID}" >&2
aws ec2 stop-instances --region "${AWS_REGION:-us-east-1}" --instance-ids "$ID" || true
