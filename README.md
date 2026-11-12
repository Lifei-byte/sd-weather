# 山东省城市气象数据平台

城市级气象历史（ERA5）+ 未来 45 天预测展示页，供客户浏览器访问。

## 启动

```bash
cd /山东/气象页面
.venv/bin/python serve.py          # 默认端口 8020，可加参数改端口
# 后台运行：
nohup .venv/bin/python serve.py 8020 >> serve.log 2>&1 &
```

浏览器访问 `http://<机器IP>:8020`（或双击项目目录里的 `打开气象页面.url` 快捷方式）。
**不要直接双击 static/index.html**——file:// 方式打开的页面没有数据服务，必然报 Failed to fetch。
本机验证：`curl localhost:8020/api/cities`。

## 页面内容（4 个视图）

| 视图 | 内容 | 数据源 |
|---|---|---|
| 历史逐小时 | 城市+月份+要素（温度/风速/辐照/湿度）逐小时曲线 | ERA5 再分析 |
| 45天预测 | 逐小时预报曲线 + 要点瓦片（起报/明日温度/7日风速/7日辐照） | 每日 12:00 UTC 起报 |
| 预测日报表 | 45 天按日聚合：最高/最低温、平均/最大风速、日平均辐照、平均湿度 | 同上 |
| 同月多年对比 | 指定月份各年份逐日均值叠画 | ERA5 |

## 数据口径（已实测确认）

- 时间轴一律为**北京时间**；ERA5 数组首值 = 当月 1 日 00:00
- 要素单位：TMP=℃，WS=m/s，RH=%，SSRD=**W/m²**（逐时平均辐照度，数值上等于该小时累计辐照 Wh/m²）
- 日平均辐照 = 当日 24 个逐时辐照度平均值（W/m²，山东 11 月晴日约 100~200）；**页面辐照一律用平均值、不做累计**（2026-11-11 起口径，不再展示 kWh/m² 日总量）
- ERA5 数据范围：**2024-01 起**；入库滞后 1~2 个月（如 2026-11 时最新为 2026-09），未入库月份返回空数组，页面提示"暂未入库"
- 预测每日只有 12:00 UTC（北京 20:00）一个起报时次，共 1081 个逐小时点（45 天+1h）；当日起报未发布时自动回退前一天

## 结构

```
气象页面/
├── serve.py          # 本机代理服务（stdlib http.server，无 Flask 依赖）
├── weather_api.py    # EnOS Poseidon 签名客户端 + 磁盘缓存 + 限速退避
├── static/           # index.html + echarts.min.js（本地化，不走 CDN）
├── cache/            # 上游数据缓存（历史永久；未入库24h重取；起报映射6h）
├── .venv/            # pycryptodome（签名必需）
├── area_info.csv     # 城市清单（接口方提供）
└── AccessKey.txt     # 凭据（仅服务端读取，静态目录不含此文件）
```

## 接口（供排障）

```
GET /api/cities                          16 市列表
GET /api/months?adminCode=               可选月份范围（自动探测最新入库月）
GET /api/history?adminCode=&month=2026-09
GET /api/forecast?adminCode=
GET /api/forecast_daily?adminCode=
GET /api/history_years?adminCode=&month=11&attr=TMP
```

## 注意事项

- 上游网关限流激进（连续约 15 次调用触发 403，数分钟恢复）。服务端已做：
  调用间隔 ≥1s、403/429/5xx 指数退避重试、全量磁盘缓存。**正常浏览不会触发**；
  若手动狂刷多个城市可能短暂 502，稍候即可。
- 官方 poseidon Python SDK 有缺陷（返回顶层 JSON 数组时抛 TypeError），
  本项目在 weather_api.py 内复刻了其签名算法，不依赖该 SDK 的 urlopen。
- 清理缓存：删除 `cache/` 下对应文件即可，下次访问重新取数。
- 如更换凭据：改 `AccessKey.txt` 后重启服务（token 每请求现签，但缓存不变）。
