#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""山东16市气象历史+预测展示页 — 本机代理服务。

前端静态页 + /api JSON 接口。AK/SK 只在本进程内存与 AccessKey.txt 中，
静态目录仅 static/，凭据文件不对网开放。

启动:  .venv/bin/python serve.py [端口]    默认 8020
"""
import csv
import datetime as dt
import json
import os
import re
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import weather_api
from weather_api import ApiError, ATTRIBUTES

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, 'static')
LOG = os.path.join(BASE_DIR, 'serve.log')
DEFAULT_PORT = 8020
EPOCH_YEAR = 2024  # ERA5 实测最早有数年份（2024-01 起）

UNITS = {'TMP': '°C', 'WS': 'm/s', 'SSRD': 'W/m²', 'RH': '%'}


def log(msg):
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    try:
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass


def load_cities():
    """area_info.csv 过滤山东省(370000)。"""
    cities = []
    with open(os.path.join(BASE_DIR, 'area_info.csv'), encoding='utf-8') as f:
        for row in csv.DictReader(f):
            if row['province_code'] == '370000':
                cities.append({'code': row['admin_code'], 'name': row['city_name']})
    return cities


CITIES = load_cities()


def latest_month(code):
    """探测最新入库月（缓存24h）。从当前月起向前找第一个 hasData 的月。"""
    cpath = os.path.join(weather_api.CACHE_DIR, f'months_{code}.json')
    try:
        with open(cpath, encoding='utf-8') as f:
            cached = json.load(f)
        if time.time() - cached.get('fetchedAt', 0) < 24 * 3600:
            return cached['latest']
    except (OSError, ValueError):
        pass
    today = dt.date.today()
    y, m = today.year, today.month
    latest = None
    for _ in range(6):
        ym = f'{y}-{m:02d}'
        h = weather_api.fetch_era5(code, ym, 'TMP')
        if h['hasData']:
            latest = ym
            break
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    if latest is None:
        latest = '2026-09'
    weather_api._write_cache(cpath, {'latest': latest, 'fetchedAt': time.time()})
    return latest


def month_list(code):
    """可选月份列表：2024-01 .. 最新入库月。"""
    lm = latest_month(code)
    y, m = int(lm[:4]), int(lm[5:7])
    out = []
    cy, cm = EPOCH_YEAR, 1
    while (cy, cm) <= (y, m):
        out.append(f'{cy}-{cm:02d}')
        cm += 1
        if cm == 13:
            cy, cm = cy + 1, 1
    return {'latest': lm, 'months': out}


def api_history(code, month):
    """某月4要素逐小时。TMP 全空则认为当月未入库，不再取其余要素省调用。"""
    h = weather_api.fetch_era5(code, month, 'TMP')
    if not h['hasData']:
        return {'month': month, 'hasData': False, 'series': {}}
    series = {'TMP': h}
    for attr in ('WS', 'SSRD', 'RH'):
        series[attr] = weather_api.fetch_era5(code, month, attr)
    return {'month': month, 'hasData': True, 'units': UNITS, 'series': series}


def api_forecast(code):
    """最新起报4要素逐小时。"""
    run = weather_api._latest_run(code)
    series = {}
    for attr in ATTRIBUTES:
        series[attr] = weather_api.fetch_forecast(code, run, attr)
    return {'runDate': run, 'hasData': True, 'units': UNITS, 'series': series}


def api_forecast_daily(code):
    """45天按日聚合：TMP高/低、WS均/最大、SSRD日均辐照、RH均。"""
    run = weather_api._latest_run(code)
    agg = {attr: weather_api.daily_aggregate(weather_api.fetch_forecast(code, run, attr))
           for attr in ATTRIBUTES}
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
    return {'runDate': run, 'rows': [by_date[d] for d in sorted(by_date)]}


def api_history_years(code, month, attr):
    """同月多年日均值对比。month=1..12。"""
    if attr not in ATTRIBUTES:
        attr = 'TMP'
    mm = int(month)
    today = dt.date.today()
    years = {}
    for y in range(EPOCH_YEAR, today.year + 1):
        if y == today.year and mm >= today.month:
            continue
        ym = f'{y}-{mm:02d}'
        h = weather_api.fetch_era5(code, ym, attr)
        if h['hasData']:
            years[str(y)] = weather_api.month_daily_means(h)
    return {'month': mm, 'attribute': attr, 'unit': UNITS[attr], 'years': years}


class Handler(BaseHTTPRequestHandler):
    server_version = 'sd-weather/1.0'

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, mime):
        try:
            with open(path, 'rb') as f:
                blob = f.read()
        except OSError:
            self._json(404, {'code': 404, 'message': 'not found'})
            return
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def do_GET(self):
        t0 = time.time()
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ('/', '/index.html'):
                self._file(os.path.join(STATIC_DIR, 'index.html'), 'text/html; charset=utf-8')
            elif u.path == '/echarts.min.js':
                self._file(os.path.join(STATIC_DIR, 'echarts.min.js'), 'application/javascript')
            elif u.path == '/api/cities':
                self._json(200, CITIES)
            elif u.path == '/api/months':
                code = q.get('adminCode', ['370100'])[0]
                self._json(200, month_list(code))
            elif u.path == '/api/history':
                code = q.get('adminCode', ['370100'])[0]
                month = q.get('month', [''])[0]
                if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', month):
                    self._json(400, {'code': 400, 'message': 'month 需形如 2026-09'})
                    return
                self._json(200, api_history(code, month))
            elif u.path == '/api/forecast':
                code = q.get('adminCode', ['370100'])[0]
                self._json(200, api_forecast(code))
            elif u.path == '/api/forecast_daily':
                code = q.get('adminCode', ['370100'])[0]
                self._json(200, api_forecast_daily(code))
            elif u.path == '/api/history_years':
                code = q.get('adminCode', ['370100'])[0]
                try:
                    month = int(q.get('month', ['1'])[0])
                    assert 1 <= month <= 12
                except (ValueError, AssertionError):
                    self._json(400, {'code': 400, 'message': 'month 需为 1-12'})
                    return
                attr = q.get('attr', ['TMP'])[0]
                self._json(200, api_history_years(code, month, attr))
            else:
                self._json(404, {'code': 404, 'message': 'not found'})
        except ApiError as e:
            log(f'API ERR {u.path} {e.code} {e}')
            self._json(502, {'code': e.code, 'message': f'上游接口错误: {e}'})
        except Exception as e:
            log(f'ERR {u.path} {e}\n{traceback.format_exc()}')
            self._json(500, {'code': 500, 'message': str(e)})
        finally:
            log(f'{u.path}?{u.query} {time.time() - t0:.1f}s')

    def log_message(self, fmt, *args):
        pass  # 静默默认访问日志，统一走 serve.log


if __name__ == '__main__':
    port = int(os.sys.argv[1]) if len(os.sys.argv) > 1 else DEFAULT_PORT
    os.makedirs(weather_api.CACHE_DIR, exist_ok=True)
    srv = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    log(f'气象页面服务启动: http://0.0.0.0:{port}  城市数={len(CITIES)}')
    srv.serve_forever()
