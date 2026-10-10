# 黄金样本目录与**标注规范**

> 本目录用于把**真实电费账单**作为黄金样本做字段级回归比对。
> **真实账单 PDF 不入仓库**（业务资料、含客户信息），只登记其来源路径；
> 期望值以 JSON 形式入库，必须经**人工核对**后才能标记为 `confirmed`。

---

## 1. 目录结构

```
tests/data/golden_bills/
├── README.md                     ← 本文件：标注规范
├── manifest.json                 ← 样本清单：逻辑名、来源路径、账期、期望值、复核状态
└── expected/
    ├── TEMPLATE.json             ← 期望值 JSON 模板（新样本照此填写）
    └── <样本集名>.json            ← 期望值（可复用仓库已有的 dongfeng_2025_bill_facts.json）
```

* **期望值文件可以放在本目录之外**（例如仓库已有的
  `tests/data/dongfeng_2025_bill_facts.json`），只要在 `manifest.json` 里写清相对/绝对路径即可。
  **不重复造轮子**：已有 12 个月的期望值文件直接引用，不另抄一份。
* 生成的差异报告建议写到**仓库外**（如 `taiqu-storage/`），避免把中间产物入库。

## 2. 合规红线

1. **不得**把真实账单 PDF（或其中的客户名、户号、地址）提交进仓库。
2. **不得**把未经人工核对的数值标成 `confirmed`。
3. **不得**为了"让比对通过"而放宽容差或删除失败样本；容差必须能追溯到账单的有效位数。
4. 规范 §14：**未实际运行的测试不得标记通过；合成测试不得代替真实账单验收**。
5. 本机无真实账单时（`manifest.json` 中的路径不存在），测试必须 **skip 并给出中文原因**，
   **不得**伪造样本，也**不得**把"跳过"写成"通过"。

## 3. `manifest.json` 字段规范

| 字段 | 必填 | 说明 |
|---|---|---|
| `schema_version` | ✔ | 清单格式版本，当前 `"1.0"` |
| `description` | ✔ | 样本集中文说明 |
| `samples[].id` | ✔ | **唯一逻辑名**，与期望值 JSON 里的 `billing_month` 对应，命名规则见 §4 |
| `samples[].billing_month` | ✔ | 账期键，必须与期望值 JSON 的 `billing_month` **完全一致** |
| `samples[].file_name` | ✔ | 账单文件名（须与期望值 JSON 的 `file_name` 一致，否则比对报告会给出提示） |
| `samples[].source_path` | ✔ | 真实文件**绝对路径**；**只读**，不入库 |
| `samples[].source_path_alt` | ✕ | 备用路径（同名文件可能存在多份副本；优先用 `source_path`） |
| `samples[].expected_source` | ✔ | 期望值 JSON 路径（相对仓库根或绝对） |
| `samples[].expected_verified_by` | ✔ | 期望值出处（脚本/人工），如 `"人工核对（待签署）"` |
| `samples[].review_status` | ✔ | 见 §5：`confirmed` / `pending_human_review` / `unverified` |
| `samples[].notes` | ✕ | 中文备注（版式差异、已知缺口等） |

## 4. 命名规则

* `id` 采用 `"<主体>-<YYYY-MM>"`，全小写英文与短横线：`dongfeng-3rd-2025-09`。
* `billing_month` 固定 `YYYY-MM`（与账单账期的起始月一致，跨月账单取**起始月**并备注）。
* 期望值文件命名：`expected/<样本集名>.json`；样本集名用 `"<主体>_<年份>_bill_facts"`。
* 一个账期**只允许一条** `samples[]` 记录；同月多份账单请在 `notes` 里说明并以 `id` 后缀区分
  （如 `dongfeng-3rd-2025-09-sub`）。

## 5. 复核状态取值

| `review_status` | 含义 | 是否可作验收依据 |
|---|---|---|
| `confirmed` | **已人工核对并签署**（核对人/日期记录在核对清单里） | ✔ 是 |
| `pending_human_review` | 已产生逐项核对清单，**等待需求方核对** | ✘ 否 |
| `unverified` | 尚未核对（可能连提取都未做） | ✘ 否 |

> 本次交付的两份账单（2025-09 / 2025-10）状态为 **`pending_human_review`**，
> 核对清单见 `taiqu-storage/golden_candidate_9月.md` 与 `golden_candidate_10月.md`。
> 在核对人签署前，**不得**把它们当作已验收的黄金样本。

## 6. 期望值 JSON 的字段与容差写法

顶层结构（与仓库现有文件保持一致，见 `expected/TEMPLATE.json`）：

```jsonc
{
  "source": "文字说明：这些数值来自哪些账单、什么口径",
  "source_files": "真实文件的路径模式（可写目录 + 通配）",
  "extracted_by": "提取方式（脚本名 / 人工）",
  "verification": "已做过的勾稽与核对说明",
  "totals": { "energy_kwh": …, "amount_yuan": …, "weighted_price_yuan_per_kwh": … },
  "months": [
    {
      "billing_month": "2025-09",          // 必填，与 manifest 对应
      "file_name": "本三9月账单.pdf",        // 必填
      "billing_period_start": "2025-09-01", // 日期用 ISO，解析器亦归一为 date
      "billing_period_end": "2025-09-30",
      "customer_no": "4206851784045",
      "voltage_level": "110千伏",           // 允许与解析器写法不同，见 §6.3
      "energy_total_kwh": 8780498.0,
      "energy_charge_yuan": 5505532.31,
      "demand_kw": 30844.0,
      "demand_charge_yuan": 1202916.0,
      "power_factor_yuan": -9206.37,
      "bill_total_yuan": 6699241.94,
      "avg_price_declared": 0.76297,
      "period_energy_kwh": { "VALLEY": …, "FLAT": …, "PEAK": …, "SHARP_PEAK": … },
      "component_unit_price_yuan_per_kwh": { "market_energy": …, "…": … },
      "period_unit_price_yuan_per_kwh": { "VALLEY": …, "…": … }
    }
  ]
}
```

