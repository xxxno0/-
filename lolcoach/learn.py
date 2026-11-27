"""챌린저/그마/프로 경기를 학습해서 챔피언·포지션별 '정점 플레이어 합의' 통계를 만든다.

흐름
1) 챌린저·그랜드마스터 랭킹 + 직접 지정한 프로 계정을 플레이어 목록으로 저장
2) 그들의 솔로랭크 경기(match-v5) + 분 단위 타임라인을 받아서, 그 경기에 있던 상위권 플레이어 각각의
   라인전 지표(CS, 골드차, 첫 귀환, 스킬 순서, 빌드, 킬 직후 판단, 와드…)를 뽑아 저장
   → 한 경기에서 최대 10명 분이 나오므로 모든 챔피언이 동시에 쌓임
3) 챔피언을 물어보면 그 챔피언 '장인'(판수·승률·LP·프로 여부로 점수) 상위 N명을 고르고,
   각자의 통계를 낸 뒤 그들 사이의 합의(중앙값, 최빈값과 일치율)를 정답으로 낸다.
   한 사람의 버릇이 아니라 여러 정점 플레이어가 공통으로 하는 것만 남기기 위함.
"""
from __future__ import annotations

import json
import math
import sqlite3
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable

from .config import DATA_DIR
from .riot import DataDragon, Riot

SKILL = {1: "Q", 2: "W", 3: "E", 4: "R"}
POSITIONS = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
POS_KO = {"TOP": "탑", "JUNGLE": "정글", "MIDDLE": "미드", "BOTTOM": "원딜", "UTILITY": "서폿"}


