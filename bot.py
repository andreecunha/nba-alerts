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
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from core import (STATS, Sub, Tracker, alvo, box_espn, calendario_espn, confronto, cores, descr_linha, encontrar,
                  falta_dd, jogadores_espn, jogo_espn, linha_stats, marcador, nome_completo, parse_add, parse_seguir,
                  proximo_jogo, quando, resumo_dd, situacao_linha, texto_pedidos, valor_de, _numero)

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
PASTA_BOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH", os.path.join(PASTA_BOT, "subs.db"))
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
def preparar_db():
    """Garante que a pasta da base de dados existe. Se não der (ex.: disco /data do Fly não criado),
    usa a pasta do bot e avisa nos logs em vez de o bot morrer ao arrancar."""
    global DB_PATH
    try:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        sqlite3.connect(DB_PATH).close()
    except (OSError, sqlite3.Error) as e:
        alternativa = os.path.join(PASTA_BOT, "subs.db")
        log.warning("Não consegui usar %s (%s). A usar %s — as listas perdem-se ao atualizar o bot.",
                    DB_PATH, e, alternativa)
        DB_PATH = alternativa
    log.info("Base de dados: %s", DB_PATH)


def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS subs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, jogador TEXT,
        stat TEXT, linha REAL, perto INTEGER)""")
    for coluna in ("fim REAL",        # quando o jogo acabou (para remoção automática)
                   "lado TEXT"):      # "over" ou "under"
        try:
            con.execute(f"ALTER TABLE subs ADD COLUMN {coluna}")
        except sqlite3.OperationalError:
            pass  # a coluna já existe
    return con


def fins(chat_id: int) -> dict[int, float]:
    with db() as con:
        return dict(con.execute("SELECT id, fim FROM subs WHERE chat_id = ? AND fim IS NOT NULL", (chat_id,)))


def todas_subs() -> list[Sub]:
    with db() as con:
        return [Sub(*r[:6], r[6] or "over")
                for r in con.execute("SELECT id, chat_id, jogador, stat, linha, perto, lado FROM subs")]


def subs_de(chat_id: int) -> list[Sub]:
    return [s for s in todas_subs() if s.chat_id == chat_id]


def descrever(s: Sub) -> str:
    t = f"<b>{html.escape(s.jogador)}</b> · {STATS[s.stat]['nome']}"
    if s.linha is not None:
        t += f" · {descr_linha(s)}"
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
        return f"🔴 Não está a jogar este jogo\n{marcador(game)}"
    st = p.get("statistics", {})
    valor = valor_de(s.stat, st)
    if tracker and s.id:
        tracker.semear(s, game["gameId"], st)
    alvo_dd = STATS[s.stat].get("dd")
    if alvo_dd:
        falta = falta_dd(st, alvo_dd)
        fim = "✅ já tem" if falta is None else f"{valor}/{alvo_dd} categorias · falta{'m' if falta > 1 else ''} {falta}"
        return f"🟢 {resumo_dd(st)} · {fim}\n{marcador(game)}"
    texto = f"🟢 <b>{valor}</b> {STATS[s.stat]['nome']}"
    if s.linha is not None:
        texto += f" · {situacao_linha(s, valor)}"
    return f"{texto}\n{marcador(game)}"


async def estado(dados: Dados, s: Sub, tracker: Tracker | None = None) -> str:
    """Linha de estado: o que tem agora, ou quanto falta para o jogo começar.
    Primeiro procura nos jogos a decorrer (só precisa dos jogos do dia); só depois usa plantéis e calendário."""
    agora = datetime.now(timezone.utc)
    try:
        game, p = await procurar_ao_vivo(dados, s.jogador)
        if p:
            return texto_ao_vivo(s, game, p, tracker)
    except Exception:
        log.exception("estado: jogos a decorrer")
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
            return f"⏳ {confronto(jogo['fora'], jogo['casa'])}\n{quando(jogo['inicio'], agora)}"
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


# ---------- menu principal (botões fixos em baixo) ----------
BOTAO_JOGOS, BOTAO_LISTA, BOTAO_AJUDA = "🏀 Jogos de hoje", "📋 A minha lista", "❓ Ajuda"
MENU = ReplyKeyboardMarkup([[BOTAO_JOGOS, BOTAO_LISTA], [BOTAO_AJUDA]], resize_keyboard=True, is_persistent=True)
FECHAR = InlineKeyboardButton("✖️ Fechar", callback_data="fechar")

AJUDA = (
    "🏀 <b>NBA Alerts</b>\n\n"
    "Usa os botões em baixo:\n\n"
    f"<b>{BOTAO_JOGOS}</b>\n"
    "Escolhe o jogo, depois a equipa e os jogadores. "
    "Para cada jogador escolhes a estatística e a linha (over/under).\n\n"
    f"<b>{BOTAO_LISTA}</b>\n"
    "O que estás a seguir, como está cada aposta, e botão para remover.\n\n"
    "Também podes escrever o nome de um jogador, ex.: <code>lebron</code>\n"
    "Atalhos: <code>lebron pontos 24.5</code> (over) · <code>lebron pontos under 24.5</code>\n\n"
    "Recebes aviso a cada cesto, quando a linha está perto, quando bate (ou o under se perde), "
    "estatísticas anuladas, 4–5 faltas e o resultado no fim.\n\n"
    "Outros comandos: /p pedidos possíveis · /limpar apaga a conversa\n\n"
    "Os dados vêm da ESPN com cerca de 10–30 s de atraso em relação à TV."
)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "Olá! 👋 Eu aviso-te no telemóvel quando os teus jogadores da NBA marcam, ressaltam, assistem…\n\n"
        "Os botões do menu ficam aqui em baixo 👇", reply_markup=MENU)
    await update.message.reply_html(
        "Queres ver os jogos de hoje?",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏀 Sim, ver jogos", callback_data="menu:jogos"),
                                            InlineKeyboardButton("Agora não", callback_data="fechar")]]))


async def cmd_ajuda(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(AJUDA, reply_markup=MENU)


async def cmd_pedidos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(texto_pedidos())


async def fechar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.application.bot_data.get("pendente", {}).pop(q.message.chat.id, None)
    try:
        await q.message.delete()
    except Exception:
        await q.edit_message_text("Menu fechado.")


async def menu_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "menu:jogos":
        try:
            await q.message.delete()
        except Exception:
            pass
        await enviar_jogos(ctx, q.message.chat.id)


# ---------- jogos de hoje ----------
async def enviar_jogos(ctx, chat_id: int, intro: str = ""):
    try:
        jogos = await ctx.application.bot_data["dados"].jogos_hoje()
    except Exception as e:
        log.warning("scoreboard: %s", e)
        await ctx.bot.send_message(chat_id, "Não consegui ler os jogos agora. Tenta daqui a pouco.")
        return
    if not jogos:
        await ctx.bot.send_message(chat_id, "Não há jogos da NBA hoje.")
        return
    ctx.application.bot_data.setdefault("jogos_listados", {})[chat_id] = [g["gameId"] for g in jogos]
    agora = datetime.now(timezone.utc)
    blocos = [intro] if intro else []
    for i, g in enumerate(jogos, 1):
        if g["gameStatus"] == 1 and g.get("gameTimeUTC"):
            inicio = datetime.fromisoformat(g["gameTimeUTC"].replace("Z", "+00:00"))
            blocos.append(f"{i}. {confronto(g['awayTeam']['teamTricode'], g['homeTeam']['teamTricode'])}\n{quando(inicio, agora)}")
        else:
            blocos.append(f"{i}. {marcador(g)}")
    blocos.append("📊 Que jogo queres ver?")
    botoes = [InlineKeyboardButton(str(i), callback_data=f"jogo:{i}") for i in range(1, len(jogos) + 1)]
    teclado = [botoes[i:i + 4] for i in range(0, len(botoes), 4)] + [[FECHAR]]
    await ctx.bot.send_message(chat_id, "\n\n".join(blocos), reply_markup=InlineKeyboardMarkup(teclado))


async def cmd_jogos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await enviar_jogos(ctx, update.effective_chat.id)


async def jogo_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await mostrar_jogo(ctx, q.message.chat.id, int(q.data.split(":")[1]))


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        n = int(ctx.args[0])
    except (IndexError, ValueError):
        n = None
    if n is None or not ctx.application.bot_data.get("jogos_listados", {}).get(chat_id):
        await enviar_jogos(ctx, chat_id, "👇 Primeiro escolhe o jogo. Estes são os jogos de hoje:")
        return
    await mostrar_jogo(ctx, chat_id, n)


async def mostrar_jogo(ctx, chat_id: int, n: int):
    """Stats de todos os jogadores do jogo N, com botões para escolher a equipa e seguir jogadores."""
    dados: Dados = ctx.application.bot_data["dados"]
    ids = ctx.application.bot_data.get("jogos_listados", {}).get(chat_id) or []
    if not 1 <= n <= len(ids):
        await enviar_jogos(ctx, chat_id, f"O jogo {n} não está na lista. Estes são os jogos de hoje:")
        return
    try:
        game = (await dados.boxscore(ids[n - 1]))["game"]
    except Exception as e:
        log.warning("stats: %s", e)
        await ctx.bot.send_message(chat_id, "Não consegui ler as stats desse jogo agora. Tenta daqui a pouco.")
        return
    if game.get("gameStatus") == 1:
        await ctx.bot.send_message(chat_id, "⏳ Esse jogo ainda não começou, por isso ainda não há stats.\n\n"
                                            "Podes seguir já um jogador pelo nome, por exemplo: lebron")
        return

    numerados, linhas, equipas = [], [marcador(game)], {}
    for equipa in (game["awayTeam"], game["homeTeam"]):
        tri = equipa["teamTricode"]
        linhas.append(f"\n<b>{cores(tri)} {tri}</b>")
        fora, ativos = [], []
        for p in equipa.get("players", []):
            if p.get("status") == "INACTIVE":
                fora.append(html.escape(nome_completo(p)))
                continue
            numerados.append(nome_completo(p))
            ativos.append(p)
            linhas.append(f"{len(numerados)}. <b>{html.escape(nome_completo(p))}</b>\n    {linha_stats(p)}")
        if fora:
            linhas.append(f"🔴 Não jogam: {', '.join(fora)}")
        # Para os botões: quem joga mais aparece primeiro.
        ativos.sort(key=lambda p: -int(p["statistics"].get("minutes", 0) or 0))
        equipas[tri] = [(nome_completo(p), int(p["statistics"].get("points", 0) or 0)) for p in ativos]
    ctx.application.bot_data.setdefault("stats_listados", {})[chat_id] = numerados
    ctx.application.bot_data.setdefault("escolha", {})[chat_id] = {"equipas": equipas, "sel": [], "msg": None}
    linhas.append("\n➕ Queres seguir algum jogador? Escolhe a equipa:")
    teclado = InlineKeyboardMarkup([[InlineKeyboardButton(f"{cores(t)} {t}", callback_data=f"eq:{t}") for t in equipas],
                                    [FECHAR]])
    texto = "\n".join(linhas)
    while texto:  # o Telegram aceita até 4096 caracteres por mensagem
        corte = texto.rfind("\n", 0, 4000) if len(texto) > 4000 else len(texto)
        ultimo = corte >= len(texto)
        await ctx.bot.send_message(chat_id, texto[:corte], parse_mode="HTML", reply_markup=teclado if ultimo else None)
        texto = texto[corte:].lstrip("\n")


# ---------- escolher jogadores com botões ----------
def teclado_jogadores(esc: dict, tri: str) -> InlineKeyboardMarkup:
    botoes = []
    for i, (nome, pts) in enumerate(esc["equipas"][tri]):
        marca = "✅ " if nome in esc["sel"] else ""
        botoes.append(InlineKeyboardButton(f"{marca}{nome} · {pts}", callback_data=f"pl:{tri}:{i}"))
    linhas = [botoes[i:i + 2] for i in range(0, len(botoes), 2)]
    outra = [t for t in esc["equipas"] if t != tri]
    fundo = [InlineKeyboardButton(f"🔁 {cores(outra[0])} {outra[0]}", callback_data=f"eq:{outra[0]}")] if outra else []
    fundo.append(InlineKeyboardButton(f"▶️ Seguir ({len(esc['sel'])})", callback_data="plok"))
    linhas += [fundo, [FECHAR]]
    return InlineKeyboardMarkup(linhas)


def texto_escolha(esc: dict, tri: str) -> str:
    texto = f"👆 Toca nos jogadores de <b>{cores(tri)} {tri}</b> que queres seguir (o número é os pontos)."
    if esc["sel"]:
        texto += "\n\nEscolhidos: " + ", ".join(f"<b>{html.escape(n)}</b>" for n in esc["sel"])
    return texto + "\n\nNo fim carrega em ▶️ Seguir."


async def equipa_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat.id
    esc = ctx.application.bot_data.get("escolha", {}).get(chat_id)
    tri = q.data.split(":")[1]
    if not esc or tri not in esc["equipas"]:
        await q.message.reply_text("Este menu já expirou. Volta a escolher o jogo.", reply_markup=MENU)
        return
    if esc["msg"] == q.message.message_id:
        await q.edit_message_text(texto_escolha(esc, tri), parse_mode="HTML", reply_markup=teclado_jogadores(esc, tri))
    else:
        m = await ctx.bot.send_message(chat_id, texto_escolha(esc, tri), parse_mode="HTML",
                                       reply_markup=teclado_jogadores(esc, tri))
        esc["msg"] = m.message_id


async def jogador_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = q.message.chat.id
    esc = ctx.application.bot_data.get("escolha", {}).get(chat_id)
    _, tri, i = q.data.split(":")
    if not esc or tri not in esc["equipas"]:
        await q.answer("Este menu já expirou.")
        return
    nome = esc["equipas"][tri][int(i)][0]
    if nome in esc["sel"]:
        esc["sel"].remove(nome)
        await q.answer(f"Tiraste {nome}")
    else:
        esc["sel"].append(nome)
        await q.answer(f"Escolheste {nome}")
    await q.edit_message_text(texto_escolha(esc, tri), parse_mode="HTML", reply_markup=teclado_jogadores(esc, tri))


async def jogadores_ok(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = q.message.chat.id
    esc = ctx.application.bot_data.get("escolha", {}).get(chat_id)
    if not esc or not esc["sel"]:
        await q.answer("Toca primeiro num jogador para o escolheres.", show_alert=True)
        return
    await q.answer()
    nomes = list(esc["sel"])
    esc["sel"] = []
    await q.edit_message_text("Escolhidos: " + ", ".join(f"<b>{html.escape(n)}</b>" for n in nomes), parse_mode="HTML")
    await iniciar_fila(ctx, chat_id, nomes)


# ---------- perguntas: estatística → over/under → linha ----------
BOTOES_STATS = [("Pontos", "pts"), ("Ressaltos", "reb"), ("Assistências", "ast"),
                ("Triplos", "3pt"), ("Roubos", "stl"), ("Desarmes", "blk"),
                ("Pts+Res+Ast", "pra"), ("Pts+Res", "pr"), ("Pts+Ast", "pa"),
                ("Res+Ast", "ra"), ("Duplo-duplo", "dd"), ("Triplo-duplo", "td")]
CANCELAR = InlineKeyboardButton("✖️ Cancelar", callback_data="cancelar")


def teclado_stats() -> InlineKeyboardMarkup:
    botoes = [InlineKeyboardButton(t, callback_data=f"stat:{k}") for t, k in BOTOES_STATS]
    return InlineKeyboardMarkup([botoes[i:i + 3] for i in range(0, len(botoes), 3)] + [[CANCELAR]])


TECLADO_LADO = InlineKeyboardMarkup([
    [InlineKeyboardButton("📈 Over (mais de)", callback_data="lado:over"),
     InlineKeyboardButton("📉 Under (menos de)", callback_data="lado:under")],
    [InlineKeyboardButton("Sem linha (só avisos)", callback_data="lado:sem")],
    [CANCELAR]])


def pendente(ctx, chat_id: int) -> dict | None:
    return ctx.application.bot_data.setdefault("pendente", {}).get(chat_id)


def progresso(pend: dict) -> str:
    feito = pend["total"] - len(pend["fila"])
    return f" ({feito + 1} de {pend['total']})" if pend["total"] > 1 else ""


async def iniciar_fila(ctx, chat_id: int, nomes: list[str], stat=None, linha=None, lado="over", aviso: str = ""):
    """Começa as perguntas para uma lista de jogadores (um de cada vez)."""
    ctx.application.bot_data.setdefault("pendente", {})[chat_id] = {
        "fila": list(nomes), "total": len(nomes), "stat_fixa": stat, "stat": stat,
        "linha_fixa": linha, "lado_fixo": lado, "lado": None, "espera_linha": False}
    await proximo_passo(ctx, chat_id, aviso)


async def proximo_passo(ctx, chat_id: int, aviso: str = "", q=None):
    """Faz a pergunta seguinte para o jogador atual, ou adiciona-o se já houver tudo o que é preciso."""
    pend = pendente(ctx, chat_id)
    while pend and pend["fila"]:
        nome = html.escape(pend["fila"][0])
        if pend["stat"] is None:
            await ctx.bot.send_message(chat_id, f"{aviso}📊 Que estatística queres que eu notifique para "
                                                f"<b>{nome}</b>?{progresso(pend)}",
                                       parse_mode="HTML", reply_markup=teclado_stats())
            return
        if not STATS[pend["stat"]].get("dd") and pend["linha_fixa"] is None and pend["lado"] is None:
            await ctx.bot.send_message(chat_id, f"🎯 <b>{nome}</b> · {STATS[pend['stat']]['nome']}{progresso(pend)}\n\n"
                                                f"Apostaste no over ou no under?",
                                       parse_mode="HTML", reply_markup=TECLADO_LADO)
            return
        if pend["espera_linha"]:
            return
        # Já temos tudo: adiciona.
        linha = None if (STATS[pend["stat"]].get("dd") or pend["lado"] == "sem") else pend["linha_fixa"]
        lado = pend["lado"] if pend["lado"] in ("over", "under") else pend["lado_fixo"]
        await adicionar_atual(ctx, chat_id, linha, lado)
        pend = pendente(ctx, chat_id)


async def adicionar_atual(ctx, chat_id: int, linha, lado: str):
    pend = pendente(ctx, chat_id)
    nome = pend["fila"].pop(0)
    texto = await criar_sub(ctx, chat_id, nome, pend["stat"], linha, lado)
    await ctx.bot.send_message(chat_id, texto, parse_mode="HTML")
    # prepara o próximo jogador
    pend.update({"stat": pend["stat_fixa"], "lado": None, "espera_linha": False})
    if not pend["fila"]:
        ctx.application.bot_data["pendente"].pop(chat_id, None)


async def stat_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat.id
    pend = pendente(ctx, chat_id)
    if not pend or not pend["fila"]:
        await q.edit_message_text("Este pedido já expirou. Volta a escolher o jogador.")
        return
    pend["stat"] = q.data.split(":")[1]
    await q.edit_message_text(f"📊 <b>{html.escape(pend['fila'][0])}</b> · {STATS[pend['stat']]['nome']}",
                              parse_mode="HTML")
    await proximo_passo(ctx, chat_id)


async def lado_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat.id
    pend = pendente(ctx, chat_id)
    if not pend or not pend["fila"] or pend["stat"] is None:
        await q.edit_message_text("Este pedido já expirou. Volta a escolher o jogador.")
        return
    lado = q.data.split(":")[1]
    pend["lado"] = lado
    nome = html.escape(pend["fila"][0])
    if lado == "sem":
        await q.edit_message_text(f"🎯 <b>{nome}</b> · {STATS[pend['stat']]['nome']} · sem linha", parse_mode="HTML")
        await proximo_passo(ctx, chat_id)
        return
    pend["espera_linha"] = True
    exemplo = "24.5" if pend["stat"] in ("pts", "pra", "pr", "pa") else "5.5"
    await q.edit_message_text(
        f"🎯 <b>{nome}</b> · {STATS[pend['stat']]['nome']} · {'📈 Over' if lado == 'over' else '📉 Under'}\n\n"
        f"✍️ Escreve a linha da aposta (ex.: <code>{exemplo}</code>)",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[CANCELAR]]))


async def cancelar_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    pend = ctx.application.bot_data.setdefault("pendente", {}).pop(q.message.chat.id, None)
    resto = len(pend["fila"]) if pend else 0
    extra = f" ({resto} jogador{'es' if resto != 1 else ''} por adicionar)" if resto else ""
    await q.edit_message_text(f"Cancelado{extra}. Nada mais foi adicionado.")


async def recebeu_linha(update: Update, ctx, pend: dict):
    """O utilizador escreveu o número da linha."""
    texto = update.message.text.strip().lower().replace(",", ".")
    linha = _numero(texto)
    if linha is None or linha < 0:
        await update.message.reply_text("Escreve só o número da linha, por exemplo 24.5 (ou carrega em Cancelar).")
        return
    pend["espera_linha"] = False
    await adicionar_atual(ctx, update.effective_chat.id, linha, pend["lado"])
    await proximo_passo(ctx, update.effective_chat.id)


# ---------- seguir pelo nome escrito ----------
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


async def seguir(update: Update, ctx: ContextTypes.DEFAULT_TYPE, args: list[str]):
    res, erro = parse_seguir(args)
    if erro:
        await update.message.reply_text(erro + "\n\nVê todos os pedidos possíveis em /p", reply_markup=MENU)
        return
    consulta, stat, linha, lado = res
    dados: Dados = ctx.application.bot_data["dados"]
    estado_nome, valor = await resolver(dados, consulta)
    aviso = ""
    if estado_nome == "nada" and len(consulta.split()) > 1:
        # Talvez a última palavra seja uma estatística que não conheço (ex.: "lebron rebotes").
        resto, palavra = consulta.rsplit(" ", 1)
        estado2, valor2 = await resolver(dados, resto)
        if estado2 != "nada":
            estado_nome, valor, stat = estado2, valor2, None
            aviso = f"Não conheço a estatística «{html.escape(palavra)}».\n\n"
    if estado_nome == "varios":
        nomes = "\n".join(f"• {html.escape(nome_completo(p))} ({p['team']})" for p in valor[:8])
        await update.message.reply_html(f"Há vários jogadores com esse nome. Qual deles?\n\n{nomes}\n\nEscreve o nome completo.")
        return
    if estado_nome == "nada":
        await update.message.reply_html(f"Não encontrei nenhum jogador «{html.escape(consulta)}».\n\n"
                                        f"Verifica o nome ou carrega em <b>{BOTAO_JOGOS}</b> para escolheres da lista.",
                                        reply_markup=MENU)
        return
    await iniciar_fila(ctx, update.effective_chat.id, [valor], stat, linha, lado, aviso)


async def criar_sub(ctx, chat_id: int, nome: str, stat: str, linha, lado: str = "over") -> str:
    """Guarda o seguimento e devolve a mensagem de confirmação com o estado atual."""
    lado = lado if linha is not None else "over"
    with db() as con:
        cur = con.execute("INSERT INTO subs (chat_id, jogador, stat, linha, perto, lado) VALUES (?,?,?,?,NULL,?)",
                          (chat_id, nome, stat, linha, lado))
        sub = Sub(cur.lastrowid, chat_id, nome, stat, linha, None, lado)
    linha_estado = await estado(ctx.application.bot_data["dados"], sub, ctx.application.bot_data["tracker"])
    texto = f"✅ A seguir: {descrever(sub)}"
    if linha_estado:
        texto += f"\n\n{linha_estado}"
    return texto


async def cmd_seguir(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Escreve o nome do jogador, por exemplo: lebron", reply_markup=MENU)
        return
    await seguir(update, ctx, ctx.args)


async def texto_livre(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    chat_id = update.effective_chat.id
    if texto == BOTAO_JOGOS:
        await enviar_jogos(ctx, chat_id)
        return
    if texto == BOTAO_LISTA:
        await enviar_lista(ctx, chat_id)
        return
    if texto == BOTAO_AJUDA:
        await update.message.reply_html(AJUDA, reply_markup=MENU)
        return
    pend = pendente(ctx, chat_id)
    if pend and pend.get("espera_linha"):
        await recebeu_linha(update, ctx, pend)
        return
    await seguir(update, ctx, texto.split())


# ---------- a minha lista ----------
async def texto_lista(ctx, chat_id: int) -> str | None:
    subs = subs_de(chat_id)
    if not subs:
        return None
    dados = ctx.application.bot_data["dados"]
    estados = await asyncio.gather(*(estado(dados, s) for s in subs))
    acabados = fins(chat_id)
    blocos = ["📋 <b>A tua lista</b>"]
    for i, (s, e) in enumerate(zip(subs, estados), 1):
        cabecalho = f"{i}. <b>{html.escape(s.jogador)}</b>"
        if s.linha is not None:
            cabecalho += f" · {descr_linha(s)}"
        if not e or not e.startswith("🟢"):
            cabecalho += f" · {STATS[s.stat]['nome']}"
        if s.id in acabados:
            falta = max(1, round((acabados[s.id] + REMOVER_APOS * 60 - time.time()) / 60))
            e = f"🏁 Jogo terminado · sai da lista daqui a {falta} min"
        blocos.append(cabecalho + (f"\n{e}" if e else ""))
    return "\n\n".join(blocos)


TECLADO_LISTA = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Atualizar", callback_data="lista:atualizar"),
                                       InlineKeyboardButton("🗑️ Remover", callback_data="rm:menu")]])


async def enviar_lista(ctx, chat_id: int):
    texto = await texto_lista(ctx, chat_id)
    if not texto:
        await ctx.bot.send_message(chat_id, f"Ainda não segues ninguém.\n\nCarrega em {BOTAO_JOGOS} para escolheres "
                                            f"jogadores, ou escreve um nome (ex.: lebron).", reply_markup=MENU)
        return
    await ctx.bot.send_message(chat_id, texto, parse_mode="HTML", reply_markup=TECLADO_LISTA)


async def cmd_lista(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await enviar_lista(ctx, update.effective_chat.id)


async def lista_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("A atualizar…")
    texto = await texto_lista(ctx, q.message.chat.id)
    try:
        await q.edit_message_text(texto or "A tua lista está vazia.", parse_mode="HTML",
                                  reply_markup=TECLADO_LISTA if texto else None)
    except Exception:
        pass  # nada mudou desde a última vez


def teclado_remover(chat_id: int) -> InlineKeyboardMarkup | None:
    subs = subs_de(chat_id)
    if not subs:
        return None
    linhas = [[InlineKeyboardButton(f"❌ {s.jogador} · {STATS[s.stat]['nome']}"
                                    + (f" · {'u' if s.under else 'o'}{s.linha:g}" if s.linha is not None else ""),
                                    callback_data=f"rm:{s.id}")] for s in subs]
    linhas.append([InlineKeyboardButton("🗑️ Remover todos", callback_data="rm:tudo"),
                   InlineKeyboardButton("✅ Feito", callback_data="fechar")])
    return InlineKeyboardMarkup(linhas)


async def remover_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = q.message.chat.id
    acao = q.data.split(":")[1]
    if acao == "menu":
        await q.answer()
        teclado = teclado_remover(chat_id)
        if teclado:
            await ctx.bot.send_message(chat_id, "🗑️ Toca em quem queres deixar de seguir:", reply_markup=teclado)
        return
    with db() as con:
        if acao == "tudo":
            con.execute("DELETE FROM subs WHERE chat_id = ?", (chat_id,))
        else:
            con.execute("DELETE FROM subs WHERE id = ? AND chat_id = ?", (int(acao), chat_id))
    await q.answer("Removido")
    teclado = teclado_remover(chat_id)
    if teclado:
        await q.edit_message_reply_markup(teclado)
    else:
        await q.edit_message_text("A tua lista está vazia.")


async def cmd_remover(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    subs = subs_de(update.effective_chat.id)
    if not ctx.args:
        teclado = teclado_remover(update.effective_chat.id)
        if teclado:
            await update.message.reply_text("🗑️ Toca em quem queres deixar de seguir:", reply_markup=teclado)
        else:
            await update.message.reply_text("A tua lista está vazia.", reply_markup=MENU)
        return
    if ctx.args[0].lower() == "tudo":
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
            await update.message.reply_text(f"Usa /r 1 2 5, /r 2-4 ou /r tudo (vê os números na lista).{extra}")
            return
        ids = [subs[n - 1].id for n in sorted(numeros)]
    removidos = [s for s in subs if s.id in ids]
    with db() as con:
        con.executemany("DELETE FROM subs WHERE id = ?", [(i,) for i in ids])
    if not removidos:
        await update.message.reply_text("Não tinhas nada para remover.")
        return
    lista = "\n".join(f"• {descrever(s)}" for s in removidos)
    await update.message.reply_html(f"🗑️ Removido{'s' if len(removidos) != 1 else ''}:\n\n{lista}")


# ---------- /add (atalho por números do /statsjogo) ----------
async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    nomes = ctx.application.bot_data.get("stats_listados", {}).get(update.effective_chat.id)
    if not nomes:
        await update.message.reply_text(f"Carrega em {BOTAO_JOGOS}, escolhe o jogo e depois a equipa — "
                                        f"aí escolhes os jogadores com botões.", reply_markup=MENU)
        return
    res, erro = parse_add(ctx.args)
    if erro:
        await update.message.reply_text(f"Não percebi. Coloca /add e o número do jogador.\n\n{erro}")
        return
    numeros, stat, linha = res
    invalidos = [n for n in numeros if not 1 <= n <= len(nomes)]
    if invalidos:
        await update.message.reply_text(f"Não existe: {', '.join(map(str, invalidos))}. Os números vão de 1 a {len(nomes)}.")
        return
    await iniciar_fila(ctx, update.effective_chat.id, [nomes[n - 1] for n in numeros], stat, linha)


# ---------- limpar ----------
async def cmd_limpar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Apaga as mensagens recentes da conversa (limite do Telegram: menos de 48 h).
    A lista de jogadores seguidos mantém-se."""
    chat_id = update.effective_chat.id
    ultimo = update.message.message_id
    falhas = 0
    for fim in range(ultimo, max(ultimo - 3000, 0), -100):
        ids = list(range(fim, max(fim - 100, 0), -1))
        try:
            await ctx.bot.delete_messages(chat_id, ids)
            falhas = 0
        except Exception:
            falhas += 1  # mensagens com mais de 48 h ou já apagadas
            if falhas >= 3:
                break
    await ctx.bot.send_message(chat_id, "🧹 Conversa limpa.", reply_markup=MENU)
    await ctx.bot.send_message(chat_id, "Queres ver os jogos de hoje?", reply_markup=InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏀 Sim, ver jogos", callback_data="menu:jogos"),
          InlineKeyboardButton("Agora não", callback_data="fechar")]]))


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


