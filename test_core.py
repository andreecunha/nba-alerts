"""Simula um jogo LAL @ GSW e verifica os alertas. Correr: python test_core.py"""
from core import Sub, Tracker, parse_seguir, encontrar


def jogador(pid, first, last, pts=0, reb=0, ast=0, tres=0):
    return {"personId": pid, "name": f"{first} {last}", "firstName": first, "familyName": last,
            "statistics": {"points": pts, "reboundsTotal": reb, "assists": ast, "threePointersMade": tres}}


def box(estado, periodo, clock, lebron, curry, sl=0, sg=0):
    return {"game": {"gameId": "0022600050", "gameStatus": estado, "period": periodo, "gameClock": clock,
                     "awayTeam": {"teamTricode": "LAL", "score": sl, "players": [lebron, jogador(2, "Bronny", "James")]},
                     "homeTeam": {"teamTricode": "GSW", "score": sg, "players": [curry]}}}


# --- interpretar comandos ---
assert parse_seguir(["lebron", "pontos", "24,5"])[0] == ("lebron", "pts", 24.5)
assert parse_seguir(["lebron", "james", "24.5"])[0] == ("lebron james", "pts", 24.5)
assert parse_seguir(["nikola", "jokic", "ressaltos", "11.5"])[0] == ("nikola jokic", "reb", 11.5)
assert parse_seguir(["curry", "triplos"])[0] == ("curry", "3pt", None)
assert parse_seguir(["lebron"])[0] == ("lebron", "pts", None)
assert parse_seguir(["24.5"])[1] is not None

# --- encontrar jogadores ---
ps = [jogador(1, "LeBron", "James"), jogador(2, "Bronny", "James"), jogador(3, "Nikola", "Jokić")]
assert [p["personId"] for p in encontrar(ps, "lebron")] == [1]
assert len(encontrar(ps, "james")) == 2          # ambíguo
assert [p["personId"] for p in encontrar(ps, "jokic")] == [3]  # sem acento

# --- jogo simulado ---
subs = [
    Sub(1, 100, "lebron", "pts", 24.5),          # margem 3 -> aviso aos 22
    Sub(2, 100, "curry", "3pt", 4.5),            # margem 1 -> aviso aos 4
    Sub(3, 200, "james", "pts"),                 # ambíguo
]
t = Tracker()
seq = [
    box(2, 1, "PT12M00.00S", jogador(1, "LeBron", "James"), jogador(9, "Stephen", "Curry"), 0, 0),
    box(2, 1, "PT10M41.00S", jogador(1, "LeBron", "James", pts=2), jogador(9, "Stephen", "Curry", pts=3, tres=1), 2, 3),
    box(2, 3, "PT05M12.00S", jogador(1, "LeBron", "James", pts=22), jogador(9, "Stephen", "Curry", pts=15, tres=4), 80, 75),
    box(2, 4, "PT08M02.00S", jogador(1, "LeBron", "James", pts=25), jogador(9, "Stephen", "Curry", pts=15, tres=4), 95, 90),
    box(2, 4, "PT06M30.00S", jogador(1, "LeBron", "James", pts=27), jogador(9, "Stephen", "Curry", pts=15, tres=4), 99, 92),
    box(3, 4, "PT00M00.00S", jogador(1, "LeBron", "James", pts=27), jogador(9, "Stephen", "Curry", pts=15, tres=4), 110, 104),
]
todas = []
for i, b in enumerate(seq):
    m = t.processar(b, subs)
    print(f"--- leitura {i} ---")
    for chat, txt in m:
        print(f"[{chat}] {txt}\n")
    todas.append(m)

