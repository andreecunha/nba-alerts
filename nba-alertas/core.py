"""Lógica central: interpretar comandos, encontrar jogadores no boxscore e decidir que alertas enviar.

Não depende do Telegram nem da rede, para poder ser testada com jogos simulados.
"""
import html
import math
import re
import unicodedata
from dataclasses import dataclass, field

# chave -> nome em pt, campos do boxscore da NBA que se somam, margem "perto" por defeito
STATS = {
    "pts": {"nome": "pontos", "campos": ["points"], "perto": 3},
    "reb": {"nome": "ressaltos", "campos": ["reboundsTotal"], "perto": 1},
    "ast": {"nome": "assistências", "campos": ["assists"], "perto": 1},
    "3pt": {"nome": "triplos", "campos": ["threePointersMade"], "perto": 1},
    "stl": {"nome": "roubos", "campos": ["steals"], "perto": 1},
    "blk": {"nome": "desarmes", "campos": ["blocks"], "perto": 1},
    "pra": {"nome": "pts+res+ast", "campos": ["points", "reboundsTotal", "assists"], "perto": 3},
    "pr": {"nome": "pts+res", "campos": ["points", "reboundsTotal"], "perto": 2},
    "pa": {"nome": "pts+ast", "campos": ["points", "assists"], "perto": 2},
    "ra": {"nome": "res+ast", "campos": ["reboundsTotal", "assists"], "perto": 2},
}

ALIASES = {
    "pontos": "pts", "ponto": "pts", "pts": "pts",
    "ressaltos": "reb", "ressalto": "reb", "res": "reb", "reb": "reb",
    "assistencias": "ast", "assistencia": "ast", "assists": "ast", "ast": "ast",
    "triplos": "3pt", "triplo": "3pt", "3pt": "3pt", "3s": "3pt",
    "roubos": "stl", "roubo": "stl", "stl": "stl",
    "desarmes": "blk", "desarme": "blk", "blocos": "blk", "blk": "blk",
    "pra": "pra", "pr": "pr", "pa": "pa", "ra": "ra",
}


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


def parse_seguir(args: list[str]):
    """'/seguir lebron james pontos 24.5 [margem]' -> (jogador, stat, linha, margem) ou erro."""
    idx = next((i for i, a in enumerate(args) if i > 0 and norm(a) in ALIASES), None)
    if idx is None:
        return None, "Diz o jogador e a estatística, por exemplo: /seguir lebron pontos 24.5"
    jogador = " ".join(args[:idx]).strip()
    stat = ALIASES[norm(args[idx])]
    numeros = args[idx + 1:]
    linha = margem = None
    try:
        if len(numeros) >= 1:
            linha = float(numeros[0].replace(",", "."))
        if len(numeros) >= 2:
            margem = int(numeros[1])
        if len(numeros) > 2:
            raise ValueError
    except ValueError:
        return None, "Não percebi os números. Exemplo: /seguir lebron pontos 24.5 (a linha é opcional)"
    if linha is not None and linha < 0:
        return None, "A linha tem de ser positiva."
    return (jogador, stat, linha, margem), None


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


def marcador(game: dict) -> str:
    a, h = game["awayTeam"], game["homeTeam"]
    return f"{a['teamTricode']} {a.get('score', 0)}–{h.get('score', 0)} {h['teamTricode']} · {relogio(game)}"


@dataclass
class Tracker:
    ultimo: dict = field(default_factory=dict)   # (sub_id, game_id) -> valor
    flags: dict = field(default_factory=dict)    # (sub_id, game_id) -> {"perto", "batida", "final"}
    avisados: set = field(default_factory=set)   # (sub_id, game_id) já avisados de nome ambíguo

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
            valor = sum(int(st.get(c, 0) or 0) for c in STATS[s.stat]["campos"])
            fl = self.flags.setdefault(chave, set())
            nome = html.escape(nome_completo(p))
            info = STATS[s.stat]

            if chave not in self.ultimo:
                # Primeira leitura (início do jogo ou bot reiniciado): só regista, sem alertar.
                self.ultimo[chave] = valor
                if s.linha is not None:
                    falta = alvo(s.linha) - valor
                    if falta <= 0:
                        fl.update({"perto", "batida"})
                    elif falta <= s.margem():
                        fl.add("perto")
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
                elif valor < anterior and s.linha is not None and valor < alvo(s.linha):
                    # Correção de estatística pela NBA: volta a permitir os alertas.
                    fl.discard("batida")
                    if alvo(s.linha) - valor > s.margem():
                        fl.discard("perto")

            if final and "final" not in fl:
                fl.add("final")
                if s.linha is not None:
                    ok = valor >= alvo(s.linha)
                    msgs.append((s.chat_id, f"🏁 <b>{nome}</b> acabou com <b>{valor}</b> {info['nome']} — mais de {s.linha:g} {'✅' if ok else '❌'}\n{marcador(game)}"))
                else:
                    msgs.append((s.chat_id, f"🏁 <b>{nome}</b> acabou com <b>{valor}</b> {info['nome']}\n{marcador(game)}"))
        return msgs
