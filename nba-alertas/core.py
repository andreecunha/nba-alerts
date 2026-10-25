"""Lógica central: interpretar comandos, encontrar jogadores no boxscore e decidir que alertas enviar.

Não depende do Telegram nem da rede, para poder ser testada com jogos simulados.
"""
import html
import math
import re
import unicodedata
from dataclasses import dataclass, field

# Categorias que contam para duplo-duplo / triplo-duplo (10 ou mais em cada).
CATEGORIAS_DD = [("points", "pts"), ("reboundsTotal", "res"), ("assists", "ast"), ("steals", "rou"), ("blocks", "des")]

# chave -> nome, nome em inglês, palavras aceites, campos do boxscore que se somam, margem "perto"
STATS = {
    "pts": {"nome": "pontos", "en": "Points", "palavras": ["pontos", "ponto", "points", "pts"],
            "campos": ["points"], "perto": 3},
    "reb": {"nome": "ressaltos", "en": "Rebounds", "palavras": ["ressaltos", "ressalto", "rebounds", "res", "reb"],
            "campos": ["reboundsTotal"], "perto": 1},
    "ast": {"nome": "assistências", "en": "Assists", "palavras": ["assistencias", "assistencia", "assists", "ast"],
            "campos": ["assists"], "perto": 1},
    "3pt": {"nome": "triplos", "en": "Threes", "palavras": ["triplos", "triplo", "threes", "3pm", "3pt", "3s"],
            "campos": ["threePointersMade"], "perto": 1},
    "stl": {"nome": "roubos", "en": "Steals", "palavras": ["roubos", "roubo", "steals", "stl"],
            "campos": ["steals"], "perto": 1},
    "blk": {"nome": "desarmes", "en": "Blocks", "palavras": ["desarmes", "desarme", "blocos", "blocks", "blk"],
            "campos": ["blocks"], "perto": 1},
    "pra": {"nome": "pts+res+ast", "en": "Pts+Reb+Ast", "palavras": ["pra"],
            "campos": ["points", "reboundsTotal", "assists"], "perto": 3},
    "pr": {"nome": "pts+res", "en": "Pts+Reb", "palavras": ["pr"], "campos": ["points", "reboundsTotal"], "perto": 2},
    "pa": {"nome": "pts+ast", "en": "Pts+Ast", "palavras": ["pa"], "campos": ["points", "assists"], "perto": 2},
    "ra": {"nome": "res+ast", "en": "Reb+Ast", "palavras": ["ra"], "campos": ["reboundsTotal", "assists"], "perto": 2},
    "dd": {"nome": "duplo-duplo", "en": "Double-double", "palavras": ["duplo-duplo", "double-double", "dd"],
           "campos": [], "perto": 2, "dd": 2},
    "td": {"nome": "triplo-duplo", "en": "Triple-double", "palavras": ["triplo-duplo", "triple-double", "td"],
           "campos": [], "perto": 2, "dd": 3},
}

ALIASES = {p: chave for chave, info in STATS.items() for p in info["palavras"]}

# Frases de duas palavras que viram uma só (ex.: "triplo duplo" -> "triplo-duplo").
JUNTAR = {("duplo", "duplo"): "duplo-duplo", ("triplo", "duplo"): "triplo-duplo",
          ("double", "double"): "double-double", ("triple", "double"): "triple-double"}


def texto_pedidos() -> str:
    linhas = ["<b>Pedidos possíveis</b>\n", "<b>Nome · English · atalhos</b>"]
    for chave, info in STATS.items():
        atalhos = ", ".join(p for p in info["palavras"] if p not in (info["nome"], info["en"].lower()))
        linhas.append(f"• {info['nome'].capitalize()} · {info['en']} · <code>{atalhos}</code>")
    linhas += [
        "",
        "<b>Exemplos</b>",
        "<code>lebron</code> — pontos, aviso a cada cesto",
        "<code>lebron 24.5</code> — pontos com linha",
        "<code>jokic res 11.5</code> — ressaltos com linha",
        "<code>curry 3pm 4.5</code> — triplos",
        "<code>sga pra 40.5</code> — pontos + ressaltos + assistências",
        "<code>jokic td</code> — triplo-duplo (sem linha)",
        "",
        "Duplo-duplo = 10 ou mais em 2 categorias (pontos, ressaltos, assistências, roubos, desarmes). "
        "Triplo-duplo = em 3.",
    ]
    return "\n".join(linhas)