assert any("vários jogadores" in x for _, x in todas[0])
assert sum("Começou" in x for _, x in todas[0]) == 2
assert any("+2 pontos" in x for _, x in todas[1])
assert any("PERTO DA LINHA" in x and "LeBron" in x for _, x in todas[2])
assert any("PERTO DA LINHA" in x and "Curry" in x for _, x in todas[2])
assert any("LINHA BATIDA" in x for _, x in todas[3])
assert not any("LINHA BATIDA" in x for _, x in todas[4])               # só uma vez
assert any("27 pontos — mais de 24.5 ✅" in x.replace("</b>", "").replace("<b>", "") for _, x in todas[5])
assert any("❌" in x for _, x in todas[5])                              # Curry falhou a linha de triplos

# bot reiniciado a meio do jogo: não reenvia nada
t2 = Tracker()
assert [m for m in t2.processar(seq[4], subs[:2])] == []

# seguir a meio do jogo: semear com o valor atual -> o próximo cesto já é avisado
t3 = Tracker()
s4 = Sub(4, 300, "LeBron James", "pts", 30.5)
t3.semear(s4, "0022600050", {"points": 25})
m = t3.processar(seq[4], [s4])
assert len(m) == 1 and "+2 pontos" in m[0][1] and "faltam 4" in m[0][1], m

# jogador inativo
inativo = jogador(1, "LeBron", "James"); inativo["status"] = "INACTIVE"; inativo["notPlayingReason"] = "INACTIVE_INJURY"
m = Tracker().processar(box(2, 2, "PT06M00.00S", inativo, jogador(9, "Stephen", "Curry"), 50, 50), [subs[0]])
assert len(m) == 1 and "não está a jogar" in m[0][1] and "injury" in m[0][1], m

# calendário e contagem
from datetime import datetime, timezone
from core import jogos_calendario, proximo_jogo, quando, jogadores_espn
cal = jogos_calendario({"leagueSchedule": {"gameDates": [{"games": [
    {"gameId": "A", "gameDateTimeUTC": "2026-10-24T23:30:00Z", "gameStatus": 3, "homeTeam": {"teamTricode": "BOS"}, "awayTeam": {"teamTricode": "LAL"}},
    {"gameId": "B", "gameDateTimeUTC": "2026-10-25T02:30:00Z", "gameStatus": 1, "homeTeam": {"teamTricode": "GSW"}, "awayTeam": {"teamTricode": "LAL"}},
    {"gameId": "C", "gameDateTimeUTC": "2026-10-27T00:00:00Z", "gameStatus": 1, "homeTeam": {"teamTricode": "LAL"}, "awayTeam": {"teamTricode": "PHX"}},
]}]}})
agora = datetime(2026, 10, 25, 0, 50, tzinfo=timezone.utc)   # 01:50 em Lisboa (ainda hora de verão)
j = proximo_jogo(cal, "LAL", agora)
assert j["gameId"] == "B", j
print("LAL @ GSW", quando(j["inicio"], agora))
assert quando(j["inicio"], agora).startswith("daqui a 1h40")
assert proximo_jogo(cal, "LAL", agora, {"B": 3})["gameId"] == "C"
print("PHX @ LAL", quando(cal[2]["inicio"], agora))
esp = jogadores_espn("GS", {"athletes": [{"fullName": "Stephen Curry", "firstName": "Stephen", "lastName": "Curry"}]})
assert esp[0]["team"] == "GSW" and encontrar(esp, "curry")

# duplo/triplo-duplo
assert parse_seguir(["jokic", "triplo", "duplo"])[0] == ("jokic", "td", None)
assert parse_seguir(["jokic", "dd", "10.5"])[0] == ("jokic", "dd", None)
assert parse_seguir(["lebron", "triple-double"])[0] == ("lebron", "td", None)
def jk(pts, reb, ast):
    return jogador(3, "Nikola", "Jokić", pts=pts, reb=reb, ast=ast)
def bx(p, estado=2):
    return box(estado, 2, "PT05M00.00S", p, jogador(9, "Stephen", "Curry"), 50, 50)
