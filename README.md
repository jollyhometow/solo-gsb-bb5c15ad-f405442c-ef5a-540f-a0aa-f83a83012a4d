# 文物展柜环境变点分析 API

基于 **FastAPI + SQLite + SQLAlchemy 2.0** 的展柜温湿度变点（change point）
分析服务。客户端按 **设备序号 + 时间戳** 批量上报温度、湿度读数；服务对每个
展柜独立执行**带惩罚项的确定性变点分段**，自动合并同序号重复上报，支持迟到
读数补入后的时间线重放、锚点校准与“不落库”的重放预览。

---

## 一条命令启动

```bash
docker compose up --build
```

服务在**固定端口 `8080`** 启动（首次运行自动建库，数据存于命名卷 `case-data`）：

- Swagger / OpenAPI 交互文档：<http://localhost:8080/docs>
- OpenAPI 3 描述文件：<http://localhost:8080/openapi.json>
- 健康检查：<http://localhost:8080/health>

无 Docker 环境时本地运行：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

---

## 核心行为

### 1. 确定性变点分段

对每个展柜，把温湿度先按量纲缩放、按权重合成二维段内平方误差（SSE），再求
**精确动态规划**最优分段（与一维 PELT 不做剪枝时等价）：

```
最小化   Σ 段内SSE(段) + β × 段数
```

- `β`（惩罚项）越大，分段越保守；默认 `4.0`，可全局配置或在上报/预览时覆盖。
- 结果**完全确定**：相同输入（含相同读数、锚点、惩罚项）恒得相同分段，
  代价相等时选择段数更少的切法。
- 最小段长默认 3 个读数；被锚点强制切开的小区间会自动放宽。

每段返回：`start_ts / end_ts`、样本数、温湿度均值与段内标准差、相对上一段的
`delta_temperature / delta_humidity`、缩放加权的变化幅度 `magnitude`、置信标记
`confidence`，以及是否由锚点界定（`start_anchored / end_anchored`）。

**置信标记**（段首段为 `baseline`）依据相邻两段跳变相对其局部噪声的倍数：

| 标记 | 规则 |
| --- | --- |
| `high` | 跳变 ≥ 3σ 且两段样本数均 ≥ 4，或该边界为**锚点**（人工校准的已知变点） |
| `medium` | 跳变 ≥ 1.5σ 且幅度不低于下限 |
| `low` | 不满足上述条件 |

### 2. 重复上报合并

`(device_sn, ts)` 在库内唯一。同序号同时间戳重复上报按**已有累计均值并入**：

```
new_mean = (old_mean × report_count + value) / (report_count + 1)
```

`report_count` 随之加一，合并规则可重放、结果稳定。

### 3. 迟到读数与时间线重放

读数可乱序、迟到补入。每次入库后，受影响展柜的**整条时间线重新分段**，
覆盖旧的分段快照，版本号 `version` 单调递增。预览接口还会给出
`affected_from_ts / affected_to_ts`（第一个发生变化的段起点 → 时间线末尾）。

### 4. 锚点校准

锚点是强制分段边界。登记后在容差（默认 300 秒）内对齐到最近读数；
对齐成功即成为强制边界且该变点标记为 `high`。锚点先于读数登记时保持
`aligned=false`，读数补入并重放后自动纳入。

### 5. 重放预览（绝不写库）

`POST /api/v1/replay/preview` 把**库内已有数据 + 本次待补入读数/锚点**在内存中
合并试算，返回“预览分段 + 当前分段”的差异对比。待预览读数与库内同时间戳、
或请求内多条读数时间戳重复时，同样按 `report_count` 逐条累计均值——但只在
**内存副本**上合并。整个请求只执行只读查询，不写入任何表、不推进版本号、
不产生副作用。

---

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/readings/batch` | 批量上报，自动合并重复并重放受影响展柜 |
| GET  | `/api/v1/cases` | 展柜列表（版本、读数/锚点数） |
| GET  | `/api/v1/cases/{sn}/segments` | 查询分段，支持 `start` / `end` 时间过滤 |
| POST | `/api/v1/anchors` | 登记/更新锚点并重放 |
| GET  | `/api/v1/cases/{sn}/anchors` | 查询锚点及对齐情况 |
| DELETE | `/api/v1/cases/{sn}/anchors/{ts}` | 删除锚点并重放 |
| POST | `/api/v1/replay/preview?device_sn=...` | 无写库重放预览 |
| GET  | `/health` | 健康检查 |

### 上报读数

```bash
curl -X POST http://localhost:8080/api/v1/readings/batch \
  -H 'Content-Type: application/json' \
  -d '{
    "penalty": 4.0,
    "readings": [
      {"device_sn": "CASE-007", "ts": "2026-09-01T08:00:00+08:00", "temperature": 20.1, "humidity": 55.0},
      {"device_sn": "CASE-007", "ts": "2026-09-01T08:10:00+08:00", "temperature": 20.2, "humidity": 55.1}
    ]
  }'
```

响应含每个展柜的 `inserted` / `merged`、前后版本号与本次重放分段（`replay`）。
若只想入库暂不重放，可传 `"replay": false`。

### 锚点校准

```bash
curl -X POST http://localhost:8080/api/v1/anchors \
  -H 'Content-Type: application/json' \
  -d '{"device_sn": "CASE-007", "ts": "2026-09-01T09:00:00+08:00",
       "label": "更换调湿剂", "ref_temperature": 21.0, "ref_humidity": 60.0}'
```

### 迟到读数预览（不写库）

```bash
curl -X POST 'http://localhost:8080/api/v1/replay/preview?device_sn=CASE-007' \
  -H 'Content-Type: application/json' \
  -d '{"readings": [
        {"device_sn": "CASE-007", "ts": "2026-09-01T08:30:00+08:00",
         "temperature": 24.9, "humidity": 70.2}
      ]}'
```

返回 `changed`、`would_bump_version`、受影响时间范围，以及 `preview` / `current`
两份分段；执行后再查读数数量与版本号均不会变化。

---

## 配置（环境变量）

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `CASE_PORT` | `8080` | 监听端口（镜像内与宿主映射均固定 8080） |
| `CASE_DB_URL` | `sqlite:///./data/cases.db` | SQLAlchemy 数据库 URL |
| `CASE_DATA_DIR` | `./data` | SQLite 文件目录 |
| `CASE_PENALTY` | `4.0` | 默认惩罚项 β |
| `CASE_MIN_SEGMENT_SIZE` | `3` | 最小段长（锚点小区间自动放宽） |
| `CASE_ANCHOR_TOLERANCE` | `300` | 锚点对齐容差（秒） |
| `CASE_TEMP_SCALE` / `CASE_HUMIDITY_SCALE` | `2.0` / `5.0` | 温/湿度特征缩放 |
| `CASE_TEMP_WEIGHT` / `CASE_HUMIDITY_WEIGHT` | `1.0` / `1.0` | 联合代价权重 |
| `CASE_CONF_HIGH_SIGMAS` / `CASE_CONF_MEDIUM_SIGMAS` | `3.0` / `1.5` | 高/中置信 σ 阈值 |
| `CASE_CONF_MIN_MAGNITUDE` | `0.5` | 显著变化最小幅度（缩放空间） |

---

## 测试

```bash
pip install -r requirements.txt pytest httpx
python -m pytest -q
```

测试覆盖：变点检出与置信标记、惩罚项单调性、确定性、重复合并、迟到重放、
锚点强制边界、时间范围查询以及**预览不写库**（含待预览读数与库内重复、
请求内时间戳重复时在内存副本上按 `report_count` 累计均值的回归用例）。
