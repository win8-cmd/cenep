# 湖北政策模型（HUBEI_POLICY_MODEL）

> **文档性质**：`cenep`（工商业新能源项目经济评价软件 V1）的**省级政策模型设计文件**，
> 以湖北省为首个（也是 V1 唯一规划中的）政策模板示例，说明政策的建模方式、字段结构、版本机制与披露要求。
>
> **状态快照日期**：2026-10-06
> **当前状态**：**Phase 8 已实现（快照进行中）**。实际实现文件位于 `src/cenep/policy/`：
> `template.py`（`PolicyTemplate`，用 `None` 表达"未填写"，与用户填写 `0` 严格区分）、
> `hubei.py`（`HUBEI_TEMPLATE` 占位模板 + `describe_startup_notice()` 启动提示）、
> `store.py`（模板存取）。测试见 `tests/test_policy.py`（⬜ 待测试全绿）。
> **本文档为实现依据与现状记录**：其中的字段名与 `src/cenep/domain/models.py` 的 `PolicyProfile` **完全一致**，
> 结构化占位示例中的数值字段**一律为 `null` 或 `0`**，**禁止预填具体数值**。
>
> 本文件**不包含**任何具体电价数值、机制电量数值或补贴金额——所有数值必须由用户
> 按项目所在地**现行**政策填写（规范 §34、§89、§90、§159）。

---

## 1. 设计原则

### 1.1 六条硬性原则（规范 §34–§36、§89、§90）

| 序号 | 原则 | 含义 | 软件中的体现 |
|---|---|---|---|
| 1 | **政策不硬编码** | 计算引擎里不得出现任何电价、补贴、机制电量常量 | `calculation/revenue.py` 模块头明确：不得在代码中硬编码任何一个市场价格，所有价格均来自参数或政策 Profile |
| 2 | **政策独立建模** | 政策是独立的数据对象，不是散落在各处的字段 | `PolicyProfile` 定义于 `src/cenep/domain/models.py`，`Project.policy: PolicyProfile \| None` |
| 3 | **支持版本化** | 同一政策可有多个版本，版本之间可追溯、可比较 | `policy_id` + `policy_version` + `effective_date` + `expiry_date` |
| 4 | **旧版本只新增、不覆盖** | 新版本作为**新记录**加入，历史记录保留，正在使用的项目仍指向原版本 | 模板库按 `policy_id` 聚合多条记录；版本升级不改写旧记录 |
| 5 | **每个参数带出处** | 参数值必须能回答"这个数字从哪来" | `source`、`source_url`、`notes`；进入 `ParameterRegistry` 时 `source_type = SourceType.POLICY` |
| 6 | **不确定的必须标假设** | 无法核实的政策参数不得伪装成事实 | `ParameterMeta.is_assumption = True`，界面黄色标记（规范 §144） |

### 1.2 政策优先级的处理（规范 §85）

当同一个电价既可能来自政策、又可能来自合同或用户输入时，按下表处理：

| 来源 | 优先级数值（`SOURCE_PRIORITY`） | 是否可被用户覆盖 |
|---|---|---|
| 合同参数 `CONTRACT` | 5 | 可以（需记录覆盖原因） |
| 政策参数 `POLICY` | 4 | 可以（需记录覆盖原因） |
| 历史数据 `HISTORICAL` / 假设值 `ASSUMPTION` | 3 | 可以 |
| 用户输入 `USER_INPUT` | 2 | — |
| 行业经验 `EXPERIENCE` | 1 | 可以 |
| 系统默认 `SYSTEM_DEFAULT` | 0 | 可以 |

> `tariff.avoided_price_override` 与 `tariff.charge_price_override` 即为"用户覆盖政策/推导结果"的显式入口，
> 一旦填写，`TariffResolution.basis` 会追加"替代电价已被用户覆盖"（规范 §85）。

### 1.3 政策版本化的三条铁律

```text
铁律一：政策模型只描述"机制与出处"，不描述"某个具体数值"
        ↓
铁律二：新政策 = 新记录（新 policy_version + effective_date），旧记录永不被改写
        ↓
铁律三：每个项目文件（.nep）保存它计算时采用的 policy 快照；
        政策库更新后，已有项目的结果不自动改变，除非用户显式升级政策版本
```

---

## 2. `PolicyProfile` 字段表（与代码完全一致）