def valor_de(stat: str, st: dict) -> int:
    """Valor da estatística. Para duplo/triplo-duplo é o nº de categorias com 10 ou mais."""
    if STATS[stat].get("dd"):
        return sum(int(st.get(c, 0) or 0) >= 10 for c, _ in CATEGORIAS_DD)
    return sum(int(st.get(c, 0) or 0) for c in STATS[stat]["campos"])


def resumo_dd(st: dict) -> str:
    """'24 pts · 8 res · 10 ast' (roubos e desarmes só aparecem se forem relevantes)."""
    partes = []
    for c, abrev in CATEGORIAS_DD:
        v = int(st.get(c, 0) or 0)
        if abrev in ("pts", "res", "ast") or v >= 5:
            partes.append(f"{v} {abrev}")
    return " · ".join(partes)


def falta_dd(st: dict, alvo_dd: int) -> int | None:
    """Quanto falta na categoria mais próxima para completar o duplo/triplo-duplo (None se já tem)."""
    valores = sorted((int(st.get(c, 0) or 0) for c, _ in CATEGORIAS_DD), reverse=True)
    em_falta = [10 - v for v in valores[:alvo_dd] if v < 10]
    return sum(em_falta) if em_falta else None


def norm(texto: str) -> str:
    """Minúsculas e sem acentos (Jokić -> jokic)."""
    t = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in t if not unicodedata.combining(c)).lower().strip()


def alvo(linha: float) -> int:
    """Valor mínimo para o 'mais de' entrar. Mais de 24.5 -> 25; mais de 10 -> 11."""
    return math.floor(linha) + 1


@dataclass
class Sub:
    id: int
    chat_id: int
    jogador: str
    stat: str
    linha: float | None = None
    perto: int | None = None

    def margem(self) -> int:
        return self.perto if self.perto is not None else STATS[self.stat]["perto"]


def _numero(t: str):
    try:
        return float(t.replace(",", "."))
    except ValueError:
        return None


def parse_seguir(args: list[str]):
    """'lebron james [pontos] [24.5]' -> (jogador, stat, linha) ou erro. Sem estatística = pontos."""
    exemplo = "Exemplo: lebron 24.5  ou  jokic ressaltos 11.5"
    args = [a for a in args if a.strip()]
    if len(args) >= 2:
        for i in range(len(args) - 1):
            par = (norm(args[i]), norm(args[i + 1]))
            if par in JUNTAR:
                args = args[:i] + [JUNTAR[par]] + args[i + 2:]
                break
    if not args:
        return None, exemplo
    linha = None
    if len(args) > 1 and _numero(args[-1]) is not None:
        linha = _numero(args.pop())
    stat = None  # sem estatística: o bot pergunta qual
    if len(args) > 1 and norm(args[-1]) in ALIASES:
        stat = ALIASES[norm(args.pop())]
    jogador = " ".join(args).strip()
    if not jogador or any(_numero(a) is not None for a in args):
        return None, f"Não percebi. {exemplo}"
    if linha is not None and linha < 0:
        return None, "A linha tem de ser positiva."
    if stat and STATS[stat].get("dd"):
        linha = None  # duplo/triplo-duplo não tem linha
    return (jogador, stat, linha), None


def nome_completo(p: dict) -> str:
    return p.get("name") or f"{p.get('firstName', '')} {p.get('familyName', '')}".strip()


