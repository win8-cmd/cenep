"""湖北政策模板（规范 §34–§36、§89、§90）。

**强制约束：本模板不预填任何具体数值。**
所有数值字段均为 ``None``（未填写），必须由用户按项目所在地现行政策填写。
`source_url` 只给出**已核实的官方发布页**作为填写指引，不代表该页面的数值可直接采用。
"""

from __future__ import annotations

from ..domain.models import Project
from .template import PolicyTemplate

#: 湖北新能源电价政策模板（占位，数值待用户填写）
HUBEI_TEMPLATE = PolicyTemplate(
    policy_id="HUBEI_NEW_ENERGY_TARIFF",
    policy_name="湖北省新能源上网电价政策（模板占位，数值待用户填写）",
    policy_version=None,
    effective_date=None,
    expiry_date=None,
    province="湖北",
    pricing_mechanism=(
        "新能源上网电量通过市场化交易形成价格；机制电量与市场电量分别计价。"
        "具体机制、比例与价格以项目所在地现行政策文件为准，软件不预填任何数值。"
    ),
    market_price=None,
    mechanism_price=None,
    mechanism_volume_ratio=None,
    green_energy_price=None,
    green_environmental_value=None,
    source=None,
    source_url=None,
    notes=(
        "本模板为占位模板：所有数值字段必须由用户按项目所在地现行政策填写，禁止预填具体数值。"
        "填写前请到官方发布页面核对文号、生效日期与适用范围。"
        "可参考的官方发布页：国家发展改革委《关于深化新能源上网电价市场化改革促进新能源高质量发展的通知》"
        "（发改价格〔2025〕136号）、国家发展改革委 国家能源局《关于完善发电侧容量电价机制的通知》、"
        "国家发展改革委办公厅 国家能源局综合司《关于进一步推动新型储能参与电力市场和调度运用的通知》"
        "（发改办运行〔2022〕475号）。上述页面为政策出处，不是数值来源。"
    ),
)


def describe_startup_notice(project: Project) -> str:
    """政策版本确认提示（规范 §90）。

    软件启动/打开项目时展示：当前项目采用的政策版本为 XXXX，请确认是否为项目所在地现行政策。
    """
    policy = project.policy
    if policy is None or not policy.policy_name:
        return "当前项目未关联政策模板，电价与政策性参数完全取决于用户输入，请确认是否已按现行政策填写。"
    version = policy.policy_version or "未标注版本"
    expiry = policy.expiry_date.isoformat() if policy.expiry_date else "未标注失效日期"
    return (
        f"当前项目采用的政策版本为 {version}（{policy.policy_name}），失效日期：{expiry}。"
        "请确认是否为项目所在地现行政策。"
    )