来源：`src/cenep/domain/models.py` 中的 `class PolicyProfile(_Model)`。
以下字段名**逐字取自代码**，实现时不得改名。

| 字段名 | 类型 | 默认值 | 中文标签 | 单位 | 约束 | 说明与用途 |
|---|---|---|---|---|---|---|
| `policy_id` | `str` | `""` | 政策标识 | — | 无 | 政策的稳定标识，同一政策的不同版本**共用**同一个 `policy_id`（例：`"HUBEI_NEW_ENERGY_TARIFF"`） |
| `policy_name` | `str` | `""` | 政策名称 | — | 无 | 展示用名称；报告显示"本测算采用 XX 政策版本" |
| `policy_version` | `str` | `""` | 政策版本 | — | 无 | 版本号/版本日期字符串，与 `effective_date` 配合使用 |
| `effective_date` | `date \| None` | `None` | 生效日期 | — | 可空 | 该版本开始生效的日期 |
| `expiry_date` | `date \| None` | `None` | 失效日期 | — | 可空 | 该版本失效日期；未标注时显示"未标注" |
| `province` | `str` | `""` | 适用范围（省份） | — | 无 | 湖北模板填 `"湖北"`；用于模板筛选与"是否为项目所在地政策"的校验提示 |
| `pricing_mechanism` | `str` | `""` | 价格机制说明 | — | 无 | 自由文本，描述该政策的定价方式（市场化交易/机制电价/分时电价等），**不得写入具体数值** |
| `market_price` | `float` | `0.0` | 市场电价 | 元/kWh | `ge=0` | 市场化交易形成的电价；湖北模板中**必须为 `0`，由用户填写** |
| `mechanism_price` | `float` | `0.0` | 机制电价 | 元/kWh | `ge=0` | 机制电价（保障性/机制电量对应的电价） |
| `mechanism_volume_ratio` | `float` | `0.0` | 机制电量比例 | 小数（0–1） | `ge=0, le=1` | 机制电量占全部上网电量的比例；内部统一用小数 |
| `green_energy_price` | `float` | `0.0` | 绿电价格 | 元/kWh | `ge=0` | 绿电交易价格 |
| `green_environmental_value` | `float` | `0.0` | 绿色环境价值 | 元/kWh | `ge=0` | 绿色环境权益（绿证等）折算的环境价值 |
| `source` | `str` | `""` | 来源 | — | 无 | 政策文件名称/文号（如"湖北省深化新能源上网电价市场化改革促进新能源高质量发展实施方案"，见第 6 节） |
| `source_url` | `str` | `""` | 来源链接 | — | 无 | 官方发布页面 URL；**不得编造**，未核实则留空并标 ⚠️待核实 |
| `notes` | `str` | `""` | 备注 | — | 无 | 口径说明、适用范围限制、待核实事项等 |

### 2.1 派生属性（代码中的只读属性）

| 属性名 | 返回值 | 用途 |
|---|---|---|
| `display_version` | `"{policy_name}（版本：{policy_version}）"`；`policy_version` 为空时返回 `policy_name` | 报告与界面统一展示"本测算采用 XX 政策版本"（规范 §35、§90） |

### 2.2 进入计算引擎后的登记方式

`CalculationEngine._register_parameters` 会为以下字段登记 `ParameterMeta`，
`source_type = SourceType.POLICY`（界面配色：**灰色**，规范 §144）：

| 登记的键名 | 取值字段 | 单位 | 来源名称 | 备注 |
|---|---|---|---|---|
| `policy.market_price` | `policy.market_price` | 元/kWh | `policy.source` | `policy.display_version` |
| `policy.mechanism_price` | `policy.mechanism_price` | 元/kWh | `policy.source` | `policy.display_version` |
| `policy.mechanism_volume_ratio` | `policy.mechanism_volume_ratio` | 小数 | `policy.source` | `policy.display_version` |
| `policy.green_energy_price` | `policy.green_energy_price` | 元/kWh | `policy.source` | `policy.display_version` |
| `policy.green_environmental_value` | `policy.green_environmental_value` | 元/kWh | `policy.source` | `policy.display_version` |
| `policy.version` | `policy.display_version` | — | `policy.source` | `policy.source_url` |

并且当政策存在时，`CalculationResult.notes` 会追加：
> "本测算采用政策：`{policy.display_version}`（来源：`{policy.source}`）。"

