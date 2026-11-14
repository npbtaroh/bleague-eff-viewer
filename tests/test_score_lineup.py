"""Run with: python3 -m unittest discover -s tests"""
from pathlib import Path
import itertools
import random
import unittest

ROOT = Path(__file__).resolve().parents[1]
ns = {"__name__": "score_test_app", "__file__": str(ROOT / "eff_app/app.py")}
exec(compile((ROOT / "eff_app/app.py").read_text(), ns["__file__"], "exec"), ns)
roles = ns["ROLES"]
opt = ns["optimize_score_lineup"]
bonus = {1: 0, 2: 1, 3: 3, 4: 5, 5: 10}

def brute(players):
 best=None
 for lineup in itertools.permutations(players,5):
  if any(role not in p['position'].split('/') for role,p in zip(roles,lineup)):continue
  counts={p['team_name']:sum(q['team_name']==p['team_name'] for q in lineup) for p in lineup}
  eff=sum(p['efficiency'] for p in lineup);wins=sum(p['matchup']['result_mark']=='○' for p in lineup)
  score=eff*(100+wins*10+bonus[max(counts.values())])/100
  best=score if best is None else max(best,score)
 return None if best is None else round(best,2)

class ScoreLineupTests(unittest.TestCase):
    def test_against_exhaustive_search_and_bonus_rules(self):
        rng=random.Random(717)
        for case in range(40):
         players=[{'player_id':str(i),'league':'L','team_name':str(i%3),'position':'/'.join(rng.sample(roles,rng.randint(1,3))), 'efficiency':rng.randint(-15,40),'matchup':{'result_mark':'○' if i%3==0 else '×'}} for i in range(8)]
         result=opt(players);assert (None if result is None else result['score_total'])==brute(players),(case,result)
        fixture=[{'player_id':str(i),'league':'L','team_name':str(i//2),'position':roles[i],'efficiency':30,'matchup':{'result_mark':'○' if i<3 else '×'}} for i in range(5)]
        r=opt(fixture);assert r['same_team_bonus_percent']==1 and r['score_total']==196.5,r
        for i in range(3):fixture[i]['team_name']='A'
        fixture[3]['team_name']='B';fixture[4]['team_name']='C';assert opt(fixture)['score_total']==199.5
        for n,pct in [(2,1),(3,3),(4,5),(5,10)]:
         for i,p in enumerate(fixture):p['team_name']='A' if i<n else str(i)
         assert opt(fixture)['same_team_bonus_percent']==pct
        assert opt([]) is None
        fixture[0]['matchup']={};assert opt(fixture) is None
