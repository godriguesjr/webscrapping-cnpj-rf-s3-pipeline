# Pipeline CNPJ → S3

Automação para espelhar os **dados abertos de CNPJ da Receita Federal** (publicados
em WebDAV) em um bucket S3 — com custo próximo de zero nos dias em que não há dado novo.

A base é publicada em ~37 arquivos por mês (~7,8 GB). Manter uma máquina ligada o mês
inteiro só para baixar isso é desperdício; fazer o download na mão todo mês é fácil de
esquecer. O desenho aqui resolve os dois: um **vigia diário** compara origem e destino
e só liga a EC2 quando falta arquivo. A máquina baixa, confere e se desliga sozinha.

## Como funciona

```
EventBridge Scheduler (23:30 America/Sao_Paulo)
        │
        ▼
Lambda watchdog ──PROPFIND──► origem (WebDAV público)
        │                     │
        │        S3 List ─────┘
        │        diff por nome + tamanho
        │
        ├── nada faltando ──► fim (custo zero)
        │
        └── faltando ──► EC2 start
                            │
                            ▼
                 worker (systemd oneshot, roda no boot)
                   rclone copy + check --size-only
                   grava state/ e logs/ no bucket
                   StopInstances ──► máquina parada
```

O que sustenta o desenho:

- **Idempotência por mês**: o worker compara `(arquivos, bytes)` de cada mês na origem
  e no bucket. Se bate, pula sem baixar nada.
- **Diff por arquivo, não por watermark**: um objeto que desapareça do bucket é
  detectado no dia seguinte; um watermark não detectaria.
- **A máquina se para sozinha** via `StopInstances` (API), nunca `shutdown`.
  O atributo `InstanceInitiatedShutdownBehavior` fica em `terminate` como rede de
  segurança: se o SO for desligado por qualquer motivo, a instância se autodestrói.
- **Nada fica ligado em caso de falha**: `ExecStopPost` no systemd para a instância
  quando o job sai com erro.
- **Credencial só por role**: nenhuma chave estática no código, na máquina ou no repo.

## Componentes

| Peça | Papel |
|---|---|
| `lambda/lambda_function.py` | vigia diário: PROPFIND na origem, `ListObjectsV2` no S3, diff, `StartInstances` |
| `worker/lote.py` | job: por mês, compara, copia com `rclone copy`, confere com `check --size-only`, publica estado e para a instância |
| `worker/bootstrap.sh` | user-data idempotente: instala awscli, Python, rclone fixo, venv e habilita o systemd |
| `worker/cnpj-lote.service` | oneshot que roda no boot; `ExecStopPost` garante a parada em falha |
| `worker/parar.sh` | rede de segurança: para a instância se o job falhar |
| `infra/iam/*.json` | policies e trusts, com `${PLACEHOLDERS}` em vez de IDs |
| `scripts/render-infra.py` | substitui os placeholders e valida o JSON |
| `scripts/verificar-segredos.py` | impede que ID de conta, e-mail ou token entrem no repo |

## Estrutura

```
.
├── lambda/lambda_function.py
├── worker/
│   ├── bootstrap.sh
│   ├── lote.py
│   ├── cnpj-lote.service
│   ├── parar.sh
│   └── requirements.txt
├── infra/iam/            # templates com ${AWS_ACCOUNT_ID} etc.
├── scripts/              # render-infra.py, verificar-segredos.py
├── docs/                 # arquitetura, implantação, operação, lições
├── .env.example
└── .github/workflows/segredos.yml
```

## Começando

Pré-requisitos: AWS CLI v2 autenticada, `tar`, Python 3.10+ (só para os scripts).
A instância e o bucket precisam estar **na mesma região**.

```bash
git clone https://github.com/<seu-usuario>/webscrapping-cnpj-rf-s3-pipeline.git
cd webscrapping-cnpj-rf-s3-pipeline

cp .env.example .env          # preencha conta, região, bucket e token
python scripts/render-infra.py   # gera build/infra/*.json a partir dos templates
```

O passo a passo completo (IAM, EC2, AMI, Lambda, Scheduler) está em
[docs/implantacao.md](docs/implantacao.md). O dia a dia está em
[docs/operacao.md](docs/operacao.md).

## Custos

Valores de referência em `us-east-1`, preço de tabela:

| Item | Mensal |
|---|---|
| EBS 8 GB gp3 da instância parada | ~US$ 0,64 |
| Snapshot da AMI (8 GB) | ~US$ 0,05 |
| EC2 ligada (~30 min em mês com dado novo) | ~US$ 0,01 |
| Lambda (30 invocações/dia) + Scheduler | US$ 0,00 (free tier) |
| S3: ~7,2 GiB por mês novo, `INTELLIGENT_TIERING` | ~US$ 0,17 por mês acumulado |

Três meses extraídos (~23 GiB) ficam em torno de **US$ 1,25/mês** no total.

## Segurança e privacidade

Este repositório é uma versão **sanitizada** do projeto real. Ficaram de fora:

- ID da conta AWS, nome do bucket, IDs de instância, AMI, snapshot, VPC, subnet e
  security group — nos templates aparecem como `${AWS_ACCOUNT_ID}`, `${CNPJ_BUCKET}` etc.;
- o token do link público da origem (funciona como credencial de leitura);
- e-mail, nome de usuário do sistema e caminhos locais.

O que **não** existe no projeto, por desenho: chave de acesso estática (tudo usa role),
porta aberta na instância (acesso só por SSM), bucket público (Block Public Access
ligado, SSE-S3).

Além da remoção dos valores, uma diferença funcional em relação ao deploy original:
lá o bucket, a região, o piso histórico e o token eram constantes no código; aqui eles
vêm de variáveis de ambiente (`CNPJ_BUCKET`, `AWS_REGION`, `CNPJ_DESDE`,
`CNPJ_WEBDAV_TOKEN`). Nenhuma outra lógica mudou.

Para garantir que nada volte por acidente, `scripts/verificar-segredos.py` varre o
repositório e o CI roda esse script em todo push. Para rodar localmente:

```bash
python scripts/verificar-segredos.py
```

## Limitações

- Só considera meses `>= CNPJ_DESDE` (o piso é uma escolha, não uma limitação técnica).
- O diff é por **tamanho**: a origem não publica hash. Um arquivo corrompido com o
  mesmo tamanho passaria — mitigado pelo `check --size-only` e pela comparação de
  contagem.
- Um único worker: duas execuções simultâneas disputariam a mesma instância.
- Região única; trocar de região exige recriar bucket e instância.

## Documentação

| Documento | Conteúdo |
|---|---|
| [docs/arquitetura.md](docs/arquitetura.md) | desenho, decisões e por que cada uma |
| [docs/implantacao.md](docs/implantacao.md) | reproduzir do zero, com comandos |
| [docs/operacao.md](docs/operacao.md) | runbook: verificar, forçar, recuperar |
| [docs/licoes-aprendidas.md](docs/licoes-aprendidas.md) | os erros que custaram tempo |

## Licença

MIT — veja [LICENSE](LICENSE).