### 2.3 与电价参数的关系（避免重复口径）

| 情形 | 数据流 | 说明 |
|---|---|---|
| 用户手工输入电价 | `Project.tariff.*` → `resolve_tariff()` | `PolicyProfile` 可为 `None`；报告显示"未关联政策 Profile" |
| 电价来自政策 | `Project.policy.*` → 用户确认/覆盖 → `Project.tariff.*` → `resolve_tariff()` | **政策值不会自动写入 tariff**；必须经用户确认，保证"可修改、可追溯"（规范 §5） |
| 储能容量/辅助服务收益 | `Project.storage.annual_capacity_revenue` 等 | V1 由用户输入或政策模板给出，**不随年份增长**（规范 §44） |

> **禁止**：任何"政策值 → 直接覆盖 `tariff.*` 而不留痕迹"的隐式行为。所有政策值进入计算前都必须：
> ① 在界面上显示为灰色政策值；② 允许用户修改；③ 记录覆盖原因（`ParameterMeta.override_reason`）。

---

## 3. 湖北模型必须支持的要素清单

以下要素在湖北模板中**必须逐项建模或显式声明"不适用"**（规范 §34–§36、§89、§90）。

### 3.1 要素清单

| 序号 | 要素 | 必须支持的内容 | 对应 `PolicyProfile` 字段 | 对应 `TariffConfig` 字段 | 当前状态 |
|---|---|---|---|---|---|
| 1 | **市场化交易** | 上网电量进入电力市场、由市场形成价格；需记录市场电价的来源与口径 | `market_price`、`pricing_mechanism` | `tariff_mode = MARKET`、`market_price` | 字段已实现；湖北模板 Phase 8 未实现 |
| 2 | **分时电价** | 峰/平/谷电价与电量比例；峰平谷比例之和必须为 1 | — （分时电价属电价参数，不属于政策 Profile） | `peak_price`、`flat_price`、`valley_price`、`peak_ratio`、`flat_ratio`、`valley_ratio` | 字段与校验已实现 |
| 3 | **新能源市场化交易** | 新能源（光伏/风电）参与市场化交易的电量、电价机制 | `pricing_mechanism`、`market_price` | `tariff_mode`、`market_price` | 字段已实现 |
| 4 | **绿电价格** | 绿色电力交易价格 | `green_energy_price` | `green_energy_price` | 字段已实现 |
| 5 | **绿色环境价值** | 绿证/环境权益折算价值 | `green_environmental_value` | `green_environmental_value` | 字段已实现 |
| 6 | **机制电量** | 机制电量比例（占上网电量的比例） | `mechanism_volume_ratio` | —（用于收益拆分口径说明） | 字段已实现 |
| 7 | **市场电量** | 市场化交易电量占比（= 1 − 机制电量比例 − 其他） | 由 `mechanism_volume_ratio` 推导 | — | 需在 Phase 8 明确推导与披露口径 |

### 3.2 要素的收益拆分口径（Phase 8 实现要求）

```text
上网电量 = 机制电量 + 市场电量 + （V1 不单独建模的其他电量）

机制电量  =  上网电量 × mechanism_volume_ratio        → 按 mechanism_price 计价
市场电量  =  上网电量 × (1 − mechanism_volume_ratio)  → 按 market_price 计价
上网收益  =  机制电量 × mechanism_price + 市场电量 × market_price
          （若用户填写 tariff.export_price，则以用户输入为准，并记录覆盖原因）
```

- [ ] **Phase 8 待办**：明确"机制电量/市场电量"的拆分是否影响 `pv_export_revenue`，并在 `CalculationResult.notes` 中披露口径。
- [ ] **Phase 8 待办**：确认 V1 是否引入"绿电价格 + 绿色环境价值"叠加口径（当前 `TariffConfig` 有字段，但引擎未单独计入收益）。
- [ ] **Phase 8 待办**：为湖北模板补充"分时电价时段划分"的**说明性字段**（仅文本，不参与计算；V1 不做小时级仿真，规范 §37、§137）。

### 3.3 湖北模型**不做**的事（规范 §137、§138）

