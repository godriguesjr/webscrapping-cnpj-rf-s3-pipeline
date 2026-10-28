# Operação

O sistema roda sozinho. Este é o documento para quando for preciso mexer.

## 1. Onde olhar

| Chave no bucket | Conteúdo |
|---|---|
| `state/watchdog.json` | última execução da Lambda: ação, meses faltando, estado do worker |
| `state/last-run.json` | último run do worker: por mês, `ja_completo` / `copiado` / `falhou` |
| `state/worker.json` | heartbeat: `running` durante o job, `done` no fim |
| `logs/lote-<ts>.log` | saída completa do rclone daquele run |
| `logs/bootstrap-falha-<ts>.log` | só existe se o bootstrap quebrou |

```bash
aws s3 cp "s3://$BUCKET/state/watchdog.json" -
aws s3 cp "s3://$BUCKET/state/last-run.json" -
aws s3 ls "s3://$BUCKET/cnpj/" --human-readable
```

## 2. Fluxo normal

- **23:30 (America/Sao_Paulo)**: a Lambda compara origem e bucket a partir de
  `CNPJ_DESDE`.
- Nada faltando → `"acao": "nada"`. O worker continua parado. Custo do dia: zero.
- Faltando → liga o worker; ele baixa só o que falta, confere por tamanho e se para.
- Um mês novo aparece na origem uma vez por mês e é pego no primeiro run seguinte.

## 3. Forçar um run agora

```bash
aws lambda invoke --region "$REGIAO" --function-name cnpj-rf-watchdog \
  --cli-binary-format raw-in-base64-out --payload '{}' resposta.json
cat resposta.json
```

Se a Lambda disser `"acao": "nada"` e você quiser rodar mesmo assim, ligue o worker
à mão — o serviço roda no boot:

```bash
aws ec2 start-instances --region "$REGIAO" --instance-ids "$ID"
```

Acompanhe por `state/worker.json` e por `aws s3 ls`.

## 4. Recriar o worker pela AMI

Se a instância for terminada por engano (ou você quiser uma máquina limpa), recrie
**exatamente assim**:

```bash
aws ec2 run-instances --region "$REGIAO" \
  --image-id <AMI_ID> \
  --instance-type t3.small \
  --iam-instance-profile Name=EC2-rclone-cnpj \
  --security-group-ids <SG_ID> \
  --subnet-id <SUBNET_ID> \
  --associate-public-ip-address \
  --instance-initiated-shutdown-behavior terminate \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=cnpj-rf-worker},{Key=Project,Value=cnpj-s3}]' \
  --query "Instances[0].InstanceId" --output text
```

Sem `--user-data`: a AMI já tem venv, rclone, config, unit habilitado e
`.bootstrap-ok`.

**As duas tags não são decorativas:**

- `Name=cnpj-rf-worker` → a Lambda acha a instância;
- `Project=cnpj-s3` → condição da policy de `StopInstances`. Sem ela, o worker não
  consegue se parar e fica ligado.

## 5. Atualizar o código do worker

A AMI congela o código. Sequência completa:

```bash
# 1. empacotar e publicar
tar -czf build/payload.tar.gz -C worker \
  lote.py requirements.txt cnpj-lote.service parar.sh
aws s3 cp build/payload.tar.gz "s3://$BUCKET/automacao/worker/payload.tar.gz"
```

```bash
# 2. aplicar na instância (com ela ligada), via SSM
cat > build/ssm-params.json <<JSON
{"commands": [
  "aws s3 cp s3://$BUCKET/automacao/worker/payload.tar.gz /tmp/payload.tar.gz",
  "tar -xzf /tmp/payload.tar.gz -C /opt/cnpj",
  "chmod 0755 /opt/cnpj/parar.sh",
  "install -m 0644 /opt/cnpj/cnpj-lote.service /etc/systemd/system/",
  "systemctl daemon-reload",
  "systemctl restart cnpj-lote"
]}
JSON

aws ssm send-command --region "$REGIAO" \
  --instance-ids "$ID" \
  --document-name AWS-RunShellScript \
  --parameters file://build/ssm-params.json \
  --query "Command.CommandId" --output text
```

```bash
# 3. AMI nova (senão a recuperação traz código velho)
aws ec2 create-image --region "$REGIAO" --instance-id "$ID" \
  --name "cnpj-rf-worker-$(date +%F)" --no-reboot
aws ec2 deregister-image --image-id <AMI_ANTIGA>
```

## 6. Sintomas e causas

| Sintoma | O que verificar |
|---|---|
| Lambda diz `"acao": "nada"` mas falta dado | `state/watchdog.json` → `faltando`. Se vazio, o diff considerou completo; compare `rclone size rfb:<mes>` com `aws s3 ls` |
| Worker ligado por horas | `state/worker.json`. Se `running` há mais de 12 h, a Lambda para no dia seguinte; para agora com `stop-instances` |
| Worker `terminated` | recriar pela AMI (§4), com as duas tags |
| `bootstrap-falha-*.log` no bucket | o bootstrap falhou e a instância se parou; o log diz a linha |
| Nada novo no bucket depois de um run | veja `logs/` e `last-run.json`; se o job falhou, `parar.sh` parou a máquina |
| Lambda com `RuntimeError: worker não encontrado` | a instância foi terminada ou perdeu as tags; recriar pela AMI |
| Schedule não dispara | `aws scheduler get-schedule --name cnpj-rf-diario` → `State`; conferir o fuso |

## 7. Nunca

- `rclone sync`, `delete`, `purge`, `move`, `rmdir` — não existem em nenhum script e
  não devem entrar. O projeto só copia.
- `shutdown -h now` dentro do worker: com `InstanceInitiatedShutdownBehavior=terminate`,
  isso **destrói** a instância. O correto é `StopInstances`, que é o que o job faz.
- `terminate-instances` no worker sem antes confirmar que a AMI está atualizada.
- Editar a policy de stop removendo a condição por tag: ela passaria a permitir parar
  qualquer instância da conta.

## 8. Custo

| Item | Mensal |
|---|---|
| EBS 8 GB gp3 (parado) | ~US$ 0,64 |
| Snapshot da AMI | ~US$ 0,05 |
| EC2 (~30 min/mês) | ~US$ 0,01 |
| Lambda + Scheduler | US$ 0,00 |
| S3 (~7,2 GiB/mês novo) | ~US$ 0,17 por mês acumulado |

Se o custo importar, o caminho é lifecycle no S3 (mover meses antigos para Glacier
Instant Retrieval) — mas isso muda o tempo de leitura e não é gratuito em requisições.
