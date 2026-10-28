# Implantação

Do zero ao sistema rodando. Ordem importa: cada passo depende do anterior.
Tempo estimado: 40–60 min, sendo ~10 min de espera da AMI.

Os comandos são bash (Linux, macOS ou WSL). No PowerShell, dois cuidados:
`--target` com JSON inline quebra por causa das aspas — use `--cli-input-json`;
e a continuação de linha é a crase (`` ` ``), não `\`.

## 0. Pré-requisitos

- AWS CLI v2 autenticada (`aws sts get-caller-identity` deve responder);
- permissão para criar bucket, IAM, EC2, Lambda e Scheduler;
- `tar` e `zip`;
- Python 3.10+ local (só para os scripts de apoio).

## 1. Variáveis e templates

```bash
export REGIAO=us-east-1
export CONTA=$(aws sts get-caller-identity --query Account --output text)
export BUCKET=cnpj-rf-$CONTA
export CNPJ_WEBDAV_TOKEN='<token do link público da Receita>'

cp .env.example .env
# edite o .env com CONTA, REGIAO, BUCKET e o token

python scripts/render-infra.py
# gera build/infra/*.json já com conta, região e bucket substituídos
```

O token é o trecho que aparece depois de `/public.php/dav/files/` no endereço da base
CNPJ publicado pela Receita. Ele funciona como credencial de leitura: **não comite**.

## 2. Bucket

```bash
aws s3api create-bucket --bucket "$BUCKET" --region "$REGIAO"

aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

aws s3api put-bucket-encryption --bucket "$BUCKET" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
```

Bucket e instância precisam estar **na mesma região** — o worker usa a credencial da
role e o endpoint regional.

## 3. IAM do worker

```bash
aws iam create-role --role-name EC2-rclone-cnpj \
  --assume-role-policy-document file://build/infra/trust-ec2.json

aws iam create-policy --policy-name cnpj-worker-s3 \
  --policy-document file://build/infra/policy-worker-s3.json

aws iam create-policy --policy-name cnpj-worker-selfstop \
  --policy-document file://build/infra/policy-worker-selfstop.json

for p in cnpj-worker-s3 cnpj-worker-selfstop; do
  aws iam attach-role-policy --role-name EC2-rclone-cnpj \
    --policy-arn "arn:aws:iam::$CONTA:policy/$p"
done

aws iam attach-role-policy --role-name EC2-rclone-cnpj \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore

aws iam create-instance-profile --instance-profile-name EC2-rclone-cnpj
aws iam add-role-to-instance-profile \
  --instance-profile-name EC2-rclone-cnpj --role-name EC2-rclone-cnpj
```

Três permissões, três papéis distintos:

| Policy | Serve para |
|---|---|
| `cnpj-worker-s3` | ler o payload e escrever dados, estado e logs |
| `cnpj-worker-selfstop` | **só** `StopInstances`, e só em instância com `Project=cnpj-s3` |
| `AmazonSSMManagedInstanceCore` | acesso por Session Manager, sem abrir porta 22 |

O worker **não** tem permissão de `StartInstances` nem de apagar objetos.

## 4. Payload

O código do worker não vai no user-data (é grande e muda mais que a infra). Vai como
um tar no bucket, baixado pelo próprio worker com a role:

```bash
mkdir -p build
tar -czf build/payload.tar.gz -C worker \
  lote.py requirements.txt cnpj-lote.service parar.sh

aws s3 cp build/payload.tar.gz \
  "s3://$BUCKET/automacao/worker/payload.tar.gz"
```

## 5. EC2

```bash
AMI=$(aws ssm get-parameter --region "$REGIAO" \
  --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --query Parameter.Value --output text)

VPC=$(aws ec2 describe-vpcs --region "$REGIAO" \
  --filters Name=is-default,Values=true --query "Vpcs[0].VpcId" --output text)

SUBNET=$(aws ec2 describe-subnets --region "$REGIAO" \
  --filters Name=vpc-id,Values=$VPC Name=default-for-az,Values=true \
  --query "Subnets[0].SubnetId" --output text)

SG=$(aws ec2 create-security-group --region "$REGIAO" \
  --group-name sgr-rclone-cnpj --description "worker CNPJ: sem ingress" \
  --vpc-id "$VPC" --query GroupId --output text)

# sem ingress; egress liberado para WebDAV e S3
aws ec2 authorize-security-group-egress --region "$REGIAO" --group-id "$SG" \
  --ip-permissions 'IpProtocol=-1,IpRanges=[{CidrIp=0.0.0.0/0}]'
```

Preencha o user-data numa cópia local (o original do repo fica com os placeholders):

```bash
sed -e "s|COLOQUE_O_BUCKET|$BUCKET|" \
    -e "s|COLOQUE_O_TOKEN|$CNPJ_WEBDAV_TOKEN|" \
    worker/bootstrap.sh > build/bootstrap.sh
```

Lance a instância:

```bash
ID=$(aws ec2 run-instances --region "$REGIAO" \
  --image-id "$AMI" \
  --instance-type t3.small \
  --iam-instance-profile Name=EC2-rclone-cnpj \
  --security-group-ids "$SG" \
  --subnet-id "$SUBNET" \
  --associate-public-ip-address \
  --instance-initiated-shutdown-behavior terminate \
  --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=8,VolumeType=gp3,DeleteOnTermination=true}' \
  --user-data file://build/bootstrap.sh \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=cnpj-rf-worker},{Key=Project,Value=cnpj-s3}]' \
  --query "Instances[0].InstanceId" --output text)

