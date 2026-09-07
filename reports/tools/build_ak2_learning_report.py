#!/usr/bin/env python3
"""Build a shareable AK2 case study from runstats, server summaries and evidence.

The existing runstats definitions remain unchanged; narrative verification is
stored separately. No network requests or simulator mutations are performed.
"""
import argparse
import copy
import datetime as dt
import hashlib
import html
import json
import re
import sqlite3
from pathlib import Path

LATEST = 'KfRgiEVUNuTv0SUNlcWOJUgn'
BEFORE = 'i3DeiIf8iN2lkCyDqGy7izYW'
AFTER = 'SSivLkFdZvZ4czmTqQulgE8H'
SIM = Path('/Users/aigizk/projects/HackerSprint2_sim')
ROOT = Path(__file__).resolve().parents[2]

def read(p): return json.loads(Path(p).read_text())
def parse(s): return dt.datetime.fromisoformat(re.sub(r'(\.\d{6})\d+', r'\1', s.replace('Z', '+00:00')))
def num(n, digits=2):
    if n is None: return '—'
    return f'{n:,.{digits}f}'.replace(',', '\u202f').replace('.', ',')
def esc(x): return html.escape(str(x))
def score(r):
    if r['status'] != 'completed': return None
    if not r.get('outcome', {}).get('slo_passed'): return 0
    for bound, points in [(50e6,100),(100e6,90),(150e6,80),(200e6,70),(300e6,60),(500e6,50),(1e9,40),(2e9,30),(3e9,20),(5e9,10)]:
        if r['costs']['total_cost_minor']/100 < bound: return points
    return 0