def encontrar(jogadores: list[dict], consulta: str) -> list[dict]:
    """Nome completo exato > apelido exato > todas as palavras contidas no nome."""
    q = norm(consulta)
    palavras = q.split()
    exato = [p for p in jogadores if norm(nome_completo(p)) == q]
    if exato:
        return exato
    apelido = [p for p in jogadores if norm(p.get("familyName", "")) == q]
    if apelido:
        return apelido
    def bate(p):
        partes = norm(nome_completo(p)).replace("-", " ").replace(".", "").split()
        return all(any(t.startswith(w) for t in partes) for w in palavras)
    return [p for p in jogadores if bate(p)]


def relogio(game: dict) -> str:
    estado = game.get("gameStatus")
    if estado == 3:
        return "Final"
    periodo = game.get("period") or 0
    m = re.match(r"PT(\d+)M(\d+)", game.get("gameClock") or "")
    t = f"{int(m.group(1))}:{m.group(2)}" if m else ""
    p = f"{periodo}.º P" if periodo <= 4 else f"Prol. {periodo - 4}"
    return f"{p} {t}".strip()


# Cores de cada equipa em emoji (só para reconhecer a equipa à primeira vista).
CORES = {
    "ATL": "🔴⚪", "BOS": "🟢⚪", "BKN": "⚫⚪", "CHA": "🟣🔵", "CHI": "🔴⚫", "CLE": "🟤🟡",
    "DAL": "🔵⚪", "DEN": "🔵🟡", "DET": "🔴🔵", "GSW": "🔵🟡", "HOU": "🔴⚪", "IND": "🔵🟡",
    "LAC": "🔴🔵", "LAL": "🟣🟡", "MEM": "🔵⚪", "MIA": "🔴⚫", "MIL": "🟢⚪", "MIN": "🔵🟢",
    "NOP": "🔵🔴", "NYK": "🔵🟠", "OKC": "🔵🟠", "ORL": "🔵⚫", "PHI": "🔵🔴", "PHX": "🟣🟠",
    "POR": "🔴⚫", "SAC": "🟣⚪", "SAS": "⚫⚪", "TOR": "🔴⚫", "UTA": "🟣⚫", "WAS": "🔴🔵",
}


def cores(tri: str) -> str:
    return CORES.get(tri, "🏀")


def confronto(fora: str, casa: str) -> str:
    """'🟣🟡 LAL @ GSW 🔵🟡' (jogo ainda por começar)."""
    return f"{cores(fora)} {fora} @ {casa} {cores(casa)}"


def marcador(game: dict) -> str:
    a, h = game["awayTeam"], game["homeTeam"]
    return (f"{cores(a['teamTricode'])} {a['teamTricode']} {a.get('score', 0)}–{h.get('score', 0)} "
            f"{h['teamTricode']} {cores(h['teamTricode'])}\n{relogio(game)}")


