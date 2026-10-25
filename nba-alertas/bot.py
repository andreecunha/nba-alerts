"""Bot do Telegram: alertas de estatísticas de jogadores da NBA ao vivo.

Correr:  TELEGRAM_TOKEN=xxxx python bot.py
"""
import asyncio
import html
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone

import httpx
from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from core import (STATS, Sub, Tracker, alvo, encontrar, falta_dd, jogadores_espn, jogos_calendario,
                  marcador, nome_completo, parse_seguir, proximo_jogo, quando, resumo_dd, texto_pedidos, valor_de)

TOKEN = os.environ["TELEGRAM_TOKEN"]
INTERVALO = int(os.getenv("INTERVALO", "20"))  # segundos entre leituras
DB_PATH = os.getenv("DB_PATH", "subs.db")

NBA = "https://cdn.nba.com/static/json"
SCOREBOARD = f"{NBA}/liveData/scoreboard/todaysScoreboard_00.json"
BOXSCORE = NBA + "/liveData/boxscore/boxscore_{}.json"
CALENDARIO = f"{NBA}/staticData/scheduleLeagueV2.json"
ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
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

    async def jogos_hoje(self) -> list[dict]:
        return (await self._json(SCOREBOARD, 10))["scoreboard"]["games"]

    async def boxscore(self, gid: str) -> dict:
        return await self._json(BOXSCORE.format(gid), 10)

    async def calendario(self) -> list[dict]:
        t, valor = self.cache.get("calendario", (0, None))
        if valor is None or time.time() - t > 6 * 3600:
            valor = jogos_calendario(await self._json(CALENDARIO, 6 * 3600))
            self.cache["calendario"] = (time.time(), valor)
        return valor

    async def jogadores(self) -> list[dict]:
        """Todos os jogadores da liga com a equipa (plantéis da ESPN, atualizados de 12 em 12 h)."""
        t, valor = self.cache.get("jogadores", (0, None))
        if valor is not None and time.time() - t < 12 * 3600:
            return valor
        equipas = (await self._json(ESPN, 12 * 3600))["sports"][0]["leagues"][0]["teams"]

        async def plantel(e):
            r = await self.http.get(f"{ESPN}/{e['team']['id']}/roster")
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
    return con


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
        game = (await dados.boxscore(g["gameId"]))["game"]
        achados = encontrar(game["homeTeam"]["players"] + game["awayTeam"]["players"], nome)
        if len(achados) == 1:
            return game, achados[0]
    return None, None


async def estado(dados: Dados, s: Sub, tracker: Tracker | None = None) -> str:
    """Linha de estado: o que tem agora, ou quanto falta para o jogo começar."""
    agora = datetime.now(timezone.utc)
    try:
        hoje = await dados.jogos_hoje()
        estados = {g["gameId"]: g["gameStatus"] for g in hoje}
        equipa = None
        try:
            achados = encontrar(await dados.jogadores(), s.jogador)
            if len(achados) == 1:
                equipa = achados[0]["team"]
        except Exception as e:
            log.warning("plantéis: %s", e)

        jogo = None
        if equipa:
            jogo = proximo_jogo(await dados.calendario(), equipa, agora, estados)

        # A jogar agora?
        if jogo is None or jogo["estado"] == 2:
            if jogo:
                game = (await dados.boxscore(jogo["gameId"]))["game"]
                achados = encontrar(game["homeTeam"]["players"] + game["awayTeam"]["players"], s.jogador)
                p = achados[0] if len(achados) == 1 else None
            else:
                game, p = await procurar_ao_vivo(dados, s.jogador)
            if p:
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
            if jogo is None:
                return "Sem jogos marcados para já." if equipa else ""

        if jogo["estado"] == 1:
            return f"⏳ {jogo['fora']} @ {jogo['casa']} {quando(jogo['inicio'], agora)}"
    except Exception as e:
        log.warning("estado: %s", e)
    return ""


# ---------- comandos ----------
AJUDA = (
    "<b>Alertas NBA</b> 🏀\n\n"
    "Para seguir, escreve só o jogador, sem barra:\n"
    "• <code>lebron</code> — aviso a cada cesto\n"
    "• <code>lebron 24.5</code> — com linha de pontos\n"
    "• <code>jokic ressaltos 11.5</code> — outra estatística\n"
    "(também dá com /s antes)\n\n"
    "Sem estatística, segue pontos.\n\n"
    "/p — todos os pedidos possíveis (ressaltos, triplos, duplo-duplo…)\n"
    "/l — o que estás a seguir e quando joga\n"
    "/r 2 — remove o número 2 da lista · /r tudo\n"
    "/j — jogos de hoje\n"
    "/a — esta ajuda\n\n"
    "Os dados vêm da NBA com cerca de 10–30 s de atraso em relação à TV."
)


