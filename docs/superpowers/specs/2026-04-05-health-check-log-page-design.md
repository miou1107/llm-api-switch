# Health Check 歷史紀錄頁

## 目標

在 dashboard 新增獨立頁面，顯示所有 provider/model 的健康檢查歷史紀錄。
主要用途：**排查問題** — 篩選失敗紀錄、找出錯誤類型、追蹤特定 provider 的狀況變化。

## 架構

| 部分 | 做法 |
|------|------|
| 前端 | `src/dashboard/health-log.html`（Alpine.js + Tailwind，與 index.html 風格一致） |
| 後端 | `GET /admin/health-checks` API，支援分頁 + 多條件篩選 |
| DB | 直接查 `health_checks` 表，已有 provider/model/timestamp index |

## 後端 API

### `GET /admin/health-checks`

**參數：**

| 參數 | 類型 | 預設 | 說明 |
|------|------|------|------|
| `page` | int | 1 | 第幾頁 |
| `per_page` | int | 50 | 每頁筆數（上限 200） |
| `provider` | str? | null | 篩選 provider_id |
| `model` | str? | null | 篩選 model_id |
| `success` | bool? | null | 篩選成功/失敗（null=全部） |
| `error_type` | str? | null | 篩選錯誤類型（timeout/rate_limit/auth/server_error/unknown） |

**回傳：**

```json
{
  "items": [
    {
      "id": 1234,
      "timestamp": "2026-04-05T15:14:26",
      "provider_id": "deepseek",
      "model_id": "deepseek-chat",
      "success": false,
      "latency_ms": 1200.5,
      "error_type": "auth",
      "quality_score": null,
      "tokens_used": null
    }
  ],
  "total": 5678,
  "page": 1,
  "per_page": 50,
  "filters": {
    "providers": ["groq", "cerebras", "gemini", "deepseek", ...],
    "models": ["llama-3.3-70b-versatile", "deepseek-chat", ...],
    "error_types": ["auth", "timeout", "rate_limit", "server_error", "unknown"]
  }
}
```

`filters` 欄位提供所有可選值，供前端下拉選單使用。

### DB Query

新增 `get_health_checks_paginated()` 到 `queries.py`：
- 動態組 WHERE 條件（只加有值的篩選）
- `ORDER BY timestamp DESC`
- `LIMIT ? OFFSET ?` 分頁
- 同時 `SELECT COUNT(*)` 取總數
- 另查 `SELECT DISTINCT provider_id / model_id / error_type` 供篩選選項

## 前端頁面

### 導航

- `index.html` header 加一個「檢查紀錄」按鈕連到 `health-log.html`
- `health-log.html` header 加「返回 Dashboard」按鈕

### 篩選列

一排水平排列：
- **Provider** 下拉選單（全部 / 各 provider）
- **Model** 下拉選單（全部 / 各 model）— 選了 provider 後自動篩選對應 model
- **狀態** toggle：全部 / 成功 / 失敗
- **錯誤類型** 下拉（僅當選「失敗」時顯示）：全部 / auth / timeout / rate_limit / server_error / unknown

### 表格

| 欄位 | 說明 |
|------|------|
| 時間 | `timestamp`，格式 `MM-DD HH:mm:ss` |
| Provider | `provider_id` |
| Model | `model_id` |
| 狀態 | 綠點/紅點 + 文字 |
| 延遲 | `latency_ms`，單位 ms |
| 錯誤類型 | `error_type`，失敗時顯示紅字標籤 |
| 品質 | `quality_score`，有值時顯示（0~1） |

### 分頁

表格下方：
- 「上一頁 / 下一頁」按鈕
- 顯示「第 X-Y 筆，共 Z 筆」
- 每頁 50 筆

### 排查用的視覺提示

- 失敗的列整行淡紅底色，方便一眼掃到
- 錯誤類型用顏色標籤：`auth`=紅、`timeout`=橘、`rate_limit`=黃、`server_error`=紫、`unknown`=灰
- 預設載入時 **不篩選** — 顯示全部紀錄，最新的在上面

## 改動清單

1. `src/db/queries.py` — 新增 `get_health_checks_paginated()`
2. `src/admin/routes.py` — 新增 `GET /admin/health-checks` endpoint
3. `src/dashboard/health-log.html` — 新頁面
4. `src/dashboard/index.html` — header 加「檢查紀錄」連結

## 不做的事

- 不做圖表/趨勢圖
- 不做匯出 CSV
- 不做即時更新（手動重整或切頁即可）
- 不做全文搜尋