@dataclass
class Tracker:
    ultimo: dict = field(default_factory=dict)   # (sub_id, game_id) -> valor
    flags: dict = field(default_factory=dict)    # (sub_id, game_id) -> {"perto", "batida", "final"}
    avisados: set = field(default_factory=set)   # (sub_id, game_id) já avisados de nome ambíguo
    terminados: list = field(default_factory=list)  # sub_ids cujo jogo acabou (o bot remove-os mais tarde)
    faltas: dict = field(default_factory=dict)      # (chat_id, jogador, game_id) -> nº de faltas já visto

    def semear(self, s: Sub, gid: str, st: dict):
        """Regista o valor atual de um seguimento novo a meio do jogo, para o próximo cesto já ser avisado."""
        chave = (s.id, gid)
        if chave in self.ultimo:
            return
        valor = valor_de(s.stat, st)
        self.ultimo[chave] = valor
        fl = self.flags.setdefault(chave, set())
        self._flags_dd(s, st, fl)
        if s.linha is not None:
            falta = alvo(s.linha) - valor
            if falta <= 0:
                fl.update({"perto", "batida"})
            elif falta <= s.margem():
                fl.add("perto")

    @staticmethod
    def _flags_dd(s: Sub, st: dict, fl: set):
        alvo_dd = STATS[s.stat].get("dd")
        if not alvo_dd:
            return
        falta = falta_dd(st, alvo_dd)
        if falta is None:
            fl.update({"perto", "batida"})
        elif falta <= STATS[s.stat]["perto"]:
            fl.add("perto")

    def _dd(self, s: Sub, st: dict, anterior: int, valor: int, fl: set, nome: str, game: dict) -> str | None:
        """Mensagem de duplo/triplo-duplo: quando uma categoria chega aos 10, quando está perto e quando completa."""
        info = STATS[s.stat]
        alvo_dd = info["dd"]
        falta = falta_dd(st, alvo_dd)
        linhas = []
        if valor > anterior and falta is None and "batida" not in fl:
            fl.update({"batida", "perto"})
            linhas.append(f"✅ <b>{info['nome'].upper()}</b> — <b>{nome}</b>")
        elif valor > anterior and "batida" not in fl:
            novas = [abrev for c, abrev in CATEGORIAS_DD if int(st.get(c, 0) or 0) >= 10]
            linhas.append(f"🎯 <b>{nome}</b> chegou aos 10 → {valor}/{alvo_dd} para o {info['nome']} ({', '.join(novas)})")
        if falta is not None and falta <= info["perto"] and "perto" not in fl:
            fl.add("perto")
            linhas.insert(0, f"⚠️ <b>PERTO DO {info['nome'].upper()}</b> — falta{'m' if falta > 1 else ''} {falta}")
            if len(linhas) == 1:
                linhas.append(f"<b>{nome}</b>")
        if not linhas:
            return None
        linhas += [resumo_dd(st), marcador(game)]
        return "\n".join(linhas)

    def processar(self, box: dict, subs: list[Sub]) -> list[tuple[int, str]]:
        game = box["game"]
        gid = game["gameId"]
        jogadores = game["homeTeam"].get("players", []) + game["awayTeam"].get("players", [])
        final = game.get("gameStatus") == 3
        msgs = []
        for s in subs:
            achados = encontrar(jogadores, s.jogador)
            if not achados:
                continue
            chave = (s.id, gid)
            if len(achados) > 1:
                if chave not in self.avisados:
                    self.avisados.add(chave)
                    nomes = ", ".join(nome_completo(p) for p in achados)
                    msgs.append((s.chat_id, f"❓ «{html.escape(s.jogador)}» corresponde a vários jogadores neste jogo ({html.escape(nomes)}). Remove e segue com o nome completo."))
                continue
            p = achados[0]
            st = p.get("statistics", {})
            valor = valor_de(s.stat, st)
            fl = self.flags.setdefault(chave, set())
            nome = html.escape(nome_completo(p))
            info = STATS[s.stat]

            if chave not in self.ultimo:
                # Primeira leitura (início do jogo ou bot reiniciado): regista sem alertas de estatística.
                self.ultimo[chave] = valor
                if final:
                    # Jogo já acabado na primeira leitura (bot reiniciado ou seguido depois do fim): sem mensagens.
                    fl.add("final")
                elif p.get("status") == "INACTIVE":
                    fl.add("inativo")
                    motivo = p.get("notPlayingDescription") or p.get("notPlayingReason") or ""
                    motivo = motivo.replace("INACTIVE_", "").replace("_", " ").strip().lower()
                    msgs.append((s.chat_id, f"🚫 <b>{nome}</b> não está a jogar este jogo{f' ({html.escape(motivo)})' if motivo else ''}.\n{marcador(game)}"))
                elif game.get("gameStatus") == 2 and (game.get("period") or 0) <= 1 and \
                        int(game["homeTeam"].get("score", 0) or 0) + int(game["awayTeam"].get("score", 0) or 0) < 12:
                    msgs.append((s.chat_id, f"▶️ Começou o jogo de <b>{nome}</b> ({info['nome']}{f', mais de {s.linha:g}' if s.linha is not None else ''})\n{marcador(game)}"))
                self._flags_dd(s, st, fl)
                if s.linha is not None:
                    falta = alvo(s.linha) - valor
                    if falta <= 0:
                        fl.update({"perto", "batida"})
                    elif falta <= s.margem():
                        fl.add("perto")
            elif info.get("dd"):
                anterior = self.ultimo[chave]
                self.ultimo[chave] = valor
                if valor < anterior:
                    perdeu = "batida" in fl
                    fl.discard("batida")
                    msgs.append((s.chat_id, f"↩️ <b>ANULADO</b> — correção nas stats de <b>{nome}</b>: "
                                            f"{valor}/{info['dd']} categorias para o {info['nome']}"
                                            f"{' (deixou de ter)' if perdeu else ''}\n{resumo_dd(st)}\n{marcador(game)}"))
                texto = self._dd(s, st, anterior, valor, fl, nome, game)
                if texto:
                    msgs.append((s.chat_id, texto))
            else:
                anterior = self.ultimo[chave]
                self.ultimo[chave] = valor
                if valor > anterior:
                    linhas = [f"🏀 <b>{nome}</b> +{valor - anterior} {info['nome']} → <b>{valor}</b>"]
                    if s.linha is not None:
                        falta = alvo(s.linha) - valor
                        if falta <= 0:
                            if "batida" not in fl:
                                fl.update({"batida", "perto"})
                                linhas.insert(0, f"✅ <b>LINHA BATIDA</b> — mais de {s.linha:g} {info['nome']}")
                            else:
                                linhas.append(f"Linha {s.linha:g} já batida")
                        else:
                            if falta <= s.margem() and "perto" not in fl:
                                fl.add("perto")
                                linhas.insert(0, f"⚠️ <b>PERTO DA LINHA</b> — falta{'m' if falta > 1 else ''} {falta}")
                            linhas.append(f"Linha {s.linha:g} · falta{'m' if falta > 1 else ''} {falta}")
                    linhas.append(marcador(game))
                    msgs.append((s.chat_id, "\n".join(linhas)))
                elif valor < anterior:
                    # Correção de estatística (cesto ou ressalto retirado, por exemplo).
                    linhas = [f"↩️ <b>ANULADO</b> — <b>{nome}</b> {anterior - valor} {info['nome']} retirado{'s' if anterior - valor > 1 else ''}",
                              f"Agora tem <b>{valor}</b> {info['nome']}"]
                    if s.linha is not None:
                        falta = alvo(s.linha) - valor
                        if falta > 0:
                            if "batida" in fl:
                                linhas.append(f"⚠️ A linha {s.linha:g} deixou de estar batida · falta{'m' if falta > 1 else ''} {falta}")
                            else:
                                linhas.append(f"Linha {s.linha:g} · falta{'m' if falta > 1 else ''} {falta}")
                            fl.discard("batida")
                            if falta > s.margem():
                                fl.discard("perto")
                    linhas.append(marcador(game))
                    msgs.append((s.chat_id, "\n".join(linhas)))

            chave_f = (s.chat_id, nome, gid)
            faltas = int(st.get("fouls", 0) or 0)
            vistas = self.faltas.get(chave_f)
            self.faltas[chave_f] = max(faltas, vistas or 0)
            if vistas is not None and faltas > vistas and not final and "inativo" not in fl:
                if faltas >= 6:
                    msgs.append((s.chat_id, f"🟥 <b>{nome}</b> fez a 6.ª falta e <b>está fora do jogo</b> (foul out)\n{marcador(game)}"))
                elif faltas >= 4:
                    msgs.append((s.chat_id, f"🟨 <b>{nome}</b> tem <b>{faltas} faltas</b> — à 6.ª sai do jogo"
                                            f"{' (falta só 1!)' if faltas == 5 else ''}\n{marcador(game)}"))

            if final and "final" not in fl:
                fl.add("final")
                self.terminados.append(s.id)
                if "inativo" in fl:
                    continue
                if info.get("dd"):
                    ok = valor >= info["dd"]
                    msgs.append((s.chat_id, f"🏁 <b>{nome}</b> acabou com {resumo_dd(st)} — {info['nome']} {'✅' if ok else '❌'}\n{marcador(game)}"))
                elif s.linha is not None:
                    ok = valor >= alvo(s.linha)
                    msgs.append((s.chat_id, f"🏁 <b>{nome}</b> acabou com <b>{valor}</b> {info['nome']} — mais de {s.linha:g} {'✅' if ok else '❌'}\n{marcador(game)}"))
                else:
                    msgs.append((s.chat_id, f"🏁 <b>{nome}</b> acabou com <b>{valor}</b> {info['nome']}\n{marcador(game)}"))
        return msgs