- [ ] 不内置任何具体电价数值、机制电量数值、补贴金额；
- [ ] 不做 8760 小时仿真与分时电量平衡（湖北分时电价仅以"峰平谷电量比例"近似）；
- [ ] 不做需量电费与容量电费（V2 候选，规范 §139）；
- [ ] 不做湖北各地市（武汉、宜昌、襄阳等）的差异化政策细分——V1 只到省级，地市差异由用户在备注中说明；
- [ ] 不自动联网更新政策（V3 候选，规范 §140）。

---

## 4. 湖北模板结构化占位示例（JSON）

> **强制约束**：下面示例中的**全部数值字段均为 `null` 或 `0`**。
> **严禁预填任何具体数值**——必须由用户按项目所在地现行政策填写（规范 §34、§89、§159）。
> 示例中的 `source` / `source_url` 只给出**已核实的官方发布页**作为填写指引，不代表该页面的数值可直接采用。

```json
{
  "policy_id": "HUBEI_NEW_ENERGY_TARIFF",
  "policy_name": "湖北省新能源上网电价政策（模板占位，数值待用户填写）",
  "policy_version": null,
  "effective_date": null,
  "expiry_date": null,
  "province": "湖北",
  "pricing_mechanism": "新能源上网电量通过市场化交易形成价格；机制电量与市场电量分别计价。具体机制、比例与价格以项目所在地现行政策文件为准，软件不预填任何数值。",
  "market_price": null,
  "mechanism_price": null,
  "mechanism_volume_ratio": null,
  "green_energy_price": null,
  "green_environmental_value": null,
  "source": null,
  "source_url": null,
  "notes": "本模板为占位模板：所有数值字段必须由用户按项目所在地现行政策填写，禁止预填具体数值。填写前请到官方发布页面核对文号、生效日期与适用范围。"
}
```

### 4.1 与代码类型的映射注意

| JSON 中的写法 | Pydantic 字段类型 | 载入后的结果 | 提示 |
|---|---|---|---|
| `null` | `date \| None` | `None` | `effective_date` / `expiry_date` 允许空 |
| `null` | `float`（`market_price` 等） | ⚠️ **Pydantic 校验失败**（这些字段不可为 `None`） | 模板载入时须先把 `null` 归一化为 `0.0`，或把 `null` 解释为"用户必须填写、当前未填写" |
| `null` | `str`（`source` 等） | ⚠️ **Pydantic 校验失败**（`str` 不可为 `None`） | 同上，须归一化为 `""` |

> **实现现状（实际代码的做法，优于把 `null` 直接塞进 `PolicyProfile`）**：
> 已实现的 `src/cenep/policy/template.py` 定义了 `PolicyTemplate`，其数值字段类型为 `float | None`：
> - `None` 表示**尚未填写**，与用户确实填写 `0` **严格区分**（避免报告里出现"机制电价 0 元/kWh"这种看起来像事实的假数据，规范 §91）；
> - `unfilled_numeric_fields()` / `unfilled_text_fields()` / `is_complete()` / `missing_description()` 提供"还缺什么"的中文提示；
> - `to_profile(allow_unfilled=False)` 在未填写完整时**默认拒绝转换**并抛出 `PolicyTemplateError`（携带 `fields` 列表，供界面定位）；
>   传 `allow_unfilled=True` 可强制转换，但会在 `notes` 中自动追加"【未填写警示】…已在测算中按 0 处理，不得视为正式政策数值"。
>
> 因此模板层 DTO 与 `PolicyProfile` 分离的方案已落地，**不要把 `null` 直接塞进 `PolicyProfile`**。

### 4.2 YAML 形式（等价，供人工审阅）

```yaml
# 湖北政策模板（占位）—— 数值字段一律为 null / 0，禁止预填具体数值
policy_id: HUBEI_NEW_ENERGY_TARIFF
policy_name: 湖北省新能源上网电价政策（模板占位，数值待用户填写）
policy_version: null
effective_date: null          # YYYY-MM-DD，用户按现行政策填写
expiry_date: null             # 未标注则留空
province: 湖北
pricing_mechanism: >-
  新能源上网电量通过市场化交易形成价格；机制电量与市场电量分别计价。
  具体机制、比例与价格以项目所在地现行政策文件为准，软件不预填任何数值。
market_price: null            # 元/kWh，必填，用户填写
mechanism_price: null         # 元/kWh，必填，用户填写
mechanism_volume_ratio: null  # 小数 0~1，必填，用户填写
green_energy_price: null      # 元/kWh，无此项则填 0 并注明"不适用"
green_environmental_value: null  # 元/kWh，无此项则填 0 并注明"不适用"
source: null                  # 政策文件名称/文号，用户填写
source_url: null              # 官方发布页 URL，用户填写，禁止编造
notes: >-
  本模板为占位模板：所有数值字段必须由用户按项目所在地现行政策填写，禁止预填具体数值。
```