class Brain:
    """학습 데이터 저장소 (sqlite)"""

    def __init__(self, path: Path | str | None = None):
        path = Path(path) if path else DATA_DIR / "brain.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS players (
                puuid TEXT PRIMARY KEY, riot_id TEXT, tier TEXT, lp INTEGER,
                wins INTEGER, losses INTEGER, is_pro INTEGER DEFAULT 0, updated REAL);
            CREATE TABLE IF NOT EXISTS matches (match_id TEXT PRIMARY KEY, patch TEXT, fetched REAL);
            CREATE TABLE IF NOT EXISTS samples (
                match_id TEXT, puuid TEXT, champion TEXT, position TEXT, win INTEGER,
                opp_champion TEXT, features TEXT, PRIMARY KEY (match_id, puuid));
            CREATE INDEX IF NOT EXISTS samples_champ ON samples (champion, position);
        """)

    # --- 플레이어 ---
    def upsert_player(self, puuid: str, tier: str, lp: int, wins: int, losses: int,
                      riot_id: str | None = None, is_pro: bool = False) -> None:
        self.db.execute("""
            INSERT INTO players (puuid, riot_id, tier, lp, wins, losses, is_pro, updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(puuid) DO UPDATE SET tier=excluded.tier, lp=excluded.lp, wins=excluded.wins,
                losses=excluded.losses, riot_id=COALESCE(excluded.riot_id, players.riot_id),
                is_pro=MAX(players.is_pro, excluded.is_pro), updated=excluded.updated
        """, (puuid, riot_id, tier, lp, wins, losses, int(is_pro), time.time()))
        self.db.commit()

    def players(self) -> dict[str, dict]:
        rows = self.db.execute("SELECT puuid, riot_id, tier, lp, wins, losses, is_pro FROM players")
        return {r[0]: {"riot_id": r[1], "tier": r[2], "lp": r[3] or 0, "wins": r[4] or 0,
                       "losses": r[5] or 0, "is_pro": bool(r[6])} for r in rows}

    def has_match(self, match_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM matches WHERE match_id=?", (match_id,)).fetchone() is not None

    def add_match(self, match_id: str, patch: str, samples: list[dict]) -> None:
        self.db.execute("INSERT OR IGNORE INTO matches VALUES (?, ?, ?)", (match_id, patch, time.time()))
        for s in samples:
            self.db.execute("INSERT OR REPLACE INTO samples VALUES (?, ?, ?, ?, ?, ?, ?)", (
                match_id, s["puuid"], s["champion"], s["position"], int(s["win"]),
                s.get("opp_champion"), json.dumps(s["features"])))
        self.db.commit()

    def samples(self, champion: str, position: str | None = None) -> list[dict]:
        q = "SELECT match_id, puuid, champion, position, win, opp_champion, features FROM samples WHERE champion=?"
        args: list = [champion]
        if position:
            q += " AND position=?"
            args.append(position)
        return [{"match_id": r[0], "puuid": r[1], "champion": r[2], "position": r[3], "win": bool(r[4]),
                 "opp_champion": r[5], "features": json.loads(r[6])} for r in self.db.execute(q, args)]

    def mains(self, champion: str, limit: int = 30) -> list[str]:
        rows = self.db.execute("""SELECT puuid, COUNT(*) c FROM samples WHERE champion=?
                                  GROUP BY puuid ORDER BY c DESC LIMIT ?""", (champion, limit))
        return [r[0] for r in rows]

    def stats(self) -> dict:
        n_p = self.db.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        n_m = self.db.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        n_s = self.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        champs = self.db.execute("""SELECT champion, COUNT(*) c FROM samples GROUP BY champion
                                    ORDER BY c DESC LIMIT 200""").fetchall()
        return {"players": n_p, "matches": n_m, "samples": n_s, "champions": dict(champs)}


# ---------------------------------------------------------------- 특징 추출

def _frame_at(frames: list[dict], minute: int) -> dict | None:
    return frames[minute] if len(frames) > minute else None


def extract(match: dict, timeline: dict, keep_puuids: set[str] | None = None) -> list[dict]:
    """경기 1개 → 참가자별 라인전 지표. keep_puuids가 있으면 그 사람들만."""
    info = match["info"]
    frames = timeline["info"]["frames"]
    parts = info["participants"]
    by_pid = {p["participantId"]: p for p in parts}
    events = [e for f in frames for e in f.get("events", [])]

    def opp_of(p: dict) -> dict | None:
        for q in parts:
            if q["teamId"] != p["teamId"] and q.get("teamPosition") == p.get("teamPosition") and p.get("teamPosition"):
                return q
        return None

    out = []
    for p in parts:
        if keep_puuids is not None and p["puuid"] not in keep_puuids:
            continue
        pid = p["participantId"]
        opp = opp_of(p)
        opp_pid = opp["participantId"] if opp else None
        f: dict = {}

        def pf(minute: int, who: int = pid) -> dict | None:
            fr = _frame_at(frames, minute)
            return fr["participantFrames"].get(str(who)) if fr else None

        for m in (5, 10, 15):
            x = pf(m)
            if x:
                f[f"cs{m}"] = x["minionsKilled"] + x["jungleMinionsKilled"]
                f[f"gold{m}"] = x["totalGold"]
                f[f"lvl{m}"] = x["level"]
                if opp_pid and pf(m, opp_pid):
                    o = pf(m, opp_pid)
                    f[f"golddiff{m}"] = x["totalGold"] - o["totalGold"]
                    f[f"csdiff{m}"] = f[f"cs{m}"] - (o["minionsKilled"] + o["jungleMinionsKilled"])

        # 구매 기록 (되돌리기 반영)
        purchases: list[tuple[int, int]] = []
        skills: list[str] = []
        for e in events:
            if e.get("participantId") != pid:
                continue
            t = e.get("type")
            if t == "ITEM_PURCHASED":
                purchases.append((e["timestamp"], e["itemId"]))
            elif t == "ITEM_UNDO":
                for i in range(len(purchases) - 1, -1, -1):
                    if purchases[i][1] == e.get("beforeId"):
                        purchases.pop(i)
                        break
            elif t == "SKILL_LEVEL_UP" and e.get("levelUpType", "NORMAL") == "NORMAL":
                skills.append(SKILL.get(e.get("skillSlot"), "?"))
        f["purchases"] = [[t // 1000, i] for t, i in purchases]
        f["skills"] = skills[:18]

        # 첫 귀환: 시작템 이후(60초 뒤) 첫 구매
        later = [(t, i) for t, i in purchases if t > 60_000]
        if later:
            t0 = later[0][0]
            f["first_back"] = t0 // 1000
            f["first_back_items"] = [i for t, i in later if t - t0 <= 20_000]
            fr = _frame_at(frames, t0 // 60_000)
            if fr:
                f["first_back_gold"] = fr["participantFrames"][str(pid)].get("currentGold")

        # 킬/데스/플레이트/와드
        kills_lane, deaths14, solo, plates, wards, control = [], 0, 0, 0, 0, 0
        for e in events:
            t = e.get("type")
            ts = e.get("timestamp", 0)
            if t == "CHAMPION_KILL":
                if e.get("victimId") == pid and ts < 14 * 60_000:
                    deaths14 += 1
                if e.get("killerId") == pid:
                    if not e.get("assistingParticipantIds"):
                        solo += 1
                    if ts < 14 * 60_000 and opp_pid and e.get("victimId") == opp_pid:
                        kills_lane.append(e)
            elif t == "TURRET_PLATE_DESTROYED" and e.get("killerId") == pid:
                plates += 1
            elif t == "WARD_PLACED" and e.get("creatorId") == pid:
                wards += 1
                if e.get("wardType") == "CONTROL_WARD":
                    control += 1

        # 라인 상대를 잡은 직후 무엇을 했나 (귀환했나, 플레이트를 깼나)
        after = []
        for k in kills_lane:
            ts = k["timestamp"]
            back = next((t for t, _ in purchases if ts < t <= ts + 75_000), None)
            plate = any(e.get("type") == "TURRET_PLATE_DESTROYED" and e.get("killerId") == pid
                        and ts < e["timestamp"] <= ts + 60_000 for e in events)
            fr = _frame_at(frames, ts // 60_000)
            gold = fr["participantFrames"][str(pid)].get("currentGold") if fr else None
            after.append({"t": ts // 1000, "backed": back is not None,
                          "back_delay": (back - ts) // 1000 if back else None,
                          "plate": plate, "gold": gold})
        mins = max(1.0, info.get("gameDuration", 1800) / 60)
        f.update({"deaths14": deaths14, "solo_kills": solo, "plates": plates,
                  "wards_pm": round(wards / mins, 2), "control_wards": control, "after_lane_kill": after,
                  "keystone": _keystone(p), "styles": _styles(p),
                  "spells": sorted([p.get("summoner1Id"), p.get("summoner2Id")])})
        out.append({"puuid": p["puuid"], "champion": p["championName"], "position": p.get("teamPosition", ""),
                    "win": p["win"], "opp_champion": opp["championName"] if opp else None, "features": f})
    return out


def _keystone(p: dict) -> int | None:
    try:
        return p["perks"]["styles"][0]["selections"][0]["perk"]
    except (KeyError, IndexError):
        return None


def _styles(p: dict) -> list[int]:
    try:
        return [s["style"] for s in p["perks"]["styles"]]
    except KeyError:
        return []


# ---------------------------------------------------------------- 크롤링

def crawl(riot: Riot, brain: Brain, *, tiers=("challenger", "grandmaster"), pro_accounts=(),
          champion: str | None = None, matches_per_player: int = 20, max_calls: int = 600,
          log: Callable[[str], None] = print) -> dict:
    """상위권 경기를 모아서 학습. champion을 주면 그 챔피언 장인들의 경기를 더 깊게 판다."""
    calls = 0

    def spent(n=1):
        nonlocal calls
        calls += n
        return calls >= max_calls

    for rid in pro_accounts:
        acc = riot.account(rid)
        spent()
        if acc:
            brain.upsert_player(acc["puuid"], "PRO", 0, 0, 0, riot_id=rid, is_pro=True)
            log(f"프로 계정 등록: {rid}")
    for tier in tiers:
        entries = riot.apex_league(tier)
        spent()
        for e in entries:
            if e.get("puuid"):
                brain.upsert_player(e["puuid"], tier.upper(), e.get("leaguePoints", 0),
                                    e.get("wins", 0), e.get("losses", 0))
        log(f"{tier} {len(entries)}명 목록 갱신")

    players = brain.players()
    apex = set(players)
    mains = set(brain.mains(champion)) if champion else set()
    if champion:
        order = brain.mains(champion) + [p for p in players if players[p]["is_pro"]]
        depth = 100
        if not order:
            log(f"{champion} 장인 기록이 아직 없음 → 전체 상위권부터 훑어서 찾는 중")
    else:
        order, depth = [], matches_per_player
    order += sorted(players, key=lambda p: (-players[p]["is_pro"], -players[p]["lp"]))
    seen_players, new_matches = set(), 0
    for puuid in order:
        if puuid in seen_players:
            continue
        seen_players.add(puuid)
        ids = riot.match_ids(puuid, count=depth)
        if spent():
            break
        for mid in ids:
            if brain.has_match(mid):
                continue
            m = riot.match(mid)
            if spent() or not m:
                break
            if champion and not any(p["puuid"] == puuid and p["championName"] == champion
                                    for p in m["info"]["participants"]) and puuid in mains:
                # 장인 경기 중 다른 챔피언 판은 타임라인 생략(호출 절약), 기록만 남김
                brain.add_match(mid, m["info"].get("gameVersion", ""), [])
                continue
            tl = riot.timeline(mid)
            if spent() or not tl:
                break
            samples = extract(m, tl, keep_puuids=apex)
            brain.add_match(mid, m["info"].get("gameVersion", ""), samples)
            new_matches += 1
            hit = [s["champion"] for s in samples]
            log(f"경기 {mid}: 상위권 {len(samples)}명 학습 ({', '.join(hit)})")
        if calls >= max_calls:
            break
    log(f"완료: 새 경기 {new_matches}개, API 호출 {calls}회")
    return {"new_matches": new_matches, "calls": calls}


# ---------------------------------------------------------------- 정점 플레이어 합의

def _median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _mode(xs) -> tuple | None:
    """최빈값과 일치율"""
    xs = [x for x in xs if x not in (None, "", [], ())]
    if not xs:
        return None
    c = Counter(xs).most_common(1)[0]
    return c[0], round(100 * c[1] / len(xs))


def skill_max_order(skills: list[str]) -> str | None:
    """선마 순서: 레벨업 기록에서 Q/W/E 중 먼저 5포인트 찍힌 순"""
    cnt, done = Counter(), []
    for s in skills:
        if s in "QWE":
            cnt[s] += 1
            if cnt[s] == 5 and s not in done:
                done.append(s)
    if len(done) < 2:
        if len(skills) < 9:
            return None
        rest = sorted("QWE", key=lambda s: -cnt[s])
        done = [s for s in rest]
    for s in "QWE":
        if s not in done:
            done.append(s)
    return ">".join(done[:3])


def expert_score(games: int, wins: int, player: dict) -> float:
    wr = (wins + 3) / (games + 6)               # 판수가 적으면 50%로 끌어당김
    return wr * math.log1p(games) + player.get("lp", 0) / 1500 + (0.6 if player.get("is_pro") else 0)


def benchmark(brain: Brain, champion: str, position: str | None = None, *, top_n: int = 5,
              min_games: int = 5, opp_champion: str | None = None, dd: DataDragon | None = None) -> dict:
    samples = brain.samples(champion, position)
    if not samples:
        return {"champion": champion, "position": position, "experts": [], "note": "학습 데이터 없음"}
    if not position:
        position = Counter(s["position"] for s in samples).most_common(1)[0][0]
        samples = [s for s in samples if s["position"] == position]
    players = brain.players()
    by_player: dict[str, list[dict]] = defaultdict(list)
    for s in samples:
        by_player[s["puuid"]].append(s)

    ranked = sorted(by_player, key=lambda u: -expert_score(
        len(by_player[u]), sum(s["win"] for s in by_player[u]), players.get(u, {})))
    qualified = [u for u in ranked if len(by_player[u]) >= min_games]
    low_conf = len(qualified) < min(3, top_n)
    experts = (qualified or ranked)[:top_n]

    def per_expert(fn) -> list:
        """각 장인별 중앙값 → 장인들끼리 다시 중앙값 (한 사람이 판수로 결과를 독점하지 않게)"""
        return [_median([fn(s["features"]) for s in by_player[u]]) for u in experts]

    def per_expert_mode(fn) -> list:
        vals = []
        for u in experts:
            m = _mode([fn(s["features"]) for s in by_player[u]])
            vals.append(m[0] if m else None)
        return vals

    out: dict = {"champion": champion, "position": position, "low_confidence": low_conf}
    out["experts"] = [{
        "riot_id": players.get(u, {}).get("riot_id"), "tier": players.get(u, {}).get("tier"),
        "lp": players.get(u, {}).get("lp"), "pro": players.get(u, {}).get("is_pro", False),
        "games": len(by_player[u]), "wins": sum(s["win"] for s in by_player[u]),
    } for u in experts]
    for key in ("cs10", "cs15", "golddiff10", "golddiff15", "csdiff10", "first_back", "first_back_gold",
                "deaths14", "wards_pm", "plates", "solo_kills"):
        v = _median(per_expert(lambda f, k=key: f.get(k)))
        if v is not None:
            out[key] = round(v, 1)

    sk = _mode(per_expert_mode(lambda f: skill_max_order(f.get("skills", []))))
    if sk:
        out["skill_max"] = {"order": sk[0], "agree": sk[1]}
    first3 = _mode(per_expert_mode(lambda f: "".join(f.get("skills", [])[:3]) or None))
    if first3:
        out["skill_start"] = {"order": first3[0], "agree": first3[1]}
    ks = _mode(per_expert_mode(lambda f: f.get("keystone")))
    if ks:
        out["keystone"] = {"id": ks[0], "agree": ks[1]}
    sp = _mode(per_expert_mode(lambda f: tuple(f.get("spells") or ()) or None))
    if sp:
        out["spells"] = {"ids": list(sp[0]), "agree": sp[1]}

    if dd and dd.items:
        def core(f) -> tuple | None:
            seq = []
            for _, i in f.get("purchases", []):
                if dd.is_legendary(i) and i not in seq:
                    seq.append(i)
            return tuple(seq[:3]) or None
        cores = per_expert_mode(core)
        slots = []
        for slot in range(3):
            m = _mode([c[slot] for c in cores if c and len(c) > slot])
            if m:
                slots.append({"item": dd.name(m[0]), "id": m[0], "cost": dd.cost(m[0]), "agree": m[1]})
        if slots:
            out["core_build"] = slots

    # 라인 상대를 잡은 직후 판단 (모든 장인의 사례를 합침)
    afters = [a for u in experts for s in by_player[u] for a in s["features"].get("after_lane_kill", [])]
    if afters:
        backed = [a for a in afters if a["backed"]]
        out["after_lane_kill"] = {
            "cases": len(afters),
            "back_within_75s": round(100 * len(backed) / len(afters)),
            "plate_within_60s": round(100 * sum(a["plate"] for a in afters) / len(afters)),
            "median_back_delay": _median([a["back_delay"] for a in backed]),
            "median_gold_when_backed": _median([a["gold"] for a in backed]),
        }

    if opp_champion:
        mu = [s for s in samples if s["opp_champion"] == opp_champion]
        if len(mu) >= 3:
            out["matchup"] = {
                "vs": opp_champion, "games": len(mu),
                "winrate": round(100 * sum(s["win"] for s in mu) / len(mu)),
                "golddiff10": _median([s["features"].get("golddiff10") for s in mu]),
                "csdiff10": _median([s["features"].get("csdiff10") for s in mu]),
                "deaths14": _median([s["features"].get("deaths14") for s in mu]),
            }
    return out


def describe(b: dict) -> str:
    """benchmark 결과를 LLM/사람이 읽을 한국어 요약으로"""
    if not b.get("experts"):
        return f"{b.get('champion')}: 챌린저 학습 데이터 없음 (학습 탭에서 수집 필요)"
    pos = POS_KO.get(b.get("position") or "", b.get("position") or "")
    lines = [f"[{b['champion']} {pos} 정점 플레이어 {len(b['experts'])}명 합의 통계"
             + (" — 표본 적음, 참고만" if b.get("low_confidence") else "") + "]"]
    ex = ", ".join(f"{e['riot_id'] or '익명'}({'프로' if e['pro'] else e['tier']}, {e['games']}판 {e['wins']}승)"
                   for e in b["experts"])
    lines.append(f"장인: {ex}")

    def add(label, key, unit=""):
        if key in b:
            lines.append(f"{label}: {b[key]}{unit}")
    add("10분 CS", "cs10")
    add("15분 CS", "cs15")
    add("10분 맞라이너 대비 골드차", "golddiff10")
    add("10분 맞라이너 대비 CS차", "csdiff10")
    if "first_back" in b:
        fb = int(b["first_back"])
        lines.append(f"첫 귀환 시점: {fb // 60}분 {fb % 60}초" +
                     (f" (보유 골드 약 {int(b['first_back_gold'])})" if b.get("first_back_gold") else ""))
    add("14분 전 데스", "deaths14", "회")
    add("분당 와드", "wards_pm")
    add("플레이트", "plates", "개")
    if "skill_start" in b:
        lines.append(f"1~3레벨 스킬: {b['skill_start']['order']} (일치 {b['skill_start']['agree']}%)")
    if "skill_max" in b:
        lines.append(f"스킬 선마: {b['skill_max']['order']} (일치 {b['skill_max']['agree']}%)")
    if "core_build" in b:
        lines.append("코어 빌드 순서: " + " → ".join(f"{c['item']}({c['cost']}골드, 일치 {c['agree']}%)"
                                               for c in b["core_build"]))
    if "after_lane_kill" in b:
        a = b["after_lane_kill"]
        lines.append(
            f"맞라이너를 잡은 직후({a['cases']}사례): 75초 안에 귀환 {a['back_within_75s']}%, "
            f"60초 안에 플레이트 파괴 {a['plate_within_60s']}%"
            + (f", 귀환까지 중앙값 {int(a['median_back_delay'])}초" if a.get("median_back_delay") is not None else "")
            + (f", 귀환 시 보유골드 중앙값 {int(a['median_gold_when_backed'])}" if a.get("median_gold_when_backed") else ""))
    if "matchup" in b:
        m = b["matchup"]
        lines.append(f"상대 {m['vs']} 상대로 {m['games']}판: 승률 {m['winrate']}%, 10분 골드차 {m['golddiff10']}, "
                     f"10분 CS차 {m['csdiff10']}, 14분 전 데스 {m['deaths14']}")
    return "\n".join(lines)