# ---------- plantéis e calendário (para saber quando joga cada jogador) ----------
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LISBOA = ZoneInfo("Europe/Lisbon")
DIAS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
# A ESPN usa algumas abreviaturas diferentes das da NBA.
ESPN_PARA_NBA = {"GS": "GSW", "NY": "NYK", "SA": "SAS", "NO": "NOP", "UTAH": "UTA", "WSH": "WAS"}


def jogadores_espn(abreviatura: str, roster: dict) -> list[dict]:
    """Converte o plantel da ESPN para o mesmo formato de jogador usado no boxscore."""
    equipa = ESPN_PARA_NBA.get(abreviatura, abreviatura)
    atletas = roster.get("athletes", [])
    if atletas and "items" in atletas[0]:  # formato agrupado por posição
        atletas = [a for grupo in atletas for a in grupo["items"]]
    return [{"name": a.get("fullName") or a.get("displayName", ""), "firstName": a.get("firstName", ""),
             "familyName": a.get("lastName", ""), "team": equipa} for a in atletas]


def _data(t: str) -> datetime:
    return datetime.fromisoformat(t.replace("Z", "+00:00"))


def jogos_calendario(schedule: dict) -> list[dict]:
    """scheduleLeagueV2.json -> lista simples de jogos com início em UTC."""
    jogos = []
    for dia in schedule["leagueSchedule"]["gameDates"]:
        for g in dia["games"]:
            t = g.get("gameDateTimeUTC") or g.get("gameTimeUTC")
            if not t or not g["homeTeam"].get("teamTricode"):
                continue
            jogos.append({"gameId": g["gameId"], "inicio": _data(t), "estado": g.get("gameStatus", 1),
                          "casa": g["homeTeam"]["teamTricode"], "fora": g["awayTeam"]["teamTricode"]})
    return sorted(jogos, key=lambda j: j["inicio"])


