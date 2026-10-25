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
assert parse_seguir(["lebron", "pontos", "24,5"])[0] == ("lebron", "pts", 24.5, None)
assert parse_seguir(["nikola", "jokic", "ressaltos", "11.5", "2"])[0] == ("nikola jokic", "reb", 11.5, 2)
assert parse_seguir(["curry", "triplos"])[0] == ("curry", "3pt", None, None)
assert parse_seguir(["lebron"])[1] is not None

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

assert len(todas[0]) == 1 and "vários jogadores" in todas[0][0][1]     # só aviso de ambiguidade
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
print("Todos os testes passaram ✅")
