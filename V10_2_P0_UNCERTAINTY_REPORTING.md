# V10.2 P0 賽前不確定性報告層

## 目的

P0 在 `predict.py` 完成既有 LightGBM＋CatBoost 集成、校準與場內機率正規化後，透過 `race_risk_guidance.py` 產生**不確定性診斷欄位**。它只補充 JSON、篩選 Markdown 和 WhatsApp 預覽文字；不修改任何 `predicted_win_probability`、模型排序、EV、Kelly 或既有熱門／冷門篩選規則。

> `top2_gap < 0.01` 代表首二模型機率相差不足 **1 個百分點**。這是場內排序缺乏足夠分離度的研究提示，不是反向選馬規則，也不是事後校準或投注指令。

## JSON 契約

`prediction.json` 的 `race_guidance.uncertainty` 會包含以下欄位：

| 欄位 | 定義 | 用途 |
|---|---|---|
| `status` | `available` 或 `unavailable`。 | 只有完整場內機率向量才可解讀。 |
| `probability_sum` | 儲存的 Win 機率總和。 | 必須為 `1 ± 1e-6`。 |
| `top1_*`／`top2_*` | 首二馬名與機率。 | 呈現排序分離度。 |
| `top2_gap` | `p_top1 - p_top2`。 | 嚴格小於 `0.01` 才觸發低分離度。 |
| `normalized_entropy` | `-Σp log(p) / log(field_size)`。 | 描述整體機率分散程度；目前不設未校準的自動門檻。 |
| `ensemble_disagreement_top1` | 各模型 component 暫時正規化後，在集成首選的絕對差。 | 只作模型分歧審計，不反饋訓練。 |
| `label` | 觸發時的繁中提示。 | 說明場內不適合作單膽。 |

若機率缺失、非有限、超出 `[0, 1]` 或場內總和不守恆，輸出 `status="unavailable"` 和原因碼；系統不會自行重正規化、猜測或強行生成提示。

## 報告呈現

`filter_high_probability.py` 的 Markdown／WhatsApp 預覽會顯示首二機率、首二差距、正規化熵、可用時的集成分歧，以及低分離度標籤。當 P0 與既有高爆冷風險同時觸發時，兩個提示會並列，並保留原有賽事結構提示。

```json
{
  "top2_gap": 0.0064,
  "top2_gap_percentage_points": 0.64,
  "low_separation_warning": true,
  "label": "⚠️【低分離度】首二模型勝率只差 0.64 個百分點；場內排序缺乏足夠分離，不適合作單膽。"
}
```

## 驗證

```bash
cd /home/ubuntu/hkjc_v10_database
python3 verify_race_uncertainty_reporting.py
```

契約測試會驗證：低分離度觸發、嚴格的 `<1pp` 門檻、正規化熵、可用 component 分歧、無效機率向量安全降級、JSON／Markdown 渲染，以及呼叫前後輸入預測機率完全不變。

## 限制與後續校準

P0 不是模型重新訓練或 calibration 修改。`normalized_entropy` 和 `ensemble_disagreement_top1` 目前只保留作報告與後續走步驗證切片，沒有未經驗證的自動紅線。任何將 P0 訊號影響模型分數或排序的計畫，必須依 `V10_2_TAIL_RANK_AND_LOW_SEPARATION_OPTIMIZATION_PLAN.md` 進行至少三個時間外 fold、100 場以上的預先登記驗證。
