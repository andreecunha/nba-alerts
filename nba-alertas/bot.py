"""Bot do Telegram: alertas de estatísticas de jogadores da NBA ao vivo.

Correr:  TELEGRAM_TOKEN=xxxx python bot.py
"""
import asyncio
import html
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from core import (STATS, Sub, Tracker, alvo, box_espn, calendario_espn, encontrar, falta_dd, jogadores_espn, jogo_espn,
                  confronto, cores, linha_stats, marcador, nome_completo, parse_add, parse_seguir, proximo_jogo, quando, resumo_dd, texto_pedidos, valor_de)

def _ler_token() -> str:
    """Token da variável TELEGRAM_TOKEN ou, se não existir, do ficheiro token.txt ao lado do bot."""
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    if not token:
        caminho = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.txt")
        try:
            with open(caminho, encoding="utf-8") as f:
                token = f.read().strip()
        except FileNotFoundError:
            pass
    if not token:
        raise SystemExit("Falta o token: define TELEGRAM_TOKEN ou cria um ficheiro token.txt ao lado do bot.py")
    return token


TOKEN = _ler_token()
INTERVALO = int(os.getenv("INTERVALO", "20"))  # segundos entre leituras
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "subs.db"))
REMOVER_APOS = int(os.getenv("REMOVER_APOS", "10"))  # minutos depois do fim do jogo até sair da lista

# Dados ao vivo da ESPN (o cdn.nba.com bloqueia pedidos a partir de alguns países/redes).
ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
ESPN_EQUIPAS = f"{ESPN}/teams"
NOVA_IORQUE = ZoneInfo("America/New_York")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "application/json",
}

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # não escrever o token nem cada pedido no terminal
log = logging.getLogger("nba-alertas")


# ---------- dados da NBA / ESPN, com cache ----------
class Dados:
    def __init__(self, http: httpx.AsyncClient):
        self.http = http
        self.cache: dict[str, tuple[float, object]] = {}

    async def _json(self, url: str, validade: float):
        t, valor = self.cache.get(url, (0, None))
        if valor is not None and time.time() - t < validade:
            return valor
        r = await self.http.get(url)
        r.raise_for_status()
        valor = r.json()
        self.cache[url] = (time.time(), valor)
        return valor

    async def _dia(self, data, validade: float) -> list[dict]:
        """Jogos de um dia (data dos EUA), já no formato da NBA."""
        j = await self._json(f"{ESPN}/scoreboard?dates={data:%Y%m%d}", validade)
        return [jogo_espn(ev) for ev in j.get("events", [])]

    async def jogos_hoje(self) -> list[dict]:
        """Jogos do dia nos EUA, mais os de ontem que ainda estejam a decorrer (jogos depois da meia-noite)."""
        hoje = datetime.now(NOVA_IORQUE).date()
        jogos = await self._dia(hoje, 10)
        try:
            jogos += [g for g in await self._dia(hoje - timedelta(days=1), 10) if g["gameStatus"] == 2]
        except Exception as e:
            log.warning("jogos de ontem: %s", e)
        return jogos

    async def boxscore(self, gid: str) -> dict:
        return box_espn(await self._json(f"{ESPN}/summary?event={gid}", 10), gid)

    async def calendario(self) -> list[dict]:
        """Jogos de ontem até daqui a 7 dias (atualizado de 3 em 3 h)."""
        t, valor = self.cache.get("calendario", (0, None))
        if valor is None or time.time() - t > 3 * 3600:
            hoje = datetime.now(NOVA_IORQUE).date()
            dias = await asyncio.gather(*(self._dia(hoje + timedelta(days=d), 3 * 3600) for d in range(-1, 8)),
                                        return_exceptions=True)
            valor = calendario_espn([g for d in dias if isinstance(d, list) for g in d])
            self.cache["calendario"] = (time.time(), valor)
        return valor

    async def jogadores(self) -> list[dict]:
        """Todos os jogadores da liga com a equipa (plantéis da ESPN, atualizados de 12 em 12 h)."""
        t, valor = self.cache.get("jogadores", (0, None))
        if valor is not None and time.time() - t < 12 * 3600:
            return valor
        equipas = (await self._json(ESPN_EQUIPAS, 12 * 3600))["sports"][0]["leagues"][0]["teams"]

        async def plantel(e):
            r = await self.http.get(f"{ESPN_EQUIPAS}/{e['team']['id']}/roster")
            r.raise_for_status()
            return jogadores_espn(e["team"]["abbreviation"], r.json())

        listas = await asyncio.gather(*(plantel(e) for e in equipas))
        valor = [p for lista in listas for p in lista]
        self.cache["jogadores"] = (time.time(), valor)
        return valor


