#!/usr/bin/python

import os
import sys
import time
import logging
import asyncio
import numpy as np
import ccxt.async as ccxt
import bz_conf


# 注：只有少数交易所，支持作空
# 假定是保证金交易，2个交易所，都是可以作空

# usd == usdt，没有usd 的交易所，自动更换成 usdt

# 统计套利搬砖
class calc_stat_arb():
    def __init__(self, symbol, exchange1_data, exchange2_data, db_symbol):
        self.symbol = symbol         # BTC/USD
        self.ex1 = exchange1_data
        self.ex2 = exchange2_data
        self.db = db_symbol

        self.spread1List = []       # exchange1_buy1 - exchange2_sell1
        self.spread2List = []       # exchange2_buy1 - exchange1_sell1

        self.sma_window_size = 3600        # 多少根k线, 计算的时间跨度. 默认1秒1k线，计算1小时
        self.spread1_mean = None        # 均值
        self.spread1_stdev = None        # 方差
        self.spread2_mean = None
        self.spread2_stdev = None
        
        self.spread1_open_condition_stdev_coe = 2        # 价格超过方差多少倍，下单
        self.spread2_open_condition_stdev_coe = 2
        self.spread1_close_condition_stdev_coe = 0.3        # 价格小于方差多少倍，可以考虑调整仓位
        self.spread2_close_condition_stdev_coe = 0.3

        # 0: no position
        # 1: long spread1(sell exchange1, buy exchange2)
        # 2: long spread2(buy exchange1, sell exchange2)
        self.current_position_direction = 0  

        self.spread1_pos_qty = 0
        self.spread2_pos_qty = 0

        # 交易相关参数
        self.spread_entry_threshold = 0.0200    # 价格相差达到 % ，才下单搬砖，必需大于手续费
        self.order_book_ratio = 0.25  # 并不是所有挂单都能成交，每次预计能吃到的盘口深度的百分比
        self.max_exposure_ratio = 0.1    # 每次下单，可以下单的数量，占最大数量的多少 %

        # log
        self.logger = logging.getLogger(__name__)
        self.formatter = logging.Formatter('%(asctime)s %(levelname)-8s: %(message)s')
        self.file_handler = logging.FileHandler(bz_conf.log_dir + "/mm.log")
        self.file_handler.setFormatter(self.formatter)
        self.console_handler = logging.StreamHandler(sys.stdout)
        self.console_handler.formatter = self.formatter
        self.logger.addHandler(self.file_handler)
        self.logger.addHandler(self.console_handler)
        self.logger.setLevel(logging.INFO)
        

    # 向list添加1个元素，当list长度大于某个定值时，会将前面超出的部分删除
    def add_to_list(self, dest_list, element):
        if self.sma_window_size == 1:
            return [element]
        while len(dest_list) > self.sma_window_size:
            del (dest_list[0])
        dest_list.append(element)
        return dest_list

    # 计算移动平均
    def calc_sma_and_deviation(self):
        self.spread1_mean = np.mean(self.spread1List[-1 * self.sma_window_size:])
        self.spread1_stdev = np.std(self.spread1List[-1 * self.sma_window_size:])
        self.spread2_mean = np.mean(self.spread2List[-1 * self.sma_window_size:])
        self.spread2_stdev = np.std(self.spread2List[-1 * self.sma_window_size:])

    # 判断开仓、平仓
    def calc_position_direction(self):
        # 没有仓位
        if self.current_position_direction == 0:
            if (self.spread1List[-1] - self.spread1_mean) / self.spread1_stdev > self.spread1_open_condition_stdev_coe:
                return 1
            elif (self.spread2List[-1] - self.spread2_mean) / self.spread2_stdev > self.spread2_open_condition_stdev_coe:
                return 2
        # 已有仓位, 方向1 (sell exchange1, buy exchange2)
        elif self.current_position_direction == 1:
            if (self.spread1List[-1] - self.spread1_mean) / self.spread1_stdev > self.spread1_open_condition_stdev_coe:
                # 还是方向1，可以继续判断，是否加仓
                return 1
            if (self.spread2List[-1] - self.spread2_mean) / self.spread2_stdev > -self.spread1_close_condition_stdev_coe:
                # 平仓
                return 2
        # 已有仓位, 方向2 (buy exchange1, sell exchange2)
        elif self.current_position_direction == 2:
            if (self.spread2List[-1] - self.spread2_mean) / self.spread2_stdev > self.spread2_open_condition_stdev_coe:
                # 还是方向2，可以继续判断，是否加仓
                return 2
            if (self.spread1List[-1] - self.spread1_mean) / self.spread1_stdev > -self.spread2_close_condition_stdev_coe:
                # 平仓
                return 1
        # 没有达到搬砖条件
        return 0

    # 刚启动时，没有k线数据，尝试从数据库中取数据
    def fetch_history_data_from_db(self):
        if len(self.spread1List) > 0:
            return
        # 取最近的 sma_window_size 个k线
        from_dt = int(time.time()) - self.sma_window_size * 2
        for i in range(from_dt, from_dt + self.sma_window_size):
            self.fetch_data_from_db(i)
    
    # 从 db 中取1条指定时间的数据
    def fetch_data_from_db(self, sql_con_timestamp):
        rows1 = self.db.fetch_one(self.ex1.ex.id, sql_con_timestamp)
        rows2 = self.db.fetch_one(self.ex2.ex.id, sql_con_timestamp)
        if rows1 is None or rows2 is None:
            return
        if len(rows1) >= 1:
            for row1 in rows1:
                self.ex1.buy_1_price = row1[1]
                self.ex1.sell_1_price = row1[2]
                self.ex1.order_book_time = sql_con_timestamp
        if len(rows2) >= 1:
            for row2 in rows2:
                self.ex2.buy_1_price = row2[1]
                self.ex2.sell_1_price = row2[2]
                self.ex2.order_book_time = sql_con_timestamp
        if self.ex1.buy_1_price <= 0 or self.ex1.sell_1_price <= 0 or self.ex2.buy_1_price <= 0 or self.ex2.sell_1_price <= 0:
            return
        spread1 = round(self.ex1.buy_1_price, self.ex1.market['precision']['price']) - round(self.ex2.sell_1_price, self.ex2.market['precision']['price'])
        spread1 = round(spread1,  max(self.ex1.market['precision']['price'], self.ex2.market['precision']['price']))
        spread2 = round(self.ex2.buy_1_price, self.ex2.market['precision']['price']) - round(self.ex1.sell_1_price, self.ex1.market['precision']['price'])
        spread2 = round(spread2,  max(self.ex1.market['precision']['price'], self.ex2.market['precision']['price']))
        self.spread1List = self.add_to_list(self.spread1List, spread1)
        self.spread2List = self.add_to_list(self.spread2List, spread2)

    # 检查是否支持 symbol，确定最小交易量，费用
    async def load_markets(self):
        if not await self.ex1.load_markets():
            return False
        if not await self.ex2.load_markets():
            return False
        return True

    #  查看余额
    async def fetch_balance(self):
        if not await self.ex1.fetch_balance():
            return False
        if not await self.ex2.fetch_balance():
            return False
        return True

    # 取深度信息
    async def fetch_order_book(self):
        if not await self.ex1.fetch_order_book():
            return False
        if not await self.ex2.fetch_order_book():
            return False

        # 加入数据列表
        spread1 = round(self.ex1.buy_1_price, self.ex1.market['precision']['price']) - round(self.ex2.sell_1_price, self.ex2.market['precision']['price'])
        spread1 = round(spread1,  max(self.ex1.market['precision']['price'], self.ex2.market['precision']['price']))
        spread2 = round(self.ex2.buy_1_price, self.ex2.market['precision']['price']) - round(self.ex1.sell_1_price, self.ex1.market['precision']['price'])
        spread2 = round(spread2,  max(self.ex1.market['precision']['price'], self.ex2.market['precision']['price']))
        self.spread1List = self.add_to_list(self.spread1List, spread1)
        self.spread2List = self.add_to_list(self.spread2List, spread2)
        return True

    def init_spread_qty(self):
        # exc1 有空单
        if self.ex1.balance_symbol1['used'] > 0:
            self.spread1_pos_qty = self.ex1.balance_symbol1['used']

        # exc2 有空单
        if self.ex2.balance_symbol1['used'] > 0:
            self.spread2_pos_qty = self.ex2.balance_symbol1['used']

        if self.spread1_pos_qty > 0:
            self.current_position_direction = 1
        elif self.spread2_pos_qty > 0:
            self.current_position_direction = 2
        else:
            self.current_position_direction = 0


    def check_fees(self):
        p1 = abs((self.spread1List[-1]) - (self.spread1_mean))
        if (p1 / self.ex1.sell_1_price) > (self.ex1.long_fee  + self.ex2.short_fee) * 3:
            return True
        return False
        '''
        fee_total = self.ex1.long_fee +  self.ex1.short_fee + self.ex2.long_fee + self.ex2.short_fee
        profit1 = self.spread1List[-1] - (self.spread2_mean - self.spread2_stdev * self.spread2_open_condition_stdev_coe)
        profit2 = self.spread1List[-1] - (self.spread2_mean - self.spread2_stdev * self.spread2_open_condition_stdev_coe)
        if abs(profit1)/self.ex1.buy_1_price + abs(profit2)/self.ex2.sell_1_price  >  fee_total
            return True
        '''

    '''
    # 订单结构
    {
        'id': str(order['id']),
        'timestamp': timestamp,
        'datetime': self.iso8601(timestamp),
        'status': status,
        'symbol': symbol,
        'type': order['ord_type'],
        'side': order['side'],
        'price': float(order['price']),
        'amount': float(order['volume']),
        'filled': float(order['executed_volume']),
        'remaining': float(order['remaining_volume']),
        'trades': None,
        'fee': None,
        'info': order,
    }
    '''
    # 先执行第1个交易所的下单，等交易结果
    # 假定交易所，支持保证金交易，但是只用1倍保证金
    # 有交易所，只支持 limit order 
    # 不支持, 失败, 未全部成交   ????????????
    # 异常，网络问题，报警   ?????????????
    async def do_order_spread1(self, todo_qty):
        exc1_order_ret = await self.ex1.ex.create_order(self.ex1.symbol, 'market', 'sell', todo_qty, None, {'leverage': 1})
        # 订单没有成交全部，剩下的订单取消
        if exc1_order_ret['remaining'] > 0:
            self.ex1.ex.cancel_order(exc1_order_ret['id'])
        ok_qty = exc1_order_ret['filled']
        if ok_qty <= 0:    # 订单完全没有成交，等待下一次机会
            return
        # 第1交易所已下单成功，第2交易所下单
        exc2_order_ret = await self.ex2.ex.create_order(self.ex2.symbol, 'market', 'buy', ok_qty, None, {'leverage': 1})
        while exc2_order_ret['remaining'] > 0:
            exc2_order_ret = await self.ex2.ex.create_order(self.ex2.symbol, 'market', 'buy', exc2_order_ret['remaining'], None, {'leverage': 1})

        if self.current_position_direction == 0 or self.current_position_direction == 1:
            self.spread1_pos_qty += ok_qty
        elif self.current_position_direction == 2:
            self.spread2_pos_qty -= ok_qty



    async def do_order_spread2(self, todo_qty):
        exc2_order_ret = await self.ex2.ex.create_order(self.ex2.symbol, 'market', 'sell', todo_qty, None, {'leverage': 1})
        # 订单没有成交全部，剩下的订单取消
        if exc2_order_ret['remaining'] > 0:
            self.ex2.ex.cancel_order(exc2_order_ret['id'])
        ok_qty = exc2_order_ret['filled']
        if ok_qty <= 0:    # 订单完全没有成交，等待下一次机会
            return
        # 第2交易所已下单成功，第1交易所下单
        exc1_order_ret = await self.ex1.ex.create_order(self.ex1.symbol, 'market', 'buy', ok_qty, None, {'leverage': 1})
        while exc1_order_ret['remaining'] > 0:
            exc1_order_ret = await self.ex1.ex.create_order(self.ex1.symbol, 'market', 'buy', exc1_order_ret['remaining'], None, {'leverage': 1})

        if self.current_position_direction == 0 or self.current_position_direction == 2:
            self.spread2_pos_qty += ok_qty
        elif self.current_position_direction == 1:
            self.spread1_pos_qty -= ok_qty

    def log(self, position_direction):
        str_bz = '\n' + self.symbol + ';bz=' + str(position_direction) + '\n'     \
            + self.ex1.symbol + ';ex1=' + self.ex1.ex.id + ',bid=' + str(self.ex1.buy_1_price) + ',ask=' + str(self.ex1.sell_1_price) + '\n'    \
            + self.ex2.symbol + ';ex2=' + self.ex2.ex.id + ',bid=' + str(self.ex2.buy_1_price) + ',ask=' + str(self.ex2.sell_1_price) + '\n'
        if position_direction == 1:
            str_bz = str_bz + self.symbol + ';sell=' + str(self.ex1.buy_1_price) + ';buy=' + str(self.ex2.sell_1_price) + '\n'
        if position_direction == 2:
            str_bz = str_bz + self.symbol + ';buy=' + str(self.ex1.sell_1_price) + ';sell=' + str(self.ex2.buy_1_price)
        self.logger.info(str_bz)

    async def do_it(self):
        self.fetch_history_data_from_db()
        if not await self.load_markets():
            return
        if not await self.fetch_balance():
            return
        self.init_spread_qty()
        while True:
            # 余额，仓位
            if not await self.fetch_balance():
                continue

            # 取订单深度信息
            if not await self.fetch_order_book():
                continue

            # 数据不足，不计算, 等待足够的数据
            if len(self.spread1List) < self.sma_window_size or len(self.spread2List) < self.sma_window_size:
                #self.logger.info(self.symbol + ',' + self.ex1.ex.id + ',' + self.ex2.ex.id + ';data len=' + str(len(self.spread1List)))
                continue
            
            # 超过 3 秒钟没有收到新数据，等新数据
            cur_t = int(time.time())
            timeout_warn = 3
            if self.ex1.order_book_time < cur_t - timeout_warn or self.ex2.order_book_time < cur_t - timeout_warn:
                continue

            # 计算方差
            self.calc_sma_and_deviation()

            '''
            # 价格回归平均值
            # 是否需要调整仓位 
            if abs(self.spread1List[-1] - self.spread1_mean) / self.spread1_stdev < self.spread1_close_condition_stdev_coe:
                continue
            if abs(self.spread2List[-1] - self.spread2_mean) / self.spread2_stdev < self.spread2_close_condition_stdev_coe:
                continue
            '''

            # 检查是否有机会
            todo_qty = 0.0
            position_direction = self.calc_position_direction()
            if position_direction == 0:
                # 没有交易信号，继续=
                continue
            elif position_direction == 1:
                self.log(position_direction)
                if self.current_position_direction == 0:  # 当前没有持仓
                    if not self.check_fees():
                        continue
                    # 计算第1次开仓数量
                    todo_qty = min(self.ex1.buy_1_quantity, self.ex2.sell_1_quantity) * self.order_book_ratio
                elif self.current_position_direction == 1:  # 当前long spread1
                    if not self.check_fees():
                        continue
                    # 已有仓位，计算加仓数量
                    todo_qty = min(self.ex1.buy_1_quantity, self.ex2.sell_1_quantity) * self.order_book_ratio
                elif self.current_position_direction == 2:  # 当前long spread2
                    # 另一个方向有仓位，计算可以减仓的数量
                    depth_qty = min(self.ex1.buy_1_quantity, self.ex2.sell_1_quantity) * self.order_book_ratio
                    todo_qty = min(depth_qty, self.spread2_pos_qty)
                    



                # 最大可以开多少仓位
                # ?????????????? 需要调试  ????????????????
                # free used 什么意思
                can_op_qty_1 = self.ex1.balance_symbol1['free']
                can_op_qty_2 = self.ex2.balance_symbol2['free'] / self.ex2.sell_1_price
                if self.ex1.support_short:
                    can_op_qty_1 = can_op_qty_1 + self.ex1.balance_symbol2['free'] / self.ex1.buy_1_price
                if self.ex2.support_short:
                    can_op_qty_2 = can_op_qty_2 + self.ex2.balance_symbol1['used']
                qty_max = min(can_op_qty_1, can_op_qty_2)
                # 每次最多只能买的数量, 暂定为最大交易量的  1/10
                qty_by_cash_one = qty_max / 10
                todo_qty = min(todo_qty, qty_by_cash_one)
                
                # 计算出的交易量 < 交易所要求的最小量
                # 无法下单，忽略这次机会
                if todo_qty < self.ex1.market['limits']['amount']['min'] or todo_qty < self.ex2.market['limits']['amount']['min']:
                    continue
                    
                await self.do_order_spread1(todo_qty)

            elif position_direction == 2:
                self.log(position_direction)
                if self.current_position_direction == 0:  # 当前没有持仓
                    if not self.check_fees():
                        continue
                    # 计算第1次开仓数量
                    todo_qty = min(self.ex2.buy_1_quantity, self.ex1.sell_1_quantity) * self.order_book_ratio
                elif self.current_position_direction == 2:  # 当前long spread2
                    if not self.check_fees():
                        continue
                    # 已有仓位，计算加仓数量
                    todo_qty = min(self.ex2.buy_1_quantity, self.ex1.sell_1_quantity) * self.order_book_ratio
                elif self.current_position_direction == 1:  # 当前long spread1
                    # 另一个方向有仓位，计算可以减仓的数量
                    depth_qty = min(self.ex2.buy_1_quantity, self.ex1.sell_1_quantity) * self.order_book_ratio
                    todo_qty = min(depth_qty, self.spread1_pos_qty)

                # 假定是保证金交易，2个交易所，都是持有 usd

                # 最大可以开多少仓位
                # ?????????????? 需要调试  ????????????????
                # free used 什么意思
                # 作空的仓位，是什么
                can_op_qty_1 = self.ex1.balance_symbol1['used'] + self.ex1.balance_symbol2['free'] / self.ex1.sell_1_price
                can_op_qty_2 = self.ex2.balance_symbol1['free'] + self.ex2.balance_symbol2['free'] / self.ex2.buy_1_price
                qty_max = min(can_op_qty_1, can_op_qty_2)
                # 每次最多只能买的数量, 暂定为最大交易量的  1/10
                qty_by_cash_one = qty_max / 10
                todo_qty = min(todo_qty, qty_by_cash_one)
                
                # 计算出的交易量 < 交易所要求的最小量
                # 无法下单，忽略这次机会
                if todo_qty < self.ex1.market['limits']['amount']['min'] or todo_qty < self.ex2.market['limits']['amount']['min']:
                    continue

                await self.do_order_spread2(todo_qty)

            if self.spread1_pos_qty > 0:
                self.current_position_direction = 1
            elif self.spread2_pos_qty > 0:
                self.current_position_direction = 2
            else:
                self.current_position_direction = 0
                

