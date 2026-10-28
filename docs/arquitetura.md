# Arquitetura

## 1. Contexto

A Receita Federal publica a base de CNPJ em um **WebDAV público**, organizada por mês
(`AAAA-MM/`), com ~37 arquivos por pasta e ~7,8 GB por mês. Não há API, não há hash
publicado, não há notificação de publicação — só o diretório.

O objetivo é ter esses arquivos em um bucket S3, de forma contínua e barata, sem
intervenção manual.

Restrições que moldaram o desenho:

- a origem é lenta e o volume mensal é grande — não cabe em Lambda (15 min, 10 GB de disco);
- o custo precisa ser desprezível nos dias sem novidade;
- nada pode ficar ligado por esquecimento;
- nunca apagar dados no destino: só copiar.

## 2. Visão geral

```
┌──────────────────────────────┐
│ EventBridge Scheduler        │
│ cron(30 23 * * ? *)          │
│ America/Sao_Paulo            │
└──────────────┬───────────────┘
               │ invoke
               ▼
┌──────────────────────────────────────────────┐
│ Lambda cnpj-rf-watchdog                      │
│                                              │
│  1. PROPFIND /            → meses            │
│  2. PROPFIND /<mes>/      → nome + tamanho   │
│  3. ListObjectsV2 cnpj/<mes>/ → nome+tamanho │
│  4. diff                                     │
│                                              │
│  sem diferença  → grava state/ e termina     │
│  com diferença  → StartInstances             │
└──────────────┬───────────────────────────────┘
               │ (só quando falta algo)
               ▼
┌──────────────────────────────────────────────┐
│ EC2 cnpj-rf-worker (t3.small, 8 GB gp3)      │
│                                              │
│  boot → systemd cnpj-lote.service (oneshot)  │
│    ├─ heartbeat  state/worker.json           │
│    ├─ por mês: (arquivos,bytes) origem × bucket │
│    │    iguais → pula                        │
│    │    diferentes → rclone copy + check     │
│    ├─ state/last-run.json + logs/lote-*.log  │
│    └─ StopInstances                          │
│  ExecStopPost → parar.sh (se exit != 0)      │
└──────────────────────────────────────────────┘
```

A Lambda nunca transfere dados: ela só faz duas listagens e um punhado de chamadas de
API (KB de tráfego). Quem move bytes é a EC2, e só quando há o que mover.

## 3. Por que o diff é por arquivo e tamanho

"Já foi extraído" precisa de uma definição verificável. As opções:

| Abordagem | Problema |
|---|---|
| Marcar o mês como concluído após o primeiro run | um objeto que falhou no meio nunca é recuperado |
| Watermark (`último mês processado`) | não detecta arquivo novo dentro de um mês já processado, nem objeto removido do bucket |
| Comparar só contagem de arquivos | não detecta arquivo truncado ou substituído |
| **Comparar nome + tamanho, arquivo a arquivo** | é a definição literal de "está igual"; custa duas listagens |

A origem não publica checksum, então tamanho é o melhor disponível. Para reduzir o risco
de corrupção silenciosa, o worker faz ainda `rclone check --size-only` depois do copy e
compara `(arquivos, bytes)` do mês inteiro.

## 4. Máquina de estados do worker

```
stopped ──StartInstances──► pending ──► running ──► stopping ──► stopped
                                              │
                                              └─(falha)─► ExecStopPost para
```

A Lambda é a única que liga a máquina. Quem desliga é o próprio job (`StopInstances`
no `finally`), com dois backups:

1. `ExecStopPost=/opt/cnpj/parar.sh` no systemd: se o job sair com código != 0, a
   instância é parada mesmo que o Python tenha morrido antes do `finally`
   (foi exatamente o que aconteceu com um `NoRegionError` no import).
2. Guarda de 12 h na Lambda: se a instância estiver `running` há mais de 12 h desde
   que a Lambda a ligou, ela chama `StopInstances`. O relógio vem de
   `state/watchdog.json`, não do heartbeat — um job que morre no import nunca escreve
   heartbeat.