### 6.1 容差（写在代码里，不写在 JSON 里）

容差集中在 `cenep/data/bill_golden_compare.py` 的常量中，**依据是账单自身的有效位数**：

| 类别 | 常量 | 容差 | 依据 |
|---|---|---|---|
| 金额（元） | `TOL_MONEY` | 绝对 ≤ 0.01 | 账单保留两位小数 |
| 电量（kWh） | `TOL_ENERGY` | 绝对 ≤ 0.5 | 账单为整数 |
| 功率/需量（kW、kVA） | `TOL_POWER` | 绝对 ≤ 0.5 | 多为整数，9 月需量含 6 位小数 |
| 单价（元/kWh） | `TOL_PRICE` | 相对 ≤ 1e-4 | 账单 6~7 位小数 |
| 比率（功率因数等） | `TOL_RATIO` | 绝对 ≤ 1e-4 | 账单 2~4 位小数 |
| 文本 / 日期 | — | **必须完全相等** | 不允许用容差掩盖语义差异 |

判定式为 `|实际 − 期望| <= 绝对容差 + 相对容差 × |期望|`。

### 6.2 「缺失」与「零」必须分开（规范 §4 规则 1）

| 期望 | 实际 | 结果 | 说明 |
|---|---|---|---|
| `null` / 键不存在 | `None` | **通过（双方均未提供）** | 账单没写、程序也留空——**不是 0** |
| `null` / 键不存在 | 有值 | **失败（期望缺失但实际有值）** | 程序给出了账单没有的数，必须人工确认 |
| `0` | `None` | **失败（实际缺失）** | 账单明写 0，程序却留空 |
| `0` | `0` | 通过 | 真实零值 |

> 账单上写成 `-` 的单元格（如 9 月「辅助服务费用」）**不是 0**，
> 期望值里应写 `null` 并在 `notes` 里说明。

### 6.3 等价值（物理量相同、写法不同）

同一物理量在不同来源写法不同是允许的，但必须由**显式归一化函数**处理：

| 字段 | 期望写法 | 解析器写法 | 归一化 |
|---|---|---|---|
| `voltage_level` | `110千伏` | `交流110kV` / `110kV` | `normalize_voltage_kv` → `110.0`（按数值 + `TOL_POWER` 比） |

**不允许**用容差去掩盖语义差异（例如把「不考核」当成 `0.9`）。

### 6.4 派生字段

期望值里有、解析行字典里没有的字段（如各分项单价），通过 `FieldSpec.derive` **现算**，
并在报告备注里写明算式（`derive_note`），例如：

* `avg_price_declared` ← `bill_total_yuan ÷ energy_total_kwh`
* `component_unit_price_yuan_per_kwh.market_energy` ← `market_purchase_charge_yuan ÷ energy_total_kwh`

派生只用于**交叉校验**，不得反过来当作账单事实写入模型。

### 6.5 未覆盖的字段一律标「未验证」

比对规格 `FIELD_SPECS` 之外的期望值字段（例如
`period_unit_price_yuan_per_kwh`——解析行字典目前**没有**分时电价字段）
在报告中**不出现即为未验证**，**不得**写成"通过"。
确需比对时，先给 `parse_bill_pdf_full` 增加对应字段（追加式），再补 `FieldSpec`。

## 7. 已知缺口登记

真实的、已查明的解析缺陷登记在
`cenep/data/bill_golden_compare.py` 的 `DEFAULT_KNOWN_GAPS`，键为
`"<账期>:<字段键>"`（或全局 `<字段键>`），值为**可复现的原因**。

* 登记后，该字段的失败归入「已知缺口」，与「意外失败」分开统计；
* 测试只对「意外失败」报错——这样既不会掩盖缺陷，也不会让已知缺陷把回归测试刷红；
* 一旦该字段**开始通过**，报告会输出「已通过，请复核后从缺口清单移除」的提示。

## 8. 新增一个黄金样本的步骤

1. 把真实账单放到**仓库外**的只读目录（**不要**复制进仓库）。
2. 运行 `taiqu-storage/extract_golden_candidates.py` 生成逐页原文与候选值底稿。
3. 按本规范产出**人工核对清单**（字段｜提取值｜来源页码｜原始文本），交需求方核对。
4. 核对通过后，把确认值写入 `expected/<样本集名>.json`（照 `TEMPLATE.json` 结构），
   并在 `verification` 里写明核对人与日期。
5. 在 `manifest.json` 的 `samples[]` 中登记该账期，`review_status` 置为 `confirmed`。
6. 若该账期引入了新的字段口径，先在 `FIELD_SPECS` 中补规格，再跑比对。
7. 运行：`python -m pytest tests/test_v25_golden_bill_compare.py -o addopts="" -q`

## 9. 比对怎么跑

```powershell
$ws="C:\Users\Administrator\Documents\deepseek-harness\default-workspace\cenep"
$py="C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
$env:PYTHONPATH="$ws\.pylibs;$ws\src"
Set-Location $ws
& $py -m pytest tests/test_v25_golden_bill_compare.py -o addopts="" -q
```

单独出一份中文差异报告（含 字段｜期望｜实际｜绝对差｜相对差｜通过/失败）：

```powershell
& $py -c "from cenep.data.bill_golden_compare import *; \
r=compare_pdf_to_golden(r'<账单.pdf>', r'tests/data/dongfeng_2025_bill_facts.json','2025-09'); \
print(render_report_text(r))" > 报告.txt
```