def dataset(args):
    base=read(args.base); refreshed=read(args.refresh); catalog=read(args.catalog)
    by_id={r['run_id']:copy.deepcopy(r) for r in base['runs'] if r['agent_id']=='ak2-agent'}
    for r in refreshed['runs']: by_id[r['run_id']]=copy.deepcopy(r)
    facts={r['run_id']:r for r in read(args.facts)}
    current={r['run_id']:r for r in catalog['runs']}
    cutoff=current[LATEST]['created_at']
    runs=sorted((r for r in by_id.values() if r['created_at']<=cutoff),key=lambda r:(r['created_at'],r['run_id']))
    local={}
    c=sqlite3.connect(Path(args.memory).resolve().as_uri()+'?mode=ro&immutable=1',uri=True)
    for row in c.execute('SELECT state FROM runs'):
        s=json.loads(row[0]);overview=(s.get('last_observation') or {}).get('data',{}).get('overview') or {}
        external=overview.get('run_id')
        if not external: continue
        decisions=[]
        for eid,kind,raw in c.execute("SELECT id,kind,data FROM events WHERE run=? AND kind IN ('decision','reflection') ORDER BY id",(s['id'],)):
            d=json.loads(raw)
            if external in (BEFORE,AFTER,LATEST):
                decisions.append({'event_id':eid,'kind':kind,'assessment':d.get('assessment'),'used_memory_ids':d.get('used_memory_ids',[]),'accepted_lessons':d.get('accepted_lessons',[])})
        local[external]={'local_id':s['id'],'task':s.get('task'),'model':s.get('model'),'effort':s.get('effort'),'decisions':s.get('decisions'),'llm_seconds':s.get('llm_seconds'),'runtime_versions':s.get('runtime_versions',[]),'knowledge_hash':s.get('knowledge_hash'),'memory_enabled':s.get('recall'),'learning_enabled':s.get('learn'),'retrospective_status':s.get('retrospective',{}).get('status'),'selected_decisions':decisions}
    c.close()
    verified=[]
    for r in runs:
        rid=r['run_id'];s=current[rid]['cached_summary']['Summary'];o=s['Overview'];a=o['Availability']
        assert not r.get('scan_error'),r.get('scan_error')
        assert r['domain_event_count']==s['EventCount'],rid+' event mismatch'
        assert r['costs']['total_cost_minor']==o['Costs']['TotalCostMinor'],rid+' cost mismatch'
        assert r['status']==o['RunStatus'],rid+' status mismatch'
        if r['status']=='completed': assert abs(r['observed_seconds']-r['scheduled_seconds'])<.01
        r['outcome']={'uptime_ratio':a.get('UptimeRatio'),'slo_passed':a.get('SLOPassed'),'downtime_seconds':a['DowntimeDuration']/1e9,'real_started_at':s.get('RealStartedAt'),'real_completed_at':s.get('RealCompletedAt')}
        r['score_current_policy']=score(r)
        r['metrics_generated_at']=refreshed['generated_at'] if rid in {x['run_id'] for x in refreshed['runs']} else base['generated_at']
        if rid in facts:
            assert facts[rid]['event_count']==r['domain_event_count']
            assert facts[rid]['request_count']==r['agent_request_count']
            r['behavior']=facts[rid]
        if rid in local:r['agent_trace']=local[rid]
        verified.append(rid)
    cross=read(ROOT/'ak2-agent/worlds/uptick/experiments/cross-run-memory-use.json')
    memory_case={'lesson':{k:cross['lesson'][k] for k in ('id','title','body','procedure')},'source_seed':89,'application_seed':cross['application']['seed'],'application_event_id':cross['application']['event_id'],'assessment':cross['application']['decision']['assessment'],'used_memory_ids':cross['application']['decision']['used_memory_ids'],'outcome_verified':cross['outcome_verified'],'probes':[{'event_id':p['event_id'],'page':p['action']['params']['page'],'status':p['response']['data']['status'],'load_units':p['response']['data']['load_units']} for p in cross['post_expansion_probes']]}
    paths=[args.base,args.refresh,args.catalog,args.facts,ROOT/'ak2-agent/worlds/uptick/experiments/cross-run-memory-use.json',SIM/'internal/runstats/analyzer.go',SIM/'tools/build_runstats_report.py']
    return {'schema_version':1,'title':'AK2: до и после накопления опыта','built_at':dt.datetime.now(dt.timezone.utc).isoformat(),'server_snapshot_at':catalog['captured_at'],'requested_latest_run':LATEST,'scope':'All 24 AK2 runs through the requested run; 168h and 48h shown separately. Four journals freshly rescanned. Other stats verified against fresh server event counts, costs and status.','definitions':base['source']['definitions'],'limitations':base['source']['limitations']+['Development runs changed runtime, knowledge and task prompts. This is not a controlled memory ablation.','Original DDoS analyzer requires system availability at the command response; delayed recovery may be rejected.','Original disk metric follows a database object; migration does not always close the retired object incident.','Zero DDoS incidents means no attack in the scenario, not instant mitigation.','Cost/event and capacity-incident duration are descriptive simulation measures, not isolated agent efficiency or reaction time.'],'verified_runs':verified,'sources':[{'name':Path(p).name,'sha256':hashlib.sha256(Path(p).read_bytes()).hexdigest()} for p in paths],'memory_case':memory_case,'runs':runs}

def compact(r):
    a=r['incidents'];o=r['outcome'];b=r.get('behavior',{});trace=r.get('agent_trace',{})
    return {'id':r['run_id'],'seed':r['seed'],'at':r['created_at'],'h':r['horizon_kind'],'status':r['status'],'uptime':None if o['uptime_ratio'] is None else o['uptime_ratio']*100,'slo':o['slo_passed'],'score':r['score_current_policy'],'downtime':o['downtime_seconds'],'cost':r['costs']['total_cost_minor']/100,'costHour':r['costs']['cost_per_observed_hour_minor']/100 if r['costs']['cost_per_observed_hour_minor'] is not None else None,'costEvent':r['costs']['cost_per_domain_event_minor']/100 if r['costs']['cost_per_domain_event_minor'] is not None else None,'costRequest':r['costs']['cost_per_agent_request_minor']/100 if r['costs']['cost_per_agent_request_minor'] is not None else None,'hours':r['observed_seconds']/3600,'events':r['domain_event_count'],'http':r['agent_request_count'],'backendCount':a['backend_capacity']['count'],'backendMean':a['backend_capacity']['mean_duration_seconds'],'backendTotal':a['backend_capacity']['total_duration_seconds'],'backendOpen':a['backend_capacity']['unresolved_count'],'dbCount':a['database_disk_space']['count'],'dbOpen':a['database_disk_space']['unresolved_count'],'diskCount':a['all_disk_space_union']['count'],'diskOpen':a['all_disk_space_union']['unresolved_count'],'diskMean':a['all_disk_space_union']['mean_duration_seconds'],'ddosCount':a['ddos']['count'],'ddosResolved':a['ddos']['resolved_count'],'ddosMean':a['ddos']['mean_resolution_seconds'],'authErrors':r['credential_failures']['count'],'decisions':trace.get('decisions'),'llm':trace.get('llm_seconds'),'upserts':b.get('commands',{}).get('firewall.rules.upsert'),'falseBlocks':sum(x['non_attack_requests'] for x in b.get('firewall_blocks',{}).values()) if b else None}