def proximo_jogo(jogos: list[dict], equipa: str, agora: datetime, estados: dict | None = None):
    """Jogo a decorrer ou o próximo da equipa. 'estados' (gameId -> estado) vem do scoreboard, mais atual."""
    estados = estados or {}
    for j in jogos:
        if equipa not in (j["casa"], j["fora"]):
            continue
        estado = estados.get(j["gameId"], j["estado"])
        if estado == 3 or j["inicio"] < agora - timedelta(hours=5):
            continue
        return {**j, "estado": estado}
    return None


def quando(inicio: datetime, agora: datetime) -> str:
    """'daqui a 2h15 (hoje às 01:30)' em hora de Lisboa."""
    falta = inicio - agora
    local, hoje = inicio.astimezone(LISBOA), agora.astimezone(LISBOA)
    hora = local.strftime("%H:%M")
    if local.date() == hoje.date():
        dia = f"hoje às {hora}"
    elif local.date() == hoje.date() + timedelta(days=1):
        dia = f"amanhã às {hora}"
    else:
        dia = f"{DIAS[local.weekday()]} {local.day}/{local.month} às {hora}"
    if falta <= timedelta(0):
        return f"está a começar ({dia})"
    minutos = int(falta.total_seconds() // 60)
    d, resto = divmod(minutos, 1440)
    h, m = divmod(resto, 60)
    if d:
        texto = f"{d} dia{'s' if d > 1 else ''} e {h}h"
    elif h:
        texto = f"{h}h{m:02d}"
    else:
        texto = f"{m} min"
    return f"daqui a {texto} ({dia})"


# ---------- dados ao vivo da ESPN (convertidos para o formato da NBA usado no resto do código) ----------
ESTADOS_ESPN = {"pre": 1, "in": 2, "post": 3}
LABELS_ESPN = {"PTS": "points", "REB": "reboundsTotal", "AST": "assists", "STL": "steals", "BLK": "blocks"}


def _int(v) -> int:
    try:
        return int(str(v).split("-")[0])
    except (ValueError, TypeError):
        return 0


def _relogio_espn(texto: str) -> str:
    """'4:10' -> 'PT04M10.00S' (formato da NBA); '45.3' (último minuto) -> 'PT00M45.00S'."""
    texto = (texto or "").strip()
    if ":" in texto:
        m, s = texto.split(":", 1)
    else:
        m, s = "0", texto or "0"
    return f"PT{_int(m):02d}M{_int(s.split('.')[0]):02d}.00S"


def _equipas_espn(comp: dict) -> dict:
    eq = {}
    for c in comp.get("competitors", []):
        abrev = c.get("team", {}).get("abbreviation", "?")
        eq[c.get("homeAway")] = {"teamTricode": ESPN_PARA_NBA.get(abrev, abrev), "score": _int(c.get("score", 0))}
    return eq


def jogo_espn(ev: dict) -> dict:
    """Evento do scoreboard da ESPN -> jogo no formato do scoreboard da NBA."""
    comp = ev["competitions"][0]
    st = ev.get("status") or comp.get("status", {})
    eq = _equipas_espn(comp)
    return {"gameId": str(ev["id"]), "gameStatus": ESTADOS_ESPN.get(st.get("type", {}).get("state"), 1),
            "gameStatusText": st.get("type", {}).get("shortDetail", ""), "gameTimeUTC": ev.get("date"),
            "period": st.get("period", 0), "gameClock": _relogio_espn(st.get("displayClock")),
            "homeTeam": eq.get("home", {"teamTricode": "?", "score": 0}),
            "awayTeam": eq.get("away", {"teamTricode": "?", "score": 0})}


def box_espn(summary: dict, gid: str) -> dict:
    """'summary' da ESPN -> boxscore no formato da NBA ({'game': {..., 'homeTeam': {'players': [...]}}})."""
    comp = summary["header"]["competitions"][0]
    st = comp.get("status", {})
    eq = _equipas_espn(comp)
    for lado in ("home", "away"):
        eq.setdefault(lado, {"teamTricode": "?", "score": 0})["players"] = []
    lado_de = {v["teamTricode"]: k for k, v in eq.items()}
    for bloco in summary.get("boxscore", {}).get("players", []):
        abrev = bloco.get("team", {}).get("abbreviation", "?")
        lado = lado_de.get(ESPN_PARA_NBA.get(abrev, abrev))
        if not lado:
            continue
        for tabela in bloco.get("statistics", []):
            labels = tabela.get("labels", [])
            for a in tabela.get("athletes", []):
                nome = a.get("athlete", {}).get("displayName", "")
                partes = nome.split(" ", 1)
                stats = {campo: 0 for campo in list(LABELS_ESPN.values()) + ["threePointersMade", "minutes"]}
                for label, valor in zip(labels, a.get("stats", [])):
                    if label in LABELS_ESPN:
                        stats[LABELS_ESPN[label]] = _int(valor)
                    elif label == "3PT":
                        stats["threePointersMade"] = _int(valor)  # "3-7" -> 3
                    elif label == "MIN":
                        stats["minutes"] = _int(valor)
                    elif label == "PF":
                        stats["fouls"] = _int(valor)
                eq[lado]["players"].append({
                    "name": nome, "firstName": partes[0], "familyName": partes[1] if len(partes) > 1 else "",
                    "status": "INACTIVE" if a.get("didNotPlay") else "ACTIVE",
                    "notPlayingReason": a.get("reason", ""), "statistics": stats})
    return {"game": {"gameId": gid, "gameStatus": ESTADOS_ESPN.get(st.get("type", {}).get("state"), 1),
                     "period": st.get("period", 0), "gameClock": _relogio_espn(st.get("displayClock")),
                     "homeTeam": eq["home"], "awayTeam": eq["away"]}}


def calendario_espn(jogos: list[dict]) -> list[dict]:
    """Jogos (já convertidos) de vários dias -> lista para proximo_jogo()."""
    vistos, lista = set(), []
    for g in jogos:
        if g["gameId"] in vistos or not g.get("gameTimeUTC"):
            continue
        vistos.add(g["gameId"])
        lista.append({"gameId": g["gameId"], "inicio": _data(g["gameTimeUTC"]), "estado": g["gameStatus"],
                      "casa": g["homeTeam"]["teamTricode"], "fora": g["awayTeam"]["teamTricode"]})
    return sorted(lista, key=lambda j: j["inicio"])


# ---------- /stats e /add ----------
def linha_stats(p: dict) -> str:
    """'14 pts · 6 res · 5 ast · 2 tri · 20 min' (roubos/desarmes só se tiver)."""
    st = p.get("statistics", {})
    v = lambda c: int(st.get(c, 0) or 0)
    partes = [f"{v('points')} pts", f"{v('reboundsTotal')} res", f"{v('assists')} ast", f"{v('threePointersMade')} tri"]
    if v("steals"):
        partes.append(f"{v('steals')} rou")
    if v("blocks"):
        partes.append(f"{v('blocks')} des")
    if "minutes" in st:
        partes.append(f"{v('minutes')} min")
    if v("fouls") >= 4:
        partes.append(f"{'🟥' if v('fouls') >= 6 else '🟨'} {v('fouls')} faltas")
    return " · ".join(partes)


def parse_add(args: list[str]):
    """'/add 3 7 [estatística] [linha]' -> (números, stat, linha) ou erro.
    Antes da estatística, os números são jogadores; depois dela, um número é a linha."""
    exemplo = "Exemplo: /add 3 7  ·  /add 3 ressaltos  ·  /add 3 pontos 24.5"
    tokens = " ".join(args).split()
    # juntar "triplo duplo" etc.
    for i in range(len(tokens) - 1):
        par = (norm(tokens[i]), norm(tokens[i + 1]))
        if par in JUNTAR:
            tokens = tokens[:i] + [JUNTAR[par]] + tokens[i + 2:]
            break
    idx = next((i for i, t in enumerate(tokens) if norm(t) in ALIASES), None)
    antes = tokens if idx is None else tokens[:idx]
    depois = [] if idx is None else tokens[idx + 1:]
    stat = None if idx is None else ALIASES[norm(tokens[idx])]
    try:
        # antes da estatística, a vírgula separa jogadores ("1,4"); depois, é a vírgula decimal da linha ("24,5")
        numeros = sorted({int(x) for t in antes for x in t.split(",") if x})
    except ValueError:
        return None, exemplo
    if not numeros or len(depois) > 1:
        return None, exemplo
    linha = None
    if depois:
        linha = _numero(depois[0])
        if linha is None or linha < 0:
            return None, exemplo
    if stat and STATS[stat].get("dd"):
        linha = None
    return (numeros, stat, linha), None
