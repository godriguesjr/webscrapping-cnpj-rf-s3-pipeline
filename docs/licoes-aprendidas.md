# Lições aprendidas

Erros reais, com sintoma, causa e correção. Servem para não repetir — e para
reconhecer rápido quando aparecerem de novo.

## 1. Substituição de texto em script gerado

**Sintoma:** a instância subia, o cloud-init registrava
`Failed to run module scripts-user`, e o serviço não existia.

**Causa:** o user-data era um template e o PowerShell fazia
`$tpl.Replace("__PAYLOAD_URL__", $url)`. O `.Replace()` troca **todas** as ocorrências
— inclusive a do `case` que validava se o placeholder tinha sido preenchido. A guarda
passou a comparar a URL com ela mesma, deu verdadeiro e abortou o script.

**Correção:** o placeholder foi eliminado. O payload passou a ser baixado do S3 com a
credencial da role (`aws s3 cp`), o que também tirou a URL pré-assinada do desenho.

**Lição:** não gere script por substituição de texto sem âncora única. Se precisar,
valide o resultado (`grep` pelo placeholder) antes de executar.

## 2. `dnf install awscli-2` não existe no AL2023

**Sintoma:** `No matching Packages` durante o bootstrap.

**Causa:** o Amazon Linux 2023 não empacota o AWS CLI — nem v1, nem v2. A suposição
veio do AL2, onde o pacote existe.

**Correção:** instalador oficial (`awscli.amazonaws.com/awscli-exe-linux-x86_64.zip`).

**Lição:** em imagem nova, confirme a existência do pacote antes de escrever o script.
Um `dnf search` de 5 segundos teria evitado um ciclo de debug de 20 minutos.

## 3. Cliente boto3 criado no import, fora do `try/finally`

**Sintoma:** o serviço falhava em menos de 1 segundo, sem heartbeat, e a instância
ficava ligada.

**Causa:** `boto3.client("ec2")` era criado no topo do módulo, sem região. Sem
`AWS_DEFAULT_REGION` no ambiente, o import levantava `NoRegionError` **antes** do
`try`, então nem o `finally` nem o registro de estado rodavam.

**Correção:** região explícita nos dois clients, `AWS_DEFAULT_REGION` no unit e — a
parte que realmente importa — `ExecStopPost=/opt/cnpj/parar.sh` no systemd, que para a
instância independentemente de onde o processo morreu.

**Lição:** a rede de segurança tem que estar **fora** do processo que pode falhar.
Qualquer coisa no `finally` só vale se o `try` foi alcançado.

## 4. Bootstrap sem caminho de falha

**Sintoma:** máquina ligada, sem serviço, sem log em lugar nenhum.

**Causa:** o script tinha `set -euo pipefail` e mais nada. Na primeira falha ele
simplesmente terminava.

**Correção:** `trap parar ERR` — publica `/var/log/cnpj-bootstrap.log` em
`logs/bootstrap-falha-<ts>.log` e chama `StopInstances`.

**Lição:** em script que roda uma vez, em máquina efêmera, o caminho de erro é o
caminho principal. Escreva-o primeiro.

## 5. JSON inline no PowerShell

**Sintoma:** `aws scheduler create-schedule --target '{...}'` →
`Invalid JSON: Expecting property name enclosed in double quotes`.

**Causa:** o PowerShell remove as aspas internas ao passar o argumento para o
executável. O JSON chega mutilado.

**Correção:** a definição foi para um arquivo e o comando passou a usar
`--cli-input-json file://...`.

**Lição:** no PowerShell, qualquer argumento com JSON vai por arquivo. Vale para
`scheduler`, `lambda`, `ssm send-command` e `iam`.

## 6. A AMI congela o código

**Sintoma:** depois de corrigir o `lote.py`, uma instância recriada pela AMI voltou a
falhar com o código antigo.

**Causa:** a AMI é um snapshot do disco. O payload novo estava no S3, mas a máquina
recriada não roda o bootstrap de novo (`.bootstrap-ok` presente).

**Correção:** tratar "atualizar código" como três passos — publicar payload, aplicar
via SSM, **gerar AMI nova** e desregistrar a antiga.

**Lição:** se o artefato de recuperação não é reconstruído no deploy, ele vira uma
armadilha silenciosa.

## 7. Tag como condição de permissão

**Sintoma:** nenhum, até a primeira recriação da instância sem a tag `Project`.

**Causa:** a policy de `StopInstances` tem condição `ec2:ResourceTag/Project=cnpj-s3`.
Sem a tag, a chamada é negada — e o worker não se para.

**Correção:** as duas tags (`Name` e `Project`) passaram a ser obrigatórias no
`run-instances`, e isso está documentado no runbook.

**Lição:** permissão condicionada a tag é ótima para escopo, mas cria um acoplamento
invisível entre IAM e provisionamento. Documente a tag como se fosse senha.

## 8. Testar idempotência antes de confiar

O caminho "não há nada a fazer" é o que roda 29 dias por mês. Ele precisa ser testado
tanto quanto o caminho de download: ligar a máquina à mão, ver os meses como
`ja_completo`, conferir que **zero bytes** foram baixados e que ela se parou.

Foi esse teste que revelou que a lógica de `started_at` da Lambda precisava vir do
estado gravado por ela, e não do heartbeat do job — porque um job que morre no import
nunca escreve heartbeat.