def make_markdown(D):
    index={r['run_id']:compact(r) for r in D['runs']};b,a,z=(index[k] for k in (BEFORE,AFTER,LATEST))
    lines=['# AK2 — пример работы до и после накопления опыта','',
    'Отчёт для Hacker Sprint #2. Данные: 24 запуска AK2, из них 10 завершённых; недельные и двухдневные сценарии разделены. Последний run заново получен с сервера и разобран по полному журналу.','',
    'Под обучением здесь понимается накопление и применение опыта во внешней памяти. Веса базовой модели не менялись.','',
    '## Что изменилось на одном и том же сценарии','',
    'На seed 1 ранняя попытка перебирала отдельные IP, затем заблокировала всю подсеть и сделала недоступными контрольные профили на 30 минут. В последующей попытке агент читал сводки трафика и установил одно правило по сочетанию подсети и точного User-Agent. По журналу оно заблокировало 49 645 запросов атаки и ни одного наблюдавшегося запроса вне атаки.','',
    '| Метрика | Ранняя попытка | Последующая попытка |','|---|---:|---:|',
    f"| Uptime за 48 часов | {num(b['uptime'],4)}% | {num(a['uptime'],4)}% |",
    f"| Простой | {num(b['downtime']/60)} мин | {num(a['downtime']/60)} мин |",
    f"| Игровые расходы | {num(b['cost'])} ₽ | {num(a['cost'])} ₽ |",
    f"| Расходы за симуляционный час | {num(b['costHour'])} ₽ | {num(a['costHour'])} ₽ |",
    '| Установки/обновления firewall-правил | 18 | 1 |','| Обращения к logs/summary | 0 | 20 |','| Баллы по текущей шкале | 0 | 100 |','',
    f"Расходы снизились на {num((1-a['cost']/b['cost'])*100)}%, простой — на {num((1-a['downtime']/b['downtime'])*100)}%. Между попытками менялись код, инструменты и текст задания; эту разницу нельзя целиком приписать памяти. Число эпизодов недостатка backend выросло с 31 до 82 — улучшение не было одинаковым по всем метрикам.",'',
    '## Проверяемый пример переноса памяти','',
    'В seed 89 агент выяснил, что свободного ресурса всего пула недостаточно: отдельный запрос должен помещаться в свободную мощность конкретного backend. Урок сохранён как uptick.per_backend_request_headroom. В seed 67 агент явно сослался на него и рассчитал ceil(1696 / (100 − 16)) = 21 backend. После добавления 19 серверов оба контрольных запроса вернули HTTP 200. Исходные решения и ответы включены в JSON и HTML. Это подтверждённое применение опыта между запусками одного мира.','',
    '## Последний запуск','',
    f"Run {LATEST}, seed 939280: 48 часов, uptime {num(z['uptime'],6)}%, простой {num(z['downtime'])} с, расходы {num(z['cost'])} ₽, оценка 100/100. Инцидент диска БД закрыт за 9,14 с; выполнены backup/restore, переключение на новую БД и удаление старого сервера.",'',
    'DDoS-сценариев в последнем мире не было. При этом агент ошибочно интерпретировал обычный WebCamera/1.0 как атаку: его правило заблокировало 5 144 запроса вне DDoS. Это ограничение качества сохранилось несмотря на успешный SLO. Последний run не является доказательством улучшения защиты от DDoS.','',
    '## Полная выборка завершённых двухдневных попыток','',
    '| Seed | Uptime | Расходы, млн ₽ | Баллы |','|---|---:|---:|---:|']
    for r in D['runs']:
        x=compact(r)
        if x['h']=='48h' and x['status']=='completed':lines.append(f"| {x['seed']} | {num(x['uptime'],4)}% | {num(x['cost']/1e6)} | {x['score']} |")
    lines+=['','Четыре из семи завершённых двухдневных попыток выполнили SLO. Четыре других двухдневных мира не завершены; они также перечислены в HTML. В истории есть последующие неудачи на seed 102 и 10, поэтому улучшение нельзя описывать как устойчивый монотонный тренд.','',
    '## Как читать показатели','',
    '- Стоимость относится к игровой инфраструктуре, а не к расходам на LLM. Основное сравнение — на час фактически наблюдённого симуляционного времени.','- Средняя длительность инцидентов рассчитана только по закрытым эпизодам; незакрытые показаны отдельно. Естественное освобождение мощности тоже закрывает эпизод и не обязательно означает действие агента.','- Старый счётчик дисков отслеживает объект БД. После миграции прежний объект может остаться «незакрытым», хотя сайт восстановлен.','- Старый DDoS-счётчик требует доступности непосредственно при ответе команды. В двух попытках seed 1 backend восстановился после финального правила через 1,50 и 1,74 секунды; формальная метрика при этом отвергла кандидатов. В HTML это обозначено отдельно от фактов журнала.','- Нулевые ошибки credentials означают отсутствие записанных отказов. Ошибки до создания run или до попадания запроса в его аудит исторически не покрыты.','- Количество событий зависит от мира и действий агента. Цена события — описательная величина, её нельзя отдельно считать мерой качества.','- SLO определяется моделью доступности симулятора; он не гарантирует отсутствие блокировок других легитимных профилей.','',
    '## Краткий текст для формы','',
    'Мы приложили HTML-отчёт с проверяемым переносом урока между запусками, сравнением двух попыток одного сценария и полной историей завершённых прогонов AK2. На seed 1 агент перешёл от перебора IP и широкого запрета подсети к одному правилу по подсети и User-Agent: uptime вырос с 98,7358% до 99,7023%, расходы снизились на 15,66%. Отдельно показан урок о запасе мощности backend: он был получен в одном запуске, явно применён в другом и проверен успешными запросами. Последний run завершил 48 часов с uptime 99,919% и 100/100. В отчёте сохранены неудачи и ограничения: версии и подсказки менялись, поэтому показана наблюдаемая эволюция поведения, а средний причинный эффект памяти требует отдельного эксперимента.','']
    return '\n'.join(lines)

