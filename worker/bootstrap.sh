#!/bin/bash
# ---------------------------------------------------------------------------
# Bootstrap do worker CNPJ (EC2 persistente). Roda UMA vez, via user-data.
# Idempotente: se /opt/cnpj/.bootstrap-ok existe, nao faz nada.
# Sem URL pre-assinada: o payload vem do S3 com a credencial da role.
# Em qualquer erro: sobe o log para o S3 e PARA a instancia.
# NAO chama shutdown. O job (lote.py) para a instancia via StopInstances.
#
# Configure as tres variaveis abaixo (ou exporte-as antes de rodar o script).
# Nada de valor real deve ser comitado: veja scripts/verificar-segredos.py.
# ---------------------------------------------------------------------------
set -euo pipefail

BUCKET="${CNPJ_BUCKET:-COLOQUE_O_BUCKET}"
REGION="${AWS_REGION:-us-east-1}"
TOKEN="${CNPJ_WEBDAV_TOKEN:-COLOQUE_O_TOKEN}"
RCLONE_VER="1.75.1"
LOG="/var/log/cnpj-bootstrap.log"

exec >>"$LOG" 2>&1
echo "=== bootstrap $(date -Is) ==="

if [ -f /opt/cnpj/.bootstrap-ok ]; then
  echo "ja configurado (.bootstrap-ok presente) - nada a fazer"
  exit 0
fi

if [[ "$BUCKET" == COLOQUE_* || "$TOKEN" == COLOQUE_* ]]; then
  echo "ERRO: preencha CNPJ_BUCKET e CNPJ_WEBDAV_TOKEN antes de subir"
  exit 1
fi

imds_token() {
  curl -fsS -X PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 300"
}

imds_get() {
  curl -fsS -H "X-aws-ec2-metadata-token: $(imds_token)" \
    "http://169.254.169.254/latest/meta-data/$1"
}

parar() {
  rc=$?
  echo "ERRO (rc=$rc) na linha ${BASH_LINENO[0]}"
  if command -v aws >/dev/null 2>&1; then
    aws s3 cp "$LOG" \
      "s3://${BUCKET}/logs/bootstrap-falha-$(date -u +%Y%m%dT%H%M%SZ).log" \
      --region "$REGION" || true
    aws ec2 stop-instances --instance-ids "$(imds_get instance-id)" \
      --region "$REGION" || true
  else
    echo "sem awscli: nao consigo parar a instancia daqui"
  fi
}
trap parar ERR

# rede pronta (cloud-init roda antes do network-online.target)
for _ in $(seq 1 60); do
  if curl -fsS -o /dev/null https://s3.amazonaws.com 2>/dev/null; then
    break
  fi
  sleep 5
done

dnf -y install unzip tar gzip >/dev/null

# AWS CLI pelo instalador oficial: nao existe pacote awscli nos repos da AL2023
if ! command -v aws >/dev/null 2>&1; then
  cd /tmp
  curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip
  unzip -q -o awscliv2.zip
  ./aws/install --update >/dev/null
fi
aws --version

PY=""
for c in python3.12 python3.11; do
  if dnf -y install "$c" "${c}-pip" >/dev/null 2>&1; then
    PY="$c"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "ERRO: nenhum Python >= 3.10 disponivel (boto3 1.43 exige >= 3.10)"
  exit 1
fi
echo "python: $($PY --version 2>&1)"

cd /tmp

# rclone em versao fixa, com verificacao de checksum
if ! /usr/local/bin/rclone version 2>/dev/null | head -1 | grep -q "v${RCLONE_VER}"; then
  ZIP="rclone-v${RCLONE_VER}-linux-amd64.zip"
  curl -fsSLO "https://downloads.rclone.org/v${RCLONE_VER}/${ZIP}"
  curl -fsSL "https://downloads.rclone.org/v${RCLONE_VER}/SHA256SUMS" -o SHA256SUMS
  grep " ${ZIP}\$" SHA256SUMS | sha256sum -c -
  unzip -o -q "$ZIP"
  install -m 0755 "rclone-v${RCLONE_VER}-linux-amd64/rclone" /usr/local/bin/rclone
fi
/usr/local/bin/rclone version | head -1

# payload: lote.py, requirements.txt, cnpj-lote.service
aws s3 cp "s3://${BUCKET}/automacao/worker/payload.tar.gz" /tmp/payload.tar.gz \
  --region "$REGION"
mkdir -p /opt/cnpj
tar -xzf /tmp/payload.tar.gz -C /opt/cnpj
chmod 0755 /opt/cnpj/parar.sh

# venv com versoes fixas
"$PY" -m venv /opt/cnpj/venv
/opt/cnpj/venv/bin/pip install --quiet --upgrade pip
/opt/cnpj/venv/bin/pip install --quiet -r /opt/cnpj/requirements.txt
/opt/cnpj/venv/bin/pip freeze > /opt/cnpj/requirements.lock
echo "venv: $(/opt/cnpj/venv/bin/pip freeze | tr '\n' ' ')"

# config do rclone (origem publica + destino com credencial da role)
umask 077
cat > /opt/cnpj/rclone.conf <<EOF
[rfb]
type = webdav
url = https://arquivos.receitafederal.gov.br/public.php/dav/files/${TOKEN}
vendor = nextcloud
user = ${TOKEN}
pass =

[s3]
type = s3
provider = AWS
env_auth = true
region = ${REGION}
EOF
chmod 600 /opt/cnpj/rclone.conf
umask 022

# variaveis do job (fora do unit, para o systemd carregar)
cat > /opt/cnpj/cnpj.env <<EOF
CNPJ_BUCKET=${BUCKET}
CNPJ_DESDE=${CNPJ_DESDE:-2026-08}
AWS_REGION=${REGION}
EOF
chmod 600 /opt/cnpj/cnpj.env

# falha cedo se o codigo estiver quebrado
/opt/cnpj/venv/bin/python -m py_compile /opt/cnpj/lote.py

install -m 0644 /opt/cnpj/cnpj-lote.service /etc/systemd/system/cnpj-lote.service
systemctl daemon-reload
systemctl enable cnpj-lote.service

touch /opt/cnpj/.bootstrap-ok
echo "=== bootstrap OK $(date -Is) ==="

# primeira execucao (e toda execucao em boots seguintes)
systemctl start --no-block cnpj-lote.service
