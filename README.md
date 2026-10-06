# Timeline Projection Service

录音棚乐谱提示点（cue）投影服务：把以「四分音符刻度（tick）」表示的提示点，
按速度自动化曲线精确投影到纳秒时间轴与零基采样帧，保证灯光、字幕与录音不发生
跨段累计舍入漂移。

- 纯 Python 3.11 标准库实现，运行时零第三方依赖。
- 全部累计时长使用 `fractions.Fraction` 精确计算，**只在最后一步**对纳秒时刻和
  采样帧各自按「四舍六入、五取偶」（round-half-to-even）取整一次。
- 同刻度提示点必然得到相同投影；响应严格保持提示点的原始顺序。

## 接口

`POST /api/timelines/project`

```json
{
  "ticks_per_quarter": 480,
  "sample_rate": 48000,
  "tempo_points": [
    {"tick": 0,    "microseconds_per_quarter": 500000, "mode": "constant"},
    {"tick": 1920, "microseconds_per_quarter": 300000, "mode": "linear"},
    {"tick": 3840, "microseconds_per_quarter": 200000, "mode": "constant"}
  ],
  "cues": [
    {"id": "light-01",    "tick": 0},
    {"id": "subtitle-07", "tick": 2400}
  ]
}
```

字段约束：

| 字段 | 约束 |
| --- | --- |
| `ticks_per_quarter` | 正整数 |
| `sample_rate` | 正整数（Hz） |
| `tempo_points` | 1–500 个；`tick` 必须从 0 开始且严格递增；`microseconds_per_quarter` 为正整数（微秒/四分音符）；`mode` 为 `constant` 或 `linear` |
| `cues` | 1–2000 个；`id` 为非空字符串或整数且全请求唯一；`tick` 为非负整数 |

语义：

- `constant`：该点之后区间速度恒定。
- `linear`：该点到下一点之间，微秒/四分音符随 tick 线性变化（区间时长按
  梯形面积 `(v_a + v_b) / 2 × Δtick / tpq` 精确积分）。
- 最后一个速度点之后永远保持其速度，因此提示点无上界。

成功响应（200），保持 `cues` 原顺序：

```json
{
  "ticks_per_quarter": 480,
  "cues": [
    {"id": "light-01",    "tick": 0,    "time_nanoseconds": 0,         "sample_frame": 0},
    {"id": "subtitle-07", "tick": 2400, "time_nanoseconds": 2362500000, "sample_frame": 113400}
  ]
}
```

- `time_nanoseconds = round_half_even(累计微秒 × 1000)`
- `sample_frame = round_half_even(累计微秒 × sample_rate / 1_000_000)`（零基帧）

健康检查：`GET /health` → `200 {"status":"ok"}`（同时提供 `/healthz`、`/ready`）。

## 错误处理

任何字段级或结构级错误都使**整个请求失败（HTTP 400），响应绝不夹带部分投影
结果**。错误码为稳定字符串，并通过 `path` 给出对应位置（JSON Pointer 风格）：

```json
{
  "error": {
    "code": "VALIDATION_FAILED",
    "message": "request validation failed; no projections were produced",
    "errors": [
      {"code": "TEMPO_GAP",          "path": "/tempo_points/0/tick",                           "message": "..."},
      {"code": "DUPLICATE_TEMPO_TICK","path": "/tempo_points/2/tick",                          "message": "..."},
      {"code": "TEMPO_NOT_ORDERED",  "path": "/tempo_points/3/tick",                           "message": "..."},
      {"code": "NON_POSITIVE_TEMPO", "path": "/tempo_points/1/microseconds_per_quarter",       "message": "..."},
      {"code": "INVALID_MODE",       "path": "/tempo_points/1/mode",                           "message": "..."},
      {"code": "CUE_OUT_OF_RANGE",   "path": "/cues/4/tick",                                   "message": "..."},
      {"code": "DUPLICATE_CUE_ID",   "path": "/cues/9/id",                                     "message": "... /cues/2 ..."}
    ]
  }
}
```

其他错误码：`MISSING_FIELD`、`INVALID_TYPE`（JSON 布尔值不接受为整数）、
`INVALID_COUNT`、`UNKNOWN_FIELD`，以及传输层的 `MALFORMED_JSON`、
`EMPTY_BODY`、`BODY_TOO_LARGE`、`NOT_FOUND`。同一请求产生的错误集合与顺序是
确定性的，可用于稳定比对。

## 本地运行（无需 Docker）

```bash
python3 -m app.server            # 默认 0.0.0.0:8080，可用 PORT / HOST 覆盖
python3 -m unittest discover -s tests -t .
```

## Docker 与一键校验

```bash
# 宿主机端口可配置（默认 8080）
HOST_PORT=9090 docker compose up -d app

# 一次性 verify 服务：等待 app 健康后，依次执行
# 构建（字节码编译）、全部单元测试、恒定段/渐变段/错误请求 API 冒烟，
# 随后自行退出，退出码即结果（0 成功）。
docker compose up --abort-on-container-exit --exit-code-from verify verify
```

Compose 服务：

- `app`：长驻 API 服务，带容器级 HEALTHCHECK，发布 `${HOST_PORT:-8080}:8080`。
- `verify`：依赖 `app` 的 `service_healthy` 条件，`restart: "no"`，结束即退出。
