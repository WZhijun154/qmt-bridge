import time
from xtquant import xtdata

def main():
    print("====== QMT 数据下载与读取测试 ======")
    
    # ⚠️ 重要前提：请确保你的 QMT 客户端此刻正在后台运行并处于登录状态！

    # 1. 设置参数
    stock_list = ['000001.SZ', '600000.SH']  # 测试标的：平安银行, 浦发银行
    period = '1d'                            # 获取日线数据
    start_date = '20231214'
    end_date = '20231231'

    print(f"准备下载数据 | 周期: {period} | 时间: {start_date} - {end_date}")
    print(f"股票池: {stock_list}")
    print("-" * 40)

    # 2. 下载数据 (从券商服务器下载到本地 userdata_mini/datadir 目录)
    # 第一次下载可能会花几秒钟，以后有了本地缓存就会瞬间完成
    for code in stock_list:
        print(f"正在向服务器请求 {code} 的历史数据...", end=" ", flush=True)
        # 这是一个阻塞函数，下载完成才会往下走
        xtdata.download_history_data(
            stock_code=code, 
            period=period, 
            start_time=start_date, 
            end_time=end_date
        )
        print("下载完成！")

    print("-" * 40)
    print("所有数据下载完毕，正在从本地加载到内存...")

    # 3. 读取数据 (从本地硬盘读取为 Pandas DataFrame 格式)
    data_dict = xtdata.get_market_data(
        field_list=['open', 'high', 'low', 'close', 'volume', 'amount'],
        stock_list=stock_list,
        period=period,
        start_time=start_date,
        end_time=end_date,
        count=-1,                # -1 表示获取该时间段内所有数据
        dividend_type='front',   # 强烈建议回测使用 'front' 前复权
        fill_data=True           # 停牌日是否用前一日数据补齐
    )

    # 4. 验证数据获取结果
    if 'close' in data_dict and not data_dict['close'].empty:
        print("\n✅ 数据读取成功！")
        print("以下是收盘价 (前复权) 的最后 5 个交易日数据：")
        close_df = data_dict['close']
        print(close_df.tail())
    else:
        print("\n❌ 数据读取失败，请检查：")
        print("1. 你的 QMT 客户端是否已经启动并且登录成功？")
        print("2. 账户是否具有行情查看权限？")

if __name__ == '__main__':
    main()