## 5. Estados persistidos

Tudo em S3, sem DynamoDB. São três JSONs pequenos e um log por execução:

| Chave | Escrito por | Para quê |
|---|---|---|
| `state/worker.json` | job | heartbeat `running`/`done`; a Lambda usa para saber se o job terminou |
| `state/last-run.json` | job | relatório por mês: `ja_completo`, `copiado`, `falhou`, rc de copy/check |
| `state/watchdog.json` | Lambda | última decisão e `started_at` (base do timeout de 12 h) |
| `logs/lote-<ts>.log` | job | saída completa do rclone daquele run |

## 6. Decisões

| # | Decisão | Por quê |
|---|---|---|
| 1 | **rclone** como motor, Python só orquestra | streaming sem disco, retry e multipart já testados com 7,8 GB; reimplementar em boto3 perderia retry de baixo nível |
| 2 | venv com **pins exatos** | reprodutibilidade; o build gera `requirements.lock` com as transitivas |
| 3 | rclone **1.75.1 fixo + SHA256** | `dnf` traria versão variável; o checksum garante integridade do binário |
| 4 | `terminate` no atributo **+** `StopInstances` via API | o atributo só age em shutdown do SO; a API para a máquina sem destruí-la. Se algo desligar o SO, ela se autodestrói |
| 5 | Diff **diário** em vez de watermark | detecta arquivo faltando, não só mês novo |
| 6 | Estado em **S3**, não DynamoDB | três JSONs, custo zero, sem recurso extra para manter |
| 7 | Lambda **fora de VPC** | precisa de internet para o PROPFIND e de rota para o S3; dentro de VPC exigiria NAT |
| 8 | Permissões por **tag** (`Project=cnpj-s3`) | recriar a instância não exige editar policy |
| 9 | **AMI** de recuperação | reconstruir o worker em ~2 min sem repetir o bootstrap |
| 10 | Payload via `s3 cp` com a role, **sem URL pré-assinada** | URL expira junto com a sessão de quem a gerou |
| 11 | Fuso declarado no Scheduler (`America/Sao_Paulo`) | sem conversão manual para UTC, inclusive em horário de verão |
| 12 | `rclone copy`, nunca `sync` | `sync` apagaria no destino o que não existe na origem — inaceitável |

## 7. Modelo de falhas

| Falha | Efeito | Proteção |
|---|---|---|
| Job levanta exceção | máquina ficaria ligada | `finally` + `ExecStopPost` |
| Job morre no import (antes do `try`) | idem | `ExecStopPost` |
| Bootstrap quebra | máquina ligada sem serviço | `trap ERR` sobe o log e para |
| Origem fora do ar | mês fica incompleto | próximo run tenta de novo; nada é apagado |
| Objeto some do bucket | diff acusa no dia seguinte | religa o worker |
| Instância terminada por engano | Lambda falha alto (`RuntimeError`) | recriar pela AMI, com as duas tags |
| Sessão do operador expira | comando pela metade | operações são idempotentes |

## 8. Custos

O desenho é dominado por custo fixo (EBS parado + snapshot) e pelo crescimento do S3:

- instância parada: ~US$ 0,70/mês (EBS 8 GB gp3 + snapshot);
- execução: ~US$ 0,02/h de t3.small, ~30 min por mês com dado novo;
- Lambda e Scheduler: dentro do free tier;
- S3: ~7,2 GiB por mês, classe `INTELLIGENT_TIERING`.

## 9. O que ficou fora

- **Alarme de erro** na Lambda (CloudWatch + SNS): o estado em `state/watchdog.json`
  já registra a decisão, mas ninguém é avisado.
- **Histórico completo**: o piso `CNPJ_DESDE` limita o escopo; meses anteriores são
  opcionais e custam ~250 GiB.
- **Múltiplos workers**: o desenho é single-writer.