# ---------- base de dados ----------
def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS subs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, jogador TEXT,
        stat TEXT, linha REAL, perto INTEGER)""")
    try:
        con.execute("ALTER TABLE subs ADD COLUMN fim REAL")  # quando o jogo acabou (para remoção automática)
    except sqlite3.OperationalError:
        pass  # a coluna já existe
    return con


def fins(chat_id: int) -> dict[int, float]:
    with db() as con:
        return dict(con.execute("SELECT id, fim FROM subs WHERE chat_id = ? AND fim IS NOT NULL", (chat_id,)))


def todas_subs() -> list[Sub]:
    with db() as con:
        return [Sub(*r) for r in con.execute("SELECT id, chat_id, jogador, stat, linha, perto FROM subs")]


def subs_de(chat_id: int) -> list[Sub]:
    return [s for s in todas_subs() if s.chat_id == chat_id]


def descrever(s: Sub) -> str:
    t = f"<b>{html.escape(s.jogador)}</b> · {STATS[s.stat]['nome']}"
    if s.linha is not None:
        t += f" · mais de {s.linha:g}"
    return t


# ---------- estado atual de um jogador ----------
async def procurar_ao_vivo(dados: Dados, nome: str):
    """Procura o jogador nos jogos a decorrer. Devolve (jogo, jogador) ou (None, None)."""
    for g in await dados.jogos_hoje():
        if g["gameStatus"] != 2:
            continue
        try:
            game = (await dados.boxscore(g["gameId"]))["game"]
        except Exception as e:
            log.warning("boxscore %s: %s", g["gameId"], e)
            continue
        achados = encontrar(game["homeTeam"].get("players", []) + game["awayTeam"].get("players", []), nome)
        if len(achados) == 1:
            return game, achados[0]
    return None, None


def texto_ao_vivo(s: Sub, game: dict, p: dict, tracker: Tracker | None) -> str:
    if p.get("status") == "INACTIVE":
        return f"🚫 Não está a jogar este jogo\n{marcador(game)}"
    st = p.get("statistics", {})
    valor = valor_de(s.stat, st)
    if tracker and s.id:
        tracker.semear(s, game["gameId"], st)
    alvo_dd = STATS[s.stat].get("dd")
    if alvo_dd:
        falta = falta_dd(st, alvo_dd)
        fim = "✅ já tem" if falta is None else f"{valor}/{alvo_dd} categorias · falta{'m' if falta > 1 else ''} {falta}"
        return f"🟢 A jogar: {resumo_dd(st)} · {fim}\n{marcador(game)}"
    texto = f"🟢 A jogar: <b>{valor}</b> {STATS[s.stat]['nome']}"
    if s.linha is not None:
        falta = alvo(s.linha) - valor
        texto += " · ✅ linha já batida" if falta <= 0 else f" · falta{'m' if falta > 1 else ''} {falta}"
    return f"{texto}\n{marcador(game)}"


async def estado(dados: Dados, s: Sub, tracker: Tracker | None = None) -> str:
    """Linha de estado: o que tem agora, ou quanto falta para o jogo começar.
    Primeiro procura nos jogos a decorrer (só precisa dos jogos do dia); só depois usa plantéis e calendário."""
    agora = datetime.now(timezone.utc)

    # 1) Está a jogar agora?
    try:
        game, p = await procurar_ao_vivo(dados, s.jogador)
        if p:
            return texto_ao_vivo(s, game, p, tracker)
    except Exception:
        log.exception("estado: jogos a decorrer")

    # 2) Quando é o próximo jogo?
    try:
        achados = encontrar(await dados.jogadores(), s.jogador)
        if len(achados) != 1:
            return ""
        equipa = achados[0]["team"]
        estados = {}
        try:
            estados = {g["gameId"]: g["gameStatus"] for g in await dados.jogos_hoje()}
        except Exception:
            pass
        jogo = proximo_jogo(await dados.calendario(), equipa, agora, estados)
        if jogo is None:
            return "Sem jogos marcados para já."
        if jogo["estado"] == 1:
            return f"⏳ {confronto(jogo['fora'], jogo['casa'])} {quando(jogo['inicio'], agora)}"
        if jogo["estado"] == 2:
            return f"🟢 O jogo {jogo['fora']} @ {jogo['casa']} está a decorrer, mas ainda não aparece na ficha do jogo."
    except Exception:
        log.exception("estado: plantéis/calendário")
    return ""


async def cmd_diag(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Testa cada fonte de dados e diz o que funciona."""
    dados: Dados = ctx.application.bot_data["dados"]
    dados.cache.clear()
    linhas = ["<b>Diagnóstico</b>"]
    vivos = []
    try:
        jogos = await dados.jogos_hoje()
        vivos = [g for g in jogos if g["gameStatus"] == 2]
        linhas.append(f"✅ ESPN jogos de hoje: {len(jogos)} ({len(vivos)} a decorrer)")
    except Exception as e:
        linhas.append(f"❌ ESPN jogos de hoje: {html.escape(repr(e))[:200]}")
    if vivos:
        try:
            game = (await dados.boxscore(vivos[0]["gameId"]))["game"]
            n = len(game["homeTeam"].get("players", [])) + len(game["awayTeam"].get("players", []))
            linhas.append(f"✅ ESPN ficha do jogo {vivos[0]['awayTeam']['teamTricode']}@{vivos[0]['homeTeam']['teamTricode']}: {n} jogadores")
        except Exception as e:
            linhas.append(f"❌ ESPN ficha do jogo: {html.escape(repr(e))[:200]}")
    try:
        linhas.append(f"✅ ESPN calendário: {len(await dados.calendario())} jogos")
    except Exception as e:
        linhas.append(f"❌ ESPN calendário: {html.escape(repr(e))[:200]}")
    try:
        linhas.append(f"✅ ESPN plantéis: {len(await dados.jogadores())} jogadores")
    except Exception as e:
        linhas.append(f"❌ ESPN plantéis: {html.escape(repr(e))[:200]}")
    await update.message.reply_html("\n".join(linhas))


