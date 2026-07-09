"""表格数据路由模块 /api/tabular/*。

提供通用表格数据的查询端点，可用于查询各种命名数据表。
底层调用 xtquant.xtdata 的数据接口，包括：
- xtdata.get_financial_data()      — 按表名获取表格数据
- xtdata.get_financial_table_list() — 获取可用数据表列表
"""

from fastapi import APIRouter, Query
from xtquant import xtdata

from ..helpers import _numpy_to_python

try:
    from xtquant import xtbson as _BSON_
except ImportError:
    import bson as _BSON_

router = APIRouter(prefix="/api/tabular", tags=["tabular"])


@router.get("/data")
def get_tabular_data(
    table_name: str = Query(..., description="表名"),
    stocks: str = Query("", description="股票代码列表，逗号分隔"),
    start_time: str = Query("", description="开始时间"),
    end_time: str = Query("", description="结束时间"),
):
    """按表名查询表格数据。

    通用数据查询接口，通过指定表名查询对应的结构化数据。

    Args:
        table_name: 数据表名称。
        stocks: 逗号分隔的股票代码列表，为空查询全部。
        start_time: 开始时间。
        end_time: 结束时间。

    Returns:
        table: 查询的表名。
        data: 表格数据。

    底层调用: xtdata.get_financial_data(stock_list, table_list=[table_name], ...)
    """
    # 将逗号分隔的代码字符串解析为列表，为空则传空列表
    stock_list = [s.strip() for s in stocks.split(",") if s.strip()] if stocks else []
    raw = xtdata.get_financial_data(stock_list, table_list=[table_name], start_time=start_time, end_time=end_time)
    return {"table": table_name, "data": _numpy_to_python(raw)}


@router.get("/tables")
def list_tables():
    """列出所有可用的数据表名称。

    Returns:
        tables: 可用数据表名称列表。

    底层调用: xtdata.get_financial_table_list()
    """
    try:
        tables = xtdata.get_financial_table_list()
        return {"tables": _numpy_to_python(tables)}
    except Exception:
        # 接口不可用时返回空列表
        return {"tables": []}


@router.get("/formula")
def get_tabular_formula(
    stocks: str = Query(..., description="股票代码列表，逗号分隔"),
    fields: str = Query(..., description="字段列表，逗号分隔，格式为 表名.字段名，如 Balance.total_assets"),
    period: str = Query("1d", description="K线周期"),
    start_time: str = Query("", description="开始时间"),
    end_time: str = Query("", description="结束时间"),
):
    """按公式表格查询数据。

    fields 需为 "表名.字段名" 格式（如 Balance.total_assets），
    xtdata 内部会按表名分组批量调用公式引擎。返回值是 BSON 编码的记录列表，
    这里解码为 JSON 友好的记录格式。

    Args:
        stocks: 逗号分隔的股票代码列表。
        fields: 逗号分隔的字段列表，格式为 表名.字段名。
        period: K 线周期。
        start_time: 开始时间。
        end_time: 结束时间。

    Returns:
        data: 解码后的表格数据记录列表。

    部分 miniQMT 客户端版本未实现该接口，此时返回 error 字段而非 500。

    底层调用: xtdata.get_tabular_formula(codes, fields, period, start_time, end_time)
    """
    stock_list = [s.strip() for s in stocks.split(",") if s.strip()]
    field_list = [f.strip() for f in fields.split(",") if f.strip()]
    try:
        raw = xtdata.get_tabular_formula(stock_list, field_list, period, start_time, end_time)
    except Exception as e:
        return {"error": str(e)}
    decoded = [_numpy_to_python(_BSON_.BSON.decode(item)) for item in raw] if raw else []
    return {"data": decoded}