### 4.3 模板填写检查清单（用户侧）

- [ ] 政策文号与名称是否与官方发布页一致？
- [ ] `effective_date` 是否早于或等于项目评价日期？
- [ ] `expiry_date` 是否晚于项目评价日期（若已失效必须更换版本）？
- [ ] `province` 是否为"湖北"，且项目所在地是否适用该省级文件？
- [ ] 数值字段是否已按现行政策填写，并已记录 `source` 与 `source_url`？
- [ ] 无法核实的字段是否已标记为"假设值"（黄色）？

---

## 5. 版本机制

### 5.1 版本记录的字段职责

| 字段 | 职责 | 示例（**占位，非真实数值**） |
|---|---|---|
| `policy_id` | 政策族标识，跨版本不变 | `"HUBEI_NEW_ENERGY_TARIFF"` |
| `policy_name` | 政策名称 | `"湖北省新能源上网电价政策"` |
| `policy_version` | 版本标识（建议用生效日期 `YYYY-MM-DD`，便于排序） | `"YYYY-MM-DD"` |
| `effective_date` | 该版本生效日期 | `YYYY-MM-DD` |
| `expiry_date` | 该版本失效日期；`None` 表示尚未失效 | `None` |
| `province` | 适用范围 | `"湖北"` |
| 参数（5 个数值字段） | 该版本下的政策参数 | 由用户填写 |
| `source` | 政策文件名称/文号 | 用户填写 |
| `source_url` | 官方发布页 URL | 用户填写 |
| `notes` | 备注、口径限制、待核实事项 | 文本 |

### 5.2 版本演进规则

| 规则 | 说明 |
|---|---|
| **只新增，不覆盖** | 新版本以新记录形式加入模板库，`policy_id` 相同、`policy_version` 与 `effective_date` 不同 |
| **不追溯修改** | 已发布过的版本记录不得修改参数值；如发现错误，新增"修订版"记录并在 `notes` 中说明 |
| **项目绑定快照** | `.nep` 文件保存项目计算时使用的 `PolicyProfile` 完整快照；模板库更新不影响已保存项目 |
| **显式升级** | 用户须在界面上主动选择"升级到新政策版本"，升级动作写入日志，并在报告中披露"本次测算采用政策版本 X（生效日期 D）" |
| **过期提示** | 当项目评价日期 > `expiry_date`，或评价日期 < `effective_date` 时，界面必须给出黄色警示，要求用户确认 |
| **版本对比** | V1 只展示单版本；多版本对比列 V2 候选（规范 §139） |

### 5.3 版本选择流程（ASCII）

```text
软件启动
   │
   ├─ 读取模板库（SQLite：政策模板表）
   │
   ├─ 按 project.basic_info.province == "湖北" 过滤候选版本
   │
   ├─ 按 project.basic_info.evaluation_date 落在 [effective_date, expiry_date] 内筛选"现行版本"
   │        │
   │        ├─ 命中唯一版本 ──→ 提示确认
   │        ├─ 命中多个版本 ──→ 列出让用户选择（默认取 effective_date 最新者）
   │        └─ 未命中       ──→ 提示"未找到适用于评价日期的现行政策版本，请手动填写或选择其他版本"
   │
   └─ 用户确认后 → Project.policy = 选定 PolicyProfile（快照存入 .nep）
```

### 5.4 版本存储建议（Phase 8）

| 存储位置 | 内容 | 说明 |
|---|---|---|
| SQLite 表 `policy_profiles` | 模板库全部版本，主键 `(policy_id, policy_version)` | 只追加，不更新已有行 |
| SQLite 表 `policy_selection_log` | 项目与政策版本的绑定记录 | 用于日志与审计 |
| `.nep` 文件 `project.policy` | 选中版本的完整快照 | 保证项目可离线复现 |
| Excel「政策依据」表 | 选中版本的字段 + `display_version` | 报告披露 |
| PDF「政策依据」章 | 同上 + 来源链接 | 报告披露 |