std = Sub(10, 400, "jokic", "td"); sdd = Sub(11, 400, "jokic", "dd")
t4 = Tracker()
passos = [jk(4, 3, 2), jk(10, 6, 5), jk(12, 9, 7), jk(14, 10, 8), jk(18, 12, 9), jk(20, 12, 10), jk(22, 13, 11)]
print("--- triplo-duplo ---")
saida = []
for pp in passos:
    m = t4.processar(bx(pp), [std, sdd]); saida.append(m)
    for _, x in m: print(x, "\n")
m = t4.processar(bx(jk(22, 13, 11), 3), [std, sdd])
for _, x in m: print(x, "\n")
txt = lambda ms: " | ".join(x for _, x in ms)
assert "chegou aos 10" in txt(saida[1])
assert "DUPLO-DUPLO" in txt(saida[3]) and "PERTO DO TRIPLO-DUPLO" in txt(saida[3])
assert "TRIPLO-DUPLO</b> — " in txt(saida[5])
assert txt(saida[6]) == ""
assert "3/2" not in txt(saida[5])
assert "triplo-duplo ✅" in txt(m) and "duplo-duplo ✅" in txt(m)
# remoção automática: o fim do jogo fica registado; reiniciar depois do fim não reenvia o resumo
assert set(t.terminados) == {1, 2}, t.terminados
assert Tracker().processar(seq[5], subs[:2]) == []
print("Testes da lógica OK")

# --- conversão dos dados da ESPN ---
from core import jogo_espn, box_espn, marcador
ev = {"id": "401800123", "date": "2026-10-25T02:30Z",
      "status": {"period": 2, "displayClock": "4:10", "type": {"state": "in", "shortDetail": "4:10 - 2nd"}},
      "competitions": [{"competitors": [
          {"homeAway": "home", "score": "48", "team": {"abbreviation": "GS"}},
          {"homeAway": "away", "score": "51", "team": {"abbreviation": "LAL"}}]}]}
g = jogo_espn(ev)
assert g["gameStatus"] == 2 and g["homeTeam"]["teamTricode"] == "GSW" and g["awayTeam"]["score"] == 51, g
labels = ["MIN", "PTS", "FG", "3PT", "FT", "REB", "AST", "TO", "STL", "BLK", "OREB", "DREB", "PF", "+/-"]
summary = {"header": {"competitions": [{"status": ev["status"], "competitors": ev["competitions"][0]["competitors"]}]},
           "boxscore": {"players": [
               {"team": {"abbreviation": "LAL"}, "statistics": [{"labels": labels, "athletes": [
                   {"athlete": {"displayName": "LeBron James"}, "stats": ["20", "14", "6-11", "2-4", "0-0", "6", "5", "1", "1", "0", "1", "5", "2", "+3"]},
                   {"athlete": {"displayName": "Bronny James"}, "didNotPlay": True, "reason": "COACH'S DECISION", "stats": []}]}]},
               {"team": {"abbreviation": "GS"}, "statistics": [{"labels": labels, "athletes": [
                   {"athlete": {"displayName": "Stephen Curry"}, "stats": ["19", "17", "6-12", "4-8", "1-1", "2", "4", "2", "1", "0", "0", "2", "1", "-3"]}]}]}]}}
b = box_espn(summary, "401800123")
lb = encontrar(b["game"]["awayTeam"]["players"], "lebron")[0]
assert lb["statistics"]["points"] == 14 and lb["statistics"]["threePointersMade"] == 2 and lb["statistics"]["reboundsTotal"] == 6
cu = encontrar(b["game"]["homeTeam"]["players"], "curry")[0]
assert cu["statistics"]["threePointersMade"] == 4
assert encontrar(b["game"]["awayTeam"]["players"], "bronny")[0]["status"] == "INACTIVE"
print("ESPN:", marcador(b["game"]))
print("Conversão ESPN OK ✅")
print("Todos os testes passaram ✅")