def main():
    p=argparse.ArgumentParser();p.add_argument('--dataset',help='Rebuild offline from the assembled report JSON');p.add_argument('--base',default=str(SIM/'reports/all-runs-stats.json'));p.add_argument('--refresh',default='/private/tmp/ak2-report-data/refreshed-stats.json');p.add_argument('--catalog',default='/private/tmp/ak2-report-data/catalog.json');p.add_argument('--facts',default='/private/tmp/ak2-report-data/facts.json');p.add_argument('--memory',default='/private/tmp/ak2-report-data/experience.sqlite3');p.add_argument('--out',default=str(ROOT/'reports'));args=p.parse_args()
    D=read(args.dataset) if args.dataset else dataset(args)
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    (out/'ak2-learning-report.json').write_text(json.dumps(D,ensure_ascii=False,indent=2)+'\n')
    (out/'ak2-learning-report.md').write_text(make_markdown(D))
    template=Path(__file__).with_name('ak2-learning-report.template.html').read_text()
    packed=json.dumps({'meta':{k:v for k,v in D.items() if k!='runs'},'runs':[compact(r) for r in D['runs']]},ensure_ascii=False,separators=(',',':')).replace('<','\\u003c')
    template=template.replace('__REPORT_DATA__',packed)
    # Evidence embedded for an offline, single-file handoff.
    template=template.replace('__FULL_DATA__',json.dumps(D,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c'))
    (out/'ak2-learning-report.html').write_text(template)
    print(json.dumps({'runs':len(D['runs']),'completed':sum(r['status']=='completed' for r in D['runs']),'newly_scanned':4,'output':str(out/'ak2-learning-report.html')},ensure_ascii=False))

if __name__=='__main__':main()