echo "$ID"
```

**As duas tags são obrigatórias**, não decorativas:

- `Name=cnpj-rf-worker` → a Lambda encontra a instância;
- `Project=cnpj-s3` → é a condição da policy de `StopInstances`. Sem ela o worker
  **não consegue se parar**.

`--instance-initiated-shutdown-behavior terminate` é a rede de segurança: se o SO for
desligado (por engano ou por panic), a instância se autodestrói em vez de ficar ligada.

## 6. Primeiro run

O bootstrap instala awscli, Python, rclone 1.75.1 (com SHA256), cria a venv, grava
`/opt/cnpj/rclone.conf` e `/opt/cnpj/cnpj.env`, habilita o systemd e dispara o job.

Acompanhe:

```bash
aws s3 cp "s3://$BUCKET/state/worker.json" -
# {"status": "running", ...}  →  {"status": "done", "ok": true, ...}

aws s3 cp "s3://$BUCKET/state/last-run.json" -
```

Se o bootstrap falhar, ele publica `logs/bootstrap-falha-<ts>.log` no bucket e para a
instância. Esse log diz a linha exata.

Confira os bytes de forma independente do job:

```bash
aws s3api list-objects-v2 --bucket "$BUCKET" --prefix cnpj/ \
  --query "sum(Contents[].Size)" --output text
```

Esperado: a instância termina o job e fica `stopped` sozinha.

## 7. AMI

A AMI congela venv, rclone, config e o `.bootstrap-ok`. Recriar o worker a partir dela
leva ~2 min e dispensa o bootstrap.

```bash
aws ec2 create-image --region "$REGIAO" --instance-id "$ID" \
  --name "cnpj-rf-worker-$(date +%F)" \
  --description "worker CNPJ: venv + rclone fixo + systemd cnpj-lote" \
  --no-reboot \
  --tag-specifications 'ResourceType=image,Tags=[{Key=Project,Value=cnpj-s3}]'

aws ec2 wait image-available --region "$REGIAO" --image-ids <AMI_ID>
```

Atenção: a AMI **congela o código**. Depois de mudar `lote.py`, `parar.sh` ou o unit,
é preciso publicar payload novo, aplicar na instância e **gerar AMI nova** — senão a
recuperação traz código velho.

## 8. Lambda

```bash
aws iam create-role --role-name cnpj-rf-watchdog \
  --assume-role-policy-document file://build/infra/trust-lambda.json

aws iam create-policy --policy-name cnpj-rf-watchdog \
  --policy-document file://build/infra/policy-watchdog.json

aws iam attach-role-policy --role-name cnpj-rf-watchdog \
  --policy-arn "arn:aws:iam::$CONTA:policy/cnpj-rf-watchdog"

sleep 10   # propagação do IAM

cd lambda && zip -q ../build/watchdog.zip lambda_function.py && cd ..

aws lambda create-function --region "$REGIAO" \
  --function-name cnpj-rf-watchdog \
  --runtime python3.13 --architectures x86_64 \
  --handler lambda_function.lambda_handler \
  --role "arn:aws:iam::$CONTA:role/cnpj-rf-watchdog" \
  --zip-file fileb://build/watchdog.zip \
  --timeout 60 --memory-size 256 \
  --environment "Variables={CNPJ_BUCKET=$BUCKET,CNPJ_WEBDAV_TOKEN=$CNPJ_WEBDAV_TOKEN,CNPJ_DESDE=2026-08}"
