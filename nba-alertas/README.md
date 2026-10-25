# Alertas NBA — bot do Telegram

Recebe uma notificação no telemóvel sempre que um jogador soma pontos, ressaltos, assistências, triplos, roubos ou desarmes. Se lhe deres uma linha de aposta, também avisa quando ela estiver perto, quando for batida e com o resultado no fim do jogo.

## 1. Criar o bot (2 minutos)
1. No Telegram, abre o **@BotFather** e envia `/newbot`.
2. Escolhe um nome (ex.: *Alertas NBA do André*) e um username que acabe em `bot`.
3. O BotFather dá-te um **token**, do género `123456:ABC...`. Guarda-o e não o partilhes com ninguém.

## 2. Pôr a correr
Precisas de Python 3.10 ou mais recente.

```bash
pip install -r requirements.txt
TELEGRAM_TOKEN=o_teu_token python bot.py
```

No Windows (PowerShell):
```powershell
pip install -r requirements.txt
$env:TELEGRAM_TOKEN="o_teu_token"; python bot.py
```

Depois abre o teu bot no Telegram e carrega em **Iniciar**. Para os teus amigos usarem, manda-lhes o link `t.me/username_do_bot`. Cada pessoa tem a sua própria lista.

## 3. Comandos
| Comando | O que faz |
|---|---|
| `/seguir lebron pontos` | Aviso a cada cesto |
| `/seguir lebron pontos 24.5` | E também quando faltarem 3 e quando a linha for batida |
| `/seguir jokic ressaltos 11.5 2` | O último número é a margem do aviso "perto" |
| `/seguir sga pra 40.5` | Pontos + ressaltos + assistências |
| `/lista` | Ver o que estás a seguir |
| `/remover 2` / `/remover tudo` | Remover |
| `/jogos` | Jogos de hoje e resultados |

Estatísticas aceites: `pontos`, `ressaltos`, `assistencias`, `triplos`, `roubos`, `desarmes`, `pra`, `pr`, `pa`, `ra`.
Margem do aviso "perto" por defeito: 3 para pontos, 2 para PR/PA/RA, 3 para PRA e 1 para as restantes.

## Detalhes a ter em conta
- **"Mais de" significa estritamente acima.** Com linha 24.5 precisa de 25. Com linha 10 precisa de 11, porque 10 não chega para um "mais de 10".
- **Atraso:** os dados vêm do site da NBA, atualizados a cada 20 s, com cerca de 10 a 30 s de atraso em relação à TV.
- **Correções:** às vezes a NBA corrige estatísticas (um ressalto passa para outro jogador, por exemplo). Se o valor descer abaixo da linha, os avisos voltam a ficar ativos.
- **Nomes repetidos:** `james` apanha o LeBron e o Bronny. Nesse caso o bot avisa e deves usar o nome completo.
- **Reiniciar a meio de um jogo** não volta a enviar alertas antigos: o bot só regista o valor atual e continua a partir daí.
- Os dados são públicos mas **não oficiais**: se a NBA mudar o formato, o bot pode precisar de um ajuste.

## Onde alojar
O bot tem de estar ligado durante os jogos, que em Portugal são sobretudo entre a 0h e as 5h.
- **O teu PC ligado**: funciona, mas só enquanto o PC estiver ligado.
- **Raspberry Pi** ou um portátil velho em casa: barato e fica sempre ligado.
- **Servidor na cloud** (ex.: VM gratuita da Oracle Cloud ou um VPS barato): a opção mais fiável.

Opcional: `INTERVALO=15` altera o tempo entre leituras e `DB_PATH=/caminho/subs.db` altera o local onde a lista é guardada.

## Teste
`python test_core.py` simula um jogo completo e verifica todos os alertas.