async def cmd_ajuda(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(AJUDA)


async def cmd_pedidos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(texto_pedidos())


async def seguir(update: Update, ctx: ContextTypes.DEFAULT_TYPE, args: list[str]):
    res, erro = parse_seguir(args)
    if erro:
        await update.message.reply_text(erro)
        return
    consulta, stat, linha = res
    dados: Dados = ctx.application.bot_data["dados"]

    # Confirmar o jogador e guardar o nome completo.
    nome = consulta
    try:
        achados = encontrar(await dados.jogadores(), consulta)
    except Exception as e:
        log.warning("plantéis: %s", e)
        achados = None
    if achados is not None and len(achados) > 1:
        nomes = "\n".join(f"• {html.escape(nome_completo(p))} ({p['team']})" for p in achados[:8])
        await update.message.reply_html(f"Há vários jogadores com esse nome. Qual deles?\n{nomes}\n\nEscreve o nome completo.")
        return
    if achados:
        nome = nome_completo(achados[0])
    else:
        game, p = await procurar_ao_vivo(dados, consulta)
        if p:
            nome = nome_completo(p)
        elif achados is not None:
            await update.message.reply_html(f"Não encontrei nenhum jogador «{html.escape(consulta)}». Verifica o nome.")
            return

    with db() as con:
        cur = con.execute("INSERT INTO subs (chat_id, jogador, stat, linha, perto) VALUES (?,?,?,?,NULL)",
                          (update.effective_chat.id, nome, stat, linha))
        sub = Sub(cur.lastrowid, update.effective_chat.id, nome, stat, linha)
    linha_estado = await estado(dados, sub, ctx.application.bot_data["tracker"])
    texto = f"✅ A seguir: {descrever(sub)}"
    if linha_estado:
        texto += f"\n{linha_estado}"
    await update.message.reply_html(texto)


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
    blocos = [f"{i}. {descrever(s)}" + (f"\n{e}" if e else "") for i, (s, e) in enumerate(zip(subs, estados), 1)]
    await update.message.reply_html("\n\n".join(blocos))


async def cmd_remover(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    subs = subs_de(update.effective_chat.id)
    if ctx.args and ctx.args[0].lower() == "tudo":
        ids = [s.id for s in subs]
    else:
        try:
            ids = [subs[int(ctx.args[0]) - 1].id]
        except (IndexError, ValueError):
            await update.message.reply_text("Usa /r N (vê o número em /l) ou /r tudo")
            return
    with db() as con:
        con.executemany("DELETE FROM subs WHERE id = ?", [(i,) for i in ids])
    await update.message.reply_text(f"🗑️ Removido{'s' if len(ids) != 1 else ''}: {len(ids)}")


async def cmd_jogos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        jogos = await ctx.application.bot_data["dados"].jogos_hoje()
    except Exception as e:
        log.warning("scoreboard: %s", e)
        await update.message.reply_text("Não consegui ler os jogos da NBA agora. Tenta daqui a pouco.")
        return
    if not jogos:
        await update.message.reply_text("Não há jogos da NBA hoje.")
        return
    agora = datetime.now(timezone.utc)
    linhas = []
    for g in jogos:
        if g["gameStatus"] == 1 and g.get("gameTimeUTC"):
            inicio = datetime.fromisoformat(g["gameTimeUTC"].replace("Z", "+00:00"))
            linhas.append(f"{g['awayTeam']['teamTricode']} @ {g['homeTeam']['teamTricode']} · {quando(inicio, agora)}")
        else:
            linhas.append(marcador(g))
    await update.message.reply_text("\n".join(linhas))


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
        except Exception as e:
            log.warning("ciclo: %s", e)
        await asyncio.sleep(INTERVALO)


async def ao_iniciar(app: Application):
    app.bot_data["dados"] = Dados(httpx.AsyncClient(headers=HEADERS, timeout=15))
    app.bot_data["tracker"] = Tracker()
    await app.bot.set_my_commands([
        BotCommand("l", "O que estás a seguir e quando joga"),
        BotCommand("j", "Jogos de hoje"),
        BotCommand("r", "Remover (ex.: /r 2)"),
        BotCommand("p", "Todos os pedidos possíveis"),
        BotCommand("s", "Seguir (ou escreve só: lebron 24.5)"),
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
    app.add_handler(CommandHandler(["r", "remover"], cmd_remover))
    app.add_handler(CommandHandler(["j", "jogos"], cmd_jogos))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, texto_livre))
    log.info("Bot a correr. Leitura a cada %ss.", INTERVALO)
    app.run_polling()


if __name__ == "__main__":
    main()