```

Teste antes de agendar:

```bash
aws lambda invoke --region "$REGIAO" --function-name cnpj-rf-watchdog \
  --cli-binary-format raw-in-base64-out --payload '{}' build/resposta.json
cat build/resposta.json
```

Resposta esperada quando está tudo em dia:

```json
{"acao": "nada", "detalhe": "bucket sincronizado com a origem",
 "worker": {"id": "<INSTANCE_ID>", "estado": "stopped"},
 "faltando": {}, "total_meses_faltando": 0}
```

## 9. Scheduler

```bash
aws iam create-role --role-name cnpj-rf-scheduler \
  --assume-role-policy-document file://build/infra/trust-scheduler.json

aws iam create-policy --policy-name cnpj-rf-scheduler-invoke \
  --policy-document file://build/infra/policy-scheduler-invoke.json

aws iam attach-role-policy --role-name cnpj-rf-scheduler \
  --policy-arn "arn:aws:iam::$CONTA:policy/cnpj-rf-scheduler-invoke"

sleep 10
aws scheduler create-schedule --region "$REGIAO" \
  --cli-input-json file://build/infra/schedule.json
```

O JSON do agendamento usa `--cli-input-json` de propósito: `--target` com JSON inline
é destruído pelo PowerShell. O cron é `cron(30 23 * * ? *)` com
`ScheduleExpressionTimezone: America/Sao_Paulo`.

## 10. Teste de idempotência

Com tudo já extraído, force um run manual e confirme que o worker **não baixa nada** e
se para:

```bash
aws ec2 start-instances --region "$REGIAO" --instance-ids "$ID"

# ~1–2 min depois
aws ec2 describe-instances --region "$REGIAO" --instance-ids "$ID" \
  --query "Reservations[0].Instances[0].State.Name" --output text   # stopped

aws s3 cp "s3://$BUCKET/state/last-run.json" -
```

Esperado: todos os meses como `ja_completo`, `ok: true`, execução de poucos segundos.

## 11. Checklist final

| Verificação | Comando |
|---|---|
| instância parada | `aws ec2 describe-instances --instance-ids <ID> --query "Reservations[0].Instances[0].State.Name"` |
| comportamento de shutdown | `aws ec2 describe-instance-attribute --instance-id <ID> --attribute instanceInitiatedShutdownBehavior` → `terminate` |
| schedule ativo | `aws scheduler get-schedule --name cnpj-rf-diario --query State` |
| Lambda sem erro | `aws lambda invoke ...` e conferir `FunctionError` |
| permissões | `aws iam simulate-principal-policy --policy-source-arn arn:aws:iam::$CONTA:role/cnpj-rf-watchdog --action-names ec2:StartInstances --resource-arns arn:aws:ec2:$REGIAO:$CONTA:instance/<ID> --context-entries ContextKeyName=ec2:ResourceTag/Project,ContextKeyType=string,ContextKeyValues=cnpj-s3` |

## 12. Limpeza

```bash
aws scheduler delete-schedule --name cnpj-rf-diario
aws lambda delete-function --function-name cnpj-rf-watchdog

aws iam detach-role-policy --role-name cnpj-rf-watchdog \
  --policy-arn "arn:aws:iam::$CONTA:policy/cnpj-rf-watchdog"
aws iam detach-role-policy --role-name cnpj-rf-scheduler \
  --policy-arn "arn:aws:iam::$CONTA:policy/cnpj-rf-scheduler-invoke"

aws iam delete-policy --policy-arn "arn:aws:iam::$CONTA:policy/cnpj-rf-watchdog"
aws iam delete-policy --policy-arn "arn:aws:iam::$CONTA:policy/cnpj-rf-scheduler-invoke"
aws iam delete-role --role-name cnpj-rf-watchdog
aws iam delete-role --role-name cnpj-rf-scheduler

aws ec2 terminate-instances --instance-ids "$ID"
aws ec2 deregister-image --image-id <AMI_ID>
```

Nenhum desses comandos apaga os dados em `cnpj/`. Para remover os dados, é decisão
consciente — e fora do escopo deste projeto.