---

## 6. 湖北政策的官方来源（核实状态）

> **纪律**：以下条目**只作为"到哪里找现行政策"的指引**，**不代表其中的数值可被软件预填**。
> 软件**不内置**任何数值；用户必须自行到官方页面核对现行版本（规范 §34、§89）。

| 序号 | 文件名称 | 发布机构 | 核实状态 | 官方链接 |
|---|---|---|---|---|
| 1 | 省发改委 省能源局关于印发湖北省深化新能源上网电价市场化改革促进新能源高质量发展实施方案的通知 | 湖北省发展和改革委员会、湖北省能源局 | ✅已核实（官方发布页） | https://fgw.hubei.gov.cn/fbjd/zc/gfwj/gf/202508/t20250828_5757033.shtml |
| 2 | 国家发展改革委 国家能源局关于深化新能源上网电价市场化改革 促进新能源高质量发展的通知（发改价格〔2025〕136号） | 国家发展改革委、国家能源局 | ✅已核实 | https://www.ndrc.gov.cn/xxgk/zcfb/tz/202502/t20250209_1396066.html |
| 3 | 湖北省分时电价相关政策文件 | 湖北省发展和改革委员会 | ⚠️待核实（**具体文件名称、文号与发布页未从官方来源确认**；填写时须到湖北省发改委官网核实） | 待补：湖北省发展和改革委员会 https://fgw.hubei.gov.cn/ |
| 4 | 湖北省绿电交易 / 绿色环境价值相关政策 | 湖北省能源局、湖北电力交易中心 | ⚠️待核实 | 待补 |
| 5 | 湖北省机制电价/机制电量的具体结果公告 | 湖北省发展和改革委员会、湖北省能源局 | ⚠️待核实（**曾在非官方渠道见到具体数值，但未经官方页面核实，严禁写入软件**） | 待补 |

### 6.1 待核实清单（湖北）

- [ ] 核实湖北省级承接文件的**准确文号与发布日期**（仅确认了官方发布页，URL 中的 `202508` 表明约在 2025 年 8 月发布，**具体日期与文号待核**）；
- [ ] 核实湖北省现行分时电价的文件名称、文号、峰平谷时段划分（**仅用于填写指引，不写入软件**）；
- [ ] 核实湖北省绿电交易与绿色环境价值的政策依据；
- [ ] 核实湖北省机制电价、机制电量比例等参数口径（**数值一律由用户填写**）；
- [ ] 核实湖北各地市是否存在差异化政策（V1 只到省级，地市差异在备注中说明）。

### 6.2 引用规范

在软件与报告中引用湖北政策时，必须使用如下格式（示例）：

```text
来源：湖北省发展和改革委员会、湖北省能源局《省发改委 省能源局关于印发湖北省深化
      新能源上网电价市场化改革促进新能源高质量发展实施方案的通知》
      发布页：https://fgw.hubei.gov.cn/fbjd/zc/gfwj/gf/202508/t20250828_5757033.shtml
      核实状态：✅已核实（官方发布页）
      政策版本：<由用户填写的 policy_version>
      生效日期：<由用户填写的 effective_date>
```

> **不得**在报告或界面中出现"根据湖北政策，机制电价为 X 元/kWh"这类**未经用户确认的断言**。

---

## 7. 启动提示与报告展示文案

### 7.1 软件启动时的政策版本提示（规范 §35、§90）

```text
当前项目采用的政策版本为：{policy.display_version}
（省份：{policy.province}；生效日期：{policy.effective_date}）

请确认该版本是否为项目所在地的现行政策。

[ 确认为现行政策 ]   [ 更换政策版本 ]   [ 暂不关联政策，全部手工填写 ]
```

未关联政策时的提示：

```text
当前项目未关联政策 Profile。
本测算的全部电价参数均为用户输入，请自行核对现行政策与合同约定。
```

政策已过期时的提示（黄色警示）：

```text
当前项目采用的政策版本已于 {policy.expiry_date} 失效，
可能导致测算结果与现行政策不符。请更换为现行版本后重新计算。
```

### 7.2 报告中的"本测算采用 XX 政策版本"展示位

