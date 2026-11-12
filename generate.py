#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静态站点生成器：调 EnOS 接口，把山东16市气象数据落成 docs/ 纯静态网站。

设计要点（对应「公网托管 + 定时更新」需求）：
- docs/data/*.json 即更新状态：已生成且有效的文件直接跳过（增量续跑；
  CI 超时/失败下次自动从断点继续，配合 --push 每城市提交一次防丢进度）。
- 上游 403 限流：单次取数失败不写文件、不打断整体，下次运行自动重试。
- 预测：16 市共用同一最新起报（只探测济南一次），保证页面口径一致。
- 凭据：优先环境变量 ENOS_AK / ENOS_SK（GitHub Secrets 注入），
  本地回退读 AccessKey.txt。

用法：
  python generate.py                             # 全量增量更新（本地）
  python generate.py --cities 370100 --since 2026-08   # 本地调试，少调上游
  python generate.py --push                      # CI：每城市完成后 git commit+push
"""
import argparse
import csv
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import weather_api
from weather_api import ApiError, ATTRIBUTES

DOCS = os.path.join(BASE_DIR, 'docs')
DATA = os.path.join(DOCS, 'data')
EPOCH_YEAR = 2024
UNITS = {'TMP': '°C', 'WS': 'm/s', 'SSRD': 'W/m²', 'RH': '%'}
RUN_CACHE_TTL = 6 * 3600      # 最新起报映射缓存 6h
NAN_RETRY_AGE = 24 * 3600     # 未入库月 24h 后重探


def log(msg):
    print(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def load_cities():
    cities = []
    with open(os.path.join(BASE_DIR, 'area_info.csv'), encoding='utf-8') as f:
        for row in csv.DictReader(f):
            if row['province_code'] == '370000':
                cities.append({'code': row['admin_code'], 'name': row['city_name']})
    return cities


def load_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def month_range(since, today):
    out = []
    y, m = int(since[:4]), int(since[5:7])
    while (y, m) <= (today.year, today.month):
        out.append(f'{y}-{m:02d}')
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def gen_history(code, yyyymm):
    """生成 history_{code}_{yyyymm}.json（api_history 同构）。返回是否新写入。"""
    path = os.path.join(DATA, f'history_{code}_{yyyymm}.json')
    old = load_json(path)
    if old and (old.get('hasData') or time.time() - old.get('fetchedAt', 0) < NAN_RETRY_AGE):
        return False
    try:
        tmp = weather_api.fetch_era5(code, yyyymm, 'TMP')
        if not tmp['hasData']:
            write_json(path, {'month': yyyymm, 'hasData': False, 'fetchedAt': time.time()})
            return True
        series = {'TMP': tmp}
        for attr in ('WS', 'SSRD', 'RH'):
            series[attr] = weather_api.fetch_era5(code, yyyymm, attr)
        write_json(path, {'month': yyyymm, 'hasData': True, 'units': UNITS,
                          'series': series, 'fetchedAt': time.time()})
        return True
    except ApiError as e:
        log(f'  {code} {yyyymm} 上游错误 {e.code}：跳过，下次运行重试')
        return False


def latest_run():
    """16市共用的最新起报日：data/_run.json 缓存 6h，只探测济南。"""
    path = os.path.join(DATA, '_run.json')
    old = load_json(path)
    if old and time.time() - old.get('fetchedAt', 0) < RUN_CACHE_TTL:
        return old.get('runDate')
    try:
        run = weather_api._latest_run('370100')
    except ApiError as e:
        log(f'起报探测失败 {e.code}：沿用旧值')
        return (old or {}).get('runDate')
    write_json(path, {'runDate': run, 'fetchedAt': time.time()})
    return run


def build_daily(fc):
    """45天按日聚合（口径与 serve.py api_forecast_daily 一致）。"""
    agg = {attr: weather_api.daily_aggregate(fc['series'][attr]) for attr in ATTRIBUTES}
    by_date = {}
    for attr, rows in agg.items():
        for r in rows:
            d = by_date.setdefault(r['date'], {'date': r['date']})
            if attr == 'TMP':
                d['tmax'], d['tmin'] = r['max'], r['min']
            elif attr == 'WS':
                d['ws_avg'], d['ws_max'] = r['avg'], r['max']
            elif attr == 'SSRD':
                d['ssrd_avg'] = r['avg']
            elif attr == 'RH':
                d['rh_avg'] = r['avg']
    return {'runDate': fc['runDate'],
            'rows': [by_date[d] for d in sorted(by_date)],
            'fetchedAt': time.time()}


def gen_forecast(code, run):
    """最新起报的逐小时 + 日报表。同起报已有完整文件则跳过。"""
    if not run:
        log(f'  {code} 无可用起报，跳过预测')
        return False
    daily_path = os.path.join(DATA, f'forecast_daily_{code}.json')
    old = load_json(os.path.join(DATA, f'forecast_{code}.json'))
    if old and old.get('runDate') == run and old.get('hasData') \
            and os.path.exists(daily_path):
        return False
    series = {}
    for attr in ATTRIBUTES:
        try:
            series[attr] = weather_api.fetch_forecast(code, run, attr)
        except ApiError as e:
            log(f'  {code} 预测 {attr} 上游错误 {e.code}：保留旧文件，下次重试')
            return False
    fc = {'runDate': run, 'hasData': True, 'units': UNITS, 'series': series,
          'fetchedAt': time.time()}
    write_json(os.path.join(DATA, f'forecast_{code}.json'), fc)
    write_json(daily_path, build_daily(fc))
    return True


def gen_months_file(code, months):
    """月份列表只含已入库月（前端下拉框直接用）。"""
    have = []
    for m in months:
        h = load_json(os.path.join(DATA, f'history_{code}_{m}.json'))
        if h and h.get('hasData'):
            have.append(m)
    write_json(os.path.join(DATA, f'months_{code}.json'),
               {'latest': have[-1] if have else None, 'months': have,
                'fetchedAt': time.time()})
    return have


def gen_years(code, today):
    """同月多年对比：从已生成的 history 文件派生（无上游调用）。"""
    for attr in ATTRIBUTES:
        for mm in range(1, 13):
            years = {}
            for y in range(EPOCH_YEAR, today.year + 1):
                if y == today.year and mm >= today.month:
                    continue
                h = load_json(os.path.join(DATA, f'history_{code}_{y}-{mm:02d}.json'))
                if h and h.get('hasData'):
                    years[str(y)] = weather_api.month_daily_means(h['series'][attr])
            write_json(os.path.join(DATA, f'years_{code}_{mm:02d}_{attr}.json'),
                       {'month': mm, 'attribute': attr, 'unit': UNITS[attr],
                        'years': years, 'fetchedAt': time.time()})


def build_site(cities, latest_months, run):
    os.makedirs(DOCS, exist_ok=True)
    shutil.copyfile(os.path.join(BASE_DIR, 'static', 'echarts.min.js'),
                    os.path.join(DOCS, 'echarts.min.js'))
    html = open(os.path.join(BASE_DIR, 'static', 'index.html'), encoding='utf-8').read()
    if 'WX_STATIC = true' not in html:  # 源文件含 window.WX_STATIC 引用，不能用其做幂等标记
        html = html.replace(
            '<script src="/echarts.min.js"></script>',
            '<script src="echarts.min.js"></script>\n<script>window.WX_STATIC = true;</script>')
    with open(os.path.join(DOCS, 'index.html'), 'w', encoding='utf-8') as f:
        f.write(html)
    write_json(os.path.join(DATA, 'cities.json'), cities)
    valid = [m for m in latest_months if m]
    write_json(os.path.join(DATA, 'meta.json'), {
        'generatedAt': dt.datetime.now().isoformat(timespec='seconds'),
        'latestMonth': max(valid) if valid else None,
        'runDate': run,
        'cities': len(cities),
    })


def git_push(msg):
    """CI 断点续跑：每城市提交一次。push 失败不致命（下次运行会重试）。"""
    try:
        subprocess.run(['git', 'add', 'docs'], check=True, cwd=BASE_DIR)
        if subprocess.run(['git', 'diff', '--cached', '--quiet'], cwd=BASE_DIR).returncode == 0:
            return
        subprocess.run(['git', 'commit', '-m', msg], check=True, cwd=BASE_DIR)
        subprocess.run(['git', 'pull', '--rebase', '--autostash'], cwd=BASE_DIR)
        r = subprocess.run(['git', 'push'], cwd=BASE_DIR)
        if r.returncode != 0:
            log('  git push 失败（进度已在本地提交，下次运行会重新推送）')
    except subprocess.CalledProcessError as e:
        log(f'  git 操作失败：{e}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cities', default='', help='逗号分隔 admin_code，仅生成这些城市（调试用）')
    ap.add_argument('--since', default=f'{EPOCH_YEAR}-01', help='历史起始月 YYYY-MM')
    ap.add_argument('--push', action='store_true', help='每城市完成后 git commit+push（CI 断点续跑）')
    args = ap.parse_args()

    today = dt.date.today()
    cities = load_cities()
    if args.cities:
        keep = set(args.cities.split(','))
        cities = [c for c in cities if c['code'] in keep]
    months = month_range(args.since, today)
    log(f'城市 {len(cities)} 个，月份 {months[0]}..{months[-1]}（共 {len(months)} 个月）')

    weather_api.load_keys()  # 提前校验凭据（env 优先，回退 AccessKey.txt）

    run = latest_run()
    log(f'最新预测起报：{run}')
    latest_months = []
    for c in cities:
        t0 = time.time()
        n_new = 0
        for m in months:
            if gen_history(c['code'], m):
                n_new += 1
        have = gen_months_file(c['code'], months)
        latest_months.append(have[-1] if have else None)
        gen_years(c['code'], today)
        f_new = gen_forecast(c['code'], run)
        log(f"{c['name']} {c['code']}: 历史新增/重探 {n_new} 个月，"
            f"最新入库月 {have[-1] if have else '无'}，预测{'更新' if f_new else '沿用'}，"
            f"耗时 {time.time() - t0:.0f}s")
        if args.push:
            git_push(f'weather: {c["name"]} {today}')

    build_site(cities, latest_months, run)
    log('站点文件已生成 docs/')
    if args.push:
        git_push(f'weather: site meta {today}')


if __name__ == '__main__':
    main()