# ---------- comandos ----------
AJUDA = (
    "<b>Alertas NBA</b> 🏀\n\n"
    "<b>Seguir pelo nome</b> (sem barra):\n"
    "• <code>lebron</code> — escolhes a estatística nos botões\n"
    "• <code>lebron pontos 24.5</code> — direto, com linha\n"
    "• <code>jokic ressaltos 11.5</code>\n\n"
    "<b>Seguir pela lista</b>:\n"
    "/j → /statsjogo 2 → /add 3\n\n"
    "/p — todos os pedidos possíveis (ressaltos, triplos, duplo-duplo…)\n"
    "/l — o que estás a seguir e quando joga\n"
    "/r 1 2 5 — remove esses números da lista · /r 2-4 · /r tudo\n"
    "/j — jogos de hoje (numerados)\n"
    "/statsjogo N — stats de todos os jogadores do jogo N\n"
    "/add 3 7 — segue os jogadores 3 e 7 do /statsjogo\n"
    "/limpar — apaga a conversa e começa do zero\n"
    "/a — esta ajuda\n\n"
    "Os dados vêm da ESPN com cerca de 10–30 s de atraso em relação à TV."
)


async def cmd_ajuda(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(AJUDA)


async def cmd_pedidos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(texto_pedidos())


async def resolver(dados: Dados, consulta: str):
    """Nome escrito -> ("ok", nome completo) | ("varios", [jogadores]) | ("nada", None)."""
    try:
        achados = encontrar(await dados.jogadores(), consulta)
    except Exception as e:
        log.warning("plantéis: %s", e)
        achados = None
    if achados and len(achados) > 1:
        return "varios", achados
    if achados:
        return "ok", nome_completo(achados[0])
    game, p = await procurar_ao_vivo(dados, consulta)
    if p:
        return "ok", nome_completo(p)
    if achados is None:
        return "ok", consulta  # sem plantéis nem jogo ao vivo: aceita o nome tal como está
    return "nada", None


# Botões de estatística (até 3 por linha).
BOTOES_STATS = [("Pontos", "pts"), ("Ressaltos", "reb"), ("Assistências", "ast"),
                ("Triplos", "3pt"), ("Roubos", "stl"), ("Desarmes", "blk"),
                ("Pts+Res+Ast", "pra"), ("Pts+Res", "pr"), ("Pts+Ast", "pa"),
                ("Res+Ast", "ra"), ("Duplo-duplo", "dd"), ("Triplo-duplo", "td")]


def teclado_stats() -> InlineKeyboardMarkup:
    botoes = [InlineKeyboardButton(t, callback_data=f"stat:{k}") for t, k in BOTOES_STATS]
    linhas = [botoes[i:i + 3] for i in range(0, len(botoes), 3)]
    linhas.append([InlineKeyboardButton("✖️ Cancelar", callback_data="stat:cancelar")])
    return InlineKeyboardMarkup(linhas)


def texto_pergunta(pend: dict, aviso: str = "") -> str:
    """Pergunta para o próximo jogador da fila: um de cada vez, cada um com a sua estatística."""
    nome = pend["fila"][0]
    total, feito = pend["total"], pend["total"] - len(pend["fila"])
    texto = f"{aviso}📊 Que estatística queres que eu notifique para <b>{html.escape(nome)}</b>?"
    if total > 1:
        texto += f" ({feito + 1} de {total})"
    if pend["linha"] is not None:
        texto += f"\n(com a linha {pend['linha']:g})"
    return texto


async def perguntar_stat(update: Update, ctx, nomes: list[str], linha, aviso: str = ""):
    """Guarda os jogadores escolhidos e pergunta, com botões, a estatística de cada um."""
    pend = {"fila": list(nomes), "total": len(nomes), "linha": linha}
    ctx.application.bot_data.setdefault("pendente", {})[update.effective_chat.id] = pend
    await update.message.reply_html(texto_pergunta(pend, aviso), reply_markup=teclado_stats())


async def escolheu_stat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Carregou num botão de estatística: adiciona o jogador atual e, se houver mais, pergunta pelo seguinte."""
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat.id
    pendentes = ctx.application.bot_data.setdefault("pendente", {})
    pend = pendentes.get(chat_id)
    escolha = q.data.split(":", 1)[1]
    if escolha == "cancelar":
        pendentes.pop(chat_id, None)
        resto = f" ({len(pend['fila'])} jogador{'es' if len(pend['fila']) != 1 else ''} por adicionar)" if pend else ""
        await q.edit_message_text(f"Cancelado{resto}. Nada mais foi adicionado.")
        return
    if not pend or not pend["fila"]:
        await q.edit_message_text("Este pedido já expirou. Volta a escrever o jogador (ex.: lebron) ou usa /add.")
        return
    nome = pend["fila"].pop(0)
    linha = None if STATS[escolha].get("dd") else pend["linha"]
    texto = await criar_sub(ctx, chat_id, nome, escolha, linha)
    if pend["fila"]:
        # Ainda há jogadores: confirma este e pergunta pelo próximo numa mensagem nova.
        await q.edit_message_text(texto, parse_mode="HTML")
        await q.message.reply_html(texto_pergunta(pend), reply_markup=teclado_stats())
        return
    pendentes.pop(chat_id, None)
    if linha is None and not STATS[escolha].get("dd"):
        texto += (f"\n\n💡 Para juntar uma linha de aposta escreve, por exemplo: "
                  f"<code>{html.escape(nome.lower())} {STATS[escolha]['palavras'][0]} "
                  f"{'24.5' if escolha in ('pts', 'pra', 'pr', 'pa') else '5.5'}</code>")
    await q.edit_message_text(texto, parse_mode="HTML")


async def seguir(update: Update, ctx: ContextTypes.DEFAULT_TYPE, args: list[str]):
    res, erro = parse_seguir(args)
    if erro:
        await update.message.reply_text(erro + "\nVê todos os pedidos possíveis em /p")
        return
    consulta, stat, linha = res
    dados: Dados = ctx.application.bot_data["dados"]

    estado_nome, valor = await resolver(dados, consulta)
    aviso = ""
    if estado_nome == "nada" and len(consulta.split()) > 1:
        # Talvez a última palavra seja uma estatística que não conheço (ex.: "lebron rebotes").
        resto, palavra = consulta.rsplit(" ", 1)
        estado2, valor2 = await resolver(dados, resto)
        if estado2 != "nada":
            estado_nome, valor = estado2, valor2
            stat = None
            aviso = f"Não conheço a estatística «{html.escape(palavra)}». "
    if estado_nome == "varios":
        nomes = "\n".join(f"• {html.escape(nome_completo(p))} ({p['team']})" for p in valor[:8])
        await update.message.reply_html(f"Há vários jogadores com esse nome. Qual deles?\n{nomes}\n\nEscreve o nome completo.")
        return
    if estado_nome == "nada":
        await update.message.reply_html(f"Não encontrei nenhum jogador «{html.escape(consulta)}». Verifica o nome "
                                        f"ou faz /j e /statsjogo para escolheres da lista.")
        return
    if stat is None:
        await perguntar_stat(update, ctx, [valor], linha, aviso)
        return
    await update.message.reply_html(await criar_sub(ctx, update.effective_chat.id, valor, stat, linha))


async def criar_sub(ctx, chat_id: int, nome: str, stat: str, linha) -> str:
    """Guarda o seguimento e devolve a mensagem de confirmação com o estado atual."""
    with db() as con:
        cur = con.execute("INSERT INTO subs (chat_id, jogador, stat, linha, perto) VALUES (?,?,?,?,NULL)",
                          (chat_id, nome, stat, linha))
        sub = Sub(cur.lastrowid, chat_id, nome, stat, linha)
    linha_estado = await estado(ctx.application.bot_data["dados"], sub, ctx.application.bot_data["tracker"])
    texto = f"✅ A seguir: {descrever(sub)}"
    if linha_estado:
        texto += f"\n{linha_estado}"
    return texto


async def cmd_seguir(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Escreve o jogador, por exemplo: lebron 24.5")
        return
    await seguir(update, ctx, ctx.args)


async def texto_livre(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await seguir(update, ctx, update.message.text.split())


async def cmd_lista(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    subs = subs_de(update.effective_chat.id)
    if not subs:
        await update.message.reply_text("Ainda não segues nada. Escreve, por exemplo: lebron 24.5")
        return
    dados = ctx.application.bot_data["dados"]
    estados = await asyncio.gather(*(estado(dados, s) for s in subs))
    acabados = fins(update.effective_chat.id)
    blocos = []
    for i, (s, e) in enumerate(zip(subs, estados), 1):
        cabecalho = f"{i}. <b>{html.escape(s.jogador)}</b>"
        if s.linha is not None:
            cabecalho += f" · mais de {s.linha:g}"
        if not e or not e.startswith("🟢"):
            # Sem jogo a decorrer, a estatística não aparece no estado: mostra-a aqui.
            cabecalho += f" · {STATS[s.stat]['nome']}"
        if s.id in acabados:
            falta = max(1, round((acabados[s.id] + REMOVER_APOS * 60 - time.time()) / 60))
            e = f"🏁 Jogo terminado · sai da lista daqui a {falta} min"
        blocos.append(cabecalho + (f"\n{e}" if e else ""))
    await update.message.reply_html("\n\n".join(blocos))


async def cmd_remover(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    subs = subs_de(update.effective_chat.id)
    if ctx.args and ctx.args[0].lower() == "tudo":
        ids = [s.id for s in subs]
    else:
        # Aceita "/r 1 2 5", "/r 1,2,5" e intervalos como "/r 2-4".
        numeros = set()
        try:
            for parte in " ".join(ctx.args).replace(",", " ").split():
                if "-" in parte:
                    a, b = (int(x) for x in parte.split("-", 1))
                    numeros.update(range(min(a, b), max(a, b) + 1))
                else:
                    numeros.add(int(parte))
        except ValueError:
            numeros = set()
        invalidos = sorted(n for n in numeros if not 1 <= n <= len(subs))
        if not numeros or invalidos:
            extra = f" Não existe: {', '.join(map(str, invalidos))}." if invalidos else ""
            await update.message.reply_text(f"Usa /r 1 2 5, /r 2-4 ou /r tudo (vê os números em /l).{extra}")
            return
        ids = [subs[n - 1].id for n in sorted(numeros)]
    removidos = [s for s in subs if s.id in ids]
    with db() as con:
        con.executemany("DELETE FROM subs WHERE id = ?", [(i,) for i in ids])
    if not removidos:
        await update.message.reply_text("Não tinhas nada para remover.")
        return
    lista = "\n".join(f"• {descrever(s)}" for s in removidos)
    await update.message.reply_html(f"🗑️ Removido{'s' if len(removidos) != 1 else ''}:\n{lista}")


async def cmd_limpar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Apaga as mensagens recentes da conversa (limite do Telegram: menos de 48 h) e volta a mostrar a ajuda.
    A lista de jogadores seguidos mantém-se (para a apagar: /r tudo)."""
    chat_id = update.effective_chat.id
    ultimo = update.message.message_id
    falhas = 0
    # Os IDs das mensagens numa conversa são sequenciais: apaga para trás, 100 de cada vez.
    for fim in range(ultimo, max(ultimo - 3000, 0), -100):
        ids = list(range(fim, max(fim - 100, 0), -1))
        try:
            await ctx.bot.delete_messages(chat_id, ids)
            falhas = 0
        except Exception:
            falhas += 1  # mensagens com mais de 48 h ou já apagadas
            if falhas >= 3:
                break
    await ctx.bot.send_message(chat_id, "🧹 Conversa limpa.\n\n" + AJUDA, parse_mode="HTML")


async def cmd_jogos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await enviar_jogos(update, ctx)


async def enviar_jogos(update: Update, ctx, intro: str = ""):
    try:
        jogos = await ctx.application.bot_data["dados"].jogos_hoje()
    except Exception as e:
        log.warning("scoreboard: %s", e)
        await update.message.reply_text("Não consegui ler os jogos agora. Tenta daqui a pouco.")
        return
    if not jogos:
        await update.message.reply_text("Não há jogos da NBA hoje.")
        return
    # Guardar a ordem para o /stats N usar os mesmos números.
    ctx.application.bot_data.setdefault("jogos_listados", {})[update.effective_chat.id] = [g["gameId"] for g in jogos]
    agora = datetime.now(timezone.utc)
    blocos = [intro] if intro else []
    for i, g in enumerate(jogos, 1):
        if g["gameStatus"] == 1 and g.get("gameTimeUTC"):
            inicio = datetime.fromisoformat(g["gameTimeUTC"].replace("Z", "+00:00"))
            blocos.append(f"{i}. {confronto(g['awayTeam']['teamTricode'], g['homeTeam']['teamTricode'])}\n{quando(inicio, agora)}")
        else:
            blocos.append(f"{i}. {marcador(g)}")
    blocos.append("📊 Para veres as stats de um jogo coloca /statsjogo e o número do jogo (ex.: /statsjogo 2)")
    await update.message.reply_text("\n\n".join(blocos))


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Stats de todos os jogadores de um jogo do /j, numerados para o /add."""
    dados: Dados = ctx.application.bot_data["dados"]
    chat_id = update.effective_chat.id
    ids = ctx.application.bot_data.get("jogos_listados", {}).get(chat_id)
    try:
        n = int(ctx.args[0])
    except (IndexError, ValueError):
        n = None
    if n is None or not ids:
        # Ainda não viu a lista (ou não disse o número): mostra os jogos de hoje e explica.
        await enviar_jogos(update, ctx, "👇 Primeiro escolhe o jogo. Estes são os jogos de hoje:")
        return
    if not 1 <= n <= len(ids):
        await enviar_jogos(update, ctx, f"O jogo {n} não está na lista. Estes são os jogos de hoje:")
        return
    try:
        game = (await dados.boxscore(ids[n - 1]))["game"]
    except Exception as e:
        log.warning("stats: %s", e)
        await update.message.reply_text("Não consegui ler as stats desse jogo agora. Tenta daqui a pouco.")
        return
    if game.get("gameStatus") == 1:
        await update.message.reply_text("⏳ Esse jogo ainda não começou, por isso ainda não há stats.\n"
                                        "Podes seguir já um jogador pelo nome, por exemplo: lebron")
        return

    numerados, linhas = [], [marcador(game)]
    for equipa in (game["awayTeam"], game["homeTeam"]):
        tri = equipa["teamTricode"]
        linhas.append(f"\n<b>{cores(tri)} {tri}</b>")
        fora = []
        for p in equipa.get("players", []):
            if p.get("status") == "INACTIVE":
                fora.append(html.escape(nome_completo(p)))
                continue
            numerados.append(nome_completo(p))
            linhas.append(f"{len(numerados)}. <b>{html.escape(nome_completo(p))}</b>\n    {linha_stats(p)}")
        if fora:
            linhas.append(f"🚫 Não jogam: {', '.join(fora)}")
    ctx.application.bot_data.setdefault("stats_listados", {})[chat_id] = numerados
    linhas.append("\n➕ Para seguires jogadores coloca /add e o número (ex.: /add 3 ou /add 3 7). "
                  "Depois escolhes a estatística nos botões.")
    texto = "\n".join(linhas)
    # O Telegram aceita até 4096 caracteres por mensagem.
    while texto:
        corte = texto.rfind("\n", 0, 4000) if len(texto) > 4000 else len(texto)
        await update.message.reply_html(texto[:corte])
        texto = texto[corte:].lstrip("\n")


async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Segue jogadores pelo número do último /stats."""
    nomes = ctx.application.bot_data.get("stats_listados", {}).get(update.effective_chat.id)
    if not nomes:
        await update.message.reply_text("Os números do /add são os jogadores do /statsjogo.\n"
                                        "Primeiro faz /j, depois /statsjogo N para veres os jogadores numerados.")
        return
    res, erro = parse_add(ctx.args)
    if erro:
        await update.message.reply_text(f"Não percebi. Coloca /add e o número do jogador no /statsjogo.\n{erro}")
        return
    numeros, stat, linha = res
    invalidos = [n for n in numeros if not 1 <= n <= len(nomes)]
    if invalidos:
        await update.message.reply_text(f"Não existe: {', '.join(map(str, invalidos))}. Os números vão de 1 a {len(nomes)}.")
        return
    if stat is None:
        await perguntar_stat(update, ctx, [nomes[n - 1] for n in numeros], linha)
        return
    respostas = [await criar_sub(ctx, update.effective_chat.id, nomes[n - 1], stat, linha) for n in numeros]
    await update.message.reply_html("\n\n".join(respostas))


# ---------- ciclo de leitura ----------
async def ciclo(app: Application):
    dados: Dados = app.bot_data["dados"]
    tracker: Tracker = app.bot_data["tracker"]
    terminados: set[str] = set()
    while True:
        try:
            subs = todas_subs()
            if subs:
                for g in await dados.jogos_hoje():
                    gid = g["gameId"]
                    if g["gameStatus"] == 1 or gid in terminados:
                        continue
                    box = await dados.boxscore(gid)
                    for chat_id, texto in tracker.processar(box, subs):
                        try:
                            await app.bot.send_message(chat_id, texto, parse_mode="HTML")
                        except Exception as e:
                            log.warning("envio para %s falhou: %s", chat_id, e)
                    if box["game"]["gameStatus"] == 3:
                        terminados.add(gid)
                # Jogos acabados: marcar a hora do fim e remover da lista passados REMOVER_APOS minutos.
                agora = time.time()
                with db() as con:
                    if tracker.terminados:
                        con.executemany("UPDATE subs SET fim = ? WHERE id = ? AND fim IS NULL",
                                        [(agora, i) for i in tracker.terminados])
                        tracker.terminados.clear()
                    apagados = con.execute("DELETE FROM subs WHERE fim IS NOT NULL AND fim < ?",
                                           (agora - REMOVER_APOS * 60,)).rowcount
                if apagados:
                    log.info("Removidos automaticamente: %s", apagados)
        except Exception as e:
            log.warning("ciclo: %s", e)
        await asyncio.sleep(INTERVALO)


async def ao_iniciar(app: Application):
    app.bot_data["dados"] = Dados(httpx.AsyncClient(headers=HEADERS, timeout=15))
    app.bot_data["tracker"] = Tracker()
    await app.bot.set_my_commands([
        BotCommand("l", "O que estás a seguir e quando joga"),
        BotCommand("j", "Jogos de hoje"),
        BotCommand("statsjogo", "Stats de um jogo (ex.: /statsjogo 2)"),
        BotCommand("add", "Seguir pelo número do /statsjogo (ex.: /add 3)"),
        BotCommand("r", "Remover (ex.: /r 1 2 5)"),
        BotCommand("p", "Todos os pedidos possíveis"),
        BotCommand("s", "Seguir (ou escreve só: lebron 24.5)"),
        BotCommand("limpar", "Apagar a conversa e começar do zero"),
        BotCommand("a", "Ajuda"),
    ])
    app.bot_data["ciclo"] = asyncio.get_running_loop().create_task(ciclo(app))


def main():
    db().close()
    app = Application.builder().token(TOKEN).post_init(ao_iniciar).build()
    app.add_handler(CommandHandler(["start", "a", "ajuda", "help"], cmd_ajuda))
    app.add_handler(CommandHandler(["s", "seguir", "f"], cmd_seguir))
    app.add_handler(CommandHandler(["l", "lista"], cmd_lista))
    app.add_handler(CommandHandler(["p", "pedidos"], cmd_pedidos))
    app.add_handler(CommandHandler(["diag"], cmd_diag))
    app.add_handler(CommandHandler(["limpar", "clear", "c"], cmd_limpar))
    app.add_handler(CommandHandler(["r", "remover"], cmd_remover))
    app.add_handler(CommandHandler(["j", "jogos"], cmd_jogos))
    app.add_handler(CommandHandler(["statsjogo", "stats", "st"], cmd_stats))
    app.add_handler(CallbackQueryHandler(escolheu_stat, pattern=r"^stat:"))
    app.add_handler(CommandHandler(["add", "seguir_n"], cmd_add))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, texto_livre))
    log.info("Bot a correr. Leitura a cada %ss.", INTERVALO)
    app.run_polling()


if __name__ == "__main__":
    main()