async def saude(reader, writer):
    """Responde 'ok' a quem bater na porta (o Fly.io verifica se a app está viva)."""
    try:
        await reader.read(1024)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
    finally:
        writer.close()


async def ao_iniciar(app: Application):
    # No Fly.io (ou se PORT estiver definido) abre uma porta só para responder "ok" à verificação de saúde.
    if os.getenv("PORT") or os.getenv("FLY_APP_NAME"):
        porta = int(os.getenv("PORT", "8080"))
        app.bot_data["saude"] = await asyncio.start_server(saude, "0.0.0.0", porta)
        log.info("Verificação de saúde na porta %s", porta)
    app.bot_data["dados"] = Dados(httpx.AsyncClient(headers=HEADERS, timeout=15))
    app.bot_data["tracker"] = Tracker()
    await app.bot.set_my_commands([
        BotCommand("start", "Menu inicial"),
        BotCommand("j", "Jogos de hoje"),
        BotCommand("l", "A minha lista"),
        BotCommand("p", "Todos os pedidos possíveis"),
        BotCommand("limpar", "Apagar a conversa e começar do zero"),
        BotCommand("a", "Ajuda"),
    ])
    app.bot_data["ciclo"] = asyncio.get_running_loop().create_task(ciclo(app))


def main():
    preparar_db()
    db().close()
    app = Application.builder().token(TOKEN).post_init(ao_iniciar).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler(["a", "ajuda", "help"], cmd_ajuda))
    app.add_handler(CommandHandler(["s", "seguir", "f"], cmd_seguir))
    app.add_handler(CommandHandler(["l", "lista"], cmd_lista))
    app.add_handler(CommandHandler(["p", "pedidos"], cmd_pedidos))
    app.add_handler(CommandHandler(["diag"], cmd_diag))
    app.add_handler(CommandHandler(["limpar", "clear", "c"], cmd_limpar))
    app.add_handler(CommandHandler(["r", "remover"], cmd_remover))
    app.add_handler(CommandHandler(["j", "jogos"], cmd_jogos))
    app.add_handler(CommandHandler(["statsjogo", "stats", "st"], cmd_stats))
    app.add_handler(CommandHandler(["add"], cmd_add))
    app.add_handler(CallbackQueryHandler(fechar, pattern=r"^fechar$"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(jogo_callback, pattern=r"^jogo:"))
    app.add_handler(CallbackQueryHandler(equipa_callback, pattern=r"^eq:"))
    app.add_handler(CallbackQueryHandler(jogador_callback, pattern=r"^pl:"))
    app.add_handler(CallbackQueryHandler(jogadores_ok, pattern=r"^plok$"))
    app.add_handler(CallbackQueryHandler(stat_callback, pattern=r"^stat:"))
    app.add_handler(CallbackQueryHandler(lado_callback, pattern=r"^lado:"))
    app.add_handler(CallbackQueryHandler(cancelar_callback, pattern=r"^cancelar$"))
    app.add_handler(CallbackQueryHandler(lista_callback, pattern=r"^lista:"))
    app.add_handler(CallbackQueryHandler(remover_callback, pattern=r"^rm:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, texto_livre))
    log.info("Bot a correr. Leitura a cada %ss.", INTERVALO)
    app.run_polling()


if __name__ == "__main__":
    main()