| 报告位置 | 展示内容 | 数据来源 |
|---|---|---|
| Excel「政策依据」表（第 12 张表） | 政策名称、政策版本、生效/失效日期、适用范围、价格机制、5 个数值字段、来源、来源链接、备注；最后一行"报告披露：本测算采用政策：`{display_version}`" | `project.policy.*` |
| Excel「参数来源」表（第 13 张表） | `policy.*` 行，来源类型列显示"政策参数"（灰色填充） | `CalculationResult.parameter_sources` |
| PDF「测算条件」章 | `policy.display_version` + `effective_date` + 省级适用范围 | `project.policy.*` |
| PDF「政策依据」章 | 完整政策清单 + 核实状态 + 来源链接 | `project.policy.*` + 本文件 |
| PDF「测算说明」章 | "本测算采用政策：`{display_version}`（来源：`{source}`）。" | `CalculationResult.notes` 中由引擎自动追加的语句 |

引擎已实现的语句（`_calculate_core` 末尾）：

```text
本测算采用政策：{policy.display_version}（来源：{policy.source}）。
```

> 该语句只在 `project.policy is not None and project.policy.policy_name` 时追加；
> 未关联政策时报告必须显式声明"未关联政策 Profile"（规范 §35）。

### 7.3 报告免责声明（必须原文出现，规范 §111）

> **本软件用于新能源项目开发阶段的前期经济测算和投资决策辅助，不替代项目正式可行性研究、工程设计、工程造价咨询、审计、税务咨询、金融机构审查及政府审批文件。**
>
> **电价、市场交易、税务、储能收益等政策参数具有时效性，应以项目实施时的最新正式政策及实际合同为准。**

---

## 8. Phase 8 实现任务清单（含现状）

- [x] 新建 `src/cenep/policy/` 包：`__init__.py`、`template.py`（模板 DTO 与完整性校验）、`hubei.py`（湖北模板占位定义 + 启动提示）、`store.py`（模板存取）
- [x] 定义模板 DTO，支持"未填写"状态（`PolicyTemplate`，`None` ≠ `0.0`，见 4.1）
- [x] 湖北模板**不预填任何具体数值**（`HUBEI_TEMPLATE` 全部数值字段为 `None`）
- [x] 启动提示文案生成（`describe_startup_notice(project)`，规范 §90）
- [x] 编写测试 `tests/test_policy.py`（未填写状态、拒绝转换、警示写入 `notes`、启动提示文案） —— ✅已建立，⬜ 待全绿
- [x] Excel「政策依据」表与 PDF「政策依据」章的填充（依赖 Phase 6/7，已实现）
- [ ] 在 SQLite（`src/cenep/infrastructure/db.py`）中新增 `policy_profiles` 与 `policy_selection_log` 表（版本历史持久化）
- [ ] 实现版本匹配逻辑（按 `province` + `evaluation_date` 筛选现行版本）
- [ ] 实现启动提示对话框与政策已过期的**黄色警示**（当前只有文案函数，尚无 GUI）
- [ ] 实现"政策值 → 电价页灰色预填（可修改、需记录覆盖原因）"的交互（依赖 Phase 5 GUI）
- [ ] 编写测试：政策未关联时的报告披露语句（部分已由 Excel/PDF 测试覆盖）
- [ ] **验收标准**：模板库中不得存在任何预填的具体数值（**已满足**）；任何政策数值都必须能追溯到用户填写记录与官方来源链接

---

## 9. 关联文档

| 文档 | 关系 |
|---|---|
| `src/cenep/domain/models.py` | `PolicyProfile` 的真实字段定义（本文档第 2 节的唯一权威来源） |
| `src/cenep/domain/provenance.py` | `SOURCE_PRIORITY`、`ParameterMeta`、`SourceType.ui_color` |
| `src/cenep/calculation/engine.py` | 政策参数的登记逻辑与 `notes` 追加语句 |
| `src/cenep/calculation/revenue.py` | 电价解析与收益口径（禁止硬编码政策数值） |
| `REGULATIONS_AND_POLICY.md` | 法规与政策依据的分层清单与核实状态 |
| `REPORT_SPEC.md` | Excel「政策依据」表与 PDF「政策依据」章的字段设计 |
| `src/cenep/policy/template.py` | `PolicyTemplate`（未填写状态与转换规则）的现有实现 |
| `src/cenep/policy/hubei.py` | 湖北占位模板与启动提示文案的现有实现 |
| `ROADMAP.md` | Phase 8 交付物与验收标准 |
