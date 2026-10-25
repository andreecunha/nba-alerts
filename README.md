# 🏀 Alertas NBA — bot do Telegram

Notificações no telemóvel, em tempo real, sempre que um jogador da NBA soma pontos, ressaltos, assistências, triplos, roubos ou desarmes. Avisa também quando completa um duplo-duplo ou um triplo-duplo.

As apps de resultados avisam quando a tua equipa marca, mas não quando *um jogador* chega a X pontos. Este bot faz isso: escreves `lebron 24.5` e recebes um aviso a cada cesto, outro quando estiver perto da linha, outro quando a bater e o resultado no fim do jogo.

*Real-time NBA player stat alerts on Telegram: points, rebounds, assists, threes, double-doubles and triple-doubles, with optional prop lines.*

---

## Exemplos de alertas

```
⚠️ PERTO DA LINHA — faltam 3
🏀 LeBron James +2 pontos → 22
Linha 24.5 · faltam 3
LAL 80–75 GSW · 3.º P 5:12
```

```
✅ LINHA BATIDA — mais de 24.5 pontos
🏀 LeBron James +3 pontos → 25
LAL 95–90 GSW · 4.º P 8:02
```

```
⚠️ PERTO DO TRIPLO-DUPLO — faltam 2
🎯 Nikola Jokić chegou aos 10 → 2/3 para o triplo-duplo
14 pts · 10 res · 8 ast
```

## Funcionalidades

- **Aviso a cada evento** da estatística que escolheres
- **Linhas de aposta opcionais**: quanto falta, destaque quando estiver perto e quando bater, ✅/❌ no fim
- **Duplo-duplo e triplo-duplo**: aviso quando cada categoria chega aos 10 e quando completa
- **Combinações**: pontos+ressaltos+assistências (PRA), PR, PA, RA
- **Estado ao seguir**: se o jogador já está em campo, mostra logo quanto tem; se não, quanto falta para o jogo começar (hora de Lisboa)
- **Avisos de início de jogo** e de **jogador fora do jogo** (lesão, por exemplo)
- **Lista sempre limpa**: cada jogador sai sozinho da lista 10 min depois do fim do jogo
- **Multiutilizador**: cada pessoa tem a sua lista; funciona também em grupos
- Sem barra: basta escrever `jokic ressaltos 11.5`

## Como usar

| Mensagem | O que faz |
|---|---|
| `lebron` | Pontos, aviso a cada cesto |
| `lebron 24.5` | Pontos com linha |
| `jokic ressaltos 11.5` | Outra estatística com linha |
| `curry 3pm 4.5` | Triplos |
| `sga pra 40.5` | Pontos + ressaltos + assistências |
| `jokic td` / `jokic dd` | Triplo-duplo / duplo-duplo |

| Comando | O que faz |
|---|---|
| `/p` | Todos os pedidos possíveis (pt, inglês e atalhos) |
| `/l` | O que estás a seguir e quando joga |
| `/r 1 2 5` · `/r 2-4` · `/r tudo` | Remover um ou vários |
| `/j` | Jogos de hoje |
| `/limpar` | Apaga a conversa (mensagens com menos de 48 h) e começa do zero |
| `/a` | Ajuda |

Também aceita os nomes em inglês (points, rebounds, assists, threes, steals, blocks, double-double, triple-double).

## Criar o teu próprio bot

### 1. Token do Telegram
No Telegram, abre o **@BotFather**, envia `/newbot` e guarda o token. **Nunca o ponhas no código nem no GitHub.**

### 2a. Alojar no Railway (recomendado)
1. Faz *fork* deste repositório.
2. Em [railway.com](https://railway.com), escolhe **New Project → Deploy from GitHub repo** e seleciona o fork.
3. Em **Variables**, adiciona:
   - `TELEGRAM_TOKEN` = o teu token
   - `DB_PATH` = `/data/subs.db`
4. Adiciona um **volume** montado em `/data`, para as listas sobreviverem às atualizações.

O arranque já está configurado em `railway.json`.

### 2b. Correr no teu PC
Requer Python 3.10+.
```bash
pip install -r requirements.txt
TELEGRAM_TOKEN=o_teu_token python bot.py
```
No Windows (cmd): `set TELEGRAM_TOKEN=o_teu_token` e depois `py bot.py`.

### 2c. Servidor Ubuntu (VPS)
```bash
bash instalar.sh
```
Instala tudo e cria um serviço `systemd` que arranca sozinho.

### Variáveis opcionais
| Variável | Por defeito | O que faz |
|---|---|---|
| `INTERVALO` | `20` | Segundos entre leituras dos jogos |
| `DB_PATH` | `subs.db` | Onde guardar as listas |
| `REMOVER_APOS` | `10` | Minutos depois do fim do jogo até o jogador sair da lista |

## Como funciona

- **Estatísticas ao vivo**: endpoints públicos da ESPN (scoreboard e summary), lidos a cada 20 s
- **Calendário**: scoreboard da ESPN dos próximos 7 dias
- **Equipa de cada jogador**: plantéis públicos da ESPN
- **Guardar as listas**: SQLite

```
core.py       lógica (interpretar pedidos, alertas, duplo/triplo-duplo, calendário), sem rede
bot.py        Telegram, leitura dos dados e base de dados
test_core.py  simula jogos completos e verifica os alertas
```

Testes: `python test_core.py`

## Detalhes

- **"Mais de" é estritamente acima**: linha 24.5 precisa de 25, linha 10 precisa de 11.
- **Atraso** de cerca de 10–30 s em relação à TV.
- **Correções de estatística** da NBA são tratadas: se o valor descer abaixo da linha, os avisos voltam a ficar ativos.
- **Reiniciar a meio de um jogo** não reenvia alertas antigos.
- Os dados são públicos mas **não oficiais**: se a ESPN mudar o formato, pode ser preciso um ajuste.

## Aviso

Projeto pessoal, sem ligação à NBA, à ESPN nem a casas de apostas. Os dados podem ter atrasos ou erros: confirma sempre na fonte oficial. Aposta com responsabilidade.

## Licença

[MIT](LICENSE)
