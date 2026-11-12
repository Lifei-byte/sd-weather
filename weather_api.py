#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""远景 EnOS 城市级气象接口客户端。

- 签名复刻自官方 poseidon SDK（poseidon.py 三个内部函数），因官方 urlopen
  在返回顶层 JSON 数组时会抛 TypeError，故自行封装请求。
- 限流保护：全局锁 + 调用间隔 >=1s；403/429/500/503 退避重试（该网关限流激进，
  连续约15次调用即 403）。
- 磁盘缓存：ERA5 历史数据不可变（入库即最终值），全 NaN（未入库）24h 后重取；
  预测按起报时次永久缓存，"最新起报"映射缓存 6h。
"""
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from Crypto.Cipher import PKCS1_OAEP
from Crypto.PublicKey import RSA

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, 'cache')
BASE = 'https://ag-cn5.envisioniot.com'

# ---- poseidon SDK 内置常量（取自官方 poseidon.py 源码）----
_SIGN_SK = 'knoczslufdasvhbivbewnrvuywachsrawqdpzesccknrhhetgmrcwfqfudywbeon'
_PUBKEY_B64 = (b'MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAokFjy0wLMKH0/39hxPN6JYRk'
               b'MDXzvVIGQh55Keo2LIsP/jRU/yZHT/Vkg34yU9koNjSaacPvooXEoI5eFGuRrsBMro'
               b'tZ5xfejCrTbGvZqjhnMPBheDmxfflIZzRrF/zoQvF0nIbmGNkxEfROHtDDkgNuGRd'
               b'thXrNavCgfM2z3LNF83UT9CGpxJWBeKK3pXfYLsQ4f8uyrQRcy2BhKfJ/PKai1moc'
               b'XYqr07JfQ0XZM4xIzuQ7E4ybNk5IFreDuuhF63wXAi1uonGzqjEYcbC1xT2boNiZO'
               b'RoOQWpAHhSbIRpljmW/uHBvoKZ573PQbbxE62hXv1Z1iVky0dtAV65dXwIDAQAB')
_EXPIRY_MS = 2 * 60 * 60 * 1000
_REFRESH_MS = 30 * 24 * 60 * 60 * 1000

ATTRIBUTES = ('TMP', 'WS', 'SSRD', 'RH')

_lock = threading.Lock()
_last_call = [0.0]  # 上次上游调用的 epoch 秒

NAN_RETRY_AGE = 24 * 3600      # 全 NaN 历史缓存 24h 后允许重取
LATEST_RUN_AGE = 6 * 3600      # 最新起报映射缓存 6h


def load_keys():
    """优先环境变量 ENOS_AK/ENOS_SK（CI Secrets 注入），本地回退 AccessKey.txt。"""
    ak = os.environ.get('ENOS_AK')
    sk = os.environ.get('ENOS_SK')
    if ak and sk:
        return ak, sk
    ak = sk = None
    with open(os.path.join(BASE_DIR, 'AccessKey.txt'), encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            low = line.lower()
            if low.startswith('accesskey'):
                ak = line.split('：')[-1].split(':')[-1].strip()
            elif low.startswith('secretkey'):
                sk = line.split('：')[-1].split(':')[-1].strip()
    if not ak or not sk:
        raise RuntimeError('AccessKey.txt 缺少 AccessKey/SecretKey')
    return ak, sk


def _derive_key(ak, sk):
    return str(int(ak.split('-')[1] + sk.split('-')[1], 16))[:6]


def _hmac_sig(key, offset_ms):
    msg = key + str(int(round(time.time() * 1000)) + offset_ms)
    return hmac.new(_SIGN_SK.encode('utf-8'), msg.encode('utf-8'), hashlib.sha256).hexdigest()


def build_token(ak, sk):
    key = _derive_key(ak, sk)
    plain = '$'.join([key, ak, sk, _hmac_sig(key, _EXPIRY_MS), _hmac_sig(key, _REFRESH_MS)])
    cipher = PKCS1_OAEP.new(RSA.importKey(base64.b64decode(_PUBKEY_B64)))
    return base64.b64encode(cipher.encrypt(plain.encode('utf-8'))).decode()


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(f'HTTP {code}: {message}')
        self.code = code


def _raw_get(path, params, tries=4):
    """带限速与退避的签名 GET。404 直接抛 ApiError(404) 由调用方按业务处理。"""
    ak, sk = load_keys()
    url = f'{BASE}{path}?{urllib.parse.urlencode(params)}'
    last_err = None
    for i in range(tries):
        with _lock:
            gap = time.time() - _last_call[0]
            if gap < 1.0:
                time.sleep(1.0 - gap)
            _last_call[0] = time.time()
        try:
            req = urllib.request.Request(url)
            req.add_header('apim-accesstoken', build_token(ak, sk))
            req.add_header('Content-Type', 'application/json;charset=utf-8')
            req.add_header('User-Agent', 'Python_enos_api')
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode('UTF-8')
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 404:
                raise ApiError(404, '该时次/参数无数据')
            if e.code in (403, 429, 500, 503) and i < tries - 1:
                time.sleep(20 * (i + 1))
                continue
            raise ApiError(e.code, str(e))
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            if i < tries - 1:
                time.sleep(10 * (i + 1))
                continue
            raise ApiError(0, str(e))
    raise ApiError(getattr(last_err, 'code', 0), str(last_err))


def _parse_values(body):
    """返回体为 JSON 数组，元素可能是数字或字符串（含 "NaN"）。"""
    arr = json.loads(body)
    if not isinstance(arr, list):
        raise ApiError(0, f'返回体非数组: {body[:200]}')
    out = []
    for x in arr:
        try:
            v = float(x)
            out.append(None if v != v else v)  # NaN -> None
        except (TypeError, ValueError):
            out.append(None)
    return out


def _cache_path(kind, code, key, attr):
    return os.path.join(CACHE_DIR, f'{kind}_{code}_{key}_{attr}.json')


def _read_cache(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_cache(path, payload):
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)


def _month_days(yyyymm):
    y, m = int(yyyymm[:4]), int(yyyymm[5:7])
    if m == 12:
        nxt = dt.date(y + 1, 1, 1)
    else:
        nxt = dt.date(y, m + 1, 1)
    return (nxt - dt.date(y, m, 1)).days


def fetch_era5(code, yyyymm, attr):
    """ERA5 历史，按月。返回 {"times":[ISO+08...], "values":[...], "hasData":bool}。"""
    path = _cache_path('history', code, yyyymm, attr)
    cached = _read_cache(path)
    if cached:
        # 有真实数据的历史月永久有效；全空（未入库）超过24h允许重取
        if cached.get('hasData') or time.time() - cached.get('fetchedAt', 0) < NAN_RETRY_AGE:
            return cached
    try:
        body = _raw_get('/weather-service/v1/api/city/era5/data',
                        {'adminCode': code, 'yearMonth': yyyymm, 'attribute': attr})
    except ApiError:
        if cached:  # 上游故障时回退旧缓存
            return cached
        raise
    values = _parse_values(body)
    days = _month_days(yyyymm)
    expect = days * 24
    # 接口按当月天数返回逐小时值；时间轴为北京时间（首值=当月1日 00:00）
    start = dt.datetime(int(yyyymm[:4]), int(yyyymm[5:7]), 1)
    times = [(start + dt.timedelta(hours=i)).isoformat() for i in range(min(len(values), expect))]
    values = values[:expect]
    payload = {
        'adminCode': code, 'yearMonth': yyyymm, 'attribute': attr,
        'times': times, 'values': values,
        'hasData': any(v is not None for v in values),
        'fetchedAt': time.time(),
    }
    _write_cache(path, payload)
    return payload


def _latest_run(code):
    """探测最新可用起报日（12Z 时次），今天→昨天→前天。缓存6h。"""
    cpath = os.path.join(CACHE_DIR, f'latest_run_{code}.json')
    cached = _read_cache(cpath)
    if cached and time.time() - cached.get('fetchedAt', 0) < LATEST_RUN_AGE:
        return cached['runDate']
    today = dt.date.today()
    for back in range(0, 4):
        d = today - dt.timedelta(days=back)
        # 用 TMP 探测该起报是否存在；404 则前一天
        try:
            fetch_forecast(code, d.isoformat(), 'TMP', probe=True)
            _write_cache(cpath, {'runDate': d.isoformat(), 'fetchedAt': time.time()})
            return d.isoformat()
        except ApiError as e:
            if e.code == 404:
                continue
            raise
    raise ApiError(404, '近4天无可用预测起报')


def fetch_forecast(code, run_date=None, attr='TMP', probe=False):
    """45天预测。run_date 形如 2026-11-10（对应 12Z 起报）；不传则取最新起报。"""
    if run_date is None:
        run_date = _latest_run(code)
        if probe:  # 探测路径只验证存在性
            return None
    path = _cache_path('forecast', code, run_date, attr)
    cached = _read_cache(path)
    if cached:
        return cached
    body = _raw_get('/weather-service/v1/api/city/forecast/data',
                    {'adminCode': code, 'startTime': f'{run_date}T12:00:00Z', 'attribute': attr})
    values = _parse_values(body)
    # 起报时次为 UTC 12:00（北京时间 20:00），逐小时
    start = dt.datetime.fromisoformat(run_date) + dt.timedelta(hours=12 + 8)  # 北京 20:00
    times = [(start + dt.timedelta(hours=i)).isoformat() for i in range(len(values))]
    payload = {
        'adminCode': code, 'runDate': run_date, 'attribute': attr,
        'times': times, 'values': values,
        'hasData': any(v is not None for v in values),
        'fetchedAt': time.time(),
    }
    _write_cache(path, payload)
    return payload


def daily_aggregate(fc):
    """预测逐小时 -> 按自然日聚合（北京时间）。

    SSRD 单位为 W/m²（逐时平均辐照度，2026-11-11 实测正午峰值~800 确认），
    日平均辐照度 = 当日有效小时值平均（不累计，页面统一 W/m² 口径）。
    """
    days = {}
    for t, v in zip(fc['times'], fc['values']):
        d = t[:10]
        days.setdefault(d, []).append(v)
    rows = []
    for d in sorted(days):
        vs = [x for x in days[d] if x is not None]
        if not vs:
            rows.append({'date': d, 'avg': None, 'min': None, 'max': None})
            continue
        rows.append({'date': d,
                     'avg': round(sum(vs) / len(vs), 2),
                     'min': round(min(vs), 2),
                     'max': round(max(vs), 2)})
    return rows


def month_daily_means(hist):
    """ERA5 逐小时 -> 当月逐日均值（多年对比用）。"""
    days = {}
    for t, v in zip(hist['times'], hist['values']):
        days.setdefault(int(t[8:10]), []).append(v)
    out = {}
    for d, vs in days.items():
        real = [x for x in vs if x is not None]
        out[d] = round(sum(real) / len(real), 2) if real else None
    return out
